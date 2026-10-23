"""Review log + TabPFN approval predictor."""

import importlib.util
import json
import random
from types import SimpleNamespace

import pytest

from slothfile import automation, predict, reviewlog, service, settings
from slothfile.models import Plan, Proposal

DAY_NS = 86400 * 10**9
NOW_NS = 1_788_000_000 * 10**9  # ~2026-08-29


def prop(rel, cat="installers", host=None, conf=0.95, engine="rules", size=100_000, age_days=3,
         **kw) -> Proposal:
    ext = "." + rel.rsplit(".", 1)[1] if "." in rel else ""
    return Proposal(rel_path=rel, action=kw.pop("action", "move"), category=cat, new_name=kw.pop("new_name", None),
                    dest_rel=f"{cat}/{rel}", confidence=conf, reasons=[], engine=engine,
                    fingerprint={"size": size, "mtime_ns": NOW_NS - age_days * DAY_NS, "head_sha": "x"},
                    source_host=host, ext=ext, **kw)


def plan_of(props, pid="p1", created="2026-09-18T10:30:00.000") -> Plan:
    return Plan(plan_id=pid, root="C:/x", created=created, engine="rules", proposals=props)


def report(moved) -> SimpleNamespace:
    return SimpleNamespace(results=[SimpleNamespace(src=m, status="moved") for m in moved])


def labels():
    return {r["features"]["ext"]: r["label"] for r in reviewlog.load_rows()}


# --------------------------------------------------------------------------- review log

def test_labels_cover_accept_untick_keep_and_folder_change():
    ps = [prop("a.exe"), prop("b.zip", "archives"), prop("c.pdf", "documents"), prop("d.png", "images"),
          prop("e.mp3", "audio"), prop("f.iso", "installers")]
    pl = plan_of(ps)
    approved = {"a.exe", "c.pdf", "d.png", "f.iso"}  # b.zip unticked
    overrides = {"c.pdf": "finance", "d.png": "keep"}
    # f.iso ticked but not moved (skipped): says nothing about the user.
    n = reviewlog.record(pl, report({"a.exe", "c.pdf"}), approved, overrides)
    assert n == 5
    assert labels() == {".exe": 1, ".zip": 0, ".pdf": 0, ".png": 0, ".mp3": 0}
    row = reviewlog.load_rows()[0]["features"]
    assert row["category"] == "installers" and row["engine"] == "rules" and row["hour"] == 10
    assert row["weekday"] == 4 and row["source_host_known"] is False and row["age_days"] > 0
    assert row["log2_size"] == pytest.approx(16.61, abs=0.01)


def test_duplicates_near_duplicates_and_non_moves_not_logged():
    ps = [prop("a.exe"), prop("dup.pdf", "duplicates", duplicate_of="x.pdf"),
          prop("near.png", "images", near_duplicate=True), prop("k.txt", action="keep")]
    approved = {"a.exe", "dup.pdf", "near.png"}
    assert reviewlog.record(plan_of(ps), report(approved), approved, {}) == 1
    assert [r["features"]["ext"] for r in reviewlog.load_rows()] == [".exe"]


def test_approved_none_uses_preticked_rows():
    ps = [prop("a.exe", approved=True), prop("b.zip", "archives")]
    reviewlog.record(plan_of(ps), report({"a.exe"}), None, {})
    assert labels() == {".exe": 1, ".zip": 0}


def test_each_plan_recorded_once():
    pl = plan_of([prop("a.exe")])
    assert reviewlog.record(pl, report({"a.exe"}), {"a.exe"}, {}) == 1
    assert reviewlog.record(pl, report({"a.exe"}), {"a.exe"}, {}) == 0
    assert len(reviewlog.load_rows()) == 1


def test_torn_and_garbage_lines_are_skipped():
    reviewlog.record(plan_of([prop("a.exe")], "p1"), report({"a.exe"}), {"a.exe"}, {})
    with open(reviewlog.log_path(), "ab") as f:
        f.write(b'not json\n\x00\x00\xff\n{"label": 1, "features": {"ext": 5}}\n[1,2]\n{"label": 1, "feat')
    reviewlog.record(plan_of([prop("b.zip", "archives")], "p2"), report({"b.zip"}), {"b.zip"}, {})
    assert labels() == {".exe": 1, ".zip": 1}


