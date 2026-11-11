"""Flight recorder, viewer, recordings CLI, and the machine-tool / prompt / hook surface."""

import json
import logging
import os
import shutil
import stat
import struct
import subprocess
import zlib

import pytest

from barehand import cli, companion, config, ledger, machine
from barehand.backend import BarehandBackend
from barehand.frames import parse_ppm


@pytest.fixture(autouse=True)
def fresh_registry():
    ledger.reset_registry()
    yield
    ledger.reset_registry()


@pytest.fixture
def quick(fake):
    return BarehandBackend(settings={"qmp": fake.address, "settle_quiet": 0.1, "settle_timeout": 1.0,
                                     "settle_delay": 0.02, "settle_poll": 0.03, "settle_blink": 0.2,
                                     "record_typed_text": True})


def session():
    sessions = ledger.list_sessions()
    assert len(sessions) == 1
    return sessions[0]["path"]


def load_records(path):
    p = os.fspath(path)
    p = os.path.join(p, ledger.LEDGER_FILE) if os.path.isdir(p) else p
    out = []
    for line in open(p, encoding="utf-8").read().split("\n"):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def records(path=None):
    return load_records(path or session())


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


# --- recording ----------------------------------------------------------------------------------------
def test_one_record_per_action_with_exact_arguments_digests_and_effect(quick, fake):
    quick.key("ctrl+alt+f2")
    quick.type_text("hello\n")
    quick.click(x=10, y=20, button="right", click_count=2, modifiers=["shift"])
    quick.drag(from_xy=(1, 2), to_xy=(30, 40))
    quick.scroll(direction="down", amount=4)
    quick.capture()
    quick.wait(0.1)
    recs = records()
    assert recs[0]["action"] == "session_start"
    acts = [r["action"] for r in recs[1:]]
    assert acts == ["key", "type", "click", "drag", "scroll", "capture", "wait"]
    key, typed, click, drag, scroll = recs[1:6]
    assert key["args"] == {"keys": "ctrl+alt+f2"} and typed["args"] == {"text": "hello\n"}
    assert click["args"] == {"x": 10, "y": 20, "button": "right", "click_count": 2, "modifiers": ["shift"]}
    assert drag["args"]["from_xy"] == [1, 2] and scroll["args"]["amount"] == 4
    assert key["effect"] == "confirmed" and key["ok"] is True and "pressed" in key["message"]
    assert key["pre"] and key["post"] and key["pre"] != key["post"]
    assert all(r["text"] is None for r in recs) and [r["seq"] for r in recs] == list(range(len(recs)))
    assert all(isinstance(r["ts"], float) for r in recs) and key["snapshots"] == []


def test_failed_actions_are_recorded_too(quick, fake):
    fake.status = "shutdown"
    quick.key("return")
    rec = records()[-1]
    assert rec["ok"] is False and rec["code"] == "vm_not_running" and rec["effect"] is None


def test_rejected_inputs_are_recorded_with_the_reason(quick, fake):
    quick.key("f13")
    quick.type_text("x" * 2001)
    quick.type_text("caf\u00e9")
    quick.click()
    quick.click(x=1, y=1, button="side")
    quick.click(x=1, y=1, modifiers=["fn"])
    quick.click(element=3)
    quick.drag(from_xy=(1, 1))
    quick.scroll(direction="sideways")
    quick.scroll(direction="up", x=3)
    quick.focus_app("firefox")
    quick.set_value("secret", element=1)
    assert not fake.input_events()
    recs = records()[1:]
    assert [r["code"] for r in recs] == ["bad_key", "type_too_long", "bad_key", "missing_coordinate", "bad_button",
                                         "bad_key", "unsupported_action", "missing_coordinate", "bad_direction",
                                         "missing_coordinate", "unsupported_action", "unsupported_action"]
    assert all(r["ok"] is False and r["message"] for r in recs)
    assert recs[0]["args"] == {"keys": "f13"} and recs[3]["action"] == "click"
    assert "secret" not in open(os.path.join(session(), "ledger.jsonl")).read()    # set_value's text is not stored


