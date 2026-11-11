"""``hermes barehand ...`` argparse tree, handler and ``/barehand`` slash command (registered by
``companion.py``). Pure stdlib; talks to ``machine`` and ``config`` only.

The human gate covers the ``barehand_machine`` tool. This CLI is for people: destructive options
(``down --purge``, ``up --fresh``) ask for confirmation on a terminal and otherwise need ``--yes``
(or ``BAREHAND_ASSUME_YES=1``). Nothing in the model-facing text points at this CLI."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from typing import Any, List
from urllib.parse import quote

from . import config, ledger, machine, screentext, transport
from .doctor import report


def setup(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--machine", default=None, help="machine name (default: the configured one)")
    sub = parser.add_subparsers(dest="barehand_cmd")
    # --machine is also accepted after the subcommand; SUPPRESS keeps a value given before it from being reset
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--machine", default=argparse.SUPPRESS, help="machine name (default: the configured one)")

    def _sp(name: str, **kw: Any) -> argparse.ArgumentParser:
        return sub.add_parser(name, parents=[common], **kw)
    up = _sp("up", help="start (or resume) a blank UEFI machine under QEMU")
    up.add_argument("--iso", help="installer/live ISO to attach as a CD-ROM")
    up.add_argument("--arch", choices=list(machine.ARCH_CHOICES), default=None,
                    help="guest architecture (default: the `arch` setting, else auto = the ISO's own, the "
                         "machine's existing disk, else this host's). Fixed for the life of a machine's disk.")
    up.add_argument("--disk-gb", type=int, default=8, help="blank disk size on first creation (default 8)")
    up.add_argument("--memory-mb", type=int, default=2048)
    up.add_argument("--cpus", type=int, default=2)
    up.add_argument("--window", action="store_true", help="also show a QEMU window (default: headless)")
    up.add_argument("--splash-ms", type=int, default=None,
                    help="x86_64 firmware splash time (default 15000 under software emulation, else 5000)")
    up.add_argument("--fresh", action="store_true", help="delete the existing disk and UEFI variables first")
    up.add_argument("--enter-setup", action="store_true", help="hold Escape after start so the firmware setup menu shows")
    up.add_argument("--yes", action="store_true", help="skip the confirmation for destructive options")
    down = _sp("down", help="stop the machine (disk is kept)")
    down.add_argument("--purge", action="store_true", help="also delete the machine's disk, snapshots and state")
    down.add_argument("--yes", action="store_true", help="skip the confirmation for --purge")
    _sp("status", help="show the machine and its QMP endpoint")
    _sp("eject", help="eject the CD-ROM (so the next boot uses the disk)")
    rst = _sp("reset", help="hard reset (also powers a shut-down machine back on)")
    rst.add_argument("--enter-setup", action="store_true", help="hold Escape after the reset to land in firmware setup")
    rst.add_argument("--seconds", type=float, default=None, help="how long to hold Escape (default depends on architecture and acceleration: x86_64 35 s under TCG, 12 s accelerated; aarch64 20 s, 6 s)")
    snap = _sp("snapshot", help="checkpoint / restore the whole machine (RAM, disk, UEFI vars)")
    snap.add_argument("action", choices=["save", "load", "delete", "list"])
    snap.add_argument("tag", nargs="?")
    snap.add_argument("--overwrite", action="store_true", help="allow `save` to replace an existing snapshot")
    _sp("doctor", help="check QEMU, firmware, endpoint and screenshots")
    rec = _sp("recordings", help="flight-recorder sessions (screenshots and key chords; typed text only if record_typed_text is on)")
    rec.add_argument("action", choices=["list", "path", "purge"])
    rec.add_argument("--latest", action="store_true", help="path: print the newest session folder instead of the root")
    rec.add_argument("--yes", action="store_true", help="purge: skip the confirmation")
    atl = _sp("atlases", help="learned screen-text fonts (glyph atlases): list them or remove them")
    atl.add_argument("action", choices=["list", "purge"])
    atl.add_argument("--yes", action="store_true", help="purge: skip the confirmation")
    dem = _sp("demo", help="open a bundled recording; needs no QEMU, model or network")
    dem.add_argument("which", nargs="?", choices=["rescue", "debian"], default="rescue",
                     help="rescue: the GRUB rescue repair (default); debian: the Debian 13 install")
    dem.add_argument("--list", action="store_true", help="list the bundled recordings and exit")
    dem.add_argument("--no-open", action="store_true", help="only print the path and URL; do not open a browser")
    scr = _sp("screen", help="print the machine's current screen as text (no vision model needed)")
    scr.add_argument("--png", metavar="FILE", default=None, help="also write a screenshot to FILE")
    parser.set_defaults(func=handle)


def _name(args: Any) -> str:
    return getattr(args, "machine", None) or config.resolve().machine


def _confirm(what: str, args: Any, interactive: bool) -> None:
    if getattr(args, "yes", False) or os.environ.get("BAREHAND_ASSUME_YES") == "1":
        return
    if interactive and sys.stdin.isatty():
        if input(f"{what} Type 'yes' to continue: ").strip().lower() == "yes":
            return
        raise machine.MachineError("cancelled")
    raise machine.MachineError(f"{what} Re-run with --yes to confirm (or set BAREHAND_ASSUME_YES=1).")


def run(argv: List[str]) -> str:
    """Execute one command line and return its text output (raises machine.MachineError / ConfigError)."""
    p = argparse.ArgumentParser(prog="barehand", add_help=False, exit_on_error=False)
    setup(p)
    return _dispatch(p.parse_args(argv))[0]


def _dispatch(args: Any, interactive: bool = False):
    """(text, exit code)."""
    cmd = getattr(args, "barehand_cmd", None)
    if cmd == "demo":
        return _demo(args), 0
    name = _name(args)
    if cmd in ("up", "down", "status", "eject", "reset", "snapshot"):
        try:
            resolved = config.resolve()
        except config.ConfigError:
            resolved = None
        if resolved is not None and resolved.kind == "vnc":
            if cmd == "status":
                from .companion import vnc_status
                return json.dumps(vnc_status(resolved), indent=2), 0
            raise machine.MachineError(transport.unavailable(cmd) + " (Barehand does not manage a VNC target)")
    if cmd == "up":
        if args.fresh:
            _confirm(f"--fresh deletes machine {name!r}'s disk, snapshots and UEFI variables.", args, interactive)
        if args.iso:
            machine.check_iso_image(args.iso)
        st = machine.up(iso=args.iso, disk_gb=args.disk_gb, memory_mb=args.memory_mb, cpus=args.cpus,
                        window=args.window, name=name, splash_ms=args.splash_ms, fresh=args.fresh,
                        arch=args.arch)
        text = (f"machine {name!r} up: pid {st['pid']}, arch {st['arch']} ({st['arch_source']}), accel {st['accel']}, "
                f"QMP {st['qmp']}\n"
                f"state: {config.state_path(name)}")
        if not _backend_is_barehand():
            text += "\nSelect the driver with `computer_use.backend: barehand` in config.yaml."
        if st["accel"].startswith("tcg"):
            text += ("\nSoftware emulation: slow" +
                     ("; the firmware needs ~30 s before it reads the keyboard." if st["arch"] == "x86_64" else "."))
        if args.enter_setup:
            from .qmp import QMP
            with QMP(st["qmp"], timeout=15) as q:
                n = machine.hold_escape(q, machine.setup_window(name))
            text += f"\nheld Escape ({n} presses): firmware setup should be showing"
        return text, 0
    if cmd == "down":
        if args.purge:
            _confirm(f"--purge deletes machine {name!r}'s disk, snapshots and state permanently.", args, interactive)
        return machine.down(name, purge=args.purge), 0
    if cmd == "status":
        return json.dumps(machine.status(name), indent=2), 0
    if cmd == "eject":
        return machine.eject(name), 0
    if cmd == "reset":
        return machine.reset(name, enter_setup=args.enter_setup, setup_seconds=args.seconds), 0
    if cmd == "snapshot":
        return machine.snapshot(args.action, args.tag, name, overwrite=args.overwrite), 0
    if cmd == "doctor":
        return report()
    if cmd == "recordings":
        return _recordings(args, interactive), 0
    if cmd == "atlases":
        return _atlases(args, interactive), 0
    if cmd == "screen":
        return _screen(args), 0
    return _HELP, 0


_HELP = """usage: hermes barehand [--machine NAME] <command> ...

  demo        replay a bundled recording (needs no QEMU, model or network); start here
  doctor      check QEMU, firmware, endpoint and screenshots
  up          start (or resume) a blank UEFI machine under QEMU (--iso FILE to attach an installer)
  status      show the machine and its QMP endpoint
  screen      print the current screen as text
  reset | eject | snapshot save|load|delete|list | down
  recordings | atlases   inspect or purge what Barehand stored"""


def _backend_is_barehand() -> bool:
    """True when config.yaml already selects computer_use.backend: barehand (False if it cannot be read)."""
    from .companion import _backend_is_barehand as check
    return bool(check())


_DEMO_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo")
DEMO_DIR = os.path.join(_DEMO_ROOT, "rescue-grub")
DEMOS = {
    "rescue": ("rescue-grub", "gpt-6.1-terra repairs an aarch64 VM stuck at grub rescue> (40 tool calls; judge: PASS)",
               "a recorded GRUB rescue by a model (judge: PASS)"),
    "debian": ("debian-install", "gpt-6.1-terra installs Debian 13 on a blank aarch64 machine, repairs its own "
               "boot loader (145 tool calls, 29 minutes)",
               "a recorded Debian 13 install by a model, including a boot loader repair"),
}


def _demo(args: Any) -> str:
    """Print (and open) a bundled recording's viewer. Copies nothing; needs no QEMU, config or network."""
    if getattr(args, "list", False):
        return "\n".join(f"{k:<8}{v[1]}" for k, v in DEMOS.items())
    key = getattr(args, "which", None) or "rescue"
    folder, _, blurb = DEMOS[key]
    index = os.path.join(_DEMO_ROOT, folder, "index.html")
    if not os.path.isfile(index):
        raise machine.MachineError(f"the bundled demo recording is missing ({index}); reinstall the plugin")
    url = "file://" + quote(index)
    text = (f"Barehand demo: {blurb}. Step through it with the slider "
            f"or the arrow keys.\n{index}\n{url}")
    if getattr(args, "no_open", False):
        return text
    import webbrowser
    try:
        opened = webbrowser.open(url)
    except Exception:  # noqa: BLE001 - a missing browser is not an error
        opened = False
    return text + ("" if opened else "\n(could not open a browser; open the path above yourself)")


