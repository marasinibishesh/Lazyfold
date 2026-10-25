"""High-level operations shared by the CLI and the web UI."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Callable

from .engine import make_classifier
from . import executor
from .executor import RunReport, apply_plan, incomplete_runs, recover, run_status
from .journal import iter_runs, plans_dir
from .models import FileRecord, Plan
from .pathsafe import UnsafePath, norm_key, sanitize_component, sanitize_stem
from .planner import DEFAULT_THRESHOLD, PlanOptions, build_plan
from .rules import CATEGORIES, Decision
from .scanner import scan_folder

Progress = Callable[[int, int, str], None]
PREDICT_STAGE = "Checking your past reviews (TabPFN)…"


def make_plan(folder: str | Path, *, model: str | None = None, threshold: float = DEFAULT_THRESHOLD,
              rename: bool = True, progress: Progress | None = None,
              options: PlanOptions | None = None, ocr: bool | None = None,
              on_stage: Callable[[str], None] | None = None,
              content_names: bool | None = None) -> tuple[Plan, str]:
    """Scan (read-only) and plan. Optional behaviours come from the user's
    settings unless `options`/`ocr` are given explicitly. `on_stage(label)`
    is told about slow steps after the per-file pass (the predictor)."""
    root = Path(os.path.abspath(folder))
    if options is None or ocr is None or content_names is None:
        from .settings import load_settings
        s = load_settings()
        options = options or PlanOptions.from_settings(s)
        ocr = bool(s.get("ocr")) if ocr is None else ocr
        content_names = bool(s.get("name_from_content", True)) if content_names is None else content_names
    records, stats = scan_folder(root, ocr=ocr)
    classifier, engine, status = make_classifier(model, root=root, content_names=content_names)
    total = len(records)
    counter = {"n": 0}

    def tracked(rec: FileRecord) -> Decision:
        counter["n"] += 1
        if progress:
            progress(counter["n"], total, rec.name)
        return classifier(rec)

    try:
        plan = build_plan(root, records, classifier=tracked, engine=engine, threshold=threshold,
                          rename=rename, scan_stats={**stats, "model_status": status}, options=options)
    finally:
        if hasattr(classifier, "close"):
            classifier.close()  # the picture-preview worker
    if getattr(classifier, "disabled_reason", None):
        plan.scan_stats["model_status"] = f"model stopped mid-run ({classifier.disabled_reason}); rest used rules"
    if getattr(classifier, "calls", 0):
        plan.scan_stats["model_calls"] = classifier.calls
        plan.scan_stats["model_seconds"] = round(classifier.seconds, 1)
    _attach_predictions(plan, on_stage)
    save_plan(plan)
    return plan, plan.scan_stats["model_status"]


def _attach_predictions(plan: Plan, on_stage: Callable[[str], None] | None = None) -> None:
    """Score moves with the optional approval predictor (TabPFN, see
    predict.py). Any failure just leaves the plan unscored. Scores only add
    reasons for display; they never tick or untick anything."""
    try:
        from . import predict
        from .settings import load_settings
        if not load_settings().get("use_predictor"):
            plan.scan_stats["predictor"] = "predictor turned off in settings"
            return
        ok, why = predict.available()
        if not ok:
            plan.scan_stats["predictor"] = why
            return
        if on_stage:
            on_stage(PREDICT_STAGE)
        probs = predict.predict_accept(plan)
        plan.scan_stats["predictor"] = predict.status()
        for p in plan.proposals:
            prob = (probs or {}).get(p.rel_path)
            if prob is None:
                continue
            p.accept_prob = round(prob, 3)
            pct = round(prob * 100)
            if prob >= 0.8:
                p.reasons = p.reasons + [f"you usually accept moves like this ({pct}%)"]
            elif prob < 0.5:
                p.reasons = p.reasons + [f"you often change moves like this ({pct}%) — check it"]
            else:
                p.reasons = p.reasons + [f"not sure whether you'd accept this ({pct}%)"]
    except Exception as e:  # a hint must never stop a plan
        plan.scan_stats["predictor"] = f"predictor error ({type(e).__name__})"


def save_plan(plan: Plan) -> Path:
    p = plans_dir() / f"{plan.plan_id}.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(plan.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)  # our own state file, not a user file
    return p


def load_plan(plan_id: str) -> Plan:
    if not isinstance(plan_id, str) or not plan_id or not plan_id.replace("-", "").isalnum():
        raise ValueError("bad plan id")
    return Plan.from_dict(json.loads((plans_dir() / f"{plan_id}.json").read_text(encoding="utf-8")))


def apply_edits(plan: Plan, approved: set[str] | None = None, renames: dict[str, str] | None = None,
                overrides: dict[str, str] | None = None) -> Plan:
    """Apply the user's review choices: which rows are ticked, new names (the
    original extension is always kept) and a different folder for a row
    (`overrides`: rel_path -> category key, or "keep"). Names are made unique
    within the plan; the executor re-checks the disk anyway."""
    renames = renames or {}
    overrides = overrides or {}
    for p in plan.proposals:
        if p.action not in ("move", "recycle"):
            continue
        if approved is not None:
            p.approved = p.rel_path in approved
        choice = overrides.get(p.rel_path)
        if choice == "keep":
            p.approved = False
            continue
        if p.action == "recycle":
            if not choice:
                continue
            # A folder chosen for a suggested-recycle installer: file it instead of recycling it.
            if choice not in CATEGORIES or choice == "duplicates":
                raise UnsafePath(f"unknown folder choice {choice!r} for {p.rel_path}")
            p.action, p.dest_rel = "move", str(Path(CATEGORIES[choice]) / p.rel_path)
            p.category, p.approved = choice, True
            p.reasons = p.reasons + [f"you chose {CATEGORIES[choice]}"]
            continue
        elif choice and choice != p.category:
            if choice not in CATEGORIES or choice == "duplicates":
                raise UnsafePath(f"unknown folder choice {choice!r} for {p.rel_path}")
            p.category = choice
            p.dest_rel = str(Path(CATEGORIES[choice]) / Path(p.dest_rel).name)
            p.reasons = p.reasons + [f"you chose {CATEGORIES[choice]}"]
            p.extract_to = None if choice != "archives" else p.extract_to
        if p.rel_path in renames:
            stem = sanitize_stem(renames[p.rel_path])
            if not stem:
                raise UnsafePath(f"empty name for {p.rel_path}")
            ext = os.path.splitext(p.rel_path)[1]
            p.new_name = sanitize_component(stem + ext)
            p.dest_rel = str(Path(p.dest_rel).parent / p.new_name)
    # Unique against other approved moves *and* against what is already on disk.
    root = Path(plan.root)
    on_disk: dict[str, set[str]] = {}
    planned: set[str] = set()

    def taken_in(folder: Path) -> set[str]:
        key = norm_key(str(folder))
        if key not in on_disk:
            d = root / folder
            names = {norm_key(n) for n in os.listdir(d)} if d.is_dir() else set()
            on_disk[key] = names
        return on_disk[key]

    for p in plan.proposals:
        if p.action != "move" or not p.approved:
            continue
        base = Path(p.dest_rel)
        taken = taken_in(base.parent)
        # Names the user just typed (or a folder they changed) are checked against
        # the disk too; names from the preview were already unique, and a clash
        # appearing since is the executor's job to refuse (it never renames silently).
        edited = p.rel_path in renames or p.rel_path in overrides
        clash = norm_key(str(base)) in planned or (edited and norm_key(base.name) in taken)
        if clash:
            for i in range(2, 1000):
                cand = f"{base.stem} ({i}){base.suffix}"
                if norm_key(cand) not in taken and norm_key(str(base.parent / cand)) not in planned:
                    p.dest_rel, p.new_name = str(base.parent / cand), cand
                    break
        taken.add(norm_key(Path(p.dest_rel).name))
        planned.add(norm_key(p.dest_rel))
    return plan


def _learn(plan: Plan, approved: set[str] | None, overrides: dict[str, str], report: RunReport) -> None:
    """Teach Lazyfold from a review that actually ran. Lessons come only from
    rows that really moved and rows the user deliberately left; duplicates,
    near-duplicates and recycle suggestions are not lessons. Each plan teaches
    at most once, so a retried or resubmitted request can't double-count."""
    from . import learning
    from .journal import state_dir
    seen_file = state_dir() / "learned_plans.txt"
    try:
        seen = set(seen_file.read_text(encoding="utf-8").split())
    except OSError:
        seen = set()
    if plan.plan_id in seen:
        return
    moved = {r.src for r in report.results if r.status == "moved"}
    ticked = approved if approved is not None else {p.rel_path for p in plan.proposals if p.approved}
    lesson = copy.deepcopy(plan)
    lesson.proposals = [p for p in lesson.proposals if p.action == "move" and not p.near_duplicate
                        and not p.duplicate_of
                        and (p.rel_path in moved or p.rel_path not in ticked or overrides.get(p.rel_path) == "keep")]
    with open(seen_file, "a", encoding="utf-8") as f:
        f.write(plan.plan_id + "\n")
    if not lesson.proposals:
        return
    records = {p.rel_path: FileRecord(rel_path=p.rel_path, name=p.rel_path, ext=p.ext, size=0, mtime_ns=0,
                                      mtime_iso="", source_host=p.source_host) for p in lesson.proposals}
    try:
        learning.record_review(lesson, records, ticked, overrides)
    except Exception:  # learning is a convenience; it must never block a run
        pass


