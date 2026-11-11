import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from barehand import config, machine


def test_accel_selection():
    assert machine.pick_accel("arm64", "darwin") == ("tcg", "max")       # x86 guest on Apple Silicon
    assert machine.pick_accel("x86_64", "darwin") == ("hvf", "host")
    assert machine.pick_accel("x86_64", "linux", has_kvm=True) == ("kvm", "host")
    assert machine.pick_accel("x86_64", "linux", has_kvm=False)[0] == "tcg"
    assert machine.pick_accel("aarch64", "linux", has_kvm=True)[0] == "tcg"  # KVM only for the same arch
    assert machine.pick_accel("AMD64", "win32")[0] == "tcg"                  # Windows is unsupported in v0


def test_tcg_is_never_forced_multithreaded():
    assert "thread=multi" not in machine.pick_accel("arm64", "darwin")[0]


def test_display_args():
    assert machine.display_args(False) == ["-display", "none"]
    assert machine.display_args(True, "darwin") == ["-display", "cocoa"]
    assert machine.display_args(True, "linux") == ["-display", "gtk"]


def _cmd(**kw):
    base = dict(qemu="/q/qemu-system-x86_64", accel="tcg", cpu="max", qmp="tcp:127.0.0.1:4321", code="/fw/code.fd",
                vars_img="/w/vars.qcow2", disk="/w/disk.qcow2", iso="/i/x.iso", memory_mb=1024, cpus=2,
                window=False, splash_ms=5000)
    base.update(kw)
    return machine.build_command(**base)


def test_qmp_spec_is_passed_through_and_loopback_only():
    cmd = _cmd()
    assert cmd[cmd.index("-qmp") + 1] == "tcp:127.0.0.1:4321,server=on,wait=off"
    assert not any("0.0.0.0" in part for part in cmd)
    cmd = _cmd(qmp="unix:/private/dir/qmp.sock")
    assert cmd[cmd.index("-qmp") + 1] == "unix:/private/dir/qmp.sock,server=on,wait=off"


def test_command_has_tablet_uefi_snapshotable_vars_and_no_shutdown():
    cmd = _cmd()
    joined = " ".join(cmd)
    assert "usb-tablet,bus=xhci.0" in cmd and "-vga" in cmd and "-no-shutdown" in cmd
    assert "if=pflash,unit=1,format=qcow2,file=/w/vars.qcow2" in joined
    assert "readonly=on,file=/fw/code.fd" in joined
    assert "format=raw,readonly=on,file=/i/x.iso" in joined      # never probe the ISO's format
    assert "ide-cd,drive=cd0" in joined and "-display none" in joined


def test_cdrom_drive_exists_even_without_iso_but_has_no_file():
    cmd = _cmd(iso=None)
    joined = " ".join(cmd)
    assert "ide-cd,drive=cd0" in joined and "id=cd0,media=cdrom" in joined and "/i/x.iso" not in joined


def _hardware(cmd):
    """Everything that defines the guest's device layout: all -device/-drive/-netdev/-vga options, paths of
    the ISO excluded (the ISO changes media, never hardware)."""
    out, it = [], iter(range(len(cmd)))
    for i in it:
        if cmd[i] in ("-device", "-drive", "-netdev", "-vga", "-machine", "-boot"):
            out.append((cmd[i], cmd[i + 1].split(",file=")[0].replace(",format=raw,readonly=on", ",readonly=on")
                        if cmd[i] == "-drive" else cmd[i + 1]))
    return out


def test_device_layout_is_identical_with_or_without_iso_and_window():
    ref = _hardware(_cmd())
    for kw in (dict(iso=None), dict(window=True, plat="darwin"), dict(window=True, plat="linux", iso=None),
               dict(memory_mb=4096, cpus=4), dict(accel="kvm", cpu="host")):
        assert _hardware(_cmd(**kw)) == ref, kw


