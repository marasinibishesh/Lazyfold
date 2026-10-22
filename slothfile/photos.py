"""Photos by date: Images/<year>/<month> from the date the camera recorded.

Only the EXIF capture date is trusted. A download's mtime is when it arrived,
not when the picture was taken, so a photo without EXIF stays where the normal
rules put it instead of being filed under a misleading month."""

from __future__ import annotations

import re
from pathlib import Path

from .models import FileRecord

MIN_YEAR, MAX_YEAR = 1990, 2100
# ASCII digits only (\d also matches other scripts' digits) and \Z, not $,
# which would accept a trailing newline.
_TAKEN = re.compile(r"\A\s*([0-9]{4})[:\-/]([0-9]{2})[:\-/]([0-9]{2})")
_YEAR = re.compile(r"\A[0-9]{4}\Z")
_MONTH = re.compile(r"\A[0-9]{2}\Z")


def photo_date(rec: FileRecord) -> tuple[int, int] | None:
    """(year, month) from rec.meta["taken"] ("YYYY:MM:DD HH:MM:SS"), else None."""
    taken = rec.meta.get("taken") if isinstance(rec.meta, dict) else None
    if not isinstance(taken, str):
        return None
    m = _TAKEN.match(taken)
    if not m:
        return None
    year, month, day = (int(g) for g in m.groups())
    # Cameras with an unset clock write 0000:00:00 or 1970/1980 defaults.
    if not (MIN_YEAR <= year <= MAX_YEAR and 1 <= month <= 12 and 1 <= day <= 31):
        return None
    return year, month


def dated_subfolder(year: int, month: int) -> str:
    return str(Path("Images", f"{year:04d}", f"{month:02d}"))


def is_dated_image_folder(rel: str) -> bool:
    """True only for exactly Images/YYYY/MM (any case, either separator), so the
    executor's destination allowlist can accept these folders and nothing else."""
    if not isinstance(rel, str) or not rel:
        return False
    parts = rel.replace("\\", "/").split("/")
    if len(parts) != 3 or parts[0].casefold() != "images":
        return False
    year, month = parts[1], parts[2]
    if not (_YEAR.match(year) and _MONTH.match(month)):
        return False
    return MIN_YEAR <= int(year) <= MAX_YEAR and 1 <= int(month) <= 12
