"""Glue between the pixel text reader (``text.py``) and the rest of the plugin: where atlases live, deriving the
EDK2 one from the user's firmware, reading a frame without ever raising or stalling a call, fencing the result
for the model, calibrating a Linux console, and waiting for text to appear.

Every function here is best effort: a failure to read text must never fail a capture or an action."""

from __future__ import annotations

import dataclasses
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from . import config, text

log = logging.getLogger("barehand.screentext")
MAX_ROWS = 60
MAX_CHARS = 6000
SLOW_READ = 2.0            # a read slower than this switches text reading off for a while
PAUSE_SECONDS = 120.0
WAIT_DEFAULT, WAIT_MAX, WAIT_POLL = 60.0, 240.0, 0.5
WAIT_BLIND_AFTER = 20.0    # wait_for_text gives up early when no frame was readable and the screen stayed still this long
PATTERN_MAX = 200          # longest `re:` pattern wait_for_text accepts
RESYNC_EVERY = 8           # a full block at least every this many results
DELTA_MAX_ROWS = 12        # a delta is sent only when at most this many rows (and < half the screen) changed
UNREADABLE_RETRY = 1.0     # seconds before a frame geometry that read as "not text" is read again
EDK2_RETRY = 600.0
MEMO_MAX = 8

_lock = threading.Lock()
_atlas_cache: Tuple[Optional[float], str, List[text.Atlas]] = (None, "", [])
_edk2_tried: set = set()             # firmware files whose atlas is derived (or already stored)
_edk2_inflight: set = set()          # firmware files a thread is working on right now
_edk2_threads: Dict[str, threading.Thread] = {}
_edk2_failed: Dict[str, float] = {}  # firmware file -> time of the last failed derivation
_paused_until = 0.0
_memo: "OrderedDict[Tuple[bytes, Tuple[str, ...]], text.ScreenText]" = OrderedDict()
_unreadable: Dict[Tuple[int, int, Tuple[str, ...]], Tuple[float, text.ScreenText]] = {}


def reset_caches() -> None:
    """Forget every cache (tests, and after the atlas store changes by hand)."""
    global _atlas_cache, _paused_until
    with _lock:
        _atlas_cache = (None, "", [])
        _edk2_tried.clear(); _edk2_inflight.clear(); _edk2_failed.clear(); _edk2_threads.clear()
        _memo.clear(); _unreadable.clear()
        _paused_until = 0.0


def store() -> text.AtlasStore:
    return text.AtlasStore(str(config.data_dir() / "atlases"))


def _store_mtime(path: str) -> Optional[float]:
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def atlases() -> List[text.Atlas]:
    """All saved atlases (cached until the store directory changes)."""
    global _atlas_cache
    st = store()
    mt = _store_mtime(st.path)
    with _lock:
        if _atlas_cache[0] == mt and _atlas_cache[1] == st.path:
            return list(_atlas_cache[2])
    loaded = st.load_all()
    with _lock:
        _atlas_cache = (mt, st.path, loaded)
    return list(loaded)


def derive_edk2(code_path: Optional[str]) -> bool:
    """Build the EDK2 atlas from the firmware code file and save it. Silent on failure; a failure is retried
    after ten minutes at the earliest."""
    if not code_path:
        return False
    with _lock:
        if code_path in _edk2_tried:
            return True
        if time.monotonic() - _edk2_failed.get(code_path, -EDK2_RETRY) < EDK2_RETRY:
            return False
    try:
        if any(a.source == "edk2-firmware" for a in store().load_all()):
            ok = True
        else:
            store().save(text.atlas_from_edk2_fd(code_path))
            ok = True
    except Exception as exc:   # ValueError on unknown layouts (aarch64 is untested), OSError, ...
        log.info("no EDK2 text atlas from %s: %s", code_path, exc)
        ok = False
    with _lock:
        if ok:
            _edk2_tried.add(code_path)
        else:
            _edk2_failed[code_path] = time.monotonic()
    return ok


