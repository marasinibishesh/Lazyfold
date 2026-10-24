"""Execute an approved plan, undo a run, and recover from interrupted runs.

Guarantees (each one has a test):
- Nothing is deleted and nothing is overwritten: moves use a primitive that
  fails if the destination exists (os.rename on Windows, renamex_np with
  RENAME_EXCL on macOS, link+unlink elsewhere).
- Every source is re-fingerprinted right before it moves; a file that changed
  since the preview is skipped.
- Every destination is re-validated against the root and the category table,
  so a tampered plan file cannot send files elsewhere.
- An intent record is fsync'ed before each move; recovery reconciles any
  operation left without an outcome by looking at the disk.
"""

from __future__ import annotations

import errno
import os
import re
import stat
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .journal import Journal, read_journal, runs_dir, state_dir
from .models import Plan, Proposal
from .photos import is_dated_image_folder
from .pathsafe import UnsafePath, is_link_like, resolve_inside, sanitize_component
from .rules import CATEGORIES
from .scanner import file_fingerprint
from .winfs import pinned_dirs, real_inside

# Exact (ASCII, case-insensitive) category folders and every prefix of them.
# Deliberately not casefold/NFC: NTFS treats "ﬁ" and "fi" as different names.
_ALLOWED_DIRS = {str(Path(v)).lower() for v in CATEGORIES.values()}
_CATEGORY_PREFIXES = {str(Path(*Path(v).parts[:i + 1])).lower()
                      for v in CATEGORIES.values() for i in range(len(Path(v).parts))}
_RUN_ID = re.compile(r"[A-Za-z0-9-]{1,80}")


def canonical(root: str | Path) -> Path:
    """One spelling per folder: expands 8.3 short names, subst and junction aliases."""
    return Path(os.path.realpath(os.path.abspath(root)))


def same_root(a: str | Path, b: str | Path) -> bool:
    return os.path.normcase(str(canonical(a))) == os.path.normcase(str(canonical(b)))


def check_root(root: Path) -> None:
    """Refuse links, non-folders, and Lazyfold's own state folder."""
    if is_link_like(root) or not root.is_dir():
        raise UnsafePath(f"{root} is not a real folder")
    st = os.path.normcase(str(canonical(state_dir())))
    r = os.path.normcase(str(canonical(root)))
    try:
        inside = os.path.commonpath([st, r]) == st
    except ValueError:
        inside = False
    if inside:
        raise UnsafePath("that folder holds Lazyfold's own journals and plans; choose another folder")


class NeedsRecovery(Exception):
    """A previous run on this folder was interrupted; recover before continuing."""


@dataclass
class OpResult:
    src: str
    dst: str | None
    status: str  # moved | skipped | failed | restored
    detail: str = ""


@dataclass
class RunReport:
    run_id: str
    results: list[OpResult] = field(default_factory=list)

    def count(self, status: str) -> int:
        return sum(1 for r in self.results if r.status == status)

    def summary(self) -> dict[str, int]:
        return {s: self.count(s) for s in ("moved", "recycled", "restored", "skipped", "failed")}


def safe_move(src: Path, dst: Path) -> None:
    """Move without ever replacing an existing destination."""
    if sys.platform == "darwin":
        from . import macos
        if macos.rename_excl(src, dst):  # atomic, EEXIST if taken, no hard links needed
            return
    elif os.name == "nt":
        os.rename(src, dst)  # raises FileExistsError if dst exists; same-volume only
        return
    try:
        os.link(src, dst)  # atomic "create if absent" on POSIX
    except OSError as e:
        if e.errno == errno.EEXIST:
            raise FileExistsError(errno.EEXIST, "destination exists", str(dst)) from e
        if os.path.lexists(dst):
            raise FileExistsError(errno.EEXIST, "destination exists", str(dst)) from e
        os.rename(src, dst)  # filesystem without hard links: checked just above
        return
    os.unlink(src)


def _fp_matches(path: Path, fp: dict) -> bool:
    try:
        cur = file_fingerprint(path)
    except OSError:
        return False
    return all(cur.get(k) == fp.get(k) for k in ("size", "mtime_ns", "head_sha"))


def _regular_file(path: Path) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and not is_link_like(path)


def _err(e: OSError) -> str:
    if getattr(e, "winerror", None) == 32:
        return "file is open in another program"
    if isinstance(e, PermissionError):
        return "permission denied"
    return e.strerror or type(e).__name__


def _run_path(run_id: str) -> Path:
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise FileNotFoundError(f"no run named {run_id!r}")
    return runs_dir() / f"{run_id}.jsonl"


def incomplete_runs(root: Path) -> list[str]:
    """Runs on this root whose journal lacks an end marker for its last phase."""
    out = []
    for p in runs_dir().glob("*.jsonl"):
        recs = read_journal(p)
        if not recs or not recs[0].get("root") or not same_root(recs[0]["root"], root):
            continue
        phases = [r["t"] for r in recs if r["t"] in ("begin", "end", "undo_begin", "undo_end")]
        if phases and phases[-1] in ("begin", "undo_begin"):
            out.append(p.stem)
    return sorted(out)


def _valid_name(name: str) -> bool:
    return bool(name) and ":" not in name and name == sanitize_component(name)


def _dated_parent(category: str | None, parent: Path) -> Path | None:
    """Images/YYYY/MM (photos sorted by camera date) rebuilt from validated digits."""
    if category != "images" or not is_dated_image_folder(str(parent)):
        return None
    _, year, month = parent.parts
    return Path(CATEGORIES["images"], year, month)


def _cleanable_dir(rel: str) -> bool:
    """Folders undo may remove (if empty): category folders and dated photo folders."""
    low = str(Path(rel)).lower()
    return (low in _CATEGORY_PREFIXES or is_dated_image_folder(rel)
            or bool(re.fullmatch(r"images[\\/](19|20|21)\d\d", low)))


def _validate_src(p: Proposal) -> None:
    if len(Path(p.rel_path).parts) != 1 or ":" in p.rel_path:
        raise UnsafePath("source must be a file directly inside the selected folder")


def _validate_op(root: Path, p: Proposal) -> tuple[Path, Path]:
    """Re-derive and re-check everything at execution time; the plan file is
    treated as untrusted input."""
    if p.action != "move" or not p.dest_rel:
        raise UnsafePath("not a move")
    _validate_src(p)
    if p.category not in CATEGORIES:
        raise UnsafePath(f"unknown category {p.category!r}")
    dest = Path(p.dest_rel)
    parent = Path(CATEGORIES[p.category])
    if str(dest.parent).lower() != str(parent).lower():
        parent = _dated_parent(p.category, dest.parent)
        if parent is None:
            raise UnsafePath(f"destination folder '{dest.parent}' is not the '{p.category}' category folder")
    if not _valid_name(dest.name) or str(dest).lower() in _CATEGORY_PREFIXES:
        raise UnsafePath(f"unsafe destination name {dest.name!r}")
    # Build the path from the trusted table, not from the plan's spelling.
    dest_rel = str(parent / dest.name)
    return resolve_inside(root, p.rel_path), resolve_inside(root, dest_rel)


def _validate_extract_to(root: Path, rel: str) -> Path:
    """Unpack target: a brand-new folder directly inside Archives/."""
    target = Path(rel)
    if len(target.parts) != 2 or target.parts[0].lower() != CATEGORIES["archives"].lower() \
            or not _valid_name(target.name):
        raise UnsafePath(f"unpack folder {rel!r} must be a new folder inside {CATEGORIES['archives']}")
    full = resolve_inside(root, str(Path(CATEGORIES["archives"]) / target.name))
    if os.path.lexists(full):
        raise UnsafePath(f"unpack folder {rel!r} already exists")
    return full


def _ensure_dir(root: Path, rel_dir: Path, j: Journal) -> None:
    cur = root
    for part in rel_dir.parts:
        cur = cur / part
        if is_link_like(cur):
            raise UnsafePath(f"{cur} is a link or junction")
        if cur.exists():
            if not cur.is_dir():
                raise UnsafePath(f"{cur} exists and is not a folder")
            continue
        os.mkdir(cur)
        j.write({"t": "mkdir", "path": str(cur.relative_to(root))})


