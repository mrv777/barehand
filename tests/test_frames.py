from barehand.frames import cursor_like, diff_box, parse_ppm


def ppm(w, h, fill=(0, 0, 0), boxes=()):
    px = bytearray(bytes(fill) * (w * h))
    for x, y, bw, bh, color in boxes:
        for yy in range(y, y + bh):
            for xx in range(x, x + bw):
                px[(yy * w + xx) * 3:(yy * w + xx) * 3 + 3] = bytes(color)
    return parse_ppm(b"P6\n%d %d\n255\n" % (w, h) + bytes(px))


def test_identical_frames_share_a_digest():
    assert ppm(64, 48).digest == ppm(64, 48).digest
    assert ppm(64, 48).digest != ppm(64, 48, boxes=[(1, 1, 1, 1, (9, 9, 9))]).digest


def test_bounding_box_of_a_change():
    a, b = ppm(100, 80), ppm(100, 80, boxes=[(10, 20, 5, 7, (255, 255, 255))])
    assert diff_box(a, b) == (10, 20, 14, 26)


def test_geometry_change_has_no_box():
    assert diff_box(ppm(10, 10), ppm(20, 10)) is None


def test_big_changes_give_up_early():
    assert diff_box(ppm(64, 200), ppm(64, 200, fill=(1, 1, 1)), max_rows=50) is None


def test_cursor_like_thresholds():
    assert cursor_like((10, 10, 21, 49), 1280, 800)         # 12x40 caret
    assert cursor_like((0, 0, 7, 15), 640, 480)
    assert not cursor_like((0, 0, 1279, 0), 1280, 800)       # a full-width one-row line is not a caret
    assert not cursor_like((0, 0, 399, 299), 1280, 800)
    assert not cursor_like(None, 1280, 800)