def test_every_pci_device_has_a_pinned_unique_slot():
    cmd = _cmd()
    devs = [cmd[i + 1] for i, c in enumerate(cmd) if c == "-device"]
    pci = [d for d in devs if "bus=pcie.0" in d]
    addrs = [d.split("addr=")[1].split(",")[0] for d in pci]
    assert len(pci) == 5 and len(set(addrs)) == 5          # vga, disk, net, xhci, ahci
    for d in devs:
        # nothing is left to QEMU's first-free-slot allocation
        assert "bus=" in d or d.startswith(("usb-", "ide-cd"))
    assert "-vga" in cmd and cmd[cmd.index("-vga") + 1] == "none"


def test_pinned_slots_do_not_collide_with_q35_builtin_functions():
    assert all(1 <= n < 0x1f for n in machine.PCI_SLOTS.values())


def test_commas_in_paths_cannot_inject_drive_options():
    nasty = "/Users/me/ubuntu, 24.04.iso,readonly=off"
    cmd = _cmd(iso=nasty, disk="/w/a,b/disk.qcow2", vars_img="/w/v,v.qcow2", code="/fw/c,c.fd")
    joined = " ".join(cmd)
    assert "file=/Users/me/ubuntu,, 24.04.iso,,readonly=off" in joined
    assert "file=/w/a,,b/disk.qcow2,if=none" in joined
    assert "file=/w/v,,v.qcow2" in joined and "file=/fw/c,,c.fd" in joined
    # QEMU splits options on single commas only: every option key left after un-escaping is a known one
    for arg in cmd:
        if arg.startswith(("if=pflash", "file=", "if=none")):
            single = [p for p in arg.replace(",,", "\0").split(",")]
            assert all("=" in p for p in single if p), arg
            assert not any(p.startswith("readonly=off") for p in single), arg


def test_version_parsing_and_minimum():
    assert machine.parse_version("QEMU emulator version 11.1.3") == (11, 1)
    assert machine.parse_version("QEMU emulator version 6.2.0 (Debian)") == (6, 2)
    assert machine.parse_version("garbage") is None


def test_old_qemu_is_refused_with_a_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(machine, "qemu_version", lambda q: "QEMU emulator version 6.2.0")
    assert "7.1" in machine.check_qemu_version("/x")
    monkeypatch.setattr(machine, "qemu_version", lambda q: "QEMU emulator version 7.1.0")
    assert machine.check_qemu_version("/x") is None
    monkeypatch.setattr(machine, "qemu_version", lambda q: "unknown")
    assert machine.check_qemu_version("/x") is None


def _exe(path, body="#!/bin/sh\n"):
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def test_qemu_discovery_prefers_explicit_then_path(tmp_path, monkeypatch):
    explicit = _exe(tmp_path / "my-qemu")
    assert machine.find_qemu({"qemu_binary": explicit}) == explicit
    monkeypatch.setenv("BAREHAND_QEMU", explicit)
    assert machine.find_qemu({}) == explicit
    monkeypatch.delenv("BAREHAND_QEMU")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    onpath = _exe(bindir / "qemu-system-x86_64")
    monkeypatch.setenv("PATH", str(bindir))
    assert machine.find_qemu({}) == onpath


def test_firmware_discovery_from_env_and_prefix(tmp_path, monkeypatch):
    code, vars_ = tmp_path / "code.fd", tmp_path / "vars.fd"
    code.write_bytes(b"x"), vars_.write_bytes(b"y")
    monkeypatch.setenv("BAREHAND_FIRMWARE_CODE", str(code))
    monkeypatch.setenv("BAREHAND_FIRMWARE_VARS", str(vars_))
    assert machine.find_firmware({}) == (str(code), str(vars_))
    monkeypatch.delenv("BAREHAND_FIRMWARE_CODE")
    monkeypatch.delenv("BAREHAND_FIRMWARE_VARS")
    share = tmp_path / "prefix" / "share" / "qemu"
    share.mkdir(parents=True)
    (share / "edk2-x86_64-code.fd").write_bytes(b"c")
    (share / "edk2-i386-vars.fd").write_bytes(b"v")
    (tmp_path / "prefix" / "bin").mkdir()
    found = machine.find_firmware({}, str(tmp_path / "prefix" / "bin" / "qemu-system-x86_64"))
    assert found[0].endswith("edk2-x86_64-code.fd") and found[1].endswith("edk2-i386-vars.fd")