def test_capture_takes_one_screendump_and_the_recorded_frame_is_the_one_the_model_got(quick, fake):
    before = len(fake.calls("screendump"))
    cap = quick.capture()
    assert len(fake.calls("screendump")) == before + 1
    rec = records()[-1]
    assert rec["action"] == "capture" and rec["frame_post"] and rec["post"]
    assert cap.width == 128 and cap.height == 96


def test_pngs_are_native_resolution_and_deduplicated_by_digest(quick, fake):
    fake.react_to_input = False
    for _ in range(3):
        quick.key("return")
    fake.react_to_input = True
    quick.key("return")
    d = session()
    pngs = sorted(os.listdir(os.path.join(d, "frames")))
    assert len(pngs) == 2                      # the unchanged frame once, the changed frame once
    data = open(os.path.join(d, "frames", pngs[0]), "rb").read()
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and struct.unpack(">II", data[16:24]) == (128, 96)
    raw = zlib.decompress(data[data.index(b"IDAT") + 4:data.index(b"IEND") - 8])
    assert len(raw) == 96 * (128 * 3 + 1)
    recs = records()
    assert recs[1]["frame_pre"] == recs[2]["frame_pre"] == recs[3]["frame_pre"] == recs[1]["frame_post"]
    assert recs[4]["frame_post"] != recs[4]["frame_pre"]


def test_files_are_private(quick):
    quick.key("return")
    d = session()
    for name in ("ledger.jsonl", "ledger.js", "index.html", ".lock"):
        assert mode(os.path.join(d, name)) == 0o600
    assert mode(os.path.join(d, "frames", os.listdir(os.path.join(d, "frames"))[0])) == 0o600
    assert mode(d) == 0o700 and mode(ledger.recordings_dir()) == 0o700


def test_existing_directories_and_files_are_tightened_and_symlinks_refused(tmp_path):
    root = ledger.recordings_path()
    root.parent.mkdir(parents=True, exist_ok=True)
    root.mkdir(mode=0o755)
    os.chmod(root, 0o755)
    ledger.Ledger(root, "m").record("key", {"keys": "a"})
    assert mode(root) == 0o700
    loose = tmp_path / "loose"
    loose.write_text("x")
    os.chmod(loose, 0o644)
    os.close(ledger._open_private(loose, os.O_WRONLY | os.O_APPEND))
    assert mode(loose) == 0o600
    link = tmp_path / "link"
    link.symlink_to(loose)
    if ledger._NOFOLLOW:
        with pytest.raises(OSError):
            ledger._open_private(link, os.O_WRONLY | os.O_APPEND)
    with pytest.raises(OSError):
        ledger._private_dir(link)


def test_record_off_writes_nothing(fake):
    b = BarehandBackend(settings={"qmp": fake.address, "settle_quiet": 0.1, "settle_timeout": 0.5,
                                  "settle_delay": 0.01, "settle_poll": 0.03, "record": False})
    assert b.key("return").ok
    assert ledger.list_sessions() == []


def test_typed_text_is_withheld_by_default_but_chords_and_frames_are_still_recorded(fake):
    b = BarehandBackend(settings={"qmp": fake.address, "settle_quiet": 0.1, "settle_timeout": 0.5,
                                  "settle_delay": 0.01, "settle_poll": 0.03})       # no record_* settings at all
    b.type_text("hunter2")
    b.key("h u n t e r 2")
    recs = records()
    assert recs[1]["args"] == {"text": None, "chars": 7, "redacted": True}
    assert recs[2]["args"] == {"keys": "h u n t e r 2"}               # key chords are NOT redacted
    ledger_text = open(os.path.join(session(), "ledger.jsonl")).read()
    js_text = open(os.path.join(session(), "ledger.js")).read()
    assert "hunter2" not in ledger_text and "hunter2" not in js_text
    assert recs[1]["frame_pre"] and os.listdir(os.path.join(session(), "frames"))      # screenshots are stored


def test_typed_text_can_be_stored_when_asked(quick):
    quick.type_text("hunter2")
    assert records()[-1]["args"] == {"text": "hunter2"}


