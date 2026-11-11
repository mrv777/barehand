import base64

from barehand import imaging
from barehand.backend import BarehandBackend
from barehand.qmp import png_size
from fake_qmp import make_png


def test_small_images_are_untouched():
    png = make_png(100, 50, 9)
    assert imaging.shrink_png(png) == (png, 100, 50, False)


def test_fit_dims():
    assert imaging.fit_dims(3136, 1568) == (1568, 784)
    assert imaging.fit_dims(1000, 5000, 1000) == (200, 1000)


def test_oversize_png_is_shrunk_to_limit(monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "PIL", None)  # force the stdlib path
    png = make_png(400, 200, 77)
    out, w, h, shrunk = imaging.shrink_png(png, limit=100)
    assert shrunk and (w, h) == (100, 50) and png_size(out) == (100, 50)


def test_decode_roundtrip_preserves_colour(monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "PIL", None)
    out, *_ = imaging.shrink_png(make_png(40, 40, 123), limit=10)
    _, _, bpp, rows = imaging._decode(out)
    assert bytes(rows[0][:3]) == bytes((123, (123 * 3) % 256, 0))


def test_backend_scales_clicks_in_the_shrunk_image(fake, monkeypatch):
    monkeypatch.setattr("barehand.backend.MAX_SIDE", 64)
    monkeypatch.setattr("barehand.imaging.MAX_SIDE", 64)
    b = BarehandBackend(settings={"qmp": fake.address, "settle_quiet": 0.1, "settle_timeout": 1,
                                  "settle_delay": 0.01, "settle_poll": 0.03})
    cap = b.capture()
    assert max(cap.width, cap.height) <= 64 and "downscaled" in cap.note
    assert png_size(base64.b64decode(cap.png_b64)) == (cap.width, cap.height)
    b.click(x=cap.width - 1, y=cap.height - 1)
    vals = [e["data"]["value"] for e in fake.input_events() if e["type"] == "abs"]
    assert vals == [32767, 32767]
