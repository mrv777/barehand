"""Test setup: project root on sys.path; Hermes base classes via PYTHONPATH (repo checkout), never installed."""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import tools.computer_use.backend  # noqa: F401
except ImportError:  # pragma: no cover
    pytest.exit("Hermes source not importable: set PYTHONPATH to a hermes-agent checkout "
                "(tools.computer_use.backend must import).", returncode=3)


@pytest.fixture(autouse=True)
def scratch_home(tmp_path, monkeypatch):
    """Every test gets its own plugin data dir and a clean environment: nothing touches ~/.hermes."""
    monkeypatch.setenv("BAREHAND_HOME", str(tmp_path / "bh-home"))
    # Hermes code the tests import must never see (or write under) the real ~/.hermes.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    real = Path.home() / ".hermes"
    assert not str(tmp_path).startswith(str(real))
    for var in ("BAREHAND_QMP", "BAREHAND_MACHINE", "BAREHAND_QEMU", "BAREHAND_FIRMWARE_CODE",
                "BAREHAND_FIRMWARE_VARS"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


@pytest.fixture
def fake(request):
    from fake_qmp import FakeQMP
    server = FakeQMP()
    server.start()
    yield server
    server.stop()


@pytest.fixture
def backend(fake):
    from barehand.backend import BarehandBackend
    settings = {"qmp": fake.address, "settle_quiet": 0.15, "settle_timeout": 1.0,
                "settle_delay": 0.02, "settle_poll": 0.03}
    return BarehandBackend(settings=settings)


class VClock:
    """A virtual clock: ``sleep`` advances it instantly, so settle/watch timing is a pure function of the polls
    made (logical time) and never of how loaded the host is. Frame fetches cost no virtual time."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(0.0, seconds)


@pytest.fixture
def vclock(monkeypatch, request):
    """Route the watch/settle code (qmp.py, backend.py) and the requesting test module's own ``time`` through
    VClock."""
    from barehand import backend as backend_mod
    from barehand import qmp as qmp_mod
    clock = VClock()
    for mod in (qmp_mod, backend_mod):
        monkeypatch.setattr(mod, "time", clock)
    if hasattr(request.module, "time"):
        monkeypatch.setattr(request.module, "time", clock)
    return clock


def on_frames(fake, hook):
    """Call ``hook()`` before every frame the fake QMP server serves (a virtual-time trigger: with ``vclock`` the
    frame fetches are exactly the watch loop's polls). The hook may paint, fill or resize the fake screen."""
    inner = fake._frame

    def frame():
        hook()
        return inner()
    fake._frame = frame


def at_virtual(fake, vclock, delay, action):
    """Run ``action()`` once, on the first frame fetched at or after ``delay`` virtual seconds from now (the
    virtual-time replacement for ``threading.Timer(delay, action)``)."""
    due = vclock.now + delay
    fired = []

    def hook():
        if not fired and vclock.now >= due:
            fired.append(True)
            action()
    on_frames(fake, hook)
