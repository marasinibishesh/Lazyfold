"""macOS feature paths (launchd, Finder Quick Action, cleanup locations,
notifications, single-instance lock, Ollama setup, UI platform flag).

Everything here runs on any OS: sys.platform, Path.home, subprocess and the
macos module are replaced, so no real launchctl, Finder or Homebrew is used.
"""

import os
import plistlib
import shlex
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

import slothfile
from conftest import write_old
from slothfile import ai_setup, automation, cleanup, desktop, executor, web, winshell


@pytest.fixture
def mac(monkeypatch, tmp_path):
    """Pretend to be macOS with a throwaway home folder."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    return home


@pytest.fixture
def fake_macos(monkeypatch):
    """A stand-in for slothfile/macos.py (the CORE agent's module)."""
    mod = types.ModuleType("slothfile.macos")
    mod.IS_MAC = True
    mod.calls = []

    def notify(title, body):
        mod.calls.append((title, body))
        return True

    mod.notify = notify
    monkeypatch.setitem(sys.modules, "slothfile.macos", mod)
    monkeypatch.setattr(slothfile, "macos", mod, raising=False)
    return mod


class Launchctl:
    """Records launchctl calls; `fail` lists subcommands that return 1."""

    def __init__(self, fail=()):
        self.calls, self.fail = [], set(fail)

    def __call__(self, args, **kw):
        assert "shell" not in kw and isinstance(args, list)
        self.calls.append(args)
        code = 1 if len(args) > 1 and args[1] in self.fail else 0
        return subprocess.CompletedProcess(args, code, stdout="", stderr="denied" if code else "")


@pytest.fixture
def launchctl(mac, monkeypatch):
    lc = Launchctl()
    monkeypatch.setattr(automation.subprocess, "run", lc)
    monkeypatch.setattr(automation, "_uid", lambda: 501)
    return lc


def agents(home):
    return home / "Library" / "LaunchAgents"


# --------------------------------------------------------------------------
# launchd


def test_daily_plist_content_and_bootstrap(tmp_path, launchctl, mac):
    folder = tmp_path / "Downloads"
    folder.mkdir()
    label = automation.schedule_daily(folder, "07:30")
    assert label == automation.tidy_task_name(folder) and label.startswith("com.slothfile.tidy-")
    path = agents(mac) / f"{label}.plist"
    data = plistlib.loads(path.read_bytes())
    canon = str(executor.canonical(folder))
    assert data["Label"] == label
    assert data["ProgramArguments"] == [sys.executable, "-m", "slothfile", "auto", canon]
    assert isinstance(data["ProgramArguments"], list)
    assert data["StartCalendarInterval"] == {"Hour": 7, "Minute": 30}
    assert data["RunAtLoad"] is False
    assert launchctl.calls == [["/bin/launchctl", "bootout", "gui/501", str(path)],
                               ["/bin/launchctl", "bootstrap", "gui/501", str(path)]]


def test_weekly_plist_calendar(launchctl, mac):
    label = automation.schedule_weekly_summary("mon", "18:05")
    assert label == "com.slothfile.weekly-summary"
    data = plistlib.loads((agents(mac) / f"{label}.plist").read_bytes())
    assert data["ProgramArguments"][-2:] == ["summary", "--notify"]
    assert data["StartCalendarInterval"] == {"Weekday": 1, "Hour": 18, "Minute": 5}
    automation.schedule_weekly_summary("SUN", "09:00")
    data = plistlib.loads((agents(mac) / f"{label}.plist").read_bytes())
    assert data["StartCalendarInterval"] == {"Weekday": 0, "Hour": 9, "Minute": 0}


def test_folder_with_shell_characters_stays_one_argument(tmp_path, launchctl, mac):
    folder = tmp_path / "a $(touch pwned) ; b 'c' & `d`"
    folder.mkdir()
    label = automation.schedule_daily(folder, "20:00")
    data = plistlib.loads((agents(mac) / f"{label}.plist").read_bytes())
    assert data["ProgramArguments"][-1] == str(executor.canonical(folder))


def test_bootstrap_falls_back_to_load_then_fails_cleanly(tmp_path, mac, monkeypatch):
    folder = tmp_path / "D"
    folder.mkdir()
    monkeypatch.setattr(automation, "_uid", lambda: 501)
    lc = Launchctl(fail={"bootstrap"})
    monkeypatch.setattr(automation.subprocess, "run", lc)
    label = automation.schedule_daily(folder)
    path = str(agents(mac) / f"{label}.plist")
    assert lc.calls[-1] == ["/bin/launchctl", "load", "-w", path]
    lc2 = Launchctl(fail={"bootstrap", "load"})
    monkeypatch.setattr(automation.subprocess, "run", lc2)
    with pytest.raises(RuntimeError, match="denied"):
        automation.schedule_daily(folder)
    assert not os.path.exists(path)  # nothing half-installed is left behind


def test_schedule_rejects_bad_time_on_mac(tmp_path, launchctl, mac):
    for at in ["7:30", "24:00", "noon", "12:00 && calc"]:
        with pytest.raises(ValueError):
            automation.schedule_daily(tmp_path, at)
    with pytest.raises(ValueError):
        automation.schedule_weekly_summary("FUNDAY")
    assert launchctl.calls == [] and not agents(mac).exists()


def test_list_and_remove_only_touch_our_plists(launchctl, mac):
    d = agents(mac)
    d.mkdir(parents=True)
    ours = {"Label": "com.slothfile.tidy-1234abcd", "ProgramArguments": ["/x/SlothFileCLI", "auto", "/Users/me/D"],
            "StartCalendarInterval": {"Hour": 20, "Minute": 0}}
    (d / "com.slothfile.tidy-1234abcd.plist").write_bytes(plistlib.dumps(ours))
    foreign = d / "com.apple.thing.plist"
    foreign.write_bytes(plistlib.dumps({"Label": "com.apple.thing", "ProgramArguments": ["/bin/true"]}))
    (d / "com.slothfile.Bad.Name.plist").write_bytes(plistlib.dumps({"Label": "com.slothfile.Bad.Name"}))
    (d / "com.slothfile.mismatch.plist").write_bytes(plistlib.dumps({"Label": "com.other.x"}))
    (d / "com.slothfile.broken.plist").write_bytes(b"not a plist")

    tasks = automation.list_tasks()
    assert [t["name"] for t in tasks] == ["com.slothfile.tidy-1234abcd"]
    assert tasks[0]["command"] == "/x/SlothFileCLI auto /Users/me/D"
    assert len(tasks[0]["next_run"]) == 16 and tasks[0]["next_run"].endswith("20:00")

    for bad in ["com.apple.thing", "com.slothfile.../../x", "com.slothfile.../com.apple.thing",
                "com.slothfile.", "com.slothfile.a/b", "com.slothfile.a.b", "../com.slothfile.x", "Tidy", 5]:
        with pytest.raises(ValueError):
            automation.remove_task(bad)
    assert launchctl.calls == [] and foreign.exists()

    automation.remove_task("com.slothfile.tidy-1234abcd")
    path = str(d / "com.slothfile.tidy-1234abcd.plist")
    assert launchctl.calls == [["/bin/launchctl", "bootout", "gui/501", path]]
    assert not os.path.exists(path) and foreign.exists()
    with pytest.raises(RuntimeError):
        automation.remove_task("com.slothfile.tidy-1234abcd")  # already gone


def test_schedule_clear_removes_only_ours(launchctl, mac, capsys):
    from slothfile import cli
    d = agents(mac)
    d.mkdir(parents=True)
    for leaf in ("tidy-aaaa0000", "weekly-summary"):
        (d / f"com.slothfile.{leaf}.plist").write_bytes(plistlib.dumps(
            {"Label": f"com.slothfile.{leaf}", "ProgramArguments": ["x"], "StartCalendarInterval": {"Hour": 1, "Minute": 2}}))
    (d / "org.example.keep.plist").write_bytes(plistlib.dumps({"Label": "org.example.keep"}))
    assert cli.main(["schedule", "clear"]) == 0
    assert sorted(os.listdir(d)) == ["org.example.keep.plist"]


def test_list_tasks_without_launch_agents_folder(launchctl, mac):
    assert automation.list_tasks() == []


def test_frozen_app_uses_cli_and_gui_executables(tmp_path, monkeypatch, mac):
    macos_dir = tmp_path / "Sloth File's" / "SlothFile.app" / "Contents" / "MacOS"
    macos_dir.mkdir(parents=True)
    for exe in ("SlothFile", "SlothFileCLI"):
        (macos_dir / exe).write_bytes(b"\xcf\xfa\xed\xfe")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(macos_dir / "SlothFile"))
    assert automation.gui_python() == str(macos_dir / "SlothFile")
    assert automation.cli_argv() == [str(macos_dir / "SlothFileCLI")]
    assert automation.launch_argv() == [str(macos_dir / "SlothFile")]
    cmd = automation.menu_command()
    assert shlex.split(cmd) == [str(macos_dir / "SlothFile"), "ui", "$1"]  # quoted path survives the shell
    assert automation.launch_prefix() == shlex.quote(str(macos_dir / "SlothFile"))


