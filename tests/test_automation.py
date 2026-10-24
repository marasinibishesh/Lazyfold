"""Automation: settings, trusted auto-runs, watching, Task Scheduler, registry.

Nothing here shows a real toast, creates a real task or touches real
registry keys outside HKCU\\Software\\LazyfoldTest.
"""

import json
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import pytest

from conftest import snapshot, write_old
from lazyfold.demo import FAKE_EXE
from lazyfold import automation, executor, service, settings
from lazyfold.rules import DOC_CATEGORIES

NO_MODEL = {"use_model": False, "notify_min_files": 5}


# --------------------------------------------------------------------------
# settings


def test_settings_defaults_when_missing():
    s = settings.load_settings()
    assert s == settings.DEFAULTS
    assert not set(s["auto_approve_categories"]) & DOC_CATEGORIES


def test_settings_round_trip_and_unknown_keys(home):
    stored = settings.save_settings({"auto_min_confidence": 0.8, "ocr": False, "bogus": 1})
    assert stored["auto_min_confidence"] == 0.8 and stored["ocr"] is False and "bogus" not in stored
    on_disk = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    assert "bogus" not in on_disk
    again = settings.load_settings()
    assert again == stored and again["notify_min_files"] == 5  # defaults filled


@pytest.mark.parametrize("bad", [
    {"auto_approve_categories": ["installers", "nonsense"]},
    {"auto_approve_categories": "installers"},
    {"auto_min_confidence": 1.5},
    {"auto_min_confidence": True},
    {"auto_min_accept_prob": 1.5},
    {"auto_min_accept_prob": 0.3},
    {"auto_min_accept_prob": True},
    {"notify_min_files": -1},
    {"notify_min_files": 2.5},
    {"photos_by_date": "yes"},
    {"ollama_path": 5},
])
def test_settings_save_rejects_bad_values(bad):
    with pytest.raises(ValueError):
        settings.save_settings(bad)
    assert settings.load_settings() == settings.DEFAULTS


def test_settings_load_tolerates_hand_edits(home):
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(json.dumps({"auto_min_confidence": "high", "ocr": False,
                                                    "auto_approve_categories": ["x"]}), encoding="utf-8")
    s = settings.load_settings()
    assert s["auto_min_confidence"] == 0.9 and s["ocr"] is False
    assert s["auto_approve_categories"] == settings.DEFAULTS["auto_approve_categories"]
    (home / "settings.json").write_text("{not json", encoding="utf-8")
    assert settings.load_settings() == settings.DEFAULTS


def test_documents_only_when_explicit():
    s = settings.save_settings({"auto_approve_categories": ["installers", "finance"]})
    assert settings.auto_approves_documents(s)
    assert not settings.auto_approves_documents(settings.DEFAULTS)


# --------------------------------------------------------------------------
# notify


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only behaviour (schtasks, toast, .exe names, creation flags)")
def test_notify_passes_text_only_through_env(monkeypatch):
    seen = {}

    def fake_run(args, **kw):
        seen["args"], seen["env"] = args, kw["env"]
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(automation.subprocess, "run", fake_run)
    evil = "<b>$(Remove-Item C:\\)</b>' ; \"x\u202e"
    assert automation.notify("Title & co", evil) is (os.name == "nt")
    if os.name != "nt":
        return
    assert all(evil not in a and "Remove-Item" not in a for a in seen["args"])
    xml = seen["env"]["LAZYFOLD_TOAST_XML"]
    assert "&lt;b&gt;$(Remove-Item C:\\)&lt;/b&gt;" in xml and "Title &amp; co" in xml
    assert "\u202e" not in xml
    assert automation.TOAST_APP_ID in seen["args"][-1]


def test_notify_never_raises(monkeypatch):
    def boom(*a, **k):
        raise OSError("no powershell")

    monkeypatch.setattr(automation.subprocess, "run", boom)
    assert automation.notify("a", "b") is False
    monkeypatch.setattr(automation.subprocess, "run",
                        lambda args, **k: subprocess.CompletedProcess(args, 1))
    assert automation.notify("a", "b") is False


