"""Build tiny synthetic ISO9660 images (primary tree, optionally a Joliet tree too) for architecture detection."""

import struct

SECTOR = 2048


def _both(n):  # 32-bit both-endian
    return struct.pack("<I", n) + struct.pack(">I", n)


def _record(name: bytes, extent: int, size: int, is_dir: bool) -> bytes:
    body = struct.pack("B", 0) + _both(extent) + _both(size) + b"\0" * 7 + bytes([2 if is_dir else 0]) \
        + b"\0\0" + struct.pack("<H", 1) + struct.pack(">H", 1) + bytes([len(name)]) + name
    if len(body) % 2:  # whole record length must be even
        body += b"\0"
    return bytes([len(body) + 1]) + body


def make_iso(tree, path, joliet=False, upper=True):
    """tree: nested dict, leaves are bytes. Writes ``path``. Names are stored upper-cased with ';1' versions
    in the primary tree (like real images); the Joliet tree keeps the given case, UCS-2 encoded."""
    sectors = {}          # lba -> bytes
    next_lba = [20]

    def alloc():
        lba = next_lba[0]
        next_lba[0] += 1
        return lba

    def build(node, parent_lba, enc, lba):
        recs = [_record(b"\0", lba, SECTOR, True), _record(b"\1", parent_lba, SECTOR, True)]
        for name, child in node.items():
            if isinstance(child, dict):
                clba = alloc()
                build(child, lba, enc, clba)
                recs.append(_record(enc(name, False), clba, SECTOR, True))
            else:
                dlba = alloc()
                sectors[dlba] = child.ljust(SECTOR, b"\0")
                recs.append(_record(enc(name, True), dlba, len(child), False))
        data = b""
        for r in recs:
            if len(data) % SECTOR + len(r) > SECTOR:
                data += b"\0" * (SECTOR - len(data) % SECTOR)
            data += r
        sectors[lba] = data.ljust(SECTOR, b"\0")

    def enc_primary(name, is_file):
        n = name.upper() if upper else name
        return (n + (";1" if is_file else "")).encode("latin-1")

    def enc_joliet(name, is_file):
        return name.encode("utf-16-be")

    def pvd(kind, root_lba, escape=b""):
        d = bytearray(SECTOR)
        d[0] = kind
        d[1:6] = b"CD001"
        d[6] = 1
        d[88:88 + len(escape)] = escape
        d[156:156 + 34] = _record(b"\0", root_lba, SECTOR, True)[:34]
        d[156] = 34
        return bytes(d)

    root = alloc()
    build(tree, root, enc_primary, root)
    descriptors = [pvd(1, root)]
    if joliet:
        jroot = alloc()
        build(tree, jroot, enc_joliet, jroot)
        descriptors.append(pvd(2, jroot, b"%/@"))
    term = bytearray(SECTOR)
    term[0], term[1:6], term[6] = 255, b"CD001", 1
    image = bytearray(16 * SECTOR)
    for d in descriptors + [bytes(term)]:
        image += d
    image = image.ljust(20 * SECTOR, b"\0")
    for lba in range(20, next_lba[0]):
        image += sectors[lba]
    with open(path, "wb") as fh:
        fh.write(image)
    return path
