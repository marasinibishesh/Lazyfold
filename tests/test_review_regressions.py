"""Regression tests for defects found by an adversarial security review of the core (C0-C15)."""

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from conftest import snapshot, write_old
from lazyfold import executor, journal, pathsafe
from lazyfold.cli import _safe
from lazyfold.demo import FAKE_EXE, make_pdf
from lazyfold.executor import apply_plan, incomplete_runs, recover, undo_run
from lazyfold.llm import ModelError, OllamaClient, parse_advice
from lazyfold.models import Plan
from lazyfold.planner import build_plan
from lazyfold.scanner import scan_folder
from lazyfold.service import apply_edits

WINDOWS = sys.platform == "win32"


def plan_for(root):
    records, stats = scan_folder(root)
    return build_plan(root, records, scan_stats=stats)


def tampered(root, **changes):
    d = plan_for(root).to_dict()
    d["proposals"][0].update(changes)
    return Plan.from_dict(d)


# C0: category folder swapped for a junction between check and rename
@pytest.mark.skipif(not WINDOWS, reason="junction race is Windows-specific")
def test_junction_swap_during_move_cannot_escape(tmp_path, monkeypatch):
    root = tmp_path / "dl"
    outside = tmp_path / "outside"
    outside.mkdir()
    write_old(root / "setup.exe", FAKE_EXE)
    (root / "Installers").mkdir()
    plan = plan_for(root)
    real_write = journal.Journal.write
    swap = {}

    def write_and_attack(self, record):
        real_write(self, record)
        if record.get("t") == "intent":
            try:  # an attacker process acting in the gap
                os.rename(root / "Installers", root / "Installers_real")
                subprocess.run(["cmd", "/c", "mklink", "/J", str(root / "Installers"), str(outside)],
                               capture_output=True)
                swap["done"] = True
            except OSError as e:
                swap["blocked"] = e

    monkeypatch.setattr(journal.Journal, "write", write_and_attack)
    report = apply_plan(plan)
    assert list(outside.iterdir()) == []
    assert "blocked" in swap, "folder chain should be pinned during the move"
    assert report.results[0].status == "moved"
    assert (root / "Installers" / "setup.exe").read_bytes() == FAKE_EXE


# C1: casefold look-alikes are not category folders
@pytest.mark.parametrize("dest", ["Documents/\ufb01nance/a.exe", "Image\u017f/a.exe", "Installer\u017f/setup.exe"])
def test_casefold_lookalike_destination_refused(tmp_path, dest):
    root = tmp_path / "dl"
    write_old(root / "setup.exe", FAKE_EXE)
    report = apply_plan(tampered(root, dest_rel=dest))
    assert report.results[0].status == "skipped"
    assert sorted(p.name for p in root.iterdir()) == ["setup.exe"]


# C2: destination file name re-validated at execution time
@pytest.mark.parametrize("dest", ["Documents/Finance", "Installers/setup.exe. ", "Installers/setup.exe:evil",
                                  "Installers/NUL.exe", "Installers/CONIN$"])
def test_unsafe_destination_name_refused(tmp_path, dest):
    root = tmp_path / "dl"
    write_old(root / "setup.exe", FAKE_EXE)
    report = apply_plan(tampered(root, dest_rel=dest, category="installers" if dest.startswith("Inst") else "finance"))
    assert report.results[0].status == "skipped"
    assert (root / "setup.exe").read_bytes() == FAKE_EXE


def test_category_swap_in_plan_refused(tmp_path):
    root = tmp_path / "dl"
    write_old(root / "setup.exe", FAKE_EXE)
    report = apply_plan(tampered(root, category="finance"))  # dest still Installers/
    assert report.results[0].status == "skipped"


# C3 / C7: run ids are validated; undo never removes folders outside the category table
@pytest.mark.parametrize("run_id", ["..\\..\\evil", "C:\\x", "a/b", ""])
def test_bad_run_id_rejected(run_id):
    with pytest.raises(FileNotFoundError):
        undo_run(run_id)