def test_log_capped(monkeypatch):
    monkeypatch.setattr(reviewlog, "MAX_ROWS", 7)
    for i in range(5):
        pl = plan_of([prop(f"f{i}a.exe"), prop(f"f{i}b.exe")], f"p{i}")
        reviewlog.record(pl, report({f"f{i}a.exe"}), {f"f{i}a.exe"}, {})
    rows = reviewlog.load_rows()
    assert len(rows) == 7 and rows[-1]["plan_id"] == "p4"
    assert len(reviewlog.log_path().read_bytes().splitlines()) == 7


# --------------------------------------------------------------------------- readiness

def _write_rows(n_pos, n_neg):
    with open(reviewlog.log_path(), "w", encoding="utf-8") as f:
        for i in range(n_pos + n_neg):
            feats = reviewlog.features(prop(f"x{i}.exe"), plan_of([]))
            f.write(json.dumps({"plan_id": "p", "features": feats, "label": int(i < n_pos)}) + "\n")


def test_history_readiness():
    st = predict.history_status()
    assert {k: st[k] for k in ("rows", "positives", "negatives", "ready")} == \
        {"rows": 0, "positives": 0, "negatives": 0, "ready": False}
    _write_rows(26, 4)
    assert predict.history_status()["ready"] is False  # too few negatives
    assert "Needs both" in predict.history_status()["status"]
    _write_rows(20, 9)
    assert predict.history_status()["ready"] is False  # too few rows
    assert "learning" in predict.history_status()["status"]
    _write_rows(25, 5)
    assert predict.history_status() == {"rows": 30, "positives": 25, "negatives": 5, "ready": True,
                                        "status": "ready"}


def test_predict_none_when_not_ready_or_unavailable(monkeypatch):
    pl = plan_of([prop("a.exe")])
    assert predict.predict_accept(pl) is None
    _write_rows(25, 5)
    monkeypatch.setattr(predict, "available", lambda: (False, "tabpfn is not installed"))
    assert predict.predict_accept(pl) is None
    assert "not installed" in predict.status()
    assert "PriorLabs-TabPFN" in predict.explain()


def test_predictor_failure_returns_none(monkeypatch):
    pytest.importorskip("numpy")  # part of the optional [predict] extra
    _write_rows(25, 5)
    monkeypatch.setattr(predict, "available", lambda: (True, "ok"))

    def boom():
        raise RuntimeError("no weights")
    monkeypatch.setattr(predict, "_make_classifier", boom)
    monkeypatch.setattr(predict, "_model_cache", None)
    assert predict.predict_accept(plan_of([prop("a.exe")])) is None
    assert "no weights" in predict.status()


def test_settings_use_predictor_validated():
    assert settings.DEFAULTS["use_predictor"] is True
    with pytest.raises(ValueError):
        settings.save_settings({"use_predictor": "yes"})
    assert settings.save_settings({"use_predictor": False})["use_predictor"] is False


# --------------------------------------------------------------------------- integration

def test_execute_writes_review_log(demo):
    root, _ = demo
    plan, _ = service.make_plan(root, model=None, ocr=False)
    moves = [p for p in plan.proposals if p.action == "move" and not p.duplicate_of and not p.near_duplicate]
    keys = [(p.category, p.ext) for p in moves]
    moves = [p for p in moves if keys.count((p.category, p.ext)) == 1]  # rows carry no file names
    assert len(moves) >= 3
    accept, untick, change = moves[0], moves[1], moves[2]
    other = "documents" if change.category != "documents" else "code"
    approved = {accept.rel_path, change.rel_path}
    service.execute(plan.plan_id, approved=approved, overrides={change.rel_path: other})
    assert reviewlog.load_rows() and all(r["plan_id"] == plan.plan_id for r in reviewlog.load_rows())
    rows = {(r["features"]["category"], r["features"]["ext"]): r["label"] for r in reviewlog.load_rows()}
    assert rows[(accept.category, accept.ext)] == 1
    assert rows[(untick.category, untick.ext)] == 0
    assert rows[(change.category, change.ext)] == 0
    # a resubmitted run of the same plan adds nothing
    n = len(reviewlog.load_rows())
    reviewlog.record(plan, report(set()), set(), {})
    assert len(reviewlog.load_rows()) == n


def test_review_log_failure_never_blocks_execute(demo, monkeypatch):
    root, _ = demo
    plan, _ = service.make_plan(root, model=None, ocr=False)

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(reviewlog, "record", boom)
    rep = service.execute(plan.plan_id)
    assert rep.count("moved") > 0


def test_make_plan_records_predictor_status(demo):
    root, _ = demo
    plan, _ = service.make_plan(root, model=None, ocr=False)
    assert "predictor" in plan.scan_stats
    assert all(p.accept_prob is None for p in plan.proposals)  # no history yet


