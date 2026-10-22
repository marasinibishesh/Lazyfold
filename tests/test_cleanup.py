"""Storage Cleanup: every test works on fake locations under tmp_path. The real
Recycle Bin is never emptied (the shell function is replaced for the whole
module), and the single real recycle test restores its own file."""

import json
import os
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from slothfile import cleanup, winshell
from slothfile.cleanup import Location, clean, load_report, scan_cleanup, temp_locations
from slothfile.journal import read_journal, state_dir
from tests.conftest import write_old

windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows shell API")


@pytest.fixture(autouse=True)
def no_real_side_effects(monkeypatch):
    """No test may empty the real bin or depend on what's running on this PC."""
    calls = []

    def fake_empty(hwnd, root, flags):
        calls.append(flags)
        return 0

    monkeypatch.setattr(winshell, "_SHEmptyRecycleBinW", fake_empty)
    monkeypatch.setattr(winshell, "running_processes", lambda: set())
    return calls


def make_temp(tmp_path):
    root = tmp_path / "Temp"
    files = {
        "old.tmp": write_old(root / "old.tmp", "x" * 10),
        "setup.log": write_old(root / "setup.log", "log" * 5),
        "crash.dmp": write_old(root / "crash.dmp", "d" * 30),
        "lock": write_old(root / "~$report.docx", "o"),
        "fresh.tmp": write_old(root / "fresh.tmp", "new", days=0),
        "inst": write_old(root / "pkg" / "setup.exe", "MZ" * 50, days=10),
        "young_inst": write_old(root / "pkg" / "young.msi", "msi", days=2),
        "top_exe": write_old(root / "tool.exe", "MZ", days=30),
        "pdf": write_old(root / "invoice.pdf", "%PDF", days=30),
        "txt": write_old(root / "notes.txt", "my notes", days=30),
        "tilde_doc": write_old(root / "~draft.docx", "doc", days=30),
        "unknown": write_old(root / "thing.xyz", "?", days=30),
    }
    return root, files


def cat(report, key):
    return next(c for c in report.categories if c.key == key)


def paths(category):
    return {Path(c.path).name for c in category.files}


def test_candidates_by_type_and_age(tmp_path):
    root, files = make_temp(tmp_path)
    rep = scan_cleanup(temp_locations((root,)))
    assert paths(cat(rep, "temp_files")) == {"old.tmp", "~$report.docx"}
    assert paths(cat(rep, "temp_logs")) == {"setup.log"}
    assert paths(cat(rep, "temp_dumps")) == {"crash.dmp"}
    assert paths(cat(rep, "temp_installers")) == {"setup.exe"}
    assert cat(rep, "temp_installers").selected_by_default is False
    assert rep.stats["too_new"] == 2  # fresh.tmp and young.msi
    kept = {Path(p.path).name: p.reason for p in rep.protected}
    assert kept == {"invoice.pdf": cleanup.KEPT_PERSONAL, "notes.txt": cleanup.KEPT_PERSONAL,
                    "~draft.docx": cleanup.KEPT_PERSONAL, "tool.exe": cleanup.KEPT_UNKNOWN,
                    "thing.xyz": cleanup.KEPT_UNKNOWN}
    assert cat(rep, "temp_files").total_bytes == 11


def test_protected_files_are_never_cleaned(tmp_path):
    root, files = make_temp(tmp_path)
    locs = temp_locations((root,))
    rep = scan_cleanup(locs)
    res = clean(rep.report_id, [l.key for l in locs], mode="delete", confirm_permanent=True, locations=locs)
    assert res.removed_count == 5 and not res.errors
    for k in ("pdf", "txt", "tilde_doc", "unknown", "top_exe", "fresh.tmp", "young_inst"):
        assert files[k].exists(), k
    for k in ("old.tmp", "setup.log", "crash.dmp", "lock", "inst"):
        assert not files[k].exists(), k


def test_tampered_report_cannot_add_personal_files(tmp_path):
    root, files = make_temp(tmp_path)
    locs = temp_locations((root,))
    rep = scan_cleanup(locs)
    path = state_dir() / "cleanup" / f"{rep.report_id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    pdf = files["pdf"]
    st = os.stat(pdf)
    outside = write_old(tmp_path / "Documents" / "x.tmp", "precious")
    for target in (pdf, outside):
        s = os.stat(target)
        data["categories"][0]["files"].append(
            {"path": str(target), "size": s.st_size, "mtime_ns": s.st_mtime_ns, "root": str(root)})
    path.write_text(json.dumps(data), encoding="utf-8")
    res = clean(rep.report_id, ["temp_files"], mode="delete", confirm_permanent=True, locations=locs)
    assert pdf.exists() and outside.exists()
    reasons = dict(res.skipped)
    assert reasons[str(pdf)] == "not a file type this category may remove"
    assert reasons[str(outside)] == "outside its cleanup folder"
    assert st.st_size == os.stat(pdf).st_size