def firmware_code(name: Optional[str] = None, settings: Optional[Mapping[str, Any]] = None) -> Optional[str]:
    try:
        from . import machine
        name = name or config.machine_name(settings)
        state = config.read_state(name) or {}
        code, _ = machine.firmware_for(settings, state.get("qemu"), state.get("arch") or "x86_64")
        return code
    except Exception:
        return None


def _derive_thread(code: str) -> None:
    try:
        derive_edk2(code)
    finally:
        with _lock:
            _edk2_inflight.discard(code)


def ensure_edk2(name: Optional[str] = None, settings: Optional[Mapping[str, Any]] = None,
                background: bool = True, wait: float = 0.0) -> None:
    """Derive the EDK2 atlas once per firmware file, in a background thread by default. At most one thread per
    firmware file. ``wait`` bounds how long the caller waits for a derivation that is in flight, so the very
    first capture of a firmware menu already carries text without an unbounded delay."""
    try:
        code = firmware_code(name, settings)
        if not code:
            return
        with _lock:
            running = _edk2_threads.get(code) if code in _edk2_inflight else None
        if running is not None:
            if wait > 0:
                running.join(wait)
            return
        with _lock:
            if code in _edk2_tried or code in _edk2_inflight:
                return
            if time.monotonic() - _edk2_failed.get(code, -EDK2_RETRY) < EDK2_RETRY:
                return
            if background:
                _edk2_inflight.add(code)
        if background:
            th = threading.Thread(target=_derive_thread, args=(code,), daemon=True, name="barehand-edk2-atlas")
            with _lock:
                _edk2_threads[code] = th
            th.start()
            if wait > 0:
                th.join(wait)
        else:
            derive_edk2(code)
    except Exception:
        pass


def _rgb(frame: Any) -> Tuple[bytes, int, int]:
    return bytes(frame.data[frame.offset:]), frame.width, frame.height


def read_frame(frame: Any, atlas_list: Optional[List[text.Atlas]] = None, *, neg_cache: bool = True
               ) -> Optional[text.ScreenText]:
    """Read a ``frames.Frame``. None when text reading is unavailable; never raises. The same frame (digest) is
    read once; a frame geometry that read as "not text" is not read again for a few seconds unless the atlases
    change."""
    global _paused_until
    try:
        if frame is None or time.monotonic() < _paused_until:
            return None
        atl = atlases() if atlas_list is None else atlas_list
        fps = tuple(a.fingerprint for a in atl)
        digest = getattr(frame, "digest", None)
        key = (digest, fps) if digest else None
        with _lock:
            if key is not None and key in _memo:
                _memo.move_to_end(key)
                return _memo[key]
            ukey = (frame.width, frame.height, fps)
            hit = _unreadable.get(ukey) if neg_cache else None
            if hit is not None and time.monotonic() - hit[0] < UNREADABLE_RETRY:
                return hit[1]
        t0 = time.monotonic()
        rgb, w, h = _rgb(frame)
        res = text.read_screen(rgb, w, h, atl)
        if time.monotonic() - t0 > SLOW_READ:
            _paused_until = time.monotonic() + PAUSE_SECONDS
            log.info("text reading took %.1fs; paused for %.0fs", time.monotonic() - t0, PAUSE_SECONDS)
        with _lock:
            if key is not None:
                _memo[key] = res
                while len(_memo) > MEMO_MAX:
                    _memo.popitem(last=False)
            if not res.reliable and not res.blank and res.confidence < 0.5:
                _unreadable[ukey] = (time.monotonic(), res)
            else:
                _unreadable.pop(ukey, None)
        return res
    except Exception as exc:
        log.info("text reading failed: %s", exc)
        return None


# ------------------------------------------------------------------------------------- what the model sees


