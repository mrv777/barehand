"""Screen text in results: fencing, reliability gating, never raising; ctrl-alt-del; wait_for_text."""

import hashlib
import json
import os

import pytest

from barehand import companion, config, screentext, text
from barehand.backend import BarehandBackend
from barehand.frames import Frame
from barehand.qmp import QMP

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "text")


def as_frame(name):
    with open(os.path.join(FIX, name), "rb") as fh:
        rgb, w, h = text.decode_png(fh.read())
    return Frame(rgb, w, h, 0, hashlib.blake2b(rgb, digest_size=16).digest())


@pytest.fixture(autouse=True)
def _fresh():
    text._ANALYSIS.clear(); text._GRID_HINT.clear(); text._PREFERRED.clear()
    screentext.reset_caches()
    yield


@pytest.fixture
def edk2_store(scratch_home):
    with open(os.path.join(FIX, "edk2_atlas.json")) as fh:
        atlas = text.Atlas.from_json(fh.read())
    screentext.store().save(atlas)
    return atlas


@pytest.fixture
def be(fake, monkeypatch):
    return BarehandBackend(settings={"qmp": fake.address, "settle_quiet": 0.15, "settle_timeout": 1.0,
                                     "settle_delay": 0.02, "settle_poll": 0.03})


def show(monkeypatch, name):
    f = as_frame(name)
    monkeypatch.setattr(QMP, "frame", lambda self: f)
    return f


def test_reliable_frame_puts_fenced_text_in_the_capture_note(be, edk2_store, monkeypatch):
    show(monkeypatch, "edk2_setup_main.png")
    cap = be.capture()
    assert "[screen-text " in cap.note and "[/screen-text " in cap.note
    assert "untrusted" in cap.note and cap.mode == "vision" and cap.png_b64


def test_unreliable_frame_adds_no_text(be, edk2_store, monkeypatch):
    show(monkeypatch, "linux_setup_disk.png")   # a Linux console; only the EDK2 atlas is known
    cap = be.capture()
    assert "screen-text" not in cap.note


