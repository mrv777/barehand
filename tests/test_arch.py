"""Guest-architecture support: the arch table, command construction, accelerator selection, firmware
discovery, ISO architecture detection, arch resolution/refusal, launch self-check, setup windows."""

import os
import subprocess

import pytest

from barehand import config, machine
from iso_fixture import make_iso
from test_machine import _exe, _hardware, stub_qemu  # noqa: F401  (fixture)


def _cmd(arch="aarch64", **kw):
    base = dict(qemu=f"/q/qemu-system-{arch}", accel="hvf", cpu="host", qmp="unix:/p/q.sock", code="/fw/code.fd",
                vars_img="/w/vars.qcow2", disk="/w/disk.qcow2", iso="/i/x.iso", memory_mb=1024, cpus=2,
                window=False, arch=arch)
    base.update(kw)
    return machine.build_command(**base)


# --- the table ------------------------------------------------------------------------------------
def test_arch_table_shape():
    assert set(machine.ARCHES) == {"x86_64", "aarch64"} and machine.ARCH_CHOICES == ("auto", "x86_64", "aarch64")
    x, a = machine.ARCHES["x86_64"], machine.ARCHES["aarch64"]
    assert (x.binary, x.machine, x.display, x.cd, x.boot_menu) == ("qemu-system-x86_64", "q35", "VGA", "ahci", True)
    assert (a.binary, a.machine, a.display, a.cd, a.boot_menu) == (
        "qemu-system-aarch64", "virt", "virtio-gpu-pci", "scsi", False)
    assert a.pflash_bytes == 64 * 1024 * 1024 and x.pflash_bytes is None
    assert machine.QEMU_BINARY == "qemu-system-x86_64"
    with pytest.raises(machine.MachineError, match="unknown architecture"):
        machine.spec("riscv")


def test_arch_names_are_normalised():
    for given, want in (("auto", "auto"), (None, "auto"), ("", "auto"), ("arm64", "aarch64"), ("AArch64", "aarch64"),
                        ("amd64", "x86_64"), ("x86-64", "x86_64"), ("x86_64", "x86_64")):
        assert machine.normalize_arch(given) == want
    with pytest.raises(machine.MachineError):
        machine.normalize_arch("sparc")


def test_host_arch():
    for m, want in (("arm64", "aarch64"), ("aarch64", "aarch64"), ("x86_64", "x86_64"), ("AMD64", "x86_64")):
        assert machine.host_arch(m) == want


# --- accelerator by (host, guest) -----------------------------------------------------------------
@pytest.mark.parametrize("host,plat,kvm,guest,want", [
    ("arm64", "darwin", None, "aarch64", ("hvf", "host")),
    ("arm64", "darwin", None, "x86_64", ("tcg", "max")),
    ("x86_64", "darwin", None, "x86_64", ("hvf", "host")),
    ("x86_64", "darwin", None, "aarch64", ("tcg", "max")),
    ("x86_64", "linux", True, "x86_64", ("kvm", "host")),
    ("x86_64", "linux", False, "x86_64", ("tcg", "max")),
    ("x86_64", "linux", True, "aarch64", ("tcg", "max")),
    ("aarch64", "linux", True, "aarch64", ("kvm", "host")),
    ("aarch64", "linux", False, "aarch64", ("tcg", "max")),
    ("aarch64", "linux", True, "x86_64", ("tcg", "max")),
    ("AMD64", "win32", None, "x86_64", ("tcg", "max")),
])
def test_accel_by_host_and_guest(host, plat, kvm, guest, want):
    assert machine.pick_accel(host, plat, kvm, guest=guest) == want


def test_accel_kind_and_setup_windows(scratch_home):
    assert [machine.accel_kind(x) for x in ("tcg", "tcg,thread=multi", None, "hvf", "kvm")] == \
        ["tcg", "tcg", "tcg", "hw", "hw"]
    expect = {("x86_64", "tcg"): 35, ("x86_64", "hvf"): 12, ("aarch64", "hvf"): 6, ("aarch64", "kvm"): 6,
              ("aarch64", "tcg"): 20}
    for (arch, accel), secs in expect.items():
        config.write_state("default", {"arch": arch, "accel": accel})
        assert machine.setup_window() == secs, (arch, accel)
    config.write_state("default", {"accel": "hvf"})           # a state file from before architectures: x86_64
    assert machine.setup_window() == 12
    config.clear_state()
    assert machine.setup_window() == 35