def test_undo_ignores_forged_mkdir_records(tmp_path):
    root = tmp_path / "dl"
    victim = tmp_path / "UserEmptyFolder"
    victim.mkdir()
    write_old(root / "setup.exe", FAKE_EXE)
    report = apply_plan(plan_for(root))
    with open(executor.runs_dir() / f"{report.run_id}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"t": "mkdir", "path": "..\\UserEmptyFolder"}) + "\n")
        f.write(json.dumps({"t": "mkdir", "path": "Old stuff"}) + "\n")
    (root / "Old stuff").mkdir()
    undo_run(report.run_id)
    assert victim.is_dir() and (root / "Old stuff").is_dir()


# C4: user rename that collides with an existing file gets a unique name
def test_user_rename_collision_with_disk_is_made_unique(tmp_path):
    root = tmp_path / "dl"
    write_old(root / "setup.exe", FAKE_EXE)
    write_old(root / "Installers" / "mine.exe", b"existing")
    plan = apply_edits(plan_for(root), renames={"setup.exe": "mine"})
    assert plan.proposals[0].dest_rel == str(Path("Installers") / "mine (2).exe")
    assert apply_plan(plan).results[0].status == "moved"
    assert (root / "Installers" / "mine.exe").read_bytes() == b"existing"


# C5: applying one plan twice creates two independent, fully undoable runs
def test_reapplying_a_plan_keeps_runs_separate(tmp_path):
    root = tmp_path / "dl"
    write_old(root / "alpha.exe", FAKE_EXE + b"a")
    write_old(root / "beta.exe", FAKE_EXE + b"b")
    before = snapshot(root)
    plan = plan_for(root)
    first = apply_plan(apply_edits(plan, approved={"beta.exe"}))
    second = apply_plan(apply_edits(plan, approved={"alpha.exe", "beta.exe"}))
    assert first.run_id != second.run_id
    assert first.count("moved") == 1 and second.count("moved") == 1  # beta already moved: skipped
    undo_run(second.run_id)
    undo_run(first.run_id)
    assert snapshot(root) == before


# C6: a torn tail is healed so recovery completes and the folder is not locked forever
def test_torn_tail_during_crash_is_healed_by_recovery(tmp_path):
    root = tmp_path / "dl"
    write_old(root / "a.exe", FAKE_EXE)
    write_old(root / "b.mp3", b"ID3\x04" + b"\x00" * 50)
    before = snapshot(root)
    plan = plan_for(root)
    with pytest.raises(KeyboardInterrupt):
        apply_plan(plan, interrupt_after=1)
    [run_id] = incomplete_runs(root)
    with open(executor.runs_dir() / f"{run_id}.jsonl", "ab") as f:
        f.write(b'{"t": "intent", "op": 1, "src": "b.m' + b"\x00" * 16)  # torn + NULs
    recover(root)
    assert incomplete_runs(root) == []
    undo_run(run_id)
    assert snapshot(root) == before


# C8: a second undo does not remove an empty folder the user made afterwards
def test_repeat_undo_keeps_users_new_empty_folder(tmp_path):
    root = tmp_path / "dl"
    write_old(root / "setup.exe", FAKE_EXE)
    report = apply_plan(plan_for(root))
    undo_run(report.run_id)
    (root / "Installers").mkdir()
    undo_run(report.run_id)
    assert (root / "Installers").is_dir()


# C9: another spelling of the same folder (8.3 short name) still hits the recovery gate
@pytest.mark.skipif(not WINDOWS, reason="8.3 names are Windows-specific")
def test_short_path_spelling_is_same_folder(tmp_path):
    import ctypes
    root = tmp_path / "a long folder name for short paths"
    write_old(root / "a.exe", FAKE_EXE)
    write_old(root / "b.mp3", b"ID3\x04" + b"\x00" * 50)
    buf = ctypes.create_unicode_buffer(1024)
    ctypes.windll.kernel32.GetShortPathNameW(str(root), buf, 1024)
    short = buf.value
    if not short or short.lower() == str(root).lower():
        pytest.skip("8.3 names disabled on this volume")
    with pytest.raises(KeyboardInterrupt):
        apply_plan(plan_for(Path(short)), interrupt_after=1)
    assert incomplete_runs(root) != []
    with pytest.raises(executor.NeedsRecovery):
        apply_plan(plan_for(root))


# C10: among identical copies with equal mtimes, the plain name is the original
def test_duplicate_original_prefers_name_without_copy_marker(tmp_path):
    root = tmp_path / "dl"
    data = make_pdf(["Quarterly report", "numbers"])
    t = time.time() - 864000
    for n in ["report (1).pdf", "report - Copy.pdf", "report.pdf"]:
        p = write_old(root / n, data)
        os.utime(p, (t, t))
    plan = plan_for(root)
    dups = {p.rel_path for p in plan.proposals if p.category == "duplicates"}
    assert dups == {"report (1).pdf", "report - Copy.pdf"}


# C11: Lazyfold refuses to organize its own state folder
def test_state_folder_is_refused(home):
    (home / "plans").mkdir(parents=True, exist_ok=True)
    write_old(home / "plans" / "x.json", "{}")
    with pytest.raises(ValueError):
        scan_folder(home / "plans")


# C12: malformed model responses become ModelError (rules fallback), never a crash
@pytest.mark.parametrize("raw", ['{"category": ["finance"], "confidence": 1, "name": "", "reason": ""}',
                                 '{"category": {}, "confidence": "NaN", "name": 3, "reason": null}'])
def test_unhashable_model_category_rejected(raw):
    with pytest.raises(ModelError):
        parse_advice(raw)


def _garbage_server(body: bytes):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(200)
            self.send_header("Content-Length", str(len(body) + 50))  # promise more than we send
            self.end_headers()
            self.wfile.write(body)

    s = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s


@pytest.mark.parametrize("body", [b"\xff\xfe not utf8", b"[1, 2]", b'{"message": "str"}', b'{"message": {"con'])
def test_broken_model_server_raises_model_error(body):
    s = _garbage_server(body)
    try:
        client = OllamaClient(base_url=f"http://127.0.0.1:{s.server_address[1]}", timeout=5)
        from lazyfold.models import FileRecord
        rec = FileRecord("a.pdf", "a.pdf", ".pdf", 1, 1, "2026-01-01", text="invoice")
        with pytest.raises(ModelError):
            client.advise(rec)
    finally:
        s.shutdown()
        s.server_close()


# C14: bidi/zero-width/look-alike dots never reach a proposed name
def test_invisible_and_lookalike_characters_removed():
    s = pathsafe.sanitize_stem("Acme Invoice \u202etxt\uff0eexe\u200b")
    assert "\u202e" not in s and "\u200b" not in s and "\uff0e" not in s and "." not in s


# C15: control characters are escaped before printing to the terminal
def test_cli_escapes_ansi():
    assert "\x1b" not in _safe("evil\x1b[2Kname")


def test_nan_confidence_falls_back_to_rules():
    with pytest.raises(ModelError):
        parse_advice('{"category": "finance", "confidence": NaN, "name": "x", "reason": "y"}')


# C13: the folder picker runs Python isolated (-I) from the temp dir, never from the cwd
def test_folder_picker_is_isolated(tmp_path, monkeypatch):
    import http.client
    from http.server import ThreadingHTTPServer as Srv
    from lazyfold import web

    seen = {}

    class Done:
        stdout = str(tmp_path) + "\n"

    def fake_run(args, **kw):
        seen["args"], seen["cwd"] = args, kw.get("cwd")
        return Done()

    monkeypatch.setattr(web.subprocess, "run", fake_run)
    app = web.App(None)
    httpd = Srv(("127.0.0.1", 0), web.make_handler(app))
    port = httpd.server_address[1]
    app.allowed_hosts = {f"127.0.0.1:{port}"}
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", port)
        c.request("POST", "/api/pick-folder", body="{}",
                  headers={"Content-Type": "application/json", "X-Lazyfold-Token": app.token})
        body = json.loads(c.getresponse().read())
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert seen["args"][1] == "-I"
    assert os.path.normcase(seen["cwd"]) == os.path.normcase(__import__("tempfile").gettempdir())
    assert body["folder"] == os.path.normpath(str(tmp_path))
