"""Optional approval predictor: "would this user accept this move unchanged?"

Fits Prior Labs' TabPFN (a pretrained tabular foundation model, run locally on
the CPU) on the user's own review history (reviewlog.py) and scores each move
of a new plan. The score only adds a hint to the review screen and holds back
automatic moves the user usually changes; it never moves anything by itself.

TabPFN pulls in torch, so it is an optional extra (`pip install
slothfile[predict]`, CPU torch first) and is imported lazily inside functions:
the rest of SlothFile and the packaged exe never import it. Weights (TabPFN v2,
~29 MB) download once from Hugging Face without a login into %APPDATA%\\tabpfn.
Built with PriorLabs-TabPFN (Prior Labs License: Apache 2.0 + attribution).
"""

from __future__ import annotations

import importlib.util
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from . import reviewlog
from .models import Plan

log = logging.getLogger(__name__)

MIN_ROWS = 30
MIN_PER_CLASS = 5
MAX_TRAIN_ROWS = 1000  # TabPFN v2's CPU guardrail
TIME_LIMIT = 60.0  # seconds for fit + predict
N_ESTIMATORS = 4

_last_status = "not run yet"
_model_cache: tuple[tuple[Any, ...], Any, dict[str, dict[str, int]]] | None = None
_lock = threading.Lock()


def available() -> tuple[bool, str]:
    """Is TabPFN installed? Cheap: looks for the packages without importing
    torch (that takes seconds and lots of memory)."""
    for mod in ("torch", "tabpfn"):
        try:
            if importlib.util.find_spec(mod) is None:
                return False, f"{mod} is not installed (pip install slothfile[predict], CPU torch first)"
        except (ImportError, ValueError):
            return False, f"{mod} cannot be imported"
    return True, "TabPFN is installed"


def history_status() -> dict[str, Any]:
    rows = reviewlog.load_rows()
    pos = sum(r["label"] for r in rows)
    neg = len(rows) - pos
    return {"rows": len(rows), "positives": pos, "negatives": neg,
            "ready": len(rows) >= MIN_ROWS and pos >= MIN_PER_CLASS and neg >= MIN_PER_CLASS}


def _cache_root() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / ".cache")
    return Path(base) / "tabpfn"


def _model_version(ModelVersion: Any) -> Any:
    """v2 by default (no login needed). v3.5-fast only if the user already has
    a token or the checkpoint is cached; we never trigger a login."""
    v35 = getattr(ModelVersion, "V3_5_FAST", None)
    if v35 is not None:
        if os.environ.get("TABPFN_TOKEN"):
            return v35
        try:
            if any(_cache_root().rglob("tabpfn-v3.5-fast*")):
                return v35
        except OSError:
            pass
    return ModelVersion.V2


def _encode(rows: list[dict[str, Any]], vocab: dict[str, dict[str, int]], grow: bool) -> list[list[float]]:
    """Feature dicts -> numeric rows. Categoricals become integer codes (TabPFN
    is told which columns they are); numbers stay raw (TabPFN needs no scaling).
    Unseen values at prediction time share one 'unknown' code."""
    out = []
    for f in rows:
        row = []
        for name in reviewlog.FEATURES:
            v = f[name]
            if name in reviewlog.CATEGORICAL:
                codes = vocab.setdefault(name, {})
                if v not in codes and grow:
                    codes[v] = len(codes)
                row.append(float(codes.get(v, len(codes))))
            else:
                row.append(float(v))
        out.append(row)
    return out


def _make_classifier() -> Any:
    os.environ.setdefault("TABPFN_NO_BROWSER", "1")
    os.environ.setdefault("TABPFN_DISABLE_TELEMETRY", "1")
    from tabpfn import TabPFNClassifier  # lazy: pulls in torch
    from tabpfn.constants import ModelVersion
    cat_idx = [i for i, n in enumerate(reviewlog.FEATURES) if n in reviewlog.CATEGORICAL]
    return TabPFNClassifier.create_default_for_version(
        _model_version(ModelVersion), device="cpu", n_estimators=N_ESTIMATORS,
        categorical_features_indices=cat_idx)


def _sig() -> tuple[Any, ...] | None:
    try:
        st = os.stat(reviewlog.log_path())
    except OSError:
        return None
    return (str(reviewlog.log_path()), st.st_mtime_ns, st.st_size)