# --------------------------------------------------------------------------
# auto_run


class Notes:
    def __init__(self):
        self.calls = []

    def __call__(self, title, body):
        self.calls.append((title, body))
        return True


def test_auto_run_moves_trusted_only_and_undoes(demo):
    root, _ = demo
    before = snapshot(root)
    plan, _ = service.make_plan(root)
    expect = {p.rel_path for p in plan.proposals if automation.trusted(p, settings.validate(NO_MODEL))}
    assert expect and not any(p.category in DOC_CATEGORIES for p in plan.proposals if p.rel_path in expect)

    notes = Notes()
    out = automation.auto_run(root, settings=NO_MODEL, notify_fn=notes)
    assert "error" not in out and out["run_id"]
    assert out["moved"] == len(expect)
    for rel in expect:
        assert not (root / rel).exists()
    # Documents are still where they were, waiting.
    docs = [p for p in plan.proposals if p.action == "move" and p.category in DOC_CATEGORIES]
    assert docs and all((root / p.rel_path).exists() for p in docs)

    waiting_plan = service.load_plan(out["plan_id"])
    remaining = [p for p in waiting_plan.proposals if p.action == "move"]
    assert out["waiting"] == len(remaining) >= len(docs)
    assert not {p.rel_path for p in waiting_plan.proposals} & expect
    assert not any(p.approved for p in waiting_plan.proposals)
    assert automation.pending() == {**automation.pending(), "plan_id": out["plan_id"], "folder": str(root),
                                    "waiting": out["waiting"]}

    assert out["notified"] and len(notes.calls) == 1
    body = notes.calls[0][1]
    assert f"tidied {out['moved']} files in Downloads" in body and f"{out['waiting']} files are waiting" in body

    executor.undo_run(out["run_id"])
    assert snapshot(root) == before

    automation.clear_pending("someone-else")
    assert automation.pending() is not None
    automation.clear_pending(out["plan_id"])
    assert automation.pending() is None


def _auto_rows():
    from lazyfold import reviewlog
    path = reviewlog.log_path()
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    return [json.loads(l) for l in lines if l.strip()]


def test_auto_run_logs_capped_rows(demo):
    from lazyfold import reviewlog
    root, _ = demo
    for i in range(3):  # with the demo's two, one group holds more installers than the cap
        write_old(root / f"setup-tool-{i}.exe", FAKE_EXE + b"tool" + bytes([i]))
    out = automation.auto_run(root, settings=NO_MODEL, notify_fn=Notes())
    assert out["run_id"] and out["moved"]
    after = snapshot(root)
    rows = [r for r in _auto_rows() if r.get("source") == "auto"]
    assert rows and all(r["run_id"] == out["run_id"] and r["label"] == 1 for r in rows)
    groups = {}
    for r in rows:
        g = (r["features"]["ext"], r["features"]["category"], r["features"]["source_host"])
        groups[g] = groups.get(g, 0) + 1
    assert max(groups.values()) <= reviewlog.AUTO_PER_GROUP and groups[(".exe", "installers", "")] == 3
    assert len(rows) <= out["moved"]
    assert snapshot(root) == after  # logging touched no user file
    # Predictor turned off: nothing is collected.
    settings.save_settings({"use_predictor": False})
    for i in range(2):
        write_old(root / f"later-{i}.exe", FAKE_EXE + b"later" + bytes([i]))
    n = len(_auto_rows())
    out2 = automation.auto_run(root, settings={**NO_MODEL, "use_predictor": False}, notify_fn=Notes())
    assert out2["moved"] == 2 and len(_auto_rows()) == n
    service.undo_run(out2["run_id"])
    assert len(_auto_rows()) == n  # and no undo marker either