def test_make_plan_attaches_probabilities(demo, monkeypatch):
    root, _ = demo
    monkeypatch.setattr(predict, "available", lambda: (True, "ok"))
    monkeypatch.setattr(predict, "predict_accept",
                        lambda plan: {p.rel_path: (0.93 if p.category == "installers" else 0.21)
                                      for p in plan.proposals if p.action == "move"})
    plan, _ = service.make_plan(root, model=None, ocr=False)
    inst = [p for p in plan.proposals if p.category == "installers" and p.action == "move"]
    docs = [p for p in plan.proposals if p.category == "documents" and p.action == "move"]
    assert inst[0].accept_prob == 0.93 and "you usually accept moves like this (93%)" in inst[0].reasons
    assert docs[0].accept_prob == 0.21 and any("check it" in r for r in docs[0].reasons)
    saved = {p.rel_path: p for p in service.load_plan(plan.plan_id).proposals}
    assert saved[inst[0].rel_path].accept_prob == 0.93
    settings.save_settings({"use_predictor": False})
    plan2, _ = service.make_plan(root, model=None, ocr=False)
    assert all(p.accept_prob is None for p in plan2.proposals)
    assert "off" in plan2.scan_stats["predictor"]


def test_scores_set_reasons_and_never_change_ticks(demo, monkeypatch):
    root, _ = demo
    before, _ = service.make_plan(root, model=None, ocr=False)
    ticks = {p.rel_path: p.approved for p in before.proposals}
    moves = sorted(p.rel_path for p in before.proposals if p.action == "move")
    score = {rel: (0.2, 0.7, 0.95)[i % 3] for i, rel in enumerate(moves)}
    calls = []
    monkeypatch.setattr(predict, "available", lambda: (True, "ok"))
    monkeypatch.setattr(predict, "predict_accept", lambda plan: calls.append("scored") or score)
    stages = []
    plan, _ = service.make_plan(root, model=None, ocr=False, on_stage=lambda s: stages.append(s) or calls.append(s))
    assert stages == [service.PREDICT_STAGE] and calls == [service.PREDICT_STAGE, "scored"]
    by = {p.rel_path: p for p in plan.proposals}
    for rel, prob in score.items():
        r = by[rel].reasons
        pct = round(prob * 100)
        if prob == 0.2:
            assert f"you often change moves like this ({pct}%) — check it" in r
        elif prob == 0.7:
            assert f"not sure whether you'd accept this ({pct}%)" in r
        else:
            assert f"you usually accept moves like this ({pct}%)" in r
        assert sum("(%d%%)" % pct in x for x in r) == 1
    assert {p.rel_path: p.approved for p in plan.proposals} == ticks  # scores never tick or untick


def test_no_stage_when_predictor_cannot_run(demo, monkeypatch):
    root, _ = demo
    stages = []
    monkeypatch.setattr(predict, "available", lambda: (False, "TabPFN is not installed"))
    service.make_plan(root, model=None, ocr=False, on_stage=stages.append)
    settings.save_settings({"use_predictor": False})
    service.make_plan(root, model=None, ocr=False, on_stage=stages.append)
    assert stages == []


def test_trusted_honours_accept_prob():
    s = settings.defaults()
    p = prop("a.exe")
    assert automation.trusted(p, s)
    p.accept_prob = 0.95
    assert automation.trusted(p, s)
    p.accept_prob = 0.5
    assert not automation.trusted(p, s)
    p.accept_prob = None
    assert automation.trusted(p, s)


def test_old_plans_without_accept_prob_load():
    d = prop("a.exe").to_dict()
    del d["accept_prob"]
    assert Proposal.from_dict(d).accept_prob is None


# --------------------------------------------------------------------------- honest labels

def auto_plan(props, pid="auto1"):
    pl = plan_of(props, pid)
    pl.scan_stats["auto"] = {"run_id": "r1", "moved": []}
    return pl


def raw_rows():
    return [json.loads(l) for l in reviewlog.log_path().read_text(encoding="utf-8").splitlines() if l.strip()]


def test_auto_review_untouched_rows_not_logged():
    ps = [prop(f"f{i}.exe") for i in range(10)]
    approved = {"f0.exe", "f1.exe"}
    assert reviewlog.record(auto_plan(ps), report(approved), approved, {}) == 2
    assert [r["label"] for r in reviewlog.load_rows()] == [1, 1]
    assert {r["source"] for r in raw_rows()} == {"review"} and all(r["v"] == 2 for r in raw_rows())
    # An explicit "keep" is still a decision.
    reviewlog.reset()
    assert reviewlog.record(auto_plan(ps, "auto2"), report(set()), set(), {"f3.exe": "keep"}) == 1
    (row,) = raw_rows()
    assert row["label"] == 0 and row["outcome"] == "keep"
    # Outside an automatic run's review list, unticking still counts as a rejection.
    assert reviewlog.record(plan_of(ps[:2], "manual"), report(set()), set(), {}) == 2


