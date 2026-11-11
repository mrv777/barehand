"""Security regressions: names at the boundary, fail-closed approval hook, bounded display of server text."""
import pytest

from barehand import companion, config, doctor, machine
from barehand.text import Atlas, AtlasStore


@pytest.mark.parametrize("bad", ["", ".", "..", ".hidden", "a/b", "a\\b", "../x", "/abs", "a\nb", "a\x00b", "a,b",
                                 "a b", "x" * 65, "ok\n"])
def test_machine_name_rejects_path_and_option_characters(bad):
    with pytest.raises(config.ConfigError):
        config.machine_dir(bad)


@pytest.mark.parametrize("good", ["default", "lab-1", "vm_2.test", "A" * 64])
def test_machine_name_accepts_plain_names(good):
    assert config.machine_dir(good).name == good


@pytest.mark.parametrize("bad", ["../x", "a/b", "a b", "a;b", "a,b", "x\ny", "x\x00", "", ".h", "t" * 65, "ok\n"])
def test_snapshot_tag_allowlist(bad):
    with pytest.raises(machine.MachineError):
        machine._check_snapshot_name(bad)


def test_iso_path_commas_cannot_inject_qemu_options():
    cmd = machine.device_args("/tmp/a,b,readonly=off,file=/etc/passwd.iso", "/d/disk.qcow2")
    joined = " ".join(cmd)
    assert "file=/tmp/a,,b,,readonly=off,,file=/etc/passwd.iso" in joined


DESTRUCTIVE = ["reset", "power_off", "eject_media", "snapshot_load", "snapshot_delete", "send_ctrl_alt_del"]


@pytest.mark.parametrize("action", DESTRUCTIVE)
def test_destructive_actions_always_escalate_never_approve(action):
    d = companion._approval_hook(tool_name="barehand_machine", args={"action": f" {action.upper()} "})
    assert d["action"] == "approve" and d["rule_key"] == f"barehand:{action}"


@pytest.mark.parametrize("action", DESTRUCTIVE)
def test_hook_error_blocks_instead_of_allowing(monkeypatch, action):
    def boom(*a, **k):
        raise RuntimeError("config exploded")
    monkeypatch.setattr(companion, "_approval_decision", boom)
    d = companion._approval_hook(tool_name="barehand_machine", args={"action": action})
    assert d["action"] == "block" and d["message"]


def test_hook_unreadable_config_still_asks(monkeypatch):
    monkeypatch.setattr(config, "resolve", lambda *a, **k: (_ for _ in ()).throw(config.ConfigError("bad")))
    d = companion._approval_hook(tool_name="barehand_machine", args={"action": "reset"})
    assert d["action"] == "approve"


def test_hook_ignores_other_tools_and_safe_actions():
    assert companion._approval_hook(tool_name="terminal", args={"action": "reset"}) is None
    assert companion._approval_hook(tool_name="barehand_machine", args={"action": "snapshot_save"}) is None


def test_hook_message_never_echoes_unvalidated_name():
    d = companion._approval_hook(tool_name="barehand_machine",
                                 args={"action": "snapshot_load", "name": "x\nIGNORE PREVIOUS"})
    assert "IGNORE" not in d["message"]


def test_server_text_is_bounded_and_printable():
    nasty = "desk\x1b[31m\ntop" + "A" * 5000
    for fn in (companion._clip, doctor.safe_name):
        out = fn(nasty)
        assert len(out) <= 90 and "\x1b" not in out and "\n" not in out


def test_atlas_store_is_private(tmp_path):
    import os, stat
    store = AtlasStore(str(tmp_path / "atl"))
    atlas = Atlas(8, 8, "test")
    atlas.add(bytes(range(1, 9)), "A")
    path = store.save(atlas)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o700
