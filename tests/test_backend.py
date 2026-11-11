import base64
import pytest

from barehand import backend as backend_mod
from barehand.backend import BarehandBackend, GUIDE
from barehand.qmp import png_size
from tools.computer_use.backend import ActionResult, CaptureResult

from conftest import at_virtual, on_frames


def keys_sent(fake):
    return [[k["data"] for k in c["arguments"]["keys"]] for c in fake.calls("send-key")]


def test_start_checks_the_machine(backend, fake):
    backend.start()
    assert fake.calls("query-status")


def test_start_without_machine_gives_actionable_error():
    with pytest.raises(RuntimeError, match="hermes barehand up"):
        BarehandBackend(settings={}).start()


def test_start_reports_a_stopped_machine_differently(scratch_home):
    from barehand import config
    config.write_state("default", {"pid": 2 ** 22 + 99, "qmp": "127.0.0.1:1", "workdir": "/w"})
    with pytest.raises(RuntimeError, match="has stopped"):
        BarehandBackend(settings={}).start()


def test_start_unreachable_endpoint(fake):
    fake.stop()
    with pytest.raises(RuntimeError):
        BarehandBackend(settings={"qmp": fake.address}).start()


@pytest.mark.parametrize("mode", ["som", "ax", "vision"])
def test_capture_always_returns_png_and_honest_note(backend, mode):
    cap = backend.capture(mode=mode)
    assert isinstance(cap, CaptureResult)
    png = base64.b64decode(cap.png_b64)
    assert png_size(png) == (cap.width, cap.height) == (128, 96)
    assert cap.mode == "vision"  # "ax" would make the tool drop the image
    assert cap.elements == [] and cap.image_mime_type == "image/png" and cap.png_bytes_len == len(png)
    assert "0 elements by design" in cap.note
    assert (f"mode={mode}" in cap.note) == (mode != "vision")


def test_first_capture_carries_extended_guidance_later_ones_do_not(backend):
    first, second = backend.capture("vision"), backend.capture("vision")
    assert "firmware setup" in first.note and "firmware setup" not in second.note
    # an explicit capture always repeats why screen text is off, so only the guidance part is compared
    assert second.note.startswith(GUIDE) and second.note[len(GUIDE):].strip().startswith("[screen text off")


def test_notes_never_point_the_model_at_the_cli():
    assert "hermes barehand" not in backend_mod.GUIDE + backend_mod.GUIDE_FIRST


def test_capture_ignores_app_with_a_note(backend):
    assert "ignored" in backend.capture(app="Safari").note


def test_capture_failure_raises_runtime_error(backend, fake):
    fake.fail_commands["screendump"] = "no display"
    with pytest.raises(RuntimeError, match="no display"):
        backend.capture()


def test_capture_flags_a_machine_that_is_not_running(backend, fake):
    fake.status = "shutdown"
    note = backend.capture().note
    assert "NOT RUNNING" in note and "powered off" in note


def test_key_sends_qcodes_and_settles(backend, fake):
    res = backend.key("ctrl + alt + f2")
    assert res.ok and res.action == "key" and res.effect == "confirmed" and res.path == "qmp"
    assert "changed, then settled" in res.message
    assert keys_sent(fake) == [["ctrl", "alt", "f2"]]


@pytest.mark.parametrize("keys", [
    "ctrl+alt+delete", "ctrl+alt+del", "ctrl+alt+forwarddelete", "alt+ctrl+forwarddelete", "delete+alt+ctrl",
    "control+option+del", "rctrl+ralt+delete", "ctrl+altgr+del", "lctrl+lalt+forwarddelete", "ctrl-alt-del",
    "a ctrl+alt+forwarddelete", "ctrl+alt+shift+del"])
def test_every_spelling_of_ctrl_alt_delete_is_refused_in_key(backend, fake, keys):
    res = backend.key(keys)
    assert not res.ok and res.code == "bad_key" and "send_ctrl_alt_del" in res.message
    assert not fake.calls("send-key")