def test_a_broken_recorder_never_breaks_an_action_and_logs_once(quick, monkeypatch, caplog):
    monkeypatch.setattr(ledger, "frame_to_png", lambda f: (_ for _ in ()).throw(OSError("disk full")))
    with caplog.at_level("WARNING", logger="barehand.ledger"):
        results = [quick.key("return") for _ in range(3)]
    assert all(r.ok for r in results)
    assert len([r for r in caplog.records if "recorder error" in r.message]) == 1


def test_unwritable_data_dir_does_not_break_the_action_and_warns_once(quick, monkeypatch, caplog):
    def nope(path):
        raise PermissionError("nope")
    monkeypatch.setattr(ledger, "_private_dir", nope)
    with caplog.at_level("WARNING", logger="barehand"):
        for _ in range(5):
            assert quick.key("return").ok
            assert quick.capture()
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING and "recorder" in r.message]
    assert len(warnings) == 1


def test_a_registry_that_cannot_resolve_the_data_dir_warns_once(monkeypatch, caplog):
    monkeypatch.setattr(ledger, "recordings_path", lambda: (_ for _ in ()).throw(OSError("no home")))
    with caplog.at_level("WARNING", logger="barehand.ledger"):
        assert [ledger.for_machine("m", {}) for _ in range(4)] == [None] * 4
    assert len([r for r in caplog.records if "unavailable" in r.message]) == 1


# --- retention, liveness, bounds ------------------------------------------------------------------------
def make_old_session(root, name, payload=b""):
    d = root / name
    d.mkdir(parents=True)
    (d / "ledger.jsonl").write_text("{}\n")
    if payload:
        (d / "frames").mkdir()
        (d / "frames" / "0001-x.png").write_bytes(payload)
    return d


def test_retention_keeps_only_the_newest_finished_sessions():
    root = ledger.recordings_path()
    for i in range(5):
        make_old_session(root, f"2026010{i}-000000-default")
    led = ledger.Ledger(root, "default", keep=3)
    led.record("key", {"keys": "a"})
    names = sorted(p.name for p in root.iterdir())
    assert len(names) == 3 and names[:2] == ["20260103-000000-default", "20260104-000000-default"] and led.session_dir.name in names


def test_a_session_another_live_process_is_writing_is_never_pruned_and_never_merged():
    """The reviewer's scenario: an old, still running session, then more launches than `keep` allows."""
    root = ledger.recordings_path()
    a = ledger.Ledger(root, "m", keep=2)
    a.record("key", {"keys": "a"})                    # A: oldest, alive (it holds its flock)
    dir_a = a.session_dir
    others = []
    for i in range(4):
        b = ledger.Ledger(root, "m", keep=2)          # same second, same machine, same pid
        b.record("key", {"keys": f"b{i}"})
        others.append(b)
    assert dir_a.exists() and ledger.session_is_live(dir_a)
    assert len({o.session_dir for o in others} | {dir_a}) == 5                 # names are never reused
    a.record("key", {"keys": "a2"})
    lines = [json.loads(x) for x in (dir_a / "ledger.jsonl").read_text().splitlines()]
    assert [r.get("args", {}).get("keys") for r in lines[1:]] == ["a", "a2"]    # A's ledger holds only A's records
    for o in others:
        text = (o.session_dir / "ledger.jsonl").read_text() if o.session_dir.exists() else ""
        assert '"a2"' not in text
    # once A is finished (closed, or its process gone) it is eligible again
    a.close()
    assert not ledger.session_is_live(dir_a)
    ledger.Ledger(root, "m", keep=2).record("key", {"keys": "z"})
    assert not dir_a.exists()


def test_session_names_carry_the_pid_and_a_random_tag(quick):
    quick.key("return")
    name = os.path.basename(session())
    assert f"-p{os.getpid()}-" in name and name.endswith(tuple("0123456789abcdef"))


