"""Settle logic: a few typed characters are a change, a blinking caret is not; honest effects; advisory loop guard."""

import threading
import time

import pytest

from barehand import backend as backend_mod
from barehand.backend import BarehandBackend
from barehand.qmp import QMP

CHAR_BOX = (40, 30, 14, 10)   # three typed characters: well inside "cursor-sized"


def type_some_text(fake):
    fake.paint(*CHAR_BOX, (230, 230, 230))


@pytest.fixture
def quick(fake):
    """Settle windows short enough for unit tests; the blink window is shorter than the settle timeout."""
    return BarehandBackend(settings={"qmp": fake.address, "settle_quiet": 0.15, "settle_timeout": 2.0,
                                     "settle_delay": 0.02, "settle_poll": 0.03, "settle_blink": 0.3})


# --- item 1: false "did not visibly change" -----------------------------------------------------------
def test_a_few_typed_characters_are_a_change(quick, fake):
    fake.on_input = type_some_text
    res = quick.type_text("abc")
    assert res.ok and "did not change within" not in res.message
    assert res.effect == "confirmed" and "changed" in res.message


def test_typed_characters_next_to_a_blinking_caret_are_a_change(quick, fake):
    fake.cursor = (60, 30, 2, 10)       # blinks on every other screendump
    fake.on_input = type_some_text
    res = quick.type_text("abc")
    assert res.effect == "confirmed" and "did not change within" not in res.message


def test_the_pre_action_frame_is_the_reference_not_the_previous_poll(quick, fake):
    """The edit has landed before the first poll, so every later poll equals the one before it: only the
    comparison with the frame taken before the input shows the change."""
    fake.on_input = type_some_text
    q = QMP(fake.address, timeout=5)
    try:
        before = q.frame()
        type_some_text(fake)
        obs = q.watch(duration=2.0, quiet=0.15, poll=0.03, baseline=before, blink_window=0.3)
    finally:
        q.close()
    assert obs.real_changes >= 1 and obs.changed and obs.first_digest != obs.last_digest


def test_a_blinking_caret_alone_is_not_a_change(quick, fake):
    fake.cursor = (10, 10, 2, 10)
    fake.react_to_input = False
    res = quick.key("return")
    assert res.ok and "did not change within" in res.message and res.effect == "unverifiable"


def test_a_caret_blinking_slower_than_the_poll_is_still_not_a_change(quick, fake):
    """Frames A A A A A' A' A' A' A' A ...: the baseline comes back within the blink window."""
    fake.react_to_input = False
    fake.cursor = (10, 10, 2, 10)
    fake.blink_every = 5
    res = quick.key("return")
    assert res.effect == "unverifiable" and "did not change within" in res.message


def test_wait_mode_ignores_a_blinking_caret(quick, fake):
    fake.cursor = (10, 10, 2, 10)
    assert _watch(fake, early_quiet=None).real_changes == 0
    assert _watch(fake, early_quiet=0.15).real_changes == 0


def _watch(fake, early_quiet):
    q = QMP(fake.address, timeout=5)
    try:
        return q.watch(duration=0.6, quiet=0.15, poll=0.03, early_quiet=early_quiet, blink_window=0.2)
    finally:
        q.close()


# --- item 2: honest effects ---------------------------------------------------------------------------
def test_effects_by_outcome(quick, fake):
    assert quick.key("return").effect == "confirmed"                  # whole screen changed
    fake.react_to_input = False
    for res in (quick.key("return"), quick.click(x=5, y=5), quick.scroll(direction="down")):
        assert res.ok and res.effect == "unverifiable" and res.escalation is None
        assert "may have had no visible effect, or the machine is busy; capture before repeating" in res.message


def test_typed_text_with_nothing_visible_is_unverifiable_not_a_noop(quick, fake):
    fake.react_to_input = False
    res = quick.type_text("secret")
    assert res.ok and res.effect == "unverifiable" and "password" in res.message


def test_an_unobservable_screen_is_unverifiable(quick, fake):
    quick.capture()
    real = fake._handle
    fake._handle = lambda m: ({"error": {"desc": "no surface"}} if m["execute"] == "screendump" else real(m))
    res = quick.key("return")
    assert res.ok and res.effect == "unverifiable" and "could not be observed" in res.message


def test_failures_have_no_effect(quick):
    res = quick.key("f13")
    assert not res.ok and res.effect is None


# --- item 4: loop guard -------------------------------------------------------------------------------
def test_identical_input_on_an_unchanged_screen_gets_a_note_from_the_third_time(quick, fake):
    fake.react_to_input = False
    msgs = [quick.key("return").message for _ in range(4)]
    assert all("NOTE" not in m for m in msgs[:2])
    assert "NOTE" in msgs[2] and "3 times in a row" in msgs[2]
    assert "4 times in a row" in msgs[3] and "capture" in msgs[3]


def test_the_guard_is_advisory_the_input_is_still_sent(quick, fake):
    fake.react_to_input = False
    for _ in range(4):
        res = quick.key("return")
        assert res.ok
    assert len(fake.calls("send-key")) == 4


