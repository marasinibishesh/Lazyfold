"""Safe unpacking of downloaded .zip archives (stdlib zipfile only).

An archive is someone else's description of files to create on this disk, so
it is checked like any other untrusted plan before a single byte is written:
no paths that climb out of the target folder (zip-slip), no links, no
encrypted members, no zip bombs, no names Windows would treat as devices.
Extraction only ever creates new things inside a folder it created itself,
never overwrites, and reports each file (with its hash) so the caller can
journal it and undo exactly what was written. 7z and rar need third-party
code and are reported as unsupported rather than half-handled.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .pathsafe import WINDOWS_RESERVED, UnsafePath, is_link_like, norm_key, resolve_inside, sanitize_component

MAX_ENTRIES = 2000
MAX_TOTAL = 2 * 1024 ** 3  # 2 GiB uncompressed
MAX_RATIO = 200
# Below this size a high ratio is harmless (a 50 KB file of spaces compresses
# 1000:1 but cannot fill a disk), so the ratio check would only cause false alarms.
RATIO_MIN_SIZE = 64 * 1024
CHUNK = 1024 * 1024
FREE_SPACE_MARGIN = 256 * 1024 ** 2

_UNSUPPORTED_EXT = {".7z": "7z", ".rar": "rar", ".tar": "tar", ".gz": "gzip", ".tgz": "tar.gz",
                    ".bz2": "bzip2", ".xz": "xz", ".zst": "zstd", ".cab": "cab"}
_MAGIC = [(b"7z\xbc\xaf\x27\x1c", "7z"), (b"Rar!\x1a\x07", "rar"), (b"\x1f\x8b", "gzip")]
_DRIVE = re.compile(r"^[A-Za-z]:")

OnFile = Callable[[str, int, str], None]


class ArchiveError(Exception):
    """Extraction stopped. `extracted` lists what was written before the stop,
    so the caller can journal it or undo it with remove_extracted()."""

    def __init__(self, message: str, extracted: list["ExtractedFile"] | None = None):
        super().__init__(message)
        self.extracted = list(extracted or [])


@dataclass
class ZipCheck:
    ok: bool
    reason: str
    entries: int = 0
    total_uncompressed: int = 0
    encrypted: bool = False
    members: list[tuple[str, bool]] = field(default_factory=list, repr=False)  # (safe rel, is_dir)


@dataclass
class ExtractedFile:
    rel: str  # relative to dest_dir, OS separator
    size: int
    sha256: str


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _unsupported(path: Path) -> str | None:
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError:
        head = b""
    for magic, kind in _MAGIC:
        if head.startswith(magic):
            return f"{kind} archives are not supported (only .zip)"
    kind = _UNSUPPORTED_EXT.get(path.suffix.lower())
    if kind and not head.startswith(b"PK"):
        return f"{kind} archives are not supported (only .zip)"
    return None


def _safe_rel(name: str) -> tuple[str | None, str]:
    """Validate one member name. Returns (safe relative path, "") or (None, why)."""
    raw = name.replace("\\", "/")
    if raw.startswith("/") or _DRIVE.match(raw):
        return None, f"absolute path {name!r}"
    if ":" in raw:
        return None, f"':' in {name!r} (drive letter or alternate data stream)"
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if not parts:
        return None, f"empty name {name!r}"
    out = []
    for part in parts:
        if part == ".." or part.strip(" .") == "":
            return None, f"path traversal in {name!r}"
        clean = sanitize_component(part)
        # sanitize_component prefixes "_" exactly when the cleaned name is a
        # device name ("CON", "nul .txt", "COM1\x00"); refuse those outright.
        if part.split(".")[0].strip().upper() in WINDOWS_RESERVED or \
                (clean.startswith("_") and not part.lstrip().startswith("_")):
            return None, f"reserved Windows name in {name!r}"
        out.append(clean)
    return os.path.join(*out), ""


def _precheck(path: Path) -> str | None:
    """Checks on the archive file itself, before it is opened as a zip."""
    why = _unsupported(path)
    if why:
        return why
    if is_link_like(path) or not path.is_file():
        return "not a regular file"
    return None


def _check_infos(infos: list[zipfile.ZipInfo]) -> ZipCheck:
    """Per-entry safety checks on an already-read central directory."""
    n = len(infos)
    if n > MAX_ENTRIES:
        return ZipCheck(False, f"too many entries ({n} > {MAX_ENTRIES})", entries=n)
    total = sum(i.file_size for i in infos)
    encrypted = any(i.flag_bits & 0x1 for i in infos)
    base = dict(entries=n, total_uncompressed=total, encrypted=encrypted)
    if encrypted:
        return ZipCheck(False, "archive is password-protected", **base)
    if total > MAX_TOTAL:
        return ZipCheck(False, f"would unpack to {total / 1024 ** 3:.1f} GiB (limit 2 GiB)", **base)
    seen: dict[str, str] = {}
    dirs: set[str] = set()
    files: set[str] = set()
    members: list[tuple[str, bool]] = []
    for info in infos:
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            return ZipCheck(False, f"symbolic link member {info.filename!r}", **base)
        if info.file_size >= RATIO_MIN_SIZE and info.file_size > MAX_RATIO * max(info.compress_size, 1):
            return ZipCheck(False, f"{info.filename!r} expands more than {MAX_RATIO}x (possible zip bomb)", **base)
        rel, why = _safe_rel(info.filename)
        if rel is None:
            return ZipCheck(False, f"unsafe name: {why}", **base)
        key = norm_key(rel)
        if key in seen:
            return ZipCheck(False, f"duplicate name {info.filename!r} (same as {seen[key]!r} on Windows)", **base)
        seen[key] = info.filename
        is_dir = info.is_dir()
        parts = Path(rel).parts
        for i in range(1, len(parts) if not is_dir else len(parts) + 1):
            dirs.add(norm_key(os.path.join(*parts[:i])))
        if not is_dir:
            files.add(key)
        members.append((rel, is_dir))
    if files & dirs:
        return ZipCheck(False, "a name is used both as a file and as a folder", **base)
    return ZipCheck(True, "ok", members=members, **base)


def inspect_zip(path: Path) -> ZipCheck:
    """Decide whether `path` is a zip that is safe to extract, without writing."""
    path = Path(path)
    why = _precheck(path)
    if why:
        return ZipCheck(False, why)
    try:
        if not zipfile.is_zipfile(path):
            return ZipCheck(False, "not a zip archive")
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
    except (OSError, zipfile.BadZipFile, ValueError, NotImplementedError) as e:
        return ZipCheck(False, f"unreadable zip ({e})")
    return _check_infos(infos)


def _mkdirs(dest_dir: Path, rel_dir: Path) -> None:
    """Create rel_dir under dest_dir one component at a time, never through a link."""
    cur = dest_dir
    for part in rel_dir.parts:
        cur = cur / part
        if is_link_like(cur):
            raise UnsafePath(f"{cur} is a link or junction")
        if cur.exists():
            if not cur.is_dir():
                raise UnsafePath(f"{cur} exists and is not a folder")
            continue
        os.mkdir(cur)


def _zip_mtime(info: zipfile.ZipInfo) -> float | None:
    try:
        return time.mktime((*info.date_time, 0, 0, -1))
    except (OverflowError, ValueError):
        return None


def extract_zip(path: Path, dest_dir: Path, *, on_file: OnFile | None = None) -> list[ExtractedFile]:
    """Extract a checked zip into a NEW folder dest_dir; never overwrites.

    The archive is opened once: the safety checks run on the same handle the
    data is read from, so swapping the file after a separate inspect_zip()
    cannot smuggle in unchecked entries.

    Raises ArchiveError (with .extracted) on the first problem; files already
    written stay in place for the caller to journal or undo.
    """
    path, dest_dir = Path(path), Path(dest_dir)
    why = _precheck(path)
    if why:
        raise ArchiveError(why)
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise ArchiveError("not a zip archive") from None
    except (OSError, ValueError, NotImplementedError) as e:
        raise ArchiveError(f"unreadable zip ({e})") from None
    with zf:
        return _extract_open(zf, dest_dir, on_file)


def _extract_open(zf: zipfile.ZipFile, dest_dir: Path, on_file: OnFile | None) -> list[ExtractedFile]:
    infos = zf.infolist()
    check = _check_infos(infos)
    if not check.ok:
        raise ArchiveError(check.reason)
    parent = dest_dir.parent
    if is_link_like(parent) or not parent.is_dir():
        raise ArchiveError(f"{parent} is not a plain folder")
    try:
        free = shutil.disk_usage(parent).free
    except OSError:
        free = None
    if free is not None and check.total_uncompressed + FREE_SPACE_MARGIN > free:
        raise ArchiveError("not enough free disk space to unpack")
    try:
        os.mkdir(dest_dir)  # exclusive: fails if anything already has that name
    except FileExistsError:
        raise ArchiveError(f"{dest_dir} already exists; refusing to extract into it") from None
    except OSError as e:
        raise ArchiveError(f"cannot create {dest_dir}: {e.strerror or e}") from None

    done: list[ExtractedFile] = []
    try:
        for info, (rel, is_dir) in zip(infos, check.members):
            if is_dir:
                _mkdirs(dest_dir, Path(rel))
                continue
            _mkdirs(dest_dir, Path(rel).parent)
            target = resolve_inside(dest_dir, rel)
            h = hashlib.sha256()
            written = 0
            with zf.open(info) as src, open(target, "xb") as out:
                try:
                    while True:
                        chunk = src.read(CHUNK)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > info.file_size:
                            raise ArchiveError(f"{rel} holds more data than it declares", done)
                        h.update(chunk)
                        out.write(chunk)
                    if written != info.file_size:
                        raise ArchiveError(f"{rel} is shorter than it declares", done)
                except BaseException:
                    out.close()
                    os.unlink(target)  # our own half-written file, created with 'xb' above
                    raise
            mtime = _zip_mtime(info)
            if mtime is not None:
                try:
                    os.utime(target, (mtime, mtime))
                except OSError:
                    pass
            ef = ExtractedFile(rel, written, h.hexdigest())
            done.append(ef)
            if on_file:
                on_file(ef.rel, ef.size, ef.sha256)
    except ArchiveError as e:
        e.extracted = list(done)
        raise
    except Exception as e:
        # Corrupt deflate/lzma/bzip2 data, LargeZipFile, a failing on_file
        # callback...: whatever it is, the caller must still learn what was
        # written so it can journal or undo it.
        msg = e.strerror if isinstance(e, OSError) and e.strerror else (str(e) or type(e).__name__)
        raise ArchiveError(f"extraction stopped: {msg}", done) from e
    return done


def remove_extracted(dest_dir: Path, files: list[ExtractedFile]) -> tuple[int, list[str]]:
    """Undo an extraction: delete files still identical to what was written,
    then remove folders left empty. Anything the user changed is kept."""
    dest_dir = Path(dest_dir)
    skipped: list[str] = []
    if is_link_like(dest_dir) or not dest_dir.is_dir():
        return 0, [f"{dest_dir}: not a plain folder; nothing removed"]
    removed = 0
    for ef in reversed(files):
        try:
            target = resolve_inside(dest_dir, ef.rel)
        except UnsafePath as e:
            skipped.append(f"{ef.rel}: {e}")
            continue
        try:
            st = os.lstat(target)
        except FileNotFoundError:
            skipped.append(f"{ef.rel}: already gone")
            continue
        except OSError as e:
            skipped.append(f"{ef.rel}: {e.strerror or e}")
            continue
        if not stat.S_ISREG(st.st_mode) or is_link_like(target):
            skipped.append(f"{ef.rel}: not a regular file")
            continue
        try:
            if st.st_size != ef.size or _sha256(target) != ef.sha256:
                skipped.append(f"{ef.rel}: changed since extraction; kept")
                continue
            os.unlink(target)
            removed += 1
        except OSError as e:
            skipped.append(f"{ef.rel}: {e.strerror or e}")
    # Bottom-up so children go before parents; rmdir refuses non-empty folders.
    for root, subdirs, _ in os.walk(dest_dir, topdown=False, followlinks=False):
        for d in subdirs:
            p = Path(root) / d
            if not is_link_like(p):
                try:
                    os.rmdir(p)
                except OSError:
                    pass
    try:
        os.rmdir(dest_dir)
    except OSError:
        pass
    return removed, skipped
