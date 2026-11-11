"""Barehand: a ``computer_use`` provider that drives a machine through its screen and keyboard/mouse
alone, with no software on the target. Transports: QEMU over QMP (loopback only) or VNC
(RFB; tested only against QEMU's own VNC server).

Select it with ``computer_use.backend: barehand`` in config.yaml. Optionally enable the plugin
(``plugins.enabled: [barehand]``) for ``hermes barehand ...``, the ``barehand_machine`` tool and guidance.
"""

from __future__ import annotations

from tools.computer_use.backend import ComputerUseBackend, ComputerUseProvider

from .backend import BarehandBackend


class BarehandProvider(ComputerUseProvider):
    name = "barehand"
    display_name = "Barehand (raw machine display over QEMU QMP or VNC)"

    def create_backend(self, *, permission_mode: str) -> ComputerUseBackend:
        # permission_mode is informational: approvals are enforced by the tool, never by the driver.
        return BarehandBackend(permission_mode=permission_mode)

    def is_available(self) -> bool:
        """A machine endpoint is configured, or QEMU is on this host so one can be started. No spawning."""
        return BarehandBackend().is_available()

    def doctor(self) -> int:
        from .doctor import run_doctor
        return run_doctor()


def register(ctx):
    """One directory, two roles. The computer-use loader hands a stub context that has
    ``register_computer_use_provider``; the ordinary plugin manager (when ``barehand`` is listed in
    ``plugins.enabled``; plugin.yaml pins ``kind: standalone`` so it is not skipped as exclusive) hands a real
    PluginContext, which gets the machine tool, approval hook, CLI, slash command, skill and prompt section.
    The provider registration is guarded because the validator's probe context lacks the method."""
    reg = getattr(ctx, "register_computer_use_provider", None)
    if reg is not None:
        reg(BarehandProvider())
    from . import companion
    companion.register(ctx)