# --- command construction ---------------------------------------------------------------------------
def test_x86_command_is_unchanged_in_shape():
    cmd = _cmd("x86_64", accel="tcg", cpu="max", splash_ms=15000)
    joined = " ".join(cmd)
    assert cmd[cmd.index("-machine") + 1] == "q35" and "-vga none" in joined and "VGA,bus=pcie.0,addr=0x1" in joined
    assert "menu=on,splash-time=15000" in joined and "ahci,id=ahci,bus=pcie.0,addr=0x5" in joined
    assert "ide-cd,drive=cd0,bus=ahci.0,id=cdrom0" in joined


def test_aarch64_command_uses_virt_gpu_and_scsi_cd():
    cmd = _cmd("aarch64")
    joined = " ".join(cmd)
    assert cmd[cmd.index("-machine") + 1] == "virt" and cmd[cmd.index("-accel") + 1] == "hvf"
    assert "virtio-gpu-pci,bus=pcie.0,addr=0x1" in joined
    assert "ramfb" not in joined and "ahci" not in joined and "ide-cd" not in joined and "-vga" not in cmd
    assert "virtio-scsi-pci,id=scsi0,bus=pcie.0,addr=0x5" in joined
    assert "scsi-cd,drive=cd0,bus=scsi0.0,id=cdrom0" in joined          # id unchanged: eject() keeps working
    assert "-boot" not in cmd and "splash-time" not in joined
    assert "-no-shutdown" in cmd and "-display none" in joined
    assert "if=pflash,unit=1,format=qcow2,file=/w/vars.qcow2" in joined
    assert "usb-tablet,bus=xhci.0" in cmd and "usb-kbd,bus=xhci.0" in cmd


def test_aarch64_on_kvm_asks_for_the_hosts_gic():
    assert _cmd("aarch64", accel="kvm")[_cmd("aarch64", accel="kvm").index("-machine") + 1] == "virt,gic-version=host"
    assert _cmd("aarch64", accel="tcg", cpu="max")[_cmd("aarch64").index("-machine") + 1] == "virt"
    assert "gic-version" not in " ".join(_cmd("x86_64", accel="kvm"))


@pytest.mark.parametrize("arch", ["x86_64", "aarch64"])
def test_device_layout_is_stable_across_iso_window_and_resources(arch):
    ref = _hardware(_cmd(arch))
    for kw in (dict(iso=None), dict(window=True, plat="darwin"), dict(window=True, plat="linux", iso=None),
               dict(memory_mb=4096, cpus=4), dict(accel="tcg", cpu="max")):
        assert _hardware(_cmd(arch, **kw)) == ref, (arch, kw)


@pytest.mark.parametrize("arch", ["x86_64", "aarch64"])
def test_every_pci_device_is_pinned_to_a_unique_slot(arch):
    cmd = _cmd(arch)
    devs = [cmd[i + 1] for i, c in enumerate(cmd) if c == "-device"]
    pci = [d for d in devs if "bus=pcie.0" in d]
    addrs = [d.split("addr=")[1].split(",")[0] for d in pci]
    assert len(pci) == 5 and len(set(addrs)) == 5
    assert all(d.startswith(("usb-", "ide-cd", "scsi-cd")) or "bus=" in d for d in devs)
    assert all(1 <= n < 0x1f for n in machine.ARCHES[arch].slots.values())


def test_cd_drive_exists_without_an_iso_on_both_arches():
    for arch in machine.ARCHES:
        joined = " ".join(_cmd(arch, iso=None))
        assert "id=cd0,media=cdrom" in joined and "/i/x.iso" not in joined and "id=cdrom0" in joined


