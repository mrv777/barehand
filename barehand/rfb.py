"""Stdlib-only RFB (VNC) client: screen capture as raw RGB, keyboard, pointer.

Scope, measured against QEMU's VNC server and a fake server (``tests/fake_rfb.py``):

* RFB 3.3 / 3.7 / 3.8 handshake; security types None and VNC Authentication (DES, implemented here).
  VeNCrypt/TLS, Apple ARD, Tight security, RSA-AES etc. are NOT supported: the error names what the server offered.
* Pixel format is forced to 32-bit true colour (little endian, R<<16 G<<8 B), so every decoder is uniform.
* Encodings: Raw, CopyRect, Hextile, Zlib, ZRLE (all decoded here), pseudo DesktopSize, ExtendedDesktopSize and the
  QEMU Extended Key Event extension. Tight/JPEG/H.264 are NOT implemented: a server that can only send those
  cannot be used (QEMU, libvncserver, TigerVNC, x11vnc and most BMC viewers also speak Raw/Hextile/ZRLE).
* Synchronous design: no background thread. Every public call holds one lock and carries a hard deadline; a
  timeout or protocol violation closes the socket, and the next call reconnects.
* Hostile-server limits: every server supplied length is bounded before anything is allocated (name, rects,
  compressed data, decompressed data, cut text, framebuffer area), zlib output is capped, rectangles must lie
  inside the framebuffer, and no loop runs past the call's deadline.

Frame capture uses a *non-incremental* full-screen request, which makes the server answer with the current
picture (an incremental request is only answered when something changed, which cannot prove "nothing changed").
"""

from __future__ import annotations

import re
import select
import socket
import struct
import threading
import time
import zlib
from typing import Any, Dict, List, Optional, Sequence, Tuple

# --- errors ---------------------------------------------------------------------------------------------------


class RFBError(Exception):
    """Base class."""


class RFBConnectionError(RFBError, ConnectionError):
    """The socket failed or the server closed it."""


class RFBTimeout(RFBConnectionError, TimeoutError):
    """The operation's deadline passed."""


class RFBProtocolError(RFBError):
    """The server sent something invalid or hostile (and the connection was dropped)."""


class RFBAuthError(RFBError):
    """Authentication failed or a password is required."""


class RFBUnsupported(RFBError):
    """The server needs something this client does not implement."""


# --- limits ---------------------------------------------------------------------------------------------------

DEFAULT_MAX_PIXELS = 16 * 1024 * 1024    # 4096x4096 or 5120x2880; 4 bytes each = 64 MiB framebuffer
MAX_NAME = 64 * 1024
MAX_FAIL_REASON = 4096
MAX_CUT_TEXT = 16 * 1024 * 1024          # bytes discarded per ServerCutText
MAX_COLOURMAP = 65535 * 6
MAX_SIDE = 16384
MAX_RESIZE_LOOPS = 4
RESIZE_GRACE = 0.3        # after a resize-only update: how long to wait for the pixels before asking again
DRAIN_GRACE = 0.5         # how long a call waits for an answer that is still owed to an earlier request

# --- encodings ------------------------------------------------------------------------------------------------

ENC_RAW, ENC_COPYRECT, ENC_HEXTILE, ENC_ZLIB, ENC_ZRLE = 0, 1, 5, 6, 16
ENC_DESKTOP_SIZE, ENC_LAST_RECT, ENC_EXT_DESKTOP_SIZE = -223, -224, -308
ENC_QEMU_EXT_KEY = -258
ENC_NAMES = {"raw": ENC_RAW, "copyrect": ENC_COPYRECT, "hextile": ENC_HEXTILE, "zlib": ENC_ZLIB, "zrle": ENC_ZRLE}
DEFAULT_ENCODINGS = ("zrle", "hextile", "copyrect", "raw")   # preference order sent to the server
SEC_NONE, SEC_VNC = 1, 2
SEC_NAMES = {0: "Invalid", 1: "None", 2: "VNC Authentication", 5: "RA2", 6: "RA2ne", 16: "Tight", 17: "Ultra",
             18: "TLS", 19: "VeNCrypt", 20: "GTK-VNC SASL", 21: "MD5 hash", 22: "Colin Dean xvp",
             30: "Apple Remote Desktop", 129: "UltraVNC MSLogonII"}

# --- DES (for VNC Authentication) -----------------------------------------------------------------------------

_IP = [58, 50, 42, 34, 26, 18, 10, 2, 60, 52, 44, 36, 28, 20, 12, 4, 62, 54, 46, 38, 30, 22, 14, 6, 64, 56, 48, 40,
       32, 24, 16, 8, 57, 49, 41, 33, 25, 17, 9, 1, 59, 51, 43, 35, 27, 19, 11, 3, 61, 53, 45, 37, 29, 21, 13, 5,
       63, 55, 47, 39, 31, 23, 15, 7]
_FP = [40, 8, 48, 16, 56, 24, 64, 32, 39, 7, 47, 15, 55, 23, 63, 31, 38, 6, 46, 14, 54, 22, 62, 30, 37, 5, 45, 13,
       53, 21, 61, 29, 36, 4, 44, 12, 52, 20, 60, 28, 35, 3, 43, 11, 51, 19, 59, 27, 34, 2, 42, 10, 50, 18, 58, 26,
       33, 1, 41, 9, 49, 17, 57, 25]