def test_source_checkout_on_mac_uses_python_module(mac):
    assert automation.cli_argv() == [sys.executable, "-m", "slothfile"]
    assert automation.launch_argv()[1:] == ["-m", "slothfile"]


# --------------------------------------------------------------------------
# Finder Quick Action


@pytest.fixture
def services(mac, monkeypatch):
    calls = []
    monkeypatch.setattr(automation.subprocess, "run",
                        lambda args, **kw: calls.append(args) or subprocess.CompletedProcess(args, 0))
    return calls


def test_quick_action_bundle_structure(services, mac):
    automation.install_context_menu()
    bundle = mac / "Library" / "Services" / "Tidy with SlothFile.workflow"
    assert sorted(p.name for p in (bundle / "Contents").iterdir()) == ["Info.plist", "document.wflow"]
    info = plistlib.loads((bundle / "Contents" / "Info.plist").read_bytes())
    svc, = info["NSServices"]
    assert svc["NSMenuItem"] == {"default": "Tidy with SlothFile"}
    assert svc["NSMessage"] == "runWorkflowAsService"
    assert svc["NSSendFileTypes"] == ["public.folder"]
    assert svc["NSRequiredContext"] == {"NSApplicationIdentifier": "com.apple.finder"}

    doc = plistlib.loads((bundle / "Contents" / "document.wflow").read_bytes())
    assert doc["AMApplicationBuild"] and doc["AMDocumentVersion"] == "2"
    meta = doc["workflowMetaData"]
    assert meta["workflowTypeIdentifier"] == "com.apple.Automator.servicesMenu"
    assert meta["serviceInputTypeIdentifier"] == "com.apple.Automator.fileSystemObject.folder"
    assert meta["serviceApplicationBundleID"] == "com.apple.finder"
    (entry,) = doc["actions"]
    action = entry["action"]
    assert action["BundleIdentifier"] == "com.apple.RunShellScript"
    params = action["ActionParameters"]
    assert params["inputMethod"] == 1  # "as arguments": the folder is "$1", not stdin text
    assert params["shell"] == "/bin/sh"
    assert automation.context_menu_installed()
    assert ["/System/Library/CoreServices/pbs", "-flush"] in services