def test_session_folders_never_collide(monkeypatch):
    monkeypatch.setattr(ledger.random, "randrange", lambda n: 7)      # even a hostile RNG cannot make two reuse a name
    root = ledger.recordings_path()
    a, b = ledger.Ledger(root, "m"), ledger.Ledger(root, "m")
    a.record("key", {"keys": "a"})
    with pytest.raises(Exception):                                     # 50 tries, all taken: refuse, do not merge
        b._create_session()
    b.record("key", {"keys": "b"})                                     # recorder swallows it
    assert a.session_dir != b.session_dir and b.session_dir is None
    assert [json.loads(x)["args"]["keys"] for x in (a.session_dir / "ledger.jsonl").read_text().splitlines()[1:]] == ["a"]


def test_the_total_size_of_old_sessions_is_capped_when_a_session_starts():
    root = ledger.recordings_path()
    for i in range(4):
        make_old_session(root, f"2026010{i}-000000-default", payload=b"x" * 400_000)
    led = ledger.Ledger(root, "default", keep=10, max_total_bytes=1_000_000)
    led.record("key", {"keys": "a"})
    names = sorted(p.name for p in root.iterdir())
    assert names[:-1] == ["20260102-000000-default", "20260103-000000-default"] and len(names) == 3


def test_a_session_stops_storing_frames_at_its_frame_cap_and_says_so_once(quick, fake, caplog):
    fake.react_to_input = False
    led = ledger.for_machine("default", {"record_max_frames": 2})
    for i in range(6):
        fake.fill(10 + 20 * i)                                         # a new picture every time
        quick.key("return")
    recs = records()
    assert len(os.listdir(os.path.join(session(), "frames"))) == 2
    notes = [r for r in recs if r["action"] == "recorder_note"]
    assert len(notes) == 1 and "frame storage stopped" in notes[0]["message"]
    assert [r["action"] for r in recs if r["action"] == "key"] == ["key"] * 6       # every action still recorded
    assert recs[-1]["frame_pre"] is None and recs[-1]["pre"]                          # digests still there
    assert led is not None


def test_a_session_stops_storing_frames_at_its_byte_cap(quick, fake):
    fake.react_to_input = False
    ledger.for_machine("default", {"record_max_mb": 1})
    led = ledger.for_machine("default", {})
    led.max_bytes = 20_000                                             # a 128x96 PNG is ~ a few hundred bytes
    led._bytes = 19_900
    fake.fill(50)
    quick.key("return")
    fake.fill(120)
    quick.key("return")
    notes = [r for r in records() if r["action"] == "recorder_note"]
    assert len(notes) == 1 and records()[-1]["frame_post"] is None


def test_recording_stops_altogether_after_max_records(quick, monkeypatch):
    monkeypatch.setattr(ledger, "MAX_RECORDS", 3)
    for _ in range(6):
        quick.key("return")
    recs = records()
    assert len(recs) == 4 and recs[-1]["action"] == "recorder_note" and "recording stopped" in recs[-1]["message"]


