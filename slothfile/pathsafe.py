"""Path validation: keep every operation inside the selected folder and produce
filenames that are legal on Windows (the strictest common target)."""

from __future__ import annotations

import os
import re
import stat
import unicodedata
from pathlib import Path

WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
    "COM¹", "COM²", "COM³", "LPT¹", "LPT²", "LPT³", "CONIN$", "CONOUT$",
}
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
MAX_NAME = 120  # keep names readable and well below the 255-char component limit
MAX_PATH = 240  # stay under the classic 260-char MAX_PATH without long-path opt-in


class UnsafePath(Exception):
    """Raised when a path would escape the root or is otherwise unsafe."""


def is_link_like(p: Path) -> bool:
    """True for symlinks, junctions and other reparse points (never followed)."""
    try:
        st = os.lstat(p)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    attrs = getattr(st, "st_file_attributes", 0)
    if attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        return True
    is_junction = getattr(os.path, "isjunction", None)
    return bool(is_junction and is_junction(p))


def is_within(root: Path, candidate: Path) -> bool:
    root_s = os.path.normcase(os.path.abspath(root))
    cand_s = os.path.normcase(os.path.abspath(candidate))
    try:
        return os.path.commonpath([root_s, cand_s]) == root_s and cand_s != root_s
    except ValueError:  # different drives on Windows
        return False


def resolve_inside(root: Path, rel: str) -> Path:
    """Turn a plan-relative path into an absolute one, refusing anything that
    escapes `root` lexically or through a link/junction component."""
    if not rel or os.path.isabs(rel) or Path(rel).drive:
        raise UnsafePath(f"not a relative path: {rel!r}")
    parts = Path(rel).parts
    if any(part in ("..", ".") for part in parts):
        raise UnsafePath(f"path traversal component in {rel!r}")
    full = Path(os.path.abspath(root / rel))
    if not is_within(root, full):
        raise UnsafePath(f"{rel!r} escapes the selected folder")
    # No component between root and target (inclusive) may be a link/junction.
    cur = Path(os.path.abspath(root))
    for part in parts:
        cur = cur / part
        if is_link_like(cur):
            raise UnsafePath(f"{cur} is a link or junction; refusing to follow it")
    return full


def sanitize_component(name: str, fallback: str = "file") -> str:
    """Make a single path component safe for Windows and readable."""
    name = unicodedata.normalize("NFC", name)
    # Invisible and direction-changing characters (U+202E etc.) would make a
    # name display differently from what it is: replace them.
    name = "".join(" " if unicodedata.category(c) in ("Cc", "Cf", "Co", "Cs", "Zl", "Zp") else c for c in name)
    name = _ILLEGAL.sub(" ", name)
    name = re.sub(r"\s+", " ", name).strip().rstrip(". ")
    if not name:
        name = fallback
    # Windows treats "CON.txt" and "con .tar.gz" as the device too.
    if name.split(".")[0].strip().upper() in WINDOWS_RESERVED:
        name = "_" + name
    if len(name) > MAX_NAME:
        stem, ext = os.path.splitext(name)
        if len(ext) > 10:
            stem, ext = name, ""
        name = stem[: MAX_NAME - len(ext)].rstrip(". ") + ext
    return name


def sanitize_stem(stem: str) -> str:
    """Clean a model- or rule-suggested filename stem (no extension)."""
    # NFKC folds look-alike dots (U+FF0E, U+FE52...) to "." before we drop dots.
    stem = unicodedata.normalize("NFKC", stem)
    stem = re.sub(r"[.\u2024\u3002\uff61]", " ", stem)
    stem = sanitize_component(stem, fallback="")
    return stem[:80].strip()


def norm_key(rel: str) -> str:
    """Key for collision detection: Windows and macOS are case-insensitive."""
    return unicodedata.normalize("NFC", rel).casefold()