def test_the_guard_ignores_a_blinking_caret(quick, fake):
    fake.react_to_input = False
    fake.cursor = (10, 10, 2, 10)
    msgs = [quick.key("return").message for _ in range(3)]
    assert "NOTE" in msgs[2]


def test_repeats_that_change_the_screen_each_time_never_trigger_it(quick, fake):
    for _ in range(6):                       # pressing return through installer prompts
        assert "NOTE" not in quick.key("return").message


def test_arrow_keys_in_a_menu_that_move_a_highlight_never_trigger_it(quick, fake):
    row = [0]
    def move(f):
        f.paint(0, 10 * row[0] % 90, 100, 8, (0, 0, 200))     # highlight bar moves down one row each time
        f.paint(0, 10 * (row[0] - 1) % 90, 100, 8, (10, 30, 0))
        row[0] += 1
    fake.on_input = move
    for _ in range(6):
        assert "NOTE" not in quick.key("down").message


def test_different_arguments_reset_the_run(quick, fake):
    fake.react_to_input = False
    for keys in ("return", "return", "down", "return", "return"):
        assert "NOTE" not in quick.key(keys).message


def test_a_screen_that_changed_in_between_resets_the_run(quick, fake):
    fake.react_to_input = False
    quick.key("return"); quick.key("return")
    fake.fill(120)                           # something else changed the screen
    assert "NOTE" not in quick.key("return").message


def test_the_guard_covers_clicks_and_typed_text_too(quick, fake):
    fake.react_to_input = False
    out = [quick.click(x=5, y=5).message for _ in range(3)]
    assert "NOTE" in out[2]
    quick._recent.clear()
    out = [quick.type_text("x").message for _ in range(3)]
    assert "NOTE" in out[2]


# --- periodic small changes (clock, spinner): bounded, honest, learned ------------------------------------------
def tick_display(fake, period, box=(100, 8, 22, 8), phase=0.0):
    """A small region that shows a new picture every ``period`` seconds, like a seconds clock: each tick is a
    new digest of a cursor-sized change (the region's width changes)."""
    t0 = time.monotonic() - phase
    x, y, wmax, h = box

    def frame():
        fake.dumps += 1
        buf = bytearray(bytes(fake.pixels))
        k = int((time.monotonic() - t0) / period)
        w = 2 + k % (wmax - 1)
        for yy in range(y, y + h):
            o = (yy * fake.width + x) * 3
            buf[o:o + w * 3] = b"\xff" * (w * 3)
        return bytes(buf)
    fake._frame = frame


def timed(fn):
    t = time.monotonic()
    res = fn()
    return res, time.monotonic() - t


@pytest.fixture
def scaled(fake, vclock):
    """The shipped defaults (delay 0.2, quiet 0.7, poll 0.25, blink 1.2) at half scale, so tests stay quick."""
    return BarehandBackend(settings={"qmp": fake.address, "settle_quiet": 0.35, "settle_timeout": 4.0,
                                     "settle_delay": 0.1, "settle_poll": 0.125, "settle_blink": 0.6})


def test_a_fast_ticking_region_is_cut_short_reported_honestly_and_then_ignored(scaled, fake):
    tick_display(fake, 0.22)                      # the reviewer's 0.45 s ticker, at half scale
    fake.react_to_input = False
    scaled.capture()
    first, t_first = timed(lambda: scaled.key("return"))     # virtual seconds (see VClock)
    assert first.ok and first.effect == "unverifiable" and first.effect != "confirmed"
    assert "keeps changing by itself" in first.message and "capture before repeating" in first.message
    assert t_first < 1.5 and t_first < scaled.settings["settle_timeout"] / 2      # bounded: far below the timeout
    assert scaled._volatile((128, 96))                                              # the region was learned
    for _ in range(3):                            # the region is re-learned if a tick lands outside it
        second, t_second = timed(lambda: scaled.key("return"))
        if "did not change within" in second.message:
            break
    assert second.effect == "unverifiable" and "did not change within" in second.message
    assert "ignored the" in second.message and t_second < 0.9 and t_second < t_first   # back to the quiet-screen cost
    assert not scaled._volatile((256, 192))                                         # only for that geometry


def test_the_ticking_region_does_not_hide_a_real_change_elsewhere(scaled, fake):
    tick_display(fake, 0.22)
    fake.react_to_input = False
    scaled.key("return")                          # learns the region
    fake.react_to_input = True
    res = scaled.key("return")                    # the whole screen changes
    assert res.effect == "confirmed" and "ignored" in res.message


def test_a_slow_ticking_region_is_bounded_and_never_costs_more_than_one_quiet_period_past_the_blink_window(scaled, fake):
    tick_display(fake, 0.4)                       # the reviewer's 0.8 s ticker, at half scale
    fake.react_to_input = False
    res, took = timed(lambda: scaled.key("return"))
    assert res.ok and took < 0.1 + 0.35 + 0.6 + 0.5      # delay + quiet + blink + slack: well below the 4 s timeout
    assert res.effect in ("confirmed", "unverifiable")  # first sight of a slow ticker cannot be told from a short edit