_E = [32, 1, 2, 3, 4, 5, 4, 5, 6, 7, 8, 9, 8, 9, 10, 11, 12, 13, 12, 13, 14, 15, 16, 17, 16, 17, 18, 19, 20, 21, 20,
      21, 22, 23, 24, 25, 24, 25, 26, 27, 28, 29, 28, 29, 30, 31, 32, 1]
_P = [16, 7, 20, 21, 29, 12, 28, 17, 1, 15, 23, 26, 5, 18, 31, 10, 2, 8, 24, 14, 32, 27, 3, 9, 19, 13, 30, 6, 22,
      11, 4, 25]
_PC1 = [57, 49, 41, 33, 25, 17, 9, 1, 58, 50, 42, 34, 26, 18, 10, 2, 59, 51, 43, 35, 27, 19, 11, 3, 60, 52, 44, 36,
        63, 55, 47, 39, 31, 23, 15, 7, 62, 54, 46, 38, 30, 22, 14, 6, 61, 53, 45, 37, 29, 21, 13, 5, 28, 20, 12, 4]
_PC2 = [14, 17, 11, 24, 1, 5, 3, 28, 15, 6, 21, 10, 23, 19, 12, 4, 26, 8, 16, 7, 27, 20, 13, 2, 41, 52, 31, 37, 47,
        55, 30, 40, 51, 45, 33, 48, 44, 49, 39, 56, 34, 53, 46, 42, 50, 36, 29, 32]
_SHIFTS = [1, 1, 2, 2, 2, 2, 2, 2, 1, 2, 2, 2, 2, 2, 2, 1]
_SBOX = [
    [14, 4, 13, 1, 2, 15, 11, 8, 3, 10, 6, 12, 5, 9, 0, 7, 0, 15, 7, 4, 14, 2, 13, 1, 10, 6, 12, 11, 9, 5, 3, 8,
     4, 1, 14, 8, 13, 6, 2, 11, 15, 12, 9, 7, 3, 10, 5, 0, 15, 12, 8, 2, 4, 9, 1, 7, 5, 11, 3, 14, 10, 0, 6, 13],
    [15, 1, 8, 14, 6, 11, 3, 4, 9, 7, 2, 13, 12, 0, 5, 10, 3, 13, 4, 7, 15, 2, 8, 14, 12, 0, 1, 10, 6, 9, 11, 5,
     0, 14, 7, 11, 10, 4, 13, 1, 5, 8, 12, 6, 9, 3, 2, 15, 13, 8, 10, 1, 3, 15, 4, 2, 11, 6, 7, 12, 0, 5, 14, 9],
    [10, 0, 9, 14, 6, 3, 15, 5, 1, 13, 12, 7, 11, 4, 2, 8, 13, 7, 0, 9, 3, 4, 6, 10, 2, 8, 5, 14, 12, 11, 15, 1,
     13, 6, 4, 9, 8, 15, 3, 0, 11, 1, 2, 12, 5, 10, 14, 7, 1, 10, 13, 0, 6, 9, 8, 7, 4, 15, 14, 3, 11, 5, 2, 12],
    [7, 13, 14, 3, 0, 6, 9, 10, 1, 2, 8, 5, 11, 12, 4, 15, 13, 8, 11, 5, 6, 15, 0, 3, 4, 7, 2, 12, 1, 10, 14, 9,
     10, 6, 9, 0, 12, 11, 7, 13, 15, 1, 3, 14, 5, 2, 8, 4, 3, 15, 0, 6, 10, 1, 13, 8, 9, 4, 5, 11, 12, 7, 2, 14],
    [2, 12, 4, 1, 7, 10, 11, 6, 8, 5, 3, 15, 13, 0, 14, 9, 14, 11, 2, 12, 4, 7, 13, 1, 5, 0, 15, 10, 3, 9, 8, 6,
     4, 2, 1, 11, 10, 13, 7, 8, 15, 9, 12, 5, 6, 3, 0, 14, 11, 8, 12, 7, 1, 14, 2, 13, 6, 15, 0, 9, 10, 4, 5, 3],
    [12, 1, 10, 15, 9, 2, 6, 8, 0, 13, 3, 4, 14, 7, 5, 11, 10, 15, 4, 2, 7, 12, 9, 5, 6, 1, 13, 14, 0, 11, 3, 8,
     9, 14, 15, 5, 2, 8, 12, 3, 7, 0, 4, 10, 1, 13, 11, 6, 4, 3, 2, 12, 9, 5, 15, 10, 11, 14, 1, 7, 6, 0, 8, 13],
    [4, 11, 2, 14, 15, 0, 8, 13, 3, 12, 9, 7, 5, 10, 6, 1, 13, 0, 11, 7, 4, 9, 1, 10, 14, 3, 5, 12, 2, 15, 8, 6,
     1, 4, 11, 13, 12, 3, 7, 14, 10, 15, 6, 8, 0, 5, 9, 2, 6, 11, 13, 8, 1, 4, 10, 7, 9, 5, 0, 15, 14, 2, 3, 12],
    [13, 2, 8, 4, 6, 15, 11, 1, 10, 9, 3, 14, 5, 0, 12, 7, 1, 15, 13, 8, 10, 3, 7, 4, 12, 5, 6, 11, 0, 14, 9, 2,
     7, 11, 4, 1, 9, 12, 14, 2, 0, 6, 10, 13, 15, 3, 5, 8, 2, 1, 14, 7, 4, 10, 8, 13, 15, 12, 9, 0, 3, 5, 6, 11],
]


