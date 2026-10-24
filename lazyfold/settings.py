"""User settings for the automatic ("be lazier") features.

Stored as JSON in Lazyfold's own state folder. The file is user-editable, so
it is treated as untrusted input: unknown keys are dropped, values of the
wrong type fall back to the default, and category names must be real
categories. Saving validates strictly and refuses bad values instead of
quietly storing something the user did not mean.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

from .journal import state_dir
from .rules import CATEGORIES, DOC_CATEGORIES

# Documents are deliberately absent: what a document is (and what to call it)
# is the part most worth a human glance. Users may add them explicitly.
DEFAULTS: dict[str, Any] = {
    "auto_approve_categories": ["installers", "archives", "screenshots", "images", "videos", "audio",
                                "fonts", "torrents", "ebooks", "duplicates"],
    "auto_min_confidence": 0.9,
    # The automatic tidy also waits when the approval predictor (TabPFN) puts
    # the chance this user accepts a move below this. It can only hold moves
    # back, never add one.
    "auto_min_accept_prob": 0.85,
    "photos_by_date": True,
    "unpack_zip_archives": False,
    "recycle_old_installers_days": 90,
    "near_duplicates": True,
    "ocr": True,
    "notify_min_files": 5,
    "use_model": True,
    "use_predictor": True,
    "ollama_path": None,
    "theme": "system",  # system | light | dark
    "default_folder": None,  # folder pre-filled in the app (None = Downloads)
}
THEMES = ("system", "light", "dark")


def settings_path() -> Path:
    return state_dir() / "settings.json"


def defaults() -> dict[str, Any]:
    return copy.deepcopy(DEFAULTS)


def _check(key: str, value: Any) -> Any:
    """Return the validated value for `key` or raise ValueError."""
    if key == "auto_approve_categories":
        if not isinstance(value, list) or not all(isinstance(c, str) for c in value):
            raise ValueError("auto_approve_categories must be a list of category names")
        unknown = [c for c in value if c not in CATEGORIES]
        if unknown:
            raise ValueError(f"unknown categor{'y' if len(unknown) == 1 else 'ies'}: {', '.join(unknown)}")
        return list(dict.fromkeys(value))  # keep order, drop repeats
    if key == "auto_min_confidence":
        # bool is an int subclass; True must not silently mean 1.0.
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 <= value <= 1.0:
            raise ValueError("auto_min_confidence must be a number between 0 and 1")
        return float(value)
    if key == "auto_min_accept_prob":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.5 <= value <= 0.99:
            raise ValueError("auto_min_accept_prob must be a number between 0.5 and 0.99")
        return float(value)
    if key in ("recycle_old_installers_days", "notify_min_files"):
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100000:
            raise ValueError(f"{key} must be a whole number of 0 or more (0 turns it off)")
        return value
    if key == "theme":
        if value not in THEMES:
            raise ValueError("theme must be system, light or dark")
        return value
    if key == "default_folder":
        if value is None or value == "":
            return None
        if not isinstance(value, str) or "\x00" in value or len(value) > 1000:
            raise ValueError("default_folder must be a folder path or empty")
        return value.strip()
    if key == "ollama_path":
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise ValueError("ollama_path must be a path or null")
        return value.strip()
    if isinstance(DEFAULTS[key], bool):
        if not isinstance(value, bool):
            raise ValueError(f"{key} must be true or false")
        return value
    raise ValueError(f"unknown setting {key!r}")


def validate(data: Any, *, strict: bool = False) -> dict[str, Any]:
    """Fill defaults, drop unknown keys. Bad values raise when `strict`,
    otherwise they fall back to the default (a hand-edited typo must not stop
    Lazyfold from starting)."""
    out = defaults()
    if not isinstance(data, dict):
        if strict:
            raise ValueError("settings must be a JSON object")
        return out
    for key in DEFAULTS:
        if key not in data:
            continue
        try:
            out[key] = _check(key, data[key])
        except ValueError:
            if strict:
                raise
    return out


def load_settings() -> dict[str, Any]:
    try:
        raw = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return defaults()
    return validate(raw)


def save_settings(data: dict[str, Any]) -> dict[str, Any]:
    """Validate strictly and write atomically; returns what was stored.
    Keys missing from `data` keep their current saved value."""
    if not isinstance(data, dict):
        raise ValueError("settings must be a dictionary")
    merged = {**load_settings(), **{k: v for k, v in data.items() if k in DEFAULTS}}
    clean = validate(merged, strict=True)
    p = settings_path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(clean, indent=1), encoding="utf-8")
    os.replace(tmp, p)  # our own state file, not a user file
    return clean


def auto_approves_documents(settings: dict[str, Any]) -> bool:
    """True if the user opted in to filing documents without review (the UI
    should say so plainly)."""
    return any(c in DOC_CATEGORIES for c in settings.get("auto_approve_categories", []))
