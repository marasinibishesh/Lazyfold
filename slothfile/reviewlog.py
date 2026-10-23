"""Append-only log of how the user reviewed proposed moves.

One JSON line per reviewed move: a few plain features of the proposal and a
label (1 = moved exactly as proposed, 0 = unticked, kept or sent to another
folder). The approval predictor (predict.py) learns from it. Unlike
learning.py, which turns repeated corrections into hard rules, this keeps the
raw history so a statistical model can pick up softer patterns.

Since v2 a row also says where it came from ("source": "review" for the review
screen, "auto" for a move the automatic tidy made) and where the file ended up
("outcome": a category key or "keep", never a path). Automatic moves count as
accepted once they are a few days old; undoing their run (an {"undo_run": id}
marker line) turns them into rejections.

The file lives in state_dir()/reviews.jsonl and is treated as untrusted on
every read: torn, garbage or wrongly typed lines are skipped, never trusted.
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .journal import state_dir
from .models import Plan
from .rules import CATEGORIES

MAX_ROWS = 5000
_VERSION = 2
AUTO_PER_GROUP = 3  # rows per (ext, category, host) from one automatic run
AUTO_SETTLE_DAYS = 3  # an automatic move counts as kept only after this
_COLLISION = re.compile(r" \(\d+\)$")  # "name (2)": the planner avoiding a clash, not a rename

# name -> type of every feature column, in model order.
FEATURES: dict[str, type] = {
    "ext": str, "category": str, "confidence": float, "engine": str, "renamed": bool,
    "source_host_known": bool, "source_host": str, "log2_size": float, "age_days": float,
    "weekday": int, "hour": int,
}
CATEGORICAL = ("ext", "category", "engine", "source_host")


def log_path() -> Path:
    return state_dir() / "reviews.jsonl"


def _seen_path() -> Path:
    return state_dir() / "reviewed_plans.txt"


def _plan_time(plan: Plan) -> datetime | None:
    try:
        return datetime.fromisoformat(plan.created)
    except (TypeError, ValueError):
        return None


def _renamed(p: Any) -> bool:
    """Did the plan suggest a real new name? A collision suffix alone is not one."""
    if not p.new_name:
        return False
    stem, ext = os.path.splitext(p.new_name)
    return _COLLISION.sub("", stem) + ext != os.path.basename(p.rel_path)


def features(p: Any, plan: Plan) -> dict[str, Any]:
    """Model features of one proposal. Everything comes from the proposal and
    its fingerprint, so a plan can be scored before anything moves."""
    fp = p.fingerprint if isinstance(p.fingerprint, dict) else {}
    size = fp.get("size") if isinstance(fp.get("size"), int) else 0
    mtime_ns = fp.get("mtime_ns") if isinstance(fp.get("mtime_ns"), int) else 0
    when = _plan_time(plan)
    age = 0.0
    if when is not None and mtime_ns > 0:
        age = max(0.0, (when.timestamp() - mtime_ns / 1e9) / 86400)
    host = (p.source_host or "").strip().lower().removeprefix("www.")[:253]
    return {
        "ext": (p.ext or os.path.splitext(p.rel_path)[1]).lower()[:16],
        "category": p.category or "",
        "confidence": round(float(p.confidence), 4),
        "engine": p.engine or "",
        "renamed": _renamed(p),
        "source_host_known": bool(host),
        "source_host": host,
        "log2_size": round(math.log2(size + 1), 3),
        "age_days": round(age, 2),
        "weekday": when.weekday() if when else 0,
        "hour": when.hour if when else 0,
    }


def _clean(row: Any) -> dict[str, Any] | None:
    """A validated copy of one stored row, or None."""
    if not isinstance(row, dict) or row.get("label") not in (0, 1) or isinstance(row.get("label"), bool):
        return None
    f = row.get("features")
    if not isinstance(f, dict):
        return None
    out: dict[str, Any] = {}
    for name, typ in FEATURES.items():
        v = f.get(name)
        if typ is float:
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                return None
            try:
                v = float(v)
            except (OverflowError, ValueError):
                return None
            if not math.isfinite(v) or abs(v) > 1e6:
                return None
        elif typ is int:
            if isinstance(v, bool) or not isinstance(v, int) or not -1e6 <= v <= 1e6:
                return None
        elif not isinstance(v, typ):
            return None
        out[name] = v
    pid = row.get("plan_id") if isinstance(row.get("plan_id"), str) else ""
    return {"plan_id": pid, "features": out, "label": row["label"]}


def _read_objects() -> list[Any]:
    """Every parseable JSON line of the log, oldest first."""
    try:
        data = log_path().read_bytes()
    except OSError:
        return []
    out = []
    for line in data.split(b"\n"):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line.decode("utf-8")))
        except (UnicodeDecodeError, ValueError, RecursionError, ArithmeticError):
            continue
    return out


def _undo_marker(obj: Any) -> str | None:
    """The run id of an {"undo_run": id} marker line, else None."""
    if isinstance(obj, dict) and "label" not in obj and isinstance(obj.get("undo_run"), str):
        return obj["undo_run"] or None
    return None


def load_rows(now: float | None = None) -> list[dict[str, Any]]:
    """Valid rows, oldest first (at most MAX_ROWS). Rows from an automatic run
    become label 0 if that run was undone, and are left out while younger
    than AUTO_SETTLE_DAYS (the user may still undo them)."""
    objs = _read_objects()
    undone = {m for m in map(_undo_marker, objs) if m}
    now = time.time() if now is None else now
    rows = []
    for obj in objs:
        if _undo_marker(obj):
            continue  # a marker, not a row
        row = _clean(obj)
        if not row:
            continue
        if obj.get("source") == "auto":
            run_id, ts = obj.get("run_id"), obj.get("ts")
            if not isinstance(run_id, str) or isinstance(ts, bool) or not isinstance(ts, (int, float)):
                continue
            if run_id in undone:
                row["label"] = 0  # the user took the whole run back
            elif not now - ts >= AUTO_SETTLE_DAYS * 86400:  # also catches NaN
                continue  # too recent to count as kept
        rows.append(row)
    return rows[-MAX_ROWS:]


def _label(p: Any, moved: set[str], ticked: set[str], overrides: dict[str, str],
           renames: dict[str, str] | None = None, auto_review: bool = False) -> int | None:
    choice = overrides.get(p.rel_path)
    if choice == "keep":
        return 0
    if p.rel_path not in ticked:
        # An automatic run's review list starts all unticked, so leaving a row
        # alone there is not a decision (picking another folder still is).
        if auto_review and not choice:
            return None
        return 0
    if choice and choice != p.category:
        return 0  # moved, but the user picked another folder
    typed = (renames or {}).get(p.rel_path)
    if typed is not None:
        proposed = os.path.splitext(p.new_name or p.rel_path)[0]
        if typed.strip() and typed.strip() != proposed:
            return 0  # moved, but the user changed the name
    if p.rel_path in moved:
        return 1
    return None  # ticked but skipped/failed: says nothing about the user


def _outcome(p: Any, ticked: set[str], overrides: dict[str, str]) -> str | None:
    """Where the user sent the file: a category key or "keep", never a path."""
    choice = overrides.get(p.rel_path)
    if choice == "keep" or (p.rel_path not in ticked and not choice):
        return "keep"
    key = choice or p.category
    return key if key in CATEGORIES else None


def _seen() -> set[str]:
    try:
        return set(_seen_path().read_text(encoding="utf-8").split())
    except OSError:
        return set()


def _mark_seen(key: str) -> None:
    with open(_seen_path(), "a", encoding="utf-8") as f:
        f.write(key + "\n")


def _append(lines: list[str]) -> None:
    path = log_path()
    with open(path, "ab") as f:
        # A torn last line (crash mid-write) must not swallow our first row.
        prefix = b"\n" if f.tell() and not _ends_with_newline(path) else b""
        f.write(prefix + ("\n".join(lines) + "\n").encode("utf-8"))
    _cap(path)


def _ok_run_id(run_id: Any) -> bool:
    return isinstance(run_id, str) and bool(run_id) and not any(c.isspace() for c in run_id)


def record(plan: Plan, report: Any, approved: set[str] | None, overrides: dict[str, str],
           renames: dict[str, str] | None = None) -> int:
    """Log one row per reviewed move of `plan` (the plan as proposed, before
    the user's edits); returns how many rows were written. Duplicates and
    near-duplicates are not lessons, and each plan is recorded only once so a
    retried request cannot double-count."""
    overrides = overrides or {}
    if plan.plan_id in _seen():
        return 0
    # The review list an automatic run left behind (automation.auto_run).
    auto_review = bool((plan.scan_stats or {}).get("auto"))
    moved = {r.src for r in report.results if r.status == "moved"}
    ticked = approved if approved is not None else {p.rel_path for p in plan.proposals if p.approved}
    lines = []
    for p in plan.proposals:
        if p.action != "move" or p.duplicate_of or p.near_duplicate:
            continue
        label = _label(p, moved, ticked, overrides, renames, auto_review)
        if label is None:
            continue
        lines.append(json.dumps({"v": _VERSION, "source": "review", "plan_id": plan.plan_id,
                                 "features": features(p, plan), "label": label,
                                 "outcome": _outcome(p, ticked, overrides)}, ensure_ascii=False))
    _mark_seen(plan.plan_id)
    if not lines:
        return 0
    _append(lines)
    return len(lines)


def record_auto(plan: Plan, moved_paths: set[str], run_id: str) -> int:
    """Log the moves automatic run `run_id` made as kept (label 1), with the
    features they had when they moved. At most AUTO_PER_GROUP rows per (ext,
    category, host), so one big run cannot flood the training window; each
    run is recorded once. load_rows() counts them only after AUTO_SETTLE_DAYS,
    and as rejections once the run is undone (mark_undone)."""
    if not _ok_run_id(run_id):
        return 0
    key = "auto:" + run_id
    if key in _seen():
        return 0
    groups: dict[tuple[str, str, str], int] = {}
    ts = time.time()
    lines = []
    for p in plan.proposals:
        if p.rel_path not in moved_paths or p.action != "move" or p.duplicate_of or p.near_duplicate:
            continue
        f = features(p, plan)
        group = (f["ext"], f["category"], f["source_host"])
        if groups.get(group, 0) >= AUTO_PER_GROUP:
            continue
        groups[group] = groups.get(group, 0) + 1
        lines.append(json.dumps({"v": _VERSION, "source": "auto", "run_id": run_id, "plan_id": plan.plan_id,
                                 "ts": ts, "features": f, "label": 1,
                                 "outcome": p.category if p.category in CATEGORIES else None},
                                ensure_ascii=False))
    _mark_seen(key)
    if not lines:
        return 0
    _append(lines)
    return len(lines)


def mark_undone(run_id: str, report: Any) -> bool:
    """Note that the user undid automatic run `run_id`, so its rows count as
    rejections. Written once per run, only if the undo really put something
    back and the run has rows in the log. True if a marker was written."""
    if not _ok_run_id(run_id):
        return False
    if not any(getattr(r, "status", None) == "restored" for r in getattr(report, "results", [])):
        return False
    key = "undo:" + run_id
    if key in _seen():
        return False
    objs = _read_objects()
    if not any(isinstance(o, dict) and o.get("source") == "auto" and o.get("run_id") == run_id for o in objs):
        return False  # not an automatic run we learned from
    if run_id not in {_undo_marker(o) for o in objs}:
        _append([json.dumps({"v": _VERSION, "undo_run": run_id})])
    _mark_seen(key)
    return True


def _ends_with_newline(path: Path) -> bool:
    with open(path, "rb") as f:
        f.seek(-1, os.SEEK_END)
        return f.read(1) == b"\n"


def _cap(path: Path) -> None:
    """Keep only the newest MAX_ROWS lines (rewritten atomically)."""
    data = path.read_bytes().split(b"\n")
    lines = [l for l in data if l.strip()]
    if len(lines) <= MAX_ROWS:
        return
    fd, tmp = tempfile.mkstemp(prefix="reviews-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(b"\n".join(lines[-MAX_ROWS:]) + b"\n")
        os.replace(tmp, path)  # our own state file, not a user file
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def reset() -> None:
    """Forget the review history."""
    for p in (log_path(), _seen_path()):
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass
