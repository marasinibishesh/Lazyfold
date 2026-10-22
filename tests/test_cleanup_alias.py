"""Alias names (NTFS stream suffix, 8.3 short name) must never reach a protected file."""

import ctypes
import os
import sys
import time

import pytest

from slothfile import winshell
from slothfile.pathsafe import UnsafePath

WINDOWS = sys.platform == "win32"


def _short(path: str) -> str:
    buf = ctypes.create_unicode_buffer(1024)
    ctypes.windll.kernel32.GetShortPathNameW(path, buf, 1024)
    return buf.value


def test_stream_suffix_is_alias(tmp_path):
    f = tmp_path / "~report.pdf"
    f.write_text("PERSONAL")
    assert winshell.is_alias_path(str(f) + "::$DATA")
    with pytest.raises(UnsafePath):
        winshell.delete_permanently(str(f) + "::$DATA")
    assert f.read_text() == "PERSONAL"


@pytest.mark.skipif(not WINDOWS, reason="8.3 names are Windows-specific")
def test_short_name_is_alias(tmp_path):
    f = tmp_path / "~photograph.jpeg"
    f.write_text("PERSONAL")
    short_dir = _short(str(tmp_path))
    short = _short(str(f))
    if not short or os.path.basename(short).lower() == f.name.lower():
        pytest.skip("8.3 names disabled on this volume")
    # Long folder + short file name, as a tampered report would list it.
    tampered = os.path.join(os.path.realpath(tmp_path), os.path.basename(short))
    assert winshell.is_alias_path(tampered)
    with pytest.raises(UnsafePath):
        winshell.delete_permanently(tampered)
    assert f.read_text() == "PERSONAL"
    assert short_dir  # the folder lookup itself worked


def test_normal_long_path_is_not_alias(tmp_path):
    f = tmp_path / "~x.tmp"
    f.write_text("x")
    assert not winshell.is_alias_path(os.path.realpath(f))


def test_cleanup_revalidate_refuses_stream_alias(tmp_path, monkeypatch):
    from slothfile import cleanup
    root = tmp_path / "Temp"
    root.mkdir()
    keep = root / "~report.pdf"
    keep.write_text("PERSONAL DATA")
    old = time.time() - 3 * 86400
    os.utime(keep, (old, old))
    locs = cleanup.temp_locations(roots=[root])
    monkeypatch.setattr(winshell, "running_processes", lambda: set())
    rep = cleanup.scan_cleanup(locations=locs)
    st = os.stat(keep)
    # Forge a candidate pointing at the protected file through its stream alias.
    cat = next(c for c in rep.categories if c.key == "temp_files")
    cat.files.append(cleanup.Candidate(path=str(keep) + "::$DATA", size=st.st_size, mtime_ns=st.st_mtime_ns,
                                       root=cat.roots[0]))
    cleanup.save_report(rep)
    res = cleanup.clean(rep.report_id, ["temp_files"], mode="delete", confirm_permanent=True, locations=locs)
    assert keep.read_text() == "PERSONAL DATA"
    assert res.removed_count == 0
