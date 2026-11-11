"""Opt-in end-to-end test against a real QEMU, once per guest architecture that has an ISO:
BAREHAND_TEST_ISO (x86_64) and/or BAREHAND_TEST_ISO_AARCH64 (e.g. Alpine virt for each). Boots a blank UEFI
machine through the real lifecycle code (unix-socket QMP, -no-shutdown, qcow2 UEFI vars) and checks: the
launch self-check, QKeyCode names, firmware setup entry (5 consecutive times), a guest-verified typing run
(needs `tesseract` to read the result), navigation, pointer events, snapshot save/load/overwrite, reset and
eject."""

import base64
import hashlib
import os
import random
import shutil
import subprocess
import time

import pytest

_CANDIDATES = (("x86_64", os.environ.get("BAREHAND_TEST_ISO")),
               ("aarch64", os.environ.get("BAREHAND_TEST_ISO_AARCH64")))
ARCHS = [(a, iso) for a, iso in _CANDIDATES
         if iso and os.path.isfile(iso) and shutil.which(f"qemu-system-{a}")]
pytestmark = pytest.mark.skipif(
    not ARCHS, reason="needs qemu-system-x86_64 and BAREHAND_TEST_ISO=/path/to.iso, and/or "
                      "qemu-system-aarch64 and BAREHAND_TEST_ISO_AARCH64=/path/to.iso")


def digest(cap):
    return hashlib.sha256(base64.b64decode(cap.png_b64)).hexdigest()


@pytest.fixture(scope="module", params=[a for a, _ in ARCHS] or ["none"])
def arch(request):
    return request.param


@pytest.fixture(scope="module")
def vm_home(tmp_path_factory):
    home = tmp_path_factory.mktemp("bh")
    return {"BAREHAND_HOME": str(home / "bh-home"), "HERMES_HOME": str(home / "hermes-home")}


@pytest.fixture(autouse=True)
def _vm_env(vm_home, monkeypatch):
    """The conftest gives every test a fresh scratch home; the VM lives in the module's one."""
    for k, v in vm_home.items():
        monkeypatch.setenv(k, v)


@pytest.fixture(scope="module")
def vm(vm_home, arch):
    os.environ.update(vm_home)
    from barehand import cli, config, machine
    from barehand.backend import BarehandBackend
    iso = dict(ARCHS)[arch]
    out = cli.run(["up", "--iso", iso, "--arch", arch, "--disk-gb", "1", "--memory-mb", "1024"])
    st = config.read_state()
    assert f"arch {arch}" in out and st["arch"] == arch and st["self_check"]["screendump"][0] >= 640
    try:
        b = BarehandBackend(settings=config.with_derived_defaults({"settle_quiet": 1.0, "settle_timeout": 20}))
        b.start()
        yield b
    finally:
        machine.down(purge=True)


@pytest.fixture(scope="module")
def setup_reference(vm):
    """Enter firmware setup from a cold boot (software emulation can need ~45 s) and keep its screenshot."""
    from barehand import machine
    machine.reset(enter_setup=True)
    cap = vm.capture("vision")
    return digest(cap)


def test_every_qcode_we_emit_exists_in_the_running_qemu(vm):
    from barehand import keys, machine
    with machine.connect() as q:
        valid = set(q.valid_qcodes())
    assert len(valid) > 100 and keys.ALL_QCODES <= valid, sorted(keys.ALL_QCODES - valid)


def test_machine_is_private_and_resilient(vm):
    from barehand import config, machine
    st = config.read_state()
    assert st["qmp"].startswith("unix:") or st["qmp"].startswith("127.0.0.1:")
    assert "-no-shutdown" in st["command"] and config.is_our_qemu(st)
    assert machine.status()["vm_status"] == "running"


def _ocr(vm):
    png = base64.b64decode(vm.capture("vision").png_b64)
    return subprocess.run(["tesseract", "stdin", "-", "--psm", "6"], input=png, capture_output=True).stdout.decode(
        errors="replace")