def test_undo_relabels_auto_rows(demo):
    from lazyfold import reviewlog
    root, _ = demo
    before = snapshot(root)
    out = automation.auto_run(root, settings=NO_MODEL, notify_fn=Notes())
    rows = [r for r in _auto_rows() if r.get("source") == "auto"]
    assert rows
    later = rows[0]["ts"] + 4 * 86400
    assert reviewlog.load_rows() == []  # too recent to count yet
    assert [r["label"] for r in reviewlog.load_rows(now=later)] == [1] * len(rows)
    service.undo_run(out["run_id"])
    assert snapshot(root) == before
    assert [r["label"] for r in reviewlog.load_rows(now=later)] == [0] * len(rows)
    service.undo_run(out["run_id"])  # nothing left to undo: no second marker
    assert sum(1 for r in _auto_rows() if "undo_run" in r) == 1


def test_auto_review_list_untouched_rows_not_logged(demo):
    from lazyfold import reviewlog
    root, _ = demo
    out = automation.auto_run(root, settings=NO_MODEL, notify_fn=Notes())
    waiting = [p for p in service.load_plan(out["plan_id"]).proposals
               if p.action == "move" and not p.duplicate_of and not p.near_duplicate]
    assert len(waiting) >= 2
    pick = waiting[0].rel_path
    n = len(_auto_rows())
    service.execute(out["plan_id"], approved={pick})
    review = [r for r in _auto_rows()[n:] if r.get("source") == "review"]
    assert len(review) == 1 and review[0]["label"] == 1 and review[0]["outcome"] == waiting[0].category


def test_logging_failure_does_not_break_run_or_undo(demo, monkeypatch):
    from lazyfold import reviewlog
    root, _ = demo
    before = snapshot(root)

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(reviewlog, "record_auto", boom)
    monkeypatch.setattr(reviewlog, "mark_undone", boom)
    out = automation.auto_run(root, settings=NO_MODEL, notify_fn=Notes())
    assert "error" not in out and out["run_id"] and out["moved"] > 0
    assert service.load_plan(out["plan_id"]).scan_stats["auto"]["run_id"] == out["run_id"]
    rep = service.undo_run(out["run_id"])
    assert rep.count("restored") == out["moved"]
    assert snapshot(root) == before


def test_auto_run_plan_only(demo):
    root, _ = demo
    before = snapshot(root)
    notes = Notes()
    out = automation.auto_run(root, apply_trusted=False, settings=NO_MODEL, notify_fn=notes)
    assert snapshot(root) == before
    assert out["moved"] == 0 and out["run_id"] is None and out["waiting"] > 0
    assert "ready for your review" in notes.calls[0][1]


def test_model_decisions_and_low_confidence_wait(tmp_path, monkeypatch):
    root = tmp_path / "dl"
    write_old(root / "a.mp3", b"ID3" + b"\x00" * 100)
    write_old(root / "b.mp3", b"ID3" + b"\x01" * 100)
    real = service.make_plan

    def fake_make_plan(folder, **kw):
        plan, st = real(folder, **kw)
        for p in plan.proposals:
            if p.rel_path == "a.mp3":
                p.engine = "rules+model"
        return plan, st

    monkeypatch.setattr(automation.service, "make_plan", fake_make_plan)
    out = automation.auto_run(root, settings={**NO_MODEL, "notify_min_files": 99}, notify_fn=Notes())
    assert out["moved"] == 1 and (root / "a.mp3").exists() and not (root / "b.mp3").exists()
    assert out["notified"] is False  # below notify_min_files

    out = automation.auto_run(root, settings={**NO_MODEL, "auto_min_confidence": 1.0}, notify_fn=Notes())
    assert out["moved"] == 0 and (root / "a.mp3").exists()


def test_notify_threshold_and_model_flag(demo, monkeypatch):
    root, _ = demo
    seen = {}
    real = service.make_plan

    def spy(folder, **kw):
        seen["model"] = kw.get("model")
        return real(folder, model=None)

    monkeypatch.setattr(automation.service, "make_plan", spy)
    notes = Notes()
    out = automation.auto_run(root, apply_trusted=False, settings={"use_model": True, "notify_min_files": 1000},
                              notify_fn=notes)
    from lazyfold.llm import DEFAULT_MODEL
    assert seen["model"] == DEFAULT_MODEL and not notes.calls and out["notified"] is False


