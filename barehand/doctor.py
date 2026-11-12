"""``hermes computer-use doctor`` for Barehand: QEMU, firmware, endpoint, screenshot."""

from __future__ import annotations

import os
from typing import Any, List, Mapping, Optional, Tuple

from . import config, machine
from .qmp import QMP, png_size

Check = Tuple[str, str, str]  # (level ok|warn|fail, name, detail)


def safe_name(value: object, limit: int = 80) -> str:
    """Server-controlled text for display: printable characters only, bounded."""
    t = "".join(ch if ch.isprintable() else "?" for ch in str(value))
    return t if len(t) <= limit else t[:limit] + "..."


def _vnc_checks(r: "config.Resolved") -> List[Check]:
    """Transport vnc: parse, password variable (name only), connect + handshake, one frame. Short timeout."""
    from . import rfb
    from .transport_vnc import read_secret
    out: List[Check] = [("ok", "transport", "vnc"), ("ok", "endpoint", f"{r.endpoint} (from {r.source})")]
    kind, target = rfb.parse_address(str(r.endpoint))
    if kind == "tcp" and target[0] not in config.LOOPBACK:
        out.append(("warn", "encryption", f"{target[0]} is not loopback: VNC is unencrypted (weak DES password, "
                                          "keystrokes readable on the wire); use an SSH tunnel or a trusted network"))
    pw_env = str(r.settings.get("vnc_password_env") or "").strip()
    password = None
    if pw_env:
        try:
            password = read_secret(pw_env)
        except Exception as exc:  # noqa: BLE001 - Hermes refused the read for this profile
            return out + [("fail", "password", str(exc))]
        out.append(("ok", "password", f"read from ${pw_env}") if password else
                   ("fail", "password", f"environment variable {pw_env} (vnc_password_env) is not set or empty"))
        if not password:
            return out
    else:
        out.append(("ok", "password", "none configured (vnc_password_env empty)"))
    c = rfb.RFB(str(r.endpoint), password, timeout=5)
    try:
        c.ensure()
        out.append(("ok", "vnc", f"reachable: RFB {c.version[0]}.{c.version[1]}, security type {c.security}, "
                                 f"desktop {safe_name(c.name)!r}"))
        c.frame()
        out.append(("ok", "screenshot", f"{c.width}x{c.height}"))
        out.append(("ok" if c.ext_keys else "warn", "keys", "raw scancodes (extended key event)" if c.ext_keys else
                    "X11 keysyms through the server's keymap: the guest layout must be US"))
    except rfb.RFBUnsupported as exc:
        out.append(("fail", "vnc", f"{exc}. Only VNC password / no authentication are supported: enable VNC "
                                   "authentication on the server, or put a local stunnel/websockify proxy in front"))
    except Exception as exc:  # noqa: BLE001
        out.append(("fail", "vnc", f"cannot connect to {r.endpoint}: {exc}"))
    finally:
        c.close()
    out.append(("ok", "machine control", "none over VNC (no snapshots, reset, eject, power)"))
    return out