def fenced(st: Optional[text.ScreenText]) -> str:
    """The full block a model sees, or "" when the text is not reliable (garbage is never emitted)."""
    try:
        if st is None or not st.reliable or st.blank or st.uninitialized:
            return ""
        return st.render(MAX_ROWS, MAX_CHARS)
    except Exception:
        return ""


PARTIAL_CONF = 0.85       # a different font can coincidentally match 70-95% of cells (blank cells count), so be strict
PARTIAL_ROW_MATCH = 0.8   # share of a row's non-blank cells that must be matched for the row to be shown
PARTIAL_ROWS = 0.8        # share of non-blank rows that must be fully matched
PARTIAL_ALNUM_MIN = 20    # letters/digits in the matched rows, and at least half of their non-blank characters


def _row_split(st: text.ScreenText) -> Tuple[List[int], set]:
    bad = {r for r, _ in st.unknown} | {i for i, row in enumerate(st.rows) if text.UNKNOWN in row}
    good = [i for i, row in enumerate(st.rows) if row.strip() and i not in bad]
    return good, bad


def _row_mostly_read(st: text.ScreenText, i: int) -> bool:
    row = st.rows[i]
    solid = len(row.replace(" ", ""))
    if solid == 0:
        return False
    bad = max(row.count(text.UNKNOWN), sum(1 for r, _ in st.unknown if r == i))
    return (solid - bad) >= PARTIAL_ROW_MATCH * solid and row.replace(text.UNKNOWN, "").strip() != ""


def _text_chars(joined: str) -> str:
    """The characters that count when asking "is this real text?": spaces and fill cells (``▒`` bars and boxes)
    are left out, so a screen with full-width title bars is not mistaken for a screen without text. The absolute
    minimum of alphanumerics still applies, which is what keeps fill-only screens out."""
    return joined.replace(" ", "").replace(text.BOX, "")


def _has_alnum_text(st: text.ScreenText) -> bool:
    """True when the screen holds real alphanumeric text (not mostly ▒, blank or unknown cells)."""
    chars = _text_chars("".join(st.rows))
    alnum = sum(c.isalnum() for c in chars)
    return alnum >= PARTIAL_ALNUM_MIN and alnum >= 0.5 * len(chars)


def partial_block(st: Optional[text.ScreenText]) -> str:
    """Rows that are mostly readable (at least PARTIAL_ROW_MATCH of their non-blank cells matched, the other cells
    shown as ``�``), the rest marked unreadable, for a screen below the reliable threshold (typically non-ASCII
    text). "" unless it is high confidence, most non-blank rows are shown and they hold real alphanumeric text."""
    try:
        if st is None or st.reliable or st.blank or not st.rows or st.grid is None or st.confidence < PARTIAL_CONF:
            return ""
        nonblank = [i for i, row in enumerate(st.rows) if row.strip()]
        good = [i for i in nonblank if _row_mostly_read(st, i)]
        if len(good) < 3 or len(good) < 0.5 * len(nonblank):
            return ""
        chars = _text_chars("".join(st.rows[i] for i in good))
        alnum = sum(c.isalnum() for c in chars)
        if alnum < PARTIAL_ALNUM_MIN or alnum < 0.5 * len(chars):
            return ""
        keep = set(good)
        rows = [(row if i in keep else "(unreadable row)" if row.strip() else row) for i, row in enumerate(st.rows)]
        part = dataclasses.replace(st, rows=rows, reliable=True)
        return part.render(MAX_ROWS, MAX_CHARS, label="PARTIAL text, rows marked (unreadable row) could not be read; "
                                                     f"{text.UNKNOWN} = unreadable cell")
    except Exception:
        return ""


def is_text_block(block: str) -> bool:
    """True for a complete, reliable block (what the text-only ``ax`` lane may use)."""
    return block.startswith("[screen-text ") and "PARTIAL" not in block.split("\n", 1)[0]


