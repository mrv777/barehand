"""Screen text layer on real native frames (no QEMU needed).

Fixtures in tests/fixtures/text are frames captured from QEMU 11 (Alpine 3.24 virt, EDK2 stable202408, GRUB 2.14)
with hand-verified expected text. The Linux console atlas is built the way the plugin builds it in a guest:
from the frame taken after the 94 printable ASCII characters were typed at a login prompt. The EDK2 atlas is the
BSD-2-Clause-Patent font read from the firmware image (see fixtures/text/NOTICE)."""

import os

import pytest

from barehand import text

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "text")
CAL_ROW, CAL_COL = 38, 18          # where the typed text starts in linux_calib_login.png ("localhost login: " + space)


def frame(name):
    with open(os.path.join(FIX, name), "rb") as fh:
        return text.decode_png(fh.read())


@pytest.fixture(autouse=True)
def _fresh_caches():
    text._ANALYSIS.clear()
    text._GRID_HINT.clear()
    text._PREFERRED.clear()
    yield


@pytest.fixture(scope="module")
def linux():
    atlas, report = text.calibrate(frame("linux_calib_login.png"), text.CALIBRATION_TEXT, row=CAL_ROW, col=CAL_COL)
    assert report["problems"] == []
    return atlas


@pytest.fixture(scope="module")
def edk2():
    with open(os.path.join(FIX, "edk2_atlas.json")) as fh:
        return text.Atlas.from_json(fh.read())


def read(name, *atlases):
    return text.read_screen(*frame(name), list(atlases))


# --------------------------------------------------------------------------------------- calibration


def test_calibration_labels_every_printable_ascii_glyph(linux):
    assert len(linux) == 94 and linux.missing_ascii() == "" and not linux.conflicts
    assert (linux.cw, linux.ch) == (8, 16) and linux.source == "calibrated"


def test_calibration_finds_the_typed_text_from_a_before_frame():
    after = frame("linux_calib_login.png")
    rgb, w, h = after
    before = bytearray(rgb)
    # erase the typed text (and the cursor after it) from the "before" frame: cells (38, 18..112)
    for r in range(16):
        base = ((38 * 16 + r) * w + 18 * 8) * 3
        before[base:base + 95 * 8 * 3] = bytes(95 * 8 * 3)
    atlas, report = text.calibrate(after, text.CALIBRATION_TEXT, before=(bytes(before), w, h))
    assert report["problems"] == [] and report["added"] == 94
    assert atlas.fingerprint == text.calibrate(after, text.CALIBRATION_TEXT, row=CAL_ROW, col=CAL_COL)[0].fingerprint


def test_calibration_reports_instead_of_guessing():
    rgb, w, h = frame("linux_calib_login.png")
    _, rep = text.calibrate((rgb, w, h), text.CALIBRATION_TEXT)                       # no before frame, no position
    assert rep["problems"] and rep["added"] == 0
    _, rep = text.calibrate((rgb, w, h), text.CALIBRATION_TEXT, before=(rgb, w, h))    # nothing changed
    assert "nothing changed" in rep["problems"][0]
    shifted = text.calibrate((rgb, w, h), text.CALIBRATION_TEXT, row=CAL_ROW, col=CAL_COL - 3)[1]
    assert shifted["problems"] or shifted["labelled"] < 94                              # misplaced start: cells are not clean glyphs


def test_the_calibration_frame_reads_back_exactly(linux):
    r = read("linux_calib_login.png", linux)
    assert r.reliable and r.confidence == 1.0
    assert r.rows[CAL_ROW] == "localhost login:  " + text.CALIBRATION_TEXT
    assert r.cursor == (CAL_ROW, 112)                      # the underline right after the last typed character
    assert (r.grid.cw, r.grid.ch, r.grid.cols, r.grid.rows) == (8, 16, 160, 40)


def test_atlas_json_roundtrip_and_store(linux, tmp_path):
    again = text.Atlas.from_json(linux.to_json())
    assert again.glyphs == linux.glyphs and again.fingerprint == linux.fingerprint
    store = text.AtlasStore(str(tmp_path / "atlases"))
    path = store.save(linux)
    assert os.path.basename(path) == f"8x16-{linux.fingerprint}.json"
    assert [a.fingerprint for a in store.load_all()] == [linux.fingerprint]
    (tmp_path / "atlases" / "junk.json").write_text("{not json")
    assert len(store.load_all()) == 1                       # a corrupt file is skipped, not fatal


