"""Stdlib-only QEMU QMP client: screenshots, keyboard, absolute pointer, human-monitor passthrough.

QEMU serves one QMP client at a time, so callers should hold a connection only for the duration of one
operation (see ``Endpoint.session`` in ``config.py`` / the backend); a second client simply waits.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
import socket
import struct
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .frames import Box, cursor_like, diff_box, mask_frame, parse_ppm, union_box, Frame

BLINK_WINDOW = 1.2  # seconds a one-way cursor-sized change may wait for the baseline to return (a slow caret)
ABS_MAX = 32767  # usb-tablet absolute axis range
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
BUTTONS = {"left": "left", "right": "right", "middle": "middle"}


class QMPError(RuntimeError):
    """QEMU answered with an error object."""


class QMPBusy(ConnectionError):
    """The single QMP client slot is taken (QEMU never sent its greeting in time)."""


@dataclass
class Observation:
    elapsed: float
    real_changes: int
    changed: bool                   # last frame differs from the first by more than a cursor-sized region
    change_box: Optional[Tuple[int, int, int, int]]
    quiet_for: float                # seconds since the last real change
    last_change_ago: Optional[float]
    size: Tuple[int, int]           # display size of the last frame
    settled: bool                   # quiet for at least the requested quiet period
    first_digest: bytes = b""       # frame before the input (the baseline), as a blake2b digest
    last_digest: bytes = b""
    blink_digests: frozenset = frozenset()   # frames equivalent to the baseline modulo a blinking caret
    first_frame: Optional[Frame] = field(default=None, repr=False, compare=False)
    last_frame: Optional[Frame] = field(default=None, repr=False, compare=False)
    first_size: Tuple[int, int] = (0, 0)   # display size of the first frame
    major: bool = False             # a change larger than a cursor-sized region was seen
    ticking: bool = False           # a small region kept producing new pictures: the watch was cut short
    minor_box: Optional[Box] = None  # that region (only when ticking)
    masked: Tuple[Box, ...] = ()    # regions that were ignored because they change by themselves


def png_size(data: bytes) -> Tuple[int, int]:
    """(width, height) from a PNG's IHDR chunk."""
    if data[:8] != PNG_MAGIC or len(data) < 24:
        raise ValueError("not a PNG")
    return struct.unpack(">II", data[16:24])


def scale_to_abs(x: int, y: int, width: int, height: int) -> Tuple[int, int]:
    """Screenshot pixel -> usb-tablet absolute coordinate (0..32767), clamped to the screen."""
    cx = min(max(int(x), 0), max(width - 1, 0))
    cy = min(max(int(y), 0), max(height - 1, 0))
    return (round(cx * ABS_MAX / max(width - 1, 1)), round(cy * ABS_MAX / max(height - 1, 1)))


def parse_address(addr: str) -> Tuple[str, Any]:
    """``'127.0.0.1:4444'`` -> ('tcp', ('127.0.0.1', 4444)); ``'unix:/path'`` -> ('unix', '/path')."""
    addr = addr.strip()
    if addr.startswith("tcp:"):
        addr = addr[4:]
    if addr.startswith("unix:"):
        return "unix", addr[5:]
    host, sep, port = addr.rpartition(":")
    if not sep or not port.isdigit():
        raise ValueError(f"QMP address must be host:port or unix:/path, got {addr!r}")
    return "tcp", (host.strip("[]") or "127.0.0.1", int(port))


