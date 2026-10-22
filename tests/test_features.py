"""Integration of the optional features into planner, executor and service."""

import io
import os
import time
import zipfile
from pathlib import Path

import pytest

from conftest import snapshot, write_old
from slothfile import executor, winshell
from slothfile.demo import FAKE_EXE, make_pdf
from slothfile.executor import apply_plan, recover, undo_run
from slothfile.models import FileRecord, Plan
from slothfile.planner import PlanOptions, build_plan
from slothfile.scanner import scan_folder
from slothfile.service import apply_edits, execute, load_plan, save_plan

DAY = 86400


def plan_for(root, **opts):
    records, stats = scan_folder(root)
    return build_plan(root, records, scan_stats=stats, options=PlanOptions(**opts))


def by_name(plan, name):
    return next(p for p in plan.proposals if p.rel_path == name)


def make_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n, c in files.items():
            z.writestr(n, c)
    return buf.getvalue()


# --- defaults stay off ------------------------------------------------------------

def test_new_features_are_off_by_default(demo):
    root, _ = demo
    plan = plan_for(root)
    assert all(p.action in ("move", "keep") for p in plan.proposals)
    assert not any(p.extract_to or p.near_duplicate for p in plan.proposals)


# --- photos by date ----------------------------------------------------------------

def test_photo_goes_to_dated_folder_and_undo_cleans_it(tmp_path):
    from PIL import Image
    root = tmp_path / "dl"
    root.mkdir()
    img = Image.new("RGB", (64, 48), (10, 120, 200))
    exif = Image.Exif()
    exif[306] = "2026:01:02 09:00:00"  # last edited: must NOT decide the folder
    exif.get_ifd(0x8769)[36867] = "2025:07:14 10:22:31"  # DateTimeOriginal: when it was taken
    img.save(root / "IMG_0001.jpg", exif=exif)
    t = time.time() - 10 * DAY
    os.utime(root / "IMG_0001.jpg", (t, t))
    before = snapshot(root)
    plan = plan_for(root, photos_by_date=True)
    p = by_name(plan, "IMG_0001.jpg")
    assert p.dest_rel == str(Path("Images", "2025", "07", "IMG_0001.jpg"))
    rep = apply_plan(plan)
    assert (root / "Images" / "2025" / "07" / "IMG_0001.jpg").exists()
    undo_run(rep.run_id)
    assert snapshot(root) == before
    assert not (root / "Images").exists()


@pytest.mark.parametrize("bad", ["Images/2025/13/x.jpg", "Images/2025/07/../x.jpg", "Images/1800/01/x.jpg",
                                 "Images/2025/7/x.jpg", "Images/2025/07/sub/x.jpg"])