def _off_reason(st: text.ScreenText) -> Tuple[str, str]:
    """(state key, one-line message) for a screen that is not readable as text."""
    if st.reason.startswith("no glyph atlas"):
        return "noatlas", ("screen text off: no font learned yet; call calibrate_text at an idle Linux shell "
                           "prompt (firmware menus need none)")
    if st.confidence >= PARTIAL_CONF and _has_alnum_text(st):
        return "partial", ("screen text partly unreadable: some characters are outside the learned set (non-ASCII "
                           "or symbols) or this console's font differs slightly from the learned one; the image is "
                           "authoritative. If plain ASCII is unreadable too, calibrate_text at a shell on THIS console")
    return "graphical", ("screen text off: this screen's font has not been learned; the image is authoritative. "
                         "Call barehand_machine calibrate_text at an idle shell prompt on the same console as this "
                         "screen (fonts can differ between consoles; ctrl+alt+f2 or the installer's own shell "
                         "option may give one)")


class TextState:
    """What the model was last sent, per backend instance: lets repeat results shrink to one line or a few
    rows, and says why the layer is off once per state change."""

    def __init__(self) -> None:
        self.prev: Optional[text.ScreenText] = None
        self.since_full = 0
        self.off_key = ""

    def block(self, st: Optional[text.ScreenText], full: bool = False) -> str:
        if st is None:
            return ""
        if st.blank or st.uninitialized:
            self.prev = None
            return ""
        if not st.reliable:
            self.prev = None
            part = partial_block(st)
            if part:
                self.off_key = "partial-on"
                return part
            key, msg = _off_reason(st)
            if key == self.off_key and not full:
                return ""
            self.off_key = key
            return f"[{msg}]"
        self.off_key = ""
        prev = self.prev
        self.prev = st
        if (not full and prev is not None and prev.grid == st.grid and prev.atlas == st.atlas
                and len(prev.rows) == len(st.rows) and self.since_full < RESYNC_EVERY - 1):
            changed = [i for i in range(len(st.rows)) if st.rows[i] != prev.rows[i] or st.inverse[i] != prev.inverse[i]]
            if not changed and st.cursor == prev.cursor:
                self.since_full += 1
                return "screen text unchanged"
            if len(changed) <= DELTA_MAX_ROWS and len(changed) < len(st.rows) / 2:
                self.since_full += 1
                return st.render(MAX_ROWS, MAX_CHARS, only_rows=changed,
                                 label="delta against the previous full block, changed rows only")
        self.since_full = 0
        return fenced(st)


def block_for(frame: Any, state: Optional[TextState] = None, full: bool = False) -> str:
    st = read_frame(frame)
    if state is None:
        return fenced(st)
    return state.block(st, full)


# ------------------------------------------------------------------------------------- wait for text


class PatternError(ValueError):
    pass


MAX_UNBOUNDED = 3          # most `*`, `+`, `{n,}` a `re:` pattern may contain
ROW_MAX = 400              # a row is cut to this many characters before a pattern is matched
_QUANT = re.compile(r"\{\d+(?:,\d*)?\}")
_OK_GROUP = re.compile(r"\(\?(?::|[aiLmsux]+[:)])")