def _fit_predict(rows: list[dict[str, Any]], test: list[dict[str, Any]], key: Any) -> list[float]:
    global _model_cache
    import numpy as np
    cached = _model_cache
    if key is not None and cached is not None and cached[0] == key:
        _, clf, vocab = cached
    else:
        vocab: dict[str, dict[str, int]] = {}
        X = np.asarray(_encode([r["features"] for r in rows], vocab, grow=True), dtype=np.float32)
        y = np.asarray([r["label"] for r in rows])
        clf = _make_classifier()
        clf.fit(X, y)
        if key is not None:
            _model_cache = (key, clf, vocab)
    Xt = np.asarray(_encode(test, vocab, grow=False), dtype=np.float32)
    proba = clf.predict_proba(Xt)  # all rows in one call
    col = list(clf.classes_).index(1)
    return [float(p[col]) for p in proba]


def fit_predict_rows(train: list[dict[str, Any]], test: list[dict[str, Any]], *,
                     time_limit: float = TIME_LIMIT, cache_key: Any = None) -> list[float] | None:
    """P(label 1) for each feature dict in `test`, trained on `train` (rows as
    stored by reviewlog; only the newest MAX_TRAIN_ROWS are used). None on any
    error or timeout. A fitted model is reused while `cache_key` is unchanged.
    Also used by scripts/predictor_eval.py."""
    global _last_status
    train = train[-MAX_TRAIN_ROWS:]
    result: dict[str, Any] = {}

    def work() -> None:
        try:
            result["p"] = _fit_predict(train, test, cache_key)
        except BaseException as e:  # noqa: BLE001 - any failure just means "no prediction"
            result["err"] = f"{type(e).__name__}: {e}"

    t0 = time.perf_counter()
    # A daemon thread lets us give up on a slow fit without blocking the plan.
    th = threading.Thread(target=work, name="slothfile-tabpfn", daemon=True)
    th.start()
    th.join(time_limit)
    took = time.perf_counter() - t0
    if th.is_alive():
        _last_status = f"predictor gave up after {time_limit:.0f} s"
        log.warning(_last_status)
        return None
    if "err" in result:
        _last_status = f"predictor failed ({result['err'][:200]})"
        log.warning(_last_status)
        return None
    _last_status = f"predictor used {len(train)} past reviews ({took:.1f} s)"
    return result["p"]


def predict_accept(plan: Plan) -> dict[str, float] | None:
    """P(the user accepts the move unchanged) per move proposal, or None if
    TabPFN is missing, the history is too small, or anything goes wrong."""
    global _last_status
    ok, why = available()
    if not ok:
        _last_status = why
        return None
    st = history_status()
    if not st["ready"]:
        _last_status = (f"predictor is learning: {st['rows']} reviews so far "
                        f"(needs {MIN_ROWS}, with at least {MIN_PER_CLASS} accepted and {MIN_PER_CLASS} changed)")
        return None
    moves = [p for p in plan.proposals if p.action == "move" and not p.duplicate_of]
    if not moves:
        _last_status = "predictor: nothing to score"
        return {}
    with _lock:  # one fit at a time; the cache is shared
        sig = _sig()
        rows = reviewlog.load_rows()
        probs = fit_predict_rows(rows, [reviewlog.features(p, plan) for p in moves],
                                 cache_key=(len(rows), sig) if sig else None)
    if probs is None:
        return None
    return {p.rel_path: pr for p, pr in zip(moves, probs)}


def status() -> str:
    return _last_status


def explain() -> str:
    st = history_status()
    ok, why = available()
    lines = [
        "SlothFile's approval predictor guesses whether you would accept a proposed move unchanged.",
        "It learns only from your own review history (which moves you kept, unticked or sent elsewhere),",
        "stored on this computer in reviews.jsonl. It uses TabPFN, a pretrained tabular model run on",
        "your CPU; no file contents, names or history leave the computer.",
        "Features: extension, proposed folder, confidence, engine, whether a rename was suggested,",
        "download host, file size, file age and the day/hour of the plan.",
        "A low score only adds a 'check' hint and keeps the file out of automatic runs.",
        f"Status: {why}; {st['rows']} reviews ({st['positives']} accepted, {st['negatives']} changed); "
        f"{'ready' if st['ready'] else 'not ready yet'}. Last run: {_last_status}.",
        "Built with PriorLabs-TabPFN.",
    ]
    return "\n".join(lines)