def test_auto_run_needs_recovery(demo):
    root, _ = demo
    plan, _ = service.make_plan(root)
    service.apply_edits(plan, approved={p.rel_path for p in plan.proposals if p.action == "move"})
    with pytest.raises(KeyboardInterrupt):
        executor.apply_plan(plan, interrupt_after=2)
    before = snapshot(root)
    notes = Notes()
    out = automation.auto_run(root, settings=NO_MODEL, notify_fn=notes)
    assert "error" in out and out["moved"] == 0 and out["run_id"] is None
    assert snapshot(root) == before
    assert notes.calls and "interrupted" in notes.calls[0][1]


def test_auto_run_refuses_state_folder(home):
    notes = Notes()
    out = automation.auto_run(home, settings=NO_MODEL, notify_fn=notes)
    assert "error" in out and out["plan_id"] is None


def test_auto_run_lock(demo, home):
    root, _ = demo
    home.mkdir(parents=True, exist_ok=True)
    (home / "auto.lock").write_text("123")
    before = snapshot(root)
    out = automation.auto_run(root, settings=NO_MODEL, notify_fn=Notes())
    assert "in progress" in out["error"] and snapshot(root) == before


def _move(cat="installers", conf=0.95, engine="rules", prob=None, action="move"):
    from lazyfold.models import Proposal
    return Proposal(rel_path="a.exe", action=action, category=cat, new_name=None, dest_rel=f"{cat}/a.exe",
                    confidence=conf, reasons=[], engine=engine, fingerprint={"size": 1}, accept_prob=prob)


def test_accept_prob_threshold_is_its_own_setting():
    s = settings.defaults()
    assert s["auto_min_accept_prob"] == 0.85
    assert not automation.trusted(_move(prob=0.80), s)
    assert automation.trusted(_move(prob=0.90), s)
    assert automation.trusted(_move(prob=0.80), {**s, "auto_min_accept_prob": 0.75})
    # The rule threshold no longer gates the predictor, and vice versa.
    assert automation.trusted(_move(prob=0.86), {**s, "auto_min_confidence": 0.95})
    assert not automation.trusted(_move(conf=0.9, prob=0.99), {**s, "auto_min_confidence": 0.95})


@pytest.mark.parametrize("p", [
    dict(), dict(conf=0.5), dict(engine="rules+model"), dict(engine="rules+learned"),
    dict(cat="finance"), dict(action="keep"),
])
def test_no_prediction_means_rules_alone_and_predictor_never_adds(p):
    s = settings.defaults()
    alone = automation.trusted(_move(**p), s)
    assert automation.trusted(_move(prob=None, **p), s) is alone
    assert automation.trusted(_move(prob=0.99, **p), s) is alone  # a high score adds nothing
    assert automation.trusted(_move(prob=0.99, **p), {**s, "auto_min_accept_prob": 0.5}) is alone
    assert not automation._held_by_predictor(_move(prob=None, **p), s)


def test_model_move_never_trusted_at_high_prob():
    s = settings.defaults()
    assert not automation.trusted(_move(engine="rules+model", prob=0.99), s)
    assert not automation._held_by_predictor(_move(engine="rules+model", prob=0.1), s)


def test_held_by_predictor_counted_and_notified(tmp_path, monkeypatch):
    root = tmp_path / "dl"
    for name in ("a.mp3", "b.mp3", "c.mp3", "d.mp3"):
        write_old(root / name, b"ID3" + name.encode() + b"\x00" * 100)
    probs = {"a.mp3": 0.4, "b.mp3": 0.6, "c.mp3": 0.97, "d.mp3": 0.1}
    real = service.make_plan

    def fake_make_plan(folder, **kw):
        plan, st = real(folder, **kw)
        for p in plan.proposals:
            p.accept_prob = probs[p.rel_path]
            if p.rel_path == "d.mp3":
                p.engine = "rules+model"  # waits anyway; not counted as held by the predictor
        return plan, st

    monkeypatch.setattr(automation.service, "make_plan", fake_make_plan)
    notes = Notes()
    out = automation.auto_run(root, settings={**NO_MODEL, "notify_min_files": 1}, notify_fn=notes)
    assert out["moved"] == 1 and not (root / "c.mp3").exists()
    assert all((root / n).exists() for n in ("a.mp3", "b.mp3", "d.mp3"))
    assert out["held_by_predictor"] == 2 and out["waiting"] == 3
    saved = service.load_plan(out["plan_id"])
    assert saved.scan_stats["auto"]["held_by_predictor"] == 2
    assert not any(p.approved for p in saved.proposals)
    assert "(2 held back because you usually change files like these)" in notes.calls[-1][1]
    assert "3 files are waiting for your review" in notes.calls[-1][1]


