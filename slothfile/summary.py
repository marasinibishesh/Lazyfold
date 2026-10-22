"""Weekly summary: what SlothFile did for you, read back from its own journals.

Read-only by design. The journals are the single source of truth (they are
written before every action), so the summary can never claim work that did not
happen: a move counts only when the journal holds an intent *and* a done for
it, and anything later undone is subtracted again.
"""

from __future__ import annotations

import math
import ntpath
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .journal import read_journal, runs_dir, state_dir


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return None
    # Journals use naive local time; drop a tz if someone hand-edited one in,
    # so comparisons with the naive window never raise.
    return ts.replace(tzinfo=None) if ts.tzinfo else ts


def _base(p: Any) -> str:
    # ntpath splits on both / and \, so the result is the same on every OS.
    return ntpath.basename(str(p))


def _first_component(p: Any) -> str:
    parts = [x for x in str(p).replace("\\", "/").split("/") if x not in ("", ".")]
    return parts[0] if len(parts) > 1 else "(top level)"


def _int(v: Any) -> int:
    # bool is an int subclass; a corrupt {"size": true} must not count as 1 byte.
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0


def _op(v: Any) -> int | None:
    # Op ids are ints. Anything else (a hand-edited list/dict is unhashable and
    # would crash the set logic; a bool would alias 0/1) is ignored.
    return v if isinstance(v, int) and not isinstance(v, bool) else None


MAX_DAYS = 36500  # 100 years; larger windows overflow datetime arithmetic


def _in_window(recs: list[dict[str, Any]], start: datetime, end: datetime) -> bool:
    """A journal belongs to the window when its first (begin) record does."""
    if not recs or not isinstance(recs[0], dict) or recs[0].get("t") != "begin":
        return False
    ts = _parse_ts(recs[0].get("ts"))
    return ts is not None and start <= ts <= end


def _safe_read(path: Path) -> list[dict[str, Any]]:
    try:
        return [r for r in read_journal(path) if isinstance(r, dict)]
    except (OSError, UnicodeDecodeError):
        return []  # locked or binary garbage: skip that journal, keep the rest


def _empty(days: int, start: datetime, end: datetime) -> dict[str, Any]:
    return {
        "days": days,
        "start": start.isoformat(timespec="seconds"),
        "end": end.isoformat(timespec="seconds"),
        "runs": 0,
        "files_organized": 0,
        "files_renamed": 0,
        "by_folder": {},
        "undone": 0,
        "cleanup_runs": 0,
        "cleanup_files": 0,
        "cleanup_bytes": {"recycle": 0, "delete": 0},
        "recycle_bin_emptied_bytes": 0,
        "recycle_bin_emptied_items": 0,
        "archives_extracted": 0,
        "recycled_installers": 0,
    }


def _add_organize_run(s: dict[str, Any], recs: list[dict[str, Any]]) -> None:
    intents: dict[Any, dict[str, Any]] = {}
    done: set[Any] = set()
    undone: set[Any] = set()
    for r in recs:
        t = r.get("t")
        op = _op(r.get("op"))
        if t == "intent" and op is not None:
            intents[op] = r
        elif t == "done" and op is not None and not r.get("escaped"):
            # An "escaped" done means the file left the folder; not a success.
            done.add(op)
        elif t == "undone" and op is not None:
            undone.add(op)
        elif t == "extracted":
            s["archives_extracted"] += 1
        elif t == "recycled":
            s["recycled_installers"] += 1
    s["runs"] += 1
    s["undone"] += len(done & undone)
    for op in done - undone:
        it = intents.get(op)
        if it is None:
            continue  # done without intent cannot happen in a sane journal; don't guess
        s["files_organized"] += 1
        dst = it.get("dst", "")
        folder = _first_component(dst)
        s["by_folder"][folder] = s["by_folder"].get(folder, 0) + 1
        if _base(dst) != _base(it.get("src", "")):
            s["files_renamed"] += 1


def _add_cleanup_run(s: dict[str, Any], recs: list[dict[str, Any]]) -> None:
    s["cleanup_runs"] += 1
    for r in recs:
        t = r.get("t")
        if t == "removed":
            mode = r.get("mode") if r.get("mode") in ("recycle", "delete") else "delete"
            s["cleanup_files"] += 1
            s["cleanup_bytes"][mode] += _int(r.get("size"))
        elif t == "emptied_recycle_bin":
            s["recycle_bin_emptied_bytes"] += _int(r.get("bytes"))
            s["recycle_bin_emptied_items"] += _int(r.get("items"))
        elif t == "extracted":
            s["archives_extracted"] += 1
        elif t == "recycled":
            s["recycled_installers"] += 1


