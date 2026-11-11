"""CLI --arch, the `arch` setting, doctor's per-architecture report and the typing-speed defaults."""

import argparse

import pytest

from barehand import cli, config, doctor, machine


def _parser():
    p = argparse.ArgumentParser()
    cli.setup(p)
    return p


def test_up_accepts_arch_choices_and_defaults_to_none():
    p = _parser()
    assert p.parse_args(["up"]).arch is None
    for a in ("auto", "x86_64", "aarch64"):
        assert p.parse_args(["up", "--arch", a]).arch == a
    with pytest.raises(SystemExit):
        p.parse_args(["up", "--arch", "riscv"])


def test_up_passes_arch_through_and_reports_it(monkeypatch):
    seen = {}

    def fake_up(**kw):
        seen.update(kw)
        return {"pid": 7, "accel": "hvf", "qmp": "unix:/q", "arch": "aarch64", "arch_source": "requested"}
    monkeypatch.setattr(machine, "up", fake_up)
    text, code = cli._dispatch(_parser().parse_args(["up", "--arch", "aarch64", "--iso", "x.iso"]))
    assert seen["arch"] == "aarch64" and code == 0
    assert "arch aarch64 (requested)" in text and "accel hvf" in text and "Software emulation" not in text
    monkeypatch.setattr(machine, "up", lambda **kw: {"pid": 7, "accel": "tcg", "qmp": "q", "arch": "aarch64",
                                                     "arch_source": "x"})
    text, _ = cli._dispatch(_parser().parse_args(["up"]))
    assert "Software emulation: slow." in text and "30 s" not in text


def test_up_mismatch_is_a_clean_cli_error(monkeypatch, capsys):
    config.set_machine_arch("default", "x86_64")
    assert cli.handle(_parser().parse_args(["up", "--arch", "aarch64"])) == 1
    err = capsys.readouterr().err
    assert "x86_64 disk" in err and "--fresh" in err


# --- doctor -----------------------------------------------------------------------------------------
def _patch_world(monkeypatch, have):
    """have: arch -> (qemu path or None, firmware tuple)"""
    monkeypatch.setattr(machine, "qemu_for", lambda st, arch: have[arch][0])
    monkeypatch.setattr(machine, "firmware_for", lambda st, q, arch: have[arch][1])
    monkeypatch.setattr(machine, "qemu_version", lambda q: "QEMU emulator version 11.1.3")
    monkeypatch.setattr(machine, "find_qemu_img", lambda q=None: "/bin/qemu-img")
    monkeypatch.setattr(machine, "host_arch", lambda m=None: "aarch64")
    monkeypatch.setattr(machine, "pick_accel", lambda *a, guest="x86_64", **k:
                        ("hvf", "host") if guest == "aarch64" else ("tcg", "max"))
    monkeypatch.setattr(machine, "os_family", lambda *a, **k: "debian")


def test_doctor_lists_usable_arches_and_what_is_missing(monkeypatch):
    _patch_world(monkeypatch, {"aarch64": ("/bin/qa", ("/c", "/v")), "x86_64": (None, (None, None))})
    text, code = doctor.report({"qmp": "unix:/nonexistent"})
    assert "[OK  ] qemu[aarch64]: /bin/qa" in text and "[OK  ] accelerator[aarch64]: hvf" in text
    assert "[WARN] qemu[x86_64]: qemu-system-x86_64 not found; install: apt install qemu-system-x86 ovmf" in text
    assert "[WARN] firmware[x86_64]" in text and "ovmf" in text
    assert "usable on this aarch64 host: aarch64 (hvf)" in text and "not usable: x86_64" in text


def test_doctor_marks_software_emulation(monkeypatch):
    _patch_world(monkeypatch, {"aarch64": ("/bin/qa", ("/c", "/v")), "x86_64": ("/bin/qx", ("/c", "/v"))})
    text, _ = doctor.report({"qmp": "unix:/nonexistent"})
    assert "accelerator[x86_64]: tcg (software emulation, slow)" in text
    assert "aarch64 (hvf), x86_64 (tcg)" in text


def test_doctor_fails_when_no_architecture_is_usable_and_nothing_is_attached(monkeypatch):
    _patch_world(monkeypatch, {"aarch64": (None, (None, None)), "x86_64": ("/bin/qx", ("/c", None))})
    text, code = doctor.report({})
    assert code == 1
    assert "[FAIL] architectures: no guest architecture is usable on this aarch64 host" in text
    assert "qemu-system-aarch64 missing (apt install qemu-system-arm qemu-efi-aarch64)" in text