def test_ctrl_delete_and_alt_delete_alone_are_allowed(backend, fake):
    assert backend.key("ctrl+delete").ok and backend.key("alt+forwarddelete").ok


def test_key_sequence(backend, fake):
    assert backend.key("esc esc esc").ok
    assert keys_sent(fake) == [["esc"]] * 3


@pytest.mark.parametrize("bad", ["f13", "", "ctrl+hyper"])
def test_unknown_key_is_a_failed_result_with_a_steer(backend, fake, bad):
    res = backend.key(bad)
    assert isinstance(res, ActionResult) and not res.ok and res.code == "bad_key"
    assert "delivery_mode" in res.message and "fix the arguments" in res.message
    assert res.escalation["recommended"] == "fix_arguments"
    assert not fake.calls("send-key")


def test_machine_failures_steer_away_from_foreground_delivery(backend, fake):
    fake.fail_commands["send-key"] = "device busy"
    res = backend.key("return")
    assert not res.ok and res.code == "qmp_error" and "busy" in res.message
    assert "not a delivery-mode problem" in res.message and res.escalation["recommended"] == "recapture"


def test_type_text_maps_every_character(backend, fake):
    res = backend.type_text("Hi 1!\n")
    assert res.ok and res.meta["requested_chars"] == res.meta["delivered_chars"] == 6
    assert keys_sent(fake) == [["shift", "h"], ["i"], ["spc"], ["1"], ["shift", "1"], ["ret"]]


def test_type_text_rejects_unmappable_before_typing_anything(backend, fake):
    res = backend.type_text("ok then ünï")
    assert not res.ok and res.code == "bad_key" and "nothing typed" in res.message
    assert not fake.calls("send-key")


def test_type_text_length_cap(backend, fake):
    res = backend.type_text("a" * (backend_mod.MAX_TYPE_CHARS + 1))
    assert not res.ok and res.code == "type_too_long" and not fake.calls("send-key")


def test_type_text_midway_failure_reports_progress(backend, fake):
    real, state = fake._handle, {"n": 0}
    def flaky(msg):
        if msg["execute"] == "send-key":
            state["n"] += 1
            if state["n"] == 3:
                return {"error": {"desc": "boom"}}
        return real(msg)
    fake._handle = flaky
    res = backend.type_text("abcdef")
    assert not res.ok and res.code == "type_text_incomplete" and res.meta["delivered_chars"] == 2


def test_key_sequence_midway_failure_reports_progress(backend, fake):
    real, state = fake._handle, {"n": 0}
    def flaky(msg):
        if msg["execute"] == "send-key":
            state["n"] += 1
            if state["n"] == 2:
                return {"error": {"desc": "boom"}}
        return real(msg)
    fake._handle = flaky
    res = backend.key("a b c")
    assert not res.ok and res.meta["delivered_keys"] == 1


# --- A1: an observation failure after a delivered input is NOT a failed input ---------------------------
def test_screen_watch_failure_after_delivery_is_still_ok(backend, fake):
    backend.capture()   # the click needs a known screen size first; that part is before delivery
    real = fake._handle
    def die_on_dump(msg):
        if msg["execute"] == "screendump":
            return {"error": {"desc": "display has no surface"}}
        return real(msg)
    fake._handle = die_on_dump
    for res in (backend.key("return"), backend.type_text("hi"), backend.click(x=1, y=1)):
        assert res.ok, res.message
        assert "delivered" in res.message and "do not repeat" in res.message
        assert res.meta["observe_failed"] and res.code is None


def test_connection_lost_during_watch_is_still_ok(backend, fake):
    fake.drop_on, fake.drop_skip = "screendump", 1   # hangs up while we watch (after the pre-input baseline)
    res = backend.key("return")
    assert res.ok and "delivered" in res.message and res.meta["observe_failed"]