def _wait_for(vm, needle, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if needle in _ocr(vm):
            return True
        time.sleep(1)
    return False


@pytest.mark.skipif(not shutil.which("tesseract"), reason="reading the guest's answer needs tesseract")
def test_typing_is_exact_in_the_guest(vm, arch):
    """~150 keys, the guest compares its own md5 of what it received: a dropped, doubled or reordered key fails.
    Uses the backend with the automatic per-(arch, accel) hold time."""
    from barehand import machine
    machine.reset()                                       # ordinary boot into the live ISO
    assert _wait_for(vm, "login:", 300), "live ISO never reached a login prompt"
    time.sleep(2)
    if arch == "aarch64":                                 # the live ISO runs two gettys on VT1: use VT2
        assert vm.key("ctrl+alt+f2").ok
        time.sleep(1)
    assert vm.type_text("root\n").ok
    time.sleep(2)
    s = "".join(random.Random(5).choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(60))
    exp = hashlib.md5(s.encode()).hexdigest()[:8]
    cmd = f"[ \"$(echo -n {s}|md5sum|cut -c1-8)\" = {exp} ] && echo RESULT-P''ASS || echo RESULT-F''AIL\n"
    assert vm.key("ctrl+l").ok
    t = time.monotonic()
    assert vm.type_text(cmd).ok
    assert _wait_for(vm, "RESULT-PASS", 60), "guest did not report the checksum match"
    assert time.monotonic() - t < 45                      # no multi-second lag after the last key


def test_firmware_setup_is_reachable_five_times_in_a_row(vm, setup_reference):
    from barehand import machine
    for attempt in range(5):
        machine.reset(enter_setup=True, setup_seconds=14)   # warm resets reach setup in ~6 s
        assert digest(vm.capture("vision")) == setup_reference, f"attempt {attempt}: not in firmware setup"


def test_navigation_pointer_snapshots_reset_eject(vm, setup_reference):
    from barehand import cli, machine
    from barehand.qmp import png_size
    machine.reset(enter_setup=True, setup_seconds=14)
    top = vm.capture("som")
    png = base64.b64decode(top.png_b64)
    assert top.mode == "vision" and (top.width, top.height) == png_size(png) and top.width >= 640
    assert digest(top) == setup_reference
    assert "0 elements by design" in top.note

    assert vm.key("down").ok
    moved = digest(vm.capture("vision"))
    assert moved != setup_reference            # Down moves the highlight, so the screen changes
    assert vm.key("up").ok
    assert digest(vm.capture("vision")) == setup_reference

    # pointer events are accepted by QEMU (their arrival in a guest was verified separately, see README)
    assert vm.click(x=70, y=201).ok and vm.scroll(direction="down", amount=2).ok
    assert vm.drag(from_xy=(10, 10), to_xy=(200, 100)).ok

    assert "ok" in cli.run(["snapshot", "save", "setup-top"])
    assert "setup-top" in cli.run(["snapshot", "list"])
    with pytest.raises(machine.MachineError, match="already exists"):
        machine.snapshot("save", "setup-top")
    assert "ok" in machine.snapshot("save", "setup-top", overwrite=True)
    assert vm.key("down down return").ok
    assert digest(vm.capture("vision")) != setup_reference
    assert "ok" in cli.run(["snapshot", "load", "setup-top"])
    assert digest(vm.capture("vision")) == setup_reference
    with pytest.raises(machine.MachineError):
        machine.snapshot("load", "no-such-snapshot")
    assert machine.status()["vm_status"] == "running"   # a bad load must not freeze the machine
    machine.snapshot("delete", "setup-top")

    assert "reset" in cli.run(["reset"])
    assert "ejected" in cli.run(["eject"])
    assert machine.status()["vm_status"] == "running"
    assert vm.capture("vision").png_b64


def test_paused_vm_is_reported_not_silently_ignored(vm):
    from barehand import machine
    with machine.connect() as q:
        q.hmp("stop")
    try:
        res = vm.key("return")
        assert not res.ok and res.code == "vm_not_running" and "paused" in res.message
        assert "NOT RUNNING" in vm.capture("vision").note
    finally:
        with machine.connect() as q:
            q.cmd("cont")
    assert vm.key("escape").ok