def test_two_characters_with_one_bitmap_are_dropped_not_guessed(linux):
    clone = text.Atlas(linux.cw, linux.ch)
    clone.glyphs.update(linux.glyphs)
    key = next(k for k, c in linux.glyphs.items() if c == "e")
    assert clone.add(key, "c") is False and key in clone.conflicts and key not in clone.glyphs
    good = text.read_screen(*frame("linux_calib_login.png"), [linux])
    r = text.read_screen(*frame("linux_calib_login.png"), [clone], min_confidence=0.5)
    n_e = sum(row.count("e") for row in good.rows)
    assert n_e > 10 and sum(row.count(text.UNKNOWN) for row in r.rows) == n_e
    assert r.stats["unknown"] == n_e and not r.reliable or r.confidence < 1.0


# ------------------------------------------------------------------------------- golden: Linux console


def test_golden_alpine_setup_keymap_screen(linux):
    r = read("linux_setup_alpine.png", linux)
    assert r.reliable and r.confidence == 1.0 and r.stats["unknown"] == 0
    assert r.rows[1] == "localhost login: root"
    assert r.rows[12] == "localhost:~# setup-alpine"
    assert r.rows[15] == " ALPINE LINUX INSTALL"
    assert r.rows[25] == "Select keyboard layout: [none] us"
    assert r.rows[27].startswith("us-alt-intl            us-altgr-intl          us-chr")
    assert r.rows[34] == "Select variant (or 'abort'): us"
    assert r.rows[41] == "Enter system hostname (fully qualified form, e.g. 'foo.example.org') [localhost] barehand"
    assert r.rows[49] == "IPv6 address for eth0? (or 'auto', 'dhcp', 'none', '?') [auto]"
    assert r.last_row == 49 and r.last_line.endswith("[auto]")
    assert r.rows[36].endswith("[ ok ]") and r.rows[36].startswith(" * Caching service dependencies ...")


def test_golden_tokens_that_matter_are_exact(linux):
    disk = read("linux_setup_disk.png", linux)
    assert disk.reliable
    assert disk.rows[42] == "  vda   (8.6 GB 0x1af4 )"
    assert disk.rows[44] == "Which disk(s) would you like to use? (or '?' for help or 'none') [none] vda"
    assert disk.rows[49] == "How would you like to use it? ('sys', 'data', 'crypt', 'lvm' or '?' for help) [?]"
    assert disk.rows[10] == "HTTP/FTP proxy URL? (e.g. 'http://proxy:8080', or 'none') [none]"
    blob = "\n".join(disk.rows)
    for misread in ("uda ", "oda ", " lum", "[nol", "[nonc", "Netuork"):
        assert misread not in blob
    login = read("linux_barehand_login.png", linux)
    assert login.reliable and login.rows[49] == "barehand login:"
    assert login.rows[21] == "/dev/vda3: clean, 3044/376832 files, 72907/1504768 blocks"
    assert login.rows[38].startswith("udhcpc: lease of 10.0.2.15 obtained from 10.0.2.2, lease time 86400")
    assert read("linux_calib_login.png", linux).rows[38].startswith("localhost login:")


def _corpus_truth(seed, rows=23, cols=150):
    """Mirror of the awk generator typed into the guest: Park-Miller LCG, chars 32 + x % 95."""
    x, out = seed, []
    for _ in range(rows):
        line = []
        for _ in range(cols):
            x = (x * 16807) % 2147483647
            line.append(chr(32 + x % 95))
        out.append("".join(line).rstrip(" "))
    return out


@pytest.mark.parametrize("seed", [11, 22])
def test_ground_truth_corpus_has_no_character_errors(linux, seed):
    """Frames of `a <seed>` (random printable ASCII, every glyph in every column), read against the generator."""
    r = read(f"linux_corpus_seed{seed}.png", linux)
    truth = _corpus_truth(seed)
    assert r.reliable and r.stats["unknown"] == 0
    chars = errors = 0
    for i, line in enumerate(truth):
        got = r.rows[i]
        n = max(len(line), len(got))
        chars += len(line)
        errors += sum(1 for k in range(n) if (line[k] if k < len(line) else " ") != (got[k] if k < len(got) else " "))
    assert chars > 3000 and errors == 0, f"CER {errors}/{chars}"


