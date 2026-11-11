"""RFB client + VNC transport against the fake server in fake_rfb.py (no real server needed)."""

import threading
import time

import pytest

from barehand import rfb
from barehand import transport_vnc as tv
from barehand.frames import parse_ppm
from fake_rfb import FakeRFB, bomb, fb_to_rgb


@pytest.fixture
def serve():
    servers = []

    def make(**kw):
        s = FakeRFB(**kw)
        servers.append(s)
        return s
    yield make
    for s in servers:
        s.stop()


def client(srv, password=None, **kw):
    kw.setdefault("timeout", 3.0)
    c = rfb.RFB(srv.address, password, **kw)
    return c


# --- handshake -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("version,minor", [("3.3", 3), ("3.7", 7), ("3.8", 8), ("3.889", 8), ("3.5", 3)])
def test_version_negotiation_none_security(serve, version, minor):
    s = serve(version=version)
    c = client(s)
    w, h, rgb = c.frame()
    assert (w, h) == (150, 100) and rgb == fb_to_rgb(s.fb)
    assert c.version == (3, minor) and c.security == 1 and c.name == "fake"
    assert s.messages[0] == ("version", b"RFB 003.%03d\n" % minor)
    c.close()


def test_pixel_format_and_encodings_requested(serve):
    s = serve()
    c = client(s)
    c.frame()
    pf = s.events("setpf")[0][1]
    assert pf == rfb._PIXEL_FORMAT and pf[:4] == bytes([32, 24, 0, 1])      # 32bpp depth 24 LE true colour
    encs = s.events("setenc")[0][1]
    assert encs[:4] == [16, 5, 1, 0] and -223 in encs and -308 in encs and -258 in encs
    assert s.events("fbreq")[0][1] == 0                                       # non-incremental


def test_raw_only_and_encoding_validation(serve):
    s = serve()
    c = client(s, encodings=["raw"])
    c.frame()
    assert s.events("setenc")[0][1][0] == 0
    with pytest.raises(ValueError):
        rfb.RFB("h:1", encodings=["tight"])


@pytest.mark.parametrize("version", ["3.3", "3.7", "3.8"])
def test_vnc_auth_success(serve, version):
    s = serve(version=version, security=(2,), password="hunter2")
    c = client(s, "hunter2")
    assert c.frame()[2] == fb_to_rgb(s.fb) and c.security == 2
    assert s.auth_results == [True]


@pytest.mark.parametrize("version", ["3.3", "3.7", "3.8"])
def test_vnc_auth_failure(serve, version):
    s = serve(version=version, security=(2,), password="hunter2")
    c = client(s, "wrong")
    with pytest.raises(rfb.RFBAuthError, match="authentication failed"):
        c.frame()
    assert s.auth_results == [False] and s.connections == 1      # no automatic retry of a bad password
    assert not c.connected


def test_auth_required_but_no_password(serve):
    s = serve(security=(2,), password="x")
    with pytest.raises(rfb.RFBAuthError, match="requires a VNC password"):
        client(s).frame()


def test_server_offers_both_and_password_given_picks_vnc_auth(serve):
    s = serve(security=(1, 2), password="pw")
    c = client(s, "pw")
    c.frame()
    assert c.security == 2
    assert client(serve(security=(2, 1), password="pw")).frame()      # no password: falls back to None


def test_password_longer_than_eight_is_truncated_like_servers_do(serve):
    s = serve(security=(2,), password="12345678")
    assert client(s, "12345678-and-more").frame()


def test_unsupported_security_names_the_offer(serve):
    s = serve(security=(19, 18))
    with pytest.raises(rfb.RFBUnsupported) as e:
        client(s).frame()
    assert "VeNCrypt" in str(e.value) and "TLS" in str(e.value)


def test_refusal_reason_is_reported(serve):
    for v in ("3.3", "3.8"):
        with pytest.raises(rfb.RFBConnectionError, match="too many connections"):
            client(serve(version=v, refuse="too many connections")).frame()


def test_not_an_rfb_server_and_unsupported_major(serve):
    with pytest.raises(rfb.RFBProtocolError, match="not an RFB server"):
        client(serve(hostile="http")).frame()
    with pytest.raises(rfb.RFBUnsupported):
        client(serve(version="4.0")).frame()


def test_connect_refused():
    c = rfb.RFB("127.0.0.1::1", timeout=1)
    with pytest.raises(rfb.RFBConnectionError):
        c.frame()


# --- encodings -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("enc,code", [("raw", 0), ("hextile", 5), ("hextile-rich", 5), ("zlib", 6), ("zrle", 16)])
def test_encodings_decode_exactly(serve, enc, code):
    s = serve(encoding=enc, width=150, height=100)
    c = client(s)
    assert c.frame()[2] == fb_to_rgb(s.fb)
    assert c.stats["enc%d" % code] >= 1
    s.fill(40, 30, 33, 21, (1, 2, 3))              # a second frame exercises the persistent zlib streams
    assert c.frame()[2] == fb_to_rgb(s.fb)


@pytest.mark.parametrize("enc", ["raw", "hextile-rich", "zlib", "zrle"])
@pytest.mark.parametrize("size", [(1, 1), (16, 16), (17, 15), (64, 64), (65, 65), (130, 70)])
def test_encoding_edge_sizes(serve, enc, size):
    s = serve(encoding=enc, width=size[0], height=size[1])
    assert client(s).frame()[2] == fb_to_rgb(s.fb)


def test_copyrect(serve):
    s = serve()
    c = client(s)
    c.frame()
    s.copy_next = (0, 0, 20, 10, 60, 50)
    c.update()
    assert c.stats["enc1"] == 1
    assert c.frame()[2] == fb_to_rgb(s.fb)


def test_copyrect_overlapping(serve):
    s = serve()
    c = client(s)
    c.frame()
    s.copy_next = (0, 0, 40, 40, 10, 10)
    c.update()
    assert c.frame()[2] == fb_to_rgb(s.fb)


def test_many_small_rects_still_work(serve):
    s = serve(hostile="many_rects")
    c = client(s)
    c.update()
    assert c.stats["rects"] >= 65535


def test_unsolicited_pseudo_update_is_not_taken_for_the_answer(serve):
    """QEMU acknowledges the extended key event with an update of its own; the real answer to the first request
    follows. Mistaking the ack for the answer makes every later capture one request stale."""
    s = serve(ext_keys=True, qemu_like=True, encoding="zrle")
    c = client(s)
    c.connect()
    assert c.ext_keys
    s.fill(0, 0, 50, 50, (200, 100, 50))
    for i in range(4):
        s.fill(i, 60, 5, 5, (i, i, 255))
        assert c.frame()[2] == fb_to_rgb(s.fb), f"capture {i} is stale"
    assert c._pending == 0


def test_resize_when_the_server_still_owes_the_pixels(serve):
    s = serve(ext_keys=True, qemu_like=True, width=64, height=48)
    c = client(s)
    c.frame()
    s.resize(96, 60)
    t0 = time.monotonic()
    w, h, rgb = c.frame()
    assert (w, h) == (96, 60) and rgb == fb_to_rgb(s.fb)
    s.fill(0, 0, 9, 9, (7, 7, 7))
    assert c.frame()[2] == fb_to_rgb(s.fb)          # and the next capture is not stale


# --- resize ----------------------------------------------------------------------------------------------------

def test_desktop_size_change_mid_session(serve):
    s = serve(encoding="zrle", width=64, height=48)
    c = client(s)
    assert c.frame()[:2] == (64, 48)
    s.resize(160, 90)
    w, h, rgb = c.frame()
    assert (w, h) == (160, 90) and rgb == fb_to_rgb(s.fb) and c.resizes == 1


def test_extended_desktop_size_is_understood(serve):
    s = serve(width=64, height=48)
    c = client(s, encodings=["raw"])
    c.frame()
    s.resize(32, 20)
    assert c.frame()[:2] == (32, 20)