def test_override_records_outcome():
    ps = [prop("a.pdf", "documents"), prop("b.exe"), prop("c.zip", "archives")]
    approved = {"a.pdf", "b.exe"}
    reviewlog.record(plan_of(ps), report(approved), approved, {"a.pdf": "finance"})
    out = {r["features"]["ext"]: (r["label"], r["outcome"]) for r in raw_rows()}
    assert out == {".pdf": (0, "finance"), ".exe": (1, "installers"), ".zip": (0, "keep")}
    # Sent to another folder from an automatic run's review list: also a lesson.
    reviewlog.reset()
    pl = auto_plan([prop("d.pdf", "documents")])
    reviewlog.record(pl, report({"d.pdf"}), {"d.pdf"}, {"d.pdf": "finance"})
    assert [(r["label"], r["outcome"]) for r in raw_rows()] == [(0, "finance")]


@pytest.mark.parametrize("rel,new_name,renamed", [
    ("report.pdf", "report (2).pdf", False),
    ("report (2).pdf", None, False),
    ("report (2).pdf", "report (2) (3).pdf", False),
    ("scan001.pdf", "Invoice 2026.pdf", True),
    ("scan001.pdf", "Invoice 2026 (2).pdf", True),
])
def test_collision_suffix_not_rename(rel, new_name, renamed):
    p = prop(rel, "documents", new_name=new_name)
    assert reviewlog.features(p, plan_of([p]))["renamed"] is renamed


def test_single_class_window(monkeypatch):
    pytest.importorskip("numpy")  # part of the optional [predict] extra
    monkeypatch.setattr(predict, "MAX_TRAIN_ROWS", 40)
    with open(reviewlog.log_path(), "w", encoding="utf-8") as f:
        for i in range(80):  # the older half is mixed, the newest 40 are all accepted
            label = int(i % 2 == 0) if i < 40 else 1
            feats = reviewlog.features(prop(f"x{i}.exe"), plan_of([]))
            f.write(json.dumps({"plan_id": "p", "features": feats, "label": label}) + "\n")
    st = predict.history_status()
    assert st["rows"] == 40 and st["negatives"] == 0 and st["ready"] is False
    monkeypatch.setattr(predict, "available", lambda: (True, "ok"))

    def no_fit():
        raise AssertionError("must not fit on one class")
    monkeypatch.setattr(predict, "_make_classifier", no_fit)
    monkeypatch.setattr(predict, "_model_cache", None)
    assert predict.predict_accept(plan_of([prop("a.exe")])) is None
    assert "Needs both" in predict.status() and "40 accepted, 0 changed" in predict.status()
    # Called directly (scripts/predictor_eval.py), one class also gives None, not a ValueError.
    rows = reviewlog.load_rows()[-40:]
    assert predict.fit_predict_rows(rows, [rows[0]["features"]]) is None
    assert "Needs both" in predict.status()


def test_v1_rows_still_load():
    feats = reviewlog.features(prop("a.exe"), plan_of([]))
    with open(reviewlog.log_path(), "w", encoding="utf-8") as f:
        f.write(json.dumps({"v": 1, "plan_id": "old", "features": {**feats, "renamed": True}, "label": 0}) + "\n")
        f.write(json.dumps({"v": 1, "plan_id": "old", "features": feats, "label": 1}) + "\n")
    reviewlog.record(plan_of([prop("b.zip", "archives")], "new"), report({"b.zip"}), {"b.zip"}, {})
    rows = reviewlog.load_rows()
    assert [(r["plan_id"], r["label"]) for r in rows] == [("old", 0), ("old", 1), ("new", 1)]
    assert rows[0]["features"]["renamed"] is True


# --------------------------------------------------------------------------- automatic runs

RESTORED = SimpleNamespace(results=[SimpleNamespace(status="restored")])