def _screen(args: Any) -> str:
    """The current screen as the fenced screen-text block the model gets (or the one-line reason it is
    unreadable); with ``--png`` also a screenshot. Works for QMP and VNC; fails clearly when nothing runs."""
    from .companion import _connect
    resolved = config.resolve()
    screentext.ensure_edk2(None, None, wait=0.3)
    with (_connect(resolved) if resolved.kind == "vnc" else machine.connect(_name(args))) as q:
        png = q.screenshot() if getattr(args, "png", None) else None
        block = screentext.block_for(q.frame(), screentext.TextState(), full=True)
    out = block or "screen text: unavailable"
    if png is not None:
        with open(args.png, "wb") as fh:
            fh.write(png)
        out += f"\nscreenshot written to {args.png}"
    return out


def _atlases(args: Any, interactive: bool) -> str:
    st = screentext.store()
    if args.action == "list":
        items = st.listing()
        if not items:
            return f"no learned fonts in {st.path}"
        return "\n".join(f"{i['file']}  {i['cell']}  {i['glyphs']} glyphs  source={i['source']}" if i.get("valid")
                         else f"{i['file']}  (invalid, ignored)" for i in items)
    _confirm("purge deletes every learned font (the EDK2 one is derived again from the firmware; Linux fonts "
             "need calibrate_text again).", args, interactive)
    n = st.purge()
    screentext.reset_caches()
    return f"deleted {n} atlas file(s) from {st.path}"