def test_resize_storm_is_cut_off(serve):
    s = serve(hostile="resize_storm")
    with pytest.raises(rfb.RFBProtocolError, match="keeps changing size"):
        client(s).frame()


# --- hostile servers -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("hostile,exc", [
    ("huge_name", rfb.RFBProtocolError), ("huge_size", rfb.RFBProtocolError), ("huge_reason", rfb.RFBProtocolError),
    ("rect_oob", rfb.RFBProtocolError), ("unrequested_enc", rfb.RFBProtocolError),
    ("zlib_bomb", rfb.RFBProtocolError), ("zrle_bomb", rfb.RFBProtocolError), ("huge_len", rfb.RFBProtocolError),
    ("bad_msg", rfb.RFBProtocolError), ("cut_text", rfb.RFBProtocolError), ("bad_palette", rfb.RFBProtocolError),
    ("close_mid", rfb.RFBConnectionError)])
def test_hostile_server_is_rejected_quickly(serve, hostile, exc):
    s = serve(hostile=hostile)
    c = client(s, timeout=3.0)
    t0 = time.monotonic()
    with pytest.raises(exc):
        c.frame()
    assert time.monotonic() - t0 < 5
    assert not c.connected


def test_hostile_server_cannot_make_us_allocate_the_claimed_amount(serve):
    import tracemalloc
    s = serve(hostile="zlib_bomb")
    bomb()
    tracemalloc.start()
    with pytest.raises(rfb.RFBProtocolError):
        client(s).frame()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 20_000_000       # the bomb expands to 50 MB; the cap is the framebuffer size (60 kB here)


@pytest.mark.parametrize("hostile", ["stall", "bell_storm"])
def test_endless_or_stalled_server_hits_the_deadline(serve, hostile):
    s = serve(hostile=hostile)
    c = client(s, timeout=1.0)
    t0 = time.monotonic()
    with pytest.raises(rfb.RFBTimeout):
        c.frame()
    assert time.monotonic() - t0 < 3.0


def test_max_pixels_is_configurable(serve):
    s = serve(width=200, height=100)
    with pytest.raises(rfb.RFBProtocolError, match="exceeds"):
        client(s, max_pixels=1000).frame()


def test_bell_and_cut_text_are_ignored(serve):
    s = serve(hostile="bell_after")
    c = client(s)
    c.frame()
    c.frame()          # the Bell + cut text sent after the first update are consumed before the next request
    assert c.bells >= 1 and c.cut_text == "hello"


# --- reconnect, threads, secrets -------------------------------------------------------------------------------

def test_reconnects_after_the_server_drops_us(serve):
    s = serve(encoding="zrle")
    c = client(s)
    c.frame()
    s.drop_all()
    time.sleep(0.1)
    assert c.frame()[2] == fb_to_rgb(s.fb)
    assert s.connections == 2 and c.connections == 2


def test_reconnects_when_the_server_restarts(serve):
    s = serve()
    c = client(s)
    c.frame()
    s.drop_all()
    s.fill(0, 0, 10, 10, (9, 9, 9))
    assert c.frame()[2] == fb_to_rgb(s.fb)


def test_timeout_does_not_retry_and_next_call_reconnects(serve):
    s = serve()
    c = client(s, timeout=0.5)
    c.frame()
    s.drop_next_request = True
    with pytest.raises(rfb.RFBTimeout):
        c.frame()
    assert not c.connected
    assert c.frame()[2] == fb_to_rgb(s.fb)


def test_concurrent_callers_are_serialised(serve):
    s = serve(encoding="zlib")
    c = client(s)
    errors, frames = [], []

    def work():
        try:
            for _ in range(10):
                frames.append(c.frame()[2])
        except Exception as exc:       # pragma: no cover
            errors.append(exc)
    ts = [threading.Thread(target=work) for _ in range(4)]
    [t.start() for t in ts]
    [t.join(20) for t in ts]
    assert not errors and len(frames) == 40 and set(frames) == {fb_to_rgb(s.fb)}
    assert s.connections == 1