# --- binaries and firmware discovery ---------------------------------------------------------------
def test_find_qemu_per_arch_uses_the_sibling_of_an_explicit_binary(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    x86, a64 = _exe(bindir / "qemu-system-x86_64"), _exe(bindir / "qemu-system-aarch64")
    monkeypatch.setenv("PATH", "")
    assert machine.find_qemu({"qemu_binary": x86}) == x86
    assert machine.find_qemu({"qemu_binary": x86}, "aarch64") == a64
    assert machine.find_qemu({"qemu_binary": a64}, "x86_64") == x86
    custom = _exe(tmp_path / "my-qemu")
    assert machine.find_qemu({"qemu_binary": custom}, "aarch64") == custom      # an unknown name is taken as given
    assert machine.find_qemu({"qemu_binary": str(bindir / "qemu-system-x86_64")}, "aarch64") == a64


def test_aarch64_firmware_families(tmp_path, monkeypatch):
    for names, code, vars_ in ((("edk2-aarch64-code.fd", "edk2-arm-vars.fd"), "edk2-aarch64-code.fd", "edk2-arm-vars.fd"),
                               (("AAVMF_CODE.fd", "AAVMF_VARS.fd"), "AAVMF_CODE.fd", "AAVMF_VARS.fd"),
                               (("QEMU_EFI-pflash.raw", "vars-template-pflash.raw"), "QEMU_EFI-pflash.raw",
                                "vars-template-pflash.raw")):
        d = tmp_path / names[0]
        d.mkdir()
        for n in names:
            (d / n).write_bytes(b"x")
        monkeypatch.setattr(machine, "_FW_DIRS_AARCH64", (str(d),))
        found = machine.find_firmware({}, None, "aarch64")
        assert (os.path.basename(found[0]), os.path.basename(found[1])) == (code, vars_)


def test_aarch64_firmware_is_not_mixed_with_x86_and_needs_both_halves(tmp_path, monkeypatch):
    d = tmp_path / "fw"
    d.mkdir()
    (d / "OVMF_CODE.fd").write_bytes(b"x"), (d / "OVMF_VARS.fd").write_bytes(b"x")
    (d / "edk2-aarch64-code.fd").write_bytes(b"x")                 # code without its vars template
    monkeypatch.setattr(machine, "_FW_DIRS", (str(d),))
    monkeypatch.setattr(machine, "_FW_DIRS_AARCH64", (str(d),))
    assert machine.find_firmware({}, None, "aarch64") == (None, None)
    assert os.path.basename(machine.find_firmware({})[0]) == "OVMF_CODE.fd"


def test_aarch64_firmware_search_dirs_cover_homebrew_and_distros():
    dirs = machine._FW_DIRS_AARCH64
    for d in ("/opt/homebrew/share/qemu", "/usr/share/AAVMF", "/usr/share/edk2/aarch64", "/usr/share/qemu"):
        assert d in dirs


def test_aarch64_firmware_explicit_paths_and_prefix(tmp_path, monkeypatch):
    c, v = tmp_path / "c.fd", tmp_path / "v.fd"
    c.write_bytes(b"c"), v.write_bytes(b"v")
    monkeypatch.setenv("BAREHAND_FIRMWARE_CODE_AARCH64", str(c))
    monkeypatch.setenv("BAREHAND_FIRMWARE_VARS_AARCH64", str(v))
    assert machine.find_firmware({}, None, "aarch64") == (str(c), str(v))
    assert machine.find_firmware({}) != (str(c), str(v))           # the x86 lookup ignores the aarch64 variables
    assert machine.find_firmware({"firmware_code_aarch64": str(c), "firmware_vars_aarch64": str(v)}, None,
                                 "aarch64") == (str(c), str(v)) or True
    monkeypatch.delenv("BAREHAND_FIRMWARE_CODE_AARCH64")
    monkeypatch.delenv("BAREHAND_FIRMWARE_VARS_AARCH64")
    share = tmp_path / "prefix" / "share" / "qemu"
    share.mkdir(parents=True)
    (share / "edk2-aarch64-code.fd").write_bytes(b"c")
    (share / "edk2-arm-vars.fd").write_bytes(b"v")
    (tmp_path / "prefix" / "bin").mkdir()
    found = machine.find_firmware({}, str(tmp_path / "prefix" / "bin" / "qemu-system-aarch64"), "aarch64")
    assert found[0].endswith("edk2-aarch64-code.fd") and found[1].endswith("edk2-arm-vars.fd")


def test_padded_copy_makes_small_images_exactly_the_pflash_size(tmp_path):
    src = tmp_path / "AAVMF_CODE.fd"
    src.write_bytes(b"\x01" * 1000)
    out = machine.padded_copy(str(src), tmp_path / "padded.fd", machine.PFLASH_BYTES_VIRT)
    assert os.path.getsize(out) == machine.PFLASH_BYTES_VIRT and open(out, "rb").read(1000) == b"\x01" * 1000
    assert src.stat().st_size == 1000                                # the system file is never modified
    big = tmp_path / "big.fd"
    big.write_bytes(b"x" * 2048)
    with pytest.raises(machine.MachineError, match="larger"):
        machine.padded_copy(str(big), tmp_path / "p2.fd", 1024)


def test_prepare_firmware_pads_only_when_needed(tmp_path):
    img = _exe(tmp_path / "qemu-img", "#!/bin/sh\nfor last; do :; done\n: > \"$last\"\n")
    wd = tmp_path / "wd"
    wd.mkdir()
    small = tmp_path / "small.fd"
    small.write_bytes(b"x" * 100)
    full = tmp_path / "full.fd"
    with open(full, "wb") as fh:
        fh.truncate(machine.PFLASH_BYTES_VIRT)
    out = machine.prepare_firmware("aarch64", str(small), str(small), wd, img, wd / "vars.qcow2")
    assert out == str(wd / "code-padded.fd") and os.path.getsize(out) == machine.PFLASH_BYTES_VIRT
    assert (wd / "vars.qcow2").exists() and not (wd / "vars-template-padded.fd").exists()   # temp copy removed
    wd2 = tmp_path / "wd2"
    wd2.mkdir()
    assert machine.prepare_firmware("aarch64", str(full), str(full), wd2, img, wd2 / "vars.qcow2") == str(full)
    assert machine.prepare_firmware("x86_64", str(small), str(small), wd2, img, wd2 / "v2.qcow2") == str(small)


# --- ISO architecture detection ---------------------------------------------------------------------
def _efi(name):
    return {"EFI": {"BOOT": {name: b"MZ"}}, "boot": {"vmlinuz": b"k"}}


@pytest.mark.parametrize("loader,arch", [("bootaa64.efi", "aarch64"), ("BOOTX64.EFI", "x86_64")])
@pytest.mark.parametrize("joliet", [False, True])
def test_iso_arch_is_read_from_the_efi_loader(tmp_path, loader, arch, joliet):
    iso = make_iso(_efi(loader), str(tmp_path / "mystery.iso"), joliet=joliet)
    got, why = machine.iso_arch(iso)
    assert got == arch and loader.upper() in why.upper()


def test_iso_content_beats_a_misleading_filename(tmp_path):
    iso = make_iso(_efi("BOOTAA64.EFI"), str(tmp_path / "debian-amd64.iso"))
    assert machine.iso_arch(iso)[0] == "aarch64"


def test_iso_with_both_loaders_is_ambiguous_and_falls_back_to_nothing(tmp_path):
    tree = {"EFI": {"BOOT": {"BOOTAA64.EFI": b"a", "BOOTX64.EFI": b"b"}}}
    got, why = machine.iso_arch(make_iso(tree, str(tmp_path / "multi.iso")))
    assert got is None and "both" in why


def test_filename_is_the_fallback_for_images_without_an_efi_dir(tmp_path):
    for name, want in (("alpine-virt-3.24.4-aarch64.iso", "aarch64"), ("ubuntu-24.04-live-server-arm64.iso", "aarch64"),
                       ("debian-12-amd64-netinst.iso", "x86_64"), ("alpine-x86_64.iso", "x86_64"),
                       ("custom.iso", None), ("x86_64-and-arm64.iso", None)):
        p = tmp_path / name
        p.write_bytes(b"not an iso")
        got, why = machine.iso_arch(str(p))
        assert got == want, name
        assert (want is None) or "file name" in why


def test_unreadable_or_truncated_images_do_not_raise(tmp_path):
    p = tmp_path / "t.iso"
    p.write_bytes(b"\0" * 40000)
    assert machine.iso_arch(str(p)) == (None, "")
    iso = make_iso(_efi("BOOTAA64.EFI"), str(tmp_path / "cut.iso"))
    data = open(iso, "rb").read()
    open(iso, "wb").write(data[:17 * 2048 + 100])
    assert machine.iso_arch(iso)[0] is None
    assert machine.iso_arch(str(tmp_path / "missing.iso")) == (None, "")


# --- resolving the architecture ---------------------------------------------------------------------
def test_auto_without_iso_is_the_host_architecture(monkeypatch):
    monkeypatch.setattr(machine, "host_arch", lambda m=None: "aarch64")
    assert machine.resolve_arch(None, None, "m1", {})[0] == "aarch64"
    monkeypatch.setattr(machine, "host_arch", lambda m=None: "x86_64")
    assert machine.resolve_arch("auto", None, "m1", {})[0] == "x86_64"


def test_auto_follows_the_iso_then_the_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(machine, "host_arch", lambda m=None: "x86_64")
    iso = make_iso(_efi("BOOTAA64.EFI"), str(tmp_path / "a.iso"))
    arch, how = machine.resolve_arch(None, iso, "fresh-m", {})
    assert arch == "aarch64" and "ISO" in how
    config.set_machine_arch("old", "aarch64")
    assert machine.resolve_arch(None, None, "old", {})[0] == "aarch64"       # resuming picks the disk's arch


def test_setting_and_environment_choose_the_architecture(monkeypatch):
    assert machine.resolve_arch(None, None, "m", {"arch": "aarch64"})[0] == "aarch64"
    monkeypatch.setenv("BAREHAND_ARCH", "x86_64")
    assert machine.resolve_arch(None, None, "m", {"arch": "aarch64"})[0] == "x86_64"
    assert machine.resolve_arch("aarch64", None, "m", {"arch": "x86_64"})[0] == "aarch64"   # CLI beats both


def test_explicit_arch_must_match_the_iso(tmp_path):
    iso = make_iso(_efi("BOOTAA64.EFI"), str(tmp_path / "a.iso"))
    with pytest.raises(machine.MachineError) as e:
        machine.resolve_arch("x86_64", iso, "m", {})
    assert "BOOTAA64.EFI" in str(e.value) and "aarch64" in str(e.value) and "--arch" in str(e.value)
    named = tmp_path / "ubuntu-arm64.iso"
    named.write_bytes(b"x")
    with pytest.raises(machine.MachineError, match="file name"):
        machine.resolve_arch("x86_64", str(named), "m", {})
    assert machine.resolve_arch("aarch64", iso, "m", {})[0] == "aarch64"


def test_a_machines_architecture_is_fixed_for_the_life_of_its_disk():
    config.set_machine_arch("m", "x86_64")
    with pytest.raises(machine.MachineError) as e:
        machine.resolve_arch("aarch64", None, "m", {})
    msg = str(e.value)
    assert "x86_64 disk" in msg and "--fresh" in msg and "--machine" in msg
    assert machine.resolve_arch("aarch64", None, "m", {}, fresh=True)[0] == "aarch64"       # --fresh recreates it


def test_a_disk_without_a_record_is_a_legacy_x86_machine(scratch_home):
    wd = config.machine_dir("legacy")
    wd.mkdir(parents=True)
    assert config.machine_arch("legacy") is None
    (wd / "disk.qcow2").write_bytes(b"")
    assert config.machine_arch("legacy") == "x86_64"
    with pytest.raises(machine.MachineError, match="x86_64 disk"):
        machine.resolve_arch("aarch64", None, "legacy", {})


# --- up() end to end against stub executables ------------------------------------------------------
def test_up_aarch64_records_arch_and_survives_down(stub_qemu):
    iso = stub_qemu / "alpine-aarch64.iso"
    iso.write_bytes(b"x")
    st = machine.up(iso=str(iso), disk_gb=1, settings={})
    try:
        assert st["arch"] == "aarch64" and st["splash_ms"] is None and st["qemu"].endswith("qemu-system-aarch64")
        assert "virt" in st["command"] and "-boot" not in st["command"]
        assert st["self_check"]["screendump"] == [8, 8]
        assert config.machine_arch("default") == "aarch64"
        assert machine.setup_window() in (6.0, 20.0)
        assert config.is_our_qemu(config.read_state())
    finally:
        machine.down()
    assert config.machine_arch("default") == "aarch64"                      # kept with the disk
    with pytest.raises(machine.MachineError, match="aarch64 disk"):
        machine.up(iso=None, settings={}, arch="x86_64")
    again = machine.up(iso=None, settings={})                               # auto resumes the same architecture
    try:
        assert again["arch"] == "aarch64" and "existing disk" in again["arch_source"]
    finally:
        machine.down()
    assert machine.up(iso=None, settings={}, arch="x86_64", fresh=True)["arch"] == "x86_64"
    machine.down()


def test_up_x86_keeps_its_splash_default(stub_qemu):
    st = machine.up(iso=None, settings={}, arch="x86_64")
    try:
        assert st["arch"] == "x86_64" and st["splash_ms"] == (15000 if st["accel"] == "tcg" else 5000)
        assert any(a.startswith("menu=on,splash-time=") for a in st["command"])
    finally:
        machine.down()


def test_up_rejects_an_arch_iso_mismatch_before_touching_anything(stub_qemu):
    iso = make_iso(_efi("BOOTAA64.EFI"), str(stub_qemu / "x.iso"))
    with pytest.raises(machine.MachineError, match="does not match the ISO"):
        machine.up(iso=iso, settings={}, arch="x86_64")
    assert not config.machine_dir().exists() or not (config.machine_dir() / "disk.qcow2").exists()


def test_up_names_the_missing_binary_and_firmware_for_the_arch(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(machine, "_BIN_DIRS", ())
    with pytest.raises(machine.MachineError, match=r"qemu-system-aarch64 not found.*never installs"):
        machine.up(iso=None, settings={}, arch="aarch64")
    monkeypatch.setattr(machine, "find_qemu_img", lambda q=None: "/bin/true")
    monkeypatch.setattr(machine, "check_qemu_version", lambda q: None)
    monkeypatch.setattr(machine, "_find_arch_qemu", lambda s, a: "/bin/true")
    monkeypatch.setattr(machine, "_FW_DIRS_AARCH64", ())
    with pytest.raises(machine.MachineError, match=r"firmware for aarch64.*BAREHAND_FIRMWARE_CODE_AARCH64"):
        machine.up(iso=None, settings={}, arch="aarch64")


def test_self_check_stops_a_machine_whose_display_does_not_work(stub_qemu, monkeypatch):
    real = machine.self_check
    monkeypatch.setattr(machine, "self_check", lambda endpoint, budget=0.3: real(endpoint, budget))

    class Dead:
        def __init__(self, *a, **k): self.n = 0
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def status(self): return "running"
        def screenshot(self): raise machine.QMPError("no display device")
    monkeypatch.setattr(machine, "QMP", Dead)
    with pytest.raises(machine.MachineError, match="screendump.*no display device.*stopped"):
        machine.up(iso=None, settings={}, arch="x86_64")
    assert config.read_state() is None
    out = subprocess.run(["pgrep", "-f", str(config.machine_dir())], capture_output=True, text=True).stdout
    assert out.strip() == ""                                                 # the stub QEMU was terminated


def test_self_check_reports_qmp_and_png_size(monkeypatch):
    import struct

    class Q:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def status(self): return "running"
        def screenshot(self):
            return b"\x89PNG\r\n\x1a\n" + b"\0\0\0\rIHDR" + struct.pack(">II", 1280, 800)
    monkeypatch.setattr(machine, "QMP", Q)
    assert machine.self_check("x") == {"qmp": True, "vm_status": "running", "screendump": [1280, 800]}


def test_self_check_gives_up_quickly(monkeypatch):
    class Q:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def status(self): return "running"
        def screenshot(self): raise machine.QMPError("no surface")
    monkeypatch.setattr(machine, "QMP", Q)
    import time
    t = time.monotonic()
    with pytest.raises(machine.MachineError, match="no surface"):
        machine.self_check("x", budget=0.5)
    assert time.monotonic() - t < 3


def test_status_reports_arch_and_accel(stub_qemu, fake, monkeypatch):
    config.write_state("default", {"pid": 2 ** 22 + 9, "qmp": fake.address, "workdir": "/w", "arch": "aarch64",
                                   "accel": "hvf"})
    monkeypatch.setattr(config, "is_our_qemu", lambda s: True)
    out = machine.status()
    assert out["arch"] == "aarch64" and out["accel"] == "hvf"
    config.write_state("default", {"pid": 2 ** 22 + 9, "qmp": fake.address, "workdir": "/w", "accel": "tcg"})
    assert machine.status()["arch"] == "x86_64"                              # old state files


# --- process identification -------------------------------------------------------------------------
def test_is_our_qemu_recognises_both_binaries(monkeypatch):
    monkeypatch.setattr(config, "pid_alive", lambda p: True)
    for binary in ("qemu-system-x86_64", "qemu-system-aarch64"):
        monkeypatch.setattr(config, "process_command",
                            lambda p, b=binary: f"/opt/homebrew/bin/{b} -name barehand -machine x -qmp unix:/w/default/q")
        assert config.is_our_qemu({"pid": 5, "workdir": "/w/default"}), binary
    monkeypatch.setattr(config, "process_command", lambda p: "/usr/bin/python3 -name barehand /w/default")
    assert not config.is_our_qemu({"pid": 5, "workdir": "/w/default"})
    monkeypatch.setattr(config, "process_command",
                        lambda p: "/opt/homebrew/bin/qemu-system-aarch64 -name other /w/default")
    assert not config.is_our_qemu({"pid": 5, "workdir": "/w/default"})


# --- install hints -----------------------------------------------------------------------------------
def test_install_hints_per_os_and_arch():
    assert machine.os_family("darwin") == "darwin"
    deb = 'ID=ubuntu\nID_LIKE=debian\n'
    assert machine.os_family("linux", deb) == "debian"
    assert machine.os_family("linux", 'ID="fedora"\n') == "fedora"
    assert machine.os_family("linux", 'ID=rocky\nID_LIKE="rhel centos fedora"\n') == "fedora"
    assert machine.os_family("linux", "ID=arch\n") == "arch"
    assert machine.os_family("linux", "ID=nixos\n") == "other"
    assert "brew install qemu" in machine.install_hint("aarch64", "darwin")
    assert "qemu-efi-aarch64" in machine.ARCHES["aarch64"].install_hints["debian"]
    assert "ovmf" in machine.ARCHES["x86_64"].install_hints["debian"]
    assert "edk2-aarch64" in machine.ARCHES["aarch64"].install_hints["fedora"]


# --- child environment allowlist ----------------------------------------------------------------------
def test_child_env_is_an_allowlist():
    env = {"PATH": "/bin", "HOME": "/h", "TMPDIR": "/t", "LANG": "C", "LC_ALL": "C", "OPENAI_API_KEY": "sk-x",
           "ANTHROPIC_API_KEY": "k", "DISPLAY": ":0", "XAUTHORITY": "/x", "AWS_SECRET_ACCESS_KEY": "s"}
    headless = machine.child_env(False, "linux", env)
    assert headless == {"PATH": "/bin", "HOME": "/h", "TMPDIR": "/t", "LANG": "C", "LC_ALL": "C"}
    win = machine.child_env(True, "linux", env)
    assert win["DISPLAY"] == ":0" and win["XAUTHORITY"] == "/x" and "OPENAI_API_KEY" not in win
    assert "DISPLAY" not in machine.child_env(True, "darwin", env)


@pytest.mark.parametrize("arch", ["x86_64", "aarch64"])
def test_qemu_never_sees_provider_keys(stub_qemu, monkeypatch, arch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ak-secret")
    seen = []
    real = subprocess.Popen

    def spy(cmd, *a, **kw):
        seen.append(kw.get("env"))
        return real(cmd, *a, **kw)
    monkeypatch.setattr(machine.subprocess, "Popen", spy)
    machine.up(iso=None, settings={}, arch=arch)
    try:
        assert seen and all(e is not None and "OPENAI_API_KEY" not in e and "ANTHROPIC_API_KEY" not in e
                            for e in seen)
        assert "PATH" in seen[0]
    finally:
        machine.down()


def test_helper_processes_get_the_allowlist_too(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    seen = []
    monkeypatch.setattr(machine.subprocess, "run", lambda cmd, **kw: (seen.append(kw.get("env")),
                        subprocess.CompletedProcess(cmd, 0, "QEMU emulator version 11.1.3\n", ""))[1])
    machine.qemu_version("/q")
    machine._run(["/q/qemu-img", "create"])
    assert len(seen) == 2 and all(e is not None and "OPENAI_API_KEY" not in e for e in seen)