def test_notification_quiet_about_predictor_when_nothing_held():
    _, body = automation._message(Path("Downloads"), 12, 3)
    assert "held back" not in body
    _, body = automation._message(Path("Downloads"), 0, 3, held=3)
    assert "(3 held back because you usually change files like these)" in body
    _, body = automation._message(Path("Downloads"), 12, 3, held=2)
    assert body.startswith("Lazyfold tidied 12 files in Downloads. 3 files are waiting for your review (2 held")


# --------------------------------------------------------------------------
# watch


def test_watch_runs_once_after_settle(tmp_path):
    root = tmp_path / "w"
    root.mkdir()
    (root / "old.txt").write_text("already here")
    stop = threading.Event()
    calls = []

    def run_fn(folder):
        calls.append(folder)
        stop.set()

    t = threading.Thread(target=automation.watch, args=(root,),
                         kwargs=dict(interval=0.02, settle_seconds=0.3, stop_event=stop, run_fn=run_fn))
    t.start()
    try:
        for i in range(5):  # a "download" still growing: no run yet
            with open(root / "new.bin", "ab") as f:
                f.write(b"x" * 1000)
            threading.Event().wait(0.08)
        assert calls == []
        t.join(timeout=5)
    finally:
        stop.set()
        t.join(timeout=5)
    assert calls == [root]


def test_watch_ignores_unchanged_and_survives_errors(tmp_path):
    root = tmp_path / "w"
    root.mkdir()
    stop = threading.Event()
    calls = []

    def run_fn(folder):
        calls.append(1)
        raise RuntimeError("boom")

    t = threading.Thread(target=automation.watch, args=(root,),
                         kwargs=dict(interval=0.01, settle_seconds=0.05, stop_event=stop, run_fn=run_fn))
    t.start()
    threading.Event().wait(0.3)
    assert calls == []  # nothing changed
    (root / "a.txt").write_text("a")
    threading.Event().wait(0.4)
    (root / "b.txt").write_text("b")
    threading.Event().wait(0.4)
    stop.set()
    t.join(timeout=5)
    assert not t.is_alive() and len(calls) == 2


# --------------------------------------------------------------------------
# Task Scheduler


@pytest.fixture
def fake_schtasks(monkeypatch):
    calls = []

    def fake_run(args, **kw):
        assert "shell" not in kw
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="SUCCESS", stderr="")

    monkeypatch.setattr(automation.subprocess, "run", fake_run)
    return calls


def _exe():
    return automation._system32("schtasks.exe")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only behaviour (schtasks, toast, .exe names, creation flags)")
def test_schedule_daily_args(tmp_path, fake_schtasks):
    folder = tmp_path / "Downloads"
    folder.mkdir()
    name = automation.schedule_daily(folder, "07:30")
    canon = str(executor.canonical(folder))
    assert name == automation.tidy_task_name(folder) and name.startswith("Lazyfold\\Tidy ")
    assert len(name.split(" ", 1)[1]) == 8
    py = automation.gui_python()
    assert fake_schtasks == [[_exe(), "/Create", "/TN", name, "/TR", f'"{py}" -m lazyfold auto "{canon}"',
                              "/SC", "DAILY", "/ST", "07:30", "/RL", "LIMITED", "/F"]]
    assert automation.tidy_task_name(str(folder).upper()) == name or os.name != "nt"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only behaviour (schtasks, toast, .exe names, creation flags)")
