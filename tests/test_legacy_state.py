"""First start after the rename from SlothFile: the old state folder is copied over."""

import os
import sys

from lazyfold import journal


def _windows_home(tmp_path, monkeypatch):
    monkeypatch.delenv("LAZYFOLD_HOME", raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))


def test_old_state_is_copied_once_and_kept(tmp_path, monkeypatch):
    _windows_home(tmp_path, monkeypatch)
    old = tmp_path / "SlothFile"
    (old / "runs").mkdir(parents=True)
    (old / "reviews.jsonl").write_text('{"label": 1}\n', encoding="utf-8")
    (old / "runs" / "r1.jsonl").write_text("{}\n", encoding="utf-8")
    new = journal.state_dir()
    assert new == tmp_path / "Lazyfold"
    assert (new / "reviews.jsonl").read_text(encoding="utf-8") == '{"label": 1}\n'
    assert (new / "runs" / "r1.jsonl").is_file()
    assert (old / "reviews.jsonl").is_file()  # the old folder is left alone
    (old / "reviews.jsonl").write_text("changed later\n", encoding="utf-8")
    journal.state_dir()  # a second start never copies again
    assert (new / "reviews.jsonl").read_text(encoding="utf-8") == '{"label": 1}\n'


def test_no_old_state_means_a_fresh_folder(tmp_path, monkeypatch):
    _windows_home(tmp_path, monkeypatch)
    new = journal.state_dir()
    assert new.is_dir() and not any(new.iterdir())


def test_explicit_home_never_migrates(tmp_path, monkeypatch):
    monkeypatch.setenv("LAZYFOLD_HOME", str(tmp_path / "custom"))
    assert journal.legacy_state_path() is None