@windows_only
def test_junctions_are_not_followed(tmp_path):
    root = tmp_path / "Temp"
    root.mkdir()
    outside = tmp_path / "Personal"
    victim = write_old(outside / "important.tmp", "keep me")
    write_old(root / "real.tmp", "x")
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(root / "junc"), str(outside)], capture_output=True)
    assert r.returncode == 0, r.stderr
    locs = temp_locations((root,))
    rep = scan_cleanup(locs)
    assert paths(cat(rep, "temp_files")) == {"real.tmp"}
    assert rep.stats["links_skipped"] >= 1
    clean(rep.report_id, [l.key for l in locs], mode="delete", confirm_permanent=True, locations=locs)
    assert victim.read_text() == "keep me"
    os.rmdir(root / "junc")  # removes the junction only


@windows_only
def test_junction_as_location_root_is_refused(tmp_path):
    target = tmp_path / "Personal"
    write_old(target / "a.tmp", "x")
    link = tmp_path / "Temp"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, check=True)
    rep = scan_cleanup(temp_locations((link,)))
    assert rep.categories == []
    os.rmdir(link)


def test_changed_after_scan_is_skipped(tmp_path):
    root, files = make_temp(tmp_path)
    locs = temp_locations((root,))
    rep = scan_cleanup(locs)
    files["old.tmp"].write_text("changed!!!!!")
    t = time.time() - 10 * 86400
    os.utime(files["old.tmp"], (t, t))
    res = clean(rep.report_id, ["temp_files"], mode="delete", confirm_permanent=True, locations=locs)
    assert (str(files["old.tmp"]), "changed since scan") in res.skipped
    assert files["old.tmp"].exists()


@windows_only
@pytest.mark.parametrize("mode", ["delete", "recycle"])
def test_locked_file_is_skipped(tmp_path, monkeypatch, mode):
    root, files = make_temp(tmp_path)
    locs = temp_locations((root,))
    rep = scan_cleanup(locs)
    real_recycle = winshell.recycle

    def guarded(ps):
        assert str(files["old.tmp"]) not in map(str, ps), "locked file must be skipped before the shell call"
        return {str(p): "simulated" for p in ps}  # never actually recycle in this test

    monkeypatch.setattr(winshell, "recycle", guarded)
    with open(files["old.tmp"], "rb"):
        res = clean(rep.report_id, ["temp_files"], mode=mode, confirm_permanent=(mode == "delete"),
                    locations=locs)
    assert (str(files["old.tmp"]), "in use by another program") in res.skipped
    assert files["old.tmp"].exists()
    assert real_recycle is not guarded


def test_blocked_by_running_process(tmp_path, monkeypatch):
    cache = tmp_path / "Edge" / "Cache_Data"
    f = write_old(cache / "data_0", b"\0" * 100)
    loc = Location("edge_cache", "Edge cache", "why", (cache,), cleanup._any_file, min_age_hours=0,
                   blocked_by_processes=frozenset({"msedge.exe"}), app_name="Microsoft Edge")
    monkeypatch.setattr(winshell, "running_processes", lambda: {"msedge.exe", "python.exe"})
    rep = scan_cleanup([loc])
    assert cat(rep, "edge_cache").blocked_reason == "Microsoft Edge is running - close Microsoft Edge first"
    res = clean(rep.report_id, ["edge_cache"], mode="delete", confirm_permanent=True, locations=[loc])
    assert f.exists() and res.removed_count == 0
    assert "close Microsoft Edge first" in res.skipped[0][1]
    # Closed at scan time but reopened before cleaning: still blocked.
    monkeypatch.setattr(winshell, "running_processes", lambda: set())
    rep2 = scan_cleanup([loc])
    assert cat(rep2, "edge_cache").blocked_reason is None
    monkeypatch.setattr(winshell, "running_processes", lambda: {"msedge.exe"})
    res2 = clean(rep2.report_id, ["edge_cache"], mode="delete", confirm_permanent=True, locations=[loc])
    assert f.exists() and res2.removed_count == 0
    # Can't tell what's running: blocked rather than guessed.

    def broken():
        raise OSError("tasklist failed")

    monkeypatch.setattr(winshell, "running_processes", broken)
    res3 = clean(rep2.report_id, ["edge_cache"], mode="delete", confirm_permanent=True, locations=[loc])
    assert f.exists() and "could not check" in res3.skipped[0][1]