def test_click_scales_pixels_to_tablet_range(backend, fake):
    backend.capture()  # the model has seen a 128x96 image
    assert backend.click(x=127, y=95).ok
    abs_events = [e["data"] for e in fake.input_events() if e["type"] == "abs"]
    assert {"axis": "x", "value": 32767} in abs_events and {"axis": "y", "value": 32767} in abs_events
    btns = [(e["data"]["button"], e["data"]["down"]) for e in fake.input_events() if e["type"] == "btn"]
    assert btns == [("left", True), ("left", False)]


def test_click_fetches_size_when_nothing_captured_yet(backend, fake):
    assert backend.click(x=0, y=0).ok
    assert fake.calls("screendump")


# --- A8: clicks keep using the geometry of the image the model saw ----------------------------------------
def test_resolution_change_is_reported_and_clicks_use_the_last_seen_image(backend, fake):
    backend.capture()                       # model saw 128x96
    fake.resize(256, 192)                   # guest switches mode...
    res = backend.key("return")             # ...as a result of this input
    assert res.ok and "resolution changed from 128x96 to 256x192" in res.message
    assert backend._size == (128, 96)       # the model has not been shown the new geometry yet
    backend.click(x=127, y=95)
    xs = [e["data"]["value"] for e in fake.input_events() if e["type"] == "abs" and e["data"]["axis"] == "x"]
    assert xs[-1] == 32767                  # still mapped against 128 wide, not 256
    assert backend.capture().width == 256 and backend._size == (256, 192)


@pytest.mark.parametrize("button,count,expected", [("right", 1, 2), ("middle", 1, 2), ("left", 2, 4)])
def test_buttons_and_double_click(backend, fake, button, count, expected):
    assert backend.click(x=5, y=5, button=button, click_count=count).ok
    btns = [e for e in fake.input_events() if e["type"] == "btn"]
    assert len(btns) == expected and btns[0]["data"]["button"] == button


def test_click_modifiers_wrap_the_click(backend, fake):
    assert backend.click(x=5, y=5, modifiers=["shift"]).ok
    kinds = [(e["type"], e["data"].get("down")) for e in fake.input_events()]
    assert kinds[0] == ("key", True) and kinds[-1] == ("key", False)


def test_click_bad_inputs(backend, fake):
    assert backend.click().code == "missing_coordinate"
    assert backend.click(x=1, y=1, button="side").code == "bad_button"
    assert backend.click(x=1, y=1, modifiers=["fn"]).code == "bad_key"
    assert not fake.input_events()


def test_button_is_released_even_if_the_click_dies_midway(backend, fake):
    real, state = fake._handle, {"btn_down": 0}
    def flaky(msg):
        if msg["execute"] == "input-send-event":
            ev = msg["arguments"]["events"][0]
            if ev["type"] == "btn" and ev["data"]["down"]:
                state["btn_down"] += 1
        if msg["execute"] == "screendump" and state["btn_down"]:
            pass
        return real(msg)
    fake._handle = flaky
    assert backend.click(x=1, y=1).ok
    downs = [e["data"]["down"] for e in fake.input_events() if e["type"] == "btn"]
    assert downs.count(True) == downs.count(False)


def test_element_targeting_is_unsupported_with_a_steer(backend):
    for res in (backend.click(element=3), backend.drag(from_element=1, to_element=2),
                backend.scroll(direction="down", element=1)):
        assert not res.ok and res.code == "unsupported_action" and "element" in res.message
        assert res.escalation["recommended"] == "use_keyboard"


def test_drag_presses_moves_and_releases(backend, fake):
    assert backend.drag(from_xy=(10, 10), to_xy=(100, 80)).ok
    evs = fake.input_events()
    assert [e["data"]["down"] for e in evs if e["type"] == "btn"] == [True, False]
    xs = [e["data"]["value"] for e in evs if e["type"] == "abs" and e["data"]["axis"] == "x"]
    assert xs[0] < xs[-1] and len(xs) > 5
    assert backend.drag(from_xy=(1, 1)).code == "missing_coordinate"