def test_repr_never_contains_the_password(serve):
    s = serve(security=(2,), password="s3cret-pw")
    c = client(s, "s3cret-pw")
    c.frame()
    assert "s3cret" not in repr(c) and "s3cret" not in str(vars(c).get("address"))


# --- transport -------------------------------------------------------------------------------------------------

def mk(srv, **kw):
    ck = {"timeout": 3.0}
    ck.update({k: kw.pop(k) for k in list(kw) if k in ("encodings", "use_ext_keys")})
    return tv.VNCTransport(srv.address, client=rfb.RFB(srv.address, kw.pop("password", None), **ck), key_gap=0.0, **kw)


def keys_of(srv, kind, n=None, wait=2.0):
    """Events of one kind the server has seen; with ``n`` wait until that many arrived (it reads asynchronously)."""
    end = time.monotonic() + wait
    while n is not None and len(srv.events(kind)) < n and time.monotonic() < end:
        time.sleep(0.01)
    return [m[1:] for m in srv.events(kind)]


def test_frame_is_a_ppm_frame_the_backend_can_use(serve):
    s = serve(encoding="zrle")
    t = mk(s)
    f = t.frame()
    assert (f.width, f.height) == (150, 100) and f.data[f.offset:] == fb_to_rgb(s.fb)
    assert parse_ppm(f.data).digest == f.digest
    s.fill(0, 0, 3, 3, (255, 0, 0))
    assert t.frame().digest != f.digest
    png = t.screenshot()
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and int.from_bytes(png[16:20], "big") == 150


def test_status_and_unsupported_capabilities(serve):
    t = mk(serve())
    assert t.status() == "running"
    caps = t.capabilities()
    for name in ("snapshots", "reset", "eject", "power"):
        assert caps[name]["available"] is False and "VNC" in caps[name]["reason"]
    assert caps["screen"]["available"] and caps["keyboard"]["available"] and caps["pointer"]["available"]
    with pytest.raises(tv.VNCUnsupported):
        t.cmd("system_reset")
    with pytest.raises(tv.VNCUnsupported):
        t.hmp("savevm x")
    with pytest.raises(tv.QMPError):                  # the backend's existing handlers catch this class
        t.hmp("eject cd")


def test_chord_with_extended_key_event(serve):
    s = serve(ext_keys=True)
    t = mk(s)
    t.status()
    assert t._rfb.ext_keys
    t0 = time.monotonic()
    t.send_chord(["ctrl", "alt", "delete"], hold_ms=40)
    assert time.monotonic() - t0 >= 0.04
    ev = keys_of(s, "extkey", 6)
    assert ev == [(1, 0xFFE3, 0x1D), (1, 0xFFE9, 0x38), (1, 0xFFFF, 0xD3),
                  (0, 0xFFFF, 0xD3), (0, 0xFFE9, 0x38), (0, 0xFFE3, 0x1D)]    # up in reverse order
    assert not s.events("key")


def test_keysym_fallback_sends_real_viewer_style_shift(serve):
    s = serve(ext_keys=False)
    t = mk(s)
    t.send_chord(["shift", "backslash"], hold_ms=20)             # '|'
    t.send_chord(["shift", "a"], hold_ms=20)                     # 'A'
    t.send_chord(["a"], hold_ms=20)
    t.send_chord(["ctrl", "c"], hold_ms=20)
    assert keys_of(s, "key", 14) == [
        (1, 0xFFE1), (1, 0x7C), (0, 0x7C), (0, 0xFFE1),
        (1, 0xFFE1), (1, 0x41), (0, 0x41), (0, 0xFFE1),
        (1, 0x61), (0, 0x61),
        (1, 0xFFE3), (1, 0x63), (0, 0x63), (0, 0xFFE3)]
    assert not s.events("extkey")


def test_ext_keys_can_be_disabled(serve):
    s = serve(ext_keys=True)
    t = mk(s, use_ext_keys=False)
    t.send_chord(["a"], hold_ms=10)
    assert keys_of(s, "key", 2) == [(1, 0x61), (0, 0x61)]