def test_delete_requires_confirmation_and_is_journaled(tmp_path):
    root, files = make_temp(tmp_path)
    locs = temp_locations((root,))
    rep = scan_cleanup(locs)
    with pytest.raises(ValueError):
        clean(rep.report_id, ["temp_files"], mode="delete", locations=locs)
    with pytest.raises(ValueError):
        clean(rep.report_id, ["temp_files"], mode="delete", confirm_permanent="yes", locations=locs)
    with pytest.raises(ValueError):
        clean(rep.report_id, ["temp_files"], mode="shred", locations=locs)
    assert files["old.tmp"].exists()
    res = clean(rep.report_id, ["temp_files", "temp_logs"], mode="delete", confirm_permanent=True,
                exclude_paths={str(files["setup.log"])}, locations=locs)
    assert res.removed_count == 2 and res.removed_bytes == 11
    assert files["setup.log"].exists()  # unticked by the user
    recs = read_journal(Path(res.journal))
    assert recs[0]["t"] == "begin" and recs[-1]["t"] == "end"
    removed = [r for r in recs if r["t"] == "removed"]
    assert {Path(r["path"]).name for r in removed} == {"old.tmp", "~$report.docx"}
    assert all(r["mode"] == "delete" for r in removed)
    # A second run on the same report gets its own journal; nothing left to remove.
    res2 = clean(rep.report_id, ["temp_files"], mode="delete", confirm_permanent=True, locations=locs)
    assert res2.journal != res.journal and res2.removed_count == 0
    assert all(r == "already gone" for _, r in res2.skipped)


@windows_only
def test_recycle_and_restore_real_file(tmp_path):
    root = tmp_path / "Temp"
    f = write_old(root / f"slothfile-test-{uuid.uuid4().hex[:8]}.tmp", "recycle me")
    locs = temp_locations((root,))
    rep = scan_cleanup(locs)
    res = clean(rep.report_id, ["temp_files"], mode="recycle", locations=locs)
    assert res.removed_count == 1 and not res.errors, res.to_dict()
    assert not f.exists()
    try:
        assert winshell.restore_from_recycle_bin(f), f"could not restore {f} from the Recycle Bin"
        assert f.read_text() == "recycle me"
    finally:
        if f.exists():
            os.remove(f)


@windows_only
def test_recycle_refuses_links_and_folders(tmp_path):
    d = tmp_path / "dir"
    d.mkdir()
    res = winshell.recycle([d, tmp_path / "missing.tmp"])
    assert all(v is not None for v in res.values())
    assert d.exists()


def test_empty_recycle_bin_needs_confirmation(no_real_side_effects, monkeypatch):
    monkeypatch.setattr(winshell, "recycle_bin_info", lambda: (5000, 3))
    for bad in (False, None, "true", 1):
        with pytest.raises(ValueError):
            cleanup.empty_recycle_bin_confirmed(bad)
    assert no_real_side_effects == []
    if os.name != "nt":
        return
    out = cleanup.empty_recycle_bin_confirmed(True)
    assert no_real_side_effects == [0x7]  # the fake, never the real shell32 function
    assert out["bytes_before"] == 5000 and out["items_before"] == 3
    recs = read_journal(state_dir() / "cleanup" / "recycle-bin.jsonl")
    assert recs[-1]["t"] == "emptied_recycle_bin" and recs[-1]["items"] == 3


@windows_only
def test_empty_recycle_bin_hresults(monkeypatch):
    monkeypatch.setattr(winshell, "_SHEmptyRecycleBinW", lambda *a: winshell.E_UNEXPECTED)
    winshell.empty_recycle_bin()  # already empty is fine
    monkeypatch.setattr(winshell, "_SHEmptyRecycleBinW", lambda *a: -2147024891)  # E_ACCESSDENIED
    with pytest.raises(OSError):
        winshell.empty_recycle_bin()


def test_report_saved_and_reloaded(tmp_path):
    root, _ = make_temp(tmp_path)
    rep = scan_cleanup(temp_locations((root,)))
    again = load_report(rep.report_id)
    assert json.dumps(again.full_dict()) == json.dumps(rep.full_dict())
    assert again.categories[0].files[0].mtime_ns == rep.categories[0].files[0].mtime_ns
    for bad in ("../x", "a/b", "", "x" * 81, "a.b"):
        with pytest.raises(FileNotFoundError):
            load_report(bad)
    assert not list((state_dir() / "cleanup").glob("*.tmp"))