def check_pattern(body: str) -> None:
    """Static check of a ``re:`` body, run BEFORE compiling. The accepted subset cannot backtrack catastrophically:
    no quantified group, no backreference, no lookaround, at most MAX_UNBOUNDED unbounded quantifiers, and
    rows are matched truncated to ROW_MAX characters. Raises ``PatternError`` naming the broken rule."""
    hint = "; use a plain substring or a simpler pattern"
    if len(body) > PATTERN_MAX:
        raise PatternError(f"regular expression longer than {PATTERN_MAX} characters{hint}")
    i, n, unbounded, in_class, prev_close = 0, len(body), 0, False, False
    while i < n:
        ch = body[i]
        step, close, quant = 1, False, 0
        if ch == "\\":
            nxt = body[i + 1] if i + 1 < n else ""
            if nxt.isdigit() and nxt != "0" or nxt in ("g", "k"):
                raise PatternError(f"regular expression rule: no backreferences{hint}")
            step = 2
        elif in_class:
            in_class = ch != "]"
        elif ch == "[":
            in_class = True
            if body[i + 1:i + 2] == "^":
                step = 2
            if body[i + step:i + step + 1] == "]":
                step += 1
        elif ch == "(":
            if body[i + 1:i + 2] == "?" and not _OK_GROUP.match(body, i):
                raise PatternError(f"regular expression rule: no lookarounds, backreferences or special "
                                   f"groups (only (?:...) and flags){hint}")
        elif ch == ")":
            close = True
        elif ch in "*+?":
            quant = 2 if ch in "*+" else 1
        elif ch == "{":
            m = _QUANT.match(body, i)
            if m:
                step = m.end() - i
                quant = 2 if m.group().endswith(",}") else 1
        if quant:
            if prev_close:
                raise PatternError(f"regular expression rule: no quantifier (*, +, ?, {{m,n}}) on a group "
                                   f"(a quantified group can run forever){hint}")
            if quant == 2:
                unbounded += 1
                if unbounded > MAX_UNBOUNDED:
                    raise PatternError(f"regular expression rule: at most {MAX_UNBOUNDED} open-ended "
                                       f"quantifiers (*, +, {{n,}}){hint}")
            i += step
            while body[i:i + 1] in ("?", "+") and body[i - 1:i] in ("*", "+", "?", "}"):
                i += 1      # lazy / possessive suffix of that quantifier
            prev_close = False
            continue
        prev_close = close
        i += step


def compile_pattern(pattern: str) -> Callable[[str], Optional[str]]:
    """``re:...`` is a simple regex searched per row; anything else is a plain substring. Returns a function that
    gives the matching row or None. Raises ``PatternError`` for patterns outside the cheap subset."""
    if pattern.startswith("re:"):
        body = pattern[3:]
        check_pattern(body)
        try:
            rx = re.compile(body)
        except re.error as exc:
            raise PatternError(f"bad regular expression: {exc}") from None
        return lambda row: row if rx.search(row[:ROW_MAX]) else None
    return lambda row: row if pattern in row else None


def _first_match(match: Callable[[str], Optional[str]], rows: List[str]) -> Optional[str]:
    for row in rows:
        hit = match(row)
        if hit is not None:
            return hit
    return None


