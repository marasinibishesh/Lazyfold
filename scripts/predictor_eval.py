"""SIMULATION: how well could the TabPFN approval predictor learn a user's taste?

This is NOT an evaluation on real users. It builds synthetic review histories
for three *simulated* users with consistent-but-noisy preferences, on plans made
from slothfile.demo fixtures with randomized names, sizes, ages and download
hosts, then compares three ways of guessing "would this user accept the move
unchanged?":

  (a) baseline   - accept everything (what auto-run does today for trusted files)
  (b) learned    - slothfile.learning rules replayed on the same history
  (c) tabpfn     - slothfile.predict.fit_predict_rows (TabPFN v2, CPU)

For each user it trains on the first N reviews (N = 30, 60, 120) and tests on
the next 60, reporting accuracy, ROC AUC and "auto-run precision": of the files
an automatic run would move (trusted by the default settings and, for (b)/(c),
not flagged), the fraction the simulated user would have accepted.

Usage:  python scripts/predictor_eval.py [--seeds 3]
Writes results_local/predictor_eval.json. Uses only temp folders.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics
import sys
import tempfile
import time
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TRAIN_SIZES = (30, 60, 120)
TEST_SIZE = 60
THRESHOLD = 0.9
PLAN_SIZE = 8
HOSTS = {
    "installers": ["github.com", "download.example.org", "", ""],
    "archives": ["github.com", "drive.example.com", ""],
    "screenshots": [""],
    "images": ["", "photos.example.com", "chat.example.com"],
    "finance": ["bank.example", "mail.example.com", "shop.example"],
    "travel": ["mail.example.com", "airline.example"],
    "career": ["hr.contoso.example", "mail.example.com"],
    "manuals": ["support.example.com", ""],
    "documents": ["hr.contoso.example", "mail.example.com", "chat.example.com", ""],
    "spreadsheets": ["bank.example", "hr.contoso.example", "kaggle.com"],
    "presentations": ["hr.contoso.example", "mail.example.com"],
}
DOCS = {"finance", "travel", "career", "manuals", "documents", "spreadsheets", "presentations"}


# ----------------------------------------------------------------- simulated users
# Each returns (label, outcome): label 1 = accepted unchanged; outcome is what
# the user did ("keep", a category, or the proposed category).

def user_a(p, rng):
    """Never wants screenshots moved (95% consistent); otherwise accepts ~95%."""
    if p.category == "screenshots":
        return (0, "keep") if rng.random() < 0.95 else (1, p.category)
    return (1, p.category) if rng.random() < 0.95 else (0, "keep")


def user_b(p, rng):
    """Re-files every document from hr.contoso.example into Career (90%)."""
    if p.category in DOCS and p.category != "career" and p.source_host == "hr.contoso.example":
        return (0, "career") if rng.random() < 0.9 else (1, p.category)
    return (1, p.category) if rng.random() < 0.93 else (0, "keep")


def user_c(p, rng):
    """Accepts almost everything (97%)."""
    return (1, p.category) if rng.random() < 0.97 else (0, "keep")


USERS = {"A (no screenshots)": user_a, "B (re-files hr.contoso docs)": user_b, "C (accepts ~all)": user_c}


# ----------------------------------------------------------------- data

def templates():
    """Move proposals SlothFile makes for the demo fixtures (rules only)."""
    from slothfile import service
    from slothfile.demo import create_demo_folder
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "Downloads"
        create_demo_folder(root)
        plan, _ = service.make_plan(root, model=None, ocr=False)
    return [p for p in plan.proposals if p.action == "move" and not p.duplicate_of and not p.near_duplicate
            and p.category in HOSTS]


def make_history(tpls, user, rng, n_reviews):
    """Sequence of (proposal, plan, label, outcome), plans of PLAN_SIZE files."""
    from slothfile.models import Plan
    out = []
    t = datetime(2026, 3, 1, 9, 0)
    i = 0
    while len(out) < n_reviews:
        t += timedelta(days=rng.uniform(0.5, 4), hours=rng.uniform(-3, 3))
        when = t.replace(hour=rng.choice([8, 9, 12, 13, 18, 20, 21, 22]))
        plan = Plan(plan_id=f"sim{i}", root="C:/sim", created=when.isoformat(timespec="milliseconds"),
                    engine="rules", proposals=[])
        for _ in range(PLAN_SIZE):
            base = rng.choice(tpls)
            stem = f"file_{i}_{rng.randrange(10**6)}"
            size = int(math.exp(rng.uniform(math.log(2_000), math.log(400_000_000))))
            age_s = rng.uniform(0.2, 120) * 86400
            mtime_ns = int((when.timestamp() - age_s) * 1e9)
            p = replace(base, rel_path=stem + base.ext, reasons=list(base.reasons),
                        source_host=rng.choice(HOSTS[base.category]) or None,
                        fingerprint={"size": size, "mtime_ns": mtime_ns, "head_sha": "sim"})
            plan.proposals.append(p)
        for p in plan.proposals:
            label, outcome = user(p, rng)
            out.append((p, plan, label, outcome))
        i += 1
    return out[:n_reviews]


# ----------------------------------------------------------------- metrics

def roc_auc(y, s):
    pos = [v for v, l in zip(s, y) if l == 1]
    neg = [v for v, l in zip(s, y) if l == 0]
    if not pos or not neg:
        return None
    wins = sum(1.0 if a > b else 0.5 if a == b else 0.0 for a in pos for b in neg)
    return wins / (len(pos) * len(neg))


def metrics(y, scores, trusted):
    pred = [1 if s >= 0.5 else 0 for s in scores]
    acc = sum(int(a == b) for a, b in zip(pred, y)) / len(y)
    auto = [l for l, s, t in zip(y, scores, trusted) if t and s >= THRESHOLD]
    return {"accuracy": round(acc, 3), "auc": None if (a := roc_auc(y, scores)) is None else round(a, 3),
            "auto_n": len(auto), "auto_precision": round(sum(auto) / len(auto), 3) if auto else None}


# ----------------------------------------------------------------- methods

def learned_scores(train, test):
    """Replay the training reviews through slothfile.learning in a fresh state
    dir; a test file is 'rejected' when a learned rule would change it."""
    from slothfile import learning
    from slothfile.models import FileRecord, Plan
    with tempfile.TemporaryDirectory() as td:
        os.environ["SLOTHFILE_HOME"] = td
        learning._invalidate()
        by_plan: dict[str, list] = {}
        for p, plan, label, outcome in train:
            by_plan.setdefault(plan.plan_id, []).append((p, plan, label, outcome))
        for items in by_plan.values():
            plan = items[0][1]
            lesson = Plan(plan.plan_id, plan.root, plan.created, plan.engine, [it[0] for it in items])
            recs = {p.rel_path: FileRecord(rel_path=p.rel_path, name=p.rel_path, ext=p.ext, size=1, mtime_ns=0,
                                           mtime_iso="", source_host=p.source_host) for p, *_ in items}
            approved = {p.rel_path for p, _, label, outcome in items if outcome != "keep"}
            overrides = {p.rel_path: outcome for p, _, label, outcome in items
                         if outcome not in ("keep", p.category)}
            learning.record_review(lesson, recs, approved, overrides)
        rules = {r.key: r for r in learning.learned_rules()}
        scores = []
        for p, *_ in test:
            host, ext = learning._norm_host(p.source_host), learning._norm_ext(p.ext)
            rule = (host and rules.get(f"host:{host}")) or (ext and rules.get(f"ext:{ext}")) or None
            scores.append(0.0 if rule is not None and rule.outcome != p.category else 1.0)
        learning._invalidate()
    return scores


def tabpfn_scores(train, test):
    from slothfile import predict, reviewlog
    rows = [{"plan_id": plan.plan_id, "features": reviewlog.features(p, plan), "label": label}
            for p, plan, label, _ in train]
    feats = [reviewlog.features(p, plan) for p, plan, *_ in test]
    t0 = time.perf_counter()
    probs = predict.fit_predict_rows(rows, feats, time_limit=600)
    took = time.perf_counter() - t0
    if probs is None:
        raise SystemExit(f"TabPFN failed: {predict.status()}")
    return probs, took


# ----------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seeds", type=int, default=3)
    args = ap.parse_args()
    print("SIMULATION - synthetic users and synthetic files, not real review data.\n")
    previous = os.environ.get("SLOTHFILE_HOME")
    with tempfile.TemporaryDirectory(prefix="slothfile-eval-") as home:  # removed afterwards
        os.environ["SLOTHFILE_HOME"] = home
        try:
            return _run(args, home)
        finally:
            if previous is None:
                os.environ.pop("SLOTHFILE_HOME", None)
            else:
                os.environ["SLOTHFILE_HOME"] = previous


def _run(args, home: str) -> int:
    from slothfile import automation, predict, settings
    ok, why = predict.available()
    if not ok:
        print(f"TabPFN unavailable: {why}")
        return 1
    tpls = templates()
    s = settings.defaults()
    results = []
    timings = []
    for uname, user in USERS.items():
        for n in TRAIN_SIZES:
            per = {"baseline": [], "learned": [], "tabpfn": []}
            for seed in range(args.seeds):
                rng = random.Random(f"{uname}-{seed}")
                hist = make_history(tpls, user, rng, max(TRAIN_SIZES) + TEST_SIZE)
                train, test = hist[:n], hist[n:n + TEST_SIZE]
                if len({l for *_, l, _ in train}) < 2:
                    continue
                y = [l for _, _, l, _ in test]
                trusted = [automation.trusted(p, s) for p, *_ in test]
                per["baseline"].append(metrics(y, [1.0] * len(y), trusted))
                per["learned"].append(metrics(y, learned_scores(train, test), trusted))
                os.environ["SLOTHFILE_HOME"] = home
                probs, took = tabpfn_scores(train, test)
                timings.append({"train_rows": n, "test_rows": len(test), "seconds": round(took, 2)})
                per["tabpfn"].append(metrics(y, probs, trusted))
            for method, ms in per.items():
                if not ms:
                    continue
                agg = {"user": uname, "train_n": n, "method": method, "seeds": len(ms)}
                for k in ("accuracy", "auc", "auto_precision", "auto_n"):
                    vals = [m[k] for m in ms if m[k] is not None]
                    agg[k] = round(statistics.mean(vals), 3) if vals else None
                results.append(agg)
    hdr = f"{'user':30} {'N':>4} {'method':9} {'acc':>6} {'AUC':>6} {'auto prec':>9} {'auto n':>7}"
    print(hdr)
    print("-" * len(hdr))
    fmt = lambda v: "  -  " if v is None else f"{v:.3f}"  # noqa: E731
    for r in results:
        print(f"{r['user']:30} {r['train_n']:>4} {r['method']:9} {fmt(r['accuracy']):>6} {fmt(r['auc']):>6} "
              f"{fmt(r['auto_precision']):>9} {r['auto_n'] if r['auto_n'] is not None else '-':>7}")
    secs = [t["seconds"] for t in timings]
    first = secs[0] if secs else None
    print(f"\nTabPFN fit+predict on CPU ({len(secs)} fits, 60 test rows): first {first} s (includes model load), "
          f"median {statistics.median(secs):.2f} s, max {max(secs):.2f} s")
    from importlib.metadata import version
    out = {"simulation": True,
           "note": "Synthetic users/files from slothfile.demo fixtures; not real review data.",
           "created": datetime.now().isoformat(timespec="seconds"), "threshold": THRESHOLD,
           "test_size": TEST_SIZE, "seeds": args.seeds, "torch": version("torch"), "tabpfn": version("tabpfn"),
           "results": results, "timings": timings}
    dest = ROOT / "results_local"
    dest.mkdir(exist_ok=True)
    (dest / "predictor_eval.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"\nSaved {dest / 'predictor_eval.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