def test_to_dict_is_small(tmp_path):
    root = tmp_path / "Temp"
    for i in range(300):
        write_old(root / f"f{i:03}.tmp", "x" * i)
    rep = scan_cleanup(temp_locations((root,)))
    d = rep.to_dict()
    tf = next(c for c in d["categories"] if c["key"] == "temp_files")
    assert tf["count"] == 300 and len(tf["largest"]) == 20
    assert tf["largest"][0]["size"] == 299
    assert d["reclaimable_bytes"] == sum(range(300))
    assert len(json.dumps(d)) < 20_000


def test_category_cap_reports_truncation(tmp_path):
    root = tmp_path / "Temp"
    for i in range(12):
        write_old(root / f"f{i}.tmp", "x")
    rep = scan_cleanup(temp_locations((root,)), max_files=5)
    c = cat(rep, "temp_files")
    assert len(c.files) == 5 and c.truncated


def test_unsafe_roots_refused(tmp_path, monkeypatch):
    loc = Location("x", "x", "x", (Path.home(),), cleanup._any_file)
    assert cleanup.resolve_roots(loc) == []
    monkeypatch.setenv("TEMP", str(tmp_path / "Documents"))
    monkeypatch.setenv("TMP", str(tmp_path / "Documents"))
    (tmp_path / "Documents").mkdir()
    assert cleanup.resolve_roots(temp_locations()[0]) == []


def test_default_locations_resolve_without_touching_anything():
    keys = [l.key for l in cleanup.default_locations()]
    assert len(keys) == len(set(keys))
    for loc in cleanup.default_locations():
        for r in cleanup.resolve_roots(loc):
            assert cleanup._root_refusal(r) is None and not winshell.is_link_like(r)


# --------------------------------------------------------------- regressions

def _raw(p: Path) -> str:
    return "\\\\?\\" + str(p)


@windows_only
def test_trailing_space_twin_never_reaches_protected_file(tmp_path, monkeypatch):
    r"""'~report.pdf ' (only creatable via \\?\) looks like a temp file, but Win32
    calls on it act on the protected '~report.pdf'."""
    def no_shell(*a):
        raise AssertionError("the shell must not be asked to recycle a twin name")
    monkeypatch.setattr(winshell, "_SHFileOperationW", no_shell)
    root = tmp_path / "Temp"
    personal = write_old(root / "~report.pdf", "PERSONAL DATA", days=30)
    twins = [root / "~report.pdf ", root / "~report.pdf."]
    try:
        for t in twins:
            with open(_raw(t), "wb") as f:
                f.write(b"decoy")
            old = time.time() - 30 * 86400
            os.utime(_raw(t), (old, old))
        locs = temp_locations((root,))
        rep = scan_cleanup(locs)
        assert all(not c.files for c in rep.categories)
        kept = {Path(p.path).name if not p.path.endswith((" ", ".")) else os.path.basename(p.path): p.reason
                for p in rep.protected}
        assert kept["~report.pdf"] == cleanup.KEPT_PERSONAL
        assert sorted(r for r in kept.values() if r == cleanup.KEPT_UNUSUAL) == [cleanup.KEPT_UNUSUAL] * 2
        # A tampered report that lists the twin is refused at re-check time.
        tc = cat(rep, "temp_files") if any(c.key == "temp_files" for c in rep.categories) else None
        if tc is None:
            tc = cleanup.Category("temp_files", "t", "w", [str(root)], True, None, 24)
            rep.categories.append(tc)
        st = os.lstat(personal)
        for t in twins:
            tc.files.append(cleanup.Candidate(os.path.join(str(root), t.name), st.st_size, st.st_mtime_ns,
                                              str(cleanup.canonical(root))))
        cleanup.save_report(rep)
        res = clean(rep.report_id, ["temp_files"], mode="delete", confirm_permanent=True, locations=locs)
        assert res.removed_count == 0
        assert {r for _, r in res.skipped} == {"unusual name"}
        assert personal.read_text() == "PERSONAL DATA"
        # winshell refuses the twin names on its own, too.
        for t in twins:
            twin = os.path.join(str(root), t.name)
            with pytest.raises(winshell.UnsafePath):
                winshell.delete_permanently(Path(twin))
            with pytest.raises(winshell.UnsafePath):
                winshell.delete_permanently(twin)
            assert winshell.recycle([twin])[twin] == "name ends in a space or dot; refusing"
        assert personal.read_text() == "PERSONAL DATA"
    finally:
        for t in twins:
            try:
                os.remove(_raw(t))
            except OSError:
                pass