def test_doctor_only_warns_when_attached_to_a_running_machine(monkeypatch):
    _patch_world(monkeypatch, {"aarch64": (None, (None, None)), "x86_64": (None, (None, None))})
    text, code = doctor.report({"qmp": "unix:/nonexistent"})
    assert "[WARN] architectures: no guest architecture" in text and "[FAIL] qmp" in text


def test_doctor_shows_the_machine_it_manages(monkeypatch):
    _patch_world(monkeypatch, {"aarch64": ("/bin/qa", ("/c", "/v")), "x86_64": (None, (None, None))})
    config.write_state("default", {"name": "default", "pid": 99, "arch": "aarch64", "accel": "hvf",
                                   "qmp": "unix:/x", "workdir": "/w"})
    monkeypatch.setattr(config, "is_our_qemu", lambda s: True)
    text, _ = doctor.report({})
    assert "[OK  ] machine: default: aarch64 on hvf, pid 99" in text


# --- typing defaults --------------------------------------------------------------------------------
def test_typing_defaults_by_arch_and_accel():
    assert config.typing_defaults("aarch64", "hvf") == {"type_hold_ms": 10}
    assert config.typing_defaults("x86_64", "kvm") == {"type_hold_ms": 10}
    assert config.typing_defaults("x86_64", "tcg") == {"type_hold_ms": 15}
    assert config.typing_defaults("aarch64", "tcg") == {"type_hold_ms": 15}
    assert config.typing_defaults(None, None) == {"type_hold_ms": 15}
    assert min(config.TYPE_HOLD_MS.values()) >= config.TYPE_HOLD_FLOOR_MS >= 5


def test_settings_get_the_machines_typing_hold_unless_set_explicitly():
    assert config.with_derived_defaults({})["type_hold_ms"] == 15                 # no machine: fallback
    config.write_state("default", {"arch": "aarch64", "accel": "hvf"})
    assert config.with_derived_defaults({})["type_hold_ms"] == 10
    assert config.with_derived_defaults({"type_hold_ms": 0})["type_hold_ms"] == 10   # 0 = auto (plugin.yaml)
    assert config.with_derived_defaults({"type_hold_ms": ""})["type_hold_ms"] == 10
    assert config.with_derived_defaults({"type_hold_ms": 45})["type_hold_ms"] == 45  # explicit wins
    assert config.with_derived_defaults({"key_hold_ms": 60})["key_hold_ms"] == 60    # nothing else touched
    config.write_state("other", {"arch": "x86_64", "accel": "tcg"})
    assert config.with_derived_defaults({"machine": "other"})["type_hold_ms"] == 15
    assert config.with_derived_defaults({"machine": "bad/name"})["type_hold_ms"] == 15


def test_backend_settings_include_the_automatic_hold():
    from barehand.backend import BarehandBackend
    config.write_state("default", {"arch": "aarch64", "accel": "hvf"})
    assert BarehandBackend().settings["type_hold_ms"] == 10
    assert BarehandBackend(settings={"type_hold_ms": 7}).settings["type_hold_ms"] == 7


def test_doctor_before_any_machine_is_info_not_fail(monkeypatch):
    _patch_world(monkeypatch, {"aarch64": ("/bin/qa", ("/c", "/v")), "x86_64": (None, (None, None))})
    text, code = doctor.report({})
    assert code == 0
    assert "[INFO] endpoint: no machine yet (expected before `hermes barehand up" in text
    assert "[FAIL]" not in text and "all required checks passed" in text


def test_doctor_fails_on_a_nonexistent_barehand_qemu(monkeypatch):
    _patch_world(monkeypatch, {"aarch64": ("/bin/qa", ("/c", "/v")), "x86_64": (None, (None, None))})
    monkeypatch.setenv("BAREHAND_QEMU", "/nonexistent/qemu-system-x86_64")
    text, code = doctor.report({})
    assert code == 1 and "[FAIL] qemu override: BAREHAND_QEMU=/nonexistent/qemu-system-x86_64" in text


def test_snapshot_exists_message_names_the_flag(monkeypatch):
    class Q:
        def __enter__(s): return s
        def __exit__(s, *a): return False
        def hmp(s, c, timeout=0): return "ID TAG\n1 pre 0 B 2020 00:00:00 0 0\n"
    monkeypatch.setattr(machine, "connect", lambda name: Q())
    monkeypatch.setattr(machine, "snapshot_tags", lambda t: ["pre"])
    with pytest.raises(machine.MachineError, match="--overwrite.*overwrite: true"):
        machine.snapshot("save", "pre", "default")