def _moving(a: Any, b: Any) -> bool:
    """True when two frames differ substantially (about 2% of sampled bytes): a console scrolling, not a caret."""
    try:
        if a is None or b is None or (a.width, a.height) != (b.width, b.height):
            return a is not b
        da, db = a.data, b.data
        n = min(len(da) - a.offset, len(db) - b.offset)
        step = max(1, n // 4000)
        sa, sb = bytes(da[a.offset:a.offset + n:step]), bytes(db[b.offset:b.offset + n:step])
        return sum(x != y for x, y in zip(sa, sb)) > 0.02 * max(1, len(sa))
    except Exception:
        return False


def wait_for_text(pattern: str, timeout: float, frame_source: Callable[[], Any], *, poll: float = WAIT_POLL,
                  atlas_list: Optional[List[text.Atlas]] = None, clock: Callable[[], float] = time.monotonic,
                  sleep: Callable[[float], None] = time.sleep) -> Dict[str, Any]:
    """Poll ``frame_source()`` (returns a ``frames.Frame``; the caller keeps ONE connection for the whole wait)
    until a reliable frame has a row matching ``pattern``. Returns ``{"ok", "matched", "elapsed", "message",
    "screen"}``."""
    if not isinstance(pattern, str) or not pattern:
        return {"ok": False, "message": "wait_for_text needs `text`"}
    try:
        match = compile_pattern(pattern)
    except PatternError as exc:
        return {"ok": False, "message": str(exc)}
    try:
        timeout = max(1.0, min(float(timeout), WAIT_MAX))
    except (TypeError, ValueError):
        timeout = WAIT_DEFAULT
    atl = atlases() if atlas_list is None else atlas_list
    if not atl:
        return {"ok": False, "message": "no glyph atlas yet, so the screen cannot be read as text: call "
                                        "barehand_machine calibrate_text once at a shell/login prompt (firmware "
                                        "menus need no calibration when the EDK2 firmware file was found)"}
    t0 = clock()
    last: Optional[text.ScreenText] = None
    last_err = ""
    first = True
    all_blank = True
    prev_frame: Any = None
    still_since = t0
    while True:
        try:
            frm = frame_source()
            st = read_frame(frm, atl, neg_cache=False)
            if st is None or not st.blank:
                all_blank = False
            if _moving(prev_frame, frm):
                still_since = clock()
            prev_frame = frm
        except Exception as exc:   # the machine may be restarting; keep trying until the deadline
            st, last_err = None, str(exc)
        if st is not None and st.reliable:
            last = st
            hit = _first_match(match, st.rows)
            if hit is not None:
                el = clock() - t0
                if first:
                    return {"ok": True, "matched": hit.strip(), "elapsed": round(el, 1), "already": True,
                            "message": f"already on screen when the wait started: {hit.strip()!r} (this may be the "
                                       "command you just typed or older output, not new output; wait for text that "
                                       "only the output contains)", "screen": st}
                return {"ok": True, "matched": hit.strip(), "elapsed": round(el, 1),
                        "message": f"found {hit.strip()!r} after {el:.1f}s", "screen": st}
        first = False
        if clock() - t0 >= timeout:
            break
        # A still, unreadable picture will not turn into text; a blank screen may be a machine that is still
        # starting, so that case waits out the timeout the caller asked for.
        if (last is None and prev_frame is not None and not all_blank
                and clock() - still_since >= WAIT_BLIND_AFTER):
            break
        sleep(min(poll, max(0.0, timeout - (clock() - t0))))
    el = clock() - t0
    tail = fenced(last)
    if last is None:
        early = el < timeout - 0.5
        stop = "gave up early" if early else "timed out"
        if prev_frame is not None and all_blank:
            what = ("the screen stayed blank/black the whole time (nothing was drawn: no console output on this "
                    "display, or the guest is still starting). Capture to look at the image; if it is still starting, wait again")
        else:
            what = ("the screen was never readable as text during the wait (graphical, or a font that is not "
                    "learned) and it stopped changing" if early else
                    "the screen was never readable as text during the wait" + (f" ({last_err})" if last_err else ""))
            what += ". Capture and look at the image; waiting again will not help"
        return {"ok": False, "elapsed": round(el, 1), "screen": None, "unreadable": True,
                "message": f"{stop} after {el:.0f}s: {pattern!r} could not be waited for: {what}"}
    return {"ok": False, "elapsed": round(el, 1), "screen": last,
            "message": f"timed out after {el:.0f}s: {pattern!r} did not appear (the screen was readable as text; "
                       "last readable screen follows)"
                       + (f"\n{tail}" if tail else "")}


# ------------------------------------------------------------------------------------- calibration


def _settle(q: Any, secs: float = 0.6, limit: float = 6.0) -> Any:
    """A frame that has stopped changing (two equal dumps ``secs`` apart)."""
    deadline = time.monotonic() + limit
    f = q.frame()
    while time.monotonic() < deadline:
        time.sleep(secs)
        g = q.frame()
        if g.digest == f.digest:
            return g
        f = g
    return f


def _calibrate_smallest(after: Tuple[bytes, int, int], cal: str, before: Tuple[bytes, int, int]
                        ) -> Tuple[Optional[text.Atlas], List[str]]:
    """Calibrate with the smallest plausible cell height that gives clean glyphs. Auto-detection on a mostly
    empty screen can pick a multiple of the real line pitch (8x32 for an 8x16 console), which labels glyphs
    together with blank space and fails on any screen with text on the next line."""
    g = text.detect_grid(*after)
    cands: List[Optional[Tuple[int, int]]] = []
    if g is not None:
        for div in (4, 2, 1):
            if g.ch % div == 0 and g.ch // div >= 8:
                cands.append((g.cw, g.ch // div))
    cands.append(None)
    best: Tuple[Optional[text.Atlas], List[str]] = (None, ["no cell grid found"])
    for cell in cands:
        atlas, rep = text.calibrate(after, cal, before=before, cell=cell)
        problems = [str(p) for p in rep.get("problems", [])]
        if not problems and not atlas.missing_ascii():
            return atlas, []
        if best[0] is None or len(problems) < len(best[1]):
            best = (atlas, problems or ["not every character was learned"])
    return best


def calibrate_machine(q: Any, hold_ms: int = 15) -> Dict[str, Any]:
    """Type CALIBRATION_TEXT (the 94 printable ASCII characters, no Enter) at the echoing prompt now showing,
    calibrate, erase exactly what was typed with Backspace (never Ctrl+U, which would wipe a text field's own
    value), type it again reversed to verify, erase again and save the atlas.
    ``q`` is a connected transport. Refuses, typing nothing, when the machine is not running or when an existing
    atlas already reads the screen reliably. Returns ``{"ok", "message", ...}``."""
    from . import keys as keymap
    cal = text.CALIBRATION_TEXT
    try:
        vm = q.status()
    except Exception:
        vm = "running"
    if vm not in ("running", "suspended"):
        return {"ok": False, "message": f"the machine is {vm} and ignores keys; nothing was typed"}
    try:
        now = read_frame(q.frame(), neg_cache=False)
    except Exception:
        now = None
    if now is not None and now.reliable and not now.blank:
        src = next((a.source for a in atlases() if a.fingerprint == now.atlas), "")
        what = ("a firmware/GRUB screen: those are read without calibration" if src.startswith("edk2")
                else "already calibrated for this font")
        return {"ok": False, "message": f"nothing typed: the screen already reads reliably ({what}). calibrate_text is "
                                        "only for a Linux console in a font that is not known yet"}

    typed = [0]

    def erase() -> None:
        """One Backspace per character typed (in batches), so a text field is left as it was."""
        n, typed[0] = typed[0], 0
        while n > 0:
            for _ in range(min(n, 32)):
                q.send_chord(["backspace"], hold_ms=hold_ms)
            n -= 32

    def type_it(s: str) -> None:
        try:
            for chord in keymap.text_to_chords(s):
                q.send_chord(chord, hold_ms=hold_ms)
                typed[0] += 1
        except Exception:
            erase()
            raise

    problems: List[str] = []
    atlas = None
    for attempt in range(2):
        before = _rgb(_settle(q))
        type_it(cal)
        after = _rgb(_settle(q))
        try:
            atlas, problems = _calibrate_smallest(after, cal, before)
        finally:
            erase()
        if not problems and atlas is not None and not atlas.missing_ascii():
            break
        if attempt == 0:
            time.sleep(0.5)
    else:
        return {"ok": False, "message": "calibration failed: " + ("; ".join(problems[:3]) or "not every character "
                "was learned") + ". Make sure the cursor is at an empty, echoing prompt (a shell or login "
                "prompt) near the top of the screen, then try again."}
    before = _rgb(_settle(q))
    rev = cal[::-1]
    type_it(rev)
    after = _rgb(_settle(q))
    try:
        diffs = text.verify_calibration(atlas, after, rev, before=before)
    finally:
        erase()
    if diffs:
        return {"ok": False, "message": "calibration did not verify (typed keys may have been dropped): "
                + "; ".join(diffs[:3])}
    path = store().save(atlas)
    return {"ok": True, "atlas": os.path.basename(path), "glyphs": len(atlas),
            "message": f"calibrated and verified {len(atlas)} glyphs ({atlas.cw}x{atlas.ch} font); screens in this "
                       "font now come with exact text"}