def test_twin_name_helper():
    assert winshell._twin_name(r"C:\x\a.pdf ") and winshell._twin_name(r"C:\x\a.")
    assert winshell._twin_name("C:\\x\\a.pdf\\") and winshell._twin_name("C:/x/a ")
    assert not winshell._twin_name(r"C:\x\a.pdf") and not winshell._twin_name(Path("C:/x/.gitignore"))


@pytest.mark.parametrize("arg", ["exclude_paths", "category_keys"])
@pytest.mark.parametrize("bad", ["STR", b"bytes", [b"x"], [["x"]], [None], [1], None])
def test_clean_rejects_string_and_non_string_items(tmp_path, arg, bad):
    root = tmp_path / "Temp"
    a = write_old(root / "a.tmp", "x", days=30)
    locs = temp_locations((root,))
    rep = scan_cleanup(locs)
    value = str(a) if bad == "STR" else bad
    kwargs = {"category_keys": ["temp_files"], "exclude_paths": [str(a)]}
    kwargs[arg] = value
    with pytest.raises(TypeError):
        clean(rep.report_id, mode="delete", confirm_permanent=True, locations=locs, **kwargs)
    assert a.exists()


def test_clean_accepts_path_objects_in_exclude(tmp_path):
    root = tmp_path / "Temp"
    a = write_old(root / "a.tmp", "x", days=30)
    b = write_old(root / "b.tmp", "x", days=30)
    locs = temp_locations((root,))
    rep = scan_cleanup(locs)
    res = clean(rep.report_id, ("temp_files",), mode="delete", confirm_permanent=True,
                exclude_paths=[a], locations=locs)
    assert a.exists() and not b.exists() and res.removed_count == 1


@windows_only
def test_junction_in_parent_of_root_is_refused(tmp_path, monkeypatch):
    elsewhere = tmp_path / "Documents"
    victim = write_old(elsewhere / "DXCache" / "thesis.docx", "mine", days=30)
    la = tmp_path / "LocalAppData"
    la.mkdir()
    subprocess.run(["cmd", "/c", "mklink", "/J", str(la / "NVIDIA"), str(elsewhere)],
                   check=True, capture_output=True)
    try:
        concrete = Location("shader", "Shader", "why", (la / "NVIDIA" / "DXCache",), cleanup._any_file)
        assert cleanup.resolve_roots(concrete) == []
        monkeypatch.setenv("LOCALAPPDATA", str(la))
        templ = Location("shader", "Shader", "why", (r"%LOCALAPPDATA%\NVIDIA\DXCache",), cleanup._any_file)
        assert cleanup.resolve_roots(templ) == []
        rep = scan_cleanup([concrete, templ])
        assert rep.categories == []
        # A real folder below the same base still works.
        real = write_old(la / "D3DSCache" / "x.bin", "x", days=30).parent
        ok = Location("d3d", "D3D", "why", (r"%LOCALAPPDATA%\D3DSCache",), cleanup._any_file)
        assert cleanup.resolve_roots(ok) == [cleanup.canonical(real)]
    finally:
        os.rmdir(la / "NVIDIA")  # removes the junction only
    assert victim.read_text() == "mine"


@windows_only
def test_redirected_env_base_itself_is_trusted(tmp_path, monkeypatch):
    target = tmp_path / "RealLocal"
    write_old(target / "CrashDumps" / "a.dmp", "d", days=30)
    link = tmp_path / "LinkLocal"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
    try:
        monkeypatch.setenv("LOCALAPPDATA", str(link))
        loc = Location("crashdumps", "x", "x", (r"%LOCALAPPDATA%\CrashDumps",), cleanup._is_dump)
        assert cleanup.resolve_roots(loc) == [cleanup.canonical(target / "CrashDumps")]
    finally:
        os.rmdir(link)


def test_tilde_installer_files_are_installer_leftovers(tmp_path):
    root = tmp_path / "Temp"
    for n in ("~setup.exe", "~core.dll", "~pkg.msi", "~upd.msp", "~data.cab"):
        write_old(root / "sub" / n, "MZ", days=10)
    write_old(root / "~top.exe", "MZ", days=30)
    write_old(root / "sub" / "~young.exe", "MZ", days=2)
    write_old(root / "sub" / "~scratch.bin", "x", days=2)
    rep = scan_cleanup(temp_locations((root,)))
    assert paths(cat(rep, "temp_installers")) == {"~setup.exe", "~core.dll", "~pkg.msi", "~upd.msp", "~data.cab"}
    assert paths(cat(rep, "temp_files")) == {"~scratch.bin"}
    kept = {Path(p.path).name: p.reason for p in rep.protected}
    assert kept == {"~top.exe": cleanup.KEPT_UNKNOWN}
