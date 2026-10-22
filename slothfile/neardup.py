"""Near-duplicate detection: files that are not byte-identical but are the same
thing for a person (a re-saved photo, a PDF downloaded twice with different
metadata). Exact duplicates are found by sha256 in the scanner/planner; this
module only reports the fuzzy cases, and only ever suggests, never deletes."""

from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from pathlib import Path
from typing import NamedTuple

from .models import FileRecord
from .pathsafe import UnsafePath, is_link_like, resolve_inside

IMAGE_KINDS = {"png", "jpeg", "gif", "bmp", "webp"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
MAX_IMAGES = 2000  # pairwise comparison is O(n^2); 2000 -> ~2M cheap int ops
MIN_TEXT_CHARS = 200  # shorter texts ("Invoice", a cover page) collide too easily
MAX_PIXELS = 100_000_000  # same decompression-bomb guard as extract.extract_image

# Same idea as the planner's copy marker; kept local so this module does not
# depend on a private name in another module.
_COPY_MARK = re.compile(r"( - copy( \(\d+\))?| \(\d+\)|_copy\d*|-copy\d*)$|^copy of ", re.I)


MIN_SIDE = 32  # icons and spacer GIFs: too few pixels for a meaningful comparison
MIN_HASH_BITS = 4  # a hash with <= 4 (or >= size*size - 4) set bits is a flat image
MAX_MEAN_DIFF = 24  # per RGB channel, 0..255: a re-save barely shifts the colours
MAX_ASPECT_DIFF = 0.10  # a resized copy keeps its shape


class _Signature(NamedTuple):
    hash: int
    bits: int  # size * size
    mean: tuple[float, float, float]  # mean R, G, B of the thumbnail
    aspect: float  # width / height of the original image
    width: int
    height: int


def _signature(path: Path, size: int = 8) -> _Signature | None:
    """dhash plus the cheap facts used to reject accidental hash matches, all
    computed from the one small thumbnail."""
    try:
        from PIL import Image  # optional dependency
    except ImportError:
        return None
    if Image.MAX_IMAGE_PIXELS is None or Image.MAX_IMAGE_PIXELS > MAX_PIXELS:
        Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        with Image.open(path) as im:
            width, height = im.size
            if width <= 0 or height <= 0:
                return None
            im.draft("RGB", (size * 8, size * 8))  # JPEG: decode at reduced scale (cheap)
            small = im.convert("RGB").resize((size + 1, size), Image.Resampling.LANCZOS)
            px = small.convert("L").tobytes()  # mode "L": one byte per pixel, row-major
            rgb = small.tobytes()
    except Exception:  # unreadable, truncated, bomb guard tripped, ...
        return None
    bits = 0
    for row in range(size):
        base = row * (size + 1)
        for col in range(size):
            bits = (bits << 1) | (px[base + col] > px[base + col + 1])
    n = len(rgb) // 3
    mean = tuple(sum(rgb[c::3]) / n for c in range(3))
    return _Signature(bits, size * size, mean, width / height, width, height)  # type: ignore[arg-type]


def dhash(path: Path, size: int = 8) -> int | None:
    """Difference hash: compare each pixel with its right neighbour on a tiny
    grayscale thumbnail. Robust to resizing, recompression and small brightness
    changes, which is exactly what a re-saved photo goes through."""
    sig = _signature(path, size)
    return None if sig is None else sig.hash


def _informative(sig: _Signature) -> bool:
    """Flat or smooth images (a solid colour, a gradient) hash to (nearly) all
    zeros or all ones, so any two of them would look "the same"."""
    if sig.width < MIN_SIDE or sig.height < MIN_SIDE:
        return False
    ones = sig.hash.bit_count()
    return MIN_HASH_BITS < ones < sig.bits - MIN_HASH_BITS


def _compatible(a: _Signature, b: _Signature) -> bool:
    if any(abs(x - y) > MAX_MEAN_DIFF for x, y in zip(a.mean, b.mean)):
        return False
    return abs(a.aspect / b.aspect - 1) <= MAX_ASPECT_DIFF


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def _normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    text = "".join(c if c.isalnum() or c.isspace() else " " for c in text)
    return re.sub(r"\s+", " ", text).strip()


def text_fingerprint(text: str) -> str | None:
    """sha256 of the normalized text, or None when there is too little text for
    "same text" to mean "same document"."""
    norm = _normalize_text(text or "")
    if len(norm) < MIN_TEXT_CHARS:
        return None
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def _is_image(rec: FileRecord) -> bool:
    if rec.sniffed:
        return rec.sniffed in IMAGE_KINDS
    return rec.ext in IMAGE_EXTS


def _pixels(rec: FileRecord) -> int:
    w, h = rec.meta.get("width"), rec.meta.get("height")
    if isinstance(w, int) and isinstance(h, int) and w > 0 and h > 0:
        return w * h
    return 0


def _exact_same(a: FileRecord, b: FileRecord) -> bool:
    return a.sha256 is not None and a.sha256 == b.sha256


def _copyish(rec: FileRecord) -> bool:
    return bool(_COPY_MARK.search(os.path.splitext(rec.name)[0]))


def _safe_path(root: Path, rec: FileRecord) -> Path | None:
    try:
        p = resolve_inside(root, rec.rel_path)
    except UnsafePath:
        return None
    return None if is_link_like(p) else p


def find_near_duplicates(root: Path, records: list[FileRecord], *,
                         max_distance: int = 4) -> dict[str, tuple[str, str]]:
    """Map rel_path of each near-copy -> (rel_path of the kept original, reason)."""
    usable = [r for r in records if not r.skip_reason and not r.ext_mismatch]
    out: dict[str, tuple[str, str]] = {}
    _images(root, [r for r in usable if _is_image(r)], max_distance, out)
    _documents([r for r in usable if not _is_image(r) and r.text], out)
    return out


def _images(root: Path, recs: list[FileRecord], max_distance: int,
            out: dict[str, tuple[str, str]]) -> None:
    hashed: list[tuple[FileRecord, _Signature]] = []
    for rec in recs:
        if len(hashed) >= MAX_IMAGES:
            break
        p = _safe_path(root, rec)
        sig = _signature(p) if p is not None else None
        if sig is not None and _informative(sig):
            hashed.append((rec, sig))
    # Best version first (most pixels, then biggest file, then oldest), so each
    # later image is compared against the originals already chosen and the
    # greedy grouping keeps the highest-quality copy.
    hashed.sort(key=lambda t: (-_pixels(t[0]), -t[0].size, t[0].mtime_ns, t[0].rel_path))
    kept: list[tuple[FileRecord, _Signature]] = []
    for rec, sig in hashed:
        if any(_exact_same(rec, orig) for orig, _ in kept):
            continue  # exact duplicate: the sha256 pass reports it, not us
        match = next(((orig, d) for orig, osig in kept
                      if (d := hamming(sig.hash, osig.hash)) <= max_distance
                      and _compatible(sig, osig)), None)
        if match is None:
            kept.append((rec, sig))
            continue
        orig, d = match
        out[rec.rel_path] = (orig.rel_path,
                             f"looks like the same picture as {orig.name} (perceptual distance {d})")


def _documents(recs: list[FileRecord], out: dict[str, tuple[str, str]]) -> None:
    groups: dict[str, list[FileRecord]] = {}
    for rec in recs:
        fp = text_fingerprint(rec.text)
        if fp:
            groups.setdefault(fp, []).append(rec)
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda r: (_copyish(r), r.meta.get("created_ns", r.mtime_ns),
                                    len(r.name), r.rel_path))
        keeper = members[0]
        for rec in members[1:]:
            if _exact_same(rec, keeper) or rec.rel_path in out:
                continue
            out[rec.rel_path] = (keeper.rel_path,
                                 f"same text as {keeper.name} but different file bytes "
                                 "(re-downloaded or re-saved)")
