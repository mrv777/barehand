"""A fake QEMU QMP server (one client at a time, like the real thing) with a synthetic display."""

import json
import socket
import struct
import threading
import time
import zlib
from typing import Any, Dict, List, Optional, Tuple


def png_from_rgb(w: int, h: int, rgb: bytes) -> bytes:
    raw = b"".join(b"\x00" + rgb[y * w * 3:(y + 1) * w * 3] for y in range(h))
    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def make_png(w: int, h: int, shade: int) -> bytes:
    return png_from_rgb(w, h, bytes((shade, (shade * 3) % 256, 0)) * (w * h))


class FakeQMP:
    def __init__(self, width: int = 128, height: int = 96):
        self.width, self.height = width, height
        self.pixels = bytearray(bytes((10, 30, 0)) * (width * height))
        self.cursor: Optional[Tuple[int, int, int, int]] = None   # x, y, w, h: blinks on every other dump
        self.blink_every = 1         # dumps per half blink period
        self.animated = False        # every frame differs: the screen never settles
        self.react_to_input = True
        self.on_input = None         # optional callable(FakeQMP) run instead of the full-screen reaction
        self.fail_commands: Dict[str, str] = {}
        self.commands: List[Dict[str, Any]] = []
        self.hmp_replies: Dict[str, str] = {}
        self.status = "running"
        self.events = False          # emit async QMP events before every response
        self.chunked = False         # deliver every response in 3-byte pieces
        self.greeting_delay = 0.0
        self.drop_skip = 0
        self.drop_on: Optional[str] = None   # close the connection when this command arrives
        self.dumps = 0
        self.mice = [{"index": 3, "name": "QEMU HID Tablet", "current": False, "absolute": True}]
        self.blocks = [{"device": "cd0", "qdev": "cdrom0", "removable": True, "inserted": {"file": "/x.iso"}}]
        self._srv = socket.socket()
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(4)
        self._srv.settimeout(0.2)
        self.address = "127.0.0.1:%d" % self._srv.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self.connections = 0

    # --- display helpers ---
    def paint(self, x: int, y: int, w: int, h: int, color: Tuple[int, int, int]) -> None:
        for yy in range(y, min(y + h, self.height)):
            for xx in range(x, min(x + w, self.width)):
                o = (yy * self.width + xx) * 3
                self.pixels[o:o + 3] = bytes(color)

    def fill(self, shade: int) -> None:
        self.pixels[:] = bytes((shade, (shade * 3) % 256, 0)) * (self.width * self.height)

    def resize(self, w: int, h: int) -> None:
        self.width, self.height = w, h
        self.pixels = bytearray(bytes((10, 30, 0)) * (w * h))

    def _frame(self) -> bytes:
        self.dumps += 1
        if self.animated:
            self.fill((self.dumps * 7) % 250)
        data = bytes(self.pixels)
        if self.cursor and (self.dumps // self.blink_every) % 2 == 0:
            buf = bytearray(data)
            x, y, w, h = self.cursor
            for yy in range(y, min(y + h, self.height)):
                for xx in range(x, min(x + w, self.width)):
                    o = (yy * self.width + xx) * 3
                    buf[o:o + 3] = b"\xff\xff\xff"
            data = bytes(buf)
        return data

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=3)
        self._srv.close()

    # --- helpers for assertions ---
    def calls(self, name: str) -> List[Dict[str, Any]]:
        return [c for c in self.commands if c["execute"] == name]

    def input_events(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for c in self.calls("input-send-event"):
            out.extend(c["arguments"]["events"])
        return out

    # --- server ---
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except (socket.timeout, OSError):
                continue
            self.connections += 1
            try:
                self._serve(conn)
            finally:
                conn.close()

    def _send(self, conn: socket.socket, obj: Dict[str, Any]) -> None:
        data = json.dumps(obj).encode() + b"\n"
        if self.chunked:
            for i in range(0, len(data), 3):
                conn.sendall(data[i:i + 3])
                time.sleep(0.0005)
        else:
            conn.sendall(data)

    def _serve(self, conn: socket.socket) -> None:
        conn.settimeout(0.2)
        if self.greeting_delay:
            time.sleep(self.greeting_delay)
        self._send(conn, {"QMP": {"version": {}, "capabilities": []}})
        buf = b""
        while not self._stop.is_set():
            try:
                chunk = conn.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if line.strip():
                    if self.drop_on and json.loads(line).get("execute") == self.drop_on:
                        if self.drop_skip <= 0:
                            return
                        self.drop_skip -= 1
                    if self.events:
                        self._send(conn, {"event": "RTC_CHANGE", "data": {"offset": 0}, "timestamp": {}})
                    self._send(conn, self._handle(json.loads(line)))

    def _handle(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        name, args = msg["execute"], msg.get("arguments", {})
        self.commands.append(msg)
        if name in self.fail_commands:
            return {"error": {"class": "GenericError", "desc": self.fail_commands[name]}}
        if name == "query-status":
            return {"return": {"status": self.status, "running": self.status == "running"}}
        if name == "screendump":
            frame = self._frame()
            if args.get("format") == "png":
                data = png_from_rgb(self.width, self.height, frame)
            else:
                data = b"P6\n%d %d\n255\n" % (self.width, self.height) + frame
            with open(args["filename"], "wb") as fh:
                fh.write(data)
            return {"return": {}}
        if name in ("send-key", "input-send-event"):
            if self.on_input is not None:
                self.on_input(self)
            elif self.react_to_input:
                self.fill(((self.pixels[0] + 50) % 250))
            return {"return": {}}
        if name == "query-mice":
            return {"return": self.mice}
        if name == "query-block":
            return {"return": self.blocks}
        if name == "human-monitor-command":
            return {"return": self.hmp_replies.get(args["command-line"].split()[0], "")}
        if name == "cont":
            self.status = "running"
        return {"return": {}}
