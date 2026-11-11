"""A scriptable fake RFB server for tests: handshake variants, auth, Raw/CopyRect/Hextile/Zlib/ZRLE updates,
resize, QEMU extended key events, and hostile behaviours. Listens on 127.0.0.1 (ephemeral port)."""

from __future__ import annotations

import os
import socket
import struct
import threading
import time
import zlib
from typing import Any, Dict, List, Optional, Tuple

from barehand import rfb

PF = struct.pack(">BBBBHHHBBBxxx", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)


def test_image(w: int, h: int) -> bytearray:
    """BGRX picture with solid areas, a 2-colour stripe area, a 5-colour area and a gradient."""
    fb = bytearray(w * h * 4)
    for y in range(h):
        for x in range(w):
            if x < w // 4:
                c = (x * 255 // max(w // 4, 1), y * 255 // max(h, 1), (x * y) & 255)         # gradient
            elif x < w // 2:
                c = (255, 255, 255) if (x // 3 + y // 5) % 2 else (0, 0, 0)                   # 2 colours
            elif x < 3 * w // 4:
                c = [(200, 0, 0), (0, 200, 0), (0, 0, 200), (200, 200, 0), (9, 9, 9)][(x + 2 * y) % 5]
            else:
                c = (10, 20, 30)                                                              # solid
            o = (y * w + x) * 4
            fb[o:o + 4] = bytes((c[2], c[1], c[0], 0))
    return fb


_BOMB: List[bytes] = []


def bomb() -> bytes:
    """50 MB of zeros, compressed (cached so memory measurements do not see the server building it)."""
    if not _BOMB:
        _BOMB.append(zlib.compress(bytes(50_000_000), 9))
    return _BOMB[0]


def fb_to_rgb(fb: bytes) -> bytes:
    n = len(fb) // 4
    out = bytearray(n * 3)
    out[0::3], out[1::3], out[2::3] = fb[2::4], fb[1::4], fb[0::4]
    return bytes(out)


class FakeRFB:
    def __init__(self, width: int = 150, height: int = 100, *, version: str = "3.8", security=(1,),
                 password: Optional[str] = None, encoding: str = "raw", name: str = "fake", ext_keys: bool = False,
                 hostile: Optional[str] = None, refuse: Optional[str] = None, qemu_like: bool = False):
        self.width, self.height = width, height
        self.version, self.security, self.password = version, tuple(security), password
        self.encoding, self.name, self.ext_keys = encoding, name, ext_keys
        self.hostile, self.refuse = hostile, refuse
        self.qemu_like = qemu_like     # unsolicited pseudo-only updates (ext-key ack, resize), like QEMU's server
        self.fb = test_image(width, height)
        self.messages: List[Tuple] = []
        self.connections = 0
        self.auth_results: List[bool] = []
        self.pending_resize: Optional[Tuple[int, int]] = None
        self.copy_next: Optional[Tuple[int, int, int, int, int, int]] = None   # sx, sy, w, h, dx, dy
        self.drop_next_request = False
        self._lock = threading.Lock()
        self._conns: List[socket.socket] = []
        self._stop = False
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(8)
        self.port = self._srv.getsockname()[1]
        self.address = f"127.0.0.1::{self.port}"
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    # --- control ----------------------------------------------------------------------------------------
    def stop(self) -> None:
        self._stop = True
        try:
            self._srv.close()
        except OSError:
            pass
        for c in list(self._conns):
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            c.close()

    def drop_all(self) -> None:
        """Close every client connection (the server keeps listening)."""
        for c in list(self._conns):
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def fill(self, x: int, y: int, w: int, h: int, rgb: Tuple[int, int, int]) -> None:
        px = bytes((rgb[2], rgb[1], rgb[0], 0))
        for yy in range(y, y + h):
            self.fb[(yy * self.width + x) * 4:(yy * self.width + x + w) * 4] = px * w

    def resize(self, w: int, h: int) -> None:
        self.pending_resize = (w, h)

    def events(self, kind: str) -> List[Tuple]:
        return [m for m in self.messages if m[0] == kind]

    # --- connection handling ----------------------------------------------------------------------------
    def _accept(self) -> None:
        while not self._stop:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            self._conns.append(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    @staticmethod
    def _recv(conn: socket.socket, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            c = conn.recv(n - len(buf))
            if not c:
                raise EOFError
            buf += c
        return buf

    def _serve(self, conn: socket.socket) -> None:
        try:
            self.connections += 1
            self._handshake(conn)
            self._loop(conn)
        except (EOFError, OSError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _handshake(self, conn: socket.socket) -> None:
        h = self.hostile
        if h == "http":
            conn.sendall(b"HTTP/1.1 400 Bad\r\n")
            raise EOFError
        ver = {"3.3": b"RFB 003.003\n", "3.7": b"RFB 003.007\n", "3.8": b"RFB 003.008\n",
               "3.889": b"RFB 003.889\n", "4.0": b"RFB 004.000\n", "3.5": b"RFB 003.005\n"}[self.version]
        conn.sendall(ver)
        reply = self._recv(conn, 12)
        self.messages.append(("version", reply))
        minor = 3 if self.version in ("3.3", "3.5") else 7 if self.version == "3.7" else 8
        if self.refuse:
            r = self.refuse.encode()
            if minor == 3:
                conn.sendall(struct.pack(">I", 0) + struct.pack(">I", len(r)) + r)
            else:
                conn.sendall(b"\0" + struct.pack(">I", len(r)) + r)
            raise EOFError
        if h == "huge_reason":
            conn.sendall(b"\0" + struct.pack(">I", 0x7FFFFFF0))
            time.sleep(0.5)
            raise EOFError
        chosen = None
        if minor == 3:
            chosen = self.security[0]
            conn.sendall(struct.pack(">I", chosen))
        else:
            conn.sendall(bytes([len(self.security)]) + bytes(self.security))
            chosen = self._recv(conn, 1)[0]
        if chosen == 2:
            challenge = os.urandom(16)
            conn.sendall(challenge)
            resp = self._recv(conn, 16)
            ok = self.password is not None and resp == rfb.vnc_auth_response(self.password, challenge)
            self.auth_results.append(ok)
            if not ok:
                conn.sendall(struct.pack(">I", 1))
                if minor == 8:
                    msg = b"Authentication failed"
                    conn.sendall(struct.pack(">I", len(msg)) + msg)
                raise EOFError
            conn.sendall(struct.pack(">I", 0))
        elif chosen == 1:
            if minor == 8:
                conn.sendall(struct.pack(">I", 0))
        else:
            raise EOFError                                    # a type the client should never have picked
        self._recv(conn, 1)                                   # ClientInit
        name = self.name.encode()
        w, h2 = self.width, self.height
        if h == "huge_size":
            w, h2 = 65535, 65535
        nlen = 0xFFFFFFF0 if h == "huge_name" else len(name)
        conn.sendall(struct.pack(">HH", w, h2) + PF + struct.pack(">I", nlen) + (name if h != "huge_name" else b""))
        if h in ("huge_name", "huge_size"):
            time.sleep(0.5)
            raise EOFError

    def _loop(self, conn: socket.socket) -> None:
        zl = zlib.compressobj()
        zr = zlib.compressobj()
        announced = False
        encs: List[int] = []
        while True:
            t = self._recv(conn, 1)[0]
            if t == 0:
                self._recv(conn, 3)
                self.messages.append(("setpf", self._recv(conn, 16)))
            elif t == 2:
                self._recv(conn, 1)
                (n,) = struct.unpack(">H", self._recv(conn, 2))
                encs = [struct.unpack(">i", self._recv(conn, 4))[0] for _ in range(n)]
                self.messages.append(("setenc", encs))
                if self.qemu_like and self.ext_keys and -258 in encs:
                    conn.sendall(struct.pack(">BxH", 0, 1) + struct.pack(">HHHHi", 0, 0, 0, 0, -258))
                    announced = True
            elif t == 3:
                inc, x, y, w, h = struct.unpack(">BHHHH", self._recv(conn, 9))
                self.messages.append(("fbreq", inc, x, y, w, h))
                if self.drop_next_request:
                    self.drop_next_request = False
                    continue
                self._answer(conn, inc, encs, zl, zr, announced)
                announced = announced or (-258 in encs and self.ext_keys)
            elif t == 4:
                down, _, sym = struct.unpack(">BHI", self._recv(conn, 7))
                self.messages.append(("key", down, sym))
            elif t == 5:
                mask, x, y = struct.unpack(">BHH", self._recv(conn, 5))
                self.messages.append(("pointer", mask, x, y))
            elif t == 6:
                self._recv(conn, 3)
                (n,) = struct.unpack(">I", self._recv(conn, 4))
                self._recv(conn, n)
            elif t == 255:
                sub = self._recv(conn, 1)[0]
                if sub == 0:
                    down, sym, code = struct.unpack(">HII", self._recv(conn, 10))
                    self.messages.append(("extkey", down, sym, code))
                else:
                    raise EOFError
            else:
                raise EOFError

    # --- updates ----------------------------------------------------------------------------------------
    def _answer(self, conn, incremental, encs, zl, zr, announced) -> None:
        h = self.hostile
        if h == "bell_storm":
            end = time.monotonic() + 30
            while time.monotonic() < end:
                conn.sendall(b"\x02" * 4096)
            return
        if h == "bad_msg":
            conn.sendall(b"\x63")
            return
        if h == "cut_text":
            conn.sendall(b"\x03\0\0\0" + struct.pack(">I", 0x70000000))
            time.sleep(0.3)
            return
        if h == "stall":
            conn.sendall(struct.pack(">BxH", 0, 1) + struct.pack(">HHHHi", 0, 0, self.width, self.height, 0)
                         + bytes(100))
            time.sleep(30)
            return
        if h == "close_mid":
            conn.sendall(struct.pack(">BxH", 0, 1) + struct.pack(">HHHHi", 0, 0, self.width, self.height, 0)
                         + bytes(100))
            raise EOFError
        if h == "rect_oob":
            conn.sendall(struct.pack(">BxH", 0, 1) + struct.pack(">HHHHi", self.width - 1, 0, 50, 50, 0) + bytes(10000))
            return
        if h == "unrequested_enc":
            conn.sendall(struct.pack(">BxH", 0, 1) + struct.pack(">HHHHi", 0, 0, 4, 4, 7) + bytes(100))
            return
        if h == "zlib_bomb":
            bomb_ = bomb()
            conn.sendall(struct.pack(">BxH", 0, 1) + struct.pack(">HHHHi", 0, 0, self.width, self.height, 6)
                         + struct.pack(">I", len(bomb_)) + bomb_)
            return
        if h == "zrle_bomb":
            bomb_ = bomb()
            conn.sendall(struct.pack(">BxH", 0, 1) + struct.pack(">HHHHi", 0, 0, self.width, self.height, 16)
                         + struct.pack(">I", len(bomb_)) + bomb_)
            return
        if h == "huge_len":
            conn.sendall(struct.pack(">BxH", 0, 1) + struct.pack(">HHHHi", 0, 0, self.width, self.height, 16)
                         + struct.pack(">I", 0xFFFFFFF0))
            time.sleep(0.5)
            return
        if h == "many_rects":
            conn.sendall(struct.pack(">BxH", 0, 65535) + b"".join(
                struct.pack(">HHHHi", 0, 0, 1, 1, 0) + b"\0\0\0\0" for _ in range(65535)))
            return
        if h == "resize_storm":
            self.width += 1
            conn.sendall(struct.pack(">BxH", 0, 1) + struct.pack(">HHHHi", 0, 0, self.width, self.height, -223))
            return
        if h == "bad_palette":
            body = zr.compress(b"\x03" + bytes(9) + bytes([0xFF] * 64 * 16)) + zr.flush(zlib.Z_SYNC_FLUSH)
            conn.sendall(struct.pack(">BxH", 0, 1) + struct.pack(">HHHHi", 0, 0, 64, 64, 16)
                         + struct.pack(">I", len(body)) + body)
            return
        out = bytearray()
        rects: List[bytes] = []
        if self.ext_keys and -258 in encs and not announced:
            rects.append(struct.pack(">HHHHi", 0, 0, 0, 0, -258))
        if self.pending_resize and (-223 in encs or -308 in encs):
            self.width, self.height = self.pending_resize
            self.pending_resize = None
            self.fb = test_image(self.width, self.height)
            if -308 in encs:
                rects.append(struct.pack(">HHHHi", 0, 0, self.width, self.height, -308)
                             + struct.pack(">Bxxx", 1) + struct.pack(">IHHHHI", 1, 0, 0, self.width, self.height, 0))
            else:
                rects.append(struct.pack(">HHHHi", 0, 0, self.width, self.height, -223))
            out += struct.pack(">BxH", 0, len(rects)) + b"".join(rects)
            conn.sendall(bytes(out))
            if not self.qemu_like:
                return               # the resize consumed the request: the client must ask again
            out, rects = bytearray(), []      # QEMU: the pending request is still answered, with the new picture
        if self.copy_next:
            sx, sy, w, h2, dx, dy = self.copy_next
            self.copy_next = None
            stride = self.width * 4
            rows = [bytes(self.fb[(sy + i) * stride + sx * 4:(sy + i) * stride + (sx + w) * 4]) for i in range(h2)]
            for i, r in enumerate(rows):
                self.fb[(dy + i) * stride + dx * 4:(dy + i) * stride + (dx + w) * 4] = r
            rects.append(struct.pack(">HHHHi", dx, dy, w, h2, 1) + struct.pack(">HH", sx, sy))
            out += struct.pack(">BxH", 0, len(rects)) + b"".join(rects)
            conn.sendall(bytes(out))
            return
        enc = self.encoding
        body = self._encode(enc, 0, 0, self.width, self.height, zl, zr)
        codes = {"raw": 0, "hextile": 5, "hextile-rich": 5, "zlib": 6, "zrle": 16}
        rects.append(struct.pack(">HHHHi", 0, 0, self.width, self.height, codes[enc]) + body)
        out += struct.pack(">BxH", 0, len(rects)) + b"".join(rects)
        conn.sendall(bytes(out))
        if self.hostile == "bell_after":
            conn.sendall(b"\x02\x03\0\0\0\0\0\0\x05hello")

    def _pix(self, x: int, y: int) -> bytes:
        o = (y * self.width + x) * 4
        return bytes(self.fb[o:o + 4])

    def _tile(self, x: int, y: int, w: int, h: int) -> List[bytes]:
        return [self._pix(x + i, y + j) for j in range(h) for i in range(w)]

    def _encode(self, enc: str, x: int, y: int, w: int, h: int, zl, zr) -> bytes:
        if enc == "raw":
            return b"".join(bytes(self.fb[((y + j) * self.width + x) * 4:((y + j) * self.width + x + w) * 4])
                            for j in range(h))
        if enc == "zlib":
            raw = self._encode("raw", x, y, w, h, zl, zr)
            data = zl.compress(raw) + zl.flush(zlib.Z_SYNC_FLUSH)
            return struct.pack(">I", len(data)) + data
        if enc in ("hextile", "hextile-rich"):
            return self._hextile(x, y, w, h, enc == "hextile-rich")
        if enc == "zrle":
            data = zr.compress(self._zrle(x, y, w, h)) + zr.flush(zlib.Z_SYNC_FLUSH)
            return struct.pack(">I", len(data)) + data
        raise ValueError(enc)

    def _hextile(self, x0, y0, w, h, rich) -> bytes:
        out = bytearray()
        bg_prev = None
        for ty in range(y0, y0 + h, 16):
            th = min(16, y0 + h - ty)
            for tx in range(x0, x0 + w, 16):
                tw = min(16, x0 + w - tx)
                px = self._tile(tx, ty, tw, th)
                colours = {}
                for p in px:
                    colours[p] = colours.get(p, 0) + 1
                if len(colours) == 1:
                    (c,) = colours
                    if c == bg_prev:
                        out.append(0)
                    else:
                        out.append(2)
                        out += c
                        bg_prev = c
                    continue
                if not rich or len(colours) > 6:
                    out.append(1)
                    out += b"".join(px)
                    continue
                bg = max(colours, key=colours.get)
                subs = []
                for j in range(th):          # one subrect per horizontal run of a non-background colour
                    i = 0
                    while i < tw:
                        p = px[j * tw + i]
                        if p == bg:
                            i += 1
                            continue
                        k = i
                        while k + 1 < tw and px[j * tw + k + 1] == p:
                            k += 1
                        subs.append((p, i, j, k - i + 1, 1))
                        i = k + 1
                if len(subs) > 255:
                    out.append(1)
                    out += b"".join(px)
                    continue
                sub = 8 | 16
                head = bytearray()
                if bg != bg_prev:
                    sub |= 2
                    head += bg
                    bg_prev = bg
                out.append(sub)
                out += head
                out.append(len(subs))
                for p, i, j, sw, sh in subs:
                    out += p[:4] + bytes([(i << 4) | j, ((sw - 1) << 4) | (sh - 1)])
        return bytes(out)

    def _zrle(self, x0, y0, w, h) -> bytes:
        out = bytearray()
        n = 0
        for ty in range(y0, y0 + h, 64):
            th = min(64, y0 + h - ty)
            for tx in range(x0, x0 + w, 64):
                tw = min(64, x0 + w - tx)
                px = self._tile(tx, ty, tw, th)
                pal: List[bytes] = []
                for p in px:
                    if p not in pal:
                        pal.append(p)
                    if len(pal) > 127:
                        break
                n += 1
                if len(pal) == 1:
                    out.append(1)
                    out += pal[0][:3]
                elif len(pal) <= 16 and n % 2:
                    bits = 1 if len(pal) == 2 else 2 if len(pal) <= 4 else 4
                    out.append(len(pal))
                    for p in pal:
                        out += p[:3]
                    for j in range(th):
                        acc, nb = 0, 0
                        row = bytearray()
                        for i in range(tw):
                            acc = (acc << bits) | pal.index(px[j * tw + i])
                            nb += bits
                            if nb == 8:
                                row.append(acc)
                                acc, nb = 0, 0
                        if nb:
                            row.append(acc << (8 - nb))
                        out += row
                elif len(pal) <= 127 and n % 3 == 0:
                    out.append(128 + len(pal))                      # palette RLE
                    for p in pal:
                        out += p[:3]
                    i = 0
                    while i < len(px):
                        k = i
                        while k + 1 < len(px) and px[k + 1] == px[i]:
                            k += 1
                        run = k - i + 1
                        idx = pal.index(px[i])
                        if run == 1:
                            out.append(idx)
                        else:
                            out.append(idx | 0x80)
                            r = run - 1
                            while r >= 255:
                                out.append(255)
                                r -= 255
                            out.append(r)
                        i = k + 1
                elif n % 5 == 0:
                    out.append(128)                                  # plain RLE
                    i = 0
                    while i < len(px):
                        k = i
                        while k + 1 < len(px) and px[k + 1] == px[i]:
                            k += 1
                        run = k - i + 1
                        out += px[i][:3]
                        r = run - 1
                        while r >= 255:
                            out.append(255)
                            r -= 255
                        out.append(r)
                        i = k + 1
                else:
                    out.append(0)
                    out += b"".join(p[:3] for p in px)
        return bytes(out)