def test_firmware_code_and_vars_come_from_the_same_family(tmp_path, monkeypatch):
    d = tmp_path / "ovmf"
    d.mkdir()
    for n in ("OVMF_CODE.fd", "OVMF_VARS_4M.fd", "OVMF_CODE_4M.fd", "OVMF_VARS.fd"):
        (d / n).write_bytes(b"x")
    monkeypatch.setattr(machine, "_FW_DIRS", (str(d),))
    code, vars_ = machine.find_firmware({})
    assert (os.path.basename(code), os.path.basename(vars_)) == ("OVMF_CODE_4M.fd", "OVMF_VARS_4M.fd")


def test_up_without_qemu_is_a_clear_error_not_an_install(monkeypatch):
    monkeypatch.setattr(machine, "find_qemu", lambda settings=None: None)
    with pytest.raises(machine.MachineError, match="never installs"):
        machine.up(iso=None, settings={}, arch="x86_64")


def test_up_without_firmware_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(machine, "find_qemu", lambda settings=None: "/bin/true")
    monkeypatch.setattr(machine, "check_qemu_version", lambda q: None)
    monkeypatch.setattr(machine, "find_qemu_img", lambda qemu=None: "/bin/true")
    monkeypatch.setattr(machine, "find_firmware", lambda settings=None, qemu=None: (None, None))
    with pytest.raises(machine.MachineError, match="firmware"):
        machine.up(iso=None, settings={}, arch="x86_64")


def test_up_refuses_old_qemu(monkeypatch):
    monkeypatch.setattr(machine, "find_qemu", lambda settings=None: "/bin/true")
    monkeypatch.setattr(machine, "qemu_version", lambda q: "QEMU emulator version 6.2.0")
    with pytest.raises(machine.MachineError, match="7.1"):
        machine.up(iso=None, settings={}, arch="x86_64")


def test_iso_validation(tmp_path):
    with pytest.raises(machine.MachineError, match="not found"):
        machine.validate_iso(str(tmp_path / "nope.iso"))
    (tmp_path / "empty.iso").write_bytes(b"")
    with pytest.raises(machine.MachineError, match="empty"):
        machine.validate_iso(str(tmp_path / "empty.iso"))
    with pytest.raises(machine.MachineError):
        machine.validate_iso(str(tmp_path))
    (tmp_path / "a, b.iso").write_bytes(b"x")
    assert machine.validate_iso(str(tmp_path / "a, b.iso")).endswith("a, b.iso")


def test_snapshot_names_are_validated_with_fullmatch():
    for bad in ("", "a b", "../x", "x;quit", "a" * 65, "-x", "abc\n", "abc\nquit", None):
        with pytest.raises(machine.MachineError):
            machine._check_snapshot_name(bad)
    assert machine._check_snapshot_name("before-install_1.0") == "before-install_1.0"


def test_snapshot_tag_parsing_handles_both_id_styles():
    listing = ("List of snapshots present on all disks:\nID      TAG               VM_SIZE                DATE\n"
               "--      uefi-top         49.2 MiB 2026-11-10 17:45:56\n"
               "1       old-style        12 MiB 2020-01-01 00:00:00\n")
    assert machine.snapshot_tags(listing) == ["uefi-top", "old-style"]
    assert machine.snapshot_tags("There is no snapshot available.") == []


def test_operations_on_a_stopped_machine_explain_themselves():
    with pytest.raises(machine.MachineError, match="not running"):
        machine.reset()
    assert machine.status()["running"] is False
    assert "nothing to stop" in machine.down()


def test_stopped_machine_is_distinguished_from_never_started(scratch_home):
    config.write_state("default", {"pid": 2 ** 22 + 7, "qmp": "127.0.0.1:1", "workdir": "/w"})
    with pytest.raises(machine.MachineError, match="has stopped"):
        machine.reset()