def test_quick_action_passes_folder_as_argument_not_text(services, mac):
    automation.install_context_menu()
    doc = plistlib.loads((mac / "Library" / "Services" / "Tidy with SlothFile.workflow" / "Contents"
                          / "document.wflow").read_bytes())
    script = doc["actions"][0]["action"]["ActionParameters"]["COMMAND_STRING"]
    assert script == automation.workflow_script()
    run_line = [line for line in script.splitlines() if " ui " in line][0]
    tokens = shlex.split(run_line)
    assert tokens[:3] == [sys.executable, "-m", "slothfile"]
    assert tokens[3:5] == ["ui", "$1"]  # "$1" double-quoted: one argument, never word-split
    assert '"$1"' in run_line and "$@" not in script and "eval" not in script
    assert run_line.rstrip().endswith("&")  # Finder is not kept waiting while the window is open


def test_quick_action_uninstall_and_foreign_bundle(services, mac):
    automation.install_context_menu()
    automation.install_context_menu()  # reinstall replaces our own bundle
    assert automation.uninstall_context_menu() == []
    bundle = mac / "Library" / "Services" / "Tidy with SlothFile.workflow"
    assert not bundle.exists() and not automation.context_menu_installed()
    (bundle / "Contents").mkdir(parents=True)
    (bundle / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"NSServices": []}))  # someone else's
    assert automation.uninstall_context_menu() == [str(bundle)]
    with pytest.raises(RuntimeError):
        automation.install_context_menu()
    assert (bundle / "Contents" / "Info.plist").exists() and not automation.context_menu_installed()


