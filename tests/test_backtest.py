"""'How well it knows you': the TabPFN backtest on the user's own review history."""

import importlib.util
import json
import random

import pytest

from slothfile import cli, predict, reviewlog

HAS_TABPFN = importlib.util.find_spec("tabpfn") is not None and importlib.util.find_spec("torch") is not None


def write_history(n_plans: int, per_plan: int = 5, seed: int = 0) -> list[dict]:
    """Made-up review rows (no user data), several per plan, oldest first."""
    rows = predict._synthetic_rows(n_plans * per_plan, random.Random(seed))
    lines = []
    for i, r in enumerate(rows):
        lines.append(json.dumps({"v": 2, "source": "review", "plan_id": f"plan-{i // per_plan:04d}",
                                 "features": r["features"], "label": r["label"]}))
    reviewlog.log_path().write_text("\n".join(lines) + "\n", encoding="utf-8")
    return reviewlog.load_rows()


@pytest.fixture
def installed(monkeypatch):
    monkeypatch.setattr(predict, "available", lambda: (True, "TabPFN is installed"))


def test_split_is_time_ordered_and_never_splits_a_plan():
    rows = write_history(30)
    train, test = predict._split_by_plan(rows)
    train_ids, test_ids = {r["plan_id"] for r in train}, {r["plan_id"] for r in test}
    assert not train_ids & test_ids
    assert max(train_ids) < min(test_ids)  # newest plans are the test
    assert len(test) == 30  # 20% of 30 plans, 5 rows each


def test_too_little_history_says_how_much_more(installed):
    write_history(4)  # 20 rows
    res = predict.backtest()
    assert res["ok"] is False and "more review" in res["why"]


def test_not_installed():
    res = predict.backtest() if not HAS_TABPFN else None
    if res is not None:
        assert res["ok"] is False and res["available"] is False


def test_metrics_arithmetic(installed, monkeypatch):
    rows = write_history(30)
    _, test = predict._split_by_plan(rows)
    labels = [r["label"] for r in test]
    # Confident (0.95) exactly on accepted rows except the first one; 0.1 elsewhere.
    probs = [0.95 if y == 1 else 0.1 for y in labels]
    first_pos = labels.index(1)
    probs[first_pos] = 0.6
    monkeypatch.setattr(predict, "fit_predict_rows", lambda train, test_f, **kw: list(probs))
    res = predict.backtest(threshold=0.85)
    assert res["ok"] is True
    assert res["n_test"] == len(test) and res["base_accepted"] == sum(labels)
    assert res["confident_n"] == sum(labels) - 1 and res["confident_accepted"] == sum(labels) - 1
    assert res["changes_n"] == labels.count(0) and res["changes_flagged"] == labels.count(0)
    assert res["auc"] == 1.0 and res["beats_baseline"] is True


def test_no_better_than_accepting_everything(installed, monkeypatch):
    write_history(30)
    monkeypatch.setattr(predict, "fit_predict_rows", lambda train, test_f, **kw: [0.9] * len(test_f))
    res = predict.backtest(threshold=0.85)
    assert res["ok"] is True and res["beats_baseline"] is False


def test_failed_fit_is_reported_not_raised(installed, monkeypatch):
    write_history(30)
    monkeypatch.setattr(predict, "fit_predict_rows", lambda *a, **kw: None)
    res = predict.backtest()
    assert res["ok"] is False and res["why"].startswith("The test could not run")


def test_cache_follows_the_review_log(installed, monkeypatch):
    write_history(30)
    monkeypatch.setattr(predict, "fit_predict_rows", lambda train, test_f, **kw: [0.5] * len(test_f))
    res = predict.backtest()
    assert predict.cached_backtest() == res
    assert predict.info()["backtest"] == res
    with open(reviewlog.log_path(), "a", encoding="utf-8") as f:
        f.write('{"undo_run": "r1"}\n')
    assert predict.cached_backtest() is None


def test_cache_follows_the_threshold(installed, monkeypatch):
    from slothfile import settings
    write_history(30)
    monkeypatch.setattr(predict, "fit_predict_rows", lambda train, test_f, **kw: [0.9] * len(test_f))
    predict.backtest()  # at the default 0.85
    assert predict.cached_backtest() is not None
    settings.save_settings({"auto_min_accept_prob": 0.95})
    assert predict.cached_backtest() is None  # its counts were for 0.85


def test_backtest_and_planning_never_wait_on_each_other(installed, monkeypatch):
    write_history(30)
    monkeypatch.setattr(predict, "LOCK_WAIT", 0.05)
    assert predict._lock.acquire()
    try:  # someone else is fitting
        res = predict.backtest()
        assert res["ok"] is False and "Try again" in res["why"]
        assert predict.predict_accept(plan_with_one_move()) is None
        assert "busy" in predict.status()
    finally:
        predict._lock.release()


def plan_with_one_move():
    from slothfile.models import Plan, Proposal
    p = Proposal(rel_path="a.exe", action="move", category="installers", new_name=None,
                 dest_rel="Installers/a.exe", confidence=0.95, reasons=[], engine="rules",
                 fingerprint={"size": 10, "mtime_ns": 1, "head_sha": "x"}, source_host=None, ext=".exe")
    return Plan(plan_id="px", root="C:/x", created="2026-10-20T10:00:00", engine="rules", proposals=[p])


def test_backtest_does_not_change_last_real_status(installed, monkeypatch):
    write_history(30)
    monkeypatch.setattr(predict, "_last_status", "predictor used 40 past reviews (1.0 s)")
    monkeypatch.setattr(predict, "fit_predict_rows", lambda train, test_f, **kw: [0.5] * len(test_f))
    predict.backtest()
    assert predict.status() == "predictor used 40 past reviews (1.0 s)"


def test_cli_backtest_reports_json(installed, monkeypatch, capsys):
    write_history(4)
    assert cli.main(["predictor", "backtest"]) == 6
    assert json.loads(capsys.readouterr().out)["ok"] is False


@pytest.mark.skipif(not HAS_TABPFN, reason="needs torch + tabpfn (Full build environment)")
def test_real_tabpfn_backtest():
    write_history(60)  # 300 made-up rows
    res = predict.backtest(time_limit=300)
    assert res["ok"] is True, res
    assert res["auc"] > 0.8 and res["n_train"] == 240
