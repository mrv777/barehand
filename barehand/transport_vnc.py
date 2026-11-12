"""VNC transport for Barehand: the same methods the backend uses on its ``QMP`` object, over RFB (``rfb.py``).

What it can do: capture the screen as raw RGB at native size, press key chords, type, move/click/drag/scroll the
pointer, and be watched for settling (``watch`` is QMP's own algorithm: it only needs ``self.frame()``).
What it cannot do (and says so instead of faking it): snapshots, reset, eject, power: VNC only exposes a display,
a keyboard and a pointer, not the hypervisor. ``cmd``/``hmp`` raise ``VNCUnsupported`` and ``capabilities()``
lists each one with the reason.

Keys arrive as QEMU qcode names (``keys.py`` output). They are sent as the QEMU Extended Key Event (raw XT scancodes)
when the server offers it (QEMU does: no keymap ambiguity), else as X11 keysyms the way a real viewer does: Shift is
pressed as its own key and the key carries the keysym that Shift produces (``|`` rather than ``\\``), which servers
map back through their keymap.
"""

from __future__ import annotations

import atexit
import hashlib
import os
import struct
import threading
import time
import zlib
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import rfb
from .frames import Frame
from .keys import char_to_qcodes
from .qmp import PNG_MAGIC, QMP, QMPError

DEFAULT_PASSWORD_ENV = "BAREHAND_VNC_PASSWORD"
MIN_HOLD_MS = 8       # a key-down and key-up closer together than a USB poll interval can be merged: never 0
BUTTON_BITS = {"left": 1, "middle": 2, "right": 4, "wheel-up": 8, "wheel-down": 16, "wheel-left": 32,
               "wheel-right": 64}


class VNCError(QMPError):
    """A VNC-side failure (authentication, protocol, unsupported server). Subclasses QMPError so the backend's
    existing ``except QMPError`` paths report it."""


class VNCUnsupported(VNCError):
    """The operation does not exist over VNC."""


# --- key tables ------------------------------------------------------------------------------------------------

# qcode -> (X11 keysym name, XT scancode or None; an 0xE0 prefix is encoded as 0x80 | code)
_NAMED: Dict[str, Tuple[str, Optional[int]]] = {
    "esc": ("Escape", 0x01), "backspace": ("BackSpace", 0x0E), "tab": ("Tab", 0x0F), "ret": ("Return", 0x1C),
    "ctrl": ("Control_L", 0x1D), "shift": ("Shift_L", 0x2A), "shift_r": ("Shift_R", 0x36), "alt": ("Alt_L", 0x38),
    "caps_lock": ("Caps_Lock", 0x3A), "num_lock": ("Num_Lock", 0x45), "scroll_lock": ("Scroll_Lock", 0x46),
    "ctrl_r": ("Control_R", 0x9D), "alt_r": ("Alt_R", 0xB8), "meta_l": ("Super_L", 0xDB),
    "meta_r": ("Super_R", 0xDC), "menu": ("Menu", 0xDD), "print": ("Print", 0xB7), "sysrq": ("Sys_Req", 0x54),
    "pause": ("Pause", None), "home": ("Home", 0xC7), "up": ("Up", 0xC8), "pgup": ("Page_Up", 0xC9),
    "left": ("Left", 0xCB), "right": ("Right", 0xCD), "end": ("End", 0xCF), "down": ("Down", 0xD0),
    "pgdn": ("Page_Down", 0xD1), "insert": ("Insert", 0xD2), "delete": ("Delete", 0xD3),
    "kp_enter": ("KP_Enter", 0x9C), "kp_add": ("KP_Add", 0x4E), "kp_subtract": ("KP_Subtract", 0x4A),
    "kp_multiply": ("KP_Multiply", 0x37), "kp_divide": ("KP_Divide", 0xB5), "kp_decimal": ("KP_Decimal", 0x53),
    **{f"kp_{i}": (f"KP_{i}", xt) for i, xt in enumerate((0x52, 0x4F, 0x50, 0x51, 0x4B, 0x4C, 0x4D, 0x47, 0x48, 0x49))},
    **{f"f{i}": (f"F{i}", 0x3A + i) for i in range(1, 11)}, "f11": ("F11", 0x57), "f12": ("F12", 0x58),
}
_QCODE_XT: Dict[str, int] = {}
_ALL_CHARS = [chr(c) for c in range(0x20, 0x7F)]
# qcode -> unshifted char / shifted char, derived from keys.py so both transports share one US layout
_BASE_CHAR: Dict[str, str] = {}
_SHIFT_CHAR: Dict[str, str] = {}
for _c in _ALL_CHARS:
    _qs = char_to_qcodes(_c)
    if len(_qs) == 1:
        _BASE_CHAR.setdefault(_qs[0], _c.lower() if _c.isalpha() else _c)
    elif _qs[0] == "shift" and len(_qs) == 2:
        _SHIFT_CHAR[_qs[1]] = _c
# XT scancodes of the character keys, via their unshifted characters
_XT_BY_CHAR = {"1234567890-=": 0x02, "qwertyuiop[]": 0x10, "asdfghjkl;'": 0x1E, "zxcvbnm,./": 0x2C}
for _chars, _start in _XT_BY_CHAR.items():
    for _i, _ch in enumerate(_chars):
        _q = char_to_qcodes(_ch)[-1]
        _QCODE_XT[_q] = _start + _i
_QCODE_XT[char_to_qcodes("`")[-1]] = 0x29
_QCODE_XT[char_to_qcodes("\\")[-1]] = 0x2B
_QCODE_XT[char_to_qcodes(" ")[-1]] = 0x39
_BASE_CHAR.setdefault("spc", " ")


def qcode_keysym(qcode: str, shifted: bool = False) -> int:
    """X11 keysym for a qcode under a US layout; ``shifted`` selects what Shift produces (A, |, ...)."""
    if qcode in _NAMED:
        return rfb.KEYSYMS[_NAMED[qcode][0]]
    if shifted and qcode in _SHIFT_CHAR:
        return rfb.char_keysym(_SHIFT_CHAR[qcode])
    if shifted and len(qcode) == 1 and qcode.isalpha():
        return rfb.char_keysym(qcode.upper())
    if qcode in _BASE_CHAR:
        return rfb.char_keysym(_BASE_CHAR[qcode])
    raise VNCError(f"no VNC key mapping for qcode {qcode!r}")


def qcode_xt(qcode: str) -> Optional[int]:
    """XT (set 1) scancode, 0xE0 prefix encoded as 0x80|code; None when the key has none (Pause)."""
    if qcode in _NAMED:
        return _NAMED[qcode][1]
    return _QCODE_XT.get(qcode)


def supported_qcodes() -> List[str]:
    return sorted(set(_NAMED) | set(_BASE_CHAR))


# --- the shared connection pool ------------------------------------------------------------------------------------

_POOL: Dict[Tuple, rfb.RFB] = {}
_POOL_LOCK = threading.Lock()


def _acquire(address: str, password: Optional[str], timeout: float, encodings: Optional[Sequence[str]],
             ext_keys: bool, max_pixels: int) -> rfb.RFB:
    pw = hashlib.sha256((password or "").encode()).hexdigest() if password is not None else None
    key = (address, pw, timeout, tuple(encodings or ()), ext_keys, max_pixels)
    with _POOL_LOCK:
        client = _POOL.get(key)
        if client is None:
            client = _POOL[key] = rfb.RFB(address, password, timeout=timeout, encodings=encodings,
                                          use_ext_keys=ext_keys, max_pixels=max_pixels)
        return client


def shutdown_all() -> None:
    """Close every pooled connection (also runs at exit)."""
    with _POOL_LOCK:
        clients = list(_POOL.values())
        _POOL.clear()
    for c in clients:
        c.close()


atexit.register(shutdown_all)


