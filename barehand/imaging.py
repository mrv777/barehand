"""Keep screenshots within a provider-friendly size using only the stdlib (Pillow is used when present)."""

from __future__ import annotations

import struct
import zlib
from typing import Any, Tuple

from .qmp import PNG_MAGIC, png_size

MAX_SIDE = 1568


def fit_dims(w: int, h: int, limit: int = MAX_SIDE) -> Tuple[int, int]:
    longest = max(w, h)
    if longest <= limit:
        return w, h
    f = limit / longest
    return max(1, round(w * f)), max(1, round(h * f))


def _chunks(data: bytes):
    pos = 8
    while pos + 8 <= len(data):
        (n,) = struct.unpack(">I", data[pos:pos + 4])
        yield data[pos + 4:pos + 8], data[pos + 8:pos + 8 + n]
        pos += 12 + n


def _decode(png: bytes) -> Tuple[int, int, int, list]:
    """(w, h, bytes_per_pixel, rows) for 8-bit RGB/RGBA non-interlaced PNGs (what QEMU writes)."""
    w, h, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", next(d for t, d in _chunks(png) if t == b"IHDR")[:13])
    if depth != 8 or ctype not in (2, 6) or interlace:
        raise ValueError("unsupported PNG layout")
    bpp = 3 if ctype == 2 else 4
    raw = zlib.decompress(b"".join(d for t, d in _chunks(png) if t == b"IDAT"))
    stride = w * bpp
    rows, prev = [], bytearray(stride)
    for y in range(h):
        base = y * (stride + 1)
        ft, line = raw[base], bytearray(raw[base + 1:base + 1 + stride])
        if ft == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 255
        elif ft == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif ft == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 255
        elif ft == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b, c = prev[i], (prev[i - bpp] if i >= bpp else 0)
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        elif ft != 0:
            raise ValueError("bad PNG filter")
        rows.append(line)
        prev = line
    return w, h, bpp, rows


def _chunk(tag: bytes, data: bytes) -> bytes:
    body = tag + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))


def _shrink_stdlib(png: bytes, nw: int, nh: int) -> bytes:
    w, h, bpp, rows = _decode(png)
    out = bytearray()
    xs = [min(w - 1, int((x + 0.5) * w / nw)) for x in range(nw)]
    for y in range(nh):
        src = rows[min(h - 1, int((y + 0.5) * h / nh))]
        out.append(0)
        for x in xs:
            out += src[x * bpp:x * bpp + 3]
    return (PNG_MAGIC + _chunk(b"IHDR", struct.pack(">IIBBBBB", nw, nh, 8, 2, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(bytes(out), 6)) + _chunk(b"IEND", b""))


def shrink_png(png: bytes, limit: int = MAX_SIDE) -> Tuple[bytes, int, int, bool]:
    """(png, width, height, shrunk). Returns the input unchanged when it already fits or cannot be decoded."""
    w, h = png_size(png)
    nw, nh = fit_dims(w, h, limit)
    if (nw, nh) == (w, h):
        return png, w, h, False
    try:
        import io
        from PIL import Image  # optional: present in a Hermes install
        buf = io.BytesIO()
        Image.open(io.BytesIO(png)).convert("RGB").resize((nw, nh), Image.LANCZOS).save(buf, "PNG", optimize=True)
        return buf.getvalue(), nw, nh, True
    except ImportError:
        pass
    except Exception:
        return png, w, h, False
    try:
        return _shrink_stdlib(png, nw, nh), nw, nh, True
    except Exception:
        return png, w, h, False


def frame_to_png(frame: Any, level: int = 3) -> bytes:
    """PNG of a parsed PPM ``Frame`` at native resolution (stdlib only; a low zlib level keeps it quick)."""
    w, h, stride = frame.width, frame.height, frame.width * 3
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        raw += frame.data[frame.offset + y * stride:frame.offset + (y + 1) * stride]
    return (PNG_MAGIC + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(bytes(raw), level)) + _chunk(b"IEND", b""))
