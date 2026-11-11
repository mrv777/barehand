"""``BarehandBackend``: the ``computer_use`` driver for a raw machine display (QEMU over QMP, or any VNC server).

There is no accessibility tree, no apps and no windows; only pixels in, keys and pointer out. Every
unsupported verb answers ``ActionResult(ok=False, code="unsupported_action")``. Action methods never
raise. A QMP connection is opened per operation (QEMU serves one client at a time) so the
``hermes barehand`` CLI can snapshot or reset the machine between agent actions.

Result contract (Hermes derives the model's "verdict" from these fields): an input that was delivered is
``ok=True`` even if watching the screen afterwards failed, so the model never re-sends it. ``effect`` is
"confirmed" only when the screen changed after the input in a way that can be attributed to it (it differs from
the frame taken just before the input, a blinking cursor does not count, and neither does a region known to
change by itself); the word means "the screen changed", not "the input was accepted". Everything else is
"unverifiable" with a message that says what was observed: no change within the settle window (the key may have
had no visible effect, or the machine is busy), a display that was not initialised or was already changing, a
resolution change, a small region that keeps changing by itself, or a screen that could not be observed.
``suspected_noop`` is never emitted: Hermes' fixed hint for it talks about pixels and foreground delivery and
never says to re-capture. A failure carries ``escalation={"recommended": ...}`` and a message that says
delivery_mode is irrelevant here, so the model is not steered into foreground-delivery approval prompts.
"""

from __future__ import annotations

import base64
import contextlib
import logging
import re
import threading
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from tools.computer_use.backend import ActionResult, CaptureResult, ComputerUseBackend

from . import config, ledger, screentext, transport

from . import keys as keymap
from .imaging import MAX_SIDE, fit_dims, frame_to_png, shrink_png
from .frames import Box, Frame, boxes_touch, cursor_like, diff_box, display_state, mask_frame, union_box
from .qmp import BLINK_WINDOW, QMP, QMPBusy, QMPError, Observation, png_size

RECAPTURE_HINT_WINDOW = 3.0   # s after an action in which a capture counts as its capture_after
_RECAPTURE_HINT = re.compile(r"; capture (?:again \(before clicking\)|before repeating)")
EDK2_FIRST_WAIT = 2.0   # s: longest a capture waits for the firmware font table being built

GUIDE = ("Raw machine display, 0 elements by design: use `key`, or click/drag/scroll by pixel coordinate of "
         "THIS image. Capture after every action (or pass capture_after=true); never type blind. A still "
         "screen does not mean the operation finished: look for a prompt, progress or console cursor.")
GUIDE_FIRST = (
    " To enter firmware setup: right after power-on or reset send `key` with `escape*40` (escape pressed 40 times) "
    "in ONE call (keys sent before the firmware is up are lost, so over-send); if the barehand_machine tool "
    "exists, `reset` with enter_setup=true does this for you. Then arrows and return. If barehand_machine "
    "exists, snapshot_save before any risky step.")

GUIDE_FIRST_VNC = (
    " This machine is reached over VNC: only screen, keyboard and pointer exist. There are no snapshots, reset, "
    "power or media controls, so be careful with irreversible steps. Keys follow a US layout.")

MODEL_ERROR = (" [raw display: delivery_mode/bring_to_front do nothing here and an `escalate` hint about "
               "foreground delivery does not apply; fix the arguments and retry once]")
MODEL_MACHINE = (" [machine/QMP problem, not a delivery-mode problem: do not retry with delivery_mode; "
                 "re-capture, and tell the user if it persists]")
log = logging.getLogger("barehand.backend")
VOLATILE_TTL = 600.0     # seconds a region that was seen changing by itself stays ignored
PRIOR_MAX_AGE = 60.0     # the previous picture counts as evidence of "already changing" only if this recent
_MODIFIER_NAMES = {"cmd", "shift", "option", "alt", "ctrl", "win", "windows", "super", "meta", "control"}
MAX_TYPE_CHARS = 2000
_RECOMMEND = {"bad_key": "fix_arguments", "missing_coordinate": "fix_arguments", "bad_button": "fix_arguments",
              "bad_direction": "fix_arguments", "bad_config": "ask_user", "unsupported_action": "use_keyboard",
              "no_machine": "ask_user", "vnc_auth": "ask_user", "vnc_unavailable": "ask_user", "qmp_busy": "retry_once", "qmp_unavailable": "ask_user",
              "qmp_error": "recapture", "vm_not_running": "reset_machine", "type_too_long": "split_text",
              "type_text_incomplete": "recapture", "internal_error": "recapture"}
_MACHINE_CODES = {"no_machine", "vnc_auth", "vnc_unavailable", "qmp_busy", "qmp_unavailable", "qmp_error", "vm_not_running", "internal_error",
                  "bad_config", "type_text_incomplete"}