def apply_plan(plan: Plan, *, run_id: str | None = None, interrupt_after: int | None = None,
               allow_recycle: bool = False) -> RunReport:
    """Execute the approved actions in `plan`. Recycle actions also need
    `allow_recycle` (the separate confirmation). `interrupt_after` simulates a crash."""
    root = Path(os.path.abspath(plan.root))
    check_root(root)
    root = canonical(root)
    pending = incomplete_runs(root)
    if pending:
        raise NeedsRecovery(f"run(s) {', '.join(pending)} on this folder were interrupted; run recovery first")
    ops = [p for p in plan.proposals if p.action in ("move", "recycle") and p.approved]
    # Every apply gets its own journal, even when the same plan is applied twice.
    run_id = run_id or f"{plan.plan_id}-{uuid.uuid4().hex[:4]}"
    report = RunReport(run_id)
    with Journal(_run_path(run_id), new=True) as j:
        j.write({"t": "begin", "root": str(root), "plan_id": plan.plan_id, "ops": len(ops)})
        for i, p in enumerate(ops):
            if interrupt_after is not None and i == interrupt_after:
                raise KeyboardInterrupt("simulated interruption")
            if p.action == "recycle":
                report.results.append(_recycle_one(root, i, p, j, allow_recycle))
            else:
                report.results.append(_apply_one(root, i, p, j))
        j.write({"t": "end", **report.summary()})
    return report


_INSTALLER_MAGIC = {".exe": (b"MZ",), ".msi": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
                    ".msix": (b"PK\x03\x04",), ".msixbundle": (b"PK\x03\x04",),
                    ".pkg": (b"xar!",), ".mpkg": (b"xar!",)}  # macOS flat packages are xar archives


def _is_installer_file(path: Path) -> bool:
    if path.suffix.lower() == ".dmg":
        from .sniff import is_dmg
        return is_dmg(path)  # disk images end in a "koly" trailer
    magic = _INSTALLER_MAGIC.get(path.suffix.lower())
    if not magic:
        return False
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError:
        return False
    return head.startswith(magic)


def _recycle_one(root: Path, i: int, p: Proposal, j: Journal, allowed: bool) -> OpResult:
    """Send one old installer to the Recycle Bin (restorable, never a permanent delete)."""
    try:
        if not allowed:
            raise UnsafePath("sending to the Recycle Bin needs its own confirmation")
        if p.category != "installers":
            raise UnsafePath("only installers can be sent to the Recycle Bin")
        _validate_src(p)
        src = resolve_inside(root, p.rel_path)
    except UnsafePath as e:
        j.write({"t": "skip", "op": i, "src": p.rel_path, "reason": str(e)})
        return OpResult(p.rel_path, None, "skipped", str(e))
    if not _regular_file(src) or not _fp_matches(src, p.fingerprint):
        reason = "file changed since the preview"
        j.write({"t": "skip", "op": i, "src": p.rel_path, "reason": reason})
        return OpResult(p.rel_path, None, "skipped", reason)
    if not _is_installer_file(src):
        # The plan's "installers" label is not trusted: the file itself must be one.
        reason = "not an installer (checked extension and file content)"
        j.write({"t": "skip", "op": i, "src": p.rel_path, "reason": reason})
        return OpResult(p.rel_path, None, "skipped", reason)
    from . import winshell
    j.write({"t": "recycle_intent", "op": i, "src": p.rel_path, "fp": p.fingerprint})
    try:
        err = winshell.recycle([src]).get(str(src))
    except (OSError, NotImplementedError) as e:
        err = str(e) or type(e).__name__
    if err or os.path.lexists(src):
        j.write({"t": "fail", "op": i, "error": err or "still present after recycling"})
        return OpResult(p.rel_path, None, "failed", err or "still present after recycling")
    if sys.platform == "darwin":
        # Journal exactly where the Trash put it, so undo restores that item.
        rec: dict = {"t": "recycled", "op": i}
        trash_path = winshell.take_trash_path(src)
        if trash_path:
            rec["trash_path"] = trash_path
        j.write(rec)
        return OpResult(p.rel_path, None, "recycled", "in the Trash")
    j.write({"t": "recycled", "op": i})
    return OpResult(p.rel_path, None, "recycled", "in the Recycle Bin")


def _apply_one(root: Path, i: int, p: Proposal, j: Journal) -> OpResult:
    try:
        src, dst = _validate_op(root, p)
    except UnsafePath as e:
        j.write({"t": "skip", "op": i, "src": p.rel_path, "reason": str(e)})
        return OpResult(p.rel_path, p.dest_rel, "skipped", str(e))
    if not _regular_file(src):
        reason = "file is gone or is no longer a regular file"
    elif not _fp_matches(src, p.fingerprint):
        reason = "file changed since the preview"
    elif os.path.lexists(dst):
        reason = "destination appeared after the preview"
    else:
        reason = None
    if reason:
        j.write({"t": "skip", "op": i, "src": p.rel_path, "reason": reason})
        return OpResult(p.rel_path, p.dest_rel, "skipped", reason)
    dest_rel = str(dst.relative_to(root))
    try:
        _ensure_dir(root, Path(dest_rel).parent, j)
        # Pin the folder chain so it can't be swapped for a junction mid-move.
        with pinned_dirs(root, Path(dest_rel).parent):
            j.write({"t": "intent", "op": i, "src": p.rel_path, "dst": dest_rel, "fp": p.fingerprint})
            safe_move(src, dst)
    except FileExistsError:
        j.write({"t": "fail", "op": i, "error": "destination exists"})
        return OpResult(p.rel_path, p.dest_rel, "skipped", "destination exists")
    except (OSError, UnsafePath) as e:
        msg = _err(e) if isinstance(e, OSError) else str(e)
        j.write({"t": "fail", "op": i, "error": msg})
        return OpResult(p.rel_path, p.dest_rel, "failed", msg)
    if not real_inside(root, dst):
        # Defence in depth: should be impossible while the chain is pinned.
        j.write({"t": "done", "op": i, "verified": False, "escaped": True})
        return OpResult(p.rel_path, dest_rel, "failed",
                        f"file ended up outside the folder at {os.path.realpath(dst)}; check it manually")
    verified = _fp_matches(dst, p.fingerprint) and not os.path.lexists(src)
    j.write({"t": "done", "op": i, "verified": verified})
    detail = "" if verified else "moved, but verification failed"
    if p.extract_to and verified:
        detail = _unpack(root, i, dst, p.extract_to, j)
    return OpResult(p.rel_path, p.dest_rel, "moved", detail)


def _unpack(root: Path, i: int, zip_path: Path, extract_to: str, j: Journal) -> str:
    """Unpack a moved zip into a new folder, journaling every file created so
    undo can remove exactly those files (and only if they are unchanged)."""
    from .archives import ArchiveError, extract_zip
    try:
        target = _validate_extract_to(root, extract_to)
    except UnsafePath as e:
        j.write({"t": "extract_fail", "op": i, "error": str(e)})
        return f"not unpacked: {e}"
    rel_dir = str(target.relative_to(root))
    j.write({"t": "extract_begin", "op": i, "dir": rel_dir})

    def on_file(rel: str, size: int, sha256: str) -> None:
        j.write({"t": "extracted_file", "op": i, "rel": rel, "size": size, "sha256": sha256})

    try:
        with pinned_dirs(root, Path(CATEGORIES["archives"])):
            files = extract_zip(zip_path, target, on_file=on_file)
    except ArchiveError as e:
        j.write({"t": "extract_fail", "op": i, "error": str(e), "files": len(e.extracted)})
        return f"moved; unpacking stopped ({e}); {len(e.extracted)} file(s) unpacked can be undone"
    except (OSError, UnsafePath) as e:
        j.write({"t": "extract_fail", "op": i, "error": str(e)})
        return f"moved; not unpacked ({e})"
    j.write({"t": "extracted", "op": i, "dir": rel_dir, "files": len(files)})
    return f"unpacked {len(files)} file(s) into {rel_dir}"


def _ops_state(recs: list[dict]) -> dict[int, dict]:
    """Collapse journal records into per-op state. `kind` is "move" or "recycle";
    `extracted` lists files unpacked for that op (for undo)."""
    ops: dict[int, dict] = {}
    for r in recs:
        t = r.get("t")
        op = r.get("op")
        if not isinstance(op, int) or isinstance(op, bool):
            continue
        if t == "intent":
            ops[op] = {"src": r["src"], "dst": r["dst"], "fp": r["fp"], "state": "intent", "kind": "move",
                       "extracted": [], "extract_dir": None}
        elif t == "recycle_intent":
            ops[op] = {"src": r["src"], "dst": None, "fp": r["fp"], "state": "recycle_intent", "kind": "recycle",
                       "extracted": [], "extract_dir": None}
        elif op not in ops:
            continue
        elif t == "recycled":
            ops[op]["state"] = "done"
            if isinstance(r.get("trash_path"), str) and r["trash_path"]:
                ops[op]["trash_path"] = r["trash_path"]  # macOS: the item's place in the Trash
        elif t in ("done", "fail"):
            ops[op]["state"] = "done" if t == "done" and not r.get("escaped") else (
                "escaped" if t == "done" else "failed")
        elif t == "extract_begin":
            ops[op]["extract_dir"] = r.get("dir")
            ops[op]["extract_done"] = False
        elif t in ("extracted", "extract_fail"):
            ops[op]["extract_done"] = True
        elif t == "extracted_file":
            ops[op]["extracted"].append((r.get("rel"), r.get("size"), r.get("sha256")))
        elif t == "undo_intent":
            ops[op]["state"] = "undo_intent"
        elif t == "undone":
            ops[op]["state"] = "undone"
        elif t == "undo_fail":
            ops[op]["state"] = "done"
            if "trash_path" in r:  # macOS: the item went back to the Trash, maybe under a new name
                tp = r.get("trash_path")
                ops[op]["trash_path"] = tp if isinstance(tp, str) and tp else None
    return ops


def recover(root: Path) -> list[RunReport]:
    """Reconcile interrupted runs on `root` with what is actually on disk."""
    root = canonical(root)
    reports = []
    for run_id in incomplete_runs(root):
        path = _run_path(run_id)
        recs = read_journal(path)
        undoing = [r["t"] for r in recs if r["t"] in ("begin", "undo_begin")][-1] == "undo_begin"
        report = RunReport(run_id)
        with Journal(path) as j:
            for i, op in sorted(_ops_state(recs).items()):
                src = root / op["src"]
                if op["state"] == "recycle_intent":  # crashed around sending to the Recycle Bin
                    if os.path.lexists(src):
                        j.write({"t": "fail", "op": i, "error": "interrupted before recycling; file untouched"})
                        report.results.append(OpResult(op["src"], None, "skipped", "never recycled"))
                    else:
                        j.write({"t": "recycled", "op": i, "recovered": True})
                        report.results.append(OpResult(op["src"], None, "recycled", "in the Recycle Bin"))
                    continue
                if op["kind"] == "recycle":
                    if op["state"] == "undo_intent":  # crashed while restoring from the bin
                        back = _regular_file(src) and _fp_matches(src, op["fp"])
                        j.write({"t": "undone", "op": i, "recovered": True} if back else
                                {"t": "undo_fail", "op": i, "error": "interrupted before restore"})
                        report.results.append(OpResult(op["src"], op["src"], "restored" if back else "skipped",
                                                       "" if back else "still in the Recycle Bin"))
                    continue
                dst = root / op["dst"]
                if op["state"] == "done" and op["extract_dir"] and op.get("extract_done") is False:
                    # Crashed while unpacking: the zip is organized; leftovers are handled by undo,
                    # which removes every unpacked file that still matches the zip.
                    j.write({"t": "extract_fail", "op": i, "error": "interrupted while unpacking", "recovered": True})
                    report.results.append(OpResult(op["dst"], op["extract_dir"], "skipped",
                                                   "unpacking was interrupted; undo removes the partly unpacked files"))
                    continue
                if op["state"] == "intent":  # crashed around a forward move
                    if _regular_file(dst) and not os.path.lexists(src) and _fp_matches(dst, op["fp"]):
                        j.write({"t": "done", "op": i, "verified": True, "recovered": True})
                        report.results.append(OpResult(op["src"], op["dst"], "moved", "completed before interruption"))
                    elif _regular_file(src) and not os.path.lexists(dst):
                        j.write({"t": "fail", "op": i, "error": "interrupted before move; file untouched"})
                        report.results.append(OpResult(op["src"], op["dst"], "skipped", "never moved"))
                    else:
                        j.write({"t": "fail", "op": i, "error": "ambiguous state after interruption"})
                        report.results.append(OpResult(op["src"], op["dst"], "failed", "check manually"))
                elif op["state"] == "undo_intent":  # crashed around an undo move
                    if _regular_file(src) and not os.path.lexists(dst):
                        j.write({"t": "undone", "op": i, "recovered": True})
                        report.results.append(OpResult(op["dst"], op["src"], "restored", "completed before interruption"))
                    else:
                        j.write({"t": "undo_fail", "op": i, "error": "interrupted before restore"})
                        report.results.append(OpResult(op["dst"], op["src"], "skipped", "still organized"))
            j.write({"t": "undo_end" if undoing else "end", "recovered": True, **report.summary()})
        reports.append(report)
    return reports


def undo_run(run_id: str) -> RunReport:
    """Move every file from a run back where it was, newest first. Re-runnable:
    files that could not be restored (e.g. open in another program) can be
    retried later."""
    path = _run_path(run_id)
    if not path.exists():
        raise FileNotFoundError(f"no run named {run_id}")
    recs = read_journal(path)
    if not recs or recs[0].get("t") != "begin" or not recs[0].get("root"):
        raise FileNotFoundError(f"run {run_id} has no readable journal")
    root = canonical(recs[0]["root"])
    if incomplete_runs(root):
        raise NeedsRecovery("an interrupted run on this folder needs recovery first")
    ops = _ops_state(recs)
    report = RunReport(run_id)
    with Journal(path) as j:
        j.write({"t": "undo_begin"})
        for i in sorted(ops, reverse=True):
            op = ops[i]
            if op["state"] != "done":
                continue
            src = root / op["src"]
            if op["kind"] == "recycle":
                report.results.append(_undo_recycle(root, i, op, j))
                continue
            try:
                dst = resolve_inside(root, op["dst"])
                resolve_inside(root, op["src"])
            except UnsafePath as e:
                j.write({"t": "undo_skip", "op": i, "reason": str(e)})
                report.results.append(OpResult(op["dst"], op["src"], "skipped", str(e)))
                continue
            if not _regular_file(dst) or not _fp_matches(dst, op["fp"]):
                reason = "file was moved or edited after organizing"
            elif os.path.lexists(src):
                reason = "a different file now has the original name"
            else:
                reason = None
            if reason:
                j.write({"t": "undo_skip", "op": i, "reason": reason})
                report.results.append(OpResult(op["dst"], op["src"], "skipped", reason))
                if op["extract_dir"]:
                    # Never remove unpacked files when the zip can't come back: they may be the only copy.
                    report.results.append(OpResult(op["extract_dir"], None, "skipped",
                                                   "unpacked files kept because the original zip " + reason))
                continue
            if op["extract_dir"] and not _undo_unpack(root, i, op, dst, j, report):
                continue
            try:
                with pinned_dirs(root, Path(op["dst"]).parent):
                    j.write({"t": "undo_intent", "op": i})
                    safe_move(dst, src)
            except UnsafePath as e:
                j.write({"t": "undo_skip", "op": i, "reason": str(e)})
                report.results.append(OpResult(op["dst"], op["src"], "skipped", str(e)))
                continue
            except OSError as e:
                j.write({"t": "undo_fail", "op": i, "error": _err(e)})
                report.results.append(OpResult(op["dst"], op["src"], "failed", _err(e)))
                continue
            j.write({"t": "undone", "op": i})
            report.results.append(OpResult(op["dst"], op["src"], "restored"))
        # Remove category folders this run created, deepest first, only if now
        # empty, only once (a later empty folder of the same name is the user's),
        # and never through a link in any component.
        removed = {r["path"] for r in recs if r["t"] == "rmdir"}
        for r in reversed([r for r in recs if r["t"] == "mkdir"]):
            rel = r.get("path", "")
            if rel in removed or not _cleanable_dir(rel):
                continue
            try:
                d = resolve_inside(root, rel)
                with pinned_dirs(root, Path(rel).parent):
                    os.rmdir(d)  # fails harmlessly if anything is inside
                j.write({"t": "rmdir", "path": rel})
                removed.add(rel)
            except (OSError, UnsafePath):
                pass
        j.write({"t": "undo_end", **report.summary()})
    return report


def _files_from_zip(zip_path: Path) -> list | None:
    """What unpacking this zip writes: (safe rel, size, sha256) per member, read
    from the zip itself. This also covers files written just before a crash
    that never reached the journal."""
    import hashlib
    import zipfile

    from .archives import ExtractedFile, _check_infos
    try:
        with zipfile.ZipFile(zip_path) as zf:
            infos = zf.infolist()
            check = _check_infos(infos)
            if not check.ok:
                return None
            out = []
            for info, (rel, is_dir) in zip(infos, check.members):
                if is_dir:
                    continue
                h = hashlib.sha256()
                with zf.open(info) as f:
                    for chunk in iter(lambda: f.read(1024 * 1024), b""):
                        h.update(chunk)
                out.append(ExtractedFile(rel, info.file_size, h.hexdigest()))
            return out
    except Exception:  # unreadable zip: fall back to the journal's list
        return None


def _undo_unpack(root: Path, i: int, op: dict, zip_path: Path, j: Journal, report: "RunReport") -> bool:
    """Remove unpacked files that are still identical to the zip's contents.
    Called only when the zip itself is present and about to be restored, so no
    data can be lost. Files the user changed are kept and reported. Returns
    False if some files could not be removed (e.g. open in another program):
    the zip then stays put so a later undo can retry."""
    from .archives import ExtractedFile, remove_extracted
    rel_dir = op["extract_dir"]
    try:
        target = resolve_inside(root, rel_dir)
        if len(Path(rel_dir).parts) != 2 or Path(rel_dir).parts[0].lower() != CATEGORIES["archives"].lower():
            raise UnsafePath("unexpected unpack folder in journal")
    except (UnsafePath, TypeError) as e:
        j.write({"t": "unextract_skip", "op": i, "reason": str(e)})
        report.results.append(OpResult(str(rel_dir), None, "skipped", f"unpacked files kept: {e}"))
        return True
    if not os.path.lexists(target):
        return True  # the user already removed the unpacked folder
    files = _files_from_zip(zip_path)
    if files is None:
        files = [ExtractedFile(rel, size, sha) for rel, size, sha in op["extracted"]
                 if isinstance(rel, str) and isinstance(sha, str)]
    with pinned_dirs(root, Path(CATEGORIES["archives"])):
        removed, skipped = remove_extracted(target, files)
    changed = [s for s in skipped if "changed since extraction" in s]
    errors = [s for s in skipped if "changed since extraction" not in s and "already gone" not in s]
    j.write({"t": "unextracted", "op": i, "removed": removed, "kept": changed, "errors": errors})
    if changed:
        report.results.append(OpResult(rel_dir, None, "skipped",
                                       f"kept {len(changed)} unpacked file(s) you changed in {rel_dir}"))
    if errors:
        report.results.append(OpResult(rel_dir, None, "failed",
                                       f"could not remove {len(errors)} unpacked file(s) (open in another program?); "
                                       "the zip stays organized - undo again later"))
        return False
    return True


def _undo_recycle_mac(root: Path, i: int, op: dict, j: Journal) -> OpResult:
    """macOS: put back exactly the item the journal says the Trash holds, after
    checking it is still the file that was recycled (nothing is moved otherwise)."""
    from . import macos, winshell
    src = root / op["src"]
    if os.path.lexists(src):
        j.write({"t": "undo_skip", "op": i, "reason": "a file with the original name exists"})
        return OpResult(op["src"], op["src"], "skipped", "a file with the original name exists")
    trashed = op.get("trash_path")
    if not trashed:
        # Location unknown (Finder fallback, or recovered after a crash): look for
        # the original name in the Trash, verify, and send a wrong copy back.
        j.write({"t": "undo_intent", "op": i})
        try:
            ok = winshell.restore_from_recycle_bin(src)
        except (OSError, NotImplementedError):
            ok = False
        return _finish_undo_recycle_mac(i, op, j, src, ok)
    tp = Path(trashed)
    if not macos._in_trash_dir(tp) or not _regular_file(tp) or not _fp_matches(tp, op["fp"]):
        reason = ("the file is no longer in the Trash" if not os.path.lexists(tp)
                  else "the item in the Trash is not the file that was recycled")
        j.write({"t": "undo_fail", "op": i, "error": reason})
        return OpResult(op["src"], op["src"], "failed", reason + "; open the Trash and choose Put Back")
    j.write({"t": "undo_intent", "op": i})
    try:
        ok = macos.restore_from_trash(tp, src)
    except OSError:
        ok = False
    return _finish_undo_recycle_mac(i, op, j, src, ok)


def _finish_undo_recycle_mac(i: int, op: dict, j: Journal, src: Path, ok: bool) -> OpResult:
    """Verify a restore; a different copy goes straight back to the Trash."""
    from . import winshell
    if ok and _regular_file(src) and _fp_matches(src, op["fp"]):
        j.write({"t": "undone", "op": i})
        return OpResult(op["src"], op["src"], "restored", "restored from the Trash")
    if ok and _regular_file(src):
        try:
            back = winshell.recycle([src]).get(str(src)) is None
        except (OSError, NotImplementedError):
            back = False
        rec = {"t": "undo_fail", "op": i, "error": "the Trash returned a different copy", "returned_to_bin": back}
        if back:
            rec["trash_path"] = winshell.take_trash_path(src)
        j.write(rec)
        return OpResult(op["src"], op["src"], "failed",
                        "the Trash held a different file with the same name"
                        + ("; it was put back. " if back else "; it is now in the folder. ")
                        + "Find the original in the Trash and choose Put Back")
    j.write({"t": "undo_fail", "op": i, "error": "could not restore automatically"})
    return OpResult(op["src"], op["src"], "failed",
                    "could not restore automatically; open the Trash and choose Put Back")


def _undo_recycle(root: Path, i: int, op: dict, j: Journal) -> OpResult:
    """Best effort: ask the Recycle Bin to restore the installer, then verify it."""
    if sys.platform == "darwin":
        return _undo_recycle_mac(root, i, op, j)
    from . import winshell
    src = root / op["src"]
    if os.path.lexists(src):
        j.write({"t": "undo_skip", "op": i, "reason": "a file with the original name exists"})
        return OpResult(op["src"], op["src"], "skipped", "a file with the original name exists")
    j.write({"t": "undo_intent", "op": i})
    try:
        ok = winshell.restore_from_recycle_bin(src)
    except (OSError, NotImplementedError):
        ok = False
    if ok and _regular_file(src) and _fp_matches(src, op["fp"]):
        j.write({"t": "undone", "op": i})
        return OpResult(op["src"], op["src"], "restored", "restored from the Recycle Bin")
    if ok and _regular_file(src):
        # The bin returned a different item with the same name (a newer recycled copy).
        # Put it back where it was and say so, rather than leaving the wrong file here.
        try:
            back = winshell.recycle([src]).get(str(src)) is None
        except (OSError, NotImplementedError):
            back = False
        j.write({"t": "undo_fail", "op": i, "error": "the Recycle Bin returned a different copy",
                 "returned_to_bin": back})
        return OpResult(op["src"], op["src"], "failed",
                        "the Recycle Bin held a newer file with the same name"
                        + ("; it was put back. " if back else "; it is now in the folder. ")
                        + "Restore the original from the Recycle Bin yourself")
    j.write({"t": "undo_fail", "op": i, "error": "could not restore automatically"})
    return OpResult(op["src"], op["src"], "failed",
                    "could not restore automatically; open the Recycle Bin and choose Restore")


def run_status(run_id: str) -> dict:
    recs = read_journal(_run_path(run_id))
    ops = _ops_state(recs)
    return {"run_id": run_id, "root": recs[0].get("root") if recs else None,
            "started": recs[0].get("ts") if recs else None,
            "moved": sum(1 for o in ops.values() if o["state"] == "done" and o["kind"] == "move"),
            "recycled": sum(1 for o in ops.values() if o["state"] == "done" and o["kind"] == "recycle"),
            "undone": sum(1 for o in ops.values() if o["state"] == "undone"),
            "complete": bool(recs) and recs[-1]["t"] in ("end", "undo_end")}