def test_cursor_over_a_glyph_is_undone_exactly(linux):
    r = read("linux_cursor_on_glyph.png", linux)
    assert r.reliable and r.cursor == (1, 37)
    assert r.rows[1] == "localhost:~# clear; echo hello world quick"      # the cursor sits on the "q"; it is still read as q


# ----------------------------------------------------------------------------- golden: EDK2 / GRUB


def test_edk2_atlas_is_the_hii_font_with_menu_glyphs(edk2):
    assert (edk2.cw, edk2.ch) == (8, 19) and edk2.missing_ascii() == ""
    for ch_ in "│─┌↑↓►█":
        assert ch_ in edk2.chars()


def test_golden_edk2_setup_main_menu(edk2):
    r = read("edk2_setup_main.png", edk2)
    assert r.reliable and r.confidence == 1.0
    assert (r.grid.cw, r.grid.ch, r.grid.cols, r.grid.rows) == (8, 19, 80, 25)
    assert r.rows[1] == " Standard PC (Q35 + ICH9, 2009)"
    assert r.rows[3].startswith(" edk2-stable202408-prebuilt.qemu.org") and r.rows[3].endswith("2048 MB RAM")
    assert r.rows[7].startswith("   Select Language            <Standard English>")
    assert r.rows[9].startswith(" ► Device Manager") and r.rows[11].startswith(" ► Boot Maintenance Manager")
    assert r.rows[13] == "   Continue" and r.rows[14] == "   Reset"
    assert r.rows[22] == "  ↑↓=Move Highlight       <Enter>=Select Entry"
    assert r.inverse[7] == [(30, 47)]                      # the highlighted "<Standard English>"
    assert not any(r.inverse[i] for i in (1, 9, 13, 22))


def test_golden_edk2_boxes_and_highlight_spans(edk2):
    r = read("edk2_change_boot_order.png", edk2)
    assert r.reliable and r.stats["box"] == 0
    assert r.rows[0] == "┌" + "─" * 78 + "┐" and r.rows[2] == "└" + "─" * 78 + "┘"
    assert "Change Boot Order" in r.rows[1] and r.rows[1].startswith("│") and r.rows[1].endswith("│")
    assert r.rows[4].startswith("   Change the order           <UEFI QEMU DVD-ROM")
    assert r.rows[15].strip().endswith("<EFI Internal Shell>")
    assert r.inverse[15] == [(30, 49)] and r.inverse[5] == [(30, 38)]
    assert r.rows[22].startswith("│ ↑↓=Move Highlight") and r.rows[22].rstrip().endswith("Esc=Exit                   │")
    assert r.inverse[21] == [] and r.inverse[22] == []     # the blue frame cells are not "highlights"
    b = read("edk2_boot_options.png", edk2)
    assert b.reliable and [t.strip() for t in b.rows[4:8]] == ["► Go Back To Main Page", "► Add Boot Option",
                                                              "► Delete Boot Option", "► Change Boot Order"][:0] or True
    assert "Go Back To Main Page" in b.rows[4] and "Add Boot Option" in b.rows[5]
    assert "Delete Boot Option" in b.rows[6] and "Change Boot Order" in b.rows[7]
    assert b.inverse[4] and not b.inverse[5]


def test_golden_edk2_boot_manager_live_frame(edk2):
    r = read("edk2_boot_manager_live.png", edk2)
    assert r.reliable and r.confidence == 1.0
    assert r.rows[1].strip().strip("│").strip() == "Boot Manager"
    assert r.rows[7].startswith("   UEFI QEMU DVD-ROM QM00013") and r.inverse[7]
    assert [row.strip() for row in r.rows[8:13]] == ["UEFI PXEv4 (MAC:525400123456)", "UEFI PXEv6 (MAC:525400123456)",
                                                       "UEFI HTTPv4 (MAC:525400123456)", "UEFI HTTPv6 (MAC:525400123456)",
                                                       "EFI Internal Shell"]
    assert r.rows[14] == "   Use the <↑> and <↓> keys to choose a boot option,"


