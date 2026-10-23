"""Regression tests for defects found when reviewing how the features fit together (I0-I15)."""

import sys

import io
import json
import os
import zipfile
from pathlib import Path

import pytest

from conftest import snapshot, write_old
from slothfile import executor, learning, winshell
from slothfile.demo import FAKE_EXE
from slothfile.executor import apply_plan, recover, undo_run
from slothfile.models import FileRecord, Plan
from slothfile.planner import PlanOptions, build_plan
from slothfile.rules import Decision
from slothfile.scanner import scan_folder
from slothfile.service import execute, load_plan, save_plan
from test_features import fake_bin  # noqa: F401 - fixture


def make_zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n, c in files.items():
            z.writestr(n, c)
    return buf.getvalue()


def unpack_plan(root):
    recs, stats = scan_folder(root)
    return build_plan(root, recs, scan_stats=stats, options=PlanOptions(unpack_zips=True))


# I0: undo must never delete unpacked files when the zip can't be restored
def test_unpacked_files_kept_when_zip_was_deleted(tmp_path):
    root = tmp_path / "dl"
    write_old(root / "photos.zip", make_zip({"wedding.jpg": b"precious pixels"}))
    rep = apply_plan(unpack_plan(root))
    os.remove(root / "Archives" / "photos.zip")  # user keeps only the unpacked folder
    undo = undo_run(rep.run_id)
    assert (root / "Archives" / "photos" / "wedding.jpg").read_bytes() == b"precious pixels"
    assert any("unpacked files kept" in r.detail for r in undo.results)


# I1/I2: crash while unpacking -> recover + undo leave the folder exactly as before
@pytest.mark.parametrize("crash_at", ["journal", "utime", "mkdirs"])
def test_crash_during_unpack_is_fully_undone(tmp_path, monkeypatch, crash_at):
    from slothfile import archives, journal
    root = tmp_path / "dl"
    write_old(root / "proj.zip", make_zip({"a.txt": b"A", "d/b.txt": b"B", "c.txt": b"C"}))
    before = snapshot(root)
    plan = unpack_plan(root)
    if crash_at == "journal":
        real = journal.Journal.write

        def write(self, rec):
            if rec.get("t") == "extracted_file":
                raise KeyboardInterrupt("power cut")
            real(self, rec)
        monkeypatch.setattr(journal.Journal, "write", write)
    elif crash_at == "utime":
        calls = {"n": 0}
        real_utime = os.utime

        def utime(*a, **k):
            calls["n"] += 1
            if calls["n"] == 2:
                raise KeyboardInterrupt("power cut")
            return real_utime(*a, **k)
        monkeypatch.setattr(archives.os, "utime", utime)
    else:
        def mk(*a, **k):
            raise KeyboardInterrupt("power cut")
        monkeypatch.setattr(archives, "_mkdirs", mk)
    with pytest.raises(KeyboardInterrupt):
        apply_plan(plan)
    monkeypatch.undo()
    os.environ["SLOTHFILE_HOME"] = str(tmp_path / "state")  # monkeypatch.undo also reset the env
    recover(root)
    [run] = [p.stem for p in executor.runs_dir().glob("*.jsonl")]
    undo_run(run)
    assert snapshot(root) == before
    assert sorted(p.name for p in root.iterdir()) == ["proj.zip"]


def test_corrupt_zip_leaves_no_empty_folders_after_undo(tmp_path):
    root = tmp_path / "dl"
    good = make_zip({"a.txt": b"A" * 100})
    write_old(root / "proj.zip", good)
    before = snapshot(root)
    plan = unpack_plan(root)
    data = bytearray(good)
    off = data.find(b"A" * 10) if b"A" * 10 in data else 40
    raw = bytearray((root / "proj.zip").read_bytes())
    # Corrupt the stored data after planning (fingerprint covers only the head, so keep size)
    raw[-60] ^= 0xFF
    st = os.stat(root / "proj.zip")
    (root / "proj.zip").write_bytes(bytes(raw))
    os.utime(root / "proj.zip", ns=(st.st_atime_ns, st.st_mtime_ns))
    rep = apply_plan(plan)
    undo_run(rep.run_id)
    assert sorted(p.name for p in root.iterdir()) == ["proj.zip"]


