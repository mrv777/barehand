import argparse
import os

import pytest

from barehand import cli, config, machine


def test_slash_usage_and_safety():
    assert "Usage" in cli.slash("")
    assert "Usage" in cli.slash("up --iso x")      # up/down are terminal-only
    assert "Usage" in cli.slash("down")
    assert "not running" in cli.slash("status") or "running" in cli.slash("status")
    assert "not running" in cli.slash("reset")
    assert "bad arguments" in cli.slash("snapshot explode") or "Usage" in cli.slash("snapshot explode")


def test_parser_shape():
    p = argparse.ArgumentParser()
    cli.setup(p)
    a = p.parse_args(["up", "--iso", "x.iso", "--disk-gb", "20", "--window"])
    assert (a.barehand_cmd, a.iso, a.disk_gb, a.window, a.splash_ms) == ("up", "x.iso", 20, True, None)
    a = p.parse_args(["snapshot", "save", "pre", "--overwrite"])
    assert (a.action, a.tag, a.overwrite) == ("save", "pre", True)
    a = p.parse_args(["reset", "--enter-setup", "--seconds", "20"])
    assert (a.enter_setup, a.seconds) == (True, 20.0)
    assert cli.handle(p.parse_args(["status"])) == 0


def test_existing_commands_and_flags_still_exist():
    p = argparse.ArgumentParser()
    cli.setup(p)
    for argv in (["up", "--iso", "a", "--disk-gb", "1", "--memory-mb", "512", "--cpus", "1", "--window",
                  "--splash-ms", "1", "--fresh"], ["down", "--purge"], ["status"], ["eject"], ["reset"],
                 ["snapshot", "list"], ["doctor"], ["--machine", "m", "status"]):
        p.parse_args(argv)


def test_destructive_options_need_confirmation(monkeypatch):
    p = argparse.ArgumentParser()
    cli.setup(p)
    monkeypatch.setattr("sys.stdin", type("T", (), {"isatty": lambda s: False})())
    with pytest.raises(machine.MachineError, match="--yes"):
        cli._dispatch(p.parse_args(["down", "--purge"]), interactive=True)
    with pytest.raises(machine.MachineError, match="--yes"):
        cli._dispatch(p.parse_args(["up", "--fresh"]), interactive=True)
    assert "nothing to stop" in cli._dispatch(p.parse_args(["down", "--purge", "--yes"]))[0]
    monkeypatch.setenv("BAREHAND_ASSUME_YES", "1")
    assert "nothing to stop" in cli._dispatch(p.parse_args(["down", "--purge"]))[0]


def test_interactive_prompt_must_say_yes(monkeypatch):
    p = argparse.ArgumentParser()
    cli.setup(p)
    monkeypatch.setattr("sys.stdin", type("T", (), {"isatty": lambda s: True})())
    monkeypatch.setattr("builtins.input", lambda prompt: "no")
    with pytest.raises(machine.MachineError, match="cancelled"):
        cli._dispatch(p.parse_args(["down", "--purge"]), interactive=True)
    monkeypatch.setattr("builtins.input", lambda prompt: "yes")
    assert "nothing to stop" in cli._dispatch(p.parse_args(["down", "--purge"]), interactive=True)[0]


def test_doctor_report_is_text_and_sets_the_exit_code(monkeypatch, capsys):
    monkeypatch.setattr(machine, "find_qemu", lambda settings=None: None)
    monkeypatch.setattr(machine, "qemu_for", lambda st, arch: None)
    p = argparse.ArgumentParser()
    cli.setup(p)
    assert cli.handle(p.parse_args(["doctor"])) == 1
    assert "[FAIL] architectures" in capsys.readouterr().out
    assert "[FAIL] architectures" in cli.slash("doctor")      # the slash command returns the report itself


def _p():
    p = argparse.ArgumentParser()
    cli.setup(p)
    return p


def test_machine_flag_works_before_and_after_the_subcommand():
    assert _p().parse_args(["--machine", "X", "up"]).machine == "X"
    assert _p().parse_args(["up", "--machine", "X"]).machine == "X"
    assert _p().parse_args(["snapshot", "list", "--machine", "X"]).machine == "X"
    assert _p().parse_args(["--machine", "X", "up", "--iso", "a"]).machine == "X"   # not reset by the subparser
    assert _p().parse_args(["up"]).machine is None


def test_no_subcommand_prints_help_with_demo_first():
    text, code = cli._dispatch(_p().parse_args([]))
    assert code == 0 and "hermes barehand" in text
    assert text.index("demo") < text.index("doctor") < text.index("up ")


def test_up_hint_only_when_backend_is_not_barehand(monkeypatch):
    monkeypatch.setattr(machine, "up", lambda **kw: {"pid": 1, "arch": "x86_64", "arch_source": "host",
                                                     "accel": "hvf", "qmp": "unix:/x"})
    monkeypatch.setattr(cli, "_backend_is_barehand", lambda: False)
    assert "computer_use.backend: barehand" in cli._dispatch(_p().parse_args(["up"]))[0]
    monkeypatch.setattr(cli, "_backend_is_barehand", lambda: True)
    assert "computer_use.backend" not in cli._dispatch(_p().parse_args(["up"]))[0]


def test_up_rejects_non_iso_files(tmp_path):
    tiny = tmp_path / "t.iso"
    tiny.write_bytes(b"hello")
    with pytest.raises(machine.MachineError, match="not an installer ISO"):
        machine.check_iso_image(str(tiny))
    big = tmp_path / "b.iso"
    big.write_bytes(b"\0" * 200_000)
    with pytest.raises(machine.MachineError, match="CD001"):
        machine.check_iso_image(str(big))
    good = tmp_path / "g.iso"
    data = bytearray(200_000)
    data[0x8001:0x8006] = b"CD001"
    good.write_bytes(bytes(data))
    machine.check_iso_image(str(good))
    with pytest.raises(machine.MachineError, match="CD001"):
        cli._dispatch(_p().parse_args(["up", "--iso", str(big)]))


def test_status_of_a_machine_that_never_existed_says_so():
    assert "no such machine" in machine.status("ghost")["detail"]
    assert "no such machine" in cli._dispatch(_p().parse_args(["status", "--machine", "ghost"]))[0]