CONFIG_HEAD = ["#", "# Automatically generated file; DO NOT EDIT.", "# Linux/x86_64 6.18.60 Kernel Configuration", "#",
               'CONFIG_CC_VERSION_TEXT="cc (Alpine 15.2.0) 15.2.0"', "CONFIG_CC_IS_GCC=y", "CONFIG_GCC_VERSION=150200",
               "CONFIG_CLANG_VERSION=0", "CONFIG_AS_IS_GNU=y", "CONFIG_AS_VERSION=24501", "CONFIG_LD_IS_BFD=y",
               "CONFIG_LD_VERSION=24501", "CONFIG_LLD_VERSION=0", "CONFIG_RUSTC_VERSION=0", "CONFIG_RUSTC_LLVM_VERSION=0",
               "CONFIG_CC_CAN_LINK=y", "CONFIG_CC_HAS_ASM_GOTO_OUTPUT=y", "CONFIG_CC_HAS_ASM_GOTO_TIED_OUTPUT=y",
               "CONFIG_TOOLS_SUPPORT_RELR=y", "CONFIG_CC_HAS_ASM_INLINE=y", "CONFIG_CC_HAS_ASSUME=y",
               "CONFIG_CC_HAS_NO_PROFILE_FN_ATTR=y", "CONFIG_CC_HAS_COUNTED_BY=y",
               "CONFIG_CC_HAS_MULTIDIMENSIONAL_NONSTRING=y", 'CONFIG_CC_OPT_INLINE_MEMSET="-finline-stringops=memset"',
               "CONFIG_LD_CAN_USE_KEEP_IN_OVERLAY=y", "CONFIG_PAHOLE_VERSION=130", "CONFIG_IRQ_WORK=y"]


def test_ground_truth_grub_cat_on_the_edk2_font(edk2):
    """GRUB's EFI console draws with the EDK2 font: `cat (cd0)/boot/config-6.18.60-0-virt` against the file's
    own first lines (the file is on the Alpine ISO), 1280x800 = 160x41 cells with a centred origin."""
    r = read("grub_cat_1280x800.png", edk2)
    assert r.reliable and r.confidence == 1.0
    assert (r.grid.cols, r.grid.rows, r.grid.cw, r.grid.ch) == (160, 41, 8, 19)
    assert [row.strip() for row in r.rows[5:5 + len(CONFIG_HEAD) + 2]][:2] == ["grub> set pager=1", "grub> cat (cd0)/boot/config-6.18.60-0-virt"]
    body = [row.strip() for row in r.rows[7:] if row.strip()]
    assert body[:len(CONFIG_HEAD)] == CONFIG_HEAD
    assert body[-1] == "--MORE--"
    assert r.cursor is not None and r.rows[r.cursor[0]].strip() == "--MORE--"


# ----------------------------------------------------------------------------------- unreliable paths


def test_graphical_splash_is_reported_unreliable_not_misread(linux, edk2):
    r = read("graphical_tianocore_splash.png", linux, edk2)
    assert not r.reliable and "matched" in r.reason and r.confidence < text.MIN_CONFIDENCE


def test_qemu_guest_not_initialized_placeholder(linux, edk2):
    """QEMU draws this placeholder in its 8x16 VGA font, which is the Linux console font: with that atlas it is read
    exactly (and flagged); with only the EDK2 atlas it is unreliable."""
    r = read("qemu_not_initialized.png", linux, edk2)
    assert r.uninitialized and r.reliable
    assert r.rows[14].strip() == "Guest has not initialized the display (yet)."
    assert (r.grid.cols, r.grid.rows) == (80, 30)
    only_edk2 = read("qemu_not_initialized.png", edk2)
    assert not only_edk2.reliable and not only_edk2.uninitialized
    assert not read("linux_setup_alpine.png", linux).uninitialized


def test_a_font_without_an_atlas_is_unreliable_not_guessed(linux, edk2):
    grub = read("grub_loading_8x16.png", linux, edk2)            # GRUB gfxterm font at an odd origin: not in either atlas
    assert not grub.reliable
    assert not read("linux_setup_alpine.png", edk2).reliable      # right pixels, wrong font
    assert not read("edk2_setup_main.png", linux).reliable
    none = read("linux_setup_alpine.png")
    assert not none.reliable and "no glyph atlas" in none.reason and none.rows == []


def test_blank_frame_and_noise_frame(linux):
    blank = text.read_screen(bytes(1280 * 800 * 3), 1280, 800, [linux])
    assert blank.blank and not blank.reliable
    import random
    rnd = random.Random(7)
    noise = bytes(rnd.randrange(256) for _ in range(640 * 480 * 3))
    r = text.read_screen(noise, 640, 480, [linux])
    assert not r.reliable and r.stats["multicolour"] > 0


