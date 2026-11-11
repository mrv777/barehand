"""Session ledger / flight recorder: an append-only JSONL record of what was done to the machine, plus the
frames before and after each action, and a zero-dependency ``index.html`` viewer next to them.

Layout, under the plugin data dir (``<data dir>/recordings/``)::

    20261110-101500-default-p4242-a1b2c3/   one folder per Hermes process that used the machine; the name
                                            carries the pid and a random tag, so it is never reused
        ledger.jsonl                        one JSON object per line, one line per action (0600)
        ledger.js                           the same records as ``BAREHAND.push({...});`` lines (0600)
        frames/0001-1a2b3c4d5e6f.png        native-resolution PNG, once per distinct frame digest (0600)
        index.html                          static viewer, written once; loads ledger.js (works from file://)
        .lock                               held (flock) by the writing process while it lives

What is recorded: every action that reached the backend (input, capture, wait, a ``barehand_machine`` call) and
every input it rejected (with the reason), with exact arguments, the frames seen before and after, the verdict
and the message. Typed strings are stored only when ``record_typed_text`` is on (default OFF: a length-only
placeholder is written). Key chords and the screenshots themselves are always stored: a screenshot shows
whatever the guest echoes. ``record: off`` disables the recorder. Files are 0600, folders 0700.

Bounds: at most ``record_keep_sessions`` (10) sessions are kept and their total size stays under
``record_max_total_mb`` (checked when a session starts); a session stops storing frames after
``record_max_frames`` (2000) or ``record_max_mb`` (200 MiB) and says so once in its ledger; it stops
recording altogether after ``MAX_RECORDS`` records. A session whose process is alive (its ``.lock`` is held) is
never pruned or purged.

The recorder must never break an action: every public entry point swallows its own errors and logs the first
one. ``text`` is a per-record field reserved for the screen text layer (null for now).
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import random
import re
import shutil
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

try:  # POSIX; the plugin does not support Windows in v0
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

from . import config
from .frames import Frame
from .imaging import frame_to_png

log = logging.getLogger("barehand.ledger")

LEDGER_FILE = "ledger.jsonl"
JS_FILE = "ledger.js"
INDEX_FILE = "index.html"
FRAMES_DIR = "frames"
LOCK_FILE = ".lock"
DEFAULT_KEEP = 10
DEFAULT_MAX_FRAMES = 2000
DEFAULT_MAX_MB = 200
DEFAULT_MAX_TOTAL_MB = 2000
MAX_RECORDS = 20000
SCHEMA = 1
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
MiB = 1024 * 1024

_WARNED: Set[str] = set()


def _warn_once(key: str, msg: str, *args: Any) -> None:
    if key not in _WARNED:
        _WARNED.add(key)
        log.warning(msg, *args)


def _flag(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


# --- settings -------------------------------------------------------------------------------------------
@dataclass
class RecorderSettings:
    enabled: bool = True
    typed_text: bool = False
    keep: int = DEFAULT_KEEP
    max_frames: int = DEFAULT_MAX_FRAMES
    max_bytes: int = DEFAULT_MAX_MB * MiB
    max_total_bytes: int = DEFAULT_MAX_TOTAL_MB * MiB


def _int_setting(st: Mapping[str, Any], key: str, default: int, lo: int, hi: int) -> int:
    raw = st.get(key)
    if raw is None or raw == "":
        return default
    try:
        if isinstance(raw, bool) or (isinstance(raw, float) and raw != int(raw)):
            raise ValueError
        v = int(raw)
        if not lo <= v <= hi:
            raise ValueError
        return v
    except (TypeError, ValueError):
        _warn_once(f"setting:{key}:{raw!r}", "barehand: setting %s=%r is invalid (an integer from %d to %d "
                                              "is needed); using %d", key, raw, lo, hi, default)
        return default


def parse_settings(st: Mapping[str, Any]) -> RecorderSettings:
    """The ``record_*`` settings, validated: a bad value falls back to its default and is logged once."""
    return RecorderSettings(
        enabled=_flag(st.get("record"), True),
        typed_text=_flag(st.get("record_typed_text"), False),
        keep=_int_setting(st, "record_keep_sessions", DEFAULT_KEEP, 1, 10000),
        max_frames=_int_setting(st, "record_max_frames", DEFAULT_MAX_FRAMES, 0, 10_000_000),
        max_bytes=_int_setting(st, "record_max_mb", DEFAULT_MAX_MB, 1, 1_000_000) * MiB,
        max_total_bytes=_int_setting(st, "record_max_total_mb", DEFAULT_MAX_TOTAL_MB, 1, 10_000_000) * MiB)


# --- files ----------------------------------------------------------------------------------------------
def recordings_path() -> Path:
    """``<data dir>/recordings`` without creating it."""
    return config.data_dir() / "recordings"


def recordings_dir(create: bool = False) -> Path:
    root = recordings_path()
    if create:
        _private_dir(root)
    return root


def _private_dir(path: Path) -> None:
    """A real directory (never a symlink) with mode 0700; an existing one we own is tightened."""
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode):
        raise OSError(f"{path} is not a plain directory")
    if st.st_uid == os.geteuid() and stat.S_IMODE(st.st_mode) & 0o077:
        os.chmod(path, 0o700)


def _open_private(path: Path, flags: int) -> int:
    fd = os.open(path, flags | _NOFOLLOW | _CLOEXEC, 0o600)
    try:
        os.fchmod(fd, 0o600)   # an existing file we own is tightened too
    except OSError:
        pass
    return fd


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        view = view[n:]


def _write_file(path: Path, data: bytes) -> None:
    fd = _open_private(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    try:
        _write_all(fd, data)
    finally:
        os.close(fd)


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    try:
        _write_file(tmp, data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _recover_tail(path: Path) -> bool:
    """Terminate a trailing partial line (an earlier write was cut short) so the next record starts on its own
    line. Returns whether anything was repaired."""
    try:
        size = path.stat().st_size
        if size == 0:
            return False
        with open(path, "rb") as fh:
            fh.seek(size - 1)
            if fh.read(1) == b"\n":
                return False
        fd = _open_private(path, os.O_WRONLY | os.O_APPEND)
        try:
            _write_all(fd, b"\n")
        finally:
            os.close(fd)
        return True
    except OSError:
        return False


def _js_json(obj: Any) -> str:
    """JSON that is safe inside any HTML/JS context: ASCII only, and no ``<``, ``>``, ``&`` or U+2028/2029."""
    return (json.dumps(obj, ensure_ascii=True).replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("&", "\\u0026").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def _safe_name(machine: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", machine)[:48] or "machine"


def session_dirs(root: Path) -> List[Path]:
    """Recording sessions under ``root`` (real directories holding a ledger), oldest first."""
    try:
        entries = sorted(os.scandir(root), key=lambda e: e.name)
    except OSError:
        return []
    return [Path(e.path) for e in entries
            if e.is_dir(follow_symlinks=False) and os.path.isfile(os.path.join(e.path, LEDGER_FILE))]


def session_is_live(d: Path) -> bool:
    """Is a process still writing this session? Its ``.lock`` is flock-held for the writer's lifetime (released
    by the kernel on crash). When in doubt the answer is True: nothing is ever pruned on a guess."""
    lock = d / LOCK_FILE
    try:
        fd = os.open(lock, os.O_RDWR | _NOFOLLOW | _CLOEXEC)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    try:
        if fcntl is None:
            try:
                return config.pid_alive(int(os.pread(fd, 32, 0).split()[0]))
            except (ValueError, IndexError, OSError):
                return True
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _dir_bytes(d: Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(d):
        for name in files:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total


def _rmtree(d: Path) -> None:
    if not d.is_symlink():
        shutil.rmtree(d, ignore_errors=True)


_OPEN: "Set[Ledger]" = set()
_OPEN_LOCK = threading.Lock()


def close_all() -> None:
    with _OPEN_LOCK:
        open_now = list(_OPEN)
    for led in open_now:
        led.close()


atexit.register(close_all)


class Ledger:
    """Records one session. Construct through :func:`for_machine`."""

    def __init__(self, root: Path, machine: str, *, keep: int = DEFAULT_KEEP, record_typed_text: bool = False,
                 max_frames: int = DEFAULT_MAX_FRAMES, max_bytes: int = DEFAULT_MAX_MB * MiB,
                 max_total_bytes: int = DEFAULT_MAX_TOTAL_MB * MiB):
        self.root, self.machine = Path(root), machine
        self.keep = max(1, int(keep))
        self.record_typed_text = record_typed_text
        self.max_frames, self.max_bytes, self.max_total_bytes = max_frames, max_bytes, max_total_bytes
        self._lock = threading.RLock()
        self._dir: Optional[Path] = None
        self._lock_fd: Optional[int] = None
        self._seq = 0
        self._bytes = 0
        self._frames: Dict[bytes, str] = {}     # digest -> relative png path
        self._records: List[Dict[str, Any]] = []
        self._snapshots: List[str] = []
        self._dirty: Set[str] = set()           # files whose last write failed midway
        self._frames_stopped = False
        self._stopped = False
        self._closed = False
        self._open_failed_at: Optional[float] = None
        self._warned = False

    # --- plumbing ---------------------------------------------------------------------------
    @property
    def session_dir(self) -> Optional[Path]:
        return self._dir

    def _warn(self, exc: BaseException) -> None:
        if not self._warned:
            self._warned = True
            log.warning("barehand recorder error (recording continues best-effort, not repeated): %s: %s",
                        type(exc).__name__, exc)

    def _open_session(self) -> Path:
        if self._dir is not None:
            return self._dir
        if self._open_failed_at is not None and time.monotonic() - self._open_failed_at < 60:
            raise OSError("recordings directory is not writable (retrying later)")
        try:
            return self._create_session()
        except BaseException:
            self._open_failed_at = time.monotonic()
            raise

    def _create_session(self) -> Path:
        self.root.parent.mkdir(parents=True, exist_ok=True)
        _private_dir(self.root)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        base = f"{stamp}-{_safe_name(self.machine)}-p{os.getpid()}"
        d: Optional[Path] = None
        for _ in range(50):
            cand = self.root / f"{base}-{random.randrange(16 ** 6):06x}"
            try:
                os.mkdir(cand, 0o700)
                d = cand
                break
            except FileExistsError:
                continue
        if d is None:
            raise OSError("could not create a unique session folder")
        try:
            (d / FRAMES_DIR).mkdir(mode=0o700)
            fd = _open_private(d / LOCK_FILE, os.O_RDWR | os.O_CREAT)
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.ftruncate(fd, 0)
            os.pwrite(fd, f"{os.getpid()}\n".encode(), 0)
            self._lock_fd = fd
            _write_file(d / INDEX_FILE, VIEWER.encode())
            _write_file(d / JS_FILE, b"window.BAREHAND=[];\n")
        except BaseException:
            self._release_lock()
            _rmtree(d)
            raise
        self._dir = d
        with _OPEN_LOCK:
            _OPEN.add(self)
        self._append({"v": SCHEMA, "seq": 0, "ts": time.time(), "action": "session_start",
                      "machine": self.machine, "pid": os.getpid(), "text": None})
        try:
            self._prune()
        except Exception as exc:   # never let housekeeping break the recording
            self._warn(exc)
        return d

    def _release_lock(self) -> None:
        if self._lock_fd is not None:
            try:
                os.close(self._lock_fd)
            except OSError:
                pass
            self._lock_fd = None

    def close(self) -> None:
        """Release the session (its folder stays). Called at exit; a closed ledger records nothing more."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._dir is not None:
                try:
                    os.unlink(self._dir / LOCK_FILE)
                except OSError:
                    pass
            self._release_lock()
        with _OPEN_LOCK:
            _OPEN.discard(self)

    def _prune(self) -> None:
        """Delete the oldest *finished* sessions beyond ``keep`` and, when the folder is over its total byte
        budget, more of the oldest finished ones. Never this session, never one a live process is writing."""
        sessions = session_dirs(self.root)
        dead = [d for d in sessions if d != self._dir and not session_is_live(d)]
        excess = len(sessions) - self.keep
        removable = list(dead)
        for d in dead[:max(0, excess)]:
            _rmtree(d)
            removable.remove(d)
        sizes = {d: _dir_bytes(d) for d in session_dirs(self.root)}
        total = sum(sizes.values())
        for d in removable:
            if total <= self.max_total_bytes:
                break
            if d in sizes:
                _rmtree(d)
                total -= sizes[d]

    def _append_line(self, name: str, data: bytes) -> None:
        """Append whole lines to ``name``; a write that failed midway is repaired before the next one."""
        path = self._dir / name
        if name in self._dirty:
            _recover_tail(path)
            self._dirty.discard(name)
        fd = _open_private(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
        try:
            _write_all(fd, data)
        except BaseException:
            self._dirty.add(name)
            raise
        finally:
            os.close(fd)
        self._bytes += len(data)

    def _append(self, rec: Dict[str, Any]) -> None:
        self._records.append(rec)
        self._append_line(LEDGER_FILE, (json.dumps(rec) + "\n").encode("ascii"))
        self._append_js(rec)

    def _append_js(self, rec: Dict[str, Any]) -> None:
        """Keep ledger.js current: append a line, or rewrite the whole file if an earlier write left it broken
        (a cut-off JS statement would stop the viewer from loading at all)."""
        path = self._dir / JS_FILE
        if JS_FILE in self._dirty:
            body = "window.BAREHAND=[];\n" + "".join(f"BAREHAND.push({_js_json(r)});\n" for r in self._records)
            _atomic_write(path, body.encode())
            self._dirty.discard(JS_FILE)
            return
        try:
            self._append_line(JS_FILE, f"BAREHAND.push({_js_json(rec)});\n".encode())
        except BaseException:
            self._dirty.add(JS_FILE)
            raise

    def _note(self, message: str) -> None:
        self._seq += 1
        log.warning("barehand recorder: %s", message)
        self._append({"v": SCHEMA, "seq": self._seq, "ts": time.time(), "action": "recorder_note", "args": {},
                      "ok": True, "pre": None, "post": None, "effect": None, "code": None, "message": message,
                      "elapsed": None, "snapshots": list(self._snapshots), "frame_pre": None,
                      "frame_post": None, "text": None})

    def _save_frame(self, frame: Optional[Frame]) -> Optional[str]:
        """Relative path of the PNG of ``frame``; written only the first time its digest is seen, and not at all
        once the session's frame budget is used up."""
        if frame is None:
            return None
        known = self._frames.get(frame.digest)
        if known:
            return known
        if self._frames_stopped:
            return None
        if len(self._frames) >= self.max_frames or self._bytes >= self.max_bytes:
            self._stop_frames()
            return None
        png = frame_to_png(frame)
        if self._bytes + len(png) > self.max_bytes:
            self._stop_frames()
            return None
        rel = f"{FRAMES_DIR}/{len(self._frames) + 1:04d}-{frame.digest.hex()[:12]}.png"
        _write_file(self._dir / rel, png)
        self._bytes += len(png)
        self._frames[frame.digest] = rel
        return rel

    def _stop_frames(self) -> None:
        self._frames_stopped = True
        self._note(f"frame storage stopped after {len(self._frames)} frames / {self._bytes // MiB} MiB "
                   f"(record_max_frames={self.max_frames}, record_max_mb={self.max_bytes // MiB}); later "
                   "actions are still recorded, without frames")

    # --- recording --------------------------------------------------------------------------
    def record(self, action: str, args: Optional[Mapping[str, Any]] = None, *, ok: bool = True,
               pre: Optional[Frame] = None, post: Optional[Frame] = None, effect: Optional[str] = None,
               message: str = "", code: Optional[str] = None, elapsed: Optional[float] = None,
               extra: Optional[Mapping[str, Any]] = None) -> None:
        """Append one record. Never raises."""
        try:
            with self._lock:
                if self._closed or self._stopped:
                    return
                self._open_session()
                if len(self._records) >= MAX_RECORDS:
                    self._stopped = True
                    self._note(f"recording stopped after {MAX_RECORDS} records")
                    return
                frame_pre, frame_post = self._save_frame(pre), self._save_frame(post)   # may add a note first
                self._seq += 1
                a = dict(args or {})
                if action == "type" and not self.record_typed_text and "text" in a:
                    a = {"text": None, "chars": len(str(a["text"])), "redacted": True}
                rec: Dict[str, Any] = {
                    "v": SCHEMA, "seq": self._seq, "ts": time.time(), "action": action, "args": a, "ok": ok,
                    "pre": pre.digest.hex() if pre is not None else None,
                    "post": post.digest.hex() if post is not None else None,
                    "effect": effect, "code": code, "message": message,
                    "elapsed": None if elapsed is None else round(elapsed, 3),
                    "snapshots": list(self._snapshots),
                    "frame_pre": frame_pre, "frame_post": frame_post,
                    "text": None,
                }
                if extra:
                    rec.update(extra)
                self._append(rec)
        except Exception as exc:
            self._warn(exc)

    def note_machine(self, action: str, args: Mapping[str, Any], message: str, ok: bool = True) -> None:
        """A ``barehand_machine`` call (no frames). Keeps the running list of snapshot names."""
        try:
            with self._lock:
                name = args.get("name")
                if ok and isinstance(name, str):
                    if action == "snapshot_save" and name not in self._snapshots:
                        self._snapshots.append(name)
                    elif action == "snapshot_delete" and name in self._snapshots:
                        self._snapshots.remove(name)
            self.record(f"machine:{action}", dict(args), ok=ok, message=message)
        except Exception as exc:
            self._warn(exc)

    def records(self) -> List[Dict[str, Any]]:
        """The records as written to disk (typed text redacted unless ``record_typed_text`` was on)."""
        with self._lock:
            return list(self._records)


VIEWER = r"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Barehand recording</title>
<style>
:root{--bg:#0b0b0c;--fg:#ededed;--mut:#8a8a8e;--acc:#e8a33d;--line:#2a2a2d;
--sans:-apple-system,"SF Pro Text","Segoe UI",system-ui,sans-serif;--mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace}
*{box-sizing:border-box}
html{color-scheme:dark}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 var(--sans)}
.wrap{max-width:2400px;margin:0 auto;padding:0 32px}
header{display:flex;align-items:baseline;gap:24px;padding:16px 0;border-bottom:1px solid var(--line)}
.mark{font-weight:600;font-size:16px;letter-spacing:.2px;white-space:nowrap}
.mark i{font-style:normal;color:var(--acc)}
#title{flex:1;min-width:0;color:var(--mut);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.stat{font-family:var(--mono);white-space:nowrap;text-align:right}
.stat span{color:var(--mut);margin-left:16px}
main{display:grid;grid-template-columns:minmax(0,1fr) 380px;gap:32px;padding:24px 0}
#frame{border:1px solid var(--line);background:#000;display:flex;justify-content:center;align-items:flex-start;min-height:240px}
#img{display:block;width:100%;height:auto}
#nof{width:100%;display:flex;align-items:center;justify-content:center;padding:160px 24px;text-align:center;color:var(--mut);font-family:var(--mono);font-size:13px}
[hidden]{display:none!important}
aside{min-width:0;display:flex;flex-direction:column;gap:24px}
.lead{margin:0;color:var(--mut);font-size:13px}
.lab{font:11px/16px var(--mono);letter-spacing:.8px;text-transform:uppercase;color:var(--mut);margin:0 0 8px;display:flex;align-items:center;gap:8px}
.act{font-family:var(--mono);font-size:14px;word-break:break-word;overflow-wrap:anywhere}
.act b{color:var(--acc);font-weight:600;margin-right:8px}
.act .note{color:var(--mut)}
.verdict{font:11px/16px var(--mono);letter-spacing:.8px;text-transform:uppercase;border:1px solid var(--mut);color:var(--mut);padding:0 8px}
.verdict.unverifiable{color:var(--acc);border-color:var(--acc)}
.verdict.failed{background:var(--acc);border-color:var(--acc);color:#0b0b0c}
#msg{margin:0;white-space:pre-wrap;word-break:break-word;overflow-wrap:anywhere}
#msg.clip{display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;overflow:hidden}
.link{background:none;border:0;padding:0;margin-top:8px;color:var(--mut);font:inherit;font-size:13px;text-decoration:underline;cursor:pointer}
.link:hover{color:var(--fg)}
#txt{margin:0;font:12px/16px var(--mono);white-space:pre;overflow:auto;max-height:288px;border:1px solid var(--line);padding:8px 16px;color:var(--fg)}
button{font:inherit}
button:focus-visible,#strip:focus-within,#txt:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
#strip{position:relative;height:48px;border-top:1px solid var(--line);border-bottom:1px solid var(--line);cursor:pointer;touch-action:none}
#cv{display:block;width:100%;height:100%}
#s{position:absolute;left:0;top:0;width:1px;height:1px;opacity:0;pointer-events:none}
.ctl{display:flex;align-items:center;gap:8px;padding:16px 0 0}
.btn{background:none;color:var(--fg);border:1px solid var(--line);padding:4px 16px;cursor:pointer;line-height:24px}
.btn:hover{border-color:var(--mut)}
.btn.on{border-color:var(--acc);color:var(--acc)}
.hint{flex:1;color:var(--mut);font-size:12px;text-align:right}
.hint kbd{font:12px var(--mono)}
footer{padding:24px 0 32px;color:var(--mut);font-size:12px;max-width:960px}
footer p{margin:0}
@media (max-width:900px){.wrap{padding:0 16px}main{grid-template-columns:minmax(0,1fr);gap:24px}header{flex-wrap:wrap;gap:8px 16px}
#title{order:3;flex-basis:100%}.hint{display:none}}
</style>
<div class="wrap">
<header><div class="mark">Barehand<i>_</i></div><div id="title"></div>
<div class="stat"><span id="pos" style="margin:0;color:var(--fg)" aria-live="polite"></span><span id="el"></span></div></header>
<main>
<div id="frame"><img id="img" alt="" hidden><div id="nof" hidden></div></div>
<aside>
<p class="lead">Each step is one tool call the model made and the screen it got back. Typed text is not recorded.</p>
<section><h2 class="lab">Action</h2><div class="act" id="act"></div></section>
<section><h2 class="lab">Reply <span id="vd" class="verdict" hidden></span></h2><p id="msg"></p>
<button class="link" id="more" hidden>show more</button></section>
<section id="txtbox" hidden><h2 class="lab">Screen text the model received</h2><pre id="txt" tabindex="0"></pre></section>
</aside>
</main>
<div id="strip"><canvas id="cv" aria-hidden="true"></canvas>
<input id="s" type="range" min="0" value="0" aria-label="Step"></div>
<div class="ctl"><button class="btn" id="prev" aria-label="Previous step">Prev</button>
<button class="btn" id="play" aria-label="Play or pause">Play</button>
<button class="btn" id="next" aria-label="Next step">Next</button>
<button class="btn" id="reload" aria-label="Reload ledger.js">Reload</button>
<div class="hint"><kbd>&larr;</kbd> <kbd>&rarr;</kbd> step &middot; <kbd>Shift</kbd> jump 10 &middot; <kbd>Home</kbd> <kbd>End</kbd> &middot; <kbd>Space</kbd> play</div></div>
<footer><p id="foot"></p></footer>
</div>
<script src="ledger.js"></script>
<script>
(function(){
var R=[],T0=0,cur=0,timer=null,W=0,H=0;
var $=function(id){return document.getElementById(id)};
var DEF_FOOT="This page contains screenshots of everything the screen showed and the key chords sent (typed strings only if record_typed_text was on): keep it private.";
function meta(){var m=window.BAREHAND&&window.BAREHAND.meta;return m&&typeof m==="object"?m:{}}
function load(){var all=window.BAREHAND||[];R=all.filter(function(r){return r&&r.action!=="session_start"});
var st=all.filter(function(r){return r&&r.action==="session_start"})[0];
T0=st&&st.ts?st.ts:(R[0]?R[0].ts:0);
var m=meta(),t=typeof m.title==="string"&&m.title?m.title:(st&&st.machine?String(st.machine):"");
$("title").textContent=t;$("title").title=t;
document.title=t?"Barehand: "+t:"Barehand recording";
$("foot").textContent=typeof m.footer==="string"&&m.footer?m.footer:DEF_FOOT;
$("s").max=Math.max(0,R.length-1)}
function fmt(sec){sec=Math.max(0,Math.round(sec));var h=Math.floor(sec/3600),m=Math.floor(sec%3600/60),s=sec%60;
return (h?h+":"+(m<10?"0":""):"")+m+":"+(s<10?"0":"")+s}
function frameFor(i){for(var k=i;k>=0;k--){var r=R[k];if(r.frame_post)return r.frame_post;
if(k===i&&r.frame_pre)return r.frame_pre}for(k=0;k<R.length;k++){if(R[k].frame_pre)return R[k].frame_pre}return""}
function flagged(r){return r.ok===false||!!r.code||r.effect==="unverifiable"||r.action==="recorder_note"}
function splitMsg(r){var m=typeof r.message==="string"?r.message:"",tx=typeof r.text==="string"?r.text:"";
var a=m.indexOf("[screen-text ");
if(a>=0){var h=m.indexOf("]\n",a),e=h<0?-1:m.indexOf("[/screen-text",h);
if(e>=0){var close=m.indexOf("]",e);tx=m.slice(h+2,e).replace(/\n$/,"");
m=(m.slice(0,a)+(close>=0?m.slice(close+1):"")).replace(/\s+$/,"")}}
return {msg:m,txt:dedent(tx)}}
function dedent(tx){if(!tx)return tx;var ls=tx.split("\n"),re=/^(\s*\d+[ >]?\|)(.*)$/,min=1e9,i,mm,pad;
for(i=0;i<ls.length;i++){mm=re.exec(ls[i]);if(mm&&/\S/.test(mm[2]))min=Math.min(min,/^ */.exec(mm[2])[0].length)}
if(min===1e9||min===0)return tx.replace(/\s+$/,"");
for(i=0;i<ls.length;i++){mm=re.exec(ls[i]);if(mm)ls[i]=mm[1]+mm[2].slice(Math.min(min,/^ */.exec(mm[2])[0].length))}
return ls.join("\n").replace(/\s+$/,"")}
function argsLine(r,box){var a=r.args||{},k,parts=[];
if(r.action==="type"&&a.redacted){box.appendChild(document.createTextNode(" "));var n=document.createElement("span");n.className="note";
n.textContent="("+a.chars+" characters, not recorded)";box.appendChild(n);return}
for(k in a){if(Object.prototype.hasOwnProperty.call(a,k)&&a[k]!==null&&a[k]!==undefined)
parts.push(k+"="+(typeof a[k]==="string"?JSON.stringify(a[k]):JSON.stringify(a[k])))}
if(parts.length)box.appendChild(document.createTextNode(parts.join("  ")))}
var showAll=false;
function clipCheck(){var m=$("msg"),b=$("more");m.classList.remove("clip");
m.classList.add("clip");
var over=m.scrollHeight>m.clientHeight+1;b.hidden=!over&&!showAll;
if(showAll){m.classList.remove("clip");b.textContent="show less"}else b.textContent="show more"}
function setFrame(f,i){var img=$("img"),no=$("nof");
function none(t){img.hidden=true;img.removeAttribute("src");no.hidden=false;no.textContent=t}
img.alt="Screen at step "+(i+1);
if(!f){none("No frame was stored for this recording.");return}
no.hidden=true;img.hidden=false;img.onerror=function(){none("Frame not available (not stored, or the frame limit was reached): "+f)};
img.onload=fit;if(img.getAttribute("src")!==f)img.src=f;else fit()}
function fit(){var img=$('img'),fr=$('frame'),nw=img.naturalWidth;if(!nw||img.hidden)return;
var av=fr.clientWidth-2,k=Math.floor(av/nw);
if(k>=1){img.style.width=(nw*k)+'px';img.style.imageRendering='pixelated'}
else{img.style.width=av+'px';img.style.imageRendering='auto'}}
function show(i){if(!R.length){$("pos").textContent="0 / 0";$("el").textContent="";$("act").textContent="";
$("msg").textContent="(no actions recorded yet)";$("txtbox").hidden=true;$("vd").hidden=true;$("more").hidden=true;
setFrame("",0);draw();return}
i=Math.max(0,Math.min(R.length-1,i|0));cur=i;$("s").value=i;var r=R[i];
$("pos").textContent=(i+1)+" / "+R.length;$("s").setAttribute("aria-valuetext","Step "+(i+1)+" of "+R.length);
$("el").textContent=fmt((r.ts||T0)-T0);
setFrame(frameFor(i),i);
var act=$("act");act.textContent="";var b=document.createElement("b");b.textContent=String(r.action);act.appendChild(b);
argsLine(r,act);
var p=splitMsg(r),vd=$("vd");
var v=r.ok===false?"failed":(r.effect||"");
if(v){vd.hidden=false;vd.textContent=v;vd.className="verdict "+v.replace(/[^a-z]/g,"")}else vd.hidden=true;
var extra=[];if(r.code)extra.push("code: "+r.code);if(r.snapshots&&r.snapshots.length)extra.push("snapshots: "+r.snapshots.join(", "));
$("msg").textContent=(p.msg||(r.ok===false?"":"(no message)"))+(extra.length?"\n"+extra.join("   "):"");
showAll=false;clipCheck();
$("txtbox").hidden=!p.txt;$("txt").textContent=p.txt;$("txt").scrollTop=0;
draw();
try{history.replaceState(null,"","#step="+(i+1))}catch(e){}}
function draw(){var cv=$("cv"),dpr=window.devicePixelRatio||1,w=cv.clientWidth,h=cv.clientHeight;
if(!w||!h)return;cv.width=Math.round(w*dpr);cv.height=Math.round(h*dpr);var c=cv.getContext("2d");
c.setTransform(dpr,0,0,dpr,0,0);c.clearRect(0,0,w,h);W=w;H=h;var n=R.length;if(!n)return;
var base=34,tw=Math.max(1,Math.min(3,Math.floor(w/n/2))),x,i,r,prev=-1;
var col=new Uint8Array(Math.ceil(w)+1);
for(i=0;i<n;i++){r=R[i];x=Math.min(col.length-1,Math.floor(i*w/n));var k=flagged(r)?3:(r.action==="capture"?2:1);
if(k>col[x])col[x]=k}
for(x=0;x<col.length;x++){var k=col[x];if(!k)continue;
c.fillStyle=k===1?"#3a3a3e":"#5c5c61";c.fillRect(x,base-(k===1?12:20),tw,k===1?12:20);
if(k===3){c.fillStyle="#ededed";c.fillRect(x,base+4,tw,4)}}
x=Math.min(w-2,Math.floor(cur*w/n));c.fillStyle="#e8a33d";c.fillRect(x-1,6,tw+2,base-6)}
function go(i){show(i)}
function stop(){if(timer){clearInterval(timer);timer=null}$("play").textContent="Play";$("play").classList.remove("on")}
function toggle(){if(timer){stop();return}if(!R.length)return;if(cur>=R.length-1)show(0);
$("play").textContent="Pause";$("play").classList.add("on");
timer=setInterval(function(){if(cur>=R.length-1){stop();return}show(cur+1)},700)}
function fromHash(){var m=/step=(\d+)/.exec(location.hash||"");return m?parseInt(m[1],10)-1:-1}
function pick(e){var rc=$("cv").getBoundingClientRect();if(!R.length)return;
show(Math.floor((e.clientX-rc.left)/rc.width*R.length))}
var down=false,st=$("strip");
st.addEventListener("pointerdown",function(e){down=true;try{st.setPointerCapture(e.pointerId)}catch(x){}stop();pick(e)});
st.addEventListener("pointermove",function(e){if(down)pick(e)});
st.addEventListener("pointerup",function(){down=false});
st.addEventListener("pointercancel",function(){down=false});
$("s").oninput=function(){stop();show(+$("s").value)};
$("prev").onclick=function(){stop();show(cur-1)};
$("next").onclick=function(){stop();show(cur+1)};
$("play").onclick=toggle;
$("more").onclick=function(){showAll=!showAll;clipCheck()};
$("reload").onclick=function(){var el=document.createElement("script");
el.src="ledger.js?t="+Date.now();el.onload=function(){load();show(R.length-1);document.body.removeChild(el)};
el.onerror=function(){document.body.removeChild(el)};document.body.appendChild(el)};
document.addEventListener("keydown",function(e){if(e.ctrlKey||e.metaKey||e.altKey)return;
var t=e.target&&e.target.tagName;if(t==="BUTTON"&&(e.key===" "||e.key==="Enter"))return;
var d=e.shiftKey?10:1,k=e.key;
if(k==="ArrowLeft"){stop();show(cur-d)}
else if(k==="ArrowRight"){stop();show(cur+d)}
else if(k==="Home"){stop();show(0)}
else if(k==="End"){stop();show(R.length-1)}
else if(k===" "){toggle()}
else return;
e.preventDefault()});
window.addEventListener("resize",function(){draw();clipCheck();fit()});
window.addEventListener("hashchange",function(){var h=fromHash();if(h>=0&&h!==cur)show(h)});
load();var h0=fromHash();show(h0>=0?h0:(+meta().start>0?Math.floor(+meta().start)-1:R.length-1));
})();
</script>
</html>
"""

_LEDGERS: Dict[Tuple[str, str], Ledger] = {}
_REG_LOCK = threading.Lock()


def for_machine(machine: str, settings: Optional[Mapping[str, Any]] = None) -> Optional[Ledger]:
    """The process-wide ledger for ``machine`` (the same object for the backend and the machine tool), or None
    when recording is off or the data dir cannot be resolved. Never raises; creates nothing on disk (the first
    record does)."""
    try:
        st = dict(config.hermes_settings() if settings is None else settings)
        rs = parse_settings(st)
        if not rs.enabled:
            return None
        key = (str(recordings_path()), machine)
        with _REG_LOCK:
            led = _LEDGERS.get(key)
            if led is None:
                led = _LEDGERS[key] = Ledger(
                    Path(key[0]), machine, keep=rs.keep, record_typed_text=rs.typed_text,
                    max_frames=rs.max_frames, max_bytes=rs.max_bytes, max_total_bytes=rs.max_total_bytes)
            else:
                led.record_typed_text = rs.typed_text
            return led
    except Exception as exc:
        _warn_once("unavailable", "barehand recorder unavailable (not repeated): %s: %s", type(exc).__name__, exc)
        return None


def reset_registry() -> None:
    """Forget cached ledgers and release their locks (tests; a new Hermes process starts a new session anyway)."""
    with _REG_LOCK:
        for led in _LEDGERS.values():
            led.close()
        _LEDGERS.clear()
    _WARNED.clear()


# --- CLI helpers ---------------------------------------------------------------------------------
def list_sessions() -> List[Dict[str, Any]]:
    out = []
    for d in session_dirs(recordings_path()):
        try:
            with open(d / LEDGER_FILE, "rb") as fh:
                n = max(0, sum(1 for _ in fh) - 1)
        except OSError:
            n = 0
        out.append({"name": d.name, "path": str(d), "records": n, "actions": n, "bytes": _dir_bytes(d),
                    "live": session_is_live(d)})
    return out


LAST_PURGE_SKIPPED = 0


def purge() -> int:
    """Delete every finished recording session (a session a live process is writing is left alone); returns how
    many session folders were removed. Only folders holding a ledger are touched."""
    global LAST_PURGE_SKIPPED
    n = skipped = 0
    for d in session_dirs(recordings_path()):
        if session_is_live(d):
            skipped += 1
            continue
        _rmtree(d)
        n += 1
    LAST_PURGE_SKIPPED = skipped
    return n
