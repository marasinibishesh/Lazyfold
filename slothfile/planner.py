"""Turn scan records + classifier decisions into a reviewable plan.

The planner is pure: it reads the disk to check for collisions but changes
nothing. Destinations are built only from the fixed CATEGORIES table, so a
classifier (rule or model) can never name an arbitrary path.
"""

from __future__ import annotations

import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from .models import FileRecord, Plan, Proposal
from .pathsafe import (MAX_PATH, UnsafePath, is_link_like, norm_key, resolve_inside,
                       sanitize_component, sanitize_stem)
from .rules import CATEGORIES, Decision, classify

DEFAULT_THRESHOLD = 0.6
_COPY_MARK = re.compile(r"( - copy( \(\d+\))?| \(\d+\)|_copy\d*|-copy\d*)$|^copy of ", re.I)

Classifier = Callable[[FileRecord], Decision]


def _fingerprint(rec: FileRecord) -> dict:
    return {"size": rec.size, "mtime_ns": rec.mtime_ns, "head_sha": rec.head_sha}


def _keep(rec: FileRecord, d: Decision, why: str | None, engine: str) -> Proposal:
    reasons = list(d.reasons)
    if why:
        reasons.append(why)
    return Proposal(rel_path=rec.rel_path, action="keep", category=d.category, new_name=None,
                    dest_rel=None, confidence=d.confidence, reasons=reasons, engine=engine,
                    fingerprint=_fingerprint(rec))


def _existing_names(dir_path: Path, cache: dict[str, set[str]]) -> set[str]:
    key = norm_key(str(dir_path))
    if key not in cache:
        names: set[str] = set()
        if dir_path.is_dir():
            with os.scandir(dir_path) as it:
                names = {norm_key(e.name) for e in it}
        cache[key] = names
    return cache[key]


def _unique_name(name: str, taken: set[str]) -> str:
    if norm_key(name) not in taken:
        return name
    stem, ext = os.path.splitext(name)
    for i in range(2, 1000):
        cand = f"{stem} ({i}){ext}"
        if norm_key(cand) not in taken:
            return cand
    raise UnsafePath("could not find a free name")


@dataclass
class PlanOptions:
    """Optional behaviours. All off by default so the core plan (and the
    evaluation) is unchanged unless the user's settings switch them on."""

    photos_by_date: bool = False  # Images/2026/10 from the EXIF date
    near_duplicates: bool = False  # similar-but-not-identical copies -> Duplicates, unticked
    learned_rules: bool = False  # apply what the user taught by correcting earlier plans
    unpack_zips: bool = False  # unpack safe .zip files into Archives/<name>/
    recycle_old_installers_days: int | None = None  # suggest recycling installers older than this
    installed: frozenset[str] | None = None  # override the installed-program list (tests)
    now: float | None = None

    @classmethod
    def from_settings(cls, s: dict) -> "PlanOptions":
        days = s.get("recycle_old_installers_days")
        return cls(photos_by_date=bool(s.get("photos_by_date")), near_duplicates=bool(s.get("near_duplicates")),
                   learned_rules=True, unpack_zips=bool(s.get("unpack_zip_archives")),
                   recycle_old_installers_days=int(days) if days else None)


def _recycle_reason(rec: FileRecord, opts: PlanOptions) -> str | None:
    """Why an installer could go to the Recycle Bin, or None to keep it."""
    if opts.recycle_old_installers_days is None or rec.ext not in (".exe", ".msi", ".msix", ".msixbundle"):
        return None
    now = opts.now if opts.now is not None else time.time()
    age_days = (now - rec.mtime_ns / 1e9) / 86400
    from .installed import matching_program
    prog = matching_program(rec.name, opts.installed)
    if prog and age_days >= 7:
        return f"'{prog}' is already installed and this installer is {age_days:.0f} days old"
    if age_days >= opts.recycle_old_installers_days:
        return f"installer is {age_days:.0f} days old (older than {opts.recycle_old_installers_days} days)"
    return None