def test_qmp_spec_prefers_a_private_unix_socket_and_falls_back_to_tcp(tmp_path, monkeypatch):
    short = tmp_path / "m"
    short.mkdir()
    monkeypatch.setattr(machine, "SOCKET_PATH_LIMIT", 500)   # pytest's tmp_path is deeper than a real data dir
    spec, endpoint, private = machine._qmp_spec(short)
    assert spec == endpoint == f"unix:{short}/qmp.sock" and private is None
    assert (short.stat().st_mode & 0o777) == 0o700
    monkeypatch.setattr(machine, "SOCKET_PATH_LIMIT", 5)
    spec, endpoint, private = machine._qmp_spec(short)
    assert spec.startswith("tcp:127.0.0.1:") and endpoint.startswith("127.0.0.1:")


def test_qmp_spec_uses_a_private_temp_dir_when_the_machine_dir_is_too_deep(tmp_path, monkeypatch):
    deep = tmp_path / ("d" * 120)
    deep.mkdir()
    spec, endpoint, private = machine._qmp_spec(deep)
    try:
        assert endpoint.startswith("unix:") and private and (os.stat(private).st_mode & 0o777) == 0o700
        assert len(os.fsencode(endpoint[5:])) <= machine.SOCKET_PATH_LIMIT
    finally:
        if private:
            import shutil; shutil.rmtree(private, ignore_errors=True)


# The stub keeps the shell (and so the original command line) as the process, as QEMU does: `exec sleep`
# would replace the command line that `is_our_qemu` checks, which /proc shows on Linux.
_STUB_QEMU = "#!/bin/sh\ntrap 'kill $c 2>/dev/null; exit 0' TERM INT\nsleep 300 &\nc=$!\nwait $c\n"


# --- up / down against stub executables (no real QEMU) ------------------------------------------------
@pytest.fixture
def stub_qemu(tmp_path, monkeypatch):
    bindir = tmp_path / "stubbin"
    bindir.mkdir()
    qemu = _exe(bindir / "qemu-system-x86_64", _STUB_QEMU)
    _exe(bindir / "qemu-system-aarch64", _STUB_QEMU)
    img = _exe(bindir / "qemu-img",
               "#!/bin/sh\nfor last; do :; done\nif [ \"$1\" = create ]; then : > \"$5\"; else : > \"$last\"; fi\n")
    fw = tmp_path / "fw"
    fw.mkdir()
    (fw / "edk2-x86_64-code.fd").write_bytes(b"c")
    (fw / "edk2-i386-vars.fd").write_bytes(b"v")
    monkeypatch.setenv("BAREHAND_QEMU", qemu)
    monkeypatch.setenv("BAREHAND_FIRMWARE_CODE", str(fw / "edk2-x86_64-code.fd"))
    monkeypatch.setenv("BAREHAND_FIRMWARE_VARS", str(fw / "edk2-i386-vars.fd"))
    (fw / "edk2-aarch64-code.fd").write_bytes(b"c")
    (fw / "edk2-arm-vars.fd").write_bytes(b"v")
    monkeypatch.setenv("BAREHAND_FIRMWARE_CODE_AARCH64", str(fw / "edk2-aarch64-code.fd"))
    monkeypatch.setenv("BAREHAND_FIRMWARE_VARS_AARCH64", str(fw / "edk2-arm-vars.fd"))
    monkeypatch.setattr(machine, "find_qemu_img", lambda qemu=None: img)
    monkeypatch.setattr(machine, "check_qemu_version", lambda q: None)

    class Probe:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def status(self): return "running"
        def screenshot(self):
            import struct
            return b"\x89PNG\r\n\x1a\n" + b"\0\0\0\rIHDR" + struct.pack(">II", 8, 8)
        def cmd(self, name, **k):
            if name == "quit":
                raise ConnectionError("stub qemu has no QMP")
    monkeypatch.setattr(machine, "QMP", Probe)
    yield tmp_path
    st = config.read_state()
    if st:
        try:
            os.kill(st["pid"], 9)
        except OSError:
            pass


