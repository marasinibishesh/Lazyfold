"""Read-only scan of exactly one folder (top level only).

The scanner opens files for reading and never writes, renames, executes or
follows links. Anything it cannot fully vouch for gets a `skip_reason` and is
left untouched by every later stage.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from . import extract, sniff
from .models import FileRecord
from .pathsafe import is_link_like

HEAD_BYTES = 64 * 1024
MAX_FULL_HASH = 2 * 1024**3
PARTIAL_DOWNLOAD_EXTS = {".crdownload", ".part", ".partial", ".download", ".opdownload",
                         ".tmp", ".!ut", ".!qb", ".aria2"}
SYSTEM_NAMES = {"desktop.ini", "thumbs.db", ".ds_store", "ehthumbs.db"}
MAC_SYSTEM_NAMES = {"icon\r"}  # Finder's custom-folder-icon file
UF_HIDDEN = 0x8000  # macOS st_flags bit set by `chflags hidden`
RECENT_SECONDS = 120
# Extensions that should be binary; a text/HTML body means a failed or fake download.
BINARY_EXTS = {".pdf", ".zip", ".exe", ".msi", ".png", ".jpg", ".jpeg", ".gif", ".docx",
               ".xlsx", ".pptx", ".mp4", ".mp3", ".7z", ".rar", ".epub", ".iso"}


def file_fingerprint(path: Path) -> dict:
    """Identity of a file's current state, re-checked right before any move."""
    st = os.stat(path, follow_symlinks=False)
    with open(path, "rb") as f:
        head = f.read(HEAD_BYTES)
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns,
            "head_sha": hashlib.sha256(head).hexdigest()}


def full_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_motw_host(path: Path) -> str | None:
    """Windows records where a download came from in the Zone.Identifier
    alternate data stream; macOS keeps the URLs in the kMDItemWhereFroms
    extended attribute. Read only the host name; it stays local."""
    if sys.platform == "darwin":
        return _where_froms_host(path)
    if os.name != "nt":
        return None
    try:
        with open(f"{path}:Zone.Identifier", "r", encoding="utf-8", errors="replace") as f:
            data = f.read(4096)
    except OSError:
        return None
    for line in data.splitlines():
        key, _, val = line.partition("=")
        if key.strip() in ("HostUrl", "ReferrerUrl") and val.strip():
            host = urlparse(val.strip()).hostname
            if host and host not in ("about", "localhost"):
                return host.lower()
    return None


def _where_froms_host(path: Path) -> str | None:
    from . import macos
    for url in macos.where_froms(path):
        try:
            u = urlparse(url.strip())
            host = u.hostname
        except ValueError:
            continue
        if u.scheme.lower() in ("http", "https") and host and host != "localhost":
            return host.lower()
    return None


def _is_hidden_or_system(st: os.stat_result, name: str) -> bool:
    attrs = getattr(st, "st_file_attributes", 0)
    hidden = attrs & (stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM) if attrs else 0
    if sys.platform == "darwin" and (getattr(st, "st_flags", 0) & UF_HIDDEN or name.lower() in MAC_SYSTEM_NAMES
                                     or name.startswith("._")):  # AppleDouble resource-fork files
        return True
    return bool(hidden) or name.lower() in SYSTEM_NAMES or (os.name != "nt" and name.startswith("."))


def scan_folder(root: Path, *, now: float | None = None, read_content: bool = True,
                exclude: set[str] | None = None, ocr: bool = False) -> tuple[list[FileRecord], dict]:
    """Scan the top level of `root`. Returns (records, stats)."""
    root = Path(os.path.abspath(root))
    if is_link_like(root):
        raise ValueError(f"{root} is a link or junction; select the real folder instead")
    if not root.is_dir():
        raise ValueError(f"{root} is not a folder")
    from .executor import check_root  # local import: executor imports scanner
    try:
        check_root(root)
    except Exception as e:
        raise ValueError(str(e)) from e
    now = time.time() if now is None else now
    exclude = {e.casefold() for e in (exclude or set())}
    with os.scandir(root) as it:
        entries = sorted(it, key=lambda e: e.name.casefold())
    with extract.IsolatedExtractor() as extractor:
        records, stats = _scan_entries(root, entries, now, read_content, exclude, extractor)
    _mark_duplicates(root, records)
    if ocr and read_content:
        stats["ocr_files"] = _ocr_pass(root, records)
    stats["skipped"] = sum(1 for r in records if r.skip_reason)
    return records, stats


MAX_OCR_IMAGES = 200
MAX_OCR_BYTES = 25 * 1024 * 1024


def _ocr_pass(root: Path, records: list[FileRecord]) -> int:
    """Read text from scanned PDFs and photos with Windows' built-in OCR (local,
    in a separate PowerShell process). Screenshots are left as screenshots."""
    from . import ocr
    if not ocr.ocr_available():
        return 0
    from .rules import is_screenshot_name
    done = 0
    photos = [r for r in records if not r.skip_reason and not r.ext_mismatch and r.sniffed in ("jpeg", "png")
              and 0 < r.size <= MAX_OCR_BYTES and not is_screenshot_name(r.name)][:MAX_OCR_IMAGES]
    if photos:
        texts = ocr.ocr_images([root / r.rel_path for r in photos])
        for r in photos:
            text = (texts.get(str(root / r.rel_path)) or "").strip()
            if len(re.sub(r"[^A-Za-z]", "", text)) >= 20:  # a picture with real text in it
                r.text = extract._clean(text)
                r.meta["ocr"] = True
                done += 1
    scans = [r for r in records if not r.skip_reason and not r.ext_mismatch and r.sniffed == "pdf" and not r.text
             and not r.meta.get("encrypted") and 0 < r.size <= MAX_OCR_BYTES]
    for r in scans[:50]:
        workdir = Path(tempfile.mkdtemp(prefix="lazyfold-ocr-"))
        try:
            text = ocr.ocr_pdf(root / r.rel_path, workdir).strip()
        finally:
            try:
                os.rmdir(workdir)  # ocr_pdf removes only the files it created
            except OSError:
                pass
        if text:
            r.text = extract._clean(text)
            r.meta["ocr"] = True
            done += 1
    return done


