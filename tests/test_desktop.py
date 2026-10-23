"""Desktop app and installed-exe behaviour (no window is opened in these tests)."""

import pytest
import sys
from pathlib import Path

from slothfile import automation, desktop


def fake_frozen(monkeypatch, tmp_path, name="SlothFileCLI.exe"):
    app = tmp_path / "Programs" / "SlothFile"
    app.mkdir(parents=True)
    for exe in ("SlothFile.exe", "SlothFileCLI.exe"):
        (app / exe).write_bytes(b"MZ")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(app / name))
    return app


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only behaviour (schtasks, toast, .exe names, creation flags)")
def test_installed_exe_launches_windowless_app(monkeypatch, tmp_path):
    app = fake_frozen(monkeypatch, tmp_path)
    gui = str(app / "SlothFile.exe")
    assert automation.gui_python() == gui
    assert automation.launch_prefix() == f'"{gui}"'
    assert automation.menu_command() == f'"{gui}" ui "%V"'
    tr = automation._tr('auto "C:\\Users\\me\\Downloads"')
    assert tr.startswith(f'"{gui}" auto') and "-m slothfile" not in tr


def test_source_checkout_uses_python_module(monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert automation.launch_prefix().endswith("-m slothfile")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only behaviour")
def test_menu_from_installed_exe_is_recognised_as_ours(monkeypatch, tmp_path):
    app = fake_frozen(monkeypatch, tmp_path)
    wr = __import__("winreg")
    base = r"Software\SlothFileTest\desktop"
    try:
        automation.install_context_menu(base=base)
        assert automation.context_menu_installed(base=base)
        automation.uninstall_context_menu(base=base)
        assert not automation.context_menu_installed(base=base)
    finally:
        for key in (r"Directory\shell\SlothFile\command", r"Directory\shell\SlothFile",
                    r"Directory\Background\shell\SlothFile\command", r"Directory\Background\shell\SlothFile"):
            try:
                wr.DeleteKey(wr.HKEY_CURRENT_USER, f"{base}\\{key}")
            except OSError:
                pass


def test_exe_routes_commands_to_cli_and_no_args_to_window(monkeypatch):
    calls = {}
    monkeypatch.setattr(desktop, "run_window", lambda folder=None: calls.setdefault("window", folder) or 0)
    import slothfile.cli as cli
    monkeypatch.setattr(cli, "main", lambda args: calls.setdefault("cli", args) and 0)
    desktop.main([])
    assert "window" in calls and calls["window"] is None
    calls.clear()
    desktop.main(["ui", "C:\\Users\\me\\Downloads"])
    assert calls["window"] == "C:\\Users\\me\\Downloads"
    calls.clear()
    desktop.main(["auto", "C:\\x"])
    assert calls["cli"] == ["auto", "C:\\x"]


def test_page_bridge_exposes_only_pick_folder():
    api = desktop.DesktopApi()
    public = [n for n in vars(api) if not n.startswith("_")]
    assert public == []  # the window object must not be reachable from the page
    assert callable(api.pick_folder) and api.pick_folder() is None


def test_windowed_exe_without_console_logs_instead_of_crashing(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    desktop._ensure_streams()
    print("hello from a windowed exe")
    sys.stdout.flush()
    log = Path(__import__("os").environ["SLOTHFILE_HOME"]) / "desktop.log"
    assert "hello from a windowed exe" in log.read_text(encoding="utf-8")