def cut_first_append_of(monkeypatch, fname):
    """The next append to ``fname`` writes half its bytes and fails (disk full), once."""
    real = ledger.Ledger._append_line
    state = {"done": False}

    def cut(self, name, data):
        if name == fname and not state["done"]:
            state["done"] = True
            fd = ledger._open_private(self._dir / name, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
            os.write(fd, data[: len(data) // 2])
            os.close(fd)
            self._dirty.add(name)
            raise OSError(28, "No space left on device")
        return real(self, name, data)
    monkeypatch.setattr(ledger.Ledger, "_append_line", cut)


def test_a_write_cut_short_does_not_glue_the_next_record_to_it(quick, monkeypatch):
    quick.key("return")
    cut_first_append_of(monkeypatch, ledger.LEDGER_FILE)
    quick.key("return")                                                # this one is cut off
    quick.key("return")
    quick.key("return")
    raw = open(os.path.join(session(), "ledger.jsonl")).read()
    assert raw.endswith("\n") and all(line.startswith("{") for line in raw.splitlines())
    loaded = records()
    assert [r["seq"] for r in loaded][-2:] == [3, 4]                     # the two later records parse
    assert sum(1 for line in raw.splitlines() if not _parses(line)) == 1  # exactly one damaged (the cut) line


def _parses(line):
    try:
        json.loads(line)
        return True
    except ValueError:
        return False


def test_recover_tail_terminates_a_partial_last_line(tmp_path):
    p = tmp_path / "ledger.jsonl"
    p.write_bytes(b'{"a": 1}\n{"b": ')
    assert ledger._recover_tail(p) is True
    assert p.read_bytes().endswith(b'{"b": \n') and ledger._recover_tail(p) is False
    assert [r for r in load_records(p)] == [{"a": 1}]


def test_listing_and_path_do_not_create_directories(tmp_path):
    assert ledger.list_sessions() == []
    assert cli.run(["recordings", "list"]).startswith("no recordings")
    assert cli.run(["recordings", "path"]) == str(ledger.recordings_path())
    assert not ledger.recordings_path().exists()


def test_only_real_sessions_are_listed_pruned_or_purged(quick):
    quick.key("return")
    stranger = ledger.recordings_path() / "my-own-notes"
    stranger.mkdir()
    (stranger / "keep.txt").write_text("mine")
    assert len(ledger.list_sessions()) == 1
    ledger.reset_registry()                                            # this "process" is finished
    assert ledger.purge() == 1
    assert (stranger / "keep.txt").read_text() == "mine"


def test_purge_leaves_a_live_session_alone(quick):
    quick.key("return")
    assert ledger.purge() == 0 and ledger.LAST_PURGE_SKIPPED == 1 and len(ledger.list_sessions()) == 1
    assert ledger.list_sessions()[0]["live"] is True


def test_record_settings_are_validated_and_bad_values_warn_once(caplog):
    with caplog.at_level("WARNING", logger="barehand.ledger"):
        for _ in range(3):
            rs = ledger.parse_settings({"record_keep_sessions": 0, "record_max_frames": -5, "record_max_mb": "lots",
                                        "record_max_total_mb": 0, "record": "off", "record_typed_text": "yes"})
    assert rs.keep == ledger.DEFAULT_KEEP and rs.max_frames == ledger.DEFAULT_MAX_FRAMES
    assert rs.max_bytes == ledger.DEFAULT_MAX_MB * ledger.MiB and rs.enabled is False and rs.typed_text is True
    assert len([r for r in caplog.records if "is invalid" in r.message]) == 4
    ok = ledger.parse_settings({"record_keep_sessions": "3", "record_max_frames": 0, "record_max_mb": 5})
    assert (ok.keep, ok.max_frames, ok.max_bytes, ok.typed_text, ok.enabled) == (3, 0, 5 * ledger.MiB, False, True)


def test_machine_tool_calls_are_recorded_with_snapshot_names(fake, monkeypatch):
    monkeypatch.setenv("BAREHAND_QMP", fake.address)
    fake.hmp_replies["info"] = ""
    companion.handle({"action": "snapshot_save", "name": "before-install"})
    companion.handle({"action": "status"})
    b = BarehandBackend(settings={"qmp": fake.address, "settle_quiet": 0.1, "settle_timeout": 0.5,
                                  "settle_delay": 0.01, "settle_poll": 0.03})
    b.key("return")
    recs = records()
    names = [r["action"] for r in recs]
    assert "machine:snapshot_save" in names and "machine:status" in names
    assert recs[-1]["snapshots"] == ["before-install"]


# --- viewer ---------------------------------------------------------------------------------------------
NASTY = ["<!--<script>", "</script><img src=x onerror=alert(1)>", "a\u2028b\u2029c", "<!--", "<script", "&amp;<"]


def test_viewer_is_static_self_contained_and_loads_its_data_from_a_script_file(quick):
    for t in NASTY:
        quick.type_text(t)
    d = session()
    html = open(os.path.join(d, "index.html")).read()
    assert 'type="range"' in html and '<script src="ledger.js"></script>' in html and "<canvas" in html
    assert "http://" not in html and "https://" not in html and "<link" not in html
    assert "innerHTML" not in html and "document.write" not in html and "eval(" not in html   # data only via textContent
    assert "step=" in html and "keep it private" in html
    assert not any(t in html for t in NASTY[:3]) and "hunter" not in html     # the page holds no recorded data at all
    js = open(os.path.join(d, "ledger.js")).read()
    assert js.startswith("window.BAREHAND=[];\n")
    assert "<" not in js and ">" not in js and "&" not in js and "\u2028" not in js and "\u2029" not in js
    assert len(js.splitlines()) == 1 + len(records())
    assert all(not (r.get("frame_post") or "x").startswith("/") for r in records())


def test_index_html_is_written_once_not_after_every_action(quick):
    quick.key("return")
    path = os.path.join(session(), "index.html")
    first = os.stat(path)
    for _ in range(4):
        quick.key("return")
    after = os.stat(path)
    assert (after.st_ino, after.st_mtime_ns) == (first.st_ino, first.st_mtime_ns)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_ledger_js_executes_and_round_trips_hostile_text(quick, tmp_path):
    for t in NASTY:
        quick.type_text(t)
    script = ("const fs=require('fs'),vm=require('vm');const ctx={};ctx.window=ctx;vm.createContext(ctx);"
              "vm.runInContext(fs.readFileSync(process.argv[1],'utf8')+';window.BAREHAND=BAREHAND;',ctx);"
              "console.log(JSON.stringify(ctx.window.BAREHAND));")
    out = subprocess.run(["node", "-e", script, os.path.join(session(), "ledger.js")], capture_output=True,
                         text=True, timeout=30, check=True).stdout
    loaded = json.loads(out)
    assert [r["args"]["text"] for r in loaded[1:]] == NASTY


def test_ledger_js_is_rewritten_whole_after_a_cut_off_append(quick, monkeypatch):
    quick.key("return")
    cut_first_append_of(monkeypatch, ledger.JS_FILE)
    quick.key("return")
    quick.key("return")
    js = open(os.path.join(session(), "ledger.js")).read().splitlines()
    assert js[0] == "window.BAREHAND=[];" and all(line.startswith("BAREHAND.push(") and line.endswith(");") for line in js[1:])
    assert len(js) == 1 + len(records())


# --- CLI ------------------------------------------------------------------------------------------------
def test_recordings_cli_list_path_purge(quick, monkeypatch):
    assert "no recordings" in cli.run(["recordings", "list"])
    quick.key("return")
    out = cli.run(["recordings", "list"])
    assert out.count("\n") == 0 and " 1 records " in out and "index.html" in out
    assert cli.run(["recordings", "path"]) == str(ledger.recordings_dir())
    assert cli.run(["recordings", "path", "--latest"]) == session()
    monkeypatch.delenv("BAREHAND_ASSUME_YES", raising=False)
    with pytest.raises(machine.MachineError, match="--yes"):
        cli.run(["recordings", "purge"])
    assert len(ledger.list_sessions()) == 1
    ledger.reset_registry()                                  # the recording process is finished
    assert "deleted 1" in cli.run(["recordings", "purge", "--yes"])
    assert ledger.list_sessions() == []


def test_recordings_are_not_reachable_from_the_slash_command():
    assert "recordings" not in cli._SLASH_SAFE


# --- prompt section, skill name, hook -------------------------------------------------------------
def test_prompt_states_the_exact_skill_name_and_the_key_rules_and_stays_short():
    p = companion.PROMPT
    assert "barehand:barehand" in p and len(p) < 2400
    for needle in ("Never type blind", "still screen is not 'done'", "enter_setup=true", "eject_media"):
        assert needle in p


def test_exactly_one_pre_tool_call_hook_is_registered_and_it_never_approves():
    hooks, tools = [], []

    class Ctx:
        def register_tool(self, **kw): tools.append(kw["name"])
        def register_hook(self, name, fn): hooks.append((name, fn))
        def register_cli_command(self, **kw): pass

    companion.register(Ctx())
    assert [n for n, _ in hooks] == ["pre_tool_call"] and hooks[0][1] is companion._approval_hook
    assert not hasattr(companion, "_skill_alias_hook")
    out = companion._approval_hook("barehand_machine", {"action": "reset"})
    assert out["action"] == "approve" and out["rule_key"] == "barehand:reset"   # escalates to the human gate
    assert companion._approval_hook("skill_view", {"name": "barehand"}) is None
