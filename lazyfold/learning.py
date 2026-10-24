"""Learn from the user's corrections in the review screen.

Every reviewed move becomes an observation: what the file looked like (its
download host and extension), what Lazyfold proposed and what the user ended
up doing (confirmed it, picked another category, or unticked it). When the
same kind of file is corrected the same way at least twice, and the user has
not since changed their mind, that correction becomes a rule that steers the
next plan. Rules only change *proposals*; the user still approves every move.

State lives in state_dir()/learned.json. The file is treated as untrusted
input on every read: anything malformed or naming an unknown category is
ignored rather than trusted.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .journal import now_iso, state_dir
from .models import FileRecord, Plan
from .rules import CATEGORIES, DOC_CATEGORIES, Decision

MAX_OBSERVATIONS = 2000
MIN_SUPPORT = 2
LEARNED_CONFIDENCE = 0.92
KEEP = "keep"
_VERSION = 1


@dataclass
class Rule:
    key: str  # "host:<host>" or "ext:<ext>"
    outcome: str  # a CATEGORIES key or "keep"
    support: int  # observations with this key that ended in `outcome`
    contradictions: int  # observations with this key that ended differently


def _path() -> Path:
    return state_dir() / "learned.json"


def _valid_outcome(v: Any) -> bool:
    # "duplicates" is never a lesson: only a byte-for-byte comparison may send a file there.
    return isinstance(v, str) and (v == KEEP or (v in CATEGORIES and v != "duplicates"))


def _norm_host(host: Any) -> str | None:
    if not isinstance(host, str):
        return None
    host = host.strip().lower().rstrip(".").removeprefix("www.")
    # Hostnames are short and printable; anything else is not one.
    if not host or len(host) > 253 or any(c.isspace() or not c.isprintable() for c in host):
        return None
    return host


def _norm_ext(ext: Any) -> str | None:
    if not isinstance(ext, str):
        return None
    ext = ext.strip().lower()
    if not ext.startswith(".") or len(ext) > 16 or len(ext) < 2 or not ext[1:].isalnum():
        return None
    return ext


def _clean_obs(o: Any) -> dict[str, Any] | None:
    """Return a validated copy of one stored observation, or None."""
    if not isinstance(o, dict) or not isinstance(o.get("features"), dict):
        return None
    if not _valid_outcome(o.get("outcome")) or not _valid_outcome(o.get("proposed")):
        return None
    f = o["features"]
    host, ext = _norm_host(f.get("host")), _norm_ext(f.get("ext"))
    if host is None and ext is None:
        return None
    ts = o.get("ts") if isinstance(o.get("ts"), str) else ""
    return {"features": {"host": host, "ext": ext}, "proposed": o["proposed"], "outcome": o["outcome"], "ts": ts}


_RETRIES = 10
_RETRY_DELAY = 0.02


def _retry_locked(fn):
    """Run fn, retrying briefly on PermissionError: on Windows a virus scanner
    or the search indexer can hold learned.json open for a moment, and treating
    that as "no history" would let the next save wipe every observation."""
    for attempt in range(_RETRIES):
        try:
            return fn()
        except PermissionError:
            if attempt == _RETRIES - 1:
                raise
            time.sleep(_RETRY_DELAY)


def _load() -> dict[str, Any]:
    """Validated observations; an unreadable file counts as empty."""
    p = _path()
    try:
        data = json.loads(_retry_locked(lambda: p.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return {"observations": []}
    except OSError:
        # Exists but cannot be read right now: use no rules, and never save
        # over it (that would erase the history we failed to read).
        return {"observations": [], "unreadable": True}
    except ( ValueError, UnicodeDecodeError, RecursionError, MemoryError):
        # RecursionError: deeply nested JSON ("[[[[...") must not crash a plan.
        return {"observations": []}
    if not isinstance(data, dict):
        return {"observations": []}
    raw = data.get("observations")
    obs = [c for c in (_clean_obs(o) for o in raw) if c] if isinstance(raw, list) else []
    return {"observations": obs[-MAX_OBSERVATIONS:]}


# apply_learned runs once per file in a plan, so the derived rules are cached
# and only rebuilt when learned.json changes (or this module rewrites it).
_rules_cache: tuple[tuple[Any, ...], dict[str, Rule]] | None = None


def _invalidate() -> None:
    global _rules_cache
    _rules_cache = None


def _file_sig(p: Path) -> tuple[Any, ...] | None:
    try:
        st = os.stat(p)
    except OSError:
        return None
    return (str(p), st.st_mtime_ns, st.st_size, st.st_ino, st.st_ctime_ns)


def _cached_rules() -> dict[str, Rule]:
    global _rules_cache
    p = _path()
    sig = _file_sig(p)
    if sig is None:
        return {}  # no file (or unreadable): same as an empty history
    cached = _rules_cache
    if cached is not None and cached[0] == sig:
        return cached[1]
    state = _load()
    rules = _rules_from(state)
    # Only trust the result if the file was read and did not change meanwhile.
    if not state.get("unreadable") and _file_sig(p) == sig:
        _rules_cache = (sig, rules)
    return rules


def _save(state: dict[str, Any]) -> None:
    """Write via a temp file + os.replace so a crash never leaves half a file."""
    _invalidate()
    p = _path()
    payload = {"version": _VERSION, "observations": state["observations"][-MAX_OBSERVATIONS:]}
    fd, tmp = tempfile.mkstemp(prefix="learned-", suffix=".tmp", dir=p.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        _retry_locked(lambda: os.replace(tmp, p))  # our own state file, not a user file
        _invalidate()
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _usable(rec: FileRecord | None) -> bool:
    """Files the rules refused to judge must not teach or receive rules."""
    return rec is not None and not rec.skip_reason and not rec.ext_mismatch


def record_review(plan: Plan, records_by_name: dict[str, FileRecord],
                  approved: set[str], overrides: dict[str, str]) -> int:
    """Store one observation per proposed move; returns how many were stored.

    overrides maps rel_path -> a category key or "keep" for proposals the user
    changed. Unapproved moves count as "keep"; approved, unchanged ones as a
    confirmation of the proposed category.
    """
    state = _load()
    if state.get("unreadable"):
        return 0
    ts = now_iso()
    added = 0
    for p in plan.proposals:
        if p.action != "move" or p.category not in CATEGORIES:
            continue
        rec = records_by_name.get(p.rel_path)
        if not _usable(rec):
            continue
        if p.rel_path in overrides:
            outcome = overrides[p.rel_path]
        elif p.rel_path not in approved:
            outcome = KEEP
        else:
            outcome = p.category
        if not _valid_outcome(outcome):
            continue
        host, ext = _norm_host(rec.source_host), _norm_ext(rec.ext)
        if host is None and ext is None:
            continue
        state["observations"].append({"features": {"host": host, "ext": ext},
                                      "proposed": p.category, "outcome": outcome, "ts": ts})
        added += 1
    if added:
        _save(state)
    return added


def _keys(obs: dict[str, Any]) -> list[str]:
    f = obs["features"]
    return [k for k in (f["host"] and f"host:{f['host']}", f["ext"] and f"ext:{f['ext']}") if k]


def _rules_from(state: dict[str, Any]) -> dict[str, Rule]:
    by_key: dict[str, list[dict[str, Any]]] = {}
    for o in state["observations"]:
        for k in _keys(o):
            by_key.setdefault(k, []).append(o)
    rules: dict[str, Rule] = {}
    for key, obs in by_key.items():
        latest = obs[-1]["outcome"]  # the user's most recent choice wins
        same = [o for o in obs if o["outcome"] == latest]
        corrected = any(o["proposed"] != latest for o in same)
        if len(same) >= MIN_SUPPORT and corrected:
            rules[key] = Rule(key, latest, len(same), len(obs) - len(same))
    return rules


def learned_rules() -> list[Rule]:
    """Active rules, host rules first (they are more specific than extensions)."""
    rules = _rules_from(_load()).values()
    return sorted(rules, key=lambda r: (not r.key.startswith("host:"), r.key))


def _describe(rule: Rule) -> str:
    kind, _, value = rule.key.partition(":")
    what = f"files from {value}" if kind == "host" else f"{value} files"
    times = f"{rule.support} time{'s' if rule.support != 1 else ''}"
    if rule.outcome == KEEP:
        return f"you left {what} where they were {times} before"
    return f"you moved {what} to {CATEGORIES[rule.outcome]} {times} before"


def apply_learned(rec: FileRecord, decision: Decision) -> Decision:
    """Return a Decision steered by a matching learned rule, else `decision`."""
    if not _usable(rec):
        return decision
    rules = _cached_rules()
    host, ext = _norm_host(rec.source_host), _norm_ext(rec.ext)
    rule = (host and rules.get(f"host:{host}")) or (ext and rules.get(f"ext:{ext}")) or None
    if rule is None:
        return decision
    reason = "learned: " + _describe(rule)
    if rule.outcome == KEEP:
        return Decision(None, 0.0, [*decision.reasons, reason], None)
    if rec.size == 0:
        return decision  # empty files (often failed downloads) stay capped below the threshold
    # A suggested name only makes sense for document folders (see suggest_stem).
    stem = decision.new_stem if rule.outcome in DOC_CATEGORIES else None
    return Decision(rule.outcome, LEARNED_CONFIDENCE, [*decision.reasons, reason], stem)


def forget_rule(key: str) -> bool:
    """Stop applying `key` until it is learned again from new corrections."""
    state = _load()
    if state.get("unreadable") or key not in _rules_from(state):
        return False
    kind, _, value = key.partition(":")
    feature = "host" if kind == "host" else "ext"
    # Blank that feature on past observations instead of deleting them, so the
    # other feature (the extension of a forgotten host, say) keeps its history.
    kept = []
    for o in state["observations"]:
        if o["features"][feature] == value:
            o["features"][feature] = None
        if o["features"]["host"] or o["features"]["ext"]:
            kept.append(o)
    state["observations"] = kept
    _save(state)  # also drops the rules cache
    return True


def reset_learning() -> None:
    """Forget every observation and rule."""
    _invalidate()
    try:
        os.unlink(_path())
    except FileNotFoundError:
        pass
