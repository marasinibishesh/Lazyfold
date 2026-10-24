"""Dangerous cases: every one of these must leave user files intact."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import snapshot, write_old
from lazyfold import executor
from lazyfold.demo import FAKE_EXE, make_pdf
from lazyfold.executor import NeedsRecovery, apply_plan, incomplete_runs, recover, undo_run
from lazyfold.models import Plan
from lazyfold.planner import build_plan
from lazyfold.scanner import scan_folder

WINDOWS = sys.platform == "win32"


def plan_for(root, **kw):
    records, stats = scan_folder(root)
    return build_plan(root, records, scan_stats=stats, **kw)


def by_name(plan, name):
    return next(p for p in plan.proposals if p.rel_path == name)


# --- read-only scanning -----------------------------------------------------------

def test_scan_and_plan_change_nothing(demo):
    root, _ = demo
    before = snapshot(root)
    mtimes = {p: p.stat().st_mtime_ns for p in root.rglob("*")}
    plan_for(root)
    assert snapshot(root) == before
    assert {p: p.stat().st_mtime_ns for p in root.rglob("*")} == mtimes


def test_subfolders_are_never_touched(demo):
    root, _ = demo
    plan = plan_for(root)
    assert all("Old stuff" not in p.rel_path for p in plan.proposals)
    report = apply_plan(plan)
    assert (root / "Old stuff" / "keep-me.txt").read_text() == "folders are never touched"
    assert report.count("failed") == 0


# --- full round trip --------------------------------------------------------------

def test_apply_then_undo_restores_folder_exactly(demo):
    root, _ = demo
    before = snapshot(root)
    plan = plan_for(root)
    report = apply_plan(plan)
    assert report.count("moved") > 30
    assert snapshot(root) != before
    undo = undo_run(report.run_id)
    assert undo.count("restored") == report.count("moved")
    assert snapshot(root) == before
    # Category folders created by the run are removed again.
    assert sorted(p.name for p in root.iterdir() if p.is_dir()) == ["Old stuff"]


def test_every_move_is_journaled_before_it_happens(demo):
    root, _ = demo
    report = apply_plan(plan_for(root))
    lines = [json.loads(l) for l in (executor.runs_dir() / f"{report.run_id}.jsonl").read_text("utf-8").splitlines()]
    order = [(r["t"], r.get("op")) for r in lines]
    for i in range(report.count("moved")):
        assert order.index(("intent", i)) < order.index(("done", i))
    assert lines[0]["t"] == "begin" and lines[-1]["t"] == "end"


def test_undo_is_idempotent(demo):
    root, _ = demo
    before = snapshot(root)
    report = apply_plan(plan_for(root))
    undo_run(report.run_id)
    second = undo_run(report.run_id)
    assert second.count("restored") == 0 and second.count("failed") == 0
    assert snapshot(root) == before


# --- never overwrite --------------------------------------------------------------

def test_destination_created_after_preview_is_not_overwritten(tmp_path):
    root = tmp_path / "dl"
    write_old(root / "setup.exe", FAKE_EXE)
    plan = plan_for(root)
    (root / "Installers").mkdir()
    (root / "Installers" / "setup.exe").write_bytes(b"precious")
    report = apply_plan(plan)
    assert report.results[0].status == "skipped"
    assert (root / "Installers" / "setup.exe").read_bytes() == b"precious"
    assert (root / "setup.exe").read_bytes() == FAKE_EXE


def test_existing_file_in_destination_gets_unique_name(tmp_path):
    root = tmp_path / "dl"
    write_old(root / "setup.exe", FAKE_EXE)
    write_old(root / "Installers" / "SETUP.EXE", b"older installer")  # case-insensitive clash
    plan = plan_for(root)
    p = by_name(plan, "setup.exe")
    assert p.dest_rel == str(Path("Installers") / "setup (2).exe")
    apply_plan(plan)
    assert (root / "Installers" / "SETUP.EXE").read_bytes() == b"older installer"
    assert (root / "Installers" / "setup (2).exe").read_bytes() == FAKE_EXE


def test_two_files_with_same_suggested_name_do_not_collide(tmp_path):
    root = tmp_path / "dl"
    text = ["Contoso Electricity", "Invoice", "Amount due: $10", "2026-09-20"]
    write_old(root / "download.pdf", make_pdf(text))
    write_old(root / "document.pdf", make_pdf(text + ["copy two"]))
    plan = plan_for(root)
    dests = [p.dest_rel for p in plan.proposals if p.action == "move"]
    assert len(dests) == 2 and len({d.casefold() for d in dests}) == 2
    report = apply_plan(plan)
    assert report.count("moved") == 2


def test_safe_move_refuses_existing_destination(tmp_path):
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("A")
    b.write_text("B")
    with pytest.raises(FileExistsError):
        executor.safe_move(a, b)
    assert a.read_text() == "A" and b.read_text() == "B"


# --- changed files ----------------------------------------------------------------

def test_file_changed_after_preview_is_skipped(tmp_path):
    root = tmp_path / "dl"
    f = write_old(root / "setup.exe", FAKE_EXE)
    plan = plan_for(root)
    f.write_bytes(FAKE_EXE + b"tampered")
    report = apply_plan(plan)
    assert report.results[0].status == "skipped"
    assert "changed" in report.results[0].detail
    assert f.exists()


def test_file_deleted_after_preview_is_skipped(tmp_path):
    root = tmp_path / "dl"
    f = write_old(root / "setup.exe", FAKE_EXE)
    write_old(root / "song.mp3", b"ID3\x04" + b"\x00" * 50)
    plan = plan_for(root)
    f.unlink()
    report = apply_plan(plan)
    statuses = {r.src: r.status for r in report.results}
    assert statuses == {"setup.exe": "skipped", "song.mp3": "moved"}


# --- tampered plans / path escapes -------------------------------------------------

@pytest.mark.parametrize("bad_dest", [
    "..\\outside.exe", "../outside.exe", "Installers/../../outside.exe",
    "C:\\Windows\\evil.exe", "/etc/evil", "Windows/System32/evil.exe", "Installers\\sub\\x.exe",
])
def test_tampered_destination_is_refused(tmp_path, bad_dest):
    root = tmp_path / "dl"
    f = write_old(root / "setup.exe", FAKE_EXE)
    plan = plan_for(root)
    d = plan.to_dict()
    d["proposals"][0]["dest_rel"] = bad_dest
    report = apply_plan(Plan.from_dict(d))
    assert report.results[0].status == "skipped"
    assert f.read_bytes() == FAKE_EXE
    assert not (tmp_path / "outside.exe").exists()


@pytest.mark.parametrize("bad_src", ["..\\victim.txt", "sub\\file.txt", "C:\\Windows\\win.ini"])
def test_tampered_source_is_refused(tmp_path, bad_src):
    root = tmp_path / "dl"
    write_old(root / "setup.exe", FAKE_EXE)
    victim = write_old(tmp_path / "victim.txt", "do not move me")
    plan = plan_for(root)
    d = plan.to_dict()
    d["proposals"][0]["rel_path"] = bad_src
    report = apply_plan(Plan.from_dict(d))
    assert report.results[0].status == "skipped"
    assert victim.read_text() == "do not move me"


def test_unapproved_items_are_not_moved(demo):
    root, _ = demo
    plan = plan_for(root)
    for p in plan.proposals:
        p.approved = False
    before = snapshot(root)
    report = apply_plan(plan)
    assert report.results == [] and snapshot(root) == before


# --- risky or ambiguous files stay put ---------------------------------------------

def test_unsafe_and_ambiguous_files_are_kept(demo):
    root, _ = demo
    plan = plan_for(root)
    for name, why in [("holiday_photo.jpg", "disguised executable"), ("report.pdf", "failed download"),
                      ("setup_v2.exe.crdownload", "unfinished download"), ("mystery.xyz", "unrecognised"),
                      ("empty.txt", "empty file")]:
        p = by_name(plan, name)
        assert p.action == "keep", name
        assert why in " ".join(p.reasons), (name, p.reasons)


def test_recently_modified_file_is_skipped(tmp_path):
    root = tmp_path / "dl"
    root.mkdir()
    (root / "new.exe").write_bytes(FAKE_EXE)  # mtime = now
    p = plan_for(root).proposals[0]
    assert p.action == "keep" and "downloading" in p.reasons[0]


def test_low_confidence_threshold_keeps_files(demo):
    root, _ = demo
    plan = plan_for(root, threshold=0.99)
    assert all(p.action == "keep" or p.confidence >= 0.99 for p in plan.proposals)


@pytest.mark.skipif(not WINDOWS, reason="Windows hidden attribute")
def test_hidden_files_are_kept(tmp_path):
    root = tmp_path / "dl"
    f = write_old(root / "secret.exe", FAKE_EXE)
    subprocess.run(["attrib", "+h", str(f)], check=True)
    p = plan_for(root).proposals[0]
    assert p.action == "keep" and "hidden" in p.reasons[0]


def test_blocking_file_where_category_folder_should_be(tmp_path):
    root = tmp_path / "dl"
    write_old(root / "setup.exe", FAKE_EXE)
    write_old(root / "Installers", b"i am a file, not a folder")
    p = by_name(plan_for(root), "setup.exe")
    assert p.action == "keep"


# --- links and junctions -------------------------------------------------------------

def _junction(link: Path, target: Path) -> bool:
    if not WINDOWS:
        try:
            os.symlink(target, link, target_is_directory=True)
            return True
        except OSError:
            return False
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
    return r.returncode == 0


def test_junction_as_category_folder_is_not_followed(tmp_path):
    root = tmp_path / "dl"
    outside = tmp_path / "outside"
    outside.mkdir()
    write_old(root / "setup.exe", FAKE_EXE)
    if not _junction(root / "Installers", outside):
        pytest.skip("cannot create junction/symlink here")
    plan = plan_for(root)
    report = apply_plan(plan)
    assert list(outside.iterdir()) == []
    assert (root / "setup.exe").exists()
    assert all(r.status != "moved" for r in report.results)


def test_selected_folder_that_is_a_junction_is_refused(tmp_path):
    real = tmp_path / "real"
    write_old(real / "setup.exe", FAKE_EXE)
    if not _junction(tmp_path / "link", real):
        pytest.skip("cannot create junction/symlink here")
    with pytest.raises(ValueError):
        scan_folder(tmp_path / "link")


def test_symlinked_file_is_never_followed(tmp_path):
    root = tmp_path / "dl"
    target = write_old(tmp_path / "target.exe", FAKE_EXE)
    root.mkdir()
    try:
        os.symlink(target, root / "link.exe")
    except OSError:
        pytest.skip("symlinks need Developer Mode or admin on Windows")
    p = plan_for(root).proposals[0]
    assert p.action == "keep" and "link" in p.reasons[0]


# --- permission / locking ------------------------------------------------------------

@pytest.mark.skipif(not WINDOWS, reason="Windows file locking semantics")
def test_locked_file_fails_cleanly_and_others_continue(tmp_path):
    root = tmp_path / "dl"
    locked = write_old(root / "a_setup.exe", FAKE_EXE)
    write_old(root / "b_song.mp3", b"ID3\x04" + b"\x00" * 50)
    plan = plan_for(root)
    with open(locked, "rb"):  # Python opens without FILE_SHARE_DELETE: rename is blocked
        report = apply_plan(plan)
    statuses = {r.src: (r.status, r.detail) for r in report.results}
    assert statuses["a_setup.exe"] == ("failed", "file is open in another program")
    assert statuses["b_song.mp3"][0] == "moved"
    assert locked.exists()
    undo = undo_run(report.run_id)
    assert undo.count("restored") == 1 and (root / "b_song.mp3").exists()


@pytest.mark.skipif(not WINDOWS, reason="Windows file locking semantics")
def test_undo_retry_after_lock_released(tmp_path):
    root = tmp_path / "dl"
    write_old(root / "setup.exe", FAKE_EXE)
    report = apply_plan(plan_for(root))
    moved = root / "Installers" / "setup.exe"
    with open(moved, "rb"):
        first = undo_run(report.run_id)
    assert first.count("failed") == 1 and moved.exists()
    second = undo_run(report.run_id)
    assert second.count("restored") == 1 and (root / "setup.exe").exists()


# --- interruption and recovery ----------------------------------------------------------

def test_interrupted_run_blocks_new_runs_until_recovered(demo):
    root, _ = demo
    before = snapshot(root)
    plan = plan_for(root)
    with pytest.raises(KeyboardInterrupt):
        apply_plan(plan, interrupt_after=5)
    [run_id] = incomplete_runs(root)
    assert run_id.startswith(plan.plan_id)
    with pytest.raises(NeedsRecovery):
        apply_plan(plan_for(root))
    with pytest.raises(NeedsRecovery):
        undo_run(run_id)
    recover(root)
    assert incomplete_runs(root) == []
    undo = undo_run(run_id)
    assert undo.count("restored") == 5
    assert snapshot(root) == before


def test_crash_after_move_before_done_record(tmp_path, monkeypatch):
    root = tmp_path / "dl"
    write_old(root / "setup.exe", FAKE_EXE)
    before = snapshot(root)
    plan = plan_for(root)
    real_move = executor.safe_move

    def move_then_crash(src, dst):
        real_move(src, dst)
        raise KeyboardInterrupt("power cut")

    monkeypatch.setattr(executor, "safe_move", move_then_crash)
    with pytest.raises(KeyboardInterrupt):
        apply_plan(plan)
    monkeypatch.setattr(executor, "safe_move", real_move)
    [rep] = recover(root)
    assert rep.results[0].status == "moved"  # journal now agrees with the disk
    undo_run(rep.run_id)
    assert snapshot(root) == before


def test_crash_after_intent_before_move(tmp_path, monkeypatch):
    root = tmp_path / "dl"
    write_old(root / "setup.exe", FAKE_EXE)
    before = snapshot(root)
    plan = plan_for(root)

    def crash(src, dst):
        raise KeyboardInterrupt("power cut")

    real_move = executor.safe_move
    monkeypatch.setattr(executor, "safe_move", crash)
    with pytest.raises(KeyboardInterrupt):
        apply_plan(plan)
    monkeypatch.setattr(executor, "safe_move", real_move)
    [rep] = recover(root)
    assert rep.results[0].status == "skipped"
    assert snapshot(root) == before


def test_crash_during_undo_is_recoverable(tmp_path, monkeypatch):
    root = tmp_path / "dl"
    write_old(root / "a.exe", FAKE_EXE)
    write_old(root / "b.mp3", b"ID3\x04" + b"\x00" * 50)
    before = snapshot(root)
    report = apply_plan(plan_for(root))
    real_move = executor.safe_move
    calls = {"n": 0}

    def move_then_crash(src, dst):
        real_move(src, dst)
        calls["n"] += 1
        raise KeyboardInterrupt("power cut during undo")

    monkeypatch.setattr(executor, "safe_move", move_then_crash)
    with pytest.raises(KeyboardInterrupt):
        undo_run(report.run_id)
    monkeypatch.setattr(executor, "safe_move", real_move)
    recover(root)
    undo_run(report.run_id)
    assert snapshot(root) == before


def test_torn_journal_line_is_ignored(tmp_path):
    root = tmp_path / "dl"
    write_old(root / "setup.exe", FAKE_EXE)
    before = snapshot(root)
    report = apply_plan(plan_for(root))
    jpath = executor.runs_dir() / f"{report.run_id}.jsonl"
    with open(jpath, "a", encoding="utf-8") as f:
        f.write('{"t": "undo_begin", "ts": "2026-')  # half-written line from a crash
    undo_run(report.run_id)
    assert snapshot(root) == before


# --- undo safety ----------------------------------------------------------------------

def test_undo_skips_file_edited_after_organizing(tmp_path):
    root = tmp_path / "dl"
    write_old(root / "setup.exe", FAKE_EXE)
    report = apply_plan(plan_for(root))
    moved = root / "Installers" / "setup.exe"
    moved.write_bytes(b"user edited this")
    undo = undo_run(report.run_id)
    assert undo.results[0].status == "skipped"
    assert moved.read_bytes() == b"user edited this"


def test_undo_never_overwrites_a_new_file_at_original_name(tmp_path):
    root = tmp_path / "dl"
    write_old(root / "setup.exe", FAKE_EXE)
    report = apply_plan(plan_for(root))
    (root / "setup.exe").write_bytes(b"a newer download")
    undo = undo_run(report.run_id)
    assert undo.results[0].status == "skipped"
    assert (root / "setup.exe").read_bytes() == b"a newer download"
    assert (root / "Installers" / "setup.exe").read_bytes() == FAKE_EXE


def test_undo_keeps_category_folder_that_now_holds_user_files(tmp_path):
    root = tmp_path / "dl"
    write_old(root / "setup.exe", FAKE_EXE)
    report = apply_plan(plan_for(root))
    (root / "Installers" / "mine.txt").write_text("added by user later")
    undo_run(report.run_id)
    assert (root / "Installers" / "mine.txt").read_text() == "added by user later"


@pytest.mark.skipif(not WINDOWS, reason="Mark-of-the-Web is an NTFS stream")
def test_mark_of_the_web_survives_move_and_is_read(tmp_path):
    root = tmp_path / "dl"
    f = write_old(root / "setup.exe", FAKE_EXE)
    try:
        with open(f"{f}:Zone.Identifier", "w") as ads:
            ads.write("[ZoneTransfer]\nZoneId=3\nHostUrl=https://downloads.example.com/setup.exe\n")
    except OSError:
        pytest.skip("filesystem without alternate data streams")
    t = time.time() - 864000
    os.utime(f, (t, t))
    records, _ = scan_folder(root)
    assert records[0].source_host == "downloads.example.com"
    plan = build_plan(root, records)
    apply_plan(plan)
    with open(f"{root / 'Installers' / 'setup.exe'}:Zone.Identifier") as ads:
        assert "ZoneId=3" in ads.read()


def test_every_proposal_explains_itself(demo):
    root, _ = demo
    plan = plan_for(root)
    assert plan.proposals and all(p.reasons and all(isinstance(r, str) and r for r in p.reasons)
                                  for p in plan.proposals)
