"""`hermes barehand demo` (bundled recording, no QEMU/model) and `screen` (screen text without a vision model)."""

import argparse
import os
from pathlib import Path

import pytest

from barehand import cli, machine

MAX_BUNDLE_BYTES = 1_000_000
FORBIDDEN = (b"vm-throwaway", b"/Users/", b"/private/tmp")


def _args(*argv):
    p = argparse.ArgumentParser()
    cli.setup(p)
    return p.parse_args(list(argv))


def test_demo_no_open_prints_existing_path(capsys, monkeypatch):
    import webbrowser
    monkeypatch.setattr(webbrowser, "open", lambda *a, **k: pytest.fail("must not open a browser"))
    assert cli.handle(_args("demo", "--no-open")) == 0
    out = capsys.readouterr().out
    index = next(l for l in out.splitlines() if l.endswith("index.html"))
    assert os.path.isabs(index) and os.path.isfile(index)
    assert "file://" in out


def test_demo_opens_browser_by_default(capsys, monkeypatch):
    import webbrowser
    seen = []
    monkeypatch.setattr(webbrowser, "open", lambda url, *a, **k: seen.append(url) or True)
    assert cli.handle(_args("demo")) == 0
    assert seen and seen[0].startswith("file://") and seen[0].endswith("index.html")


def test_demo_bundle_is_small_and_clean():
    d = Path(cli.DEMO_DIR)
    assert (d / "index.html").is_file() and (d / "ledger.js").is_file()
    assert len(list((d / "frames").glob("*.png"))) >= 10
    files = [p for p in d.rglob("*") if p.is_file()]
    assert sum(p.stat().st_size for p in files) < MAX_BUNDLE_BYTES
    for p in files:
        data = p.read_bytes()
        for bad in FORBIDDEN:
            assert bad not in data, f"{bad!r} in {p}"
    assert b'"redacted": true' in (d / "ledger.js").read_bytes()


def _bundle(name):
    return Path(cli.DEMO_DIR).parent / name


def test_both_bundles_sizes_and_frames():
    import re
    root = Path(cli.DEMO_DIR).parent
    total = sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
    assert total <= 1_200_000
    for name, limit in (("rescue-grub", 300_000), ("debian-install", 900_000)):
        d = _bundle(name)
        files = [p for p in d.rglob("*") if p.is_file()]
        assert sum(p.stat().st_size for p in files) <= limit, name
        for p in files:
            data = p.read_bytes()
            for bad in FORBIDDEN:
                assert bad not in data, f"{bad!r} in {p}"
        js = (d / "ledger.js").read_text()
        refs = set(re.findall(r'"frames/[^"]+\.png"', js))
        assert refs
        for r in refs:
            assert (d / r.strip('"')).is_file(), (name, r)


def test_demo_debian_no_open_and_list(capsys, monkeypatch):
    import webbrowser
    monkeypatch.setattr(webbrowser, "open", lambda *a, **k: pytest.fail("must not open a browser"))
    assert cli.handle(_args("demo", "debian", "--no-open")) == 0
    out = capsys.readouterr().out
    index = next(l for l in out.splitlines() if l.endswith("index.html"))
    assert "debian-install" in index and os.path.isfile(index)
    assert cli.handle(_args("demo", "--list")) == 0
    listing = capsys.readouterr().out
    assert "rescue" in listing and "debian" in listing


def test_screen_prints_text_block_and_writes_png(fake, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("BAREHAND_QMP", fake.address)
    png = tmp_path / "s.png"
    assert cli.handle(_args("screen", "--png", str(png))) == 0
    out = capsys.readouterr().out
    assert "screenshot written" in out and png.read_bytes().startswith(b"\x89PNG")
    assert out.strip()


def test_screen_fails_clearly_without_machine(capsys):
    assert cli.handle(_args("screen")) in (1, 2)
    assert capsys.readouterr().err.startswith("barehand:")


def test_bundled_viewers_are_the_current_template_and_carry_meta():
    from barehand import ledger
    for name in ("rescue-grub", "debian-install"):
        d = _bundle(name)
        assert (d / "index.html").read_text() == ledger.VIEWER, name
        js = (d / "ledger.js").read_text()
        assert js.count("BAREHAND.meta=") == 1 and "Recorded run:" in js, name
        assert "<" not in js and ">" not in js and "&" not in js