def test_run_alias_is_a_no_op_on_mac(mac, monkeypatch):
    monkeypatch.setattr(automation, "_winreg", lambda: pytest.fail("registry used on macOS"))
    automation.install_run_alias()
    automation.uninstall_run_alias()
    assert automation.run_alias_installed() is False
    assert not (mac / "Library").exists()
    assert "Spotlight" in automation.RUN_ALIAS_MAC_NOTE


# --------------------------------------------------------------------------
# notifications


def test_notify_goes_through_macos(mac, fake_macos, monkeypatch):
    monkeypatch.setattr(automation.subprocess, "run", lambda *a, **k: pytest.fail("PowerShell used on macOS"))
    assert automation.notify("Title", "Body‮ text\n") is True
    assert fake_macos.calls == [("Title", "Body text")]  # control/bidi characters dropped first


def test_notify_never_raises_on_mac(mac, fake_macos):
    def boom(t, b):
        raise OSError("osascript missing")

    fake_macos.notify = boom
    assert automation.notify("a", "b") is False
    fake_macos.notify = lambda t, b: False
    assert automation.notify("a", "b") is False


# --------------------------------------------------------------------------
# cleanup


def _old(path: Path, data: bytes = b"x" * 10) -> Path:
    return write_old(path, data, days=3)


@pytest.fixture
def mac_tree(mac, tmp_path, monkeypatch):
    h = mac
    files = {
        "crash": _old(h / "Library/Logs/DiagnosticReports/Finder-2026-10-01.ips"),
        "log": _old(h / "Library/Logs/SomeApp/app.log"),
        "chrome": _old(h / "Library/Caches/Google/Chrome/Default/Cache/Cache_Data/f_000001"),
        "chrome_gpu": _old(h / "Library/Application Support/Google/Chrome/Profile 1/GPUCache/data_0"),
        "firefox": _old(h / "Library/Caches/Firefox/Profiles/ab12.default-release/cache2/entries/ABC"),
        "edge": _old(h / "Library/Caches/Microsoft Edge/Default/Cache/Cache_Data/f_1"),
        "brave": _old(h / "Library/Caches/BraveSoftware/Brave-Browser/Default/Cache/Cache_Data/f_2"),
        "pip": _old(h / "Library/Caches/pip/http/a/b"),
        "npm": _old(h / ".npm/_cacache/content-v2/sha512/x"),
        "yarn": _old(h / "Library/Caches/Yarn/v6/pkg"),
        "brew": _old(h / "Library/Caches/Homebrew/downloads/abc--wget.bottle.tar.gz"),
        "safari": _old(h / "Library/Caches/com.apple.Safari/Cache.db"),
        "mail": _old(h / "Library/Mail/V10/msg.emlx"),
        "other_cache": _old(h / "Library/Caches/com.example.app/blob"),
        "photos": _old(h / "Pictures/Photos Library.photoslibrary/database/x"),
    }
    tmpdir = tmp_path / "var" / "folders" / "ab" / "cdef" / "T"
    files["tmp_junk"] = _old(tmpdir / "~old.tmp")
    files["tmp_personal"] = _old(tmpdir / "notes.pdf")
    monkeypatch.setenv("TMPDIR", str(tmpdir))
    monkeypatch.setattr(winshell, "recycle_bin_info", lambda: (0, 0))
    monkeypatch.setattr(winshell, "disk_free", lambda p: 10 ** 9)
    return files