class QMP:
    def __init__(self, address: str, timeout: float = 10.0):
        kind, target = parse_address(address)
        self.address = address
        self._lock = threading.RLock()
        self._buf = b""
        if kind == "unix":
            if not hasattr(socket, "AF_UNIX"):
                raise ConnectionError("unix sockets are not available on this platform")
            # A unix-socket QMP server stops listening while a client is connected (a TCP one queues the
            # second client): a refused connection to an existing socket file therefore means "busy".
            deadline = time.monotonic() + timeout
            while True:
                self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.sock.settimeout(timeout)
                try:
                    self.sock.connect(target)
                    break
                except ConnectionRefusedError:
                    self.sock.close()
                    if not os.path.exists(target):
                        raise
                    if time.monotonic() > deadline:
                        raise QMPBusy("QMP socket is held by another client") from None
                    time.sleep(0.1)
                except OSError:
                    self.sock.close()
                    raise
        else:
            self.sock = socket.create_connection(target, timeout=timeout)
        self.sock.settimeout(timeout)
        try:
            try:
                greeting = self._read()  # QEMU sends it only once it is serving us
            except socket.timeout:
                raise QMPBusy("QMP did not greet in time: another client holds the connection") from None
            if "QMP" not in greeting:
                raise ConnectionError(f"unexpected QMP greeting: {greeting!r}")
            self.cmd("qmp_capabilities")
        except Exception:
            self.close()
            raise

    # --- protocol ---------------------------------------------------------------------------
    def _read(self) -> Dict[str, Any]:
        while b"\n" not in self._buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("QMP connection closed")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return json.loads(line)

    def cmd(self, execute: str, _timeout: Optional[float] = None, **arguments: Any) -> Any:
        with self._lock:
            msg: Dict[str, Any] = {"execute": execute}
            if arguments:
                msg["arguments"] = arguments
            old = self.sock.gettimeout()
            if _timeout is not None:
                self.sock.settimeout(_timeout)
            try:
                self.sock.sendall(json.dumps(msg).encode() + b"\n")
                while True:
                    resp = self._read()
                    if "event" in resp:
                        continue
                    if "error" in resp:
                        raise QMPError(f"{execute}: {resp['error'].get('desc', resp['error'])}")
                    return resp.get("return")
            finally:
                if _timeout is not None:
                    self.sock.settimeout(old)

    def hmp(self, command: str, timeout: float = 120.0) -> str:
        """Human-monitor command (savevm/loadvm/delvm/info snapshots ...). Returns its text output."""
        return self.cmd("human-monitor-command", _timeout=timeout, **{"command-line": command}) or ""

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self) -> "QMP":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # --- eyes --------------------------------------------------------------------------------
    def _dump(self, fmt: str) -> bytes:
        # QEMU writes the file itself, so it must be somewhere both processes can reach; use a private dir.
        with tempfile.TemporaryDirectory(prefix="barehand-") as tmp:
            path = os.path.join(tmp, f"frame.{fmt}")
            self.cmd("screendump", filename=path, format=fmt)
            with open(path, "rb") as fh:
                return fh.read()

    def screenshot(self) -> bytes:
        """PNG of the current display."""
        data = self._dump("png")
        if data[:8] != PNG_MAGIC:
            raise QMPError("screendump did not produce a PNG")
        return data

    def _ppm(self) -> bytes:
        return self._dump("ppm")

    def frame(self) -> Frame:
        return parse_ppm(self._ppm())

    def watch(self, duration: float, quiet: float, poll: float = 0.25, early_quiet: Optional[float] = None,
              baseline: Optional[Frame] = None, blink_window: float = BLINK_WINDOW,
              mask: Sequence[Box] = ()) -> Observation:
        """Poll the display for up to ``duration`` seconds.

        Every frame is judged against the *baseline* (the frame before the input, when one is given), not just
        against its predecessor. A new frame that differs from the baseline only in a cursor-sized region is
        ambiguous: a blinking caret looks like that, and so does a few typed characters. It is told apart by
        what happens next: a caret *returns* to the baseline frame (A, A', A ...), while an edit never does
        and soon shows a third state (the new text with the caret on, then off). So such a frame is held as
        *pending*: it is cleared if the baseline comes back, becomes a real change as soon as a third frame
        appears, and is promoted to a real change when ``blink_window`` seconds pass (or the watch ends)
        without the baseline returning. Thin changes such as a progress line also count, and a tall caret
        costs nothing.

        A small region that keeps producing *new* pictures (a seconds clock, a spinner with a counter) would
        extend that forever. Hard cap: once three such small changes have been seen, no large change has, and
        ``quiet + blink_window`` seconds have passed without the screen settling, the watch stops with
        ``ticking=True`` and ``minor_box`` set to the region; it is not a verdict on the input.
        ``mask`` regions (known to change by themselves) are blanked in every frame before comparing.

        Stops when
          * ``early_quiet`` is None: the screen has been quiet for ``quiet`` seconds (settle mode), or
          * ``early_quiet`` is set: a real change happened and the screen was then quiet for ``early_quiet``
            seconds (wait mode); otherwise it runs for the whole ``duration``."""
        mask = tuple(mask)
        view = (lambda fr: mask_frame(fr, mask)) if mask else (lambda fr: fr)
        start = time.monotonic()
        first_raw = baseline or self.frame()  # a baseline taken before an input makes its effect count as change
        first = view(first_raw)
        anchor = first                    # the frame the current epoch started from
        seen = {first.digest}             # digests of the current epoch (since the last real change)
        all_seen = {first.digest}         # every digest of the whole watch
        prev_distinct = first
        pending_since: Optional[float] = None
        window_start = start
        last_change: Optional[float] = None
        real_changes = 0
        major, ticking = False, False
        minor_events, minor_box = 0, None
        last_raw, last = first_raw, first
        while True:
            time.sleep(poll)
            raw = self.frame()
            f = view(raw)
            now = time.monotonic()
            last_raw, last = raw, f
            if f.digest not in all_seen:
                all_seen.add(f.digest)
                step = diff_box(prev_distinct, f)
                if cursor_like(step, f.width, f.height):
                    minor_events += 1
                    minor_box = union_box(union_box(minor_box, step), diff_box(first, f))   # the whole extent
                else:
                    major, minor_events, minor_box = True, 0, None
                prev_distinct = f
            if f.digest not in seen:
                if len(seen) == 1 and cursor_like(diff_box(anchor, f), f.width, f.height):
                    seen.add(f.digest)
                    pending_since = now
                else:
                    anchor, seen, pending_since = f, {f.digest}, None
                    window_start, last_change = now, now
                    real_changes += 1
            elif pending_since is not None and f.digest == anchor.digest:
                pending_since = None   # back where it started: that was a blinking caret
            if pending_since is not None and now - pending_since >= blink_window:
                anchor, seen = f, {f.digest}
                window_start = last_change = pending_since
                pending_since = None
                real_changes += 1
            quiet_for = now - window_start
            elapsed = now - start
            if early_quiet is None and quiet_for >= quiet and pending_since is None:
                break
            if early_quiet is not None and real_changes and quiet_for >= early_quiet:
                break
            if early_quiet is None and not major and minor_events >= 3 and elapsed >= quiet + blink_window:
                ticking = True
                break
            if elapsed >= duration:
                if pending_since is not None:   # a one-way cursor-sized change that lasted to the end
                    window_start = last_change = pending_since
                    pending_since = None
                    real_changes += 1
                break
        changed = real_changes > 0 and first.digest != last.digest
        box = diff_box(first, last) if changed else None
        end = time.monotonic()
        return Observation(
            elapsed=end - start, real_changes=real_changes, changed=changed,
            change_box=box, quiet_for=end - window_start,
            last_change_ago=None if last_change is None else end - last_change,
            size=(last.width, last.height), settled=(end - window_start) >= quiet and not ticking,
            first_digest=first.digest, last_digest=last.digest,
            blink_digests=frozenset(seen) if real_changes == 0 else frozenset({first.digest}),
            first_frame=first_raw, last_frame=last_raw, first_size=(first.width, first.height),
            major=major, ticking=ticking, minor_box=minor_box if ticking else None, masked=mask)

    # --- hands: keyboard ---------------------------------------------------------------------
    def send_chord(self, qcodes: Sequence[str], hold_ms: int = 60) -> None:
        keys = [{"type": "qcode", "data": q} for q in qcodes]
        self.cmd("send-key", keys=keys, **{"hold-time": hold_ms})
        time.sleep(hold_ms / 1000 + 0.03)

    def _key_event(self, qcode: str, down: bool) -> Dict[str, Any]:
        return {"type": "key", "data": {"down": down, "key": {"type": "qcode", "data": qcode}}}

    def key_down(self, qcodes: Sequence[str]) -> None:
        if qcodes:
            self.cmd("input-send-event", events=[self._key_event(q, True) for q in qcodes])

    def key_up(self, qcodes: Sequence[str]) -> None:
        if qcodes:
            self.cmd("input-send-event", events=[self._key_event(q, False) for q in reversed(list(qcodes))])

    # --- hands: pointer (needs `-device usb-tablet`) ----------------------------------------
    def move_to(self, x: int, y: int, width: int, height: int) -> None:
        ax, ay = scale_to_abs(x, y, width, height)
        self.cmd("input-send-event", events=[
            {"type": "abs", "data": {"axis": "x", "value": ax}},
            {"type": "abs", "data": {"axis": "y", "value": ay}}])

    def button(self, name: str, down: bool) -> None:
        self.cmd("input-send-event", events=[{"type": "btn", "data": {"down": down, "button": name}}])

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
                    self.button(button, False)  # never leave a button held in the guest
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

    # --- introspection -------------------------------------------------------------------------
    def status(self) -> str:
        return self.cmd("query-status").get("status", "unknown")

    def valid_qcodes(self) -> List[str]:
        """The QKeyCode enum of the running QEMU (type names are masked in the schema, so find the enum
        by its members)."""
        for entry in self.cmd("query-qmp-schema"):
            if entry.get("meta-type") == "enum":
                vals = [v if isinstance(v, str) else v.get("name") for v in entry.get("values", [])]
                if "grave_accent" in vals and "meta_l" in vals:
                    return vals
        return []