def _scan_entries(root, entries, now, read_content, exclude, extractor):
    records: list[FileRecord] = []
    stats = {"files": 0, "folders_untouched": 0, "skipped": 0, "bytes": 0}
    for entry in entries:
        p = Path(entry.path)
        if entry.name.casefold() in exclude:
            continue
        try:
            st = os.lstat(p)
        except OSError as e:
            records.append(_skipped(entry.name, f"cannot read file info ({e.strerror or e})"))
            continue
        if is_link_like(p):
            records.append(_skipped(entry.name, "symbolic link or junction - never followed", st))
            continue
        if stat.S_ISDIR(st.st_mode):
            stats["folders_untouched"] += 1
            continue
        if not stat.S_ISREG(st.st_mode):
            records.append(_skipped(entry.name, "not a regular file", st))
            continue
        stats["files"] += 1
        stats["bytes"] += st.st_size
        rec = _base_record(entry.name, st)
        if _is_hidden_or_system(st, entry.name):
            rec.skip_reason = "hidden or system file"
        elif rec.ext in PARTIAL_DOWNLOAD_EXTS:
            rec.skip_reason = "unfinished download"
        elif now - st.st_mtime < RECENT_SECONDS:
            rec.skip_reason = "modified in the last 2 minutes (may still be downloading)"
        else:
            _inspect(p, rec, read_content, extractor)
        records.append(rec)
    return records, stats


def _base_record(name: str, st: os.stat_result | None) -> FileRecord:
    ext = os.path.splitext(name)[1].lower()
    mtime_ns = st.st_mtime_ns if st else 0
    iso = datetime.fromtimestamp(mtime_ns / 1e9).isoformat(timespec="seconds") if st else ""
    rec = FileRecord(rel_path=name, name=name, ext=ext, size=st.st_size if st else 0,
                     mtime_ns=mtime_ns, mtime_iso=iso)
    if st is not None:
        born = getattr(st, "st_birthtime_ns", None)
        rec.meta["created_ns"] = born if born else st.st_ctime_ns
    return rec


def _skipped(name: str, reason: str, st: os.stat_result | None = None) -> FileRecord:
    rec = _base_record(name, st)
    rec.skip_reason = reason
    return rec


def _inspect(p: Path, rec: FileRecord, read_content: bool, extractor) -> None:
    try:
        with open(p, "rb") as f:
            head = f.read(HEAD_BYTES)
    except OSError as e:
        rec.skip_reason = f"unreadable ({e.strerror or type(e).__name__})"
        return
    rec.head_sha = hashlib.sha256(head).hexdigest()
    kind = sniff.sniff_bytes(head)
    if kind == "zip":
        kind = sniff.refine_zip(p)
    if rec.ext == ".dmg" and kind not in sniff.EXECUTABLE_KINDS and sniff.is_dmg(p):
        kind = "dmg"  # compressed disk images have no header magic, only the trailer
    rec.sniffed = kind
    rec.source_host = read_motw_host(p)

    if kind in sniff.EXECUTABLE_KINDS and rec.ext not in sniff.COMPATIBLE[kind]:
        rec.ext_mismatch = True
        label = "Mach-O" if kind == "macho" else kind.upper()
        rec.skip_reason = f"content is a {label} program but the name says '{rec.ext or 'no extension'}' - possible disguised executable"
        return
    if sniff.mismatch(kind, rec.ext):
        rec.ext_mismatch = True
        rec.warnings.append(f"extension '{rec.ext}' does not match content ({kind})")
    elif kind is None and rec.ext in BINARY_EXTS and rec.size > 0 and sniff.looks_like_text(head):
        rec.ext_mismatch = True
        is_html = b"<html" in head[:2048].lower() or b"<!doctype html" in head[:2048].lower()
        rec.warnings.append("named '%s' but contains %s - probably a failed download"
                            % (rec.ext, "a web page" if is_html else "plain text"))
    if rec.size == 0:
        rec.warnings.append("empty file")
    if read_content and not rec.ext_mismatch:
        rec.text, rec.meta, warns = extractor(p, kind, rec.ext)
        rec.warnings.extend(warns)


def _mark_duplicates(root: Path, records: list[FileRecord]) -> None:
    """Exact duplicates: same size, then same full SHA-256. Hash only collisions."""
    by_size: dict[int, list[FileRecord]] = defaultdict(list)
    for r in records:
        if not r.skip_reason and r.size > 0 and r.head_sha:
            by_size[r.size].append(r)
    for group in by_size.values():
        if len(group) < 2:
            continue
        for r in group:
            if r.size <= MAX_FULL_HASH:
                try:
                    r.sha256 = full_hash(root / r.rel_path)
                except OSError:
                    r.warnings.append("could not hash for duplicate check")
