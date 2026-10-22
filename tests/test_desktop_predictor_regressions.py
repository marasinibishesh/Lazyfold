"""Regression tests for the desktop/installer/predictor review findings (D0-D9)."""

import json
import os
import threading
import time
from pathlib import Path

import pytest

from slothfile import automation, desktop, predict, reviewlog, settings


# D1: the running app announces itself so the uninstaller can ask to close it
def test_app_mutex_is_held(monkeypatch):
    import ctypes
    monkeypatch.setattr(desktop, "_mutex", None)
    desktop.hold_app_mutex()
    ERROR_ALREADY_EXISTS = 183
    h = ctypes.windll.kernel32.CreateMutexW(None, False, desktop.APP_MUTEX)
    try:
        assert ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS
    finally:
        ctypes.windll.kernel32.CloseHandle(h)


def test_installer_declares_the_same_mutex():
    iss = (Path(__file__).resolve().parent.parent / "packaging" / "slothfile.iss").read_text(encoding="utf-8")
    assert f"AppMutex={desktop.APP_MUTEX}" in iss
    assert "SuppressibleMsgBox" in iss and "WizardSilent()" in iss  # D0/D2: silent installs never hang


# D3: checking/removing the Win+R alias must not create the data folder
def test_alias_checks_do_not_create_state(monkeypatch, tmp_path):
    home = tmp_path / "never-created"
    monkeypatch.setenv("SLOTHFILE_HOME", str(home))
    automation.uninstall_run_alias(base=r"Software\SlothFileTest\noalias")
    assert automation.run_alias_installed(base=r"Software\SlothFileTest\noalias") is False
    assert not home.exists()


# D5: a fit that is still running blocks new fits instead of piling them up
def test_no_parallel_fits_after_timeout(monkeypatch):
    gate = threading.Event()
    started = []

    def slow(train, test, key):
        started.append(1)
        gate.wait(5)
        return [0.5] * len(test)

    monkeypatch.setattr(predict, "_fit_predict", slow)
    monkeypatch.setattr(predict, "_busy", None)
    assert predict.fit_predict_rows([{}], [{}], time_limit=0.05) is None  # gives up, thread still running
    assert predict.fit_predict_rows([{}], [{}], time_limit=0.05) is None
    assert len(started) == 1 and "busy" in predict.status()
    gate.set()
    predict._busy.join(5)
    assert predict.fit_predict_rows([{}], [{}], time_limit=2) == [0.5]


# D6: turning the predictor off stops collecting history; reset clears it
def test_opt_out_stops_review_history(demo):
    from slothfile.planner import build_plan
    from slothfile.scanner import scan_folder
    from slothfile.service import execute, save_plan
    root, _ = demo
    settings.save_settings({"use_predictor": False})
    recs, _ = scan_folder(root)
    plan = build_plan(root, recs)
    save_plan(plan)
    execute(plan.plan_id, approved={p.rel_path for p in plan.proposals if p.action == "move"})
    assert not reviewlog.log_path().exists() or reviewlog.load_rows() == []


def test_learned_reset_also_clears_review_history(monkeypatch):
    from slothfile import cli
    reviewlog.log_path().write_text(json.dumps({"v": 1, "plan_id": "p", "label": 1, "features": {}}) + "\n",
                                    encoding="utf-8")
    cli.main(["learned", "--reset"])
    assert not reviewlog.log_path().exists() or reviewlog.log_path().read_text(encoding="utf-8").strip() == ""


# D7: a move the user renamed is not "accepted unchanged"
def test_renamed_move_is_labelled_changed():
    class P:
        rel_path = "document (3).pdf"
        category = "finance"
        new_name = "Receipt - Northwind.pdf"
    moved, ticked = {P.rel_path}, {P.rel_path}
    assert reviewlog._label(P, moved, ticked, {}, {}) == 1
    assert reviewlog._label(P, moved, ticked, {}, {P.rel_path: "Receipt - Northwind"}) == 1  # unchanged
    assert reviewlog._label(P, moved, ticked, {}, {P.rel_path: "My coffee receipt"}) == 0


# D8: one absurd number rejects that row only, never the whole history
def test_huge_numbers_in_review_log_are_skipped():
    good = {name: (1.0 if typ is float else 1 if typ is int else False if typ is bool else "x")
            for name, typ in reviewlog.FEATURES.items()}
    bad = dict(good)
    float_key = next(k for k, t in reviewlog.FEATURES.items() if t is float)
    rows = [{"v": 1, "plan_id": "a", "label": 1, "features": good},
            {"v": 1, "plan_id": "b", "label": 0, "features": dict(bad, **{float_key: 10 ** 400})}]
    with open(reviewlog.log_path(), "w", encoding="utf-8") as f:
        f.write(json.dumps(rows[0]) + "\n")
        f.write(json.dumps(rows[1]).replace('"' + float_key + '": ' + str(10 ** 400), '"' + float_key + '": ' + "9" * 401) + "\n")
    loaded = reviewlog.load_rows()
    assert len(loaded) == 1 and loaded[0]["plan_id"] == "a"
