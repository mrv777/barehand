"""The real BarehandBackend / companion / doctor over transport vnc, against the fake RFB server."""

import base64
import json
import time

import pytest

from barehand import companion, config, doctor, transport
from barehand import transport_vnc as tv
from barehand.backend import BarehandBackend
from fake_rfb import FakeRFB


@pytest.fixture
def srv():
    s = FakeRFB(160, 100, ext_keys=True, qemu_like=True)
    yield s
    s.stop()
    tv.shutdown_all()


def settings(s, **kw):
    return {"transport": "vnc", "vnc_host": "127.0.0.1", "vnc_port": s.port, "settle_quiet": 0.15,
            "settle_timeout": 1.0, "settle_delay": 0.02, "settle_poll": 0.03, "record": False, **kw}


def seen(s, kind, n=1):
    end = time.monotonic() + 3
    while len(s.events(kind)) < n and time.monotonic() < end:
        time.sleep(0.02)
    return s.events(kind)


def test_capture_returns_png_over_vnc(srv):
    b = BarehandBackend(settings=settings(srv))
    b.start()
    cap = b.capture("vision")
    assert base64.b64decode(cap.png_b64)[:4] == b"\x89PNG"
    assert (cap.width, cap.height) == (160, 100)
    assert "VNC" in cap.note and "snapshot_save" not in cap.note


def test_key_type_click_reach_the_server(srv):
    b = BarehandBackend(settings=settings(srv))
    r = b.key("ret")
    assert r.ok and r.path == "vnc"
    assert seen(srv, "extkey")
    n0 = len(srv.events("extkey"))
    t = b.type_text("ab")
    assert t.ok and len(seen(srv, "extkey", n0 + 4)) >= n0 + 4
    assert b.click(x=10, y=20).ok
    assert seen(srv, "pointer")
    assert b.wait(0.2).ok


def test_screen_change_is_reported(srv):
    b = BarehandBackend(settings=settings(srv))
    b.capture("vision")
    import threading
    threading.Timer(0.15, lambda: srv.fill(0, 0, 80, 60, (250, 10, 10))).start()
    assert b.key("a").effect in ("confirmed", "unverifiable")


def test_unreachable_server_is_a_clear_failure(srv):
    port = srv.port
    srv.stop()
    tv.shutdown_all()
    b = BarehandBackend(settings=settings(srv, vnc_port=port, vnc_timeout=1))
    r = b.key("a")
    assert not r.ok and r.code == "vnc_unavailable" and r.path == "vnc"
    with pytest.raises(RuntimeError, match="VNC"):
        b.capture()


def test_missing_password_env_is_not_retried(srv, monkeypatch):
    monkeypatch.delenv("NOPE_PW", raising=False)
    r = BarehandBackend(settings=settings(srv, vnc_password_env="NOPE_PW")).key("a")
    assert not r.ok and r.code == "vnc_auth" and "NOPE_PW" in r.message
    assert srv.connections == 0


def test_password_comes_only_from_the_named_env(monkeypatch):
    s = FakeRFB(80, 60, security=(2,), password="secret12")
    try:
        monkeypatch.setenv("MY_VNC_PW", "secret12")
        assert BarehandBackend(settings=settings(s, vnc_password_env="MY_VNC_PW")).key("a").ok
        monkeypatch.setenv("MY_VNC_PW", "wrongpw")
        tv.shutdown_all()
        r = BarehandBackend(settings=settings(s, vnc_password_env="MY_VNC_PW")).key("a")
        assert not r.ok and r.code == "vnc_auth"
    finally:
        s.stop()
        tv.shutdown_all()


