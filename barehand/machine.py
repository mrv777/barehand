"""Blank-machine lifecycle for QEMU: discovery, launch, status, reset, eject, snapshots, shutdown.

This module spawns ``qemu-system-x86_64`` or ``qemu-system-aarch64`` (and runs ``qemu-img``) as ordinary
subprocesses. Everything that differs between guest architectures lives in the ``ARCHES`` table below.
QMP is served on a unix socket inside a private (0700) directory when the path fits the platform limit,
otherwise on loopback TCP (127.0.0.1, ephemeral port). Nothing is installed or downloaded. POSIX hosts only
(Linux, macOS).
"""

from __future__ import annotations

import os
import platform as _platform
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from . import config
from .frames import display_state
from .qmp import QMP, QMPBusy, QMPError

MIN_QEMU = (7, 1)          # screendump format=png
SOCKET_PATH_LIMIT = 100    # sun_path is 104 bytes on macOS, 108 on Linux; stay under both
PFLASH_BYTES_VIRT = 64 * 1024 * 1024   # `-machine virt` needs both pflash banks to be exactly 64 MiB

_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/opt/local/bin", "/run/current-system/sw/bin")
_FW_DIRS = (
    "/opt/homebrew/share/qemu", "/usr/local/share/qemu",           # Homebrew (Apple Silicon / Intel)
    "/usr/share/qemu", "/usr/share/OVMF", "/usr/share/ovmf",       # Debian/Ubuntu
    "/usr/share/edk2/ovmf", "/usr/share/edk2-ovmf", "/usr/share/edk2/x64",  # Fedora/Arch
    "/usr/share/qemu/firmware", "/run/current-system/sw/share/qemu",
)
_FW_DIRS_AARCH64 = (
    "/opt/homebrew/share/qemu", "/usr/local/share/qemu",           # Homebrew
    "/usr/share/qemu", "/usr/share/AAVMF", "/usr/share/qemu-efi-aarch64",  # Debian/Ubuntu (qemu-efi-aarch64)
    "/usr/share/edk2/aarch64", "/usr/share/edk2-aarch64",          # Fedora (edk2-aarch64) / Arch
    "/usr/share/edk2/arm", "/run/current-system/sw/share/qemu",
)
# (code, vars) pairs: a code image must be paired with the vars template of the same family and size.
_FW_PAIRS = (("edk2-x86_64-code.fd", "edk2-i386-vars.fd"), ("OVMF_CODE_4M.fd", "OVMF_VARS_4M.fd"),
             ("OVMF_CODE.4m.fd", "OVMF_VARS.4m.fd"), ("OVMF_CODE.fd", "OVMF_VARS.fd"))
_FW_PAIRS_AARCH64 = (("edk2-aarch64-code.fd", "edk2-arm-vars.fd"),               # QEMU's own / Homebrew
                     ("AAVMF_CODE.fd", "AAVMF_VARS.fd"),                          # Debian/Ubuntu
                     ("QEMU_EFI-pflash.raw", "vars-template-pflash.raw"),         # Fedora
                     ("QEMU_EFI-silent-pflash.raw", "vars-template-pflash.raw"),
                     ("QEMU_CODE.fd", "QEMU_VARS.fd"))                            # Arch


class MachineError(RuntimeError):
    """User-facing lifecycle failure."""


# --- guest architectures -----------------------------------------------------------------------
@dataclass(frozen=True)
class ArchSpec:
    """Everything that differs per guest architecture. Device layout (``device_args``) is built from this."""
    name: str
    binary: str
    machine: str                       # QEMU -machine type
    fw_pairs: Tuple[Tuple[str, str], ...]
    fw_dirs: Tuple[str, ...]
    fw_setting: str                    # settings / env name stem for an explicit firmware path
    pflash_bytes: Optional[int]        # pflash banks must be exactly this size (None = any)
    display: str                       # device name of the display adapter
    cd: str                            # "ahci" (ide-cd on AHCI) | "scsi" (virtio-scsi-pci + scsi-cd)
    slots: Mapping[str, int]           # pinned PCI slots on the root bus
    boot_menu: bool                    # x86 fw_cfg `-boot menu=on,splash-time=...`
    aliases: Tuple[str, ...]           # host `uname -m` values / filename words that mean this arch
    install_hints: Mapping[str, str]   # os key -> how to install the QEMU binary and firmware


# PCI addresses on the root bus are pinned. EDK2 stores boot options as device paths that contain the PCI
# slot, so a boot order saved in firmware only survives the next `up` if the layout never changes. The same
# devices are created in every run: the CD-ROM controller and drive exist with or without an ISO.
PCI_SLOTS = {"vga": 1, "disk": 2, "net": 3, "xhci": 4, "ahci": 5}
PCI_SLOTS_AARCH64 = {"gpu": 1, "disk": 2, "net": 3, "xhci": 4, "scsi": 5}

ARCHES: Dict[str, ArchSpec] = {
    "x86_64": ArchSpec(
        name="x86_64", binary="qemu-system-x86_64", machine="q35", fw_pairs=_FW_PAIRS, fw_dirs=_FW_DIRS,
        fw_setting="firmware", pflash_bytes=None, display="VGA", cd="ahci", slots=PCI_SLOTS, boot_menu=True,
        aliases=("x86_64", "amd64", "x64"),
        install_hints={
            "darwin": "brew install qemu",
            "debian": "apt install qemu-system-x86 ovmf",
            "fedora": "dnf install qemu-system-x86 edk2-ovmf",
            "arch": "pacman -S qemu-system-x86 edk2-ovmf",
            "other": "install qemu-system-x86_64 and OVMF/edk2 firmware with your package manager"}),
    "aarch64": ArchSpec(
        name="aarch64", binary="qemu-system-aarch64", machine="virt", fw_pairs=_FW_PAIRS_AARCH64,
        fw_dirs=_FW_DIRS_AARCH64, fw_setting="firmware_aarch64", pflash_bytes=PFLASH_BYTES_VIRT,
        display="virtio-gpu-pci", cd="scsi", slots=PCI_SLOTS_AARCH64, boot_menu=False,
        aliases=("aarch64", "arm64"),
        install_hints={
            "darwin": "brew install qemu",
            "debian": "apt install qemu-system-arm qemu-efi-aarch64",
            "fedora": "dnf install qemu-system-aarch64 edk2-aarch64",
            "arch": "pacman -S qemu-system-aarch64 edk2-aarch64",
            "other": "install qemu-system-aarch64 and AAVMF/edk2-aarch64 firmware with your package manager"}),
}
QEMU_BINARY = ARCHES["x86_64"].binary     # historical name
ARCH_CHOICES = ("auto", *ARCHES)

# Seconds to hold Escape after a reset so the firmware lands in its setup menu, by (arch, accel kind).
# x86 firmware needs ~15 s of splash under TCG; the aarch64 firmware read keys ~1.4 s after reset under HVF.
SETUP_WINDOWS = {("x86_64", "tcg"): 35.0, ("x86_64", "hw"): 12.0,
                 ("aarch64", "tcg"): 20.0, ("aarch64", "hw"): 6.0}


def spec(arch: str) -> ArchSpec:
    try:
        return ARCHES[arch]
    except KeyError:
        raise MachineError(f"unknown architecture {arch!r}; choose one of {', '.join(ARCH_CHOICES)}") from None


def host_arch(host_machine: Optional[str] = None) -> str:
    """Native guest architecture of this host: ``aarch64`` for arm64/aarch64, else ``x86_64``."""
    m = (host_machine or _platform.machine()).lower()
    return "aarch64" if m in ARCHES["aarch64"].aliases else "x86_64"


accel_kind = config.accel_kind


def os_family(plat: Optional[str] = None, os_release: Optional[str] = None) -> str:
    """darwin | debian | fedora | arch | other (for install hints)."""
    plat = plat or sys.platform
    if plat == "darwin":
        return "darwin"
    text = os_release
    if text is None:
        try:
            text = Path("/etc/os-release").read_text()
        except OSError:
            text = ""
    ids = " ".join(re.findall(r'^(?:ID|ID_LIKE)="?([^"\n]*)', text, re.M)).lower().split()
    for fam, names in (("debian", ("debian", "ubuntu")), ("fedora", ("fedora", "rhel", "centos")),
                       ("arch", ("arch", "manjaro"))):
        if any(n in ids for n in names):
            return fam
    return "other"


def install_hint(arch: str, plat: Optional[str] = None) -> str:
    return spec(arch).install_hints[os_family(plat)]


# --- discovery ---------------------------------------------------------------------------------
def _find_binary(name: str, extra: Optional[str] = None) -> Optional[str]:
    if extra and os.path.isfile(extra) and os.access(extra, os.X_OK):
        return extra
    found = shutil.which(name)
    if found:
        return found
    for d in _BIN_DIRS:
        p = os.path.join(d, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def _explicit_qemu(arch: str, explicit: Optional[str]) -> Optional[str]:
    """An explicitly configured binary (env/setting). If it is named after a *different* known architecture
    (qemu-system-x86_64 configured, aarch64 wanted) its sibling for this architecture is used instead."""
    if not explicit:
        return None
    base = os.path.basename(explicit)
    wanted = ARCHES[arch].binary
    if base == wanted or base not in {a.binary for a in ARCHES.values()}:
        return explicit
    return os.path.join(os.path.dirname(explicit), wanted)


def find_qemu(settings: Optional[Mapping[str, Any]] = None, arch: str = "x86_64") -> Optional[str]:
    return _find_arch_qemu(settings, arch)


def _find_arch_qemu(settings: Optional[Mapping[str, Any]], arch: str) -> Optional[str]:
    st = settings or {}
    explicit = _explicit_qemu(arch, os.environ.get("BAREHAND_QEMU") or st.get("qemu_binary"))
    return _find_binary(ARCHES[arch].binary, explicit)


def qemu_for(settings: Optional[Mapping[str, Any]], arch: str) -> Optional[str]:
    """``find_qemu`` for an architecture (x86_64 goes through ``find_qemu(settings)`` unchanged)."""
    return find_qemu(settings) if arch == "x86_64" else _find_arch_qemu(settings, arch)


def firmware_for(settings: Optional[Mapping[str, Any]], qemu: Optional[str], arch: str
                 ) -> Tuple[Optional[str], Optional[str]]:
    return find_firmware(settings, qemu) if arch == "x86_64" else _find_arch_firmware(settings, qemu, arch)


def find_qemu_img(qemu: Optional[str] = None) -> Optional[str]:
    img = _find_binary("qemu-img")
    if img:
        return img
    if qemu:
        sibling = Path(qemu).with_name("qemu-img")
        if sibling.is_file():
            return str(sibling)
    return None


def find_firmware(settings: Optional[Mapping[str, Any]] = None, qemu: Optional[str] = None,
                  arch: str = "x86_64") -> Tuple[Optional[str], Optional[str]]:
    return _find_arch_firmware(settings, qemu, arch)


def _find_arch_firmware(settings: Optional[Mapping[str, Any]], qemu: Optional[str], arch: str
                        ) -> Tuple[Optional[str], Optional[str]]:
    """(code, vars_template) UEFI firmware for the guest architecture from the same family, or (None, None).
    Explicit paths: ``firmware_code``/``firmware_vars`` (x86_64) or ``firmware_code_aarch64``/
    ``firmware_vars_aarch64`` settings, or the matching ``BAREHAND_FIRMWARE_CODE[_AARCH64]`` environment."""
    a = ARCHES[arch]
    st = settings or {}
    suffix = "" if arch == "x86_64" else "_aarch64"
    code = os.environ.get(f"BAREHAND_FIRMWARE_CODE{suffix.upper()}") or st.get(f"firmware_code{suffix}")
    vars_ = os.environ.get(f"BAREHAND_FIRMWARE_VARS{suffix.upper()}") or st.get(f"firmware_vars{suffix}")
    code = code if code and os.path.isfile(code) else None
    vars_ = vars_ if vars_ and os.path.isfile(vars_) else None
    if code and vars_:
        return code, vars_
    dirs: List[str] = list(_FW_DIRS if arch == "x86_64" else _FW_DIRS_AARCH64)  # read at call time (patchable)
    if qemu:  # <prefix>/bin/qemu-system-* -> <prefix>/share/qemu
        dirs.insert(0, str(Path(qemu).resolve().parent.parent / "share" / "qemu"))
    for d in dirs:
        for c, v in a.fw_pairs:
            cp, vp = os.path.join(d, c), os.path.join(d, v)
            if os.path.isfile(cp) and os.path.isfile(vp):
                return code or cp, vars_ or vp
    return code, vars_


def padded_copy(src: str, dst: Path, size: int) -> str:
    """Copy ``src`` to ``dst`` zero-padded to exactly ``size`` bytes (distros ship some aarch64 firmware
    images smaller than the 64 MiB the ``virt`` pflash banks require). A larger file is an error."""
    n = os.path.getsize(src)
    if n > size:
        raise MachineError(f"firmware image {src} is {n} bytes, larger than the {size} bytes this machine type "
                           "accepts; point BAREHAND_FIRMWARE_CODE_AARCH64 at a different image")
    shutil.copyfile(src, dst)
    with open(dst, "r+b") as fh:
        fh.truncate(size)
    return str(dst)


def pick_accel(host_machine: Optional[str] = None, plat: Optional[str] = None, has_kvm: Optional[bool] = None,
               guest: str = "x86_64") -> Tuple[str, str]:
    """(accelerator, cpu model) for a (host, guest architecture) pair: ``hvf`` on macOS and ``kvm`` on Linux
    (when /dev/kvm is usable) only if host and guest architectures match; everything else is plain ``tcg``
    with ``-cpu max``. QEMU picks its own TCG threading (forcing multi-threaded TCG for an x86 guest on a
    weakly ordered arm64 host is documented by QEMU as unsafe)."""
    plat = plat or sys.platform
    if host_arch(host_machine) == guest:
        if plat == "darwin":
            return "hvf", "host"
        if plat.startswith("linux"):
            kvm = os.access("/dev/kvm", os.R_OK | os.W_OK) if has_kvm is None else has_kvm
            if kvm:
                return "kvm", "host"
    return "tcg", "max"


def display_args(window: bool, plat: Optional[str] = None) -> List[str]:
    if not window:
        return ["-display", "none"]
    plat = plat or sys.platform
    return ["-display", "cocoa" if plat == "darwin" else "gtk"]


# Child processes (QEMU, qemu-img) never inherit Hermes' environment, which holds provider API keys.
ENV_ALLOW = ("PATH", "HOME", "TMPDIR", "LANG", "LANGUAGE", "USER", "LOGNAME", "SHELL")   # plus any LC_*
ENV_ALLOW_DISPLAY = {"linux": ("DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "XAUTHORITY", "XDG_SESSION_TYPE"),
                     "darwin": ("__CF_USER_TEXT_ENCODING",)}   # only when a window is requested


def child_env(window: bool = False, plat: Optional[str] = None, environ: Optional[Mapping[str, str]] = None
              ) -> Dict[str, str]:
    """Minimal explicit environment for QEMU and qemu-img: an allowlist, never a copy of ``os.environ``."""
    env = os.environ if environ is None else environ
    plat = plat or sys.platform
    names = list(ENV_ALLOW)
    if window:
        names += ENV_ALLOW_DISPLAY["darwin" if plat == "darwin" else "linux"]
    out = {k: env[k] for k in names if k in env}
    out.update({k: v for k, v in env.items() if k.startswith("LC_")})
    return out


def qemu_version(qemu: str) -> str:
    try:
        out = subprocess.run([qemu, "--version"], capture_output=True, text=True, timeout=10,
                             env=child_env()).stdout
        return out.splitlines()[0].strip() if out else "unknown"
    except Exception:
        return "unknown"


def parse_version(text: str) -> Optional[Tuple[int, int]]:
    m = re.search(r"version (\d+)\.(\d+)", text or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def check_qemu_version(qemu: str) -> Optional[str]:
    """None if QEMU is new enough, else an explanation."""
    text = qemu_version(qemu)
    v = parse_version(text)
    if v is not None and v < MIN_QEMU:
        return (f"{text}: QEMU >= {MIN_QEMU[0]}.{MIN_QEMU[1]} is required (screendump to PNG). "
                "Upgrade QEMU (Ubuntu 22.04 ships 6.2).")
    return None


def free_loopback_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _q(value: str) -> str:
    """Escape a value for QEMU's comma-separated option syntax (a literal comma is written ',,')."""
    return str(value).replace(",", ",,")


def device_args(iso: Optional[str], disk: str, arch: str = "x86_64") -> List[str]:
    """The guest hardware part of the command line. Depends only on the architecture and on whether an ISO is
    inserted through the ``file=`` of the (always present) CD drive, never on the window, memory, CPU count
    or splash time."""
    a = ARCHES[arch]

    def at(name: str) -> str:
        return f"bus=pcie.0,addr=0x{a.slots[name]:x}"
    cd_file = f",file={_q(iso)}" if iso else ""
    common = [
        "-drive", f"file={_q(disk)},if=none,format=qcow2,id=hd0",
        "-device", f"virtio-blk-pci,drive=hd0,{at('disk')}",
        "-netdev", "user,id=n0", "-device", f"virtio-net-pci,netdev=n0,{at('net')}",
        "-device", f"qemu-xhci,id=xhci,{at('xhci')}", "-device", "usb-tablet,bus=xhci.0",
        "-device", "usb-kbd,bus=xhci.0",
    ]
    # An empty drive must not name a format: QEMU refuses `format=` without a file.
    cd_drive = ["-drive", (f"if=none,id=cd0,media=cdrom,format=raw,readonly=on{cd_file}" if iso
                           else "if=none,id=cd0,media=cdrom,readonly=on")]
    if arch == "aarch64":
        # No ramfb (firmware only: the Linux console never appears) and no AHCI/IDE (the aarch64 firmware
        # cannot see a CD there); virtio-scsi + scsi-cd works. The drive keeps the id `cdrom0` for eject.
        return [
            "-device", f"virtio-gpu-pci,{at('gpu')}", *common, *cd_drive,
            "-device", f"virtio-scsi-pci,id=scsi0,{at('scsi')}",
            "-device", "scsi-cd,drive=cd0,bus=scsi0.0,id=cdrom0",
        ]
    return [
        "-vga", "none", "-device", f"VGA,{at('vga')}", *common,
        "-device", f"ahci,id=ahci,{at('ahci')}", *cd_drive,
        "-device", "ide-cd,drive=cd0,bus=ahci.0,id=cdrom0",
    ]


def build_command(*, qemu: str, accel: str, cpu: str, qmp: str, code: str, vars_img: str, disk: str,
                  iso: Optional[str], memory_mb: int, cpus: int, window: bool, splash_ms: Optional[int] = None,
                  plat: Optional[str] = None, arch: str = "x86_64") -> List[str]:
    """``qmp`` is the full chardev spec, e.g. ``unix:/dir/qmp.sock`` or ``tcp:127.0.0.1:4444``."""
    a = ARCHES[arch]
    machine_type = a.machine
    if arch == "aarch64" and accel == "kvm":
        machine_type += ",gic-version=host"   # arm64 hosts with a GICv2-only CPU cannot use QEMU's default v3
    cmd = [
        qemu, "-name", "barehand", "-machine", machine_type, "-cpu", cpu, "-accel", accel,
        "-smp", str(cpus), "-m", str(memory_mb), "-no-shutdown",
        "-drive", f"if=pflash,unit=0,format=raw,readonly=on,file={_q(code)}",
        "-drive", f"if=pflash,unit=1,format=qcow2,file={_q(vars_img)}",
        *device_args(iso, disk, arch),
    ]
    if a.boot_menu:
        cmd += ["-boot", f"menu=on,splash-time={splash_ms if splash_ms is not None else 5000}"]
    cmd += ["-qmp", f"{qmp},server=on,wait=off", *display_args(window, plat)]
    return cmd


def _qmp_spec(wd: Path) -> Tuple[str, str, Optional[str]]:
    """(qemu -qmp spec, client endpoint, private dir to remove on purge). Prefer a unix socket in a 0700
    directory (other local users and web pages cannot reach it); fall back to loopback TCP when the socket
    path would exceed the platform limit."""
    wd.chmod(0o700)
    cand = wd / "qmp.sock"
    if len(os.fsencode(str(cand))) <= SOCKET_PATH_LIMIT:
        return f"unix:{cand}", f"unix:{cand}", None
    try:
        d = tempfile.mkdtemp(prefix="bh")  # 0700 by construction
        sock = os.path.join(d, "q")
        if len(os.fsencode(sock)) <= SOCKET_PATH_LIMIT:
            return f"unix:{sock}", f"unix:{sock}", d
        shutil.rmtree(d, ignore_errors=True)
    except OSError:
        pass
    port = free_loopback_port()
    return f"tcp:127.0.0.1:{port}", f"127.0.0.1:{port}", None


# --- lifecycle ---------------------------------------------------------------------------------
def _run(cmd: Sequence[str]) -> None:
    proc = subprocess.run(list(cmd), capture_output=True, text=True, env=child_env())
    if proc.returncode != 0:
        raise MachineError(f"{Path(cmd[0]).name} failed: {(proc.stderr or proc.stdout).strip()}")


def validate_iso(path: str) -> str:
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise MachineError(f"ISO not found or not a regular file: {p}")
    if p.stat().st_size == 0:
        raise MachineError(f"ISO is empty: {p}")
    return str(p)


def check_iso_image(path: str) -> None:
    """Reject a file that is clearly not a disc image (permissive: only tiny or signature-less files)."""
    p = Path(path).expanduser()
    if not p.is_file():
        return  # validate_iso reports a missing file
    size = p.stat().st_size
    if size < 64 * 1024:
        raise MachineError(f"{p} is only {size} bytes; that is not an installer ISO image")
    with open(p, "rb") as fh:
        fh.seek(0x8001)
        has_iso = fh.read(5) == b"CD001"
        fh.seek(0x8001 + 2048)
        hint = fh.read(5) in (b"BEA01", b"NSR02", b"NSR03", b"CD001", b"TEA01")
        fh.seek(0)
        mbr = fh.read(512)[510:512] == b"\x55\xaa"   # hybrid ISOs and raw disk images
    if not (has_iso or hint or mbr):
        raise MachineError(f"{p} does not look like an ISO image (no ISO9660 'CD001' signature at 0x8001); "
                           "pass the installer's .iso file")


# --- guest architecture: detection and resolution ----------------------------------------------
_SECTOR = 2048
_EFI_LOADERS = {"BOOTX64.EFI": "x86_64", "BOOTAA64.EFI": "aarch64"}


def _iso_dir(fh, extent: int, size: int, joliet: bool) -> Dict[str, Tuple[bool, int, int]]:
    """Entries of one ISO9660 directory: upper-cased name (version suffix removed) -> (is_dir, extent, size)."""
    if size <= 0 or size > 4 * 1024 * 1024:
        return {}
    fh.seek(extent * _SECTOR)
    data = fh.read(size)
    out: Dict[str, Tuple[bool, int, int]] = {}
    pos = 0
    while pos < len(data):
        n = data[pos]
        if n == 0:                                   # records never straddle a sector: skip the padding
            pos = (pos // _SECTOR + 1) * _SECTOR
            continue
        rec = data[pos:pos + n]
        pos += n
        if len(rec) < 34 or len(rec) < 33 + rec[32]:
            break
        raw = rec[33:33 + rec[32]]
        if raw in (b"\x00", b"\x01"):
            continue
        name = raw.decode("utf-16-be" if joliet else "latin-1", errors="replace").upper().split(";")[0].rstrip(".")
        out[name] = (bool(rec[25] & 2), struct.unpack("<I", rec[2:6])[0], struct.unpack("<I", rec[10:14])[0])
    return out


def _iso_efi_loaders(path: str) -> List[str]:
    """Names of the EFI fallback loaders (``BOOTX64.EFI`` / ``BOOTAA64.EFI``) found in ``/EFI/BOOT`` of an
    ISO9660 image. Stdlib only, reads a handful of sectors. [] if the image is not ISO9660 or has none."""
    try:
        with open(path, "rb") as fh:
            roots = []
            for sector in range(16, 24):             # volume descriptors: 1 = primary, 2 = Joliet (supplementary)
                fh.seek(sector * _SECTOR)
                vd = fh.read(_SECTOR)
                if len(vd) < _SECTOR or vd[1:6] != b"CD001" or vd[0] == 255:
                    break
                if vd[0] in (1, 2):
                    joliet = vd[0] == 2 and vd[88:91] in (b"%/@", b"%/C", b"%/E")
                    if vd[0] == 1 or joliet:
                        roots.append((joliet, struct.unpack("<I", vd[158:162])[0], struct.unpack("<I", vd[166:170])[0]))
            for joliet, extent, size in sorted(roots, key=lambda r: not r[0]):
                node = (True, extent, size)
                for part in ("EFI", "BOOT"):
                    entries = _iso_dir(fh, node[1], node[2], joliet)
                    node = entries.get(part)
                    if not node or not node[0]:
                        node = None
                        break
                if node:
                    found = [n for n in _iso_dir(fh, node[1], node[2], joliet) if n in _EFI_LOADERS]
                    if found:
                        return sorted(found)
    except (OSError, struct.error, IndexError):
        pass
    return []


_NAME_HINTS = (("aarch64", re.compile(r"(?<![a-z0-9])(aarch64|arm64)(?![a-z0-9])")),
               ("x86_64", re.compile(r"(?<![a-z0-9])(x86[_-]64|amd64|x64)(?![a-z0-9])")))


def iso_arch(path: str) -> Tuple[Optional[str], str]:
    """(architecture, evidence) of an installer ISO: the EFI loader inside the image when it can be read,
    else words in the file name (``aarch64``, ``arm64``, ``x86_64``, ``amd64``), else (None, '')."""
    loaders = _iso_efi_loaders(path)
    arches = {_EFI_LOADERS[n] for n in loaders}
    if len(arches) == 1:
        return arches.pop(), f"the image contains EFI/BOOT/{loaders[0]}"
    if len(arches) > 1:
        return None, "the image contains EFI loaders for both architectures"
    base = os.path.basename(path).lower()
    hinted = [a for a, rx in _NAME_HINTS if rx.search(base)]
    if len(hinted) == 1:
        return hinted[0], f"the file name says {hinted[0]}"
    return None, ""


def normalize_arch(value: Any) -> str:
    v = str(value or "auto").strip().lower().replace("-", "_")
    if v in ("", "auto"):
        return "auto"
    for a in ARCHES.values():
        if v == a.name or v in a.aliases:
            return a.name
    raise MachineError(f"unknown architecture {value!r}; choose one of {', '.join(ARCH_CHOICES)}")


def resolve_arch(requested: Optional[str], iso: Optional[str], name: str, settings: Mapping[str, Any],
                 fresh: bool = False, host_machine: Optional[str] = None) -> Tuple[str, str]:
    """(guest architecture, how it was decided). ``auto`` means: the ISO's own architecture, else the
    architecture of the machine's existing disk, else the host's. An architecture is fixed for the life of a
    machine's disk (it needs the matching CPU and firmware variable store), so a conflict is refused."""
    want = normalize_arch(requested or os.environ.get("BAREHAND_ARCH") or settings.get("arch") or "auto")
    stored = None if fresh else config.machine_arch(name)
    detected, evidence = iso_arch(iso) if iso else (None, "")
    if want != "auto":
        arch, how = want, "requested"
        if detected and detected != want:
            raise MachineError(
                f"--arch {want} does not match the ISO: {evidence}, so it is a {detected} installer and cannot "
                f"boot on a {want} machine. Use --arch {detected} (or auto), or pick the {want} ISO.")
    elif detected:
        arch, how = detected, f"detected from the ISO ({evidence})"
    elif stored:
        arch, how = stored, "the machine's existing disk"
    else:
        arch, how = host_arch(host_machine), "host-native default"
    if stored and stored != arch:
        raise MachineError(
            f"machine {name!r} already has a {stored} disk and cannot be started as {arch}: a disk and its UEFI "
            f"variables belong to one CPU architecture. Re-create it with `up --fresh --arch {arch}` (deletes "
            f"its disk), or use another machine: `--machine <other-name>`.")
    return arch, how


def prepare_firmware(arch: str, code: str, vars_tpl: str, wd: Path, img: str, vars_img: Path) -> str:
    """Returns the code image to use. On ``virt`` both pflash banks must be exactly 64 MiB: images that are
    smaller (some distros) are padded into copies inside the machine directory; the system files are never
    modified. Creates ``vars_img`` (qcow2, so savevm can snapshot the variable store too) if missing."""
    size = ARCHES[arch].pflash_bytes
    if size and os.path.getsize(code) != size:
        code = padded_copy(code, wd / "code-padded.fd", size)
    if not vars_img.exists():
        src, tmp = vars_tpl, None
        if size and os.path.getsize(vars_tpl) != size:
            tmp = src = padded_copy(vars_tpl, wd / "vars-template-padded.fd", size)
        try:
            _run([img, "convert", "-f", "raw", "-O", "qcow2", src, str(vars_img)])
        finally:
            if tmp:
                os.unlink(tmp)
    return code


def self_check(endpoint: str, budget: float = 6.0) -> Dict[str, Any]:
    """Launch self-check against a silently unusable machine: QMP must answer and a screendump must produce
    a PNG. Retried briefly (the display may need a moment); raises MachineError after ``budget`` seconds."""
    from .qmp import png_size
    deadline, last = time.monotonic() + budget, None
    while True:
        try:
            with QMP(endpoint, timeout=3) as q:
                vm = q.status()
                w, h = png_size(q.screenshot())
            return {"qmp": True, "vm_status": vm, "screendump": [w, h]}
        except Exception as exc:  # noqa: BLE001 - any failure means "not usable yet"
            last = exc
        if time.monotonic() > deadline:
            raise MachineError(f"self-check failed: QMP answered but a screendump did not work ({last})")
        time.sleep(0.3)


def up(*, iso: Optional[str] = None, disk_gb: int = 8, memory_mb: int = 2048, cpus: int = 2,
       window: bool = False, name: str = config.DEFAULT_MACHINE, splash_ms: Optional[int] = None,
       settings: Optional[Mapping[str, Any]] = None, fresh: bool = False,
       arch: Optional[str] = None) -> Dict[str, Any]:
    """Create (first time) or resume a blank UEFI machine and start it. Returns the state dict.
    ``arch``: ``x86_64``, ``aarch64`` or ``auto``/None (see ``resolve_arch``)."""
    if os.name != "posix":
        raise MachineError("Barehand v0 supports Linux and macOS hosts only.")
    st = dict(config.hermes_settings() if settings is None else settings)
    existing = config.read_state(name)
    if existing and config.is_our_qemu(existing):
        raise MachineError(f"machine {name!r} is already running (pid {existing['pid']}, qmp {existing['qmp']}); "
                           "use `hermes barehand down` first")
    if iso:
        iso = validate_iso(iso)
    arch, arch_how = resolve_arch(arch, iso, name, st, fresh=fresh)
    a = ARCHES[arch]
    qemu = qemu_for(st, arch)
    if not qemu:
        raise MachineError(
            f"{a.binary} not found. Install QEMU yourself ({install_hint(arch)}) or set "
            "BAREHAND_QEMU / plugins.entries.barehand.settings.qemu_binary. Barehand never installs software.")
    too_old = check_qemu_version(qemu)
    if too_old:
        raise MachineError(too_old)
    img = find_qemu_img(qemu)
    if not img:
        raise MachineError("qemu-img not found next to QEMU; it ships in the same package.")
    code, vars_tpl = firmware_for(st, qemu, arch)
    if not code or not vars_tpl:
        kinds = ("edk2-x86_64-code.fd + a vars template, or OVMF_CODE.fd/OVMF_VARS.fd" if arch == "x86_64" else
                 "edk2-aarch64-code.fd + edk2-arm-vars.fd, AAVMF_CODE.fd/AAVMF_VARS.fd, or "
                 "QEMU_EFI-pflash.raw/vars-template-pflash.raw")
        env = "BAREHAND_FIRMWARE_CODE and BAREHAND_FIRMWARE_VARS" if arch == "x86_64" else \
            "BAREHAND_FIRMWARE_CODE_AARCH64 and BAREHAND_FIRMWARE_VARS_AARCH64"
        raise MachineError(f"UEFI firmware for {arch} ({kinds}) not found. Install it ({install_hint(arch)}) "
                           f"or set {env}.")

    wd = config.machine_dir(name)
    if fresh and wd.exists():
        shutil.rmtree(wd)
    wd.mkdir(parents=True, exist_ok=True, mode=0o700)
    disk, vars_img = wd / "disk.qcow2", wd / "vars.qcow2"
    if not disk.exists():
        _run([img, "create", "-q", "-f", "qcow2", str(disk), f"{int(disk_gb)}G"])
    code = prepare_firmware(arch, code, vars_tpl, wd, img, vars_img)
    config.set_machine_arch(name, arch)

    accel, cpu = pick_accel(guest=arch)
    if not a.boot_menu:
        splash_ms = None
    elif splash_ms is None:  # the firmware is slow to bring up its keyboard stack under software emulation
        splash_ms = 15000 if accel == "tcg" else 5000
    qmp_spec, endpoint, private_dir = _qmp_spec(wd)
    sock_path = endpoint[5:] if endpoint.startswith("unix:") else None
    if sock_path and os.path.exists(sock_path):
        os.unlink(sock_path)
    cmd = build_command(qemu=qemu, accel=accel, cpu=cpu, qmp=qmp_spec, code=code, vars_img=str(vars_img),
                        disk=str(disk), iso=iso, memory_mb=memory_mb, cpus=cpus, window=window,
                        splash_ms=splash_ms, arch=arch)
    log_path = wd / "qemu.log"
    log = os.fdopen(os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "ab")
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
                            env=child_env(window))
    log.close()
    deadline = time.monotonic() + 30
    while True:
        if proc.poll() is not None:
            tail = log_path.read_text(errors="replace")[-800:]
            raise MachineError(f"qemu exited immediately (code {proc.returncode}): {tail.strip()}")
        try:
            with QMP(endpoint, timeout=2) as probe:
                probe.status()
            break
        except (OSError, ConnectionError, QMPError):
            if time.monotonic() > deadline:
                proc.terminate()
                raise MachineError("QMP did not come up within 30s") from None
            time.sleep(0.1)
    try:
        check = self_check(endpoint)
    except MachineError as exc:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        if private_dir:
            shutil.rmtree(private_dir, ignore_errors=True)
        raise MachineError(f"{exc}; the machine was stopped. See {log_path}") from None
    state = {"name": name, "pid": proc.pid, "qmp": endpoint, "workdir": str(wd), "private_dir": private_dir,
             "iso": iso, "arch": arch, "arch_source": arch_how, "accel": accel, "cpu": cpu, "window": window,
             "splash_ms": splash_ms, "started_at": time.time(), "qemu": qemu, "command": cmd,
             "self_check": check}
    config.write_state(name, state)
    return state


def _target(name: str) -> Tuple[str, Optional[Dict[str, Any]], str]:
    """(endpoint, state, source). A configured endpoint (env or setting: an externally managed QEMU)
    wins; otherwise the machine Barehand started."""
    r = config.resolve()
    if r.endpoint and r.source in ("env", "config"):
        return r.endpoint, None, r.source
    s = config.read_state(name)
    if s and config.is_our_qemu(s):
        return s["qmp"], s, "state"
    if s:
        raise MachineError(f"machine {name!r} has stopped (QEMU exited: powered off by the QEMU process being "
                           f"killed, or it crashed; see {s.get('workdir', '?')}/qemu.log). Start it again with "
                           "`hermes barehand up`.")
    raise MachineError(f"machine {name!r} is not running; start it with `hermes barehand up --iso <path>` "
                       "or attach one via plugins.entries.barehand.settings.qmp")


def connect(name: str = config.DEFAULT_MACHINE, timeout: float = 15) -> QMP:
    endpoint, _, _ = _target(name)
    try:
        return QMP(endpoint, timeout=timeout)
    except QMPBusy as exc:
        raise MachineError(f"QMP is busy: {exc} (an agent action or snapshot is in progress; retry)") from None
    except OSError as exc:
        raise MachineError(f"cannot reach QMP at {endpoint}: {exc}") from None


def status(name: str = config.DEFAULT_MACHINE) -> Dict[str, Any]:
    try:
        endpoint, state, source = _target(name)
    except MachineError as exc:
        s = config.read_state(name)
        if s is None and "is not running" in str(exc):
            return {"name": name, "running": False, "detail": f"no such machine {name!r}; create it with "
                    "`hermes barehand up --iso <path>` (or attach one via the qmp setting)"}
        return {"name": name, "running": False, "detail": str(exc), **({"workdir": s.get("workdir")} if s else {})}
    out: Dict[str, Any] = {"name": name, "running": True, "qmp": endpoint, "endpoint_source": source}
    if state:
        out.update(pid=state.get("pid"), workdir=state.get("workdir"), iso=state.get("iso"),
                   arch=state.get("arch") or "x86_64", accel=state.get("accel"))
    try:
        with QMP(endpoint, timeout=3) as q:
            out["vm_status"] = q.status()
            out.update(facts(q))
    except QMPBusy:
        out["vm_status"] = "busy (another QMP client is mid-operation; the machine is fine)"
    except Exception as exc:
        out["vm_status"] = f"unreachable ({exc})"
    if out.get("vm_status") == "shutdown":
        out["detail"] = "the guest powered off; `reset` powers it back on"
    return out


def facts(q: QMP) -> Dict[str, Any]:
    """Small, factual observations about the machine: is installer media inserted, is the display up, how big.
    Each part is best effort and simply left out on failure."""
    out: Dict[str, Any] = {}
    try:
        drives = [b for b in q.cmd("query-block") if b.get("removable")]
        if drives:
            inserted = next((b["inserted"] for b in drives if b.get("inserted")), None)
            out["installer_media"] = ({"inserted": True, "file": inserted.get("file")} if inserted
                                      else {"inserted": False})
        else:
            out["installer_media"] = {"present": False}
    except Exception:
        pass
    try:
        f = q.frame()
        state = display_state(f)
        # Only QEMU's 640x480 two-colour "Display output is not active" banner means no guest has driven the
        # display yet. A single flat colour is a live but blank display (blanked console, early firmware).
        out["display"] = {"initialised": state != "banner" and not placeholder_shown(f),
                          "size": [f.width, f.height]}
        if state == "blank":
            out["display"]["blank"] = True
    except Exception as exc:
        out["display"] = {"initialised": False, "error": str(exc)}
    return out


def _revive(q: QMP) -> str:
    """After a reset (or load) make sure the VM is actually running. Returns the final status."""
    st = q.status()
    if st not in config.RUNNING_OK:
        try:
            q.cmd("cont")
        except QMPError:
            pass
        st = q.status()
    return st


def hold_escape(q: QMP, seconds: float, interval: float = 0.1) -> int:
    """Send Escape every ``interval`` seconds for ``seconds``: the only dependable way into firmware setup is
    to over-send, because keystrokes before the firmware's keyboard stack is up are lost."""
    end, n = time.monotonic() + seconds, 0
    while time.monotonic() < end:
        q.send_chord(["esc"], hold_ms=40)
        n += 1
        time.sleep(interval)
    return n


def setup_window(name: str = config.DEFAULT_MACHINE) -> float:
    """How long to hold Escape, keyed by (architecture, accelerator): software emulation needs far longer
    than a hardware-accelerated VM, and the aarch64 firmware reads keys much earlier than x86 OVMF."""
    try:
        s = config.read_state(name) or {}
    except config.ConfigError:
        s = {}
    return SETUP_WINDOWS.get((s.get("arch") or "x86_64", accel_kind(s.get("accel"))), 35.0)


# Seconds to wait after a reset for the firmware to bring the display up before declaring it dead, by
# (arch, accel kind). Measured on QEMU 11 + HVF: a healthy aarch64 reset shows a live display after ~1-3 s.
DISPLAY_WAITS = {("x86_64", "tcg"): 45.0, ("x86_64", "hw"): 15.0, ("aarch64", "tcg"): 45.0, ("aarch64", "hw"): 10.0}


def placeholder_shown(frame) -> bool:
    """True for QEMU's "Display output is not active" placeholder at ANY resolution: virtio-gpu keeps the last
    scanout size (1280x800), so the 640x480 test in ``display_state`` misses it. The placeholder is a dark flat
    background with a few light pixels (one line of text) in the middle of the screen."""
    w, h, step = frame.width, frame.height, 3
    if w < 64 or h < 64:
        return False
    stride, bg = w * 3, frame.data[frame.offset:frame.offset + 3]
    if sum(bg) > 90:
        return False
    x0 = y0 = None
    x1 = y1 = -1
    other = set()
    for y in range(0, h, step):
        row = frame.data[frame.offset + y * stride:frame.offset + (y + 1) * stride]
        for x in range(0, w, step):
            px = row[x * 3:x * 3 + 3]
            if px != bg:
                other.add(px)
                if len(other) > 1:
                    return False
                x0 = x if x0 is None or x < x0 else x0
                x1, y1 = max(x1, x), max(y1, y)
                y0 = y if y0 is None else y0
    if x0 is None:
        return False          # one flat colour: a live, blank display
    return x0 > w * 0.2 and x1 < w * 0.8 and y0 > h * 0.25 and y1 < h * 0.75 and (y1 - y0) < h * 0.1


def display_wait(name: str = config.DEFAULT_MACHINE) -> float:
    try:
        s = config.read_state(name) or {}
    except config.ConfigError:
        s = {}
    return DISPLAY_WAITS.get((s.get("arch") or "x86_64", accel_kind(s.get("accel"))), 45.0)


def wait_display(q: QMP, seconds: float, interval: float = 1.0) -> bool:
    """Poll until the display is not the placeholder (True) or ``seconds`` pass (False)."""
    end = time.monotonic() + seconds
    while True:
        try:
            if not placeholder_shown(q.frame()):
                return True
        except Exception:
            pass
        if time.monotonic() >= end:
            return False
        time.sleep(interval)


def command_without_iso(cmd: List[str]) -> List[str]:
    """``cmd`` with the CD drive (id=cd0) left empty: the installer ISO is not inserted again."""
    out = list(cmd)
    for i, a in enumerate(out):
        if a.startswith("if=none,id=cd0,") and ",file=" in a:
            out[i] = "if=none,id=cd0,media=cdrom,readonly=on"
    return out


def _relaunch(name: str, prepare: Optional[Callable[[Dict[str, Any]], None]] = None) -> None:
    """Stop the QEMU process and start it again with the exact same command line (same disk, UEFI vars,
    devices, QMP endpoint). Snapshots live in the disk image and survive. Used when a reset leaves the firmware
    wedged with no display (observed on QEMU 11 + HVF after savevm). ``prepare(state)`` runs while QEMU is not
    running (used to roll the images back to a disk checkpoint)."""
    s = config.read_state(name)
    if not s or not s.get("command") or not config.is_our_qemu(s):
        raise MachineError("cannot relaunch: the machine is not one Barehand started")
    pid, endpoint = int(s["pid"]), s["qmp"]
    try:
        with QMP(endpoint, timeout=5) as q:
            q.cmd("quit")
    except Exception:
        pass
    for _ in range(50):
        if not config.is_our_qemu(s):
            break
        time.sleep(0.1)
    else:
        try:
            os.kill(pid, 9)
        except OSError:
            pass
        time.sleep(0.3)
    if endpoint.startswith("unix:") and os.path.exists(endpoint[5:].split(",")[0]):
        os.unlink(endpoint[5:].split(",")[0])
    if prepare:
        prepare(s)
    log = os.fdopen(os.open(Path(s["workdir"]) / "qemu.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "ab")
    cmd = command_without_iso(s["command"]) if s.get("ejected") else s["command"]
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
                            env=child_env(bool(s.get("window"))))
    log.close()
    deadline = time.monotonic() + 30
    while True:
        if proc.poll() is not None:
            raise MachineError(f"qemu exited on relaunch (code {proc.returncode}); see {s['workdir']}/qemu.log")
        try:
            with QMP(endpoint, timeout=2) as probe:
                probe.status()
            break
        except (OSError, ConnectionError, QMPError):
            if time.monotonic() > deadline:
                proc.terminate()
                raise MachineError("QMP did not come back within 30s after relaunch") from None
            time.sleep(0.1)
    config.write_state(name, {**{k: v for k, v in s.items() if k != "snapshotted"},
                              "pid": proc.pid, "started_at": time.time()})


def reset(name: str = config.DEFAULT_MACHINE, enter_setup: bool = False, setup_seconds: Optional[float] = None
          ) -> str:
    """Hard reset (also powers a shut-down machine back on). With ``enter_setup`` it then holds Escape for
    ``setup_seconds`` so the firmware lands in its setup menu. The display is verified afterwards: if the
    firmware never redraws (QEMU's "Display output is not active" placeholder persists) the machine is
    relaunched from the same disk and vars, and the result says so."""
    secs = setup_window(name) if setup_seconds is None else float(setup_seconds)
    wait = display_wait(name)

    def boot_and_check(q: QMP) -> Tuple[str, str, int]:
        st = _revive(q)
        n = hold_escape(q, secs) if enter_setup else 0
        if not enter_setup:
            time.sleep(1.0)     # let the scanout drop to the placeholder first, or a stale frame reads as live
        return st, ("live" if wait_display(q, wait) else "dead"), n

    how = ""
    try:
        # Relaunching is only for the machine Barehand itself started (the state file is the endpoint source);
        # an attached endpoint (env/setting) may be someone else's QEMU.
        ours = _target(name)[2] == "state"
    except MachineError:
        ours = False
    try:
        s0 = config.read_state(name) or {}
        wedge_prone = ours and bool(s0.get("snapshotted")) and (s0.get("arch") or "x86_64") == "aarch64"
    except config.ConfigError:
        wedge_prone = False
    if wedge_prone:
        # QEMU 11 on aarch64 + HVF: after savevm/loadvm in this process, a system_reset leaves the firmware
        # spinning in early boot forever (display stays 'not active'; verified 10/10). A fresh process is fine.
        with connect(name):
            pass            # fail early with the usual explanation if the machine is gone
        _relaunch(name)
        how = "; relaunched QEMU (same disk/vars/snapshots) because a snapshot was taken or loaded in this " \
              "process and QEMU's reset is unreliable after that; guest RAM state is lost, it cold-booted"
        with connect(name) as q:
            st, disp, n = boot_and_check(q)
    else:
        with connect(name) as q:
            q.cmd("system_reset")
            st, disp, n = boot_and_check(q)
    if disp == "dead" and not wedge_prone and not ours:
        how = "; this machine is attached from outside Barehand, so it was not relaunched"
    elif disp == "dead" and not wedge_prone:
        try:
            _relaunch(name)
            how = "; the display stayed on QEMU's 'Display output is not active' placeholder after the reset (a second reset does not help), " \
                  "so the QEMU process was relaunched (same disk/vars/snapshots; guest RAM state is lost, it cold-booted)"
            with connect(name) as q:
                st, disp, n = boot_and_check(q)
        except MachineError as exc:
            how = f"; relaunch failed: {exc}"
    if disp == "dead":
        fix = ("restart that machine yourself" if not ours else
               "run `hermes barehand down` if QEMU is still running, then `hermes barehand up` (it works when "
               "QEMU is already gone; power_off needs a live QEMU)")
        return f"reset done but the display is still dead{how} (vm {st}). Do not keep resetting: {fix}"
    note = f"; sent {n} Escape presses over {secs:.0f}s; capture to confirm setup is showing" if enter_setup else ""
    return f"reset{note}{how} (vm {st}, display live)"


def powerdown(name: str = config.DEFAULT_MACHINE) -> None:
    """Graceful ACPI power button press (the guest OS shuts down). Firmware menus ignore it; use reset."""
    with connect(name) as q:
        q.cmd("system_powerdown")


def eject(name: str = config.DEFAULT_MACHINE) -> str:
    with connect(name) as q:
        media = [b for b in q.cmd("query-block") if b.get("removable") and b.get("inserted")]
        if not media:
            raise MachineError("eject failed: no removable media is inserted")
        dev = media[0]
        try:
            if dev.get("qdev"):
                q.cmd("eject", id=dev["qdev"], force=True)
            else:
                q.cmd("eject", device=dev["device"], force=True)
        except QMPError as exc:
            raise MachineError(f"eject failed: {exc}") from None
        try:    # a relaunch must not put the installer back
            if _target(name)[2] == "state":
                s = config.read_state(name)
                if s:
                    config.write_state(name, {**s, "ejected": True})
        except Exception:
            pass
        return f"ejected {dev.get('device') or dev.get('qdev')}"


_SNAP_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def _check_snapshot_name(tag: str) -> str:
    if not isinstance(tag, str) or not _SNAP_NAME.fullmatch(tag):
        raise MachineError("snapshot names must be 1-64 chars of letters, digits, '.', '_' or '-'")
    return tag


def snapshot_tags(listing: str) -> List[str]:
    """Tags from ``info snapshots`` output (ID column is '--' or a number, then the tag)."""
    tags = []
    for line in listing.splitlines():
        m = re.match(r"^(\S+)\s+(\S+)\s+\d", line)
        if m and m.group(1) != "ID":
            tags.append(m.group(2))
    return tags


def _mark_snapshotted(name: str) -> None:
    """Remember that savevm/loadvm ran in this QEMU process (see ``reset``). Best effort."""
    try:
        s = config.read_state(name)
        if s:
            config.write_state(name, {**s, "snapshotted": True})
    except Exception:
        pass


DISK_DEVICES = ("pflash1", "hd0")   # UEFI variable store and system disk: the drives a checkpoint covers


def disk_checkpoints_apply(state: Optional[Mapping[str, Any]]) -> bool:
    """Snapshots are disk checkpoints on aarch64 with HVF for a machine Barehand
    started. QEMU 11 + HVF wedges the guest after savevm/loadvm in the same process (UEFI variable writes and
    reset hang), and a disk-only checkpoint does not."""
    return bool(state) and (state.get("arch") or "x86_64") == "aarch64" and \
        state.get("accel") == "hvf"


def _images(q: QMP) -> Dict[str, Dict[str, int]]:
    """{device: {tag: vm_state_size}} for the checkpoint drives, from query-block."""
    out: Dict[str, Dict[str, int]] = {}
    for d in q.cmd("query-block"):
        if d.get("device") in DISK_DEVICES:
            snaps = (d.get("inserted") or {}).get("image", {}).get("snapshots") or []
            out[d["device"]] = {x["name"]: int(x.get("vm-state-size", 0)) for x in snaps}
    return out


def checkpoint_tags(q: QMP) -> Dict[str, str]:
    """{tag: 'disk' or 'full'}: a snapshot with saved VM state is 'full' (savevm), without it 'disk'."""
    imgs = _images(q)
    tags: Dict[str, str] = {}
    for dev in DISK_DEVICES:
        for tag, vm in imgs.get(dev, {}).items():
            if vm > 0:
                tags[tag] = "full"
            else:
                tags.setdefault(tag, "disk")
    return tags


def _delete_disk_checkpoint(q: QMP, tag: str) -> None:
    for dev in DISK_DEVICES:
        try:
            q.cmd("blockdev-snapshot-delete-internal-sync", device=dev, name=tag)
        except QMPError:
            pass   # absent on one drive: fine


def _disk_checkpoint(action: str, tag: Optional[str], name: str, overwrite: bool) -> str:
    with connect(name) as q:
        tags = checkpoint_tags(q)
        if action == "list":
            if not tags:
                return "(no snapshots)"
            return "\n".join(f"{t}  {'disk checkpoint' if k == 'disk' else 'full snapshot (RAM + disk)'}"
                             for t, k in sorted(tags.items()))
        tag = _check_snapshot_name(tag or "")
        kind = tags.get(tag)
        if action in ("load", "delete") and kind is None:
            raise MachineError(f"snapshot {tag}: no snapshot named {tag!r}")
        if action == "save":
            if kind and not overwrite:
                raise MachineError(f"snapshot {tag!r} already exists; pick another name, or pass --overwrite "
                                   "(CLI) / overwrite: true (tool)")
            if kind == "full":
                raise MachineError(f"snapshot {tag!r} is a full snapshot; delete it first or pick another name")
            if kind:
                _delete_disk_checkpoint(q, tag)
            was = q.status()
            q.cmd("stop")      # both drives are checkpointed at one instant
            try:
                q.cmd("transaction", actions=[
                    {"type": "blockdev-snapshot-internal-sync", "data": {"device": d, "name": tag}}
                    for d in DISK_DEVICES])
            except QMPError as exc:
                _delete_disk_checkpoint(q, tag)
                raise MachineError(f"checkpoint {tag}: {exc}") from None
            finally:
                if was in config.RUNNING_OK:
                    q.cmd("cont")
            return (f"snapshot {tag!r} save ok (disk checkpoint of the system disk and the UEFI variables, taken "
                    "while the guest was paused; it is crash-consistent: the guest was not asked to sync, and "
                    "memory is not saved)")
        if action == "delete":
            if kind == "full":
                raise MachineError(f"snapshot {tag!r} is a full snapshot; it cannot be deleted from the "
                                   "running machine here")
            _delete_disk_checkpoint(q, tag)
            return f"snapshot {tag!r} delete ok"
    # load
    if kind == "full":
        return _full_snapshot("load", tag, name, overwrite)
    st = config.read_state(name) or {}
    img = find_qemu_img((st.get("command") or [None])[0])
    if not img:
        raise MachineError("qemu-img not found; cannot restore the disk checkpoint")
    wd = Path(st["workdir"])

    def roll_back(_s: Dict[str, Any]) -> None:
        for f in ("vars.qcow2", "disk.qcow2"):
            _run([img, "snapshot", "-a", tag, str(wd / f)])

    _relaunch(name, roll_back)
    return (f"snapshot {tag!r} load ok: restored the disk to checkpoint {tag!r} and restarted the machine: it is "
            "booting from that disk state; memory state is not part of a checkpoint on this host")


def snapshot(action: str, tag: Optional[str] = None, name: str = config.DEFAULT_MACHINE,
             overwrite: bool = False) -> str:
    """savevm / loadvm / delvm / list through the human monitor, or, on aarch64 with hardware acceleration,
    disk checkpoints (see ``disk_checkpoints_apply``)."""
    try:
        _, state, source = _target(name)
    except MachineError:
        state, source = None, ""
    if source == "state" and disk_checkpoints_apply(state):
        return _disk_checkpoint(action, tag, name, overwrite)
    return _full_snapshot(action, tag, name, overwrite)


def _full_snapshot(action: str, tag: Optional[str], name: str, overwrite: bool) -> str:
    """savevm / loadvm / delvm / list through the human monitor. HMP prints errors as plain text with a 0
    status, so the output is inspected rather than trusted."""
    with connect(name) as q:
        if action == "list":
            return q.hmp("info snapshots").strip() or "(no snapshots)"
        tag = _check_snapshot_name(tag or "")
        verb = {"save": "savevm", "load": "loadvm", "delete": "delvm"}.get(action)
        if verb is None:
            raise MachineError(f"unknown snapshot action {action!r}")
        exists = tag in snapshot_tags(q.hmp("info snapshots"))
        if action in ("load", "delete") and not exists:
            # loadvm of a missing tag leaves QEMU stuck in `restore-vm`, so check the tag first.
            raise MachineError(f"{verb} {tag}: no snapshot named {tag!r}")
        if action == "save" and exists and not overwrite:
            raise MachineError(f"snapshot {tag!r} already exists; pick another name, or pass --overwrite "
                               "(CLI) / overwrite: true (tool)")
        out = q.hmp(f"{verb} {tag}", timeout=300).strip()
        if action == "load":
            _revive(q)  # a failed or partial restore must never leave the machine frozen
        if out:  # success is silent
            raise MachineError(f"{verb} {tag}: {out}")
        if action in ("save", "load"):
            _mark_snapshotted(name)
            st = config.read_state(name) or {}
            if (st.get("arch") or "x86_64") == "aarch64" and st.get("accel") == "hvf":
                # Measured (QEMU 11.1.3 + HVF): after savevm/loadvm in this process, the first guest write to a
                # UEFI variable (efibootmgr, grub-install, an installer's boot entry) wedges the guest at 100% CPU.
                return (f"snapshot {tag!r} {action} ok. Warning: on this aarch64 + HVF machine a guest write to "
                        "UEFI variables (efibootmgr, grub-install, installer boot entries) hangs the guest after "
                        "a snapshot in the same QEMU process; take snapshots before the install step that writes "
                        "the boot entry, or use `--removable` / `--no-nvram` for GRUB, or `down` + `up` to "
                        "restart QEMU first")
        return f"snapshot {tag!r} {action} ok"


def down(name: str = config.DEFAULT_MACHINE, purge: bool = False) -> str:
    r = config.resolve()
    if r.endpoint and r.source in ("env", "config"):
        raise MachineError("the machine is attached through BAREHAND_QMP / the qmp setting and is not managed "
                           "by Barehand; stop it yourself")
    s = config.read_state(name)
    if not s:
        return f"machine {name!r}: nothing to stop"
    pid = s.get("pid")
    ours = config.is_our_qemu(s)  # never signal a pid that is not the QEMU we started
    if ours:
        try:
            with QMP(s["qmp"], timeout=3) as q:
                q.cmd("quit")
        except Exception:
            if config.is_our_qemu(s):
                try:
                    os.kill(pid, 15)
                except OSError:
                    pass
        deadline = time.monotonic() + 10
        while config.pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        if config.is_our_qemu(s):
            try:
                os.kill(pid, 9)
            except OSError:
                pass
    config.clear_state(name)
    for d in (s.get("private_dir"),):
        if d:
            shutil.rmtree(d, ignore_errors=True)
    if purge:
        shutil.rmtree(config.machine_dir(name), ignore_errors=True)
        return f"machine {name!r} stopped and its disk deleted"
    return f"machine {name!r} stopped (disk kept; `up` resumes it)"
