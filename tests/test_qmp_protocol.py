"""QMP client robustness: async events, split reads, busy server, greeting problems."""
import json
import socket
import threading
import time

import pytest

from barehand import qmp
from barehand.qmp import QMP, QMPBusy, QMPError


def test_events_interleaved_with_responses_are_skipped(fake):
    fake.events = True
    with QMP(fake.address) as q:
        assert q.status() == "running"
        assert q.screenshot()[:4] == b"\x89PNG"


def test_responses_split_across_many_reads(fake):
    fake.chunked = True
    with QMP(fake.address) as q:
        assert q.status() == "running"


def test_several_lines_in_one_chunk():
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
    port = srv.getsockname()[1]
    def serve():
        c, _ = srv.accept()
        c.sendall(b'{"QMP": {}}\n')
        c.recv(1000)  # qmp_capabilities
        c.sendall(b'{"return": {}}\n')
        c.recv(1000)  # query-status
        c.sendall(b'{"event": "A"}\n{"event": "B"}\n{"return": {"status": "paused"}}\n')
        time.sleep(0.3); c.close()
    threading.Thread(target=serve, daemon=True).start()
    with QMP(f"127.0.0.1:{port}") as q:
        assert q.status() == "paused"
    srv.close()


def test_error_objects_become_qmperror(fake):
    fake.fail_commands["screendump"] = "nope"
    with QMP(fake.address) as q:
        with pytest.raises(QMPError, match="nope"):
            q.screenshot()


def test_second_client_waits_then_reports_busy(fake):
    first = QMP(fake.address)
    try:
        t = time.monotonic()
        with pytest.raises(QMPBusy):
            QMP(fake.address, timeout=0.6)
        assert time.monotonic() - t < 3
    finally:
        first.close()
    QMP(fake.address).close()  # the slot is free again


def test_garbage_greeting_is_rejected():
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
    port = srv.getsockname()[1]
    threading.Thread(target=lambda: (lambda c: (c.sendall(b"HTTP/1.1 400\n"), time.sleep(0.2), c.close()))(srv.accept()[0]),
                     daemon=True).start()
    with pytest.raises((ConnectionError, ValueError)):
        QMP(f"127.0.0.1:{port}")
    srv.close()


def test_connection_closed_mid_command(fake):
    with QMP(fake.address) as q:
        fake.stop()
        with pytest.raises((ConnectionError, OSError)):
            q.status()


def _short_dir():
    import tempfile
    return tempfile.mkdtemp(prefix="bh")      # unix socket paths are limited to ~104 bytes


def test_unix_socket_busy_semantics():
    path = _short_dir() + "/q"
    srv = socket.socket(socket.AF_UNIX); srv.bind(path); srv.listen(1)
    def serve_one():
        c, _ = srv.accept(); srv.close()           # like QEMU: stop listening while a client is connected
        c.sendall(b'{"QMP": {}}\n'); c.recv(100); c.sendall(b'{"return": {}}\n'); time.sleep(0.8); c.close()
    threading.Thread(target=serve_one, daemon=True).start()
    first = QMP(f"unix:{path}", timeout=2)
    with pytest.raises(QMPBusy):
        QMP(f"unix:{path}", timeout=0.4)
    first.close()


def test_missing_unix_socket_is_not_busy():
    with pytest.raises(OSError):
        QMP(f"unix:{_short_dir()}/none", timeout=0.3)