def test_a_caret_blink_and_typed_characters_are_still_not_mistaken_for_ticking(scaled, fake):
    fake.cursor = (60, 30, 2, 10)
    fake.on_input = type_some_text
    res, took = timed(lambda: scaled.type_text("abc"))
    assert res.effect == "confirmed" and "keeps changing" not in res.message and took < 2.0


def test_ticking_with_the_shipped_defaults_is_bounded_like_before(fake, vclock):
    """Real defaults (0.2/0.7/0.25/1.2) against the reviewer's 0.45 s ticker: the old code took 1.76 s."""
    b = BarehandBackend(settings={"qmp": fake.address})
    tick_display(fake, 0.45)
    fake.react_to_input = False
    res, took = timed(lambda: b.key("return"))
    assert res.effect == "unverifiable" and took < 2.4, (took, res.message)       # old: 1.76 s; new run was 8.36 s
    res, took = timed(lambda: b.key("return"))
    assert took < 1.4, (took, res.message)                                          # old: 1.76 s


def test_wait_honours_the_blink_setting(scaled, fake, monkeypatch):
    seen = []
    real = QMP.watch

    def spy(self, *a, **kw):
        seen.append(kw.get("blink_window"))
        return real(self, *a, **kw)
    monkeypatch.setattr(QMP, "watch", spy)
    scaled.wait(1.0)
    assert seen == [0.6]
    scaled2 = BarehandBackend(settings={"qmp": fake.address, "settle_blink": 0.0})     # invalid: back to the default
    scaled2.wait(1.0)
    assert seen[-1] == 1.2


def test_settle_blink_is_validated(fake, caplog):
    b = BarehandBackend(settings={"qmp": fake.address, "settle_blink": -3, "settle_quiet": "soon"})
    with caplog.at_level("WARNING", logger="barehand.backend"):
        for _ in range(3):
            assert b.settings and backend_mod._float(b.settings, "settle_blink", 1.2, 0.05, 60.0) == 1.2
            assert backend_mod._float(b.settings, "settle_quiet", 0.7) == 0.7
    assert len([r for r in caplog.records if "is invalid" in r.message]) == 2


# --- verdicts: only a screen change attributable to the input is `confirmed` ---------------------------------
def test_a_modifier_only_key_is_never_a_noop_claim(quick, fake):
    fake.react_to_input = False
    res = quick.key("shift")
    assert res.effect == "unverifiable" and res.escalation is None
    assert "screen did not change within" in res.message
    assert "the key may have had no visible effect, or the machine is busy; capture before repeating" in res.message


def test_a_transient_that_goes_back_is_not_confirmed(fake, vclock):
    slow = BarehandBackend(settings={"qmp": fake.address, "settle_quiet": 0.6, "settle_timeout": 2.0,
                                     "settle_delay": 0.02, "settle_poll": 0.03})

    flash_until = []
    real_frame = fake._frame

    def frame():                                     # the flash ends 0.3 virtual seconds after the input
        if flash_until and vclock.now >= flash_until[0]:
            flash_until.clear()
            fake.paint(0, 0, 128, 96, (10, 30, 0))
        return real_frame()
    fake._frame = frame

    def flash(f):
        f.paint(0, 0, 128, 96, (200, 0, 0))
        flash_until.append(vclock.now + 0.3)
    fake.on_input = flash
    res = slow.key("f1")
    assert res.effect == "unverifiable" and "went back to the picture from before" in res.message


def test_a_change_in_a_screen_that_was_already_changing_is_not_confirmed(quick, fake):
    quick.capture()                                  # the model's last look
    fake.fill(120)                                   # the screen then changed on its own, substantially
    res = quick.key("return")                        # and this input changed it again
    assert res.effect == "unverifiable" and "had already changed substantially" in res.message
    assert "cannot be attributed" in res.message
    assert quick.key("return").effect == "confirmed"  # the screen is now what the backend last saw: back to normal


def test_a_quiet_screen_since_the_last_look_leaves_confirmed_alone(quick, fake):
    quick.capture()
    assert quick.key("return").effect == "confirmed"


def test_input_while_the_display_is_not_initialised_is_not_confirmed(quick, fake):
    fake.resize(640, 480)                            # QEMU's two-colour "Display output is not active" banner
    fake.paint(200, 200, 240, 40, (255, 255, 255))
    res = quick.key("return")
    assert res.effect == "unverifiable" and "not initialised" in res.message and "banner" in res.message


def test_a_blank_but_live_display_does_not_block_confirmation(quick, fake):
    res = quick.key("return")                        # the fake's default display is one flat colour
    assert res.effect == "confirmed" and "the display was blank before the input" in res.message


def test_a_resolution_change_during_the_watch_is_not_confirmed(quick, fake):
    quick.capture()
    fake.on_input = lambda f: f.resize(256, 192)
    res = quick.key("return")
    assert res.effect == "unverifiable" and "resolution changed from 128x96 to 256x192" in res.message