def _recordings(args: Any, interactive: bool) -> str:
    if args.action == "path":
        sessions = ledger.list_sessions()
        return sessions[-1]["path"] if args.latest and sessions else str(ledger.recordings_dir())
    if args.action == "list":
        sessions = ledger.list_sessions()
        if not sessions:
            return f"no recordings in {ledger.recordings_dir()}"
        lines = [f"{s['name']}  {s['records']} records  {s['bytes'] / 1e6:.1f} MB{'  (running)' if s['live'] else ''}  {s['path']}/index.html" for s in sessions]
        return "\n".join(lines)
    _confirm("purge deletes every recording (ledgers, frames and viewers) permanently.", args, interactive)
    n = ledger.purge()
    skipped = f" (left {ledger.LAST_PURGE_SKIPPED} running session(s) alone)" if ledger.LAST_PURGE_SKIPPED else ""
    return f"deleted {n} recording session(s){skipped}"


def handle(args: Any) -> int:
    try:
        text, code = _dispatch(args, interactive=True)
        print(text)
        return code
    except machine.MachineError as exc:
        print(f"barehand: {exc}", file=sys.stderr)
        return 1
    except config.ConfigError as exc:
        print(f"barehand: {exc}", file=sys.stderr)
        return 2


_SLASH_SAFE = {"status", "eject", "reset", "snapshot", "doctor"}


def slash(raw: str) -> str:
    """``/barehand status|snapshot ...|reset|eject|doctor`` (``up``/``down`` stay CLI-only)."""
    try:
        argv = shlex.split(raw or "")
    except ValueError as exc:
        return f"barehand: {exc}"
    if not argv or argv[0] not in _SLASH_SAFE:
        return ("Usage: /barehand status | snapshot save|load|delete|list [name] [--overwrite] | reset "
                "[--enter-setup] | eject | doctor\n(start/stop the machine with `hermes barehand up` / `down` "
                "in a terminal)")
    try:
        return run(argv)
    except (machine.MachineError, config.ConfigError) as exc:
        return f"barehand: {exc}"
    except (argparse.ArgumentError, SystemExit) as exc:
        return f"barehand: bad arguments ({exc})"