def test_config_rules(monkeypatch):
    env = {}
    assert config.resolve({"vnc_host": "127.0.0.1"}, env).kind == "vnc"
    assert config.resolve({"vnc_host": "127.0.0.1", "vnc_port": 5901}, env).endpoint == "127.0.0.1::5901"
    with pytest.raises(config.ConfigError, match="transport"):
        config.resolve({"vnc_host": "127.0.0.1", "qmp": "127.0.0.1:4444"}, env)
    assert config.resolve({"transport": "qmp", "vnc_host": "127.0.0.1", "qmp": "127.0.0.1:4444"}, env).kind == "qmp"
    with pytest.raises(config.ConfigError, match="unencrypted"):
        config.resolve({"transport": "vnc", "vnc_host": "10.1.2.3"}, env)
    assert config.resolve({"transport": "vnc", "vnc_host": "10.1.2.3", "allow_remote": True}, env).kind == "vnc"
    with pytest.raises(config.ConfigError):
        config.resolve({"transport": "serial"}, env)
    assert config.resolve({}, {"BAREHAND_VNC": "127.0.0.1:5900"}).kind == "vnc"


def machine_tool(srv, monkeypatch, args):
    monkeypatch.setattr(config, "hermes_settings", lambda: settings(srv))
    return json.loads(companion.handle(args))


@pytest.mark.parametrize("action", ["snapshot_save", "snapshot_list", "snapshot_load", "reset", "power_off",
                                    "eject_media"])
def test_machine_actions_refuse_over_vnc(srv, monkeypatch, action):
    out = machine_tool(srv, monkeypatch, {"action": action, "name": "x"})
    assert out["ok"] is False and "not available over the VNC transport" in out["error"]


def test_no_approval_prompt_for_impossible_actions(srv, monkeypatch):
    monkeypatch.setattr(config, "hermes_settings", lambda: settings(srv))
    assert companion._approval_hook(tool_name="barehand_machine", args={"action": "reset"}) is None
    assert companion._approval_hook(tool_name="barehand_machine", args={"action": "send_ctrl_alt_del"})


def test_status_ctrl_alt_del_and_text_actions_work(srv, monkeypatch):
    st = machine_tool(srv, monkeypatch, {"action": "status"})
    assert st["ok"] and st["transport"] == "vnc" and st["size"] == [160, 100] and st["running"]
    out = machine_tool(srv, monkeypatch, {"action": "send_ctrl_alt_del"})
    assert out["ok"]
    assert len(seen(srv, "extkey", 6)) >= 6
    out = machine_tool(srv, monkeypatch, {"action": "wait_for_text", "text": "zzz-not-here", "timeout": 1})
    assert "error" in out or out["ok"] is False   # a readable-or-not screen is answered, never a crash
    out = machine_tool(srv, monkeypatch, {"action": "calibrate_text"})
    assert "ok" in out and "error" not in out or out["ok"] is False


def test_doctor_reports_vnc(srv):
    text, code = doctor.report(settings(srv))
    assert code == 0, text
    assert "transport: vnc" in text and "160x100" in text and "raw scancodes" in text


def test_doctor_fails_for_dead_server_and_missing_password(srv, monkeypatch):
    monkeypatch.delenv("NOPE_PW", raising=False)
    text, code = doctor.report(settings(srv, vnc_password_env="NOPE_PW"))
    assert code == 1 and "NOPE_PW" in text
    port = srv.port
    srv.stop()
    text, code = doctor.report(settings(srv, vnc_port=port))
    assert code == 1 and "cannot connect" in text


def test_cli_refuses_machine_verbs_over_vnc(srv, monkeypatch):
    from barehand import cli, machine
    monkeypatch.setattr(config, "hermes_settings", lambda: settings(srv))
    for argv in (["reset"], ["eject"], ["snapshot", "save", "x"], ["up"], ["down"]):
        with pytest.raises(machine.MachineError, match="VNC"):
            cli.run(argv)
    assert '"transport": "vnc"' in cli.run(["status"])


def test_plugin_yaml_declares_every_vnc_setting():
    import pathlib
    text = (pathlib.Path(config.__file__).parent / "plugin.yaml").read_text()
    for key in ("transport", "vnc_host", "vnc_port", "vnc", "vnc_password_env", "vnc_encodings", "vnc_timeout"):
        assert f"\n  {key}:\n" in text
    assert "unencrypted" in text.lower()