def summarize(days: int = 7, now: datetime | None = None) -> dict[str, Any]:
    """Totals for the last `days` days from organize and cleanup journals."""
    end = now or datetime.now()
    if end.tzinfo:
        end = end.replace(tzinfo=None)
    try:
        days = int(days)
    except (TypeError, ValueError, OverflowError):
        days = 7
    days = min(max(0, days), MAX_DAYS)
    try:
        start = end - timedelta(days=days)
    except OverflowError:
        start = datetime.min
    s = _empty(days, start, end)
    for p in sorted(runs_dir().glob("*.jsonl")):
        recs = _safe_read(p)
        if _in_window(recs, start, end):
            _add_organize_run(s, recs)
    cdir = state_dir() / "cleanup"
    if cdir.is_dir():
        for p in sorted(cdir.glob("*.jsonl")):
            recs = _safe_read(p)
            if _in_window(recs, start, end):
                _add_cleanup_run(s, recs)
    s["by_folder"] = dict(sorted(s["by_folder"].items(), key=lambda kv: (-kv[1], kv[0])))
    return s


def human_bytes(n: int | float) -> str:
    """1536 -> '1.5 KB'. Binary units (what Explorer shows), labelled KB/MB/GB."""
    try:
        n = float(n or 0)
    except (TypeError, ValueError, OverflowError):
        n = 0.0
    if not math.isfinite(n) or n < 0:
        n = 0.0
    units = ("bytes", "KB", "MB", "GB", "TB")
    for i, unit in enumerate(units):
        last = i == len(units) - 1
        if unit == "bytes":
            if n < 1024:
                return "1 byte" if n == 1 else f"{int(n)} bytes"
        # Compare the *rounded* value so 1023.96 KB shows as "1.0 MB", not "1024.0 KB".
        elif round(n, 1) < 1024 or last:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"  # unreachable; keeps type checkers happy


def _period(days: int) -> str:
    return {1: "Today", 7: "This week", 30: "This month", 31: "This month"}.get(
        days, f"In the last {days} days")


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def format_summary(s: dict[str, Any]) -> str:
    """One friendly headline plus a short breakdown."""
    period = _period(int(s.get("days", 7)))
    organized = s.get("files_organized", 0)
    renamed = s.get("files_renamed", 0)
    cb = s.get("cleanup_bytes", {}) or {}
    freed = sum(cb.values()) + s.get("recycle_bin_emptied_bytes", 0)
    extracted = s.get("archives_extracted", 0)
    installers = s.get("recycled_installers", 0)
    if not (organized or freed or s.get("cleanup_files") or extracted or installers or s.get("undone")):
        return f"Nothing to tidy {period.lower() if period != 'Today' else 'today'} - your sloth rested."

    bits = []
    if organized:
        bits.append(f"organized {_plural(organized, 'file')}")
    if renamed:
        bits.append(f"renamed {renamed}")
    if freed:
        bits.append(f"freed {human_bytes(freed)}")
    if not bits:
        bits.append("kept an eye on things")
    head = bits[0] if len(bits) == 1 else ", ".join(bits[:-1]) + " and " + bits[-1]
    lines = [f"{period} SlothFile {head}."]

    folders = s.get("by_folder") or {}
    if folders:
        top = ", ".join(f"{k} {v}" for k, v in list(folders.items())[:5])
        lines.append(f"  Filed into: {top}")
    if s.get("undone"):
        lines.append(f"  Undone at your request: {_plural(s['undone'], 'file')}")
    if s.get("cleanup_files"):
        parts = []
        if cb.get("recycle"):
            parts.append(f"{human_bytes(cb['recycle'])} to the Recycle Bin")
        if cb.get("delete"):
            parts.append(f"{human_bytes(cb['delete'])} deleted")
        detail = f" ({', '.join(parts)})" if parts else ""
        lines.append(f"  Storage cleanup: {_plural(s['cleanup_files'], 'file')}{detail}")
    if s.get("recycle_bin_emptied_bytes"):
        lines.append(f"  Recycle Bin emptied: {human_bytes(s['recycle_bin_emptied_bytes'])}")
    if extracted:
        lines.append(f"  Archives unpacked: {extracted}")
    if installers:
        lines.append(f"  Old installers sent to the Recycle Bin: {installers}")
    return "\n".join(lines)