def read_secret(name: str, environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """One secret by variable name, never logged. Inside Hermes this goes through its public secret API
    (``agent.secret_scope.get_secret``, which honours profile scoping); outside Hermes (the standalone CLI) or
    when an explicit ``environ`` mapping is given, it is a plain environment lookup. When Hermes refuses the
    read (one process serving several profiles) this raises: the process environment belongs to the launch
    profile, so it is never used as a fallback."""
    if environ is not None:
        return environ.get(name)
    try:
        from agent.secret_scope import get_secret
    except Exception:  # noqa: BLE001 - not running inside Hermes
        return os.environ.get(name)
    try:
        return get_secret(name)
    except Exception:  # noqa: BLE001 - e.g. an unscoped read under multiplexing
        raise VNCError(f"VNC password unavailable in this profile (Hermes refused to read {name})") from None


def _default_password_set(environ: Optional[Mapping[str, str]]) -> bool:
    """Whether the default password variable holds a value. Nothing names it in the configuration, so a
    refused read means "no password", not an error."""
    try:
        return bool(read_secret(DEFAULT_PASSWORD_ENV, environ))
    except VNCError:
        return False


def password_from_env(name: Optional[str], environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The VNC password, read from the environment variable *named* by the configuration (the secret itself is
    never a config value). No name -> no password. A name whose variable is unset or empty is an error."""
    if not name:
        return None
    value = read_secret(name, environ)
    if not value:
        raise VNCError(f"the VNC password environment variable {name} is not set")
    return value


def _png(w: int, h: int, rgb: bytes) -> bytes:
    def chunk(tag: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)
    stride = w * 3
    raw = b"".join(b"\0" + rgb[y * stride:(y + 1) * stride] for y in range(h))
    return (PNG_MAGIC + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 3)) + chunk(b"IEND", b""))


class VNCTransport:
    """Drop-in for the QMP object's display/keyboard/pointer methods."""
    kind = "vnc"
    watch = QMP.watch      # settle/watch logic is shared; it only calls self.frame()

    def __init__(self, address: str, password: Optional[str] = None, *, timeout: float = 10.0,
                 encodings: Optional[Sequence[str]] = None, ext_keys: bool = True, key_gap: float = 0.03,
                 max_pixels: int = rfb.DEFAULT_MAX_PIXELS, client: Optional[rfb.RFB] = None):
        self.address = address
        self.key_gap = key_gap
        self._lock = threading.RLock()
        self._buttons = 0
        self._pos = (0, 0)
        self._rfb = client or _acquire(address, password, timeout, encodings, ext_keys, max_pixels)
        self._call(self._rfb.ensure)

    # --- plumbing ----------------------------------------------------------------------------------------------
    @staticmethod
    def _call(fn, *args, **kw):
        try:
            return fn(*args, **kw)
        except (rfb.RFBAuthError, rfb.RFBUnsupported, rfb.RFBProtocolError) as exc:
            raise VNCError(str(exc)) from None

    @classmethod
    def from_settings(cls, settings: Mapping[str, Any], environ: Optional[Mapping[str, str]] = None,
                      **kw: Any) -> "VNCTransport":
        """Build from plugin settings: ``vnc`` (address), ``vnc_password_env`` (the NAME of the environment
        variable holding the password), optional ``vnc_encodings`` and ``vnc_timeout``."""
        env = os.environ if environ is None else environ
        address = str(settings.get("vnc") or env.get("BAREHAND_VNC") or "").strip()
        if not address:
            raise VNCError("no VNC address configured (settings.vnc or BAREHAND_VNC, e.g. 127.0.0.1:5900)")
        pw_env = str(settings.get("vnc_password_env") or "").strip() or (
            DEFAULT_PASSWORD_ENV if _default_password_set(environ) else None)
        enc = settings.get("vnc_encodings")
        if isinstance(enc, str):
            enc = [e.strip() for e in enc.split(",") if e.strip()]
        return cls(address, password_from_env(pw_env, environ), timeout=float(settings.get("vnc_timeout") or 10.0),
                   encodings=enc or None, **kw)

    def close(self) -> None:
        """Release this handle. The pooled connection stays up for the next operation (see ``shutdown_all``)."""
        self._release_inputs()

    def __enter__(self) -> "VNCTransport":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _release_inputs(self) -> None:
        with self._lock:
            if self._buttons:
                self._buttons = 0
                try:
                    self._rfb.pointer(*self._pos, 0)
                except Exception:
                    pass

    # --- capabilities ------------------------------------------------------------------------------------------
    def capabilities(self) -> Dict[str, Dict[str, Any]]:
        no = "VNC exposes only a display, a keyboard and a pointer, not the hypervisor"
        return {
            "screen": {"available": True, "reason": ""},
            "keyboard": {"available": True, "reason": "extended key event (scancodes)" if self._rfb.ext_keys
                         else "X11 keysyms through the server's keymap"},
            "pointer": {"available": True, "reason": "absolute; the guest needs a tablet-type device (QEMU: usb-tablet)"},
            "snapshots": {"available": False, "reason": no + "; use the hypervisor's own snapshot feature"},
            "reset": {"available": False, "reason": no + "; ctrl+alt+del is a key chord, a hardware reset is not"},
            "eject": {"available": False, "reason": no},
            "power": {"available": False, "reason": no + "; power state is not visible either"},
        }

    def cmd(self, execute: str, _timeout: Optional[float] = None, **arguments: Any) -> Any:
        raise VNCUnsupported(f"{execute}: not available over VNC (a VNC console has no hypervisor control channel)")

    def hmp(self, command: str, timeout: float = 120.0) -> str:
        raise VNCUnsupported(f"{command.split()[0] if command.split() else command}: not available over VNC "
                             "(no monitor); snapshots, eject and reset need the hypervisor")

    def valid_qcodes(self) -> List[str]:
        return supported_qcodes()

    def status(self) -> str:
        """VNC cannot report the VM state; a live connection is reported as 'running'."""
        self._call(self._rfb.ensure)
        return "running"

    # --- eyes ----------------------------------------------------------------------------------------------------
    @property
    def size(self) -> Tuple[int, int]:
        return self._rfb.width, self._rfb.height

    def frame(self) -> Frame:
        w, h, rgb = self._call(self._rfb.frame)
        head = b"P6\n%d %d\n255\n" % (w, h)
        return Frame(head + rgb, w, h, len(head), hashlib.blake2b(rgb, digest_size=16).digest())

    def screenshot(self) -> bytes:
        w, h, rgb = self._call(self._rfb.frame)
        return _png(w, h, rgb)

    # --- hands: keyboard -----------------------------------------------------------------------------------------
    def _events(self, qcodes: Sequence[str], down: bool) -> bytes:
        shifted = any(q in ("shift", "shift_r") for q in qcodes)
        order = list(qcodes) if down else list(reversed(list(qcodes)))
        out = []
        for q in order:
            sym = qcode_keysym(q, shifted)
            xt = qcode_xt(q)
            if self._rfb.ext_keys and xt is not None:
                out.append(rfb.RFB.ext_key_msg(sym, xt, down))
            else:
                out.append(rfb.RFB.key_msg(sym, down))
        return b"".join(out)

    def key_down(self, qcodes: Sequence[str]) -> None:
        if qcodes:
            self._call(self._rfb.send, self._events(qcodes, True))

    def key_up(self, qcodes: Sequence[str]) -> None:
        if qcodes:
            self._call(self._rfb.send, self._events(qcodes, False))

    def send_chord(self, qcodes: Sequence[str], hold_ms: int = 60) -> None:
        if not qcodes:
            return
        down = self._events(qcodes, True)
        up = self._events(qcodes, False)     # build both first: a mapping error must not leave keys held
        self._call(self._rfb.send, down)
        try:
            time.sleep(max(hold_ms, MIN_HOLD_MS) / 1000)
        finally:
            try:
                self._call(self._rfb.send, up)
            except (OSError, VNCError):
                self._call(self._rfb.send, up)       # one retry (reconnects): never leave a key held
        time.sleep(self.key_gap)

    # --- hands: pointer ------------------------------------------------------------------------------------------
    def _fb_point(self, x: int, y: int, width: int, height: int) -> Tuple[int, int]:
        fw, fh = self._rfb.width, self._rfb.height
        cx = min(max(int(x), 0), max(width - 1, 0))
        cy = min(max(int(y), 0), max(height - 1, 0))
        return (round(cx * (fw - 1) / max(width - 1, 1)), round(cy * (fh - 1) / max(height - 1, 1)))

    def move_to(self, x: int, y: int, width: int, height: int) -> None:
        with self._lock:
            self._pos = self._fb_point(x, y, width, height)
            self._call(self._rfb.pointer, *self._pos, self._buttons)

    def button(self, name: str, down: bool) -> None:
        bit = BUTTON_BITS.get(name)
        if bit is None:
            raise VNCError(f"unknown pointer button {name!r}")
        with self._lock:
            self._buttons = (self._buttons | bit) if down else (self._buttons & ~bit)
            self._call(self._rfb.pointer, *self._pos, self._buttons)

    def click(self, x: int, y: int, width: int, height: int, button: str = "left", count: int = 1,
              modifiers: Sequence[str] = ()) -> None:
        self.key_down(modifiers)
        try:
            self.move_to(x, y, width, height)
            time.sleep(0.05)
            for _ in range(max(1, count)):
                self.button(button, True)
                try:
                    time.sleep(0.05)
                finally:
                    self.button(button, False)    # never leave a button held in the guest
                time.sleep(0.07)
        finally:
            self.key_up(modifiers)

    def drag(self, start: Tuple[int, int], end: Tuple[int, int], width: int, height: int,
             button: str = "left", modifiers: Sequence[str] = (), steps: int = 12) -> None:
        self.key_down(modifiers)
        try:
            self.move_to(*start, width, height)
            time.sleep(0.08)
            self.button(button, True)
            try:
                time.sleep(0.08)
                for i in range(1, steps + 1):
                    f = i / steps
                    self.move_to(round(start[0] + (end[0] - start[0]) * f),
                                 round(start[1] + (end[1] - start[1]) * f), width, height)
                    time.sleep(0.02)
                time.sleep(0.08)
            finally:
                self.button(button, False)
        finally:
            self.key_up(modifiers)

    def wheel(self, direction: str, amount: int, at: Optional[Tuple[int, int]], width: int, height: int,
              modifiers: Sequence[str] = ()) -> None:
        name = {"up": "wheel-up", "down": "wheel-down", "left": "wheel-left", "right": "wheel-right"}[direction]
        self.key_down(modifiers)
        try:
            if at is not None:
                self.move_to(*at, width, height)
                time.sleep(0.05)
            for _ in range(max(1, amount)):
                self.button(name, True)
                self.button(name, False)
                time.sleep(0.04)
        finally:
            self.key_up(modifiers)