def _all_roots(locs):
    return {loc.key: [str(r) for r in cleanup.resolve_roots(loc)] for loc in locs}


def test_mac_locations_resolve_under_fake_home(mac_tree, mac, tmp_path):
    locs = cleanup.default_locations()
    assert {l.key for l in locs} >= {"temp_files", "mac_crash_reports", "mac_logs", "chrome_cache", "brave_cache",
                                      "edge_cache", "firefox_cache", "dev_cache"}
    roots = _all_roots(locs)
    home = str(executor.canonical(mac))
    tmpdir = str(executor.canonical(tmp_path / "var" / "folders" / "ab" / "cdef" / "T"))
    flat = [r for rs in roots.values() for r in rs]
    assert flat and all(r.startswith(home + os.sep) or r == tmpdir for r in flat)
    assert roots["temp_files"] == [tmpdir]
    lowered = [r.lower() for r in flat]
    for bad in ("safari", "mail", "photos", "mobile documents", "cloudstorage"):
        assert not any(bad in r for r in lowered), bad
    assert os.path.join(home, "Library", "Caches") not in flat  # never the whole Caches folder
    assert len(roots["dev_cache"]) == 4


def test_mac_scan_finds_only_known_files_and_blocks_running_browser(mac_tree, monkeypatch):
    monkeypatch.setattr(winshell, "running_processes", lambda: {"google chrome", "finder"})
    rep = cleanup.scan_cleanup(now=time.time(), save=False)
    cats = {c.key: c for c in rep.categories}
    found = {Path(f.path).name for c in rep.categories for f in c.files}
    assert {"Finder-2026-10-01.ips", "app.log", "f_000001", "data_0", "ABC", "b", "x", "pkg",
            "abc--wget.bottle.tar.gz", "~old.tmp"} <= found
    assert "Cache.db" not in found and "msg.emlx" not in found and "blob" not in found
    assert cats["chrome_cache"].blocked_reason and "Google Chrome" in cats["chrome_cache"].blocked_reason
    assert cats["firefox_cache"].blocked_reason is None
    kept = {Path(p.path).name: p.reason for p in rep.protected}
    assert kept.get("notes.pdf") == cleanup.KEPT_PERSONAL
    # the crash report belongs to its own category, not swallowed by the Logs walk
    assert [Path(f.path).name for f in cats["mac_crash_reports"].files] == ["Finder-2026-10-01.ips"]
    assert [Path(f.path).name for f in cats["mac_logs"].files] == ["app.log"]


def test_mac_browser_blocked_when_running_state_unknown(mac_tree, monkeypatch):
    def fail():
        raise OSError("ps failed")

    monkeypatch.setattr(winshell, "running_processes", fail)
    rep = cleanup.scan_cleanup(now=time.time(), save=False)
    assert all(c.blocked_reason for c in rep.categories if c.key.endswith("_cache") and c.key != "dev_cache")


def test_mac_clean_goes_to_trash_through_recycle(mac_tree, monkeypatch):
    monkeypatch.setattr(winshell, "running_processes", lambda: set())
    monkeypatch.setattr(winshell, "is_in_use", lambda p: False)
    monkeypatch.setattr(winshell, "is_alias_path", lambda p: False)
    monkeypatch.setattr(winshell, "delete_permanently", lambda p: pytest.fail("deleted instead of Trash"))
    sent = []

    def recycle(paths):
        sent.extend(paths)
        for p in paths:
            os.remove(p)
        return {str(p): None for p in paths}

    monkeypatch.setattr(winshell, "recycle", recycle)
    rep = cleanup.scan_cleanup(now=time.time())
    res = cleanup.clean(rep.report_id, ["mac_crash_reports", "temp_files"])
    assert res.removed_count == 2 and {p.name for p in sent} == {"Finder-2026-10-01.ips", "~old.tmp"}
    assert mac_tree["tmp_personal"].exists() and mac_tree["log"].exists()


