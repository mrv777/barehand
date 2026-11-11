"""Where Barehand finds its machine, and where it keeps state.

Precedence for the QMP endpoint (first hit wins):
  1. env  ``BAREHAND_QMP``                       (``host:port`` or ``unix:/path``)
  2. Hermes setting ``plugins.entries.barehand.settings.qmp`` in config.yaml
  3. the state file written by ``hermes barehand up`` for the selected machine
     (``<data dir>/machines/<name>/machine.json``)

Data dir: env ``BAREHAND_HOME`` > Hermes' per-plugin data dir (``plugins.plugin_storage.plugin_data_dir``,
``<hermes home>/plugin-data/barehand``). Tests and experiments set ``BAREHAND_HOME`` to a scratch dir.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

PLUGIN_NAME = "barehand"
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
RUNNING_OK = frozenset({"running", "suspended"})   # VM states in which input and capture make sense
DEFAULT_MACHINE = "default"


class ConfigError(ValueError):
    """Bad user configuration; the message is user-facing."""


def data_dir() -> Path:
    """Persistent plugin data dir (created on first use)."""
    override = os.environ.get("BAREHAND_HOME")
    if override:
        root = Path(override).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        return root
    try:
        from plugins.plugin_storage import plugin_data_dir
        return plugin_data_dir(PLUGIN_NAME)
    except Exception:
        # Not running inside Hermes (standalone CLI use): mirror the same layout under HERMES_HOME.
        home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
        root = home / "plugin-data" / PLUGIN_NAME
        root.mkdir(parents=True, exist_ok=True)
        return root


MACHINE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def machine_dir(name: str = DEFAULT_MACHINE) -> Path:
    if not isinstance(name, str) or not MACHINE_NAME.fullmatch(name):
        raise ConfigError(f"invalid machine name {str(name)[:80]!r}: use 1-64 letters, digits, '.', '_' or '-', "
                          "starting with a letter or digit")
    return data_dir() / "machines" / name


def state_path(name: str = DEFAULT_MACHINE) -> Path:
    return machine_dir(name) / "machine.json"


def read_state(name: str = DEFAULT_MACHINE) -> Optional[Dict[str, Any]]:
    path = state_path(name)  # may raise ConfigError for a bad machine name: let the caller report it
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def write_state(name: str, state: Mapping[str, Any]) -> None:
    path = state_path(name)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps(dict(state), indent=2))
    os.replace(tmp, path)


def machine_arch(name: str = DEFAULT_MACHINE) -> Optional[str]:
    """Guest architecture a machine's disk belongs to. Kept in its own file so it survives ``down`` (which
    removes the state file but keeps the disk). A disk with no record predates architecture support: x86_64."""
    wd = machine_dir(name)
    try:
        value = (wd / "arch").read_text().strip()
        if value:
            return value
    except OSError:
        pass
    return "x86_64" if (wd / "disk.qcow2").exists() else None


def set_machine_arch(name: str, arch: str) -> None:
    wd = machine_dir(name)
    wd.mkdir(parents=True, exist_ok=True, mode=0o700)
    (wd / "arch").write_text(arch + "\n")


def clear_state(name: str = DEFAULT_MACHINE) -> None:
    try:
        state_path(name).unlink()
    except OSError:
        pass


def pid_alive(pid: Any) -> bool:
    """Is a process with this pid running? POSIX only: on Windows ``os.kill(pid, 0)`` would terminate the
    process, and Windows is unsupported in v0, so this answers False there instead of risking it."""
    if os.name != "posix" or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def process_command(pid: int) -> str:
    """Command line of a process ('' if unknown)."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return fh.read().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        pass
    try:
        out = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=5,
                             env={"PATH": "/bin:/usr/bin", "LC_ALL": "C"})
        return out.stdout.strip()
    except Exception:
        return ""


def is_our_qemu(state: Optional[Mapping[str, Any]]) -> bool:
    """True only if the state's pid is a live process whose command line is the QEMU Barehand started for
    this machine (guards against pid reuse after a crash or reboot)."""
    if not state or not pid_alive(state.get("pid")):
        return False
    cmd = process_command(state["pid"])
    workdir = str(state.get("workdir") or "")
    # qemu-system-x86_64, qemu-system-aarch64 (or a wrapper with "qemu" in its path), started with our -name
    return bool(cmd) and "qemu" in cmd and "-name barehand" in cmd and bool(workdir) and workdir in cmd


