"""First start after the rename from SlothFile: the old state folder is copied over."""

import sys
from pathlib import Path

import pytest

from lazyfold import journal


@pytest.fixture
def mac_home(tmp_path, monkeypatch):
    """macOS paths under a throwaway home (works on any OS)."""
    monkeypatch.delenv("LAZYFOLD_HOME", raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path / "Library" / "Application Support"


def _old_state(base):
    old = base / "SlothFile"
    (old / "runs").mkdir(parents=True)
    (old / "reviews.jsonl").write_text('{"label": 1}\n', encoding="utf-8")
    (old / "runs" / "r1.jsonl").write_text("{}\n", encoding="utf-8")
    return old


def test_old_state_is_copied_once_and_kept(mac_home):
    old = _old_state(mac_home)
    new = journal.state_dir()
    assert new == mac_home / "Lazyfold"
    assert (new / "reviews.jsonl").read_text(encoding="utf-8") == '{"label": 1}\n'
    assert (new / "runs" / "r1.jsonl").is_file()
    assert (old / "reviews.jsonl").is_file()  # the old folder is left alone
    (old / "reviews.jsonl").write_text("changed later\n", encoding="utf-8")
    journal.state_dir()  # a second start never copies again
    assert (new / "reviews.jsonl").read_text(encoding="utf-8") == '{"label": 1}\n'


def test_no_old_state_means_a_fresh_folder(mac_home):
    new = journal.state_dir()
    assert new.is_dir() and not any(new.iterdir())


def test_explicit_home_never_migrates(tmp_path, monkeypatch):
    monkeypatch.setenv("LAZYFOLD_HOME", str(tmp_path / "custom"))
    assert journal.legacy_state_path() is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows folder layout")
def test_windows_old_state_is_copied(tmp_path, monkeypatch):
    monkeypatch.delenv("LAZYFOLD_HOME", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    _old_state(tmp_path)
    new = journal.state_dir()
    assert new == tmp_path / "Lazyfold" and (new / "reviews.jsonl").is_file()