def test_schedule_weekly_args(fake_schtasks):
    name = automation.schedule_weekly_summary("mon", "18:05")
    py = automation.gui_python()
    assert fake_schtasks == [[_exe(), "/Create", "/TN", name, "/TR", f'"{py}" -m lazyfold summary --notify',
                              "/SC", "WEEKLY", "/D", "MON", "/ST", "18:05", "/RL", "LIMITED", "/F"]]


@pytest.mark.parametrize("at", ["7:30", "24:00", "12:60", "noon", "12:00 && calc"])
def test_schedule_rejects_bad_time(tmp_path, fake_schtasks, at):
    with pytest.raises(ValueError):
        automation.schedule_daily(tmp_path, at)
    with pytest.raises(ValueError):
        automation.schedule_weekly_summary("SUN", at)
    assert fake_schtasks == []


def test_schedule_rejects_bad_day_and_long_command(tmp_path, fake_schtasks):
    with pytest.raises(ValueError):
        automation.schedule_weekly_summary("FUNDAY")
    if sys.platform != "win32":
        return  # the 261-character limit is Task Scheduler's (/TR); launchd has none
    deep = tmp_path / ("x" * 120) / ("y" * 120)
    deep.mkdir(parents=True)
    with pytest.raises(ValueError, match="at most 261"):
        automation.schedule_daily(deep)
    assert fake_schtasks == []


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only behaviour (schtasks, toast, .exe names, creation flags)")
def test_schedule_failure_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(automation.subprocess, "run",
                        lambda args, **k: subprocess.CompletedProcess(args, 1, stdout="", stderr="ERROR: denied"))
    with pytest.raises(RuntimeError, match="denied"):
        automation.schedule_daily(tmp_path)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only behaviour (schtasks, toast, .exe names, creation flags)")
def test_remove_task_only_ours(fake_schtasks):
    automation.remove_task("\\Lazyfold\\Tidy 1234abcd")
    assert fake_schtasks == [[_exe(), "/Delete", "/TN", "Lazyfold\\Tidy 1234abcd", "/F"]]
    for bad in ["Microsoft\\Windows\\Defrag", "\\Other\\Lazyfold", "Lazyfold\\", "Lazyfold\\a\\b", "Tidy"]:
        with pytest.raises(ValueError):
            automation.remove_task(bad)
    assert len(fake_schtasks) == 1


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only behaviour (schtasks, toast, .exe names, creation flags)")
def test_list_tasks_parses_and_filters(monkeypatch):
    csv_out = ('"\\Lazyfold\\Tidy 1234abcd","10/23/2026 8:00:00 PM","Ready"\r\n'
               '"\\Lazyfold\\Tidy 1234abcd","10/24/2026 8:00:00 PM","Ready"\r\n'
               '"\\Other\\Thing","N/A","Ready"\r\n')
    xml = ('<?xml version="1.0" encoding="UTF-16"?><Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
           '<Actions><Exec><Command>C:\\py\\pythonw.exe</Command><Arguments>-m lazyfold auto "C:\\D"</Arguments>'
           '</Exec></Actions></Task>')
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        out = xml if "/XML" in args else csv_out
        return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")

    monkeypatch.setattr(automation.subprocess, "run", fake_run)
    tasks = automation.list_tasks()
    assert tasks == [{"name": "Lazyfold\\Tidy 1234abcd", "next_run": "10/23/2026 8:00:00 PM", "status": "Ready",
                      "command": '"C:\\py\\pythonw.exe" -m lazyfold auto "C:\\D"'}]
    assert calls[0] == [_exe(), "/Query", "/TN", "\\Lazyfold\\", "/FO", "CSV", "/NH"]
    monkeypatch.setattr(automation.subprocess, "run",
                        lambda args, **k: subprocess.CompletedProcess(args, 1, stdout="", stderr="not found"))
    assert automation.list_tasks() == []


def test_quote_trailing_backslash():
    assert automation._quote_arg("C:\\") == '"C:\\\\"'
    with pytest.raises(ValueError):
        automation._quote_arg('C:\\a"b')


# --------------------------------------------------------------------------
# Registry (test key only)


win_only = pytest.mark.skipif(sys.platform != "win32", reason="registry is Windows-only")