def test_up_happy_path_writes_state_and_command(stub_qemu):
    iso = stub_qemu / "My, Distro.iso"
    iso.write_bytes(b"x")
    st = machine.up(iso=str(iso), disk_gb=1, settings={}, arch="x86_64")
    assert st["pid"] > 0 and st["qmp"].startswith(("unix:", "127.0.0.1:"))
    disk = f"{config.machine_dir()}/disk.qcow2"
    assert os.path.exists(disk)
    assert any("file=" in a and ",," in a for a in st["command"])              # comma escaped
    assert "-no-shutdown" in st["command"] and st["splash_ms"] == 15000          # TCG default
    assert config.is_our_qemu(config.read_state())
    assert (os.stat(config.state_path()).st_mode & 0o777) == 0o600
    with pytest.raises(machine.MachineError, match="already running"):
        machine.up(iso=None, settings={}, arch="x86_64")
    assert "stopped" in machine.down()
    assert config.read_state() is None
    assert not config.pid_alive(st["pid"]) or not config.is_our_qemu(st)


def test_down_with_purge_removes_the_machine_dir(stub_qemu):
    machine.up(iso=None, settings={}, arch="x86_64")
    machine.down(purge=True)
    assert not config.machine_dir().exists()


def test_down_never_signals_an_unrelated_process_with_a_stale_pid(stub_qemu):
    bystander = subprocess.Popen(["sleep", "60"])
    try:
        config.write_state("default", {"pid": bystander.pid, "qmp": "127.0.0.1:1",
                                       "workdir": str(config.machine_dir())})
        time.sleep(0.1)
        assert machine.down().startswith("machine 'default' stopped")
        time.sleep(0.2)
        assert bystander.poll() is None, "down() killed a process that is not our QEMU"
        assert config.read_state() is None
    finally:
        bystander.kill()
        bystander.wait()


def test_up_ignores_a_stale_state_file_whose_pid_was_reused(stub_qemu):
    bystander = subprocess.Popen(["sleep", "60"])
    try:
        config.write_state("default", {"pid": bystander.pid, "qmp": "127.0.0.1:1",
                                       "workdir": str(config.machine_dir())})
        st = machine.up(iso=None, settings={}, arch="x86_64")     # must not say "already running"
        assert st["pid"] != bystander.pid
        assert bystander.poll() is None
        machine.down()
    finally:
        bystander.kill()
        bystander.wait()


def test_down_refuses_attached_machines(monkeypatch):
    monkeypatch.setenv("BAREHAND_QMP", "127.0.0.1:5")
    with pytest.raises(machine.MachineError, match="not managed"):
        machine.down()


def test_status_reports_media_and_display_facts(fake, monkeypatch):
    monkeypatch.setenv("BAREHAND_QMP", fake.address)
    out = machine.status()
    assert out["installer_media"] == {"inserted": True, "file": "/x.iso"}
    assert out["display"]["size"] == [128, 96] and out["display"]["initialised"] is True   # one flat colour:
    assert out["display"]["blank"] is True                                                           # live but blank
    fake.paint(0, 0, 60, 40, (255, 255, 255))
    fake.paint(70, 50, 10, 10, (255, 0, 0))
    assert machine.status()["display"]["initialised"] is True
    fake.blocks[0].pop("inserted")
    assert machine.status()["installer_media"] == {"inserted": False}
    fake.blocks[:] = []
    assert machine.status()["installer_media"] == {"present": False}


def test_status_facts_survive_a_failing_query(fake, monkeypatch):
    monkeypatch.setenv("BAREHAND_QMP", fake.address)
    fake.fail_commands["query-block"] = "boom"
    out = machine.status()
    assert out["vm_status"] == "running" and "installer_media" not in out and "display" in out


def test_qemu_banner_sized_two_colour_display_is_not_initialised(fake, monkeypatch):
    monkeypatch.setenv("BAREHAND_QMP", fake.address)
    fake.resize(640, 480)
    fake.paint(10, 10, 100, 12, (255, 255, 255))
    assert machine.status()["display"]["initialised"] is False


def test_empty_cd_drive_names_no_format():
    # QEMU refuses `format=` on a drive with no file ("A block device must be specified").
    drives = [_cmd(iso=None)[i + 1] for i, c in enumerate(_cmd(iso=None)) if c == "-drive"]
    cd = [d for d in drives if "id=cd0" in d]
    assert cd == ["if=none,id=cd0,media=cdrom,readonly=on"]


# --- reset: display verification and recovery ------------------------------------------------------
from barehand.frames import Frame