# I5: locked unpacked file -> reported, zip stays, a retry finishes the job
@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only behaviour")
def test_locked_unpacked_file_is_reported_and_retryable(tmp_path):
    root = tmp_path / "dl"
    write_old(root / "proj.zip", make_zip({"a.txt": b"A", "b.txt": b"B"}))
    before = snapshot(root)
    rep = apply_plan(unpack_plan(root))
    with open(root / "Archives" / "proj" / "a.txt", "rb"):
        first = undo_run(rep.run_id)
    assert first.count("failed") == 1
    assert (root / "Archives" / "proj.zip").exists()
    undo_run(rep.run_id)
    assert snapshot(root) == before


def test_edited_unpacked_file_is_reported(tmp_path):
    root = tmp_path / "dl"
    write_old(root / "proj.zip", make_zip({"a.txt": b"A", "b.txt": b"B"}))
    rep = apply_plan(unpack_plan(root))
    (root / "Archives" / "proj" / "a.txt").write_bytes(b"my edits")
    undo = undo_run(rep.run_id)
    assert any("kept 1 unpacked file(s) you changed" in r.detail for r in undo.results)
    assert (root / "Archives" / "proj" / "a.txt").read_bytes() == b"my edits"


# I3: plan says "installers" but the file isn't one
def test_recycle_refuses_non_installer_even_if_plan_says_installer(tmp_path, fake_bin):
    root = tmp_path / "dl"
    write_old(root / "thesis.docx", b"PK\x03\x04 my thesis")
    recs, _ = scan_folder(root)
    plan = build_plan(root, recs)
    d = plan.to_dict()
    d["proposals"][0].update(action="recycle", category="installers", approved=True)
    rep = apply_plan(Plan.from_dict(d), allow_recycle=True)
    assert rep.results[0].status == "skipped" and not fake_bin
    assert (root / "thesis.docx").exists()


def test_recycle_refuses_renamed_exe_with_wrong_magic(tmp_path, fake_bin):
    root = tmp_path / "dl"
    write_old(root / "notes-setup.exe", b"just text pretending", days=200)
    recs, _ = scan_folder(root)
    plan = build_plan(root, recs)
    d = plan.to_dict()
    d["proposals"][0].update(action="recycle", category="installers", approved=True)
    rep = apply_plan(Plan.from_dict(d), allow_recycle=True)
    assert rep.results[0].status == "skipped"


# I4: the bin returns a different same-named file -> it goes back, honestly reported
@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only behaviour")
def test_undo_recycle_returns_wrong_copy_to_bin(tmp_path, monkeypatch, fake_bin):
    root = tmp_path / "dl"
    write_old(root / "Tool-setup.exe", FAKE_EXE + b"v1", days=200)
    recs, _ = scan_folder(root)
    plan = build_plan(root, recs, options=PlanOptions(recycle_old_installers_days=90, installed=frozenset()))
    plan.proposals[0].approved = True
    rep = apply_plan(plan, allow_recycle=True)
    restored = {}

    def restore_other(path, timeout=30):
        Path(path).write_bytes(FAKE_EXE + b"v2 newer")
        restored["yes"] = True
        return True

    monkeypatch.setattr(winshell, "restore_from_recycle_bin", restore_other)
    undo = undo_run(rep.run_id)
    assert undo.results[0].status == "failed" and "newer file" in undo.results[0].detail
    assert not (root / "Tool-setup.exe").exists()  # wrong copy was put back in the bin


# I6/I7: refused or repeated requests never teach rules
def _plan_with(root, name, host):
    write_old(root / name, b"plain text")
    recs, _ = scan_folder(root)
    for r in recs:
        r.source_host = host
    plan = build_plan(root, recs)
    save_plan(plan)
    return plan


def test_refused_requests_do_not_teach(tmp_path):
    from slothfile.pathsafe import UnsafePath
    for i in range(2):
        root = tmp_path / f"d{i}"
        plan = _plan_with(root, f"t{i}.txt", "evil.example")
        with pytest.raises(UnsafePath):
            execute(plan.plan_id, approved={f"t{i}.txt"}, overrides={f"t{i}.txt": "duplicates"})
    assert learning.learned_rules() == []