def accel_kind(accel: Optional[str]) -> str:
    """``tcg`` (software emulation) or ``hw`` (hvf/kvm/whpx...)."""
    return "tcg" if not accel or str(accel).startswith("tcg") else "hw"


# Per-character hold time (ms) for the `type` action, by (guest architecture, accelerator kind). QEMU queues
# each send-key's down/up events, so a long hold makes the guest lag the host (60 ms: ~12 s after 150 keys)
# while 5-20 ms never dropped a key. Measured with a guest-side md5 self-check, see the README notes.
TYPE_HOLD_MS = {("x86_64", "tcg"): 15, ("x86_64", "hw"): 10, ("aarch64", "tcg"): 15, ("aarch64", "hw"): 10}
TYPE_HOLD_FALLBACK_MS = 15      # attached/unknown machine
TYPE_HOLD_FLOOR_MS = 5          # never auto-select less: back-to-back down/up with no gap loses the key-up


def typing_defaults(arch: Optional[str] = None, accel: Optional[str] = None) -> Dict[str, int]:
    """Automatic ``type_hold_ms`` for a machine (``key_hold_ms`` keeps its 60 ms default: firmware menus
    poll the keyboard slowly and single keys do not accumulate lag)."""
    hold = TYPE_HOLD_MS.get((arch or "x86_64", accel_kind(accel)), TYPE_HOLD_FALLBACK_MS)
    return {"type_hold_ms": max(TYPE_HOLD_FLOOR_MS, hold)}


def with_derived_defaults(settings: Mapping[str, Any]) -> Dict[str, Any]:
    """Fill ``type_hold_ms`` when it is unset or 0 (= auto) from the selected machine's (arch, accel)."""
    out = dict(settings)
    try:
        if float(out.get("type_hold_ms") or 0) > 0:
            return out
    except (TypeError, ValueError):
        pass
    state = None
    try:
        state = read_state(machine_name(out))
    except Exception:  # noqa: BLE001 - a bad machine name must not break reading settings
        pass
    state = state or {}
    out["type_hold_ms"] = typing_defaults(state.get("arch"), state.get("accel"))["type_hold_ms"]
    return out


def hermes_settings() -> Dict[str, Any]:
    """``plugins.entries.barehand.settings`` from the active profile's config.yaml ({} outside Hermes), with
    the automatic ``type_hold_ms`` filled in for the selected machine."""
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly() or {}
        entry = ((cfg.get("plugins") or {}).get("entries") or {}).get(PLUGIN_NAME) or {}
        settings = entry.get("settings") or {}
        raw = dict(settings) if isinstance(settings, dict) else {}
    except Exception:
        raw = {}
    return with_derived_defaults(raw)


def validate_endpoint(addr: str, *, allow_remote: bool = False) -> str:
    """Return the endpoint if it is acceptable. QMP is unauthenticated and total control of the machine,
    so only loopback TCP and unix sockets are accepted unless ``allow_remote`` is set."""
    from .qmp import parse_address
    try:
        kind, target = parse_address(addr)
    except ValueError as exc:
        raise ConfigError(str(exc)) from None
    if kind == "tcp" and not 0 < target[1] < 65536:
        raise ConfigError(f"QMP port {target[1]} is out of range")
    if kind == "tcp" and target[0] not in LOOPBACK and not allow_remote:
        raise ConfigError(
            f"QMP host {target[0]!r} is not loopback. QMP has no authentication; tunnel it "
            "(ssh -L) to 127.0.0.1, or set plugins.entries.barehand.settings.allow_remote: true "
            "if you accept that risk.")
    return addr.strip()


def validate_vnc_endpoint(addr: str, *, allow_remote: bool = False) -> str:
    """Return the VNC address if acceptable. Same loopback rule as QMP: VNC passwords are DES-8 and every
    keystroke (including a typed password) crosses the wire unencrypted."""
    from .rfb import parse_address
    try:
        kind, target = parse_address(addr)
    except ValueError as exc:
        raise ConfigError(str(exc)) from None
    if kind == "tcp" and target[0] not in LOOPBACK and not allow_remote:
        raise ConfigError(
            f"VNC host {target[0]!r} is not loopback. VNC is unencrypted (its password is weak DES and every "
            "keystroke, including typed passwords, is readable on the network); use an SSH tunnel to 127.0.0.1 "
            "or a trusted isolated network, then set plugins.entries.barehand.settings.allow_remote: true.")
    return addr.strip()