def _frame(w, h, text=None, bg=(0, 0, 0)):
    buf = bytearray(bytes(bg) * (w * h))
    for (x, y) in (text or ()):
        buf[(y * w + x) * 3:(y * w + x) * 3 + 3] = b"\xff\xff\xff"
    return Frame(data=bytes(buf), width=w, height=h, offset=0, digest=b"")


def _line(w, h):
    return [(x, (h // 2 // 3) * 3) for x in range(w // 2 - 30, w // 2 + 30)]


def test_placeholder_is_detected_at_any_resolution():
    assert machine.placeholder_shown(_frame(640, 480, _line(640, 480)))
    assert machine.placeholder_shown(_frame(320, 200, _line(320, 200)))     # virtio-gpu keeps its scanout size


def test_placeholder_is_not_confused_with_live_displays():
    assert not machine.placeholder_shown(_frame(320, 200))                                   # flat black: live, blank
    assert not machine.placeholder_shown(_frame(320, 200, [(x, 5) for x in range(40, 200)]))  # text at the top
    assert not machine.placeholder_shown(_frame(320, 200, _line(320, 200), bg=(0, 0, 170)))  # light background
    assert not machine.placeholder_shown(_frame(320, 200, _line(320, 200) + [(3, 3)]))       # text + stray pixel


class _StubQ:
    def __init__(self, frames, log):
        self.frames, self.log = list(frames), log

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cmd(self, name, **kw):
        self.log.append(name)
        return {}

    def status(self):
        return "running"

    def frame(self):
        if "relaunch" in self.log and getattr(self, "after", None):
            return self.after[0]
        return self.frames[0]

    def send_chord(self, *a, **kw):
        self.log.append("key")


@pytest.fixture
def reset_env(monkeypatch):
    log, state = [], {"arch": "aarch64", "accel": "hvf"}
    frames = {"now": [_frame(128, 96, [(5, 5)])]}
    def _connect(name="x"):
        q = _StubQ(frames["now"], log)
        q.after = frames.get("after")
        return q
    monkeypatch.setattr(machine, "connect", _connect)
    monkeypatch.setattr(machine, "_relaunch", lambda name: (log.append("relaunch"), state.pop("snapshotted", None)))
    monkeypatch.setattr(machine.config, "read_state", lambda name="x": dict(state))
    monkeypatch.setattr(machine, "_target", lambda name: ("tcp:127.0.0.1:1", dict(state), "state"))
    monkeypatch.setattr(machine, "DISPLAY_WAITS", {k: 0.2 for k in machine.DISPLAY_WAITS})
    monkeypatch.setattr(machine.time, "sleep", lambda s: None)
    return log, state, frames


def test_reset_with_live_display_does_not_relaunch(reset_env):
    log, state, frames = reset_env
    out = machine.reset("x")
    assert "relaunch" not in log and "system_reset" in log and "display live" in out


def test_reset_recovers_from_a_dead_display_by_relaunching(reset_env):
    log, state, frames = reset_env
    dead, live = _frame(320, 200, _line(320, 200)), _frame(320, 200, [(x, 9) for x in range(300)])
    frames["now"][:] = [dead]
    frames["after"] = [live]
    out = machine.reset("x")
    assert log.count("system_reset") == 1 and log.count("relaunch") == 1
    assert "relaunched" in out and "display live" in out


def test_reset_reports_a_display_that_stays_dead_and_suggests_power_off(reset_env):
    log, state, frames = reset_env
    frames["now"][:] = [_frame(320, 200, _line(320, 200))]
    out = machine.reset("x")
    assert "still dead" in out and "hermes barehand up" in out and "hermes barehand down" in out


def test_reset_after_snapshot_on_aarch64_relaunches_first(reset_env):
    log, state, frames = reset_env
    state["snapshotted"] = True
    out = machine.reset("x")
    assert log[0] == "relaunch" and "system_reset" not in log and "relaunched" in out
    assert "snapshotted" not in state


def test_reset_after_snapshot_on_x86_stays_a_plain_reset(reset_env):
    log, state, frames = reset_env
    state.update(snapshotted=True, arch="x86_64", accel="tcg")
    machine.reset("x")
    assert "relaunch" not in log and "system_reset" in log


def test_reset_enter_setup_holds_escape_then_checks_the_display(reset_env):
    log, state, frames = reset_env
    out = machine.reset("x", enter_setup=True, setup_seconds=0.3)
    assert log.count("key") >= 1 and "Escape" in out and "display live" in out


def test_reset_of_an_attached_endpoint_never_relaunches(reset_env, monkeypatch):
    log, state, frames = reset_env
    monkeypatch.setattr(machine, "_target", lambda name: ("tcp:10.0.0.9:4444", None, "config"))
    state["snapshotted"] = True            # a stale state file of a Barehand-started QEMU must not matter
    out = machine.reset("x")
    assert "relaunch" not in log and log.count("system_reset") == 1 and "display live" in out
    frames["now"][:] = [_frame(320, 200, _line(320, 200))]
    out = machine.reset("x")
    assert "relaunch" not in log and "still dead" in out and "attached from outside Barehand" in out
    assert "restart that machine yourself" in out


class _Q:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cmd(self, name, **kw):
        _Q.sent.append((name, kw))
        if name == "query-block":
            return [{"removable": True, "inserted": True, "device": "cd0", "qdev": "cdrom0"}]
        return {}

    def status(self):
        return "running"


def _cmd_with_iso():
    return machine.build_command(qemu="/q", accel="tcg", cpu="max", qmp="tcp:127.0.0.1:1", code="/c.fd",
                                 vars_img="/v.qcow2", disk="/d.qcow2", iso="/isos/a,b.iso", memory_mb=512,
                                 cpus=1, window=False, arch="aarch64")


def test_eject_records_ejected_in_the_state(monkeypatch):
    _Q.sent, store = [], {"pid": 1, "command": _cmd_with_iso()}
    monkeypatch.setattr(machine, "connect", lambda name="x": _Q())
    monkeypatch.setattr(machine, "_target", lambda name: ("tcp:127.0.0.1:1", dict(store), "state"))
    monkeypatch.setattr(machine.config, "read_state", lambda name="x": dict(store))
    monkeypatch.setattr(machine.config, "write_state", lambda name, st: store.update(st))
    assert machine.eject("x").startswith("ejected") and store["ejected"] is True


def test_command_without_iso_empties_only_the_cd_drive():
    cmd = _cmd_with_iso()
    assert any("file=/isos/a,,b.iso" in a for a in cmd)
    out = machine.command_without_iso(cmd)
    assert len(out) == len(cmd) and not any("a,,b.iso" in a for a in out)
    assert "if=none,id=cd0,media=cdrom,readonly=on" in out
    assert [a for a in cmd if "cd0" not in a] == [a for a in out if "cd0" not in a]


@pytest.mark.parametrize("ejected", [False, True])
def test_relaunch_builds_the_command_and_leaves_the_cd_empty_after_eject(monkeypatch, tmp_path, ejected):
    cmd = _cmd_with_iso()
    state = {"pid": 4242, "qmp": "tcp:127.0.0.1:1", "workdir": str(tmp_path), "command": cmd, "window": False,
             "snapshotted": True, **({"ejected": True} if ejected else {})}
    seen, written, alive = {}, {}, [True]

    class Proc:
        pid = 777

        def poll(self):
            return None

    def popen(argv, **kw):
        seen["argv"] = argv
        return Proc()
    _Q.sent = []
    monkeypatch.setattr(machine, "QMP", _Q)
    monkeypatch.setattr(machine.subprocess, "Popen", popen)
    monkeypatch.setattr(machine.time, "sleep", lambda s: None)
    monkeypatch.setattr(machine.config, "read_state", lambda name="x": dict(state))
    monkeypatch.setattr(machine.config, "write_state", lambda name, st: written.update(st))
    monkeypatch.setattr(machine.config, "is_our_qemu", lambda s: alive.pop() if alive else False)
    machine._relaunch("x")
    assert ("quit", {}) in _Q.sent
    has_iso = any("a,,b.iso" in a for a in seen["argv"])
    assert has_iso is (not ejected)
    assert seen["argv"][0] == "/q" and "-qmp" in seen["argv"]
    assert written["pid"] == 777 and "snapshotted" not in written and written.get("ejected", False) is ejected
    assert (tmp_path / "qemu.log").exists()


# --- disk checkpoints on aarch64 + HVF -----------------------------------------------------------
class _CkQ:
    def __init__(self):
        self.snaps = {"pflash1": {}, "hd0": {}}
        self.calls = []
        self.state = "running"

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def status(self):
        return self.state

    def cmd(self, execute, **kw):
        name = execute
        self.calls.append((name, kw))
        if name == "query-block":
            return [{"device": d, "inserted": {"image": {"snapshots": [
                {"name": t, "vm-state-size": v} for t, v in s.items()]}}} for d, s in self.snaps.items()]
        if name == "stop":
            self.state = "paused"
        if name == "cont":
            self.state = "running"
        if name == "transaction":
            for a in kw["actions"]:
                self.snaps[a["data"]["device"]][a["data"]["name"]] = 0
        if name == "blockdev-snapshot-delete-internal-sync":
            self.snaps[kw["device"]].pop(kw["name"], None)
        return {}


@pytest.fixture
def ck(monkeypatch, tmp_path):
    q = _CkQ()
    st = {"arch": "aarch64", "accel": "hvf", "workdir": str(tmp_path), "command": ["/x/qemu-system-aarch64"]}
    monkeypatch.setattr(machine, "_target", lambda name: ("unix:/q", st, "state"))
    monkeypatch.setattr(machine, "connect", lambda name: q)
    monkeypatch.setattr(machine.config, "read_state", lambda name: st)
    monkeypatch.setattr(machine, "find_qemu_img", lambda qemu=None: "/x/qemu-img")
    marked = []
    monkeypatch.setattr(machine, "_mark_snapshotted", lambda name: marked.append(name))
    q.marked, q.st = marked, st
    return q


def test_disk_checkpoint_save_list_delete(ck):
    out = machine.snapshot("save", "c1", "m")
    assert "disk checkpoint" in out and "crash-consistent" in out and "Warning" not in out
    names = [c[0] for c in ck.calls]
    assert names.index("stop") < names.index("transaction") < names.index("cont")
    tx = [c for c in ck.calls if c[0] == "transaction"][0][1]["actions"]
    assert {a["data"]["device"] for a in tx} == {"pflash1", "hd0"} and not ck.marked
    assert "c1  disk checkpoint" in machine.snapshot("list", None, "m")
    with pytest.raises(machine.MachineError, match="already exists"):
        machine.snapshot("save", "c1", "m")
    machine.snapshot("save", "c1", "m", overwrite=True)
    machine.snapshot("delete", "c1", "m")
    assert machine.snapshot("list", None, "m") == "(no snapshots)"
    with pytest.raises(machine.MachineError, match="no snapshot named"):
        machine.snapshot("load", "zz", "m")


def test_disk_checkpoint_load_rolls_back_both_images_and_relaunches(ck, monkeypatch):
    machine.snapshot("save", "c1", "m")
    runs, rel = [], []
    monkeypatch.setattr(machine, "_run", lambda cmd: runs.append(list(cmd)))
    monkeypatch.setattr(machine, "_relaunch", lambda name, prepare=None: (rel.append(name), prepare(ck.st)))
    out = machine.snapshot("load", "c1", "m")
    assert rel == ["m"] and "restarted the machine" in out and "memory state is not part" in out
    assert [r[:4] for r in runs] == [["/x/qemu-img", "snapshot", "-a", "c1"]] * 2
    assert sorted(Path(r[4]).name for r in runs) == ["disk.qcow2", "vars.qcow2"]
    assert not ck.marked


def test_full_snapshot_stays_on_the_old_path_and_other_hosts_are_unchanged(ck, monkeypatch):
    ck.st["accel"] = "tcg"
    assert machine.disk_checkpoints_apply(ck.st) is False
    assert machine.disk_checkpoints_apply({"arch": "x86_64", "accel": "hvf"}) is False
    assert machine.disk_checkpoints_apply({"arch": "aarch64", "accel": "hvf"}) is True
    ck.snaps["pflash1"]["old"] = 5 << 20
    assert machine.checkpoint_tags(ck) == {"old": "full"}