def test_scroll(backend, fake):
    assert backend.scroll(direction="down", amount=3, x=20, y=20).ok
    names = [e["data"]["button"] for e in fake.input_events() if e["type"] == "btn" and e["data"]["down"]]
    assert names == ["wheel-down"] * 3
    assert backend.scroll(direction="sideways").code == "bad_direction"
    assert backend.scroll(direction="up", x=3).code == "missing_coordinate"


def test_apps_windows_focus_set_value(backend):
    assert backend.list_apps() == [] and backend.list_windows() == []
    for res in (backend.focus_app("x"), backend.set_value("v"), backend.set_value("v", element=2)):
        assert not res.ok and res.code == "unsupported_action"


def test_never_raises_when_the_machine_goes_away(backend, fake):
    fake.stop()
    for res in (backend.key("return"), backend.type_text("a"), backend.click(x=1, y=1),
                backend.drag(from_xy=(0, 0), to_xy=(1, 1)), backend.scroll(direction="up")):
        assert isinstance(res, ActionResult) and not res.ok and res.code == "qmp_unavailable"


def test_unconfigured_machine_never_raises():
    b = BarehandBackend(settings={})
    for res in (b.key("a"), b.type_text("a"), b.click(x=1, y=1), b.wait(0.1)):
        assert not res.ok and res.code == "no_machine"


# --- A7: input to a VM that is not running --------------------------------------------------------------
@pytest.mark.parametrize("status", ["paused", "shutdown", "prelaunch", "guest-panicked"])
def test_input_to_a_stopped_vm_fails_loudly_and_sends_nothing(backend, fake, status):
    fake.status = status
    res = backend.key("return")
    assert not res.ok and res.code == "vm_not_running" and status in res.message
    assert res.escalation["recommended"] == "reset_machine"
    assert not fake.calls("send-key")
    assert "reset" in res.message if status == "shutdown" else True


def test_suspended_vm_still_accepts_input(backend, fake):
    fake.status = "suspended"
    assert backend.key("return").ok


# --- A3: settling and waiting ---------------------------------------------------------------------------
def test_animated_screen_reports_not_settled(backend, fake):
    fake.animated = True
    res = backend.key("return")
    assert res.ok and "still changing" in res.message


def test_quiet_screen_after_input_is_reported_as_settled(backend, fake):
    res = backend.key("return")
    assert res.ok and "changed, then settled" in res.message
    fake.react_to_input = False
    assert "did not change within" in backend.key("return").message


def test_tall_blinking_cursor_does_not_burn_the_settle_timeout(backend, fake, vclock):
    fake.resize(640, 480)
    fake.cursor = (10, 10, 12, 40)     # 40 px tall: beyond the old 20-row allowance
    fake.react_to_input = False
    start = vclock.monotonic()
    res = backend.key("return")
    assert res.ok and "still changing" not in res.message
    assert vclock.monotonic() - start < 0.9  # settle_timeout is 1.0 s in the fixture


def test_a_thin_one_row_change_counts_as_activity(backend, fake, vclock):
    fake.react_to_input = False
    n = []

    def progress():                    # a one-pixel-high bar that keeps moving for the whole settle window
        i = len(n)
        n.append(i)
        fake.paint(0, i % 90, 20 + i % 90, 1, (255, 0, i % 250))
    on_frames(fake, progress)
    res = backend.key("return")
    assert res.ok and "still changing" in res.message and res.effect == "confirmed"


def test_wait_waits_the_full_time_when_nothing_changes(backend, fake, vclock):
    start = vclock.monotonic()
    res = backend.wait(1.2)
    assert res.ok and res.action == "wait" and 1.1 <= vclock.monotonic() - start < 3
    assert "waited the full" in res.message and "no visible change" in res.message and res.meta["early"] is False


