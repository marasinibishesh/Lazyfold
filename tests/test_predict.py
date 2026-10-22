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
    assert predict.history_status() == {"rows": 0, "positives": 0, "negatives": 0, "ready": False}
    _write_rows(26, 4)
    assert predict.history_status()["ready"] is False  # too few negatives
    _write_rows(20, 9)
    assert predict.history_status()["ready"] is False  # too few rows
    _write_rows(25, 5)
    assert predict.history_status() == {"rows": 30, "positives": 25, "negatives": 5, "ready": True}


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