def collect(settings: Optional[Mapping[str, Any]] = None) -> List[Check]:
    st = dict(config.hermes_settings() if settings is None else settings)
    out: List[Check] = []
    try:
        r0 = config.resolve(st)
        if r0.kind == "vnc":
            return _vnc_checks(r0)
    except config.ConfigError as exc:
        if str(st.get("transport") or "").lower() == "vnc":
            return [("fail", "endpoint", str(exc))]
    endpoint: Optional[str] = None
    try:
        r = config.resolve(st)
        endpoint = r.endpoint
        out.append(("ok" if endpoint else "info", "endpoint",
                    f"{endpoint} (from {r.source})" if endpoint else
                    "no machine yet (expected before `hermes barehand up --iso <path>`; or set "
                    "plugins.entries.barehand.settings.qmp / BAREHAND_QMP to attach one)"))
    except config.ConfigError as exc:
        out.append(("fail", "endpoint", str(exc)))

    explicit = os.environ.get("BAREHAND_QEMU") or st.get("qemu_binary")
    if explicit:
        cands = [machine._explicit_qemu(a, str(explicit)) for a in machine.ARCHES]
        if not any(c and os.path.isfile(c) and os.access(c, os.X_OK) for c in cands):
            which = "BAREHAND_QEMU" if os.environ.get("BAREHAND_QEMU") else "qemu_binary"
            out.append(("fail", "qemu override", f"{which}={explicit} does not exist or is not executable "
                                                 "(Barehand will not fall back to another QEMU; fix or unset it)"))
    miss = "warn" if endpoint else "fail"  # not needed just to attach to a running machine
    usable: List[str] = []
    problems: List[str] = []
    qemu_any: Optional[str] = None
    img_checked = False
    host = machine.host_arch()
    for arch in sorted(machine.ARCHES, key=lambda x: x != host):   # the host's own architecture first
        a = machine.ARCHES[arch]
        qemu = machine.qemu_for(st, arch)
        hint = machine.install_hint(arch)
        accel, _ = machine.pick_accel(guest=arch)
        tag = f"[{arch}]"
        ok_arch = True
        if qemu:
            qemu_any = qemu_any or qemu
            out.append(("ok", f"qemu{tag}", f"{qemu} ({machine.qemu_version(qemu)})"))
            old = machine.check_qemu_version(qemu)
            if old:
                ok_arch = False
                out.append(("fail", f"qemu version{tag}", old))
            elif not img_checked:
                img_checked = True
                out.append(("ok", "qemu version", f">= {machine.MIN_QEMU[0]}.{machine.MIN_QEMU[1]}"))
                img = machine.find_qemu_img(qemu)
                out.append(("ok", "qemu-img", img) if img else (miss, "qemu-img", "not found"))
        else:
            ok_arch = False
            problems.append(f"{arch}: {a.binary} missing ({hint})")
            out.append(("warn", f"qemu{tag}", f"{a.binary} not found; install: {hint}"))
        code, vars_ = machine.firmware_for(st, qemu, arch)
        if code and vars_:
            out.append(("ok", f"firmware{tag}", f"{code} + {vars_}"))
        else:
            ok_arch = False
            problems.append(f"{arch}: UEFI firmware missing ({hint})")
            names = " / ".join(c for c, _ in a.fw_pairs[:3])
            out.append(("warn", f"firmware{tag}", f"UEFI firmware ({names}) not found; install: {hint}"))
        slow = " (software emulation, slow)" if accel.startswith("tcg") else ""
        out.append(("ok", f"accelerator{tag}", accel + slow))
        if ok_arch:
            usable.append(f"{arch} ({accel})")
    if usable:
        extra = f"; not usable: {'; '.join(problems)}" if problems else ""
        out.append(("ok", "architectures", f"usable on this {host} host: {', '.join(usable)}{extra}"))
    else:
        out.append((miss, "architectures", f"no guest architecture is usable on this {host} host: "
                                          f"{'; '.join(problems) or 'QEMU too old'}"))
    # the machine we manage, if any
    try:
        state = config.read_state(config.machine_name(st))
    except config.ConfigError:
        state = None
    if state and config.is_our_qemu(state):
        out.append(("ok", "machine", f"{state.get('name')}: {state.get('arch') or 'x86_64'} on "
                                     f"{state.get('accel')}, pid {state.get('pid')}"))

    if endpoint:
        try:
            with QMP(endpoint, timeout=5) as q:
                vm = q.status()
                out.append(("ok" if vm in ("running", "suspended") else "warn", "qmp", f"reachable, vm status: {vm}"))
                try:
                    mice = q.cmd("query-mice")
                    out.append(("ok" if any(m.get("absolute") for m in mice) else "warn", "pointer",
                                ", ".join(f"{m['name']}{' (current)' if m.get('current') else ''}" for m in mice)
                                or "no mouse device (clicks have no target)"))
                except Exception:
                    pass
                try:
                    png = q.screenshot()
                    w, h = png_size(png)
                    out.append(("ok", "screenshot", f"{w}x{h} PNG, {len(png)} bytes"))
                except Exception as exc:
                    out.append(("fail", "screenshot", f"screendump failed: {exc}"))
        except Exception as exc:
            out.append(("fail", "qmp", f"cannot connect to {endpoint}: {exc}"))
    return out


def report(settings: Optional[Mapping[str, Any]] = None) -> Tuple[str, int]:
    """(text, exit code): 1 if any check failed."""
    checks = collect(settings)
    lines = [f"[{level.upper():4}] {name}: {detail}" for level, name, detail in checks]
    failed = [n for lv, n, _ in checks if lv == "fail"]
    lines.append("barehand: " + ("all required checks passed" if not failed else f"failing: {', '.join(failed)}"))
    return "\n".join(lines), (1 if failed else 0)


def run_doctor(settings: Optional[Mapping[str, Any]] = None) -> int:
    text, code = report(settings)
    print(text)
    return code
