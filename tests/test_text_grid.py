"""Grid detection and atlas building at several resolutions and cell pitches, on synthetic frames drawn from the
real 8x16 glyph shapes (padded or doubled to make 9x16, 8x19, 16x32 cells, with margins). No QEMU."""

import os

import pytest

from barehand import text

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "text")


@pytest.fixture(autouse=True)
def _fresh_caches():
    text._ANALYSIS.clear()
    text._GRID_HINT.clear()
    text._PREFERRED.clear()
    yield


@pytest.fixture(scope="module")
def base():
    with open(os.path.join(FIX, "linux_calib_login.png"), "rb") as fh:
        frame = text.decode_png(fh.read())
    atlas, rep = text.calibrate(frame, text.CALIBRATION_TEXT, row=38, col=18)
    assert not rep["problems"]
    return atlas


# ------------------------------------------------------------------------------------ tiny renderer


def _rows(key, cw):
    nb = (cw + 7) // 8
    rows = [int.from_bytes(key[i * nb:(i + 1) * nb], "big") >> (nb * 8 - cw) for i in range(len(key) // nb)]
    return [[(v >> (cw - 1 - x)) & 1 for x in range(cw)] for v in rows]


def make_font(base_atlas, scale=1, pad_right=0, pad_top=0, pad_bottom=0):
    """char -> bitmap rows for a font derived from the 8x16 shapes: scaled, with blank columns/rows added."""
    font = {}
    for key, ch in base_atlas.glyphs.items():
        rows = []
        for r in _rows(key, base_atlas.cw):
            wide = [b for b in r for _ in range(scale)] + [0] * pad_right
            rows += [wide] * scale
        font[ch] = [[0] * len(rows[0])] * pad_top + rows + [[0] * len(rows[0])] * pad_bottom
    cw = base_atlas.cw * scale + pad_right
    ch = base_atlas.ch * scale + pad_top + pad_bottom
    return font, cw, ch


def font_atlas(font, cw, ch, source="synthetic"):
    atlas = text.Atlas(cw, ch, source)
    for c, rows in font.items():
        atlas.add(text._pack([bool(b) for row in rows for b in row], cw), c)
    return atlas


def render(lines, font, cw, ch, w, h, ox=0, oy=0, fg=(170, 170, 170), bg=(0, 0, 0), inverse=(), inverse_cells=()):
    """``inverse``: whole rows drawn with swapped colours; ``inverse_cells``: {(row, col)} likewise."""
    buf = bytearray(bytes(bg) * (w * h))
    for r, line in enumerate(lines + [""] * 0):
        for c in range(max(len(line), 160 if r in inverse else 0)):
            chr_ = line[c] if c < len(line) else " "
            x0, y0 = ox + c * cw, oy + r * ch
            if x0 + cw > w or y0 + ch > h:
                continue                                             # clipped, as a real screen would
            swap = r in inverse or (r, c) in inverse_cells
            ink, back = (bg, fg) if swap else (fg, bg)
            if swap:
                for y in range(ch):
                    base_ = ((y0 + y) * w + x0) * 3
                    buf[base_:base_ + cw * 3] = bytes(back) * cw
            glyph = font.get(chr_)
            if glyph is None:
                continue
            for y, row in enumerate(glyph):
                for x, bit in enumerate(row):
                    if bit:
                        p = ((y0 + y) * w + x0 + x) * 3
                        buf[p:p + 3] = bytes(ink)
    return bytes(buf)


PAGE = ["Welcome to Alpine Linux 3.24", "Kernel 6.18.60-0-virt on x86_64 (/dev/tty1)", "",
        "localhost login: root", "localhost:~# setup-alpine", "Select keyboard layout: [none] us",
        "Which disk(s) would you like to use? (or '?' for help or 'none') [none] vda",
        "How would you like to use it? ('sys', 'data', 'crypt', 'lvm' or '?' for help) [?]",
        "af al am ara at az ba bd be bg br brai by ca ch cm cn cz de dk dz ee epo es fi fo",
        "HTTP/FTP proxy URL? (e.g. 'http://proxy:8080', or 'none') [none]", "",
        text.CALIBRATION_TEXT[34:], text.CALIBRATION_TEXT[:34], "Enter system hostname [localhost] barehand",
        "IPv6 address for eth0? (or 'auto', 'dhcp', 'none', '?') [auto]", "# CONFIG_X86_64 is not set; a|b&c~d^e`f"]

# (name, scale, pad_right, pad_top, pad_bottom, width, height, origin_x, origin_y)
MODES = [
    ("8x16 640x480", 1, 0, 0, 0, 640, 480, 0, 0),
    ("8x16 1024x768", 1, 0, 0, 0, 1024, 768, 0, 0),
    ("8x16 1280x800", 1, 0, 0, 0, 1280, 800, 0, 0),
    ("9x16 720x400 (VGA 9-dot)", 1, 1, 0, 0, 720, 400, 0, 0),
    ("8x19 1280x800 (EDK2-like pitch)", 1, 0, 1, 2, 1280, 800, 0, 0),
    ("16x32 1280x800 (2x font)", 2, 0, 0, 0, 1280, 800, 0, 0),
    ("8x16 margins 1024x768", 1, 0, 0, 0, 1024, 768, 13, 7),
    ("8x19 margins 800x600", 1, 0, 1, 2, 800, 600, 16, 11),
]


@pytest.mark.parametrize("mode", MODES, ids=[m[0] for m in MODES])
def test_pitch_is_detected_and_the_calibration_flow_reads_the_page_back(base, mode):
    name, scale, pad_right, pad_top, pad_bottom, w, h, ox, oy = mode
    font, cw, ch = make_font(base, scale, pad_right, pad_top, pad_bottom)
    rgb = render(PAGE, font, cw, ch, w, h, ox, oy)
    grid = text.detect_grid(rgb, w, h)
    assert grid is not None and (grid.cw, grid.ch) == (cw, ch), f"{name}: got {grid}"
    # calibrate by "typing": the plugin has the frame from before the text was echoed and the one after it
    blank = [line if i not in (11, 12) else "" for i, line in enumerate(PAGE)]
    before = render(blank, font, cw, ch, w, h, ox, oy)
    atlas, rep = text.calibrate((rgb, w, h), text.CALIBRATION_TEXT[34:], before=(before, w, h))
    assert rep["problems"] == [] and len(atlas) == 60
    assert (atlas.cw, atlas.ch) == (cw, ch)
    # finish the atlas from the second echoed line (a real flow types it in one go; if split, the first chunk
    # must contain `_`, the glyph that decides where the cell's top edge is)
    before2 = render([line if i != 12 else "" for i, line in enumerate(PAGE)], font, cw, ch, w, h, ox, oy)
    atlas, rep = text.calibrate((rgb, w, h), text.CALIBRATION_TEXT[:34], before=(before2, w, h), atlas=atlas)
    assert rep["problems"] == [] and atlas.missing_ascii() == ""
    r = text.read_screen(rgb, w, h, [atlas])
    assert r.reliable and r.confidence == 1.0, f"{name}: {r.reason}"
    # a margin of whole cells shows up as leading blanks, and a line wider than the screen is clipped
    visible = (w - ox) // cw
    shown = [line[:visible].strip() for line in PAGE]
    assert [row.strip() for row in r.rows[:len(PAGE)]] == shown


def test_cell_size_hint_makes_a_sparse_screen_detectable(base):
    font, cw, ch = make_font(base, 1, 0, 1, 2)
    rgb = render(["login: root", "# ls"], font, cw, ch, 1280, 800, 5, 9)
    g = text.detect_grid(rgb, 1280, 800, cell=(8, 19))
    assert g is not None and (g.cw, g.ch) == (8, 19)


def test_uniform_and_noise_frames_have_no_grid():
    assert text.detect_grid(bytes(640 * 480 * 3), 640, 480) is None
    import random
    rnd = random.Random(3)
    noise = bytes(rnd.randrange(256) for _ in range(320 * 240 * 3))
    assert text.detect_grid(noise, 320, 240) is None
    assert text.detect_grid(b"", 0, 0) is None


def test_origin_search_finds_the_cut_the_atlas_was_calibrated_in(base):
    """The same frame with the grid origin moved by a few pixels reads identically: every valid cut inside the
    font's blank margin is tried against the atlas."""
    font, cw, ch = make_font(base, 1, 0, 1, 2)
    atlas = font_atlas(font, cw, ch)
    expect = [line.rstrip() for line in PAGE]
    for oy in (0, 4, 9, 18):
        rgb = render(PAGE, font, cw, ch, 1280, 800, 3, oy)
        text._GRID_HINT.clear()
        r = text.read_screen(rgb, 1280, 800, [atlas])
        assert r.reliable and r.rows[:len(PAGE)] == expect, f"oy={oy}"


def test_inverse_video_row_is_flagged_and_still_read(base):
    font, cw, ch = make_font(base)
    atlas = font_atlas(font, cw, ch)
    rgb = render(PAGE, font, cw, ch, 1280, 800, 0, 0, inverse=(5,))
    r = text.read_screen(rgb, 1280, 800, [atlas])
    assert r.reliable and r.rows[5] == PAGE[5]
    assert r.inverse[5] == [(0, 159)] and not any(r.inverse[i] for i in range(len(PAGE)) if i != 5)
    # colours do not matter: the same page in amber on blue reads the same
    rgb2 = render(PAGE, font, cw, ch, 1280, 800, 0, 0, fg=(255, 176, 0), bg=(0, 0, 128))
    assert text.read_screen(rgb2, 1280, 800, [atlas]).rows[:len(PAGE)] == [l.rstrip() for l in PAGE]


def test_partial_inverse_span(base):
    font, cw, ch = make_font(base)
    atlas = font_atlas(font, cw, ch)
    cells = {(5, c) for c in range(16, 22)}                    # "layout" in "Select keyboard layout: [none] us"
    rgb = render(PAGE, font, cw, ch, 1280, 800, inverse_cells=cells)
    r = text.read_screen(rgb, 1280, 800, [atlas])
    assert r.reliable and r.rows[5] == PAGE[5] and r.inverse[5] == [(16, 21)]


# --------------------------------------------------------------------------- calibration hazards


def test_verify_calibration_accepts_an_independent_echo_and_rejects_a_bad_one(base):
    font, cw, ch = make_font(base)
    typed = text.CALIBRATION_TEXT
    first = render(["login: " + typed], font, cw, ch, 1280, 800)
    before = render(["login: "], font, cw, ch, 1280, 800)
    atlas, rep = text.calibrate((first, 1280, 800), typed, before=(before, 1280, 800))
    assert rep["problems"] == [] and len(atlas) == 94
    rev = typed[::-1]
    second = render(["login: " + rev], font, cw, ch, 1280, 800)
    assert text.verify_calibration(atlas, (second, 1280, 800), rev, before=(before, 1280, 800)) == []
    # a stuck Shift / dropped key: what echoed is not what was typed
    wrong = rev.replace("a", "A", 1)
    problems = text.verify_calibration(atlas, (second, 1280, 800), wrong, before=(before, 1280, 800))
    assert len(problems) == 1 and "typed 'A' but read 'a'" in problems[0]


def test_a_misaligned_echo_is_not_silently_learned(base):
    """If the echo is not where we think (here: shifted by 2 cells), cells are labelled with the wrong characters
    but the verification pass on a second echo exposes it."""
    font, cw, ch = make_font(base)
    typed = text.CALIBRATION_TEXT
    good = render(["login: " + typed], font, cw, ch, 1280, 800)
    atlas, rep = text.calibrate((good, 1280, 800), typed, row=0, col=9)        # wrong start: really 7
    assert rep["labelled"] > 0
    problems = text.verify_calibration(atlas, (good, 1280, 800), typed, row=0, col=7)
    assert problems


def test_incremental_learning_from_typed_text(base):
    """Self-learning (assessment): every echoed typed string labels its characters for free, but only the
    characters actually typed."""
    font, cw, ch = make_font(base)
    atlas = text.Atlas(cw, ch, "learned")
    rgb = render(["# setup-alpine"], font, cw, ch, 1280, 800)
    atlas, rep = text.calibrate((rgb, 1280, 800), "setup-alpine", row=0, col=2, atlas=atlas, cell=(cw, ch))
    assert rep["problems"] == [] and set(atlas.chars()) == set("setup-alin")
    rgb = render(["# barehand"], font, cw, ch, 1280, 800)
    atlas, rep = text.calibrate((rgb, 1280, 800), "barehand", row=0, col=2, atlas=atlas)
    assert set(atlas.chars()) == set("setup-alinbrehd")
    assert rep["added"] == len(set("barehand") - set("setup-alin")) and atlas.missing_ascii() != ""
    # a screen with other characters is NOT reliable yet: unknown cells are marked, never guessed
    page = render(["Welcome to Alpine [UTC]"], font, cw, ch, 1280, 800)
    r = text.read_screen(page, 1280, 800, [atlas])
    assert not r.reliable and r.rows[0].startswith(text.UNKNOWN) and r.stats["unknown"] > 5
    assert r.rows[0].count(text.UNKNOWN) == r.stats["unknown"]