def vnc_address(st: Mapping[str, Any], env: Mapping[str, str]) -> str:
    """The configured VNC address: env ``BAREHAND_VNC``, else ``vnc_host`` + ``vnc_port``, else ``vnc`` ('' if none)."""
    if env.get("BAREHAND_VNC"):
        return str(env["BAREHAND_VNC"]).strip()
    host = str(st.get("vnc_host") or "").strip()
    if host:
        try:
            port = int(st.get("vnc_port") or 5900)
        except (TypeError, ValueError):
            raise ConfigError(f"vnc_port {st.get('vnc_port')!r} is not a number") from None
        if not 0 < port < 65536:
            raise ConfigError(f"VNC port {port} is out of range")
        return f"[{host}]:{port}" if ":" in host else f"{host}::{port}"
    return str(st.get("vnc") or "").strip()


def transport_kind(st: Mapping[str, Any], env: Mapping[str, str]) -> str:
    """``qmp`` or ``vnc``. Explicit ``transport`` (env wins) decides; otherwise a lone VNC setting selects vnc,
    and both QMP and VNC configured without ``transport`` is an error rather than a guess."""
    t = str(env.get("BAREHAND_TRANSPORT") or st.get("transport") or "").strip().lower()
    if t in ("qmp", "vnc"):
        return t
    if t:
        raise ConfigError(f"transport must be qmp or vnc, not {t!r}")
    has_vnc = bool(vnc_address(st, env))
    has_qmp = bool(env.get("BAREHAND_QMP") or st.get("qmp"))
    if has_vnc and has_qmp:
        raise ConfigError("both qmp and vnc are configured: set transport: qmp or transport: vnc")
    return "vnc" if has_vnc else "qmp"


@dataclass
class Resolved:
    endpoint: Optional[str] = None
    source: str = ""            # "env" | "config" | "state" | ""
    kind: str = "qmp"           # "qmp" | "vnc"
    machine: str = DEFAULT_MACHINE
    settings: Dict[str, Any] = field(default_factory=dict)


def _truthy(v: Any) -> bool:
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def machine_name(settings: Optional[Mapping[str, Any]] = None, env: Optional[Mapping[str, str]] = None) -> str:
    """The selected machine name (cheap: no state file, no process inspection)."""
    env = os.environ if env is None else env
    st = hermes_settings() if settings is None else settings
    return str(env.get("BAREHAND_MACHINE") or st.get("machine") or DEFAULT_MACHINE)


def resolve(settings: Optional[Mapping[str, Any]] = None, env: Optional[Mapping[str, str]] = None) -> Resolved:
    """Resolve the endpoint. Raises ConfigError for a configured-but-unacceptable endpoint."""
    env = os.environ if env is None else env
    st = dict(hermes_settings() if settings is None else settings)
    machine = machine_name(st, env)
    allow_remote = _truthy(st.get("allow_remote", False))
    out = Resolved(machine=machine, settings=st)
    if transport_kind(st, env) == "vnc":     # no managed machine: never consult the state file
        addr = vnc_address(st, env)
        if not addr:
            raise ConfigError("transport is vnc but no address is set (vnc_host/vnc_port or BAREHAND_VNC)")
        out.kind, out.source = "vnc", ("env" if env.get("BAREHAND_VNC") else "config")
        out.endpoint = validate_vnc_endpoint(addr, allow_remote=allow_remote)
        return out
    if env.get("BAREHAND_QMP"):
        out.endpoint, out.source = validate_endpoint(env["BAREHAND_QMP"], allow_remote=allow_remote), "env"
    elif st.get("qmp"):
        out.endpoint, out.source = validate_endpoint(str(st["qmp"]), allow_remote=allow_remote), "config"
    else:
        state = read_state(machine)
        if state and state.get("qmp") and is_our_qemu(state):
            out.endpoint, out.source = validate_endpoint(str(state["qmp"]), allow_remote=allow_remote), "state"
    return out


def endpoint_configured(settings: Optional[Mapping[str, Any]] = None) -> bool:
    """Cheap, never raises: is there any endpoint the backend could try?"""
    try:
        return resolve(settings).endpoint is not None
    except ConfigError:
        return True  # configured, though invalid: let start() report the real error