def test_tampered_dated_folder_refused(tmp_path, bad):
    root = tmp_path / "dl"
    write_old(root / "pic.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    plan = plan_for(root)
    d = plan.to_dict()
    d["proposals"][0].update(category="images", dest_rel=bad)
    rep = apply_plan(Plan.from_dict(d))
    assert rep.results[0].status == "skipped"
    assert (root / "pic.png").exists()


# --- near duplicates ---------------------------------------------------------------

def test_near_duplicate_documents_are_unticked(tmp_path):
    root = tmp_path / "dl"
    lines = ["Quarterly planning notes for the garden club"] + [f"Item {i}: compost, seeds and tools" for i in range(12)]
    write_old(root / "notes.pdf", make_pdf(lines, title="v1"), days=20)
    write_old(root / "notes (1).pdf", make_pdf(lines, title="v2"), days=10)
    plan = plan_for(root, near_duplicates=True)
    near = by_name(plan, "notes (1).pdf")
    assert near.near_duplicate and near.category == "duplicates" and not near.approved
    assert by_name(plan, "notes.pdf").category != "duplicates"


# --- zip unpacking -----------------------------------------------------------------

def test_zip_unpack_and_undo_removes_only_unchanged_files(tmp_path):
    root = tmp_path / "dl"
    write_old(root / "project.zip", make_zip({"a.txt": b"alpha", "dir/b.txt": b"beta"}))
    before = snapshot(root)
    plan = plan_for(root, unpack_zips=True)
    p = by_name(plan, "project.zip")
    assert p.extract_to == str(Path("Archives", "project"))
    rep = apply_plan(plan)
    assert rep.results[0].status == "moved" and "unpacked 2" in rep.results[0].detail
    out = root / "Archives" / "project"
    assert (out / "a.txt").read_bytes() == b"alpha" and (out / "dir" / "b.txt").read_bytes() == b"beta"
    undo_run(rep.run_id)
    assert snapshot(root) == before


def test_zip_undo_keeps_files_the_user_edited(tmp_path):
    root = tmp_path / "dl"
    write_old(root / "project.zip", make_zip({"a.txt": b"alpha", "b.txt": b"beta"}))
    rep = apply_plan(plan_for(root, unpack_zips=True))
    edited = root / "Archives" / "project" / "a.txt"
    edited.write_bytes(b"my edits")
    undo_run(rep.run_id)
    assert edited.read_bytes() == b"my edits"
    assert not (root / "Archives" / "project" / "b.txt").exists()
    assert (root / "project.zip").exists()


def test_zip_slip_is_never_unpacked(tmp_path):
    root = tmp_path / "dl"
    write_old(root / "evil.zip", make_zip({"../outside.txt": b"x", "ok.txt": b"y"}))
    plan = plan_for(root, unpack_zips=True)
    p = by_name(plan, "evil.zip")
    assert p.extract_to is None and any("won't unpack" in r for r in p.reasons)
    apply_plan(plan)
    assert not (tmp_path / "outside.txt").exists()


@pytest.mark.parametrize("bad", ["..\\x", "Archives\\..\\..\\x", "Documents\\x", "Archives\\a\\b", "C:\\x"])
def test_tampered_extract_target_refused(tmp_path, bad):
    root = tmp_path / "dl"
    write_old(root / "project.zip", make_zip({"a.txt": b"alpha"}))
    plan = plan_for(root, unpack_zips=True)
    d = plan.to_dict()
    d["proposals"][0]["extract_to"] = bad
    rep = apply_plan(Plan.from_dict(d))
    assert "not unpacked" in rep.results[0].detail
    assert not (tmp_path / "x").exists()


# --- old installers -> Recycle Bin (mocked: tests never touch the real bin) --------

@pytest.fixture
def fake_bin(monkeypatch):
    binned = {}

    def recycle(paths):
        out = {}
        for p in paths:
            st = os.stat(p)
            binned[str(p)] = (Path(p).read_bytes(), st.st_atime_ns, st.st_mtime_ns)
            os.remove(p)
            out[str(p)] = None
        return out

    def restore(path, timeout=30):
        item = binned.pop(str(path), None)
        if item is None:
            return False
        data, atime, mtime = item
        Path(path).write_bytes(data)
        os.utime(path, ns=(atime, mtime))  # the real bin restores the original timestamps
        return True

    monkeypatch.setattr(winshell, "recycle", recycle)
    monkeypatch.setattr(winshell, "restore_from_recycle_bin", restore)
    return binned


def test_old_installer_suggested_unticked(tmp_path):
    root = tmp_path / "dl"
    write_old(root / "OldTool-setup.exe", FAKE_EXE, days=200)
    write_old(root / "NewTool-setup.exe", FAKE_EXE + b"n", days=3)
    plan = plan_for(root, recycle_old_installers_days=90, installed=frozenset())
    old = by_name(plan, "OldTool-setup.exe")
    assert old.action == "recycle" and not old.approved
    assert by_name(plan, "NewTool-setup.exe").action == "move"


def test_installed_program_match_suggests_recycle(tmp_path):
    root = tmp_path / "dl"
    write_old(root / "ChromeSetup.exe", FAKE_EXE, days=20)
    plan = plan_for(root, recycle_old_installers_days=90, installed=frozenset({"google chrome"}))
    p = by_name(plan, "ChromeSetup.exe")
    assert p.action == "recycle" and "already installed" in " ".join(p.reasons)


def test_recycle_needs_separate_confirmation(tmp_path, fake_bin):
    root = tmp_path / "dl"
    write_old(root / "OldTool-setup.exe", FAKE_EXE, days=200)
    plan = plan_for(root, recycle_old_installers_days=90, installed=frozenset())
    plan.proposals[0].approved = True
    rep = apply_plan(plan)  # no allow_recycle
    assert rep.results[0].status == "skipped" and (root / "OldTool-setup.exe").exists()


def test_recycle_and_undo_restore(tmp_path, fake_bin):
    root = tmp_path / "dl"
    write_old(root / "OldTool-setup.exe", FAKE_EXE, days=200)
    before = snapshot(root)
    plan = plan_for(root, recycle_old_installers_days=90, installed=frozenset())
    plan.proposals[0].approved = True
    rep = apply_plan(plan, allow_recycle=True)
    assert rep.results[0].status == "recycled" and not (root / "OldTool-setup.exe").exists()
    undo = undo_run(rep.run_id)
    assert undo.results[0].status == "restored"
    assert snapshot(root) == before


def test_tampered_recycle_of_non_installer_refused(tmp_path, fake_bin):
    root = tmp_path / "dl"
    write_old(root / "thesis.docx", b"PK\x03\x04personal")
    plan = plan_for(root)
    d = plan.to_dict()
    d["proposals"][0].update(action="recycle", approved=True)
    rep = apply_plan(Plan.from_dict(d), allow_recycle=True)
    assert rep.results[0].status == "skipped" and (root / "thesis.docx").exists()
    assert not fake_bin


def test_crash_during_recycle_is_recovered(tmp_path, monkeypatch, fake_bin):
    root = tmp_path / "dl"
    write_old(root / "OldTool-setup.exe", FAKE_EXE, days=200)
    plan = plan_for(root, recycle_old_installers_days=90, installed=frozenset())
    plan.proposals[0].approved = True
    real = winshell.recycle

    def recycle_then_crash(paths):
        real(paths)
        raise KeyboardInterrupt("power cut")

    monkeypatch.setattr(winshell, "recycle", recycle_then_crash)
    with pytest.raises(KeyboardInterrupt):
        apply_plan(plan, allow_recycle=True)
    monkeypatch.setattr(winshell, "recycle", real)
    [rep] = recover(root)
    assert rep.results[0].status == "recycled"
    undo_run(rep.run_id)
    assert (root / "OldTool-setup.exe").read_bytes() == FAKE_EXE


# --- learning through real reviews ----------------------------------------------------

def test_folder_override_moves_there_and_is_learned(tmp_path):
    from slothfile import learning
    for round_ in range(2):
        root = tmp_path / f"dl{round_}"
        rec_name = f"statement{round_}.xyzdoc.txt"
        write_old(root / rec_name, f"Notes round {round_}\nsome text".encode())
        records, stats = scan_folder(root)
        for r in records:
            r.source_host = "bank.example"
        plan = build_plan(root, records, options=PlanOptions(learned_rules=True))
        save_plan(plan)
        rep = execute(plan.plan_id, approved={rec_name}, overrides={rec_name: "finance"})
        assert rep.results[0].status == "moved"
        assert (root / "Documents" / "Finance" / rec_name).exists()
    rules = learning.learned_rules()
    assert any(r.key == "host:bank.example" and r.outcome == "finance" for r in rules)
    # Third time: proposed automatically, labelled as learned.
    root = tmp_path / "dl3"
    write_old(root / "letter.txt", b"Hello there\nplain text")
    records, _ = scan_folder(root)
    records[0].source_host = "bank.example"
    plan = build_plan(root, records, options=PlanOptions(learned_rules=True))
    p = plan.proposals[0]
    assert p.category == "finance" and p.engine == "rules+learned"


def test_override_to_unknown_or_duplicates_folder_refused(demo):
    root, _ = demo
    plan = plan_for(root)
    name = next(p.rel_path for p in plan.proposals if p.action == "move" and p.category != "duplicates")
    from slothfile.pathsafe import UnsafePath
    for bad in ("..\\evil", "duplicates", "Windows"):
        with pytest.raises(UnsafePath):
            apply_edits(load_plan_copy(plan), overrides={name: bad})


def load_plan_copy(plan):
    return Plan.from_dict(plan.to_dict())