def test_auto_rows_capped_and_relabelled_by_undo():
    ps = [prop(f"i{i}.exe") for i in range(5)] + [prop(f"z{i}.zip", "archives") for i in range(2)] \
        + [prop("dup.exe", duplicate_of="i0.exe"), prop("left.exe")]
    moved = {p.rel_path for p in ps} - {"left.exe"}
    assert reviewlog.record_auto(plan_of(ps), moved, "run1") == 5  # 3 of the 5 .exe + 2 .zip
    assert reviewlog.record_auto(plan_of(ps), moved, "run1") == 0  # once per run
    rows = raw_rows()
    assert all(r["source"] == "auto" and r["run_id"] == "run1" and r["label"] == 1 for r in rows)
    assert {r["outcome"] for r in rows} == {"installers", "archives"}
    assert reviewlog.load_rows() == []  # too recent: the user may still undo
    later = rows[0]["ts"] + 4 * 86400
    assert [r["label"] for r in reviewlog.load_rows(now=later)] == [1] * 5
    assert reviewlog.mark_undone("other", RESTORED) is False  # no rows for that run
    assert reviewlog.mark_undone("run1", SimpleNamespace(results=[])) is False  # nothing came back
    assert reviewlog.mark_undone("run1", RESTORED) is True
    assert reviewlog.mark_undone("run1", RESTORED) is False
    assert [r["label"] for r in reviewlog.load_rows(now=later)] == [0] * 5
    assert [r["label"] for r in reviewlog.load_rows()] == [0] * 5  # an undo is final, even if recent
    assert sum(1 for r in raw_rows() if "undo_run" in r) == 1
    reviewlog.reset()  # "Forget everything" also drops auto rows and markers
    assert not reviewlog.log_path().exists() and reviewlog.load_rows(now=later) == []


def test_recent_auto_rows_skipped():
    reviewlog.record_auto(plan_of([prop("a.exe")]), {"a.exe"}, "run1")
    reviewlog.record(plan_of([prop("b.zip", "archives")], "p2"), report({"b.zip"}), {"b.zip"}, {})
    ts = raw_rows()[0]["ts"]
    assert [r["features"]["ext"] for r in reviewlog.load_rows(now=ts + 2 * 86400)] == [".zip"]
    assert [r["features"]["ext"] for r in reviewlog.load_rows(now=ts + 3 * 86400)] == [".exe", ".zip"]
    # Forged auto rows without a usable run id or time never load.
    feats = raw_rows()[0]["features"]
    with open(reviewlog.log_path(), "a", encoding="utf-8") as f:
        for bad in ({"run_id": 5, "ts": 1}, {"run_id": "x", "ts": "old"}, {"run_id": "x", "ts": True},
                    {"run_id": "x"}):
            f.write(json.dumps({"source": "auto", "features": feats, "label": 1, **bad}) + "\n")
        f.write(json.dumps({"undo_run": 7}) + "\n")
    assert len(reviewlog.load_rows(now=ts + 30 * 86400)) == 2


# --------------------------------------------------------------------------- real TabPFN

HAS_TABPFN = importlib.util.find_spec("tabpfn") is not None and importlib.util.find_spec("torch") is not None


@pytest.mark.skipif(not HAS_TABPFN, reason="tabpfn not installed")
def test_tabpfn_learns_a_user_preference(monkeypatch):
    """Simulated user: always rejects .csv files from bank.example, accepts the rest."""
    monkeypatch.setattr(predict, "_model_cache", None)
    rng = random.Random(7)
    kinds = [("setup{}.exe", "installers", None), ("pack{}.zip", "archives", "github.com"),
             ("Screenshot {}.png", "screenshots", None), ("report{}.pdf", "documents", "mail.example"),
             ("stmt{}.csv", "spreadsheets", "bank.example"), ("data{}.csv", "spreadsheets", "kaggle.com"),
             ("song{}.mp3", "audio", None)]
    for pi in range(10):
        ps, approved = [], set()
        for i in range(8):
            fmt, cat, host = rng.choice(kinds)
            rel = fmt.format(f"{pi}_{i}")
            ps.append(prop(rel, cat, host, conf=rng.choice([0.9, 0.95, 0.97]),
                           size=rng.randint(1_000, 50_000_000), age_days=rng.randint(0, 60)))
            if not (host == "bank.example" and rel.endswith(".csv")):
                approved.add(rel)
        reviewlog.record(plan_of(ps, f"plan{pi}", f"2026-09-{10 + pi}T{9 + pi}:00:00.000"), report(approved),
                         approved, {})
    st = predict.history_status()
    assert st["ready"], st
    new = plan_of([prop("statement_oct.csv", "spreadsheets", "bank.example"),
                   prop("NewAppInstaller.exe", "installers", None)], "new", "2026-10-01T12:00:00.000")
    probs = predict.predict_accept(new)
    assert probs is not None, predict.status()
    assert probs["statement_oct.csv"] < 0.3, probs
    assert probs["NewAppInstaller.exe"] > 0.7, probs