class _Failure(Exception):
    def __init__(self, code: str, message: str, meta: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code, self.message, self.meta = code, message, meta or {}


_WARNED_SETTINGS: set = set()


def _float(settings: Mapping[str, Any], key: str, default: float, lo: float = 0.0, hi: float = 1e6) -> float:
    """A numeric setting; a value outside ``lo..hi`` or not a number falls back to the default and is logged once."""
    raw = settings.get(key)
    if raw is None or raw == "":
        return default
    try:
        v = float(raw)
        if lo <= v <= hi and v == v:
            return v
    except (TypeError, ValueError):
        pass
    if (key, str(raw)) not in _WARNED_SETTINGS:
        _WARNED_SETTINGS.add((key, str(raw)))
        log.warning("barehand: setting %s=%r is invalid (a number from %g to %g is needed); using %g",
                    key, raw, lo, hi, default)
    return default


def _fail(action: str, code: str, message: str, meta: Optional[Dict[str, Any]] = None,
          path: str = "qmp") -> ActionResult:
    steer = MODEL_MACHINE if code in _MACHINE_CODES else MODEL_ERROR
    return ActionResult(ok=False, action=action, code=code, message=message + steer, meta=meta or {},
                        path=path, escalation={"recommended": _RECOMMEND.get(code, "recapture"),
                                                "reason": "raw machine display; see message"})


class BarehandBackend(ComputerUseBackend):
    def __init__(self, permission_mode: str = "standard", settings: Optional[Mapping[str, Any]] = None):
        self.permission_mode = permission_mode  # informational only: approvals live in the tool, never here
        self._settings_override = None if settings is None else dict(settings)
        self._lock = threading.RLock()
        self._size: Optional[Tuple[int, int]] = None   # size of the last image the model was handed
        self._captures = 0
        self._text_state = screentext.TextState()
        self._endpoint: Optional[str] = None
        self._kind = "qmp"          # transport of the last resolved endpoint: "qmp" | "vnc"
        # tool.py reads these two for capture_after / app guards; there is no app concept here.
        self._last_app: Optional[str] = None
        self._last_target: Optional[Dict[str, Any]] = None
        # (signature, frame digest before, digests equivalent to it modulo a blinking caret) of recent inputs
        self._recent: Deque[Tuple[Tuple[Any, ...], bytes, frozenset]] = deque(maxlen=12)
        # the last successful action result and when it was made: a capture taken right after it (capture_after)
        # makes its "capture before repeating" hints redundant
        self._last_result: Optional[Tuple[ActionResult, float]] = None
        # the last picture this backend looked at (capture, or the end of an action's watch) and when
        self._seen: Optional[Tuple[Frame, float]] = None
        # regions that were seen changing by themselves (clock, spinner): (box, display size, monotonic time)
        self._regions: List[Tuple[Box, Tuple[int, int], float]] = []

    # --- plumbing ---------------------------------------------------------------------------
    def _failed(self, action: str, code: str, message: str, meta: Optional[Dict[str, Any]] = None) -> ActionResult:
        return _fail(action, code, message, meta, path=self._kind)

    @property
    def _label(self) -> str:
        return "VNC" if self._kind == "vnc" else "QMP"

    @property
    def settings(self) -> Dict[str, Any]:
        return dict(config.hermes_settings() if self._settings_override is None else self._settings_override)

    def _resolve(self) -> str:
        return self._resolve_full().endpoint or ""

    def _resolve_full(self) -> "config.Resolved":
        try:
            r = config.resolve(self.settings)
        except config.ConfigError as exc:
            raise _Failure("bad_config", str(exc)) from None
        self._kind = r.kind
        if not r.endpoint:
            state = config.read_state(r.machine)
            if state:
                raise _Failure("no_machine", (
                    f"The machine {r.machine!r} has stopped (its QEMU process is gone: it powered off and was "
                    f"closed, or crashed; see {state.get('workdir', '?')}/qemu.log). Ask the user to run "
                    "`hermes barehand up` to start it again."))
            raise _Failure("no_machine", (
                "No machine attached. Ask the user to start one (`hermes barehand up --iso <path>`) or to "
                "attach an existing QEMU via plugins.entries.barehand.settings.qmp (or BAREHAND_QMP)."))
        self._endpoint = r.endpoint
        return r

    @contextlib.contextmanager
    def _qmp(self) -> Iterator[Any]:
        """Yield the transport object for one operation (``QMP`` or ``VNCTransport``; see transport.py)."""
        r = self._resolve_full()
        endpoint = r.endpoint
        last: Optional[Exception] = None
        q: Any = None
        for attempt in range(3):
            try:
                q = transport.open_transport(r)
                break
            except QMPBusy as exc:
                raise _Failure("qmp_busy", f"QMP is busy: another client (a snapshot or the CLI) holds the "
                                           f"machine connection ({exc}); retry in a moment") from None
            except QMPError as exc:   # VNCError: bad password, unsupported security, protocol: never retried
                if self._kind != "vnc":
                    raise
                code = "vnc_auth" if "password" in str(exc).lower() or "auth" in str(exc).lower() else "vnc_unavailable"
                raise _Failure(code, f"VNC at {endpoint}: {exc}") from None
            except (OSError, ConnectionError) as exc:  # refused / closed: machine restarting or gone
                last = exc
                time.sleep(0.3)
                r = self._resolve_full()
                endpoint = r.endpoint
        if q is None:
            raise _Failure("vnc_unavailable" if self._kind == "vnc" else "qmp_unavailable",
                           f"cannot reach {self._label} at {endpoint}: {last}. Is the machine running?")
        try:
            yield q
        finally:
            q.close()

    def _model_size(self, w: int, h: int) -> Tuple[int, int]:
        return fit_dims(w, h, MAX_SIDE)

    def _screen_size(self, q: QMP) -> Tuple[int, int]:
        """Size of the image the model last saw; one screendump if it has not seen any yet."""
        if self._size is None:
            self._size = self._model_size(*(getattr(q, "size", None) or png_size(q.screenshot())))
        return self._size

    # --- lifecycle --------------------------------------------------------------------------
    def start(self) -> None:
        try:
            with self._qmp() as q:
                q.status()
        except _Failure as exc:
            raise RuntimeError(exc.message) from None
        except Exception as exc:
            raise RuntimeError(f"barehand: cannot talk to {'the VNC server' if self._kind == 'vnc' else 'QEMU'} at {self._endpoint}: {exc}") from None

    def stop(self) -> None:
        """Nothing is held open between operations, and the machine belongs to the user: never quit it."""
        self._size = None
        self._text_state = screentext.TextState()

    def is_available(self) -> bool:
        try:
            if config.endpoint_configured(self.settings):
                return True
            from . import machine
            return machine.find_qemu(self.settings) is not None
        except Exception:
            return False

    # --- eyes -------------------------------------------------------------------------------
    def capture(self, mode: str = "som", app: Optional[str] = None, pid: Optional[int] = None,
                window_id: Optional[int] = None) -> CaptureResult:
        # Infrastructure failures raise: the tool reports them as "capture failed: ..." (there is no
        # ActionResult on this path). Everything else about the display is returned honestly.
        # One screendump (raw PPM) serves the model's PNG, the recorder and the "what did it look like" memory.
        try:
            with self._lock, self._qmp() as q:
                frame = q.frame()
                vm = q.status()
        except _Failure as exc:
            raise RuntimeError(exc.message) from None
        except (QMPError, OSError, ConnectionError, ValueError) as exc:
            raise RuntimeError(f"screenshot failed: {exc}") from None
        self._note_seen(frame)
        self._drop_recapture_hints()
        block = self._screen_block(frame, full=True)
        cap = self._capture_result(frame_to_png(frame, 6), mode, app, vm, block)
        self._rec(self._led(), "capture", {"mode": mode},
                  ActionResult(ok=True, action="capture", message=f"{cap.width}x{cap.height}"), post=frame)
        return cap

    def _note_seen(self, frame: Optional[Frame]) -> None:
        if frame is not None:
            self._seen = (frame, time.monotonic())

    def _screen_block(self, frame: Optional[Frame], full: bool = False) -> str:
        """What the model is told about the screen text: the fenced block, a one-line "unchanged", a delta of a
        few rows, or one line saying why the layer is off (once per state change). "" on any failure. A capture
        passes ``full=True``."""
        try:
            screentext.ensure_edk2(None, self.settings, wait=EDK2_FIRST_WAIT)
            return screentext.block_for(frame, self._text_state, full)
        except Exception:
            return ""

    def _drop_recapture_hints(self) -> None:
        """A capture within a couple of seconds of an action is its capture_after (the backend is not told): the
        picture is attached, so the result's "capture before repeating" style hints would be redundant."""
        try:
            last, self._last_result = self._last_result, None
            if last is not None and time.monotonic() - last[1] < RECAPTURE_HINT_WINDOW and last[0].message:
                msg = _RECAPTURE_HINT.sub("", last[0].message)
                last[0].message = msg.replace("capture (or pass capture_after=true) to look at what the screen "
                                              "shows, then try something different",
                                              "look at the attached screenshot, then try something different")
        except Exception:
            pass

    def _capture_result(self, png: bytes, mode: str, app: Optional[str], vm: str = "running",
                        block: str = "") -> CaptureResult:
        png, w, h, shrunk = shrink_png(png, MAX_SIDE)
        self._size = (w, h)
        note = GUIDE + ((GUIDE_FIRST_VNC if self._kind == "vnc" else GUIDE_FIRST) if self._captures == 0 else "")
        self._captures += 1
        if vm not in config.RUNNING_OK:
            note = (f"MACHINE IS NOT RUNNING (status: {vm}); this is its last frame and input is ignored "
                    + ("(the guest powered off: reset powers it back on). " if vm == "shutdown" else
                       "(paused: resume or reset it). ") + note)
        if mode == "ax" and screentext.is_text_block(block):   # the text-only lane: exact text, no image (the tool drops it for mode ax)
            self._size = (w, h)
            note = ((f"MACHINE IS NOT RUNNING (status: {vm}); this is its last frame and input is ignored. "
                     if vm not in config.RUNNING_OK else "") +
                    f"(mode=ax: text only; call capture with mode=vision for the image. Row and column numbers "
                    f"are text-grid cells, not pixels.)\n{block}")
            return CaptureResult(mode="ax", width=w, height=h, png_b64="", png_bytes_len=0,
                                 image_mime_type="image/png", note=note, app="", window_title="")
        if mode == "ax":
            note = "(mode=ax: this screen could not be read as text, so here is the screenshot.) " + note
        elif mode == "som":
            note = f"(mode={mode}: a raw display has no elements, so this is a plain screenshot.) " + note
        if shrunk:
            note = f"Image downscaled to {w}x{h}; coordinates refer to this image. " + note
        if app not in (None, "", "screen"):
            note = f"app={app!r} is ignored (the whole machine display is the only target). " + note
        if block:
            note += "\n" + block
        # mode is "vision" here: the tool drops the image for mode "ax", which is only used for the text lane.
        return CaptureResult(mode="vision", width=w, height=h, png_b64=base64.b64encode(png).decode("ascii"),
                             png_bytes_len=len(png), image_mime_type="image/png", note=note,
                             app="", window_title="")

    # --- what the screen looked like / what changes by itself -----------------------------------------------
    def _volatile(self, size: Tuple[int, int]) -> Tuple[Box, ...]:
        """Regions of a display of this size that were seen changing by themselves recently."""
        now = time.monotonic()
        self._regions = [r for r in self._regions if now - r[2] < VOLATILE_TTL]
        return tuple(r[0] for r in self._regions if r[1] == size)

    def _learn_volatile(self, box: Box, size: Tuple[int, int]) -> None:
        # a margin: the next tick of a clock may touch cells that the few ticks seen so far did not
        box = (max(0, box[0] - 8), max(0, box[1] - 4), min(size[0] - 1, box[2] + 8), min(size[1] - 1, box[3] + 4))
        for i, (old, osize, _ts) in enumerate(self._regions):
            if osize == size and boxes_touch(old, box):
                box = union_box(old, box) or box
                del self._regions[i]
                break
        self._regions.append((box, size, time.monotonic()))
        del self._regions[:-4]

    def _pre_state(self, baseline: Optional[Frame], mask: Tuple[Box, ...]) -> Tuple[str, Optional[float]]:
        """(display state at the moment of the input, age in s of the previous picture if the screen had already
        changed substantially since it). Cheap; both parts are best effort."""
        if baseline is None:
            return "active", None
        state = display_state(baseline)
        if state == "active":
            try:
                from .machine import placeholder_shown   # the same placeholder at any size (virtio-gpu: 1280x800)
                if placeholder_shown(baseline):
                    state = "banner"
            except Exception:
                pass
        prior: Optional[float] = None
        if self._seen is not None:
            old, ts = self._seen
            age = time.monotonic() - ts
            if age <= PRIOR_MAX_AGE and (old.width, old.height) == (baseline.width, baseline.height):
                a, b = (mask_frame(old, mask), mask_frame(baseline, mask)) if mask else (old, baseline)
                if a.digest != b.digest and not cursor_like(diff_box(a, b), b.width, b.height):
                    prior = age
        return state, prior

    # --- shared action runner -------------------------------------------------------------------
    def _settle(self, q: QMP, baseline: Any = None, action: str = "",
                pre: Tuple[str, Optional[float]] = ("active", None)) -> Tuple[str, str, Observation]:
        """Wait for the screen to go quiet after an input; returns (sentence for the result, effect, watch).

        ``confirmed`` only when the screen changed after the input and nothing known explains it otherwise;
        everything else is ``unverifiable`` and says what was seen. ``suspected_noop`` is never produced."""
        s = self.settings
        time.sleep(_float(s, "settle_delay", 0.2))  # let the guest start reacting
        quiet = _float(s, "settle_quiet", 0.7)
        size = None if baseline is None else (baseline.width, baseline.height)
        mask = self._volatile(size) if size else ()
        obs = q.watch(duration=_float(s, "settle_timeout", 8.0), quiet=quiet,
                      poll=max(0.02, _float(s, "settle_poll", 0.25)),
                      baseline=baseline, blink_window=_float(s, "settle_blink", BLINK_WINDOW, 0.05, 60.0),
                      mask=mask)
        if obs.ticking and obs.minor_box is not None and size:
            self._learn_volatile(obs.minor_box, size)
        return self._judge(obs, action, pre)

    def _judge(self, obs: Observation, action: str, pre: Tuple[str, Optional[float]]) -> Tuple[str, str, Observation]:
        state, prior_age = pre
        what = {"type": "the typed text", "click": "the click", "drag": "the drag",
                "scroll": "the scroll"}.get(action, "the key")
        shown = self._size
        now = self._model_size(*obs.size)
        before = self._model_size(*obs.first_size) if obs.first_size != (0, 0) else now
        ignored = ""
        if obs.masked:
            ignored = ("; ignored " + ", ".join(f"the {b[2] - b[0] + 1}x{b[3] - b[1] + 1} region at ({b[0]},{b[1]})"
                                                for b in obs.masked) + " (it changes by itself)")
        if before != now or (shown is not None and now != shown):
            was = before if before != now else shown
            return (f"display resolution changed from {was[0]}x{was[1]} to {now[0]}x{now[1]}, so the picture "
                    f"cannot be compared with the one you last saw and a change cannot be attributed to "
                    f"{what}; capture again (before clicking){ignored}"), "unverifiable", obs
        note = "; the display was blank before the input" if state == "blank" else ""
        if state == "banner":
            seen = "the screen changed afterwards" if obs.changed else "the screen did not change afterwards"
            return (f"the display was not initialised yet (QEMU's 'Display output is not active' banner) when the "
                    f"input was sent, so the guest may not have received it; {seen}, and that does not show "
                    f"{what} had an effect; capture before repeating{ignored}"), "unverifiable", obs
        if obs.ticking:
            b = obs.minor_box or (0, 0, 0, 0)
            return (f"a small region ({b[2] - b[0] + 1}x{b[3] - b[1] + 1} px at ({b[0]},{b[1]})) keeps changing by "
                    f"itself (a clock or spinner?), so after {obs.elapsed:.1f} s the screen shows no effect of "
                    f"{what} that can be told apart from it; that region is ignored from now on; capture "
                    f"before repeating{note}"), "unverifiable", obs
        if obs.changed:
            if prior_age is not None:
                return (f"the screen changed after the input, but it had already changed substantially since the "
                        f"last picture seen {prior_age:.0f} s earlier (something was running, or a machine action "
                        f"happened), so the change cannot be attributed to {what}; capture before "
                        f"repeating{ignored}{note}"), "unverifiable", obs
            msg = (f"screen changed, then settled{ignored}{note}" if obs.settled else
                   f"screen changed and was still changing after {obs.elapsed:.0f}s (progress or animation; last "
                   f"change {obs.last_change_ago or 0:.1f}s ago){ignored}{note}")
            return msg, "confirmed", obs
        if obs.real_changes:
            return (f"the screen changed after the input and then went back to the picture from before it (a "
                    f"transient or an animation), so {what} cannot be confirmed; capture before "
                    f"repeating{ignored}{note}"), "unverifiable", obs
        if obs.settled:
            extra = (" (typed text may be hidden, e.g. a password prompt; or the guest is not reading the keyboard yet: wait, then test with a harmless command)" if action == "type" else "")
            return (f"screen did not change within {obs.elapsed:.1f} s; {what} may have had no visible effect, or "
                    f"the machine is busy{extra}; capture before repeating{ignored}{note}"), "unverifiable", obs
        return f"screen did not settle within {obs.elapsed:.0f}s but showed no change{ignored}{note}", "unverifiable", obs

    def _led(self, settings: Optional[Mapping[str, Any]] = None) -> Optional[ledger.Ledger]:
        try:
            s = self.settings if settings is None else settings
            return ledger.for_machine(config.machine_name(s), s)
        except Exception:
            return None

    def _rec(self, led: Optional[ledger.Ledger], action: str, args: Optional[Mapping[str, Any]], res: ActionResult, *,
             pre: Optional[Frame] = None, post: Optional[Frame] = None, t0: Optional[float] = None) -> None:
        """Flight-recorder entry; the recorder itself swallows its errors, this guards the lookup."""
        try:
            if led is not None:
                led.record(action, args, ok=res.ok, pre=pre, post=post, effect=res.effect, message=res.message,
                           code=res.code, elapsed=None if t0 is None else time.monotonic() - t0)
        except Exception:
            pass

    def _repeat_note(self, sig: Tuple[Any, ...], pre: Optional[Frame], obs: Optional[Observation],
                     action: str) -> str:
        """Advisory only: the same input sent 3+ times in a row while the screen looked the same before each."""
        if pre is None or obs is None:
            self._recent.clear()
            return ""
        equiv = obs.blink_digests if not obs.real_changes else frozenset({obs.first_digest})
        self._recent.append((sig, obs.first_digest, equiv))
        n, items = 1, list(self._recent)
        for i in range(len(items) - 1, 0, -1):
            cur, prev = items[i], items[i - 1]
            if cur[0] == sig and prev[0] == sig and cur[1] in prev[2]:
                n += 1
            else:
                break
        if n < 3:
            return ""
        return (f"; NOTE: the same {action} (identical arguments) has now been sent {n} times in a row and the "
                "screen looked the same before every one of them. Repeating it again is unlikely to help: "
                "capture (or pass capture_after=true) to look at what the screen shows, then try something "
                "different")

    def _run(self, action: str, body: Callable[[QMP], str], *, settle: bool = True,
             args: Optional[Mapping[str, Any]] = None, sig: Optional[Tuple[Any, ...]] = None) -> ActionResult:
        t0 = time.monotonic()
        baseline: Optional[Frame] = None
        s = self.settings
        led = self._led(s)
        try:
            with self._lock, self._qmp() as q:
                vm = q.status()
                if vm not in config.RUNNING_OK:
                    res = self._failed(action, "vm_not_running", (
                        f"the machine is {vm} and ignores input" +
                        (": the guest powered off; the barehand_machine tool's `reset` powers it back on "
                         "(if you do not have that tool, ask the user)"
                         if vm == "shutdown" else ": it must be resumed or reset (barehand_machine `reset`, or ask the user)")),
                        {"vm_status": vm})
                    self._rec(led, action, args, res, t0=t0)
                    return res
                try:
                    baseline = q.frame() if settle else None
                except Exception:
                    baseline = None  # watching is best effort; never block the input on it
                pre = self._pre_state(baseline, self._volatile((baseline.width, baseline.height))
                                      if baseline else ())
                try:
                    msg = body(q)
                except _Failure:
                    raise
                except QMPError as exc:
                    res = self._failed(action, "qmp_error", str(exc))
                    self._rec(led, action, args, res, pre=baseline, t0=t0)
                    return res
                except (OSError, ConnectionError) as exc:
                    res = self._failed(action, "qmp_unavailable", f"lost the {self._label} connection: {exc}")
                    self._rec(led, action, args, res, pre=baseline, t0=t0)
                    return res
                meta: Dict[str, Any] = {}
                effect, obs = "unverifiable", None
                if settle:  # the input is already delivered: a failure to WATCH must not look like a failed input
                    try:
                        text, effect, obs = self._settle(q, baseline, action, pre)
                        msg += "; " + text
                        self._note_seen(obs.last_frame)
                        block = self._screen_block(obs.last_frame)
                        if block:
                            msg += "\n" + block
                    except Exception as exc:
                        meta["observe_failed"] = str(exc)
                        msg += (f"; input was delivered but the screen could not be observed afterwards "
                                f"({exc}); capture to check, do not repeat the input")
                if sig is not None and settle:
                    msg += self._repeat_note(sig, baseline, obs, action)
                res = ActionResult(ok=True, action=action, message=msg, meta=meta, effect=effect, path=self._kind)
                self._last_result = (res, time.monotonic())
                self._rec(led, action, args, res, pre=baseline, post=None if obs is None else obs.last_frame, t0=t0)
                return res
        except _Failure as exc:
            res = self._failed(action, exc.code, exc.message, exc.meta)
            self._rec(led, action, args, res, pre=baseline, t0=t0)
            return res
        except Exception as exc:  # contract: never raise out of an action
            res = self._failed(action, "internal_error", f"{type(exc).__name__}: {exc}")
            self._rec(led, action, args, res, pre=baseline, t0=t0)
            return res

    def _rejected(self, action: str, args: Optional[Mapping[str, Any]], code: str, message: str) -> ActionResult:
        """An input refused before anything was sent: still recorded, with the reason."""
        res = self._failed(action, code, message)
        self._rec(self._led(), action, args, res)
        return res

    def _unsupported(self, action: str, why: str, args: Optional[Mapping[str, Any]] = None) -> ActionResult:
        return self._rejected(action, args or {}, "unsupported_action", why)

    NO_ELEMENTS = ("element targeting is unsupported: a raw display has no accessibility elements. "
                   "Use the keyboard, or pass `coordinate` in pixels of the latest screenshot.")

    def _modifier_qcodes(self, modifiers: Optional[Sequence[str]]) -> List[str]:
        out: List[str] = []
        for m in modifiers or []:
            if m.lower() not in _MODIFIER_NAMES:
                raise _Failure("bad_key", f"modifier {m!r} is not supported on a raw machine "
                                          "(use shift, ctrl, alt, or cmd/win/super)")
            out.extend(keymap.combo_to_qcodes(m))
        return out

    # --- hands: keyboard ----------------------------------------------------------------------
    def key(self, keys: str, *, delivery_mode: Optional[str] = None, bring_to_front: bool = False) -> ActionResult:
        try:
            chords = keymap.parse_keys(keys)
        except keymap.KeyError_ as exc:
            return self._rejected("key", {"keys": keys}, "bad_key", str(exc))
        if any({"ctrl", "ctrl_r"} & set(c) and {"alt", "alt_r"} & set(c) and "delete" in c for c in chords):
            return self._rejected("key", {"keys": keys}, "bad_key",
                                  "ctrl+alt+delete is not sent through `key` (any spelling); use barehand_machine "
                                  "action `send_ctrl_alt_del`, which asks the user for approval")
        hold = int(_float(self.settings, "key_hold_ms", 60))
        shown = " ".join(keys.split())
        shown = shown if len(shown) <= 60 else shown[:57] + "..."

        def body(q: QMP) -> str:
            done = 0
            try:
                for chord in chords:
                    q.send_chord(chord, hold_ms=hold)
                    done += 1
            except (QMPError, OSError, ConnectionError) as exc:
                raise _Failure("qmp_error", f"pressed {done} of {len(chords)} keys before failing: {exc}",
                               {"delivered_keys": done, "requested_keys": len(chords)}) from None
            return f"pressed {shown}" if len(chords) == 1 else f"pressed {len(chords)} keys ({shown})"
        return self._run("key", body, args={"keys": keys}, sig=("key", " ".join(keys.split())))

    def type_text(self, text: str, *, delivery_mode: Optional[str] = None,
                  bring_to_front: bool = False) -> ActionResult:
        text = text or ""
        if len(text) > MAX_TYPE_CHARS:
            return self._rejected("type", {"text": text}, "type_too_long", f"nothing typed: {len(text)} characters "
                                  f"exceeds the {MAX_TYPE_CHARS}-character limit per call; split the text")
        try:
            chords = keymap.text_to_chords(text)
        except keymap.KeyError_ as exc:
            return self._rejected("type", {"text": text}, "bad_key", f"nothing typed: {exc}")
        if not chords:
            res = ActionResult(ok=True, action="type", message="nothing to type")
            self._rec(self._led(), "type", {"text": text}, res)
            return res
        hold = int(_float(self.settings, "type_hold_ms", 30))
        delivered = 0

        def body(q: QMP) -> str:
            nonlocal delivered
            try:
                for chord in chords:
                    q.send_chord(chord, hold_ms=hold)
                    delivered += 1
            except (QMPError, OSError, ConnectionError) as exc:
                raise _Failure("type_text_incomplete",
                               f"typed {delivered} of {len(chords)} characters before failing: {exc}",
                               {"delivered_chars": delivered, "requested_chars": len(chords)}) from None
            return f"typed {len(chords)} characters (US layout)"
        res = self._run("type", body, args={"text": text}, sig=("type", text))
        res.meta.setdefault("requested_chars", len(chords))
        res.meta.setdefault("delivered_chars", delivered)
        return res

    # --- hands: pointer -------------------------------------------------------------------------
    def click(self, *, element: Optional[int] = None, x: Optional[int] = None, y: Optional[int] = None,
              button: str = "left", click_count: int = 1, modifiers: Optional[list[str]] = None,
              delivery_mode: Optional[str] = None, bring_to_front: bool = False) -> ActionResult:
        req = {"x": x, "y": y, "button": button, "click_count": click_count, "modifiers": list(modifiers or [])}
        if element is not None:
            return self._unsupported("click", self.NO_ELEMENTS, dict(req, element=element))
        if x is None or y is None:
            return self._rejected("click", req, "missing_coordinate",
                                  "click needs coordinate=[x, y] in pixels of the latest screenshot")
        if button not in ("left", "right", "middle"):
            return self._rejected("click", req, "bad_button", f"unknown button {button!r}")
        try:
            mods = self._modifier_qcodes(modifiers)
        except _Failure as exc:
            return self._rejected("click", req, exc.code, exc.message)
        count = max(1, min(int(click_count or 1), 3))

        def body(q: QMP) -> str:
            w, h = self._screen_size(q)
            q.click(x, y, w, h, button=button, count=count, modifiers=mods)
            return f"{button} click x{count} at ({x}, {y}) of {w}x{h}"
        return self._run("click", body, args={"x": x, "y": y, "button": button, "click_count": count,
                                              "modifiers": list(modifiers or [])},
                         sig=("click", x, y, button, count, tuple(modifiers or ())))

    def drag(self, *, from_element: Optional[int] = None, to_element: Optional[int] = None,
             from_xy: Optional[tuple[int, int]] = None, to_xy: Optional[tuple[int, int]] = None,
             button: str = "left", modifiers: Optional[list[str]] = None,
             delivery_mode: Optional[str] = None, bring_to_front: bool = False) -> ActionResult:
        req = {"from_xy": list(from_xy) if from_xy else None, "to_xy": list(to_xy) if to_xy else None,
               "button": button, "modifiers": list(modifiers or [])}
        if from_element is not None or to_element is not None:
            return self._unsupported("drag", self.NO_ELEMENTS, dict(req, from_element=from_element,
                                                                    to_element=to_element))
        if not from_xy or not to_xy:
            return self._rejected("drag", req, "missing_coordinate",
                                  "drag needs from_coordinate and to_coordinate in screenshot pixels")
        if button not in ("left", "right", "middle"):
            return self._rejected("drag", req, "bad_button", f"unknown button {button!r}")
        try:
            mods = self._modifier_qcodes(modifiers)
        except _Failure as exc:
            return self._rejected("drag", req, exc.code, exc.message)

        def body(q: QMP) -> str:
            w, h = self._screen_size(q)
            q.drag((from_xy[0], from_xy[1]), (to_xy[0], to_xy[1]), w, h, button=button, modifiers=mods)
            return f"dragged {tuple(from_xy)} -> {tuple(to_xy)} of {w}x{h}"
        return self._run("drag", body, args={"from_xy": list(from_xy), "to_xy": list(to_xy), "button": button,
                                             "modifiers": list(modifiers or [])},
                         sig=("drag", tuple(from_xy), tuple(to_xy), button, tuple(modifiers or ())))

    def scroll(self, *, direction: str, amount: int = 3, element: Optional[int] = None,
               x: Optional[int] = None, y: Optional[int] = None, modifiers: Optional[list[str]] = None,
               delivery_mode: Optional[str] = None, bring_to_front: bool = False) -> ActionResult:
        req = {"direction": direction, "amount": amount, "x": x, "y": y, "modifiers": list(modifiers or [])}
        if element is not None:
            return self._unsupported("scroll", self.NO_ELEMENTS, dict(req, element=element))
        if direction not in ("up", "down", "left", "right"):
            return self._rejected("scroll", req, "bad_direction", "direction must be up, down, left or right")
        if (x is None) != (y is None):
            return self._rejected("scroll", req, "missing_coordinate",
                                  "give both x and y of the scroll position, or neither")
        try:
            mods = self._modifier_qcodes(modifiers)
        except _Failure as exc:
            return self._rejected("scroll", req, exc.code, exc.message)
        ticks = max(1, min(int(amount or 1), 50))

        def body(q: QMP) -> str:
            w, h = self._screen_size(q)
            q.wheel(direction, ticks, None if x is None else (x, y), w, h, modifiers=mods)
            return f"scrolled {direction} x{ticks}" + ("" if x is None else f" at ({x}, {y})")
        return self._run("scroll", body, args={"direction": direction, "amount": ticks, "x": x, "y": y,
                                               "modifiers": list(modifiers or [])},
                         sig=("scroll", direction, ticks, x, y, tuple(modifiers or ())))

    # --- things a raw machine does not have -------------------------------------------------------
    def list_apps(self) -> list[dict[str, Any]]:
        return []  # the ABC returns a list: an empty one (no apps exist on a raw display)

    def focus_app(self, app: str, raise_window: bool = False) -> ActionResult:
        return self._unsupported("focus_app", "a raw machine display has no apps or windows; there is only "
                                              "one target, the whole screen", {"app": app})

    def set_value(self, value: str, element: Optional[int] = None) -> ActionResult:
        return self._unsupported("set_value", "set_value needs accessibility elements; a raw display has "
                                              "none. Type the text with `type` instead.", {"element": element})

    # --- time ---------------------------------------------------------------------------------------
    def wait(self, seconds: float) -> ActionResult:
        """Wait ``seconds`` (capped at 30). Returns early only when the screen visibly changed and then stayed
        still for max(5 s, 10% of the request); the message states which happened. A still screen is never
        reported as 'done': silent phases (kernel boot, installer steps) look exactly like finished ones."""
        try:
            seconds = max(0.0, min(float(seconds), 30.0))
        except (TypeError, ValueError):
            seconds = 1.0
        early = max(5.0, seconds * 0.1)
        st = self.settings
        led = self._led(st)
        blink = _float(st, "settle_blink", BLINK_WINDOW, 0.05, 60.0)
        try:
            with self._lock, self._qmp() as q:
                if seconds < 0.5:
                    time.sleep(seconds)
                    res = ActionResult(ok=True, action="wait", message=f"waited {seconds:.1f}s",
                                       meta={"early": False, "waited": round(seconds, 2)})
                    self._rec(led, "wait", {"seconds": seconds}, res)
                    return res
                mask = self._volatile((self._seen[0].width, self._seen[0].height)) if self._seen else ()
                obs = q.watch(duration=seconds, quiet=early, poll=0.5, early_quiet=early, blink_window=blink,
                              mask=mask)
        except _Failure as exc:
            if exc.code in ("no_machine", "bad_config", "vnc_auth"):
                res = self._failed("wait", exc.code, exc.message)
            else:
                time.sleep(seconds)
                res = ActionResult(ok=True, action="wait", message=f"waited {seconds:.1f}s (machine not observed: "
                                                                   f"{exc.message})")
            self._rec(led, "wait", {"seconds": seconds}, res)
            return res
        except Exception as exc:
            time.sleep(seconds)
            res = ActionResult(ok=True, action="wait", message=f"waited {seconds:.1f}s (machine not observed: {exc})")
            self._rec(led, "wait", {"seconds": seconds}, res)
            return res
        self._note_seen(obs.last_frame)
        block = self._screen_block(obs.last_frame)
        meta = {"waited": round(obs.elapsed, 2), "real_changes": obs.real_changes, "changed": obs.changed}
        small = " (only a small region changed)" if obs.real_changes and not obs.major else ""
        if obs.real_changes and obs.elapsed < seconds - 0.6:
            meta["early"] = True
            msg = (f"returned after {obs.elapsed:.1f}s of {seconds:.0f}s: the screen changed{small}, then stayed "
                   f"still for {obs.quiet_for:.1f}s. Still does not mean the operation is finished; capture and check")
        else:
            meta["early"] = False
            if not obs.real_changes:
                msg = f"waited the full {obs.elapsed:.1f}s; no visible change (a blinking cursor is ignored)"
            elif obs.quiet_for < 1.5:
                msg = (f"waited the full {obs.elapsed:.1f}s; the screen was still changing at the end{small} "
                       f"(last change {obs.last_change_ago or 0:.1f}s ago, {obs.real_changes} changes)")
            else:
                msg = (f"waited the full {obs.elapsed:.1f}s; the screen changed{small} and has been still for the "
                       f"last {obs.quiet_for:.1f}s")
        if block:
            msg += "\n" + block
        res = ActionResult(ok=True, action="wait", message=msg, meta=meta, path=self._kind)
        self._rec(led, "wait", {"seconds": seconds}, res, pre=obs.first_frame, post=obs.last_frame)
        return res