def _bits(data: bytes) -> List[int]:
    return [(b >> (7 - i)) & 1 for b in data for i in range(8)]


def _unbits(bits: Sequence[int]) -> bytes:
    return bytes(sum(bit << (7 - j) for j, bit in enumerate(bits[i:i + 8])) for i in range(0, len(bits), 8))


def _permute(bits: Sequence[int], table: Sequence[int]) -> List[int]:
    return [bits[i - 1] for i in table]


def _key_schedule(key: bytes) -> List[List[int]]:
    cd = _permute(_bits(key), _PC1)
    c, d = cd[:28], cd[28:]
    keys = []
    for s in _SHIFTS:
        c, d = c[s:] + c[:s], d[s:] + d[:s]
        keys.append(_permute(c + d, _PC2))
    return keys


def des_encrypt_block(key: bytes, block: bytes) -> bytes:
    """Plain single-block DES encryption (8-byte key incl. parity bits, 8-byte block). Not for general use."""
    if len(key) != 8 or len(block) != 8:
        raise ValueError("DES needs an 8-byte key and an 8-byte block")
    sub = _key_schedule(key)
    bits = _permute(_bits(block), _IP)
    left, right = bits[:32], bits[32:]
    for k in sub:
        x = [a ^ b for a, b in zip(_permute(right, _E), k)]
        out: List[int] = []
        for i in range(8):
            six = x[i * 6:i * 6 + 6]
            row = (six[0] << 1) | six[5]
            col = (six[1] << 3) | (six[2] << 2) | (six[3] << 1) | six[4]
            v = _SBOX[i][row * 16 + col]
            out += [(v >> 3) & 1, (v >> 2) & 1, (v >> 1) & 1, v & 1]
        f = _permute(out, _P)
        left, right = right, [a ^ b for a, b in zip(left, f)]
    return _unbits(_permute(right + left, _FP))


def _reverse_bits(b: int) -> int:
    return int(f"{b:08b}"[::-1], 2)


def vnc_auth_response(password: str, challenge: bytes) -> bytes:
    """VNC Authentication: DES-ECB of the 16-byte challenge. The key is the password truncated/zero-padded to 8
    bytes with the bits of every byte reversed (the well-known VNC quirk)."""
    if len(challenge) != 16:
        raise ValueError("VNC challenge must be 16 bytes")
    raw = password.encode("latin-1", "replace")[:8].ljust(8, b"\0")
    key = bytes(_reverse_bits(b) for b in raw)
    return des_encrypt_block(key, challenge[:8]) + des_encrypt_block(key, challenge[8:])


# --- keysyms --------------------------------------------------------------------------------------------------

KEYSYMS: Dict[str, int] = {
    "BackSpace": 0xFF08, "Tab": 0xFF09, "Return": 0xFF0D, "Pause": 0xFF13, "Scroll_Lock": 0xFF14,
    "Sys_Req": 0xFF15, "Escape": 0xFF1B, "Delete": 0xFFFF, "Home": 0xFF50, "Left": 0xFF51, "Up": 0xFF52,
    "Right": 0xFF53, "Down": 0xFF54, "Page_Up": 0xFF55, "Page_Down": 0xFF56, "End": 0xFF57, "Print": 0xFF61,
    "Insert": 0xFF63, "Menu": 0xFF67, "Num_Lock": 0xFF7F, "KP_Enter": 0xFF8D, "KP_Multiply": 0xFFAA,
    "KP_Add": 0xFFAB, "KP_Subtract": 0xFFAD, "KP_Decimal": 0xFFAE, "KP_Divide": 0xFFAF,
    "Shift_L": 0xFFE1, "Shift_R": 0xFFE2, "Control_L": 0xFFE3, "Control_R": 0xFFE4, "Caps_Lock": 0xFFE5,
    "Meta_L": 0xFFE7, "Meta_R": 0xFFE8, "Alt_L": 0xFFE9, "Alt_R": 0xFFEA, "Super_L": 0xFFEB, "Super_R": 0xFFEC,
    **{f"KP_{i}": 0xFFB0 + i for i in range(10)},
    **{f"F{i}": 0xFFBD + i for i in range(1, 13)},
}


def char_keysym(ch: str) -> int:
    """X11 keysym of one character: printable ASCII / Latin-1 map to their code point, anything else to the Unicode
    keysym range (0x01000000 + code point); control characters Return/Tab/BackSpace/Escape to their named keys."""
    if len(ch) != 1:
        raise ValueError(f"expected one character, got {ch!r}")
    named = {"\n": "Return", "\r": "Return", "\t": "Tab", "\b": "BackSpace", "\x1b": "Escape", "\x7f": "Delete"}
    if ch in named:
        return KEYSYMS[named[ch]]
    cp = ord(ch)
    if 0x20 <= cp <= 0x7E or 0xA0 <= cp <= 0xFF:
        return cp
    if cp < 0x20:
        raise ValueError(f"control character U+{cp:04X} has no keysym")
    return 0x01000000 + cp


# --- address --------------------------------------------------------------------------------------------------