@pytest.fixture
def test_base():
    import winreg
    top = f"Software\\LazyfoldTest\\{uuid.uuid4().hex[:8]}"
    yield top + "\\Classes"

    def wipe(path):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
                subs = []
                i = 0
                while True:
                    try:
                        subs.append(winreg.EnumKey(k, i))
                        i += 1
                    except OSError:
                        break
        except FileNotFoundError:
            return
        for s in subs:
            wipe(f"{path}\\{s}")
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)

    wipe(top)
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, "Software\\LazyfoldTest")  # only if now empty
    except OSError:
        pass


def _read(path, name=""):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
        return winreg.QueryValueEx(k, name)[0]


def _exists(path):
    import winreg
    try:
        winreg.OpenKey(winreg.HKEY_CURRENT_USER, path).Close()
        return True
    except FileNotFoundError:
        return False


@win_only
def test_context_menu_install_uninstall(test_base):
    import winreg
    assert not automation.context_menu_installed(base=test_base)
    automation.install_context_menu(sys.executable, base=test_base)
    assert automation.context_menu_installed(base=test_base)
    py = automation.gui_python(sys.executable)
    for key in ("Directory\\shell\\Lazyfold", "Directory\\Background\\shell\\Lazyfold"):
        assert _read(f"{test_base}\\{key}") == "Tidy with Lazyfold"
        assert _read(f"{test_base}\\{key}", "Icon") == py
        assert _read(f"{test_base}\\{key}\\command") == f'"{py}" -m lazyfold ui "%V"'
    # A sibling entry that is not ours must survive uninstall.
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, f"{test_base}\\Directory\\shell\\Other\\command") as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, "notepad.exe")
    automation.uninstall_context_menu(base=test_base)
    assert not automation.context_menu_installed(base=test_base)
    assert not _exists(f"{test_base}\\Directory\\shell\\Lazyfold")
    assert not _exists(f"{test_base}\\Directory\\Background\\shell\\Lazyfold")
    assert _exists(f"{test_base}\\Directory\\shell\\Other\\command")
    automation.uninstall_context_menu(base=test_base)  # idempotent


@win_only
def test_run_alias_install_uninstall(test_base, home):
    base = test_base + "\\AppPaths"
    launcher = automation.install_run_alias(sys.executable, base=base)
    assert automation.run_alias_installed(base=base)
    assert Path(_read(f"{base}\\lazyfold.exe")) == launcher and launcher.parent.parent == home
    assert launcher.read_bytes() == automation.launcher_script(automation.gui_python(sys.executable))
    automation.uninstall_run_alias(base=base)
    assert not automation.run_alias_installed(base=base) and not _exists(f"{base}\\lazyfold.exe")


# --------------------------------------------------------------------------
# Regressions


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only behaviour")
def test_schedule_refuses_percent_paths(tmp_path, fake_schtasks):
    # Task Scheduler expands %VAR% inside /TR, so such paths must be refused.
    folder = tmp_path / "a%USERNAME%b"
    folder.mkdir()
    with pytest.raises(ValueError, match="paths containing % cannot be scheduled"):
        automation.schedule_daily(folder)
    py_pct = tmp_path / "py%PATH%" / "python.exe"
    with pytest.raises(ValueError, match="paths containing % cannot be scheduled"):
        automation.schedule_weekly_summary(python_exe=str(py_pct))
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(ValueError, match="paths containing % cannot be scheduled"):
        automation.schedule_daily(plain, python_exe=str(py_pct))
    with pytest.raises(ValueError, match="cannot be scheduled"):
        automation._tr('auto "C:\\x%TEMP%"')
    assert fake_schtasks == []


def test_launcher_script_utf8_and_refusals():
    py = "C:\\Users\\\u65e5\u672c caf\u00e9 &(x)\\pythonw.exe"
    data = automation.launcher_script(py)
    assert data == ('@echo off\r\nchcp 65001>nul\r\nstart "" "' + py + '" -m lazyfold ui\r\n').encode("utf-8")
    assert not data.startswith(b"\xef\xbb\xbf") and b"\r\r" not in data
    for bad in ("C:\\a%b\\pythonw.exe", 'C:\\a"b\\pythonw.exe', "C:\\a^b\\pythonw.exe", "C:\\a!b\\pythonw.exe"):
        with pytest.raises(ValueError):
            automation.launcher_script(bad)