def execute(plan_id: str, approved: set[str] | None = None, renames: dict[str, str] | None = None,
            overrides: dict[str, str] | None = None, allow_recycle: bool = False) -> RunReport:
    overrides = overrides or {}
    original = load_plan(plan_id)
    plan = apply_edits(copy.deepcopy(original), approved, renames, overrides)  # validates first
    save_plan(plan)
    report = apply_plan(plan, allow_recycle=allow_recycle)
    _learn(original, approved, overrides, report)
    try:
        from . import reviewlog
        from .settings import load_settings
        if load_settings().get("use_predictor"):  # opting out also stops collecting history
            reviewlog.record(original, report, approved, overrides, renames)
    except Exception:  # the review log feeds a hint; it must never block a run
        pass
    return report


def undo_run(run_id: str) -> RunReport:
    """Undo a run (executor.undo_run), then note it in the review log so an
    undone automatic tidy teaches the predictor to hold such files back.
    The note comes after the real undo and can never fail it."""
    report = executor.undo_run(run_id)
    try:
        from . import reviewlog
        from .settings import load_settings
        if load_settings().get("use_predictor"):  # opting out also stops collecting history
            reviewlog.mark_undone(run_id, report)
    except Exception:  # the review log feeds a hint; it must never block an undo
        pass
    return report


def list_runs(limit: int = 20) -> list[dict]:
    out = []
    for run_id, recs in iter_runs():
        if recs:
            out.append(run_status(run_id))
        if len(out) >= limit:
            break
    return out


__all__ = ["make_plan", "save_plan", "load_plan", "apply_edits", "execute", "list_runs",
           "undo_run", "recover", "incomplete_runs"]