def parse_address(addr: str) -> Tuple[str, Any]:
    """``'host:5900'`` / ``'host::5900'`` / ``'host:1'`` (a number below 100 is a display number: port 5901, the
    VNC convention; ``host::N`` always means port N) / ``'[::1]:5900'`` / ``'vnc://host:port'`` -> ('tcp', (host,
    port)); ``'unix:/path'`` -> ('unix', path)."""
    a = addr.strip()
    if a.startswith("unix:"):
        if not a[5:]:
            raise ValueError("empty unix socket path")
        return "unix", a[5:]
    for prefix in ("vnc://", "tcp:"):
        if a.startswith(prefix):
            a = a[len(prefix):]
    bad = ValueError(f"VNC address must be host:port or unix:/path, got {addr!r}")
    if not a or "/" in a:
        raise bad
    explicit = False
    if a.startswith("["):
        host, _, rest = a[1:].partition("]")
        port_s = rest[1:] if rest.startswith(":") else ""
        explicit = not port_s
    elif "::" in a:
        host, _, port_s = a.partition("::")
        explicit = True
    elif a.count(":") == 1:
        host, _, port_s = a.partition(":")
    elif ":" in a:
        raise bad
    else:
        host, port_s, explicit = a, "", True
    try:
        port = int(port_s) if port_s else 5900
    except ValueError:
        raise bad from None
    if not explicit and port < 100:
        port += 5900
    if not 0 < port < 65536:
        raise ValueError(f"VNC port out of range in {addr!r}")
    return "tcp", (host or "127.0.0.1", port)


# --- the client -----------------------------------------------------------------------------------------------

_VERSION_RE = re.compile(rb"^RFB (\d{3})\.(\d{3})\n$")
_PIXEL_FORMAT = struct.pack(">BBBBHHHBBBxxx", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)


class _Cursor:
    """Bounds-checked reader over a decompressed ZRLE buffer."""
    __slots__ = ("d", "p")

    def __init__(self, data: bytes):
        self.d, self.p = data, 0

    def take(self, n: int) -> bytes:
        if n < 0 or self.p + n > len(self.d):
            raise RFBProtocolError("truncated ZRLE data")
        v = self.d[self.p:self.p + n]
        self.p += n
        return v

    def u8(self) -> int:
        if self.p >= len(self.d):
            raise RFBProtocolError("truncated ZRLE data")
        v = self.d[self.p]
        self.p += 1
        return v

    def run(self) -> int:
        n = 1
        while True:
            b = self.u8()
            n += b
            if b != 255:
                return n
            if n > 1 << 24:
                raise RFBProtocolError("absurd ZRLE run length")


def _expand_table(bits: int) -> List[bytes]:
    per = 8 // bits
    mask = (1 << bits) - 1
    return [bytes((b >> (8 - bits * (i + 1))) & mask for i in range(per)) for b in range(256)]


_EXPAND = {1: _expand_table(1), 2: _expand_table(2), 4: _expand_table(4)}