def test_wait_with_a_blinking_cursor_still_says_no_change(backend, fake, vclock):
    fake.resize(640, 480)
    fake.cursor = (3, 3, 10, 30)
    res = backend.wait(1.0)
    assert "no visible change" in res.message


def test_wait_sees_thin_changes(backend, fake, vclock):
    at_virtual(fake, vclock, 0.3, lambda: fake.paint(0, 50, 100, 1, (200, 0, 0)))
    res = backend.wait(1.5)
    assert res.meta["real_changes"] >= 1 and "no visible change" not in res.message


def test_wait_returns_early_only_after_change_then_a_long_still_period(backend, fake, monkeypatch, vclock):
    monkeypatch.setattr(backend_mod.BarehandBackend, "wait", backend_mod.BarehandBackend.wait)
    at_virtual(fake, vclock, 0.3, lambda: fake.fill(200))
    start = vclock.monotonic()
    res = backend.wait(30)
    elapsed = vclock.monotonic() - start
    assert res.meta["early"] is True and 5 <= elapsed < 9      # early_quiet = max(5 s, 10%)
    assert "Still does not mean the operation is finished" in res.message


def test_wait_does_not_return_early_on_short_pauses(backend, fake, vclock):
    at_virtual(fake, vclock, 0.2, lambda: fake.fill(200))
    start = vclock.monotonic()
    res = backend.wait(3)                 # 3 s < the 5 s quiet period: must wait it out
    assert vclock.monotonic() - start >= 2.9 and res.meta["early"] is False


def test_wait_survives_a_resolution_change(backend, fake, vclock):
    at_virtual(fake, vclock, 0.3, lambda: fake.resize(64, 48))
    res = backend.wait(1.5)
    assert res.ok and res.meta["real_changes"] >= 1


def test_wait_cap_is_thirty_seconds(backend, monkeypatch):
    seen = {}
    class Q:
        def watch(self, duration, **kw):
            seen["duration"] = duration
            from barehand.qmp import Observation
            return Observation(duration, 0, False, None, duration, None, (1, 1), True)
        def close(self): pass
        def status(self): return "running"
    monkeypatch.setattr(BarehandBackend, "_qmp", lambda self: __import__("contextlib").nullcontext(Q()))
    backend.wait(500)
    assert seen["duration"] == 30.0


def test_connection_is_released_between_actions(backend, fake):
    backend.key("a")
    backend.key("b")
    assert fake.connections >= 2  # per-operation sessions: the CLI can interleave


def test_stop_is_harmless_and_never_quits_the_machine(backend, fake):
    backend.start()
    backend.stop()
    assert not fake.calls("quit")


def test_busy_machine_is_reported_as_busy_not_as_missing(fake):
    from barehand import qmp
    holder = qmp.QMP(fake.address)       # occupies the single client slot
    try:
        b = BarehandBackend(settings={"qmp": fake.address})
        import socket
        orig = qmp.QMP.__init__
        def quick(self, address, timeout=10.0):
            orig(self, address, timeout=0.5)
        qmp.QMP.__init__ = quick
        try:
            res = b.key("a")
        finally:
            qmp.QMP.__init__ = orig
        assert not res.ok and res.code == "qmp_busy" and "busy" in res.message
    finally:
        holder.close()


def test_signature_matches_abc():
    import inspect
    from tools.computer_use.backend import ComputerUseBackend
    for name in ("capture", "click", "drag", "scroll", "type_text", "key", "list_apps", "focus_app",
                 "set_value", "wait", "start", "stop", "is_available"):
        want = list(inspect.signature(getattr(ComputerUseBackend, name)).parameters)
        got = list(inspect.signature(getattr(BarehandBackend, name)).parameters)
        assert got == want, (name, got, want)
