"""macOS code paths, exercised on any OS: sys.platform, subprocess, libc (ctypes)
and pyobjc are all stubbed, so nothing here touches the real Trash or Finder."""

import ctypes
import os
import plistlib
import subprocess
import sys
import types
from pathlib import Path

import pytest

from conftest import snapshot, write_old
from slothfile import executor, installed, journal, macos, ocr, scanner, sniff, winshell
from slothfile.executor import apply_plan, undo_run
from slothfile.journal import read_journal
from slothfile.planner import PlanOptions, build_plan
from slothfile.scanner import scan_folder

PKG = b"xar!" + b"\x00\x1c\x00\x01" + b"\x00" * 300
DMG = b"\x78\xda" + b"\x11" * 1000 + b"koly" + b"\x00" * 508
MACHO = b"\xcf\xfa\xed\xfe" + b"\x07\x00\x00\x01" + b"\x00" * 200


@pytest.fixture(autouse=True)
def no_real_mac(monkeypatch):
    """Never load a real libc or pyobjc here, and never start a real process."""
    monkeypatch.setattr(macos, "_libc", lambda: None)
    for mod in ("Foundation", "Vision", "objc"):
        monkeypatch.setitem(sys.modules, mod, None)  # import raises ImportError

    def no_run(*a, **k):
        raise AssertionError("a real subprocess was started")
    monkeypatch.setattr(macos, "_run", no_run)
    with winshell._trashed_lock:
        winshell._trashed.clear()