@pytest.mark.parametrize("rel,why", [
    ("Library/Caches", "too broad"), ("Library", "too broad"),
    ("Library/Safari/x", "personal"), ("Library/Containers/com.apple.Safari/Data", "personal"),
    ("Library/Caches/com.apple.Safari", "personal"), ("Library/Mail/V10", "personal"),
    ("Library/Mobile Documents/com~apple~CloudDocs", "personal"), ("Pictures/Photos Library.photoslibrary", "personal"),
])
def test_mac_root_refusals_inside_home(mac, rel, why):
    p = executor.canonical(mac / rel)
    assert why in cleanup._root_refusal(p)


def test_mac_root_refusals_outside_home(mac, tmp_path, monkeypatch):
    monkeypatch.delenv("TMPDIR", raising=False)  # on a real Mac tmp_path is inside $TMPDIR, an allowed root
    assert cleanup._root_refusal(executor.canonical(tmp_path / "System" / "Library" / "Caches")) == \
        "outside your user folders"
    assert cleanup._root_refusal(executor.canonical(tmp_path)) is not None  # contains the home folder
    assert cleanup._root_refusal(executor.canonical(mac / "Library" / "Logs")) is None
    t = tmp_path / "var" / "folders" / "a" / "b" / "T"
    other = tmp_path / "var" / "folders" / "c" / "d" / "T"
    for d in (t / "x", other / "x"):
        d.mkdir(parents=True)
    assert cleanup._root_refusal(executor.canonical(t / "x")) is not None  # no $TMPDIR: nothing temp allowed
    monkeypatch.setenv("TMPDIR", str(t))
    assert cleanup._root_refusal(executor.canonical(t / "x")) is None
    assert cleanup._root_refusal(executor.canonical(other / "x")) == "outside your user folders"  # someone else's


def test_mac_tmpdir_must_be_the_per_user_folder(mac, tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(mac))
    assert cleanup._mac_temp_roots() == []
    monkeypatch.setenv("TMPDIR", str(tmp_path / "var" / "folders" / "a" / "b" / "T"))
    assert len(cleanup._mac_temp_roots()) == 1
    monkeypatch.delenv("TMPDIR")
    assert cleanup._mac_temp_roots() == []


def test_empty_trash_needs_confirmation_and_says_trash(mac):
    with pytest.raises(ValueError, match="Trash"):
        cleanup.empty_recycle_bin_confirmed(False)


# --------------------------------------------------------------------------
# desktop single-instance lock


def _fake_fcntl(monkeypatch):
    """Windows has no fcntl: emulate flock (per open file, exclusive, non-blocking)."""
    try:
        import fcntl  # noqa: F401 - real flock on POSIX
        return
    except ImportError:
        pass
    held = {}
    mod = types.ModuleType("fcntl")
    mod.LOCK_EX, mod.LOCK_NB, mod.LOCK_UN = 2, 4, 8

    def flock(fd, op):
        key = os.path.normcase(os.path.realpath(_fd_paths[fd]))
        if key in held and held[key] != fd:
            raise BlockingIOError("locked")
        held[key] = fd

    _fd_paths = {}
    real_open = os.open

    def tracking_open(path, flags, mode=0o777):
        fd = real_open(path, flags, mode)
        _fd_paths[fd] = str(path)
        return fd

    mod.flock = flock
    monkeypatch.setitem(sys.modules, "fcntl", mod)
    monkeypatch.setattr(desktop.os, "open", tracking_open)