def test_resubmitting_same_plan_counts_once(tmp_path):
    root = tmp_path / "d"
    plan = _plan_with(root, "a.txt", "one.example")
    execute(plan.plan_id, approved={"a.txt"}, overrides={"a.txt": "finance"})
    execute(plan.plan_id, approved={"a.txt"}, overrides={"a.txt": "finance"})
    assert not [r for r in learning.learned_rules() if r.key == "host:one.example"]


def test_duplicates_is_never_a_learned_outcome(tmp_path):
    p = learning.learned_rules  # noqa: F841
    assert not learning._valid_outcome("duplicates")


# I8: /api/auto shares the operation lock
def test_api_auto_respects_lock(demo):
    import http.client
    import threading
    from http.server import ThreadingHTTPServer
    from slothfile.web import App, make_handler
    root, _ = demo
    app = App(str(root))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    port = httpd.server_address[1]
    app.allowed_hosts = {f"127.0.0.1:{port}"}
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        app.lock.acquire()
        c = http.client.HTTPConnection("127.0.0.1", port)
        c.request("POST", "/api/auto", body=json.dumps({"folder": str(root)}),
                  headers={"Content-Type": "application/json", "X-SlothFile-Token": app.token})
        assert c.getresponse().status == 409
        app.lock.release()
        # I10: a JSON body that is not an object is a clean 400, not a crash
        for body in ("[1,2]", "5", '"x"', "null", "true"):
            c = http.client.HTTPConnection("127.0.0.1", port)
            c.request("POST", "/api/apply", body=body,
                      headers={"Content-Type": "application/json", "X-SlothFile-Token": app.token})
            assert c.getresponse().status == 400
    finally:
        httpd.shutdown()
        httpd.server_close()


# I9: "keep" on a recycle row is honoured
def test_keep_override_on_recycle_row_recycles_nothing(tmp_path, fake_bin):
    root = tmp_path / "dl"
    write_old(root / "Old-setup.exe", FAKE_EXE, days=200)
    recs, _ = scan_folder(root)
    plan = build_plan(root, recs, options=PlanOptions(recycle_old_installers_days=90, installed=frozenset()))
    save_plan(plan)
    rep = execute(plan.plan_id, approved={"Old-setup.exe"}, overrides={"Old-setup.exe": "keep"}, allow_recycle=True)
    assert not fake_bin and (root / "Old-setup.exe").exists()
    assert all(r.status != "recycled" for r in rep.results)


# I12: generic installer names don't claim "already installed"
@pytest.mark.parametrize("name", ["driver_setup.exe", "Client.exe", "Launcher.exe", "setup.exe", "amd64-x64.exe"])
def test_generic_installer_names_do_not_match(name):
    from slothfile.installed import matching_program
    assert matching_program(name, frozenset({"realtek audio driver", "zoom client", "epic games launcher"})) is None


# I14: OCR never reads a PDF hiding behind another extension
def test_ocr_skips_extension_mismatch(tmp_path, monkeypatch):
    from slothfile import ocr
    from slothfile.demo import make_pdf
    called = []
    monkeypatch.setattr(ocr, "ocr_available", lambda: True)
    monkeypatch.setattr(ocr, "ocr_pdf", lambda p, w: called.append(p) or "text")
    monkeypatch.setattr(ocr, "ocr_images", lambda paths, timeout=30: {})
    root = tmp_path / "dl"
    write_old(root / "pic.jpg", make_pdf([]))
    records, stats = scan_folder(root, ocr=True)
    assert called == [] and not stats.get("ocr_files")


# I15: learned rules don't lift empty files over the threshold
def test_learned_rule_does_not_move_empty_file(tmp_path, monkeypatch):
    rule = learning.Rule("ext:.txt", "finance", 3, 0)
    monkeypatch.setattr(learning, "_cached_rules", lambda: {"ext:.txt": rule})
    rec = FileRecord("e.txt", "e.txt", ".txt", 0, 1, "2026-01-01")
    d = learning.apply_learned(rec, Decision("documents", 0.4, ["empty file"]))
    assert d.confidence <= 0.4