def test_read_screen_raising_leaves_results_fine(be, edk2_store, monkeypatch):
    show(monkeypatch, "edk2_setup_main.png")
    monkeypatch.setattr(text, "read_screen", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    cap = be.capture()
    assert "[screen-text " not in cap.note and cap.png_b64
    res = be.key("down")
    assert res.ok and "[screen-text " not in res.message


def test_action_result_carries_text_when_reliable(be, edk2_store, monkeypatch):
    show(monkeypatch, "edk2_setup_main.png")
    res = be.key("down")
    assert res.ok and "[screen-text " in res.message


def test_ax_mode_is_text_only_when_reliable_and_image_otherwise(be, edk2_store, monkeypatch):
    show(monkeypatch, "edk2_setup_main.png")
    cap = be.capture(mode="ax")
    assert cap.mode == "ax" and cap.png_b64 == "" and "[screen-text " in cap.note
    show(monkeypatch, "linux_setup_disk.png")
    cap = be.capture(mode="ax")
    assert cap.mode == "vision" and cap.png_b64 and "could not be read as text" in cap.note


def test_edk2_atlas_is_derived_once_and_failures_are_silent(scratch_home, monkeypatch):
    calls = []
    monkeypatch.setattr(text, "atlas_from_edk2_fd", lambda p: calls.append(p) or (_ for _ in ()).throw(ValueError("x")))
    assert screentext.derive_edk2("/nonexistent.fd") is False
    assert screentext.derive_edk2("/nonexistent.fd") is False and calls == ["/nonexistent.fd"]
    assert screentext.atlases() == []


def test_ctrl_alt_del_needs_approval_and_sends_the_chord(fake):
    assert "send_ctrl_alt_del" in companion.ACTIONS
    res = companion._approval_hook(tool_name="barehand_machine", args={"action": "send_ctrl_alt_del"})
    assert res and res["action"] == "approve" and res["rule_key"] == "barehand:send_ctrl_alt_del"
    out = json.loads(companion.handle({"action": "send_ctrl_alt_del"}) if False else
                     _with_endpoint(fake, {"action": "send_ctrl_alt_del"}))
    assert out["ok"], out
    sent = [[k["data"] for k in c["arguments"]["keys"]] for c in fake.calls("send-key")]
    assert sent == [["ctrl", "alt", "delete"]]


def _with_endpoint(fake, args):
    os.environ["BAREHAND_QMP"] = fake.address
    try:
        return companion.handle(args)
    finally:
        del os.environ["BAREHAND_QMP"]


# --- wait_for_text -------------------------------------------------------------------------------------


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


def run_wait(pattern, frames, timeout=10, atlas=None):
    clk = Clock()
    it = iter(frames)
    last = [None]

    def src():
        try:
            last[0] = next(it)
        except StopIteration:
            pass
        return last[0]
    return screentext.wait_for_text(pattern, timeout, src, atlas_list=atlas, clock=clk.now, sleep=clk.sleep)


def test_wait_for_text_returns_as_soon_as_it_appears(edk2_store):
    a, b = as_frame("linux_setup_disk.png"), as_frame("edk2_setup_main.png")
    res = run_wait("Device Manager", [a, a, b], atlas=[edk2_store])
    assert res["ok"] and "Device Manager" in res["matched"] and res["elapsed"] <= 2.0


def test_wait_for_text_regex_and_timeout_shows_last_screen(edk2_store):
    b = as_frame("edk2_setup_main.png")
    assert run_wait("re:Dev\\w+ Man", [b], atlas=[edk2_store])["ok"]
    res = run_wait("no such text", [b], timeout=3, atlas=[edk2_store])
    assert not res["ok"] and "timed out" in res["message"] and "[screen-text " in res["message"]


def test_wait_for_text_fails_fast_without_an_atlas(scratch_home):
    res = run_wait("x", [as_frame("edk2_setup_main.png")], atlas=[])
    assert not res["ok"] and "calibrate_text" in res["message"] and res["message"].count("timed out") == 0


def test_wait_for_text_bad_regex_and_timeout_cap(edk2_store):
    assert "bad regular expression" in run_wait("re:(", [as_frame("edk2_setup_main.png")], atlas=[edk2_store])["message"]
    res = run_wait("zzz", [as_frame("edk2_setup_main.png")], timeout=10_000, atlas=[edk2_store])
    assert not res["ok"] and res["elapsed"] <= screentext.WAIT_MAX + 1


# --- single header, delta/unchanged blocks, off-line, memo, partial, wait hardening ---------------------


def fake_st(rows, inverse=None, cursor=None, atlas="a", reliable=True, reason="", conf=1.0):
    rows = list(rows)
    last = max((i for i, r in enumerate(rows) if r), default=None)
    return text.ScreenText(reliable, reason, conf, None, rows, inverse or [[] for _ in rows], cursor, last, {}, [], atlas)


def screen(n=30, **edits):
    rows = [f"row {i}" for i in range(n)]
    for k, v in edits.items():
        rows[int(k[1:])] = v
    return rows


def test_one_header_with_glyph_legend(be, edk2_store, monkeypatch):
    show(monkeypatch, "edk2_setup_main.png")
    note = be.capture().note
    head = [l for l in note.split("\n") if l.startswith("[screen-text ")]
    assert len(head) == 1 and "exact glyph shapes" in head[0] and "unreadable cell" in head[0]
    assert "SCREEN TEXT (" not in note


def test_text_state_full_unchanged_delta_and_resync():
    ts = screentext.TextState()
    a = fake_st(screen())
    ts = screentext.TextState()
    assert "delta" not in ts.block(a).split("\n")[0]                       # first: full
    assert ts.block(fake_st(screen())) == "screen text unchanged"          # identical
    d = ts.block(fake_st(screen(r3="changed three")))                      # few rows changed: delta
    assert "delta against the previous full block" in d.split("\n")[0] and " 3 |changed three" in d
    assert " 7 |" not in d
    many = fake_st([f"new {i}" for i in range(30)])
    assert "delta" not in ts.block(many).split("\n")[0] and "new 29" in ts.block(many, full=True)   # >= half: full
    assert ts.block(many) == "screen text unchanged"
    full = ts.block(many, full=True)                                       # explicit capture: always full
    assert full.startswith("[screen-text ") and "delta" not in full.split("\n")[0]
    outs = [ts.block(many) for _ in range(screentext.RESYNC_EVERY + 1)]
    assert sum(o.startswith("[screen-text ") for o in outs) == 1 and outs[0] == "screen text unchanged"


def test_text_state_detects_moved_highlight_and_grid_change():
    ts = screentext.TextState()
    ts.block(fake_st(screen(), inverse=[[(0, 4)]] + [[] for _ in range(29)], cursor=(0, 0)))
    d = ts.block(fake_st(screen(), inverse=[[]] + [[(0, 4)]] + [[] for _ in range(28)], cursor=(1, 0)))
    assert d != "screen text unchanged" and " 1>|row 1" in d
    st = fake_st(screen())
    st.grid = text.Grid(8, 16, 0, 0, 80, 30)
    out = ts.block(st)
    assert out.startswith("[screen-text ") and "delta" not in out.split("\n")[0]       # grid changed: full


def test_off_reason_is_said_once_per_state_change():
    ts = screentext.TextState()
    gfx = fake_st(screen(), reliable=False, conf=0.1, reason="only 10% of cells matched")
    first = ts.block(gfx)
    assert first.startswith("[screen text off: this screen's font has not been learned") and "calibrate_text" in first
    assert ts.block(gfx) == ""
    part = fake_st(screen(), reliable=False, conf=0.92, reason="only 92% matched")
    assert "partly unreadable" in ts.block(part)
    assert ts.block(part) == ""
    assert ts.block(fake_st(screen())).startswith("[screen-text ")
    assert "has not been learned" in ts.block(gfx)       # state changed again: said again


def test_read_frame_memoises_by_digest_and_remembers_unreadable(edk2_store, monkeypatch):
    calls = []
    real = text.read_screen
    monkeypatch.setattr(text, "read_screen", lambda *a, **k: calls.append(1) or real(*a, **k))
    f = as_frame("edk2_setup_main.png")
    assert screentext.read_frame(f, [edk2_store]).reliable
    assert screentext.read_frame(f, [edk2_store]).reliable and len(calls) == 1
    g = as_frame("linux_setup_disk.png")
    assert not screentext.read_frame(g, [edk2_store]).reliable and len(calls) == 2
    g2 = Frame(g.data, g.width, g.height, g.offset, b"other-digest-000")      # same geometry, "new" frame
    assert not screentext.read_frame(g2, [edk2_store]).reliable and len(calls) == 2   # negative cache
    monkeypatch.setattr(screentext, "UNREADABLE_RETRY", 0.0)
    g3 = Frame(g.data, g.width, g.height, g.offset, b"third-digest-0000")
    screentext.read_frame(g3, [edk2_store])
    assert len(calls) == 3


def test_backend_sends_unchanged_line_then_full_on_capture(be, edk2_store, monkeypatch):
    show(monkeypatch, "edk2_setup_main.png")
    cap = be.capture()
    assert "[screen-text " in cap.note
    r1 = be.key("down")
    assert "screen text unchanged" in r1.message or "[screen-text " in r1.message
    r2 = be.key("down")
    assert "screen text unchanged" in r2.message
    assert "[screen-text " in be.capture().note


def test_off_line_in_results_only_once(be, edk2_store, monkeypatch):
    show(monkeypatch, "linux_setup_disk.png")
    r1, r2 = be.key("down"), be.key("down")
    assert "screen text off:" in r1.message and "screen text off:" not in r2.message


def test_partial_block_marks_unreadable_rows():
    rows = ["alpha one", "beta two", "gamma three", "caf\ufffd cr\ufffdme", "delta four"]
    st = fake_st(rows, reliable=False, conf=0.9)
    st.unknown = [(3, 3), (3, 9)]
    st.grid = text.Grid(8, 16, 0, 0, 80, 5)
    out = screentext.partial_block(st)
    assert "PARTIAL" in out.split("\n")[0] and " 3 |(unreadable row)" in out and " 0 |alpha one" in out
    assert not screentext.is_text_block(out)
    bad = fake_st(["\ufffd\ufffd", "\ufffd", "x y z", "\ufffd"], reliable=False, conf=0.7)
    bad.grid = st.grid
    bad.unknown = [(0, 0), (1, 0), (3, 0)]
    assert screentext.partial_block(bad) == ""
    low = fake_st(rows, reliable=False, conf=0.3)
    low.grid = st.grid
    assert screentext.partial_block(low) == ""


def test_wait_for_text_rejects_long_or_nested_patterns_and_caps_timeout(edk2_store):
    b = as_frame("edk2_setup_main.png")
    assert "longer than" in run_wait("re:" + "a" * 201, [b], atlas=[edk2_store])["message"]
    assert "quantifier" in run_wait("re:(a+)+b", [b], atlas=[edk2_store])["message"]
    assert "bad regular expression" in run_wait("re:(", [b], atlas=[edk2_store])["message"]
    assert screentext.WAIT_MAX == 240.0
    res = run_wait("never", [b], timeout=100000, atlas=[edk2_store])
    assert "timed out after 240s" in res["message"]


@pytest.mark.parametrize("pat", [
    "re:(a|aa)+$", "re:(a+)+b", "re:(ab)*", "re:(?:ab)?c", "re:(x){2}", "re:(a)\\1", "re:(?=a)b", "re:(?<!a)b",
    "re:(?P<n>a)(?P=n)", "re:a*b*c*d*", "re:a+b+c+d{2,}", "re:" + "a" * 201,
])
def test_regex_outside_the_cheap_subset_is_rejected_instantly(pat):
    import time as _t
    t0 = _t.monotonic()
    with pytest.raises(screentext.PatternError) as ei:
        screentext.compile_pattern(pat)
    assert _t.monotonic() - t0 < 0.1
    assert "substring" in str(ei.value)


@pytest.mark.parametrize("pat,row,hit", [
    (r"re:^login:\s*$", "login:  ", True), (r"re:\d+% complete", "copy 45% complete", True),
    ("re:(error|failed)", "it failed", True), ("re:(error|failed)", "fine", False),
    (r"re:[*+]x", "+x", True), (r"re:a{2,3}b", "aaab", True), (r"re:a.*b.*c", "a1b2c", True),
])
def test_legitimate_regexes_still_work(pat, row, hit):
    assert (screentext.compile_pattern(pat)(row) is not None) is hit


def test_regex_matches_only_the_first_400_chars_of_a_row():
    m = screentext.compile_pattern("re:needle")
    assert m("x" * 399 + "needle") is None and m("needle" + "x" * 500) is not None


def test_edk2_thread_is_started_once(scratch_home, monkeypatch):
    started = []
    monkeypatch.setattr(screentext, "firmware_code", lambda *a, **k: "/fw/code.fd")
    release = []
    import threading
    gate = threading.Event()
    monkeypatch.setattr(text, "atlas_from_edk2_fd", lambda p: started.append(p) or gate.wait(2) or (_ for _ in ()).throw(ValueError("x")))
    for _ in range(5):
        screentext.ensure_edk2()
    import time as _t
    _t.sleep(0.1)
    gate.set()
    _t.sleep(0.2)
    screentext.ensure_edk2()
    assert started == ["/fw/code.fd"]          # one thread, and the failure is not retried at once


def test_partial_mode_on_a_synthetic_screen_with_accents():
    from test_text_grid import make_font, render
    with open(os.path.join(FIX, "linux_calib_login.png"), "rb") as fh:
        rgb, w, h = text.decode_png(fh.read())
    atlas, _ = text.calibrate((rgb, w, h), text.CALIBRATION_TEXT, row=38, col=18)
    font, cw, ch = make_font(atlas)
    z = [0] * 8
    font["é"] = [z] * 3 + [[0, 0, 0, 0, 1, 0, 0, 0]] + [[0, 0, 1, 1, 1, 1, 0, 0]] + [[0, 1, 0, 0, 0, 1, 0, 0]] * 8 + [z] * 3
    lines = ["Debian installer main menu", "Select a language please", "plain ascii row number three",
             "another plain row for the test", "café café café café", "last ascii row here"]
    buf = render(lines, font, cw, ch, 800, 600)
    st = text.read_screen(buf, 800, 600, [atlas])
    if st.reliable:                       # the accents were too few to drop the confidence: nothing to test
        return
    out = screentext.partial_block(st)
    assert out and "PARTIAL" in out.split("\n")[0] and "(unreadable row)" in out and "plain ascii row number three" in out
    ts = screentext.TextState()
    assert ts.block(st).startswith("[screen-text ") and not screentext.is_text_block(ts.block(st))


def test_companion_wait_for_text_uses_one_connection_and_fails_when_not_running(fake, edk2_store):
    c0 = fake.connections
    out = json.loads(_with_endpoint(fake, {"action": "wait_for_text", "text": "zzz-never", "timeout": 2}))
    assert out["ok"] is False and "timed out" in out["error"]
    assert fake.connections - c0 == 1
    fake.status = "paused"
    out = json.loads(_with_endpoint(fake, {"action": "wait_for_text", "text": "zzz", "timeout": 1}))
    assert out["ok"] is False and "paused" in out["error"]


def test_companion_wait_for_text_derives_the_firmware_atlas_first(fake, edk2_store, monkeypatch):
    calls = []
    monkeypatch.setattr(screentext, "ensure_edk2", lambda *a, **k: calls.append((a, k)))
    json.loads(_with_endpoint(fake, {"action": "wait_for_text", "text": "zzz-never", "timeout": 1}))
    assert calls == [((None, None), {"wait": 2.0})]


def test_reset_caches_clears_edk2_threads():
    screentext._edk2_threads["x"] = object()
    screentext.reset_caches()
    assert not screentext._edk2_threads


def test_calibrate_text_refuses_without_typing_when_the_screen_already_reads(fake, edk2_store, monkeypatch):
    st = text.read_screen(*text.decode_png(open(os.path.join(FIX, "edk2_setup_main.png"), "rb").read()), [edk2_store])
    assert st.reliable
    monkeypatch.setattr(screentext, "read_frame", lambda *a, **k: st)
    out = json.loads(_with_endpoint(fake, {"action": "calibrate_text"}))
    assert out["ok"] is False and "nothing typed" in out["error"] and "firmware" in out["error"]
    assert not fake.calls("send-key")
    fake.status = "paused"
    out = json.loads(_with_endpoint(fake, {"action": "calibrate_text"}))
    assert out["ok"] is False and "paused" in out["error"] and not fake.calls("send-key")


def test_calibrate_text_is_not_approval_gated_and_sends_no_ctrl_l():
    assert companion._approval_hook(tool_name="barehand_machine", args={"action": "calibrate_text"}) is None
    assert companion._approval_hook(tool_name="barehand_machine", args={"action": "wait_for_text"}) is None
    import inspect
    assert '"l"' not in inspect.getsource(screentext.calibrate_machine)


def test_atlas_store_rejects_bad_labels_and_lists_and_purges(scratch_home, edk2_store):
    bad = json.loads(edk2_store.to_json())
    bad["glyphs"]["ff" * 19] = "‮"
    with pytest.raises(ValueError):
        text.Atlas.from_json(json.dumps(bad))
    bad["glyphs"].pop("ff" * 19)
    bad["glyphs"]["fe" * 19] = "\n"
    with pytest.raises(ValueError):
        text.Atlas.from_json(json.dumps(bad))
    st = screentext.store()
    lst = st.listing()
    assert len(lst) == 1 and lst[0]["valid"] and lst[0]["source"] == "edk2-firmware"
    assert st.purge("nothing-like-this") == 0 and st.purge() == 1 and st.listing() == []


def test_cli_atlases_list_and_purge(scratch_home, edk2_store):
    from barehand import cli
    assert "edk2-firmware" in cli.run(["atlases", "list"])
    with pytest.raises(Exception):
        cli.run(["atlases", "purge"])                       # needs --yes outside a terminal
    assert "deleted 1" in cli.run(["atlases", "purge", "--yes"])
    assert "no learned fonts" in cli.run(["atlases", "list"])


def test_placeholder_banner_is_recognised_at_any_size(be):
    w, h = 1280, 800
    buf = bytearray(b"\x00\x00\x00" * (w * h))
    for x in range(600, 700):
        for y in range(398, 402):
            buf[(y * w + x) * 3:(y * w + x) * 3 + 3] = b"\xaa\xaa\xaa"
    f = Frame(bytes(buf), w, h, 0, hashlib.blake2b(bytes(buf), digest_size=16).digest())
    assert be._pre_state(f, ())[0] == "banner"


# --- Debian installer regressions, calibrate cleanup, first-frame wait --------------------------------------


@pytest.mark.parametrize("name", ["debian_installer_r005.png", "debian_installer_r010.png"])
def test_other_font_half_matching_gives_unknown_font_line_and_no_partial_block(edk2_store, name):
    f = as_frame(name)
    st = screentext.read_frame(f, [edk2_store])
    assert st is not None and not st.reliable
    assert screentext.partial_block(st) == ""
    out = screentext.TextState().block(st)
    assert "font has not been learned" in out and "calibrate_text" in out and "non-ASCII" not in out


def test_wrong_grid_noise_rows_are_not_a_partial_block():
    rows = [""] * 5 + ["  " + "\u2592" * 60] * 6 + ["(x)"] * 3
    st = fake_st(rows, reliable=False, conf=0.95)
    st.grid = text.Grid(8, 19, 0, 0, 80, len(rows))
    assert screentext.partial_block(st) == ""
    assert "font has not been learned" in screentext.TextState().block(st)


class FakeQ:
    def __init__(self, fail_after=None):
        self.sent = []
        self.fail_after = fail_after

    def status(self):
        return "running"

    def send_chord(self, chord, hold_ms=0):
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            raise RuntimeError("link lost")
        self.sent.append(tuple(chord))

    def frame(self):
        return as_frame("linux_calib_login.png")


def _backspaces(q):
    return sum(1 for c in q.sent if c == ("backspace",))


def test_calibrate_erases_with_backspace_exactly_what_was_typed(monkeypatch):
    monkeypatch.setattr(screentext, "_settle", lambda q, *a, **k: q.frame())
    monkeypatch.setattr(screentext, "read_frame", lambda *a, **k: None)
    monkeypatch.setattr(screentext.time, "sleep", lambda s: None)
    # failure path: no cell grid is learned, both attempts type 94 characters
    q = FakeQ()
    out = screentext.calibrate_machine(q)
    assert out["ok"] is False
    assert ("ctrl", "u") not in q.sent
    assert len(q.sent) - _backspaces(q) == _backspaces(q) and _backspaces(q) >= 94
    # success path: calibrate/verify stubbed
    atlas = text.Atlas.from_json(open(os.path.join(FIX, "edk2_atlas.json")).read())
    monkeypatch.setattr(screentext, "_calibrate_smallest", lambda *a: (type("A", (), {
        "missing_ascii": lambda self: [], "cw": 8, "ch": 16, "__len__": lambda self: 94})(), []))
    monkeypatch.setattr(text, "verify_calibration", lambda *a, **k: [])
    monkeypatch.setattr(screentext, "store", lambda: type("S", (), {"save": lambda self, a: "x.json"})())
    q = FakeQ()
    out = screentext.calibrate_machine(q)
    assert out["ok"] is True
    assert ("ctrl", "u") not in q.sent
    assert _backspaces(q) == len(q.sent) - _backspaces(q) == 188
    # verification failure: the reversed line is erased too
    monkeypatch.setattr(text, "verify_calibration", lambda *a, **k: ["bad"])
    q = FakeQ()
    assert screentext.calibrate_machine(q)["ok"] is False
    assert _backspaces(q) == len(q.sent) - _backspaces(q) == 188
    del atlas


def test_calibrate_erases_what_was_typed_when_a_key_send_fails(monkeypatch):
    monkeypatch.setattr(screentext, "_settle", lambda q, *a, **k: q.frame())
    monkeypatch.setattr(screentext, "read_frame", lambda *a, **k: None)
    q = FakeQ(fail_after=40)
    q.fail_after = 40
    real = q.send_chord

    def send(chord, hold_ms=0):
        if chord == ["backspace"]:
            q.fail_after = None
        return real(chord, hold_ms)
    q.send_chord = send
    with pytest.raises(RuntimeError):
        screentext.calibrate_machine(q)
    assert len(q.sent) == 80 and _backspaces(q) == 40


def test_wait_for_text_says_when_the_match_was_already_on_screen(edk2_store):
    b = as_frame("edk2_setup_main.png")
    st = screentext.read_frame(b, [edk2_store])
    word = next(r.strip() for r in st.rows if len(r.strip()) > 6)
    res = run_wait(word, [b], atlas=[edk2_store])
    assert res["ok"] and "already on screen when the wait started" in res["message"] and word in res["message"]
    assert res["matched"] == word
    other = as_frame("edk2_boot_options.png")
    res = run_wait(word, [other, b], atlas=[edk2_store])
    assert res["ok"] and "already on screen" not in res["message"]


def test_wait_for_text_description_warns_about_the_typed_command():
    d = companion.SCHEMA["description"]
    assert "command you just typed is on screen too" in d and "erases them with Backspace" in d


# --- honest off-reasons, capture repeats the reason, mostly-readable rows in PARTIAL --------------------------


def test_three_distinct_off_reasons_and_capture_always_repeats():
    ts = screentext.TextState()
    none = fake_st(screen(), reliable=False, conf=0.0, reason="no glyph atlas for 8x16")
    assert "no font learned yet" in ts.block(none)
    part = fake_st(screen(), reliable=False, conf=0.91, reason="only 91% matched")
    msg = ts.block(part)
    assert "partly unreadable" in msg and "non-ASCII" in msg and "THIS console" in msg and "\n" not in msg
    assert ts.block(part) == ""                                  # action results: once per state
    assert "partly unreadable" in ts.block(part, full=True)      # explicit capture: always
    other = fake_st(screen(), reliable=False, conf=0.6, reason="only 60% matched")
    out = ts.block(other, full=True)
    assert "same console" in out and "fonts can differ" in out
    assert "same console" in ts.block(other, full=True)


def test_partial_shows_mostly_readable_rows_with_unknown_cells_in_place():
    rows = ["� English", "� Fran�ais ok", "� Deutsch", "� Espa�ol", "�����",
            "� Italiano", "� Portugu�s"]
    st = fake_st(rows, reliable=False, conf=0.9)
    st.grid = text.Grid(8, 16, 0, 0, 80, len(rows))
    st.unknown = [(i, 0) for i in range(len(rows))]
    out = screentext.partial_block(st)
    assert " 1 |� Fran�ais ok" in out and " 0 |� English" in out and " 4 |(unreadable row)" in out
    low = fake_st(rows, reliable=False, conf=0.7)
    low.grid = st.grid
    assert screentext.partial_block(low) == ""


# --- result text after machine actions, honest wait_for_text, redundant hints ------------------------------


def test_reset_and_snapshot_load_carry_the_screen_text(fake, edk2_store, monkeypatch):
    from barehand import machine
    monkeypatch.setattr(machine, "setup_window", lambda name=None: 0.3)
    show(monkeypatch, "edk2_setup_main.png")
    out = json.loads(_with_endpoint(fake, {"action": "reset", "enter_setup": True}))
    assert out["ok"] and "[screen-text " in out["result"] and "capture to confirm" not in out["result"]
    fake.hmp_replies["info"] = "ID  TAG  VM_SIZE\n--  s1   49 MiB 2026"
    out = json.loads(_with_endpoint(fake, {"action": "snapshot_load", "name": "s1"}))
    assert out["ok"] and "[screen-text " in out["result"]


def test_reset_keeps_the_capture_instruction_when_no_text_could_be_attached(fake, edk2_store, monkeypatch):
    from barehand import machine
    monkeypatch.setattr(machine, "setup_window", lambda name=None: 0.3)
    show(monkeypatch, "linux_setup_disk.png")   # unreadable with the edk2 atlas: only the off reason may follow
    out = json.loads(_with_endpoint(fake, {"action": "reset", "enter_setup": True}))
    assert out["ok"] and "[screen-text " not in out["result"]
    monkeypatch.setattr(companion, "_screen_tail", lambda r: "")
    out = json.loads(_with_endpoint(fake, {"action": "reset", "enter_setup": True}))
    assert "capture to confirm setup is showing" in out["result"]


def test_successful_wait_for_text_returns_the_whole_screen_block(fake, edk2_store, monkeypatch):
    show(monkeypatch, "edk2_setup_main.png")
    out = json.loads(_with_endpoint(fake, {"action": "wait_for_text", "text": "Device Manager", "timeout": 2}))
    assert out["ok"] and out["matched_line"] and "[screen-text " in out["result"]


def test_wait_timeout_says_the_screen_was_readable_but_the_text_never_came(edk2_store):
    b = as_frame("edk2_setup_main.png")
    res = run_wait("zzz-never", [b], timeout=3, atlas=[edk2_store])
    assert "did not appear (the screen was readable as text" in res["message"] and "[screen-text " in res["message"]


def test_wait_gives_up_early_on_a_screen_that_is_never_readable_and_still(edk2_store):
    f = as_frame("linux_setup_disk.png")
    clk = Clock()
    res = screentext.wait_for_text("login:", 240, lambda: f, atlas_list=[edk2_store], clock=clk.now, sleep=clk.sleep)
    assert not res["ok"] and res["elapsed"] < 30 and res.get("unreadable")
    assert "never readable as text" in res["message"] and "Capture" in res["message"] and "waiting again will not help" in res["message"]


def test_wait_reports_a_blank_screen_as_blank_but_waits_it_out(edk2_store):
    # a blank screen may be a machine that is still starting, so it is not cut short
    black = Frame(bytes(640 * 400 * 3), 640, 400, 0, b"\x00" * 16)
    clk = Clock()
    res = screentext.wait_for_text("login:", 240, lambda: black, atlas_list=[edk2_store], clock=clk.now, sleep=clk.sleep)
    assert not res["ok"] and res["elapsed"] >= 239 and "blank" in res["message"] and "wait again" in res["message"]


def test_wait_keeps_waiting_while_an_unreadable_screen_keeps_changing(edk2_store):
    import random
    rnd = random.Random(1)
    clk = Clock()

    def src():
        d = bytes(rnd.randrange(256) for _ in range(64 * 64 * 3))
        return Frame(d, 64, 64, 0, hashlib.blake2b(d, digest_size=16).digest())
    res = screentext.wait_for_text("login:", 40, src, atlas_list=[edk2_store], clock=clk.now, sleep=clk.sleep)
    assert res["elapsed"] >= 39 and "timed out" in res["message"]


def test_recapture_hints_are_dropped_when_a_capture_follows_the_action(be, edk2_store, monkeypatch, fake):
    fake.react_to_input = False
    res = be.key("return")
    assert "capture before repeating" in res.message          # no capture attached: the hint stays
    res = be.key("return")
    be.capture()                                              # capture_after
    assert "capture before repeating" not in res.message and "screen did not change" in res.message


def test_key_repeat_form():
    from barehand import keys
    assert keys.parse_keys("down*3") == [["down"]] * 3
    assert keys.parse_keys("esc*2 return") == [["esc"], ["esc"], ["ret"]]
    assert len(keys.parse_keys("esc*40")) == 40