def test_desktop_flock_single_instance(mac, monkeypatch, home):
    _fake_fcntl(monkeypatch)
    monkeypatch.setattr(desktop, "_lock_fd", None)
    assert desktop.hold_window_lock() is True
    first_fd = desktop._lock_fd
    assert (home / desktop.APP_LOCK_FILE).exists()
    assert desktop.hold_window_lock() is True  # same process again: still ours
    monkeypatch.setattr(desktop, "_lock_fd", None)  # now act like a second process
    assert desktop.hold_window_lock() is False
    opened = []
    monkeypatch.setattr(desktop, "run_window", lambda folder=None: opened.append(folder) or 0)
    monkeypatch.setattr(desktop, "_ensure_streams", lambda: None)
    assert desktop.main([]) == 0 and opened == []  # plain second launch: no second window
    assert desktop.main(["ui", "/Users/me/Downloads"]) == 0 and opened == ["/Users/me/Downloads"]
    os.close(first_fd)


def test_mac_has_no_native_splash_or_mutex(mac, monkeypatch):
    monkeypatch.setitem(sys.modules, "pyi_splash", types.SimpleNamespace(close=lambda: pytest.fail("splash")))
    desktop._close_native_splash()
    assert desktop.hold_app_mutex() is True


def test_window_lock_is_free_on_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert desktop.hold_window_lock() is True


# --------------------------------------------------------------------------
# Ollama setup


def test_mac_ollama_candidates(mac, monkeypatch):
    monkeypatch.setattr(ai_setup.shutil, "which", lambda name: None)
    c = ai_setup._candidates()
    assert c[:3] == ["/Applications/Ollama.app/Contents/Resources/ollama", "/usr/local/bin/ollama",
                     "/opt/homebrew/bin/ollama"]
    assert not any(x.endswith("ollama.exe") for x in c)
    monkeypatch.setattr(ai_setup.shutil, "which", lambda name: "/somewhere/bin/ollama")
    assert ai_setup._candidates()[-1] == "/somewhere/bin/ollama"
    monkeypatch.setattr(ai_setup.os.path, "isfile", lambda p: p == "/opt/homebrew/bin/ollama")
    assert ai_setup.find_ollama() == os.path.abspath("/opt/homebrew/bin/ollama")


def test_mac_install_with_brew(mac, monkeypatch):
    calls = []

    def fake_run(args, **kw):
        assert "shell" not in kw
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="installed", stderr="")

    monkeypatch.setattr(ai_setup, "find_brew", lambda: "/opt/homebrew/bin/brew")
    monkeypatch.setattr(ai_setup.subprocess, "run", fake_run)
    monkeypatch.setattr(ai_setup.subprocess, "Popen", lambda *a, **k: pytest.fail("winget/curl used on macOS"))
    monkeypatch.setattr(ai_setup, "find_ollama", lambda: "/opt/homebrew/bin/ollama")
    assert ai_setup.install_ollama() == "/opt/homebrew/bin/ollama"
    assert calls == [["/opt/homebrew/bin/brew", "install", "ollama"]]


def test_mac_install_brew_failure(mac, monkeypatch):
    monkeypatch.setattr(ai_setup, "find_brew", lambda: "/usr/local/bin/brew")
    monkeypatch.setattr(ai_setup.subprocess, "run",
                        lambda args, **k: subprocess.CompletedProcess(args, 1, stdout="", stderr="Error: no bottle"))
    with pytest.raises(RuntimeError, match="no bottle"):
        ai_setup.install_ollama()


def test_mac_install_without_brew_points_to_download(mac, monkeypatch):
    monkeypatch.setattr(ai_setup, "find_brew", lambda: None)
    monkeypatch.setattr(ai_setup.subprocess, "run", lambda *a, **k: pytest.fail("ran something"))
    monkeypatch.setattr(ai_setup.subprocess, "Popen", lambda *a, **k: pytest.fail("ran something"))
    with pytest.raises(RuntimeError, match=r"https://ollama\.com/download/mac"):
        ai_setup.install_ollama()