@win_only
def test_run_alias_non_ascii_written_as_utf8(tmp_path, test_base, home):
    d = tmp_path / "\u65e5\u672c \u0141\u00f3d\u017a"
    d.mkdir()
    fake = d / "python.exe"
    fake.write_bytes(b"")
    base = test_base + "\\AppPaths"
    launcher = automation.install_run_alias(str(fake), base=base)  # used to crash (mbcs)
    data = launcher.read_bytes()
    assert str(fake).encode("utf-8") in data and b"\r\r" not in data
    with pytest.raises(ValueError):
        automation.install_run_alias(str(tmp_path / "a%b" / "python.exe"), base=base)
    automation.uninstall_run_alias(base=base)


@win_only
def test_launcher_header_resolves_non_ascii_in_fresh_console(tmp_path):
    # Run the generated header for real, with the start line swapped for a
    # harmless existence check, in a new console (default OEM code page).
    d = tmp_path / "\u65e5\u672c caf\u00e9 \u0141\u00f3d\u017a &(x)"
    d.mkdir()
    fake = d / "pythonw.exe"
    fake.write_bytes(b"")
    out = d / "out.txt"
    text = automation.launcher_script(str(fake)).decode("utf-8")
    start_line = f'start "" "{fake}" -m lazyfold ui'
    assert start_line in text
    probe = d / "probe.cmd"
    probe.write_bytes(text.replace(
        start_line, f'if exist "{fake}" (echo FOUND> "{out}") else (echo MISSING> "{out}")').encode("utf-8"))
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    cmd = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe")
    subprocess.run(f'"{cmd}" /d /s /c ""{probe}""', timeout=60, startupinfo=si,
                   creationflags=subprocess.CREATE_NEW_CONSOLE, stdin=subprocess.DEVNULL)
    assert out.read_text().strip() == "FOUND"


def test_auto_run_clears_stale_pending_same_folder(demo):
    root, _ = demo
    r1 = automation.auto_run(root, settings=NO_MODEL, notify_fn=Notes())
    assert r1["waiting"] and automation.pending()["plan_id"] == r1["plan_id"]
    # The user files the waiting documents by hand (temp demo folder only).
    for p in service.load_plan(r1["plan_id"]).proposals:
        if p.action == "move" and (root / p.rel_path).exists():
            (root / p.rel_path).unlink()
    r2 = automation.auto_run(root, settings=NO_MODEL, notify_fn=Notes())
    assert r2["waiting"] == 0
    assert automation.pending() is None


def test_auto_run_keeps_pending_for_other_folder(tmp_path, home):
    root = tmp_path / "empty"
    root.mkdir()
    home.mkdir(parents=True, exist_ok=True)
    other = {"plan_id": "other-plan", "folder": str(tmp_path / "elsewhere"), "waiting": 3, "created": "x"}
    (home / "pending.json").write_text(json.dumps(other), encoding="utf-8")
    out = automation.auto_run(root, settings=NO_MODEL, notify_fn=Notes())
    assert out["waiting"] == 0
    assert automation.pending()["plan_id"] == "other-plan"


@win_only
def test_context_menu_uninstall_keeps_key_with_extra_subkeys(test_base):
    import winreg
    automation.install_context_menu(sys.executable, base=test_base)
    shell = f"{test_base}\\Directory\\shell\\Lazyfold"
    winreg.CreateKey(winreg.HKEY_CURRENT_USER, shell + "\\extra").Close()
    kept = automation.uninstall_context_menu(base=test_base)  # used to raise PermissionError
    assert kept == [shell]
    assert _exists(shell + "\\extra") and not _exists(shell + "\\command")
    assert not _exists(f"{test_base}\\Directory\\Background\\shell\\Lazyfold")
    assert not automation.context_menu_installed(base=test_base)
    assert automation.uninstall_context_menu(base=test_base) == []  # no longer ours: untouched
