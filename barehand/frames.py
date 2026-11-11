"""Frame comparison for screen settling: exact (hash) change detection plus a bounding-box test that tells a
blinking cursor from real change. Operates on raw PPM (P6) dumps; no dependencies."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

Box = Tuple[int, int, int, int]  # x0, y0, x1, y1 (inclusive pixel coordinates)


@dataclass
class Frame:
    data: bytes
    width: int
    height: int
    offset: int
    digest: bytes


def parse_ppm(data: bytes) -> Frame:
    parts = data.split(b"\n", 3)
    if len(parts) < 4 or parts[0] != b"P6":
        raise ValueError("not a P6 PPM")
    w, h = (int(v) for v in parts[1].split())
    off = len(data) - w * h * 3
    if off <= 0:
        raise ValueError("truncated PPM")
    return Frame(data, w, h, off, hashlib.blake2b(data[off:], digest_size=16).digest())


def diff_box(a: Frame, b: Frame, max_rows: int = 96) -> Optional[Box]:
    """Bounding box of the pixels that differ. None when the geometry differs or more than ``max_rows``
    rows differ (that is a big change anyway); an empty box never occurs for unequal digests."""
    if (a.width, a.height) != (b.width, b.height):
        return None
    stride = a.width * 3
    x0, y0, x1, y1 = a.width, a.height, -1, -1
    rows = 0
    for y in range(a.height):
        ra = a.data[a.offset + y * stride:a.offset + (y + 1) * stride]
        rb = b.data[b.offset + y * stride:b.offset + (y + 1) * stride]
        if ra == rb:
            continue
        rows += 1
        if rows > max_rows:
            return None
        left = len(os.path.commonprefix([ra, rb])) // 3
        right = a.width - 1 - len(os.path.commonprefix([ra[::-1], rb[::-1]])) // 3
        x0, x1 = min(x0, left), max(x1, right)
        y0, y1 = min(y0, y), max(y1, y)
    if y1 < 0:
        return (0, 0, 0, 0)
    return (x0, y0, x1, y1)


def cursor_like(box: Optional[Box], width: int, height: int) -> bool:
    """A change this small (at most ~12% of each dimension and ~3% of the area) is treated as a blinking
    text cursor / caret, not as the screen changing."""
    if box is None:
        return False
    bw, bh = box[2] - box[0] + 1, box[3] - box[1] + 1
    return bw <= max(24, width * 0.12) and bh <= max(24, height * 0.12) and bw * bh <= width * height * 0.03


def union_box(a: Optional[Box], b: Optional[Box]) -> Optional[Box]:
    if a is None or b is None:
        return a or b
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def boxes_touch(a: Box, b: Box, margin: int = 8) -> bool:
    """Do two boxes overlap, or come within ``margin`` pixels of each other?"""
    return not (a[2] + margin < b[0] or b[2] + margin < a[0] or a[3] + margin < b[1] or b[3] + margin < a[1])


def mask_frame(frame: Frame, boxes: Sequence[Box], margin: int = 2) -> Frame:
    """A copy of ``frame`` with the given regions blanked, so that a region that changes by itself (a clock, a
    spinner) cannot make two otherwise equal pictures differ. Digest and geometry follow the masked copy."""
    if not boxes:
        return frame
    buf = bytearray(frame.data)
    stride = frame.width * 3
    for x0, y0, x1, y1 in boxes:
        x0, x1 = max(0, x0 - margin), min(frame.width - 1, x1 + margin)
        if x1 < x0:
            continue
        zeros = bytes((x1 - x0 + 1) * 3)
        for y in range(max(0, y0 - margin), min(frame.height - 1, y1 + margin) + 1):
            o = frame.offset + y * stride
            buf[o + x0 * 3:o + (x1 + 1) * 3] = zeros
    data = bytes(buf)
    return Frame(data, frame.width, frame.height, frame.offset,
                 hashlib.blake2b(data[frame.offset:], digest_size=16).digest())


def display_state(frame: Frame) -> str:
    """"active", "blank" (one flat colour: a blanked console or very early firmware) or "banner" (QEMU's
    640x480 two-colour 'Display output is not active' screen, shown until a guest drives the display).
    A heuristic on a sparse sample of pixels; measured on QEMU 11 with edk2."""
    colors = distinct_colors(frame, 3, step=3)
    if colors <= 1:
        return "blank"
    if colors == 2 and (frame.width, frame.height) == (640, 480):
        return "banner"
    return "active"


def distinct_colors(frame: Frame, limit: int = 3, step: int = 5) -> int:
    """Number of distinct pixel colours in a sparse sample of the frame (stops counting at ``limit``)."""
    stride, seen = frame.width * 3, set()
    for y in range(0, frame.height, step):
        row = frame.data[frame.offset + y * stride:frame.offset + (y + 1) * stride]
        for x in range(0, frame.width, step):
            seen.add(row[x * 3:x * 3 + 3])
            if len(seen) >= limit:
                return len(seen)
    return len(seen)
