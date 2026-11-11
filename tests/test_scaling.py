import pytest

from barehand.qmp import ABS_MAX, parse_address, png_size, scale_to_abs
from fake_qmp import make_png


def test_corners_and_center():
    assert scale_to_abs(0, 0, 1280, 800) == (0, 0)
    assert scale_to_abs(1279, 799, 1280, 800) == (ABS_MAX, ABS_MAX)
    assert scale_to_abs(640, 400, 1280, 800) == (16396, 16404)  # values observed inside a real guest


def test_out_of_range_is_clamped():
    assert scale_to_abs(-50, 99999, 640, 480) == (0, ABS_MAX)


def test_degenerate_screen_does_not_divide_by_zero():
    assert scale_to_abs(5, 5, 1, 1) == (0, 0)


def test_png_size():
    assert png_size(make_png(64, 48, 3)) == (64, 48)
    with pytest.raises(ValueError):
        png_size(b"not a png at all, definitely not")


def test_parse_address():
    assert parse_address("127.0.0.1:4444") == ("tcp", ("127.0.0.1", 4444))
    assert parse_address("tcp:localhost:1") == ("tcp", ("localhost", 1))
    assert parse_address("unix:/tmp/q.sock") == ("unix", "/tmp/q.sock")
    assert parse_address("[::1]:9") == ("tcp", ("::1", 9))
    with pytest.raises(ValueError):
        parse_address("nonsense")
