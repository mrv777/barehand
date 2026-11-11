"""Barehand results and hooks as seen by Hermes' own tool layer (imported from the Hermes checkout)."""
import json

import pytest

tool = pytest.importorskip("tools.computer_use.tool")
try:
    import hermes_cli.plugins as plugins
except Exception:  # needs Hermes' full dependency set
    plugins = None

from barehand import companion
from barehand.backend import BarehandBackend


def verdict(res):
    return json.loads(tool._text_response(res))


def test_an_input_that_changed_the_screen_is_done_not_a_recapture_request(backend, fake):
    out = verdict(backend.key("return"))
    assert out["ok"] is True and out["effect"] == "confirmed" and out["verdict"]["decision"] == "done"


def test_a_quiet_screen_after_a_key_is_unverifiable_and_Hermes_says_recapture_not_retry(backend, fake):
    """suspected_noop is never emitted: Hermes' fixed hint for it says the input likely did not land and talks
    about pixels/foreground; for `unverifiable` it says to re-capture BEFORE any retry."""
    fake.react_to_input = False
    out = verdict(backend.key("return"))
    assert out["ok"] is True and out["effect"] == "unverifiable" and "escalation" not in out
    assert out["verdict"]["decision"] == "verify_fresh_state" and "Re-capture" in out["verdict"]["hint"]
    assert "screen did not change within" in out["message"] and "capture before repeating" in out["message"]


def test_typed_text_with_no_visible_effect_stays_unverifiable(backend, fake):
    fake.react_to_input = False   # a no-echo password prompt
    out = verdict(backend.type_text("hunter2"))
    assert out["ok"] is True and out["effect"] == "unverifiable"
    assert out["verdict"]["decision"] == "verify_fresh_state" and "Re-capture" in out["verdict"]["hint"]


def test_unverifiable_still_gets_the_follow_up_capture(backend, fake):
    fake.react_to_input = False
    out = tool._dispatch(backend, "key", {"keys": "return", "capture_after": True})
    assert isinstance(out, dict) and out.get("_multimodal")       # screenshot attached, not suppressed
    assert '"unverifiable"' in out["content"][0]["text"] and "suspected_noop" not in out["content"][0]["text"]


def test_observation_failure_never_produces_an_escalate_verdict(backend, fake):
    backend.capture()
    real = fake._handle
    fake._handle = lambda m: ({"error": {"desc": "no surface"}} if m["execute"] == "screendump" else real(m))
    out = verdict(backend.key("return"))
    assert out["ok"] is True and out["verdict"]["decision"] == "verify_fresh_state"


def test_argument_errors_carry_our_recommendation_and_a_no_foreground_steer(backend):
    out = verdict(backend.key("f13"))
    assert out["ok"] is False and out["verdict"]["decision"] == "escalate"
    assert out["verdict"]["recommended"] == "fix_arguments"       # not 'foreground' / 'px'
    assert "delivery_mode" in out["message"] and "foreground" in out["message"]


def test_hermes_blocks_what_we_would_have_sent_anyway():
    assert tool._reject_unsafe("key", {"keys": "ctrl + alt + delete"}) is not None   # hard-blocked in core
    assert tool._reject_unsafe("key", {"keys": "ctrl+c"}) is None
    assert tool._reject_unsafe("type", {"text": "ls -la"}) is None


@pytest.mark.parametrize("action,approve", [("reset", True), ("snapshot_load", True), ("snapshot_save", False),
                                            ("status", False)])
def test_approval_directive_is_understood_by_hermes(monkeypatch, action, approve):
    if plugins is None:
        pytest.skip("hermes_cli.plugins is not importable here (run with Hermes' own venv)")
    import hermes_cli.lifecycle as lifecycle
    args = {"action": action, "name": "pre-install"}
    monkeypatch.setattr(lifecycle, "invoke_hook",
                        lambda hook, **kw: [companion._approval_hook(tool_name=kw["tool_name"], args=kw["args"])])
    d = plugins._get_pre_tool_call_directive_details("barehand_machine", args)
    assert (d.action == "approve") is approve
    if approve:
        assert d.rule_key == f"barehand:{action}" and "pre-install" in d.message


def test_nothing_under_the_real_hermes_home_is_touched(tmp_path):
    import os
    assert os.environ["HERMES_HOME"].startswith(str(tmp_path))
    from barehand import config
    assert str(config.data_dir()).startswith(str(tmp_path))


def test_capture_after_strips_the_recapture_hint_through_Hermes_follow_up(backend, fake):
    fake.react_to_input = False
    plain = verdict(backend.key("return"))
    assert "capture before repeating" in plain["message"]          # a bare action keeps the hint
    out = tool._dispatch(backend, "key", {"keys": "return", "capture_after": True})
    assert isinstance(out, dict) and out.get("_multimodal")
    text_part = out["content"][0]["text"]
    assert "capture before repeating" not in text_part and "unverifiable" in text_part
