"""Native desktop window for SlothFile (the installed SlothFile.exe).

The same interface as `slothfile ui`, but shown in its own Windows app
window (Microsoft Edge WebView2 via pywebview) instead of the browser. The
local server still listens on 127.0.0.1 only and still requires the
per-session token; the window is simply its only client.

The exe doubles as the command line: Task Scheduler, the right-click menu and
Win+R start `SlothFile.exe <command> ...` (e.g. `auto "C:\\Users\\me\\Downloads"`),
and `SlothFile.exe` or `SlothFile.exe ui [folder]` opens the window.
"""

from __future__ import annotations

import os
import sys
import threading
from http.server import ThreadingHTTPServer

APP_TITLE = "SlothFile"
APP_MUTEX = "SlothFileAppMutex"  # must match AppMutex in packaging/slothfile.iss
_mutex = None


def hold_app_mutex() -> None:
    """Announce "SlothFile is running" for the installer/uninstaller, which
    then asks the user to close the app instead of leaving files half-removed.
    The handle lives until the process exits."""
    global _mutex
    if _mutex is None and os.name == "nt":
        import ctypes
        _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, APP_MUTEX)


def _ensure_streams() -> None:
    """A windowed exe has no console: print() would fail on sys.stdout=None.
    Send output to a log file next to SlothFile's journals instead."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        from .journal import state_dir
        log = open(state_dir() / "desktop.log", "a", encoding="utf-8", buffering=1)
    except OSError:
        log = open(os.devnull, "w", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = log
    if sys.stderr is None:
        sys.stderr = log


class DesktopApi:
    """Functions the page may call through window.pywebview.api."""

    def __init__(self) -> None:
        self._window = None  # private: pywebview must not expose the window object to the page

    def pick_folder(self) -> str | None:
        if self._window is None:
            return None
        import webview
        kind = getattr(getattr(webview, "FileDialog", None), "FOLDER", None)
        if kind is None:  # pywebview < 5
            kind = webview.FOLDER_DIALOG
        result = self._window.create_file_dialog(kind)
        if not result:
            return None
        path = result[0] if isinstance(result, (list, tuple)) else result
        return os.path.normpath(path) if os.path.isdir(path) else None


def _close_native_splash() -> None:
    """Close the PyInstaller splash image (installed exe only)."""
    try:
        import pyi_splash  # only exists inside a PyInstaller build with a Splash
        pyi_splash.close()
    except Exception:
        pass


def run_window(folder: str | None = None) -> int:
    try:
        import webview
    except ImportError:
        print("The desktop window needs pywebview: pip install \"slothfile[desktop]\". "
              "Opening the browser version instead.")
        from .web import serve
        serve(folder)
        return 0
    from .web import App, make_handler
    app = App(os.path.abspath(folder) if folder else None)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    port = httpd.server_address[1]
    app.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    api = DesktopApi()
    # Hidden until the page has loaded, so the user sees the splash, then the
    # finished window: never a blank white frame.
    window = webview.create_window(APP_TITLE, f"http://127.0.0.1:{port}/?t={app.token}", js_api=api,
                                   width=1200, height=820, min_size=(820, 580), background_color="#f3f3f3",
                                   hidden=True)
    api._window = window

    def on_loaded() -> None:
        _close_native_splash()
        window.show()

    window.events.loaded += on_loaded
    try:
        webview.start(private_mode=True)
    finally:
        httpd.shutdown()
        httpd.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    import multiprocessing
    multiprocessing.freeze_support()  # the exe re-launches itself for the file-reading worker
    _ensure_streams()
    hold_app_mutex()
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] != "ui":
        _close_native_splash()  # headless command (scheduled task, right-click): no splash
    if not args or args[0] == "ui":
        rest = [a for a in args[1:] if not a.startswith("-")]
        return run_window(rest[0] if rest else None)
    from .cli import main as cli_main
    return cli_main(args)


if __name__ == "__main__":
    sys.exit(main())