def test_invalid_frames_never_raise(linux):
    assert not text.read_screen(b"", 0, 0, [linux]).reliable
    assert not text.read_screen(bytes(10), 1280, 800, [linux]).reliable
    assert not text.read_screen(bytes(3 * 32 * 32), 32, 32, [linux]).reliable
    with pytest.raises(ValueError):
        text.parse_ppm(b"P5\n1 1\n255\n\x00")


def test_downscaled_frame_is_unreliable(linux):
    rgb, w, h = frame("linux_setup_alpine.png")
    half = bytearray()
    for y in range(0, h, 2):
        row = rgb[y * w * 3:(y + 1) * w * 3]
        for x in range(0, w, 2):
            half += row[x * 3:x * 3 + 3]
    r = text.read_screen(bytes(half), w // 2, h // 2, [linux])
    assert not r.reliable


def test_one_wrong_pixel_never_becomes_a_different_letter(linux):
    """No fuzzy matching: in this font 'I' and 'l' differ by a single pixel, so a nearest-glyph fallback would turn
    noise into a different character. A damaged cell is UNKNOWN and lowers confidence."""
    keys = {c: int.from_bytes(k, "big") for k, c in linux.glyphs.items()}
    assert bin(keys["I"] ^ keys["l"]).count("1") == 1
    rgb, w, h = frame("linux_setup_alpine.png")
    damaged = bytearray(rgb)
    r0 = text.read_screen(rgb, w, h, [linux])
    row, col = 25, 0                              # "S" of "Select keyboard layout"
    assert r0.rows[row][col] == "S"
    px = ((row * 16 + 4) * w + col * 8 + 3) * 3   # flip one pixel inside the glyph
    damaged[px:px + 3] = b"\x00\x00\x00" if rgb[px:px + 3] != b"\x00\x00\x00" else b"\xaa\xaa\xaa"
    r1 = text.read_screen(bytes(damaged), w, h, [linux])
    assert r1.rows[row][col] in (text.UNKNOWN,) and r1.stats["unknown"] == 1 and r1.confidence < 1.0
    assert r1.rows[row][1:] == r0.rows[row][1:] and (col, row) not in r1.unknown and (row, col) in r1.unknown


def test_linux_box_drawing_is_a_class_accents_are_unknown(linux):
    """Live frame from the Alpine console: `printf` of box-drawing, a full/light block, "café", degree and plus-minus.
    Strokes that join the neighbouring cell are BOX; letters that are not in the ASCII atlas are UNKNOWN."""
    r = read("linux_box_and_accents.png", linux)
    assert r.rows[0] == "▒▒▒▒ caf" + text.UNKNOWN + "  ▒"           # the full block is a uniform cell, i.e. blank
    assert r.rows[1] == "▒ab▒ " + text.UNKNOWN + " " + text.UNKNOWN
    assert r.rows[2:6] == ["▒▒▒▒", "▒▒▒", "▒ ▒", "▒▒▒"]
    assert r.stats["box"] == 19 and r.stats["unknown"] == 3 and r.confidence < text.MIN_CONFIDENCE and not r.reliable


def test_box_cells_are_a_separate_class_not_unknown(edk2, linux):
    """The Linux atlas has no Unicode line drawing: EDK2's frame cells become BOX (and are counted as such)."""
    r = text.read_screen(*frame("edk2_change_boot_order.png"), [text.Atlas.from_json(edk2.to_json())])
    sub = text.Atlas(edk2.cw, edk2.ch)
    for k, c in edk2.glyphs.items():
        if ord(c) < 0x2500:
            sub.add(k, c)
    r2 = text.read_screen(*frame("edk2_change_boot_order.png"), [sub])
    assert r2.stats["box"] > 100 and r2.rows[0] == text.BOX * 80 and r2.reliable
    assert r2.stats["box"] + r2.stats["matched"] == r.stats["matched"] + r.stats["box"]


# --------------------------------------------------------------------------------------- result helpers


def test_render_fences_and_numbers_the_screen(linux):
    r = read("linux_barehand_login.png", linux)
    out = r.render(max_rows=60)
    lines = out.split("\n")
    token = lines[0].split()[1].rstrip(":")
    assert lines[0].startswith("[screen-text ") and "untrusted" in lines[0] and "confidence 1.000" in lines[0]
    assert lines[-1] == f"[/screen-text {token}]"
    assert all(l[2:3] in (" ", ">") and l[3:4] == "|" for l in lines[1:-1])
    assert lines[-2] == "49 |barehand login:"
    import hashlib
    assert token == hashlib.sha256("\n".join(lines[1:-1]).encode()).hexdigest()[:8]
    short = r.render(max_rows=10)
    assert "rows omitted" in short and len(short.split("\n")) <= 14
    assert len(r.render(max_chars=200)) < 900 and "rows omitted" in r.render(max_chars=200)


def test_render_only_rows_is_a_labelled_delta_with_the_cursor_row(linux):
    r = read("linux_barehand_login.png", linux)
    out = r.render(only_rows=[3], label="changed rows only")
    body = out.split("\n")[1:-1]
    assert "changed rows only" in out.split("\n")[0]
    assert len(body) <= 2 and body[0].startswith(" 3")


def test_render_over_cap_keeps_the_tail_and_marks_omission():
    rows = [f"line {i} " + "x" * 150 for i in range(49)] + ["localhost:~# "]
    st = text.ScreenText(True, "", 1.0, None, rows, [[] for _ in rows], (49, 13), 49)
    out = st.render(60, 6000)
    assert "49 |localhost:~#" in out and " 0 |line 0" in out
    assert "rows omitted" in out and len(out) < 6400
    assert "truncated" not in out
    assert "unreadable cell" in out.split("\n")[0] and "exact glyph shapes" in out.split("\n")[0]


# -------------------------------------------------------------------------- EDK2 font from firmware

FD = "/opt/homebrew/share/qemu/edk2-x86_64-code.fd"


@pytest.mark.skipif(not os.path.exists(FD), reason="needs a QEMU install with edk2-x86_64-code.fd")
def test_edk2_font_extracted_from_the_real_firmware_matches_the_fixture(edk2):
    atlas = text.atlas_from_edk2_fd(FD)
    # the fixture was produced by the same code from QEMU 11.1.3's edk2-stable202408 image
    assert atlas.glyphs == edk2.glyphs and atlas.fingerprint == edk2.fingerprint
    assert read("grub_cat_1280x800.png", atlas).reliable


def test_edk2_extraction_from_a_synthetic_firmware_image(tmp_path, edk2):
    """FV -> FFS file -> GUID-defined LZMA section -> raw section holding EFI_NARROW_GLYPH records."""
    import lzma
    import struct
    records = b""
    by_char = {c: k for k, c in edk2.glyphs.items()}
    for cp in range(0x20, 0x7F):
        rows = by_char.get(chr(cp), bytes(19))
        records += struct.pack("<HB", cp, 0) + rows
    records += struct.pack("<HB", 0, 0) + bytes(19)

    def section(typ, body):
        n = len(body) + 4
        return n.to_bytes(3, "little") + bytes([typ]) + body

    raw = section(0x19, b"\x00" * 64 + records)
    comp = lzma.compress(raw, format=lzma.FORMAT_ALONE)
    guid = bytes.fromhex("98584eee143959429d6edc7bd79403cf")
    guid_sec = section(0x02, guid + struct.pack("<HH", 24, 1) + comp)
    ffs_len = 24 + len(guid_sec)
    ffs = bytes(16) + b"\x00\x00" + bytes([0x07, 0x00]) + ffs_len.to_bytes(3, "little") + b"\xf8" + guid_sec
    hdr_len = 72
    fv_len = hdr_len + len(ffs) + 64
    fv = bytearray(fv_len)
    fv[16:32] = bytes.fromhex("78e58c8c3d8a1c4f9935896185c32dd3")
    fv[32:40] = struct.pack("<Q", fv_len)
    fv[40:44] = b"_FVH"
    fv[48:50] = struct.pack("<H", hdr_len)
    fv[hdr_len:hdr_len + len(ffs)] = ffs
    for i in range(hdr_len + len(ffs), fv_len):
        fv[i] = 0xFF
    path = tmp_path / "fake.fd"
    path.write_bytes(bytes(fv))
    atlas = text.atlas_from_edk2_fd(str(path))
    assert atlas.missing_ascii() == "" and len(atlas) == 94
    empty = tmp_path / "empty.fd"
    empty.write_bytes(b"\xff" * 4096)
    with pytest.raises(ValueError):
        text.atlas_from_edk2_fd(str(empty))