def test_find_brew_paths(mac, monkeypatch):
    monkeypatch.setattr(ai_setup.shutil, "which", lambda name: None)
    monkeypatch.setattr(ai_setup.os.path, "isfile", lambda p: p == "/usr/local/bin/brew")
    assert ai_setup.find_brew() == os.path.abspath("/usr/local/bin/brew")
    monkeypatch.setattr(ai_setup.os.path, "isfile", lambda p: False)
    assert ai_setup.find_brew() is None


def test_mac_status_message(mac, monkeypatch):
    monkeypatch.setattr(ai_setup, "find_ollama", lambda: None)
    monkeypatch.setattr(ai_setup, "_tags", lambda base_url=None, timeout=2.0: None)
    monkeypatch.setattr(ai_setup, "find_brew", lambda: None)
    assert "ollama.com/download/mac" in ai_setup.status()["message"]
    monkeypatch.setattr(ai_setup, "find_brew", lambda: "/opt/homebrew/bin/brew")
    assert "Homebrew" in ai_setup.status()["message"]


def test_mac_start_server_without_windows_flags(mac, monkeypatch, tmp_path):
    exe = tmp_path / "ollama"
    exe.write_bytes(b"x")
    state, calls = {"up": False}, []

    def fake_popen(args, **kw):
        calls.append((args, kw))
        state["up"] = True
        return types.SimpleNamespace(poll=lambda: None)

    monkeypatch.setattr(ai_setup, "server_running", lambda base_url=None: state["up"])
    monkeypatch.setattr(ai_setup, "_tags", lambda base_url=None, timeout=2.0: [] if state["up"] else None)
    monkeypatch.setattr(ai_setup.subprocess, "Popen", fake_popen)
    assert ai_setup.start_server(str(exe), wait=2)
    (args, kw), = calls
    assert args == [str(exe), "serve"] and "creationflags" not in kw and kw["start_new_session"] is True
    assert kw["env"]["OLLAMA_HOST"] == "127.0.0.1:11434"


# --------------------------------------------------------------------------
# web UI


def test_api_state_reports_platform(demo, monkeypatch):
    import http.client
    import json
    import threading
    from http.server import ThreadingHTTPServer

    root, _ = demo
    app = web.App(str(root))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(app))
    port = httpd.server_address[1]
    app.allowed_hosts = {f"127.0.0.1:{port}"}
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def state():
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        c.request("GET", "/api/state", headers={"X-SlothFile-Token": app.token, "Host": f"127.0.0.1:{port}"})
        r = c.getresponse()
        body = json.loads(r.read())
        c.close()
        return body

    try:
        monkeypatch.setattr(web, "_home_folders", lambda: {})
        monkeypatch.setattr(web, "platform_name", lambda: "windows")
        assert state()["platform"] == "windows"
        monkeypatch.setattr(web, "platform_name", lambda: "mac")
        assert state()["platform"] == "mac"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_platform_name(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert web.platform_name() == "mac"
    monkeypatch.setattr(sys, "platform", "win32")
    assert web.platform_name() == "windows"


def test_home_folders_on_mac(mac):
    (mac / "Downloads").mkdir()
    (mac / "Documents").mkdir()
    assert web._home_folders() == {"Downloads": str(mac / "Downloads"), "Documents": str(mac / "Documents")}


def test_ui_has_mac_wording_and_stays_csp_safe():
    import re
    html = (Path(web.__file__).parent / "ui.html").read_text(encoding="utf-8")
    assert 'data-mac="Empty Trash"' in html and 'data-mac="Uses macOS text recognition"' in html
    assert "Finder Quick Action: Tidy with SlothFile" in html and 'id="run-alias-row"' in html
    assert "st.platform" in html
    assert not re.search(r"<[^>]+\sstyle\s*=", html)  # no inline style attributes
    assert not re.search(r"<[^>]+\son[a-z]+\s*=", html)  # no inline event handlers
