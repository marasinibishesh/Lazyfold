import hashlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from slothfile.demo import create_demo_folder  # noqa: E402


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """Journals and plans go to a throwaway state dir, never the real one."""
    h = tmp_path / "state"
    monkeypatch.setenv("SLOTHFILE_HOME", str(h))
    return h


@pytest.fixture
def demo(tmp_path):
    root = tmp_path / "Downloads"
    expected = create_demo_folder(root)
    return root, expected


def snapshot(root: Path) -> dict[str, str]:
    """Relative path -> sha256 of every file under root."""
    out = {}
    for p in root.rglob("*"):
        if p.is_file() and not p.is_symlink():
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def write_old(path: Path, data: bytes | str, days: int = 10) -> Path:
    """Create a file with an mtime in the past (fresh files are skipped as 'still downloading')."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode()
    path.write_bytes(data)
    import time
    t = time.time() - days * 86400
    os.utime(path, (t, t))
    return path