def _plan_unpack(root: Path, rec: FileRecord, d: Decision, dest_dir_rel: str,
                 dir_cache: dict[str, set[str]], planned: dict[str, set[str]]) -> str | None:
    """Choose a fresh Archives/<name> folder for a zip that passes the safety checks."""
    from .archives import inspect_zip
    check = inspect_zip(root / rec.rel_path)
    if not check.ok:
        d.reasons.append(f"won't unpack: {check.reason}")
        return None
    dkey = norm_key(dest_dir_rel)
    taken = _existing_names(root / dest_dir_rel, dir_cache) | planned.setdefault(dkey, set())
    folder = _unique_name(sanitize_component(os.path.splitext(rec.name)[0], fallback="archive"), taken)
    planned[dkey].add(norm_key(folder))
    d.reasons.append(f"will unpack {check.entries} file(s) into {Path(dest_dir_rel) / folder}")
    return str(Path(dest_dir_rel) / folder)


def build_plan(root: Path, records: list[FileRecord], *, classifier: Classifier = classify,
               engine: str = "rules", threshold: float = DEFAULT_THRESHOLD, rename: bool = True,
               scan_stats: dict | None = None, options: PlanOptions | None = None) -> Plan:
    opts = options or PlanOptions()
    root = Path(os.path.realpath(os.path.abspath(root)))
    proposals: list[Proposal] = []
    dir_cache: dict[str, set[str]] = {}
    planned: dict[str, set[str]] = {}  # dest dir key -> names claimed by this plan
    originals: dict[str, FileRecord] = {}  # sha256 -> first (oldest) copy
    near: dict[str, tuple[str, str]] = {}
    if opts.near_duplicates:
        from .neardup import find_near_duplicates
        try:
            near = find_near_duplicates(root, records)
        except Exception:  # a helper failure must never block the plan
            near = {}
    if opts.learned_rules:
        from .learning import apply_learned
        inner = classifier

        def classifier(rec: FileRecord) -> Decision:  # noqa: F811 - wraps the given classifier
            base = inner(rec)
            return apply_learned(rec, base)

    def original_rank(r: FileRecord):
        # Names without copy markers first, then the earliest created, then the shortest.
        copyish = bool(_COPY_MARK.search(os.path.splitext(r.name)[0]))
        return (copyish, r.meta.get("created_ns", r.mtime_ns), len(r.name), r.name.casefold())

    for rec in sorted(records, key=original_rank):
        if rec.sha256 and not rec.skip_reason:
            originals.setdefault(rec.sha256, rec)
    # A near-copy must point at the file that is actually kept, not at an exact
    # duplicate this same plan moves aside; exact-duplicate handling wins.
    by_rel = {r.rel_path: r for r in records}
    for rel, (orig, why) in list(near.items()):
        r = by_rel.get(rel)
        if r is not None and r.sha256 and originals.get(r.sha256) is not r:
            near.pop(rel)
            continue
        o = by_rel.get(orig)
        if o is not None and o.sha256 and o.sha256 in originals and originals[o.sha256].rel_path != orig:
            kept = originals[o.sha256].rel_path
            near[rel] = (kept, why.replace(orig, kept))

    for rec in records:
        dup_of = None
        is_near = False
        if rec.sha256 and not rec.skip_reason and originals.get(rec.sha256) is not rec:
            dup_of = originals[rec.sha256].rel_path
            d = Decision("duplicates", 0.99, [f"byte-identical to '{dup_of}' (SHA-256 match); "
                                              "this copy is moved aside, not deleted"])
            used_engine = "rules"
        elif rec.rel_path in near and not rec.skip_reason:
            dup_of, why = near[rec.rel_path]
            is_near = True
            d = Decision("duplicates", 0.8, [why, "similar but not identical: unticked until you check it"])
            used_engine = "rules"
        else:
            d = classifier(rec) if not rec.skip_reason else classify(rec)
            if any(r.startswith("Gemma") for r in d.reasons):
                used_engine = "rules+model"
            elif any(r.startswith("learned:") for r in d.reasons):
                used_engine = "rules+learned"
            else:
                used_engine = "rules"

        if d.category is None or d.category not in CATEGORIES:
            proposals.append(_keep(rec, d, None, used_engine))
            continue
        if d.confidence < threshold:
            proposals.append(_keep(rec, d, f"confidence {d.confidence:.2f} below {threshold:.2f}: left in place",
                                   used_engine))
            continue
        if d.category == "installers" and dup_of is None:
            why = _recycle_reason(rec, opts)
            if why:
                # Suggested only: unticked, and the UI asks separately before recycling.
                proposals.append(Proposal(
                    rel_path=rec.rel_path, action="recycle", category="installers", new_name=None, dest_rel=None,
                    confidence=d.confidence, reasons=d.reasons + [why, "suggest Recycle Bin (restorable)"],
                    engine=used_engine, fingerprint=_fingerprint(rec), approved=False,
                    source_host=rec.source_host, ext=rec.ext))
                continue

        stem, ext = os.path.splitext(rec.name)
        new_name = rec.name
        if rename and d.new_stem and dup_of is None:
            clean = sanitize_stem(d.new_stem)
            if clean and norm_key(clean) != norm_key(stem):
                new_name = clean + ext
        new_name = sanitize_component(new_name)

        dest_dir_rel = CATEGORIES[d.category]
        if opts.photos_by_date and d.category == "images" and dup_of is None:
            from .photos import dated_subfolder, photo_date
            ym = photo_date(rec)
            if ym:
                dest_dir_rel = dated_subfolder(*ym)
                d.reasons.append(f"photo taken {ym[0]}-{ym[1]:02d} (camera date)")
        try:
            dest_dir = resolve_inside(root, dest_dir_rel)
        except UnsafePath as e:
            proposals.append(_keep(rec, d, f"destination unsafe: {e}", used_engine))
            continue
        # A file (not a folder) already sits where the category folder should be.
        if any((root / Path(*Path(dest_dir_rel).parts[:i + 1])).is_file()
               for i in range(len(Path(dest_dir_rel).parts))):
            proposals.append(_keep(rec, d, f"a file named like '{dest_dir_rel}' blocks the folder", used_engine))
            continue
        dkey = norm_key(dest_dir_rel)
        taken = _existing_names(dest_dir, dir_cache) | planned.setdefault(dkey, set())
        final = _unique_name(new_name, taken)
        if final != new_name:
            d.reasons.append(f"'{new_name}' already exists there; using '{final}'")
        if len(str(dest_dir / final)) > MAX_PATH:
            proposals.append(_keep(rec, d, "destination path would be too long", used_engine))
            continue
        planned[dkey].add(norm_key(final))
        extract_to = None
        if opts.unpack_zips and d.category == "archives" and dup_of is None and rec.sniffed == "zip" \
                and rec.ext == ".zip":
            extract_to = _plan_unpack(root, rec, d, dest_dir_rel, dir_cache, planned)
        proposals.append(Proposal(
            rel_path=rec.rel_path, action="move", category=d.category,
            new_name=final if final != rec.name else None,
            dest_rel=str(Path(dest_dir_rel) / final), confidence=d.confidence, reasons=d.reasons,
            engine=used_engine, fingerprint=_fingerprint(rec), duplicate_of=dup_of,
            approved=not is_near, extract_to=extract_to, near_duplicate=is_near,
            source_host=rec.source_host, ext=rec.ext))

    return Plan(plan_id=datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6],
                root=str(root), created=datetime.now().isoformat(timespec="seconds"),
                engine=engine, proposals=proposals, scan_stats=scan_stats or {})