def test_never_zero_delay_between_down_and_up(serve):
    s = serve()
    t = mk(s)
    t0 = time.monotonic()
    t.send_chord(["a"], hold_ms=0)
    assert time.monotonic() - t0 >= tv.MIN_HOLD_MS / 1000 * 0.9


def test_unmappable_key_sends_nothing(serve):
    s = serve()
    t = mk(s)
    with pytest.raises(tv.VNCError):
        t.send_chord(["ctrl", "no_such_key"], hold_ms=10)
    time.sleep(0.1)
    assert not s.events("key") and not s.events("extkey")


def test_pointer_scaling_and_buttons(serve):
    s = serve(width=200, height=100)
    t = mk(s)
    t.click(99, 49, 100, 50, button="right", count=2)           # model space 100x50 -> framebuffer 200x100
    ev = keys_of(s, "pointer", 5)
    assert ev[0] == (0, 199, 99)                                  # last model pixel -> last framebuffer pixel
    assert [e[0] for e in ev] == [0, 4, 0, 4, 0] and {e[1:] for e in ev} == {(199, 99)}
    t.wheel("down", 2, (0, 0), 200, 100)
    ev = keys_of(s, "pointer", 10)[5:]
    assert [e[0] for e in ev] == [0, 16, 0, 16, 0] and ev[0][1:] == (0, 0)
    t.drag((0, 0), (199, 99), 200, 100, steps=3)
    ev = keys_of(s, "pointer", 16)[10:]
    assert ev[1][0] == 1 and ev[-1][0] == 0 and ev[-1][1:] == (199, 99)


def test_click_releases_button_even_when_interrupted(serve):
    s = serve()
    t = mk(s)
    t.move_to(5, 5, 150, 100)
    t.button("left", True)
    t.close()
    ev = keys_of(s, "pointer", 3)
    assert ev[-1][0] == 0


def test_modifiers_around_click(serve):
    s = serve(ext_keys=True)
    t = mk(s)
    t.status()
    t.click(10, 10, 150, 100, modifiers=["shift"])
    ex = keys_of(s, "extkey", 2)
    assert ex[0][0] == 1 and ex[-1][0] == 0 and ex[0][2] == 0x2A


def test_watch_sees_a_change(serve):
    s = serve()
    t = mk(s)
    threading.Timer(0.3, lambda: s.fill(10, 10, 80, 60, (255, 255, 0))).start()
    obs = t.watch(duration=3.0, quiet=0.4, poll=0.05)
    assert obs.changed and obs.size == (150, 100) and obs.major


def test_watch_on_a_still_screen_settles(serve):
    t = mk(serve())
    obs = t.watch(duration=2.0, quiet=0.3, poll=0.05)
    assert not obs.changed and obs.settled


def test_from_settings_uses_env_name_not_value(serve, monkeypatch):
    s = serve(security=(2,), password="envpw")
    monkeypatch.setenv("MY_VNC_PW", "envpw")
    t = tv.VNCTransport.from_settings({"vnc": s.address, "vnc_password_env": "MY_VNC_PW", "vnc_timeout": 3})
    assert t.status() == "running" and s.auth_results == [True]
    monkeypatch.delenv("MY_VNC_PW")
    with pytest.raises(tv.VNCError, match="MY_VNC_PW"):
        tv.VNCTransport.from_settings({"vnc": s.address, "vnc_password_env": "MY_VNC_PW"})
    with pytest.raises(tv.VNCError, match="no VNC address"):
        tv.VNCTransport.from_settings({}, {})
    tv.shutdown_all()


def test_auth_errors_are_vnc_errors_for_the_backend(serve):
    s = serve(security=(2,), password="a")
    with pytest.raises(tv.QMPError, match="authentication failed"):
        tv.VNCTransport(s.address, "b", timeout=2)
    tv.shutdown_all()


def test_pool_shares_one_connection(serve):
    s = serve()
    a = tv.VNCTransport(s.address, timeout=3)
    b = tv.VNCTransport(s.address, timeout=3)
    a.frame()
    b.frame()
    a.close()
    b.frame()
    assert s.connections == 1
    tv.shutdown_all()
