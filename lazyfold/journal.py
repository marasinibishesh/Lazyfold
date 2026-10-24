"""Append-only, human-readable audit journal (one JSON object per line).

Each line is flushed and fsync'ed before the operation it describes runs, so
after a crash the journal always says what *might* have happened and recovery
can check the disk to find out what *did*. A torn final line is ignored.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator


def state_path() -> Path:
    """Where Lazyfold keeps its state, without creating anything."""
    env = os.environ.get("LAZYFOLD_HOME")
    if env:
        return Path(env)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Lazyfold"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Lazyfold"
    return Path.home() / ".local" / "state" / "lazyfold"


def legacy_state_path() -> Path | None:
    """Where the app kept its state under its previous name (SlothFile), or
    None when an explicit LAZYFOLD_HOME is in use."""
    if os.environ.get("LAZYFOLD_HOME"):
        return None
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "SlothFile"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "SlothFile"
    return Path.home() / ".local" / "state" / "slothfile"


def _adopt_legacy_state(base: Path) -> None:
    """First start after the rename: copy the old state folder (review
    history, undo journals, settings, learned rules) so nothing is lost. The
    old folder is left untouched. Any failure just means a fresh start."""
    old = legacy_state_path()
    if old is None or base.exists() or not old.is_dir() or old.is_symlink():
        return
    import shutil
    tmp = base.with_name(base.name + ".migrating")
    try:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.copytree(old, tmp, symlinks=True)
        os.rename(tmp, base)  # appears complete or not at all
    except OSError:
        shutil.rmtree(tmp, ignore_errors=True)


def state_dir() -> Path:
    base = state_path()
    _adopt_legacy_state(base)
    base.mkdir(parents=True, exist_ok=True)
    return base


def runs_dir() -> Path:
    d = state_dir() / "runs"
    d.mkdir(exist_ok=True)
    return d


def plans_dir() -> Path:
    d = state_dir() / "plans"
    d.mkdir(exist_ok=True)
    return d


def now_iso() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def heal(path: Path) -> None:
    """Cut a torn or garbage tail (crash mid-write, NULs after power loss) back
    to the last complete, valid line, so records appended later are readable."""
    with open(path, "rb+") as f:
        data = f.read()
        good = 0
        pos = 0
        while True:
            nl = data.find(b"\n", pos)
            if nl < 0:
                break
            line = data[pos:nl].strip()
            if line:
                try:
                    json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    break
            good = nl + 1
            pos = nl + 1
        if good != len(data):
            f.truncate(good)
            f.flush()
            os.fsync(f.fileno())


class Journal:
    def __init__(self, path: Path, *, new: bool = False):
        self.path = path
        if new:
            self._f = open(path, "x", encoding="utf-8")  # never append to another run's journal
        else:
            heal(path)
            self._f = open(path, "a", encoding="utf-8")

    def write(self, record: dict[str, Any]) -> None:
        record = {"ts": now_iso(), **record}
        self._f.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._f.flush()
        os.fsync(self._f.fileno())

    def close(self) -> None:
        self._f.close()

    def __enter__(self) -> "Journal":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def read_journal(path: Path) -> list[dict[str, Any]]:
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                break  # torn write at crash time; everything after is untrustworthy
    return out


def iter_runs() -> Iterator[tuple[str, list[dict[str, Any]]]]:
    for p in sorted(runs_dir().glob("*.jsonl"), reverse=True):
        yield p.stem, read_journal(p)