@pytest.fixture
def mac(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")


def proc(rc=0, out=b"", err=b""):
    return subprocess.CompletedProcess([], rc, stdout=out, stderr=err)


@pytest.fixture
def fake_trash(tmp_path, monkeypatch):
    """macos.trash stub: moves the item into a private .Trash folder."""
    trash_dir = tmp_path / "home" / ".Trash"
    trash_dir.mkdir(parents=True)
    calls = []

    def trash(p):
        calls.append(str(p))
        dst = trash_dir / Path(p).name
        n = 2
        while dst.exists():
            dst = trash_dir / f"{Path(p).stem} {n}{Path(p).suffix}"
            n += 1
        os.rename(p, dst)
        return str(dst)

    monkeypatch.setattr(macos, "trash", trash)
    return trash_dir, calls


# --- Trash -------------------------------------------------------------------------

def test_trash_finder_fallback_returns_trash_path(tmp_path, monkeypatch, mac):
    f = tmp_path / "dl" / "a.dmg"
    write_old(f, DMG)
    trash_dir = tmp_path / "home" / ".Trash"
    trash_dir.mkdir(parents=True)
    seen = {}

    def run(cmd, timeout=30):
        seen["cmd"] = cmd
        target = trash_dir / "a.dmg"
        os.rename(cmd[-1], target)
        return proc(0, (str(target) + "\n").encode())

    monkeypatch.setattr(macos, "_run", run)
    assert winshell.recycle([f]) == {str(f): None}
    assert not f.exists()
    assert winshell.take_trash_path(f) == str(trash_dir / "a.dmg")
    assert winshell.take_trash_path(f) is None  # handed out once
    cmd = seen["cmd"]
    assert cmd[0] == "osascript" and cmd[-1] == os.path.abspath(f)
    assert all(str(f) not in part for part in cmd[:-1])  # the path is argv data, not script


def test_trash_failure_reports_and_keeps_file(tmp_path, monkeypatch, mac):
    f = write_old(tmp_path / "dl" / "a.pkg", PKG)
    monkeypatch.setattr(macos, "_run", lambda cmd, timeout=30: proc(1, b"", b"Finder got an error"))
    with pytest.raises(OSError):
        macos.trash(f)
    res = winshell.recycle([f])
    assert res[str(f)] and "Finder got an error" in res[str(f)]
    assert f.read_bytes() == PKG
    assert winshell.take_trash_path(f) is None


def test_trash_uses_foundation_when_available(tmp_path, monkeypatch, mac):
    f = write_old(tmp_path / "dl" / "a.pkg", PKG)
    trash_dir = tmp_path / "home" / ".Trash"
    trash_dir.mkdir(parents=True)

    class URL:
        def __init__(self, p):
            self.p = p

        def path(self):
            return self.p

    class Manager:
        def trashItemAtURL_resultingItemURL_error_(self, url, a, b):
            dst = str(trash_dir / Path(url.p).name)
            os.rename(url.p, dst)
            return True, URL(dst), None

    fake = types.SimpleNamespace(NSURL=types.SimpleNamespace(fileURLWithPath_=URL),
                                 NSFileManager=types.SimpleNamespace(defaultManager=Manager))
    monkeypatch.setitem(sys.modules, "Foundation", fake)
    assert macos.trash(f) == str(trash_dir / "a.pkg")
    assert not f.exists()


def test_recycle_refuses_folders_and_links_on_mac(tmp_path, fake_trash, mac):
    d = tmp_path / "dl" / "folder.app"
    d.mkdir(parents=True)
    assert winshell.recycle([d])[str(d)] == "not a regular file"
    assert d.exists() and not fake_trash[1]


def test_restore_never_overwrites(tmp_path):
    trash_dir = tmp_path / ".Trash"
    trash_dir.mkdir()
    trashed = trash_dir / "a.dmg"
    trashed.write_bytes(b"old")
    original = tmp_path / "dl" / "a.dmg"
    original.parent.mkdir()
    original.write_bytes(b"new user file")
    assert macos.restore_from_trash(trashed, original) is False
    assert original.read_bytes() == b"new user file" and trashed.read_bytes() == b"old"
    original.unlink()
    assert macos.restore_from_trash(trashed, original) is True
    assert original.read_bytes() == b"old" and not trashed.exists()
    assert macos.restore_from_trash(trashed, original) is False  # gone from the Trash


def test_restore_refuses_items_outside_the_trash(tmp_path):
    elsewhere = tmp_path / "Documents" / "taxes.pdf"
    elsewhere.parent.mkdir()
    elsewhere.write_bytes(b"%PDF-")
    target = tmp_path / "dl" / "taxes.pdf"
    target.parent.mkdir()
    assert macos.restore_from_trash(elsewhere, target) is False
    assert elsewhere.exists() and not target.exists()


def test_restore_refuses_missing_parent(tmp_path):
    trash_dir = tmp_path / ".Trash"
    trash_dir.mkdir()
    (trash_dir / "a").write_bytes(b"x")
    assert macos.restore_from_trash(trash_dir / "a", tmp_path / "gone" / "a") is False
    assert (trash_dir / "a").exists()


def test_rename_excl_unavailable_off_mac():
    assert macos.rename_excl("a", "b") is False


def test_trash_info_and_bin_info_order(tmp_path, monkeypatch, mac):
    trash_dir = tmp_path / ".Trash"
    (trash_dir / "sub").mkdir(parents=True)
    (trash_dir / "a").write_bytes(b"12345")
    (trash_dir / "sub" / "b").write_bytes(b"123")
    (trash_dir / ".DS_Store").write_bytes(b"x" * 50)
    monkeypatch.setattr(macos, "_trash_dir", lambda: trash_dir)
    assert macos.trash_info() == (2, 8)
    assert winshell.recycle_bin_info() == (8, 2)  # winshell keeps (bytes, items)
    monkeypatch.setattr(macos, "_trash_dir", lambda: tmp_path / "unreadable")
    assert macos.trash_info() == (0, 0)


def test_empty_trash(monkeypatch, mac):
    calls = []
    monkeypatch.setattr(macos, "_run", lambda cmd, timeout=30: calls.append(cmd) or proc(0))
    winshell.empty_recycle_bin()
    assert calls == [["osascript", "-e", 'tell application "Finder" to empty trash']]
    monkeypatch.setattr(macos, "_run", lambda cmd, timeout=30: proc(1, b"", b"nope"))
    with pytest.raises(OSError):
        winshell.empty_recycle_bin()


# --- notifications, processes, apps -------------------------------------------------

def test_notify_passes_text_as_argv(monkeypatch):
    calls = []
    monkeypatch.setattr(macos, "_run", lambda cmd, timeout=30: calls.append(cmd) or proc(0))
    title = 'Done"; do shell script "rm -rf ~" --'
    body = "moved 3 files\" & (do shell script \"id\")"
    assert macos.notify(title, body) is True
    cmd = calls[0]
    assert cmd[-2:] == [title, body]
    script = [cmd[i + 1] for i, c in enumerate(cmd[:-2]) if c == "-e"]
    assert script == ["on run argv",
                      "display notification (item 2 of argv) with title (item 1 of argv)", "end run"]
    assert not any("rm -rf" in s or "shell" in s for s in script)


def test_notify_failure_is_false(monkeypatch):
    def boom(cmd, timeout=30):
        raise OSError("no osascript")
    monkeypatch.setattr(macos, "_run", boom)
    assert macos.notify("t", "b") is False
    monkeypatch.setattr(macos, "_run", lambda cmd, timeout=30: proc(1))
    assert macos.notify("t", "b") is False


def test_running_processes(monkeypatch, mac):
    out = b"Google Chrome\n  firefox\nSafari\n\nBrave Browser\n"
    monkeypatch.setattr(macos, "_run", lambda cmd, timeout=30: proc(0, out))
    assert winshell.running_processes() == {"google chrome", "firefox", "safari", "brave browser"}
    monkeypatch.setattr(macos, "_run", lambda cmd, timeout=30: proc(1))
    with pytest.raises(OSError):
        winshell.running_processes()


def test_is_in_use_false_on_mac(tmp_path, mac):
    f = tmp_path / "x"
    f.write_bytes(b"x")
    assert winshell.is_in_use(f) is False


def test_installed_apps(tmp_path, monkeypatch, mac):
    apps = tmp_path / "Applications"
    (apps / "Firefox.app").mkdir(parents=True)
    (apps / "Google Chrome.app").mkdir()
    (apps / "Adobe Photoshop 2024" / "Adobe Photoshop 2024.app").mkdir(parents=True)
    (apps / "notes.txt").write_text("x")
    monkeypatch.setattr(macos, "_app_dirs", lambda: [apps, tmp_path / "missing"])
    found = macos.installed_apps()
    assert found == frozenset({"firefox", "google chrome", "adobe photoshop 2024"})
    installed.installed_programs.cache_clear()
    try:
        assert installed.installed_programs() == found
    finally:
        installed.installed_programs.cache_clear()
    assert installed.matching_program("Firefox-mac-universal.dmg", found) == "firefox"
    assert installed.matching_program("googlechrome.dmg", found) is None  # no word split: no guess
    assert installed.matching_program("GoogleChrome.dmg", found) == "google chrome"


def test_state_path_on_mac(monkeypatch, mac):
    monkeypatch.delenv("SLOTHFILE_HOME")
    assert journal.state_path() == Path.home() / "Library" / "Application Support" / "SlothFile"
    monkeypatch.setenv("SLOTHFILE_HOME", "/tmp/x")
    assert journal.state_path() == Path("/tmp/x")


# --- where-froms ------------------------------------------------------------------

class FakeLibc:
    """libc stand-in whose getxattr behaves like darwin's (6 arguments)."""

    def __init__(self, value):
        self.value = value
        self.calls = []
        outer = self

        class Fn:
            argtypes = restype = None

            def __call__(self, path, name, buf, size, position, options):
                outer.calls.append((path, name, size, position, options))
                if outer.value is None:
                    return -1
                if buf is None:
                    return len(outer.value)
                n = min(size, len(outer.value))
                ctypes.memmove(buf, outer.value, n)
                return n

        self.getxattr = Fn()


def test_where_froms_reads_binary_plist(tmp_path, monkeypatch, mac):
    f = tmp_path / "a.dmg"
    f.write_bytes(b"x")
    urls = ["https://dl.example.com/a.dmg", "https://example.com/download", 7, b"bytes"]
    lib = FakeLibc(plistlib.dumps(urls, fmt=plistlib.FMT_BINARY))
    monkeypatch.setattr(macos, "_libc", lambda: lib)
    assert macos.where_froms(f) == ["https://dl.example.com/a.dmg", "https://example.com/download"]
    path, name, _, position, options = lib.calls[-1]
    assert name == b"com.apple.metadata:kMDItemWhereFroms"
    assert position == 0 and options == macos.XATTR_NOFOLLOW
    assert scanner.read_motw_host(f) == "dl.example.com"


@pytest.mark.parametrize("raw", [b"garbage", b"bplist00\xff\xff", plistlib.dumps({"a": 1}, fmt=plistlib.FMT_BINARY),
                                 None])
def test_where_froms_garbage_is_empty(tmp_path, monkeypatch, mac, raw):
    f = tmp_path / "a"
    f.write_bytes(b"x")
    monkeypatch.setattr(macos, "_libc", lambda: FakeLibc(raw))
    assert macos.where_froms(f) == []
    assert scanner.read_motw_host(f) is None


def test_motw_host_skips_non_web_urls(tmp_path, monkeypatch, mac):
    f = tmp_path / "a"
    f.write_bytes(b"x")
    monkeypatch.setattr(macos, "where_froms", lambda p: ["file:///Users/x/a", "http://[bad", "https://Files.Example.org/x"])
    assert scanner.read_motw_host(f) == "files.example.org"


def test_where_froms_off_mac_is_empty(tmp_path):
    assert macos.where_froms(tmp_path) == []


# --- hidden files -------------------------------------------------------------------

def test_mac_hidden_flag_and_finder_files(mac):
    st = types.SimpleNamespace(st_flags=0x8000, st_file_attributes=0)
    plain = types.SimpleNamespace(st_flags=0, st_file_attributes=0)
    assert scanner._is_hidden_or_system(st, "report.pdf")
    assert not scanner._is_hidden_or_system(plain, "report.pdf")
    for name in ("Icon\r", "._report.pdf", ".DS_Store"):
        assert scanner._is_hidden_or_system(plain, name)


# --- installers and disguised executables -----------------------------------------

def test_dmg_and_pkg_detection(tmp_path):
    dmg = write_old(tmp_path / "Firefox 120.dmg", DMG)
    fake_dmg = write_old(tmp_path / "fake.dmg", b"\x00" * 2000)
    pkg = write_old(tmp_path / "Tool.pkg", PKG)
    mpkg = write_old(tmp_path / "Suite.mpkg", PKG)
    bad_pkg = write_old(tmp_path / "bad.pkg", b"PK\x03\x04" + b"\x00" * 100)
    zapp = write_old(tmp_path / "App.zip", b"PK\x03\x04" + b"\x00" * 100)
    assert sniff.is_dmg(dmg) and not sniff.is_dmg(fake_dmg)
    assert sniff.sniff_bytes(PKG) == "xar"
    assert executor._is_installer_file(dmg) and executor._is_installer_file(pkg) and executor._is_installer_file(mpkg)
    for p in (fake_dmg, bad_pkg, zapp):
        assert not executor._is_installer_file(p)


def test_scan_classifies_mac_installers(tmp_path):
    root = tmp_path / "dl"
    write_old(root / "Firefox 120.dmg", DMG)
    write_old(root / "Tool.pkg", PKG)
    write_old(root / "Suite.mpkg", PKG + b"suite")
    records, stats = scan_folder(root)
    by = {r.name: r for r in records}
    assert by["Firefox 120.dmg"].sniffed == "dmg" and not by["Firefox 120.dmg"].ext_mismatch
    assert by["Tool.pkg"].sniffed == "xar" and not by["Tool.pkg"].ext_mismatch
    plan = build_plan(root, records, scan_stats=stats, options=PlanOptions())
    cats = {p.rel_path: p.category for p in plan.proposals}
    assert cats == {"Firefox 120.dmg": "installers", "Tool.pkg": "installers", "Suite.mpkg": "installers"}


def test_macho_disguised_as_image_is_left_alone(tmp_path):
    root = tmp_path / "dl"
    write_old(root / "holiday.jpg", MACHO)
    write_old(root / "cat.png", b"\xca\xfe\xba\xbe\x00\x00\x00\x02" + b"\x00" * 100)  # universal binary
    write_old(root / "Main.class", b"\xca\xfe\xba\xbe\x00\x00\x00\x34" + b"\x00" * 100)  # Java, not Mach-O
    write_old(root / "tool", MACHO)
    records, _ = scan_folder(root)
    by = {r.name: r for r in records}
    for name in ("holiday.jpg", "cat.png"):
        assert by[name].sniffed == "macho" and by[name].ext_mismatch
        assert "Mach-O program" in by[name].skip_reason
    assert by["Main.class"].sniffed != "macho" and not by["Main.class"].skip_reason
    assert by["tool"].sniffed == "macho" and not by["tool"].skip_reason
    plan = build_plan(root, records, options=PlanOptions())
    acts = {p.rel_path: p.action for p in plan.proposals}
    assert acts.get("holiday.jpg") != "move" and acts.get("cat.png") != "move"


def test_disguised_windows_exe_still_flagged(tmp_path):
    root = tmp_path / "dl"
    write_old(root / "photo.jpg", b"MZ" + b"\x00" * 200)
    [rec], _ = scan_folder(root)
    assert rec.ext_mismatch and "EXE program" in rec.skip_reason


def test_mac_installers_suggested_for_recycle_only_on_mac(tmp_path, monkeypatch):
    root = tmp_path / "dl"
    write_old(root / "Firefox 120.dmg", DMG, days=200)
    records, stats = scan_folder(root)
    opts = dict(recycle_old_installers_days=90, installed=frozenset())
    plan = build_plan(root, records, scan_stats=stats, options=PlanOptions(**opts))
    assert plan.proposals[0].action == "move"  # Windows behaviour unchanged
    monkeypatch.setattr(sys, "platform", "darwin")
    plan = build_plan(root, records, scan_stats=stats, options=PlanOptions(**opts))
    assert plan.proposals[0].action == "recycle" and not plan.proposals[0].approved


# --- executor: atomic move, recycle journal and undo ----------------------------

def test_safe_move_uses_renamex_np_on_mac(tmp_path, monkeypatch, mac):
    calls = []

    def rename_excl(src, dst):
        calls.append((src, dst))
        if os.path.lexists(dst):
            raise FileExistsError(dst)
        os.rename(src, dst)
        return True

    monkeypatch.setattr(macos, "rename_excl", rename_excl)
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_bytes(b"1")
    executor.safe_move(a, b)
    assert calls and b.read_bytes() == b"1" and not a.exists()
    a.write_bytes(b"2")
    with pytest.raises(FileExistsError):
        executor.safe_move(a, b)
    assert a.read_bytes() == b"2" and b.read_bytes() == b"1"


def test_safe_move_falls_back_when_renamex_np_missing(tmp_path, monkeypatch, mac):
    monkeypatch.setattr(macos, "rename_excl", lambda s, d: False)
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_bytes(b"1")
    b.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        executor.safe_move(a, b)
    assert b.read_bytes() == b"keep"
    b.unlink()
    executor.safe_move(a, b)
    assert b.read_bytes() == b"1" and not a.exists()


def _recycle_plan(root):
    write_old(root / "Firefox 120.dmg", DMG, days=200)
    records, stats = scan_folder(root)
    sys_platform = sys.platform
    try:
        sys.platform = "darwin"
        plan = build_plan(root, records, scan_stats=stats,
                          options=PlanOptions(recycle_old_installers_days=90, installed=frozenset()))
    finally:
        sys.platform = sys_platform
    plan.proposals[0].approved = True
    assert plan.proposals[0].action == "recycle"
    return plan


def test_recycle_journals_trash_path_and_undo_restores_it(tmp_path, monkeypatch, fake_trash):
    trash_dir, calls = fake_trash
    root = tmp_path / "dl"
    plan = _recycle_plan(root)
    before = snapshot(root)
    monkeypatch.setattr(sys, "platform", "darwin")
    rep = apply_plan(plan, allow_recycle=True)
    assert rep.results[0].status == "recycled" and "Trash" in rep.results[0].detail
    assert not (root / "Firefox 120.dmg").exists() and (trash_dir / "Firefox 120.dmg").exists()
    recs = read_journal(executor._run_path(rep.run_id))
    [rec] = [r for r in recs if r["t"] == "recycled"]
    assert rec["trash_path"] == str(trash_dir / "Firefox 120.dmg")
    assert executor._ops_state(recs)[0]["trash_path"] == rec["trash_path"]

    restored_from = []
    real_restore = macos.restore_from_trash
    monkeypatch.setattr(macos, "restore_from_trash",
                        lambda t, o: restored_from.append(str(t)) or real_restore(t, o))
    undo = undo_run(rep.run_id)
    assert undo.results[0].status == "restored"
    assert restored_from == [rec["trash_path"]]
    assert snapshot(root) == before and not (trash_dir / "Firefox 120.dmg").exists()


def test_undo_refuses_changed_item_in_trash(tmp_path, monkeypatch, fake_trash):
    trash_dir, _ = fake_trash
    root = tmp_path / "dl"
    plan = _recycle_plan(root)
    monkeypatch.setattr(sys, "platform", "darwin")
    rep = apply_plan(plan, allow_recycle=True)
    trashed = trash_dir / "Firefox 120.dmg"
    trashed.write_bytes(b"something else")
    undo = undo_run(rep.run_id)
    assert undo.results[0].status == "failed" and "Put Back" in undo.results[0].detail
    assert trashed.read_bytes() == b"something else" and not (root / "Firefox 120.dmg").exists()


def test_undo_never_overwrites_new_file_with_same_name(tmp_path, monkeypatch, fake_trash):
    trash_dir, _ = fake_trash
    root = tmp_path / "dl"
    plan = _recycle_plan(root)
    monkeypatch.setattr(sys, "platform", "darwin")
    rep = apply_plan(plan, allow_recycle=True)
    (root / "Firefox 120.dmg").write_bytes(b"newer download")
    undo = undo_run(rep.run_id)
    assert undo.results[0].status == "skipped"
    assert (root / "Firefox 120.dmg").read_bytes() == b"newer download"
    assert (trash_dir / "Firefox 120.dmg").read_bytes() == DMG


def test_undo_without_trash_path_looks_up_name_and_verifies(tmp_path, monkeypatch, fake_trash):
    """Finder fallback gave no path: undo finds the name in ~/.Trash and checks it."""
    trash_dir, _ = fake_trash
    monkeypatch.setattr(macos, "trash", lambda p: (os.rename(p, trash_dir / Path(p).name), "")[1])
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: trash_dir.parent))
    root = tmp_path / "dl"
    plan = _recycle_plan(root)
    monkeypatch.setattr(sys, "platform", "darwin")
    rep = apply_plan(plan, allow_recycle=True)
    recs = read_journal(executor._run_path(rep.run_id))
    assert "trash_path" not in [r for r in recs if r["t"] == "recycled"][0]
    undo = undo_run(rep.run_id)
    assert undo.results[0].status == "restored"
    assert (root / "Firefox 120.dmg").read_bytes() == DMG


# --- OCR -----------------------------------------------------------------------------

def test_ocr_dispatches_to_vision_on_mac(tmp_path, monkeypatch, mac):
    img = tmp_path / "scan.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")
    monkeypatch.setattr(macos, "ocr_available", lambda: True)
    monkeypatch.setattr(macos, "ocr_image", lambda p: "Invoice 42\nTotal 10.00")
    ocr._cache_clear()
    try:
        assert ocr.ocr_available() is True
        assert ocr.ocr_images([img]) == {str(img): "Invoice 42\nTotal 10.00"}
    finally:
        ocr._cache_clear()


def test_vision_unavailable_without_pyobjc(tmp_path, mac):
    assert macos.ocr_available() is False
    assert macos.ocr_image(tmp_path / "x.png") == ""