class RFB:
    def __init__(self, address: str, password: Optional[str] = None, *, timeout: float = 10.0,
                 encodings: Optional[Sequence[str]] = None, max_pixels: int = DEFAULT_MAX_PIXELS,
                 use_ext_keys: bool = True, probe_after: float = 30.0):
        self.address = address
        self._target = parse_address(address)
        self._password = password
        self.timeout = float(timeout)
        names = list(encodings) if encodings else list(DEFAULT_ENCODINGS)
        for n in names:
            if n.lower() not in ENC_NAMES:
                raise ValueError(f"unknown encoding {n!r}; choose from {sorted(ENC_NAMES)}")
        self._enc_names = [n.lower() for n in names]
        if "raw" not in self._enc_names:
            self._enc_names.append("raw")        # every server must support it; keeps a fallback
        self.max_pixels = int(max_pixels)
        self.use_ext_keys = use_ext_keys
        self.probe_after = probe_after
        self._lock = threading.RLock()
        self._sock: Optional[socket.socket] = None
        self._deadline = 0.0
        self._fb = bytearray()
        self.width = self.height = 0
        self.name = ""
        self.version = (0, 0)
        self.security = 0
        self.ext_keys = False          # QEMU Extended Key Event negotiated on the current connection
        self.connections = 0
        self.resizes = 0
        self.bells = 0
        self.cut_text = ""
        self.stats: Dict[str, int] = {}
        self._zlib = self._zrle = None
        self._last_io = 0.0
        self._resized = False
        self._pending = 0              # requests sent whose pixel answer has not been seen
        self._no_drain = False         # the server merges answers: stop waiting for owed ones

    def __repr__(self) -> str:       # never shows the password
        return f"<RFB {self.address} {'connected %dx%d' % (self.width, self.height) if self._sock else 'closed'}>"

    # --- low level -----------------------------------------------------------------------------------------
    def _begin(self, timeout: Optional[float]) -> None:
        self._deadline = time.monotonic() + (self.timeout if timeout is None else timeout)

    def _read(self, n: int) -> bytes:
        if n < 0 or n > self.max_pixels * 4 + (1 << 20):
            raise RFBProtocolError(f"refusing to read {n} bytes")
        sock = self._sock
        if sock is None:
            raise RFBConnectionError("not connected")
        buf = bytearray(n)
        view, got = memoryview(buf), 0
        while got < n:
            left = self._deadline - time.monotonic()
            if left <= 0:
                raise RFBTimeout(f"timed out reading from {self.address}")
            sock.settimeout(left)
            try:
                k = sock.recv_into(view[got:], n - got)
            except socket.timeout:
                raise RFBTimeout(f"timed out reading from {self.address}") from None
            except OSError as exc:
                raise RFBConnectionError(f"read failed: {exc}") from None
            if k == 0:
                raise RFBConnectionError("the server closed the connection")
            got += k
        self.stats["bytes_in"] = self.stats.get("bytes_in", 0) + n
        return bytes(buf)

    def _skip(self, n: int) -> None:
        while n > 0:
            k = min(n, 1 << 16)
            self._read(k)
            n -= k

    def _send(self, data: bytes) -> None:
        sock = self._sock
        if sock is None:
            raise RFBConnectionError("not connected")
        try:
            sock.settimeout(max(self._deadline - time.monotonic(), 0.5))
            sock.sendall(data)
        except socket.timeout:
            raise RFBTimeout("timed out writing to the server") from None
        except OSError as exc:
            raise RFBConnectionError(f"write failed: {exc}") from None
        self._last_io = time.monotonic()

    def close(self) -> None:
        with self._lock:
            sock, self._sock = self._sock, None
            self.ext_keys = False
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    @property
    def connected(self) -> bool:
        return self._sock is not None

    # --- handshake -----------------------------------------------------------------------------------------
    def connect(self, timeout: Optional[float] = None) -> None:
        """(Re)connect and run the handshake. Raises RFBAuthError/RFBUnsupported/RFBProtocolError/RFBConnectionError."""
        with self._lock:
            self.close()
            self._begin(timeout)
            kind, target = self._target
            try:
                if kind == "unix":
                    if not hasattr(socket, "AF_UNIX"):
                        raise RFBConnectionError("unix sockets are not available on this platform")
                    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    sock.settimeout(self.timeout)
                    sock.connect(target)
                else:
                    sock = socket.create_connection(target, timeout=self.timeout)
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except socket.timeout:
                raise RFBTimeout(f"timed out connecting to {self.address}") from None
            except OSError as exc:
                raise RFBConnectionError(f"cannot connect to {self.address}: {exc}") from None
            self._sock = sock
            self._pending, self._no_drain = 0, False
            try:
                self._handshake()
            except BaseException:
                self.close()
                raise
            self.connections += 1
            self._last_io = time.monotonic()

    def _fail_reason(self) -> str:
        (n,) = struct.unpack(">I", self._read(4))
        if n > 1 << 20:
            raise RFBProtocolError(f"failure reason of {n} bytes is not credible")
        head = self._read(min(n, MAX_FAIL_REASON))
        self._skip(n - len(head))
        return head.decode("utf-8", "replace")

    def _handshake(self) -> None:
        banner = self._read(12)
        m = _VERSION_RE.match(banner)
        if not m:
            raise RFBProtocolError(f"not an RFB server (greeting {banner[:12]!r})")
        major, minor = int(m.group(1)), int(m.group(2))
        if major != 3:
            raise RFBUnsupported(f"unsupported RFB major version {major}")
        use = 8 if minor >= 8 else 7 if minor == 7 else 3      # 3.4-3.6 are 3.3 in practice
        self.version = (3, use)
        self._send(b"RFB 003.%03d\n" % use)
        # security
        if use == 3:
            (sec,) = struct.unpack(">I", self._read(4))
            if sec == 0:
                raise RFBConnectionError("the server refused the connection: " + self._fail_reason())
            offered = [sec]
        else:
            (count,) = struct.unpack(">B", self._read(1))
            if count == 0:
                raise RFBConnectionError("the server refused the connection: " + self._fail_reason())
            offered = list(self._read(count))
        if SEC_VNC in offered and self._password is not None:
            sec = SEC_VNC
        elif SEC_NONE in offered:
            sec = SEC_NONE
        elif SEC_VNC in offered:
            raise RFBAuthError("the server requires a VNC password but none was supplied")
        else:
            names = ", ".join(f"{t} ({SEC_NAMES.get(t, 'unknown')})" for t in offered)
            raise RFBUnsupported(f"the server offers only security types this client lacks: {names}. "
                                 "Supported: None, VNC Authentication. (VeNCrypt/TLS, ARD, Tight security, RSA-AES "
                                 "are not implemented; a local TLS/websocket proxy can bridge them.)")
        self.security = sec
        if use >= 7:
            self._send(bytes([sec]))
        if sec == SEC_VNC:
            challenge = self._read(16)
            self._send(vnc_auth_response(self._password or "", challenge))
        if sec == SEC_VNC or use == 8:
            (res,) = struct.unpack(">I", self._read(4))
            if res != 0:
                reason = self._fail_reason() if use == 8 else ""
                if sec == SEC_VNC and res == 2:
                    reason = reason or "too many authentication failures"
                raise RFBAuthError("VNC authentication failed" + (f": {reason}" if reason else
                                                                    " (wrong password?)"))
        self._send(b"\x01")                                          # ClientInit: shared
        w, h = struct.unpack(">HH", self._read(4))
        self._read(16)                                               # server's native pixel format: ignored
        (nlen,) = struct.unpack(">I", self._read(4))
        if nlen > MAX_NAME:
            raise RFBProtocolError(f"desktop name length {nlen} is not credible")
        self.name = self._read(nlen).decode("utf-8", "replace")
        self._set_size(w, h)
        self._zlib, self._zrle = zlib.decompressobj(), zlib.decompressobj()
        self.ext_keys = False
        encs = [ENC_NAMES[n] for n in self._enc_names] + [ENC_DESKTOP_SIZE, ENC_EXT_DESKTOP_SIZE]
        if self.use_ext_keys:
            encs.append(ENC_QEMU_EXT_KEY)
        self._send(struct.pack(">Bxxx", 0) + _PIXEL_FORMAT +
                   struct.pack(">BxH", 2, len(encs)) + b"".join(struct.pack(">i", e) for e in encs))
        self._request(False)
        self._pump_update()                  # first picture; also where QEMU announces the extended key event

    def _set_size(self, w: int, h: int) -> None:
        if w == 0 or h == 0:
            raise RFBProtocolError(f"the server reports an empty display ({w}x{h})")
        if w > MAX_SIDE or h > MAX_SIDE or w * h > self.max_pixels:
            raise RFBProtocolError(f"display {w}x{h} exceeds the limit of {self.max_pixels} pixels")
        if (w, h) != (self.width, self.height) or not self._fb:
            self.width, self.height = w, h
            self._fb = bytearray(w * h * 4)

    # --- server messages -----------------------------------------------------------------------------------
    def _request(self, incremental: bool) -> None:
        self._pending += 1
        self._send(struct.pack(">BBHHHH", 3, 1 if incremental else 0, 0, 0, self.width, self.height))

    def _pump_update(self) -> None:
        """Read server messages until an update that carries pixels has been applied.

        Updates without pixels are skipped (QEMU sends unsolicited pseudo-encoding updates, such as the extended
        key event acknowledgement, which must not be taken for the answer to a request: that would leave the real
        answer in flight and make the *next* capture one request stale). A resize-only update means the server
        either still owes the pixels (QEMU) or consumed the request (TigerVNC style): wait a short grace for the
        pixels, then ask again."""
        loops, grace = 0, None
        while True:
            if grace is not None:
                left = grace - time.monotonic()
                if left <= 0 or not select.select([self._sock], [], [], left)[0]:
                    grace = None
                    loops += 1
                    if loops > MAX_RESIZE_LOOPS:
                        raise RFBProtocolError("the display keeps changing size")
                    self._request(False)
                    continue
            self._resized = False
            pixels = self._read_message()
            if pixels is None:
                continue
            if pixels:
                return
            if self._resized:
                grace = time.monotonic() + RESIZE_GRACE

    def _drain_owed(self) -> None:
        """Consume answers still owed to earlier requests, so the next picture cannot be one request old."""
        if self._pending <= 0 or self._no_drain or self._sock is None:
            return
        end = min(self._deadline, time.monotonic() + DRAIN_GRACE)
        while self._pending > 0:
            left = end - time.monotonic()
            if left <= 0 or not select.select([self._sock], [], [], left)[0]:
                break
            self._read_message()
        if self._pending > 0:         # a server that merges answers never pays them: stop waiting for them
            self._no_drain = True
            self._pending = 0

    def _read_message(self) -> Optional[int]:
        """One server message. For a FramebufferUpdate: the number of pixel rectangles in it; else None."""
        (mtype,) = self._read(1)
        if mtype == 0:
            self._read(1)
            (nrects,) = struct.unpack(">H", self._read(2))
            pixels = 0
            for _ in range(nrects):
                kind = self._read_rect()
                if kind is None:
                    break
                pixels += kind
            self.stats["updates"] = self.stats.get("updates", 0) + 1
            if pixels:
                self._pending = max(0, self._pending - 1)
            return pixels
        if mtype == 1:                                       # SetColourMapEntries (we are true colour): discard
            _, first, n = struct.unpack(">BHH", self._read(5))
            self._skip(min(n * 6, MAX_COLOURMAP))
        elif mtype == 2:
            self.bells += 1
        elif mtype == 3:                                     # ServerCutText
            self._read(3)
            (n,) = struct.unpack(">i", self._read(4))
            n = -n if n < 0 else n                           # negative = extended clipboard message
            if n > MAX_CUT_TEXT:
                raise RFBProtocolError(f"cut text of {n} bytes is not credible")
            head = self._read(min(n, 4096))
            self._skip(n - len(head))
            self.cut_text = head.decode("latin-1", "replace")
        else:
            raise RFBProtocolError(f"unknown server message type {mtype}")
        return None

    def _read_rect(self) -> Optional[int]:
        """One rectangle. None = LastRect (stop); 1 = it carried pixels; 0 = a pseudo-encoding."""
        x, y, w, h, enc = struct.unpack(">HHHHi", self._read(12))
        self.stats["rects"] = self.stats.get("rects", 0) + 1
        if enc == ENC_LAST_RECT:
            return None
        if enc == ENC_DESKTOP_SIZE:
            self._resize_to(w, h)
        elif enc == ENC_EXT_DESKTOP_SIZE:
            (nscreens,) = struct.unpack(">Bxxx", self._read(4))
            self._skip(nscreens * 16)
            if y == 0:                                       # y is the status code; non-zero = a refused request
                self._resize_to(w, h)
        elif enc == ENC_QEMU_EXT_KEY:
            self.ext_keys = True
        elif enc in (ENC_RAW, ENC_COPYRECT, ENC_HEXTILE, ENC_ZLIB, ENC_ZRLE):
            if x + w > self.width or y + h > self.height:
                raise RFBProtocolError(f"rectangle {w}x{h}+{x}+{y} lies outside the {self.width}x{self.height} display")
            if w and h:
                {ENC_RAW: self._dec_raw, ENC_COPYRECT: self._dec_copyrect, ENC_HEXTILE: self._dec_hextile,
                 ENC_ZLIB: self._dec_zlib, ENC_ZRLE: self._dec_zrle}[enc](x, y, w, h)
            self.stats["enc%d" % enc] = self.stats.get("enc%d" % enc, 0) + 1
            return 1 if w and h else 0
        else:
            raise RFBProtocolError(f"the server used encoding {enc}, which was not requested")
        return 0

    def _resize_to(self, w: int, h: int) -> None:
        if (w, h) == (self.width, self.height):
            return
        self._fb = bytearray()
        self._set_size(w, h)
        self.resizes += 1
        self._resized = True

    # --- decoders ------------------------------------------------------------------------------------------
    def _put_rows(self, x: int, y: int, w: int, h: int, data: bytes) -> None:
        fb, stride, row = self._fb, self.width * 4, w * 4
        if w == self.width:
            fb[y * stride:(y + h) * stride] = data
            return
        o = y * stride + x * 4
        for i in range(h):
            fb[o:o + row] = data[i * row:(i + 1) * row]
            o += stride

    def _fill(self, x: int, y: int, w: int, h: int, pix: bytes) -> None:
        fb, stride = self._fb, self.width * 4
        line = pix * w
        o = y * stride + x * 4
        for _ in range(h):
            fb[o:o + w * 4] = line
            o += stride

    def _dec_raw(self, x, y, w, h):
        self._put_rows(x, y, w, h, self._read(w * h * 4))

    def _dec_copyrect(self, x, y, w, h):
        sx, sy = struct.unpack(">HH", self._read(4))
        if sx + w > self.width or sy + h > self.height:
            raise RFBProtocolError("CopyRect source lies outside the display")
        stride, row = self.width * 4, w * 4
        rows = [bytes(self._fb[(sy + i) * stride + sx * 4:(sy + i) * stride + sx * 4 + row]) for i in range(h)]
        self._put_rows(x, y, w, h, b"".join(rows))

    def _dec_zlib(self, x, y, w, h):
        (n,) = struct.unpack(">I", self._read(4))
        raw = w * h * 4
        if n > 2 * raw + 4096:
            raise RFBProtocolError(f"Zlib rectangle claims {n} compressed bytes for {raw} raw")
        comp = self._read(n)
        try:
            data = self._zlib.decompress(comp, raw)
        except zlib.error as exc:
            raise RFBProtocolError(f"bad zlib data: {exc}") from None
        if self._zlib.unconsumed_tail or len(data) != raw:
            raise RFBProtocolError("Zlib rectangle expands to an unexpected size")
        self._put_rows(x, y, w, h, data)

    def _dec_hextile(self, x, y, w, h):
        bg = fg = b"\0\0\0\0"
        for ty in range(y, y + h, 16):
            th = min(16, y + h - ty)
            for tx in range(x, x + w, 16):
                tw = min(16, x + w - tx)
                (sub,) = self._read(1)
                if sub & 1:
                    self._put_rows(tx, ty, tw, th, self._read(tw * th * 4))
                    continue
                if sub & 2:
                    bg = self._read(4)
                if sub & 4:
                    fg = self._read(4)
                self._fill(tx, ty, tw, th, bg)
                if sub & 8:
                    (count,) = self._read(1)
                    coloured = bool(sub & 16)
                    data = self._read(count * (6 if coloured else 2))
                    step = 6 if coloured else 2
                    for i in range(count):
                        rec = data[i * step:(i + 1) * step]
                        pix, xy, wh = (rec[:4], rec[4], rec[5]) if coloured else (fg, rec[0], rec[1])
                        sx, sy, sw, sh = xy >> 4, xy & 15, (wh >> 4) + 1, (wh & 15) + 1
                        if sx + sw > tw or sy + sh > th:
                            raise RFBProtocolError("Hextile subrectangle lies outside its tile")
                        self._fill(tx + sx, ty + sy, sw, sh, pix)

    def _dec_zrle(self, x, y, w, h):
        (n,) = struct.unpack(">I", self._read(4))
        px = w * h
        if n > 2 * px * 4 + 65536:
            raise RFBProtocolError(f"ZRLE rectangle claims {n} compressed bytes for {px} pixels")
        comp = self._read(n)
        tiles = ((w + 63) // 64) * ((h + 63) // 64)
        try:
            data = self._zrle.decompress(comp, px * 4 + tiles * 70 + 4096)
        except zlib.error as exc:
            raise RFBProtocolError(f"bad ZRLE zlib data: {exc}") from None
        if self._zrle.unconsumed_tail:
            raise RFBProtocolError("ZRLE rectangle expands beyond any plausible size")
        cur = _Cursor(data)
        for ty in range(y, y + h, 64):
            th = min(64, y + h - ty)
            for tx in range(x, x + w, 64):
                tw = min(64, x + w - tx)
                self._put_rows(tx, ty, tw, th, self._zrle_tile(cur, tw, th))

    @staticmethod
    def _zrle_tile(cur: _Cursor, tw: int, th: int) -> bytes:
        npx = tw * th
        sub = cur.u8()
        rle, psize = sub & 0x80, sub & 0x7F
        pal: List[bytes] = []
        if not rle:
            if psize == 0:
                d = cur.take(npx * 3)
                tile = bytearray(npx * 4)
                tile[0::4], tile[1::4], tile[2::4] = d[0::3], d[1::3], d[2::3]
                return bytes(tile)
            if psize == 1:
                return (cur.take(3) + b"\0") * npx
            if psize > 16:
                raise RFBProtocolError(f"invalid ZRLE tile type {sub}")
            pal = [cur.take(3) for _ in range(psize)]
            bits = 1 if psize == 2 else 2 if psize <= 4 else 4
            per_row = (tw * bits + 7) // 8
            table = _EXPAND[bits]
            idx = b"".join(b"".join(table[b] for b in cur.take(per_row))[:tw] for _ in range(th))
            if max(idx) >= psize:
                raise RFBProtocolError("ZRLE palette index out of range")
            tile = bytearray(npx * 4)
            for k in range(3):
                tile[k::4] = idx.translate(bytes([pal[i][k] for i in range(psize)]).ljust(256, b"\0"))
            return bytes(tile)
        out: List[bytes] = []
        count = 0
        if psize == 0:
            while count < npx:
                pix = cur.take(3) + b"\0"
                run = cur.run()
                count += run
                if count > npx:
                    raise RFBProtocolError("ZRLE run overflows its tile")
                out.append(pix * run)
        else:
            if psize == 1:
                raise RFBProtocolError(f"invalid ZRLE tile type {sub}")
            pal = [cur.take(3) + b"\0" for _ in range(psize)]
            while count < npx:
                b = cur.u8()
                i = b & 0x7F
                if i >= psize:
                    raise RFBProtocolError("ZRLE palette index out of range")
                run = cur.run() if b & 0x80 else 1
                count += run
                if count > npx:
                    raise RFBProtocolError("ZRLE run overflows its tile")
                out.append(pal[i] * run)
        return b"".join(out)

    # --- connection health ---------------------------------------------------------------------------------
    def _ensure(self) -> None:
        """Connected and, as far as can be told without traffic, alive; otherwise reconnect."""
        if self._sock is None:
            self.connect()
            return
        try:
            self._begin(None)
            while select.select([self._sock], [], [], 0)[0]:
                peek = self._sock.recv(1, socket.MSG_PEEK)
                if not peek:
                    raise RFBConnectionError("the server closed the connection")
                self._read_message()                       # Bell / cut text / stray update
        except (RFBError, OSError):
            self.connect()
            return
        if time.monotonic() - self._last_io > self.probe_after:
            try:                                           # a half-dead TCP connection looks fine until used
                self._begin(None)
                self._request(False)
                self._pump_update()
            except (RFBConnectionError, OSError):
                self.connect()

    def ensure(self) -> None:
        """Connect if needed; reconnect if the connection died while idle."""
        with self._lock:
            self._ensure()

    # --- public: eyes --------------------------------------------------------------------------------------
    def update(self, timeout: Optional[float] = None) -> None:
        """Ask for the whole screen and apply the answer. Reconnects once if the connection had dropped."""
        with self._lock:
            for attempt in (0, 1):
                try:
                    self._ensure()
                    self._begin(timeout)
                    self._drain_owed()
                    self._request(False)
                    self._pump_update()
                    return
                except RFBTimeout:
                    self.close()
                    raise
                except RFBConnectionError:
                    self.close()
                    if attempt:
                        raise
                except BaseException:
                    self.close()          # protocol violation or interrupt: the stream can no longer be trusted
                    raise

    def frame(self, timeout: Optional[float] = None) -> Tuple[int, int, bytes]:
        """(width, height, RGB bytes) of the screen as it is now."""
        with self._lock:
            self.update(timeout)
            fb, n = self._fb, self.width * self.height
            rgb = bytearray(n * 3)
            rgb[0::3], rgb[1::3], rgb[2::3] = fb[2::4], fb[1::4], fb[0::4]
            return self.width, self.height, bytes(rgb)

    # --- public: hands -------------------------------------------------------------------------------------
    @staticmethod
    def key_msg(keysym: int, down: bool) -> bytes:
        return struct.pack(">BBHI", 4, 1 if down else 0, 0, keysym & 0xFFFFFFFF)

    @staticmethod
    def ext_key_msg(keysym: int, scancode: int, down: bool) -> bytes:
        """QEMU Extended Key Event: message 255/0, down flag, keysym, XT scancode (0xE0 prefix as 0x80|code)."""
        return struct.pack(">BBHII", 255, 0, 1 if down else 0, keysym & 0xFFFFFFFF, scancode)

    @staticmethod
    def pointer_msg(x: int, y: int, buttons: int) -> bytes:
        return struct.pack(">BBHH", 5, buttons & 0xFF, max(0, min(int(x), 65535)), max(0, min(int(y), 65535)))

    def send(self, data: bytes) -> None:
        """Send already-encoded client messages (one write, so a chord's key-downs reach the server together).
        Reconnects first if the connection died while idle; never resends after a partial failure."""
        with self._lock:
            self._ensure()
            self._begin(None)
            try:
                self._send(data)
            except RFBConnectionError:
                self.close()
                raise

    def pointer(self, x: int, y: int, buttons: int = 0) -> None:
        self.send(self.pointer_msg(x, y, buttons))
