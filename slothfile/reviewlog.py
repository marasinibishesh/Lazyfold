"""Append-only log of how the user reviewed proposed moves.

One JSON line per reviewed move: a few plain features of the proposal and a
label (1 = moved exactly as proposed, 0 = unticked, kept or sent to another
folder). The approval predictor (predict.py) learns from it. Unlike
learning.py, which turns repeated corrections into hard rules, this keeps the
raw history so a statistical model can pick up softer patterns.

The file lives in state_dir()/reviews.jsonl and is treated as untrusted on
every read: torn, garbage or wrongly typed lines are skipped, never trusted.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from .journal import state_dir
from .models import Plan

MAX_ROWS = 5000
_VERSION = 1

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
        "renamed": bool(p.new_name),
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


def load_rows() -> list[dict[str, Any]]:
    """Valid rows, oldest first (at most MAX_ROWS)."""
    try:
        data = log_path().read_bytes()
    except OSError:
        return []
    rows = []
    for line in data.split(b"\n"):
        line = line.strip()
        if not line:
            continue
        try:
            row = _clean(json.loads(line.decode("utf-8")))
        except (UnicodeDecodeError, ValueError, RecursionError, ArithmeticError):
            continue
        if row:
            rows.append(row)
    return rows[-MAX_ROWS:]


def _label(p: Any, moved: set[str], ticked: set[str], overrides: dict[str, str],
           renames: dict[str, str] | None = None) -> int | None:
    choice = overrides.get(p.rel_path)
    if choice == "keep" or p.rel_path not in ticked:
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


def record(plan: Plan, report: Any, approved: set[str] | None, overrides: dict[str, str],
           renames: dict[str, str] | None = None) -> int:
    """Log one row per reviewed move of `plan` (the plan as proposed, before
    the user's edits); returns how many rows were written. Duplicates and
    near-duplicates are not lessons, and each plan is recorded only once so a
    retried request cannot double-count."""
    overrides = overrides or {}
    seen_file = _seen_path()
    try:
        seen = set(seen_file.read_text(encoding="utf-8").split())
    except OSError:
        seen = set()
    if plan.plan_id in seen:
        return 0
    moved = {r.src for r in report.results if r.status == "moved"}
    ticked = approved if approved is not None else {p.rel_path for p in plan.proposals if p.approved}
    lines = []
    for p in plan.proposals:
        if p.action != "move" or p.duplicate_of or p.near_duplicate:
            continue
        label = _label(p, moved, ticked, overrides, renames)
        if label is None:
            continue
        lines.append(json.dumps({"v": _VERSION, "plan_id": plan.plan_id, "features": features(p, plan),
                                 "label": label}, ensure_ascii=False))
    with open(seen_file, "a", encoding="utf-8") as f:
        f.write(plan.plan_id + "\n")
    if not lines:
        return 0
    path = log_path()
    with open(path, "ab") as f:
        # A torn last line (crash mid-write) must not swallow our first row.
        prefix = b"\n" if f.tell() and not _ends_with_newline(path) else b""
        f.write(prefix + ("\n".join(lines) + "\n").encode("utf-8"))
    _cap(path)
    return len(lines)


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
