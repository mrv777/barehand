"""The transport seam: one function returns the object the backend drives (frame, send_chord, click, drag, wheel,
watch, status, close). ``QMP`` implements it for QEMU; ``VNCTransport`` (transport_vnc.py) for any VNC server.
Everything that only a hypervisor can do is refused over VNC with one shared message."""

from __future__ import annotations

from typing import Any

from . import config

VNC_ONLY_MSG = ("not available over the VNC transport: VNC exposes only a display, a keyboard and a pointer, not "
                "the hypervisor (no snapshots, reset, power or media control; use the hypervisor's own tools)")


def unavailable(action: str) -> str:
    return f"{action}: {VNC_ONLY_MSG}"


def open_transport(resolved: "config.Resolved", *, qmp_timeout: float = 15.0) -> Any:
    """Connect to the resolved endpoint. Raises OSError for an unreachable one, ``QMPBusy`` (QMP) or
    ``VNCError`` (auth, protocol) otherwise."""
    if resolved.kind == "vnc":
        from .transport_vnc import VNCTransport
        return VNCTransport.from_settings({**resolved.settings, "vnc": str(resolved.endpoint)})
    from .qmp import QMP
    return QMP(str(resolved.endpoint), timeout=qmp_timeout)
