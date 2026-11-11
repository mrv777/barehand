import json
import os

import pytest

from barehand import config


def test_data_dir_follows_override(scratch_home):
    assert config.data_dir() == scratch_home / "bh-home"
    assert config.data_dir().is_dir()


@pytest.fixture
def ours(monkeypatch):
    """Pretend any live pid in a state file is the QEMU we started."""
    monkeypatch.setattr(config, "is_our_qemu", lambda state: bool(state) and config.pid_alive(state.get("pid")))


def test_env_beats_settings_beats_state(scratch_home, ours):
    config.write_state("default", {"pid": os.getpid(), "qmp": "127.0.0.1:3"})
    assert config.resolve({}, env={}).source == "state"
    r = config.resolve({"qmp": "127.0.0.1:2"}, env={})
    assert (r.endpoint, r.source) == ("127.0.0.1:2", "config")
    r = config.resolve({"qmp": "127.0.0.1:2"}, env={"BAREHAND_QMP": "127.0.0.1:1"})
    assert (r.endpoint, r.source) == ("127.0.0.1:1", "env")


def test_state_ignored_when_process_is_gone(scratch_home, ours):
    config.write_state("default", {"pid": 2 ** 22 + 12345, "qmp": "127.0.0.1:3"})
    assert config.resolve({}, env={}).endpoint is None


def test_nothing_configured():
    r = config.resolve({}, env={})
    assert r.endpoint is None and r.machine == "default"
    assert config.endpoint_configured({}) is False


def test_remote_hosts_rejected_unless_opted_in():
    with pytest.raises(config.ConfigError, match="loopback"):
        config.resolve({"qmp": "192.168.1.5:4444"}, env={})
    assert config.resolve({"qmp": "192.168.1.5:4444", "allow_remote": True}, env={}).endpoint
    for ok in ("127.0.0.1:1", "localhost:1", "[::1]:1", "unix:/tmp/x.sock"):
        assert config.resolve({"qmp": ok}, env={}).endpoint == ok


def test_bad_address():
    with pytest.raises(config.ConfigError):
        config.resolve({"qmp": "no-port"}, env={})


def test_machine_name_selects_state_file(ours):
    config.write_state("lab", {"pid": os.getpid(), "qmp": "127.0.0.1:9"})
    r = config.resolve({"machine": "lab"}, env={})
    assert (r.machine, r.endpoint) == ("lab", "127.0.0.1:9")
    assert config.resolve({}, env={"BAREHAND_MACHINE": "lab"}).endpoint == "127.0.0.1:9"


@pytest.mark.parametrize("name", ["../x", "a/b", ".hidden", ""])
def test_machine_names_cannot_escape(name):
    with pytest.raises(config.ConfigError):
        config.machine_dir(name)


def test_state_roundtrip_is_atomic_json(scratch_home):
    config.write_state("default", {"a": 1})
    assert json.loads(config.state_path().read_text()) == {"a": 1}
    config.clear_state()
    assert config.read_state() is None


def test_state_with_a_reused_pid_is_not_our_machine(scratch_home):
    """A pid that is alive but is some other program (stale state after a crash/reboot) is not trusted."""
    config.write_state("default", {"pid": os.getpid(), "qmp": "127.0.0.1:3", "workdir": "/w/default"})
    assert config.pid_alive(os.getpid()) and config.is_our_qemu(config.read_state()) is False
    assert config.resolve({}, env={}).endpoint is None


def test_is_our_qemu_matches_command_line(monkeypatch):
    monkeypatch.setattr(config, "pid_alive", lambda pid: True)
    monkeypatch.setattr(config, "process_command", lambda pid: "/usr/bin/qemu-system-x86_64 -name barehand -drive file=/w/default/disk.qcow2")
    assert config.is_our_qemu({"pid": 5, "workdir": "/w/default"})
    assert not config.is_our_qemu({"pid": 5, "workdir": "/w/other"})
    monkeypatch.setattr(config, "process_command", lambda pid: "/usr/bin/vim notes /w/default")
    assert not config.is_our_qemu({"pid": 5, "workdir": "/w/default"})
    assert not config.is_our_qemu(None) and not config.is_our_qemu({"pid": None})


def test_pid_alive_never_signals_on_windows(monkeypatch):
    """os.kill(pid, 0) terminates a process on Windows; v0 must not call it there."""
    called = []
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(os, "kill", lambda *a: called.append(a))
    assert config.pid_alive(os.getpid()) is False and not called


@pytest.mark.parametrize("addr", ["127.0.0.1:0", "127.0.0.1:99999", "localhost:65536"])
def test_port_range_is_checked(addr):
    with pytest.raises(config.ConfigError, match="out of range"):
        config.resolve({"qmp": addr}, env={})


def test_invalid_machine_name_is_reported_not_swallowed():
    with pytest.raises(config.ConfigError):
        config.resolve({"machine": "../x"}, env={})
