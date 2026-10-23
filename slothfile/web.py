"""Local web UI (stdlib only).

Security model: binds to 127.0.0.1 only; every API call must carry a random
per-session token; the Host header must be our loopback address (blocks DNS
rebinding); cross-origin requests are rejected; POST bodies must be JSON.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import traceback
import uuid
import webbrowser
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .executor import NeedsRecovery
from .llm import DEFAULT_MODEL, ModelError, OllamaClient
from .pathsafe import UnsafePath
from .service import execute, incomplete_runs, list_runs, load_plan, make_plan, recover, undo_run

MAX_BODY = 2 * 1024 * 1024
_PICK_FOLDER = (
    "import tkinter as tk\nfrom tkinter import filedialog\n"
    "r = tk.Tk(); r.withdraw(); r.attributes('-topmost', True)\n"
    "print(filedialog.askdirectory(title='Choose a folder for SlothFile to tidy') or '')\n"
)


class App:
    def __init__(self, initial_folder: str | None):
        self.token = secrets.token_urlsafe(24)
        self.initial_folder = initial_folder
        self.jobs: dict[str, dict] = {}
        self.lock = threading.Lock()  # one file operation at a time
        self.ai_lock = threading.Lock()  # one model setup at a time (it never touches user files)
        self.allowed_hosts: set[str] = set()

    def start_plan_job(self, folder: str, model: str | None, threshold: float) -> str:
        job_id = uuid.uuid4().hex[:12]
        job = {"state": "running", "done": 0, "total": 0, "current": "", "stage": None, "plan": None, "error": None}
        self.jobs[job_id] = job

        def progress(n, total, name):
            job.update(done=n, total=total, current=name)

        def stage(label):  # a slow step after the per-file pass, e.g. TabPFN scoring
            job["stage"] = label

        def run():
            try:
                plan, status = make_plan(folder, model=model, threshold=threshold, progress=progress,
                                         on_stage=stage)
                job["plan"] = plan.to_dict()
                job["state"] = "done"
            except Exception as e:  # surfaced to the UI as a readable error
                job["error"] = str(e) or type(e).__name__
                job["state"] = "error"
                traceback.print_exc()

        threading.Thread(target=run, daemon=True).start()
        return job_id

    def start_job(self, fn, *, progress: bool = False) -> str:
        """Run fn() in the background; the UI polls /api/job/<id> for its result.
        With progress=True, fn(report) gets a callback whose (stage, percent)
        appears in the job as job["progress"]."""
        job_id = uuid.uuid4().hex[:12]
        job = {"state": "running", "result": None, "error": None}
        if progress:
            job["progress"] = None
        self.jobs[job_id] = job

        def report(stage: str, percent: float | None) -> None:
            job["progress"] = {"stage": stage, "percent": percent}

        def run():
            try:
                job["result"] = fn(report) if progress else fn()
                job["state"] = "done"
            except Exception as e:
                job["error"] = str(e) or type(e).__name__
                job["state"] = "error"
                traceback.print_exc()

        threading.Thread(target=run, daemon=True).start()
        return job_id


def _home_folders() -> dict[str, str]:
    """The user's standard folders that exist (quick choices in the app).
    Uses the shell's known-folder locations, so redirected/OneDrive folders work."""
    out: dict[str, str] = {}
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        known = {"Downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
                 "Desktop": "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}",
                 "Documents": "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}"}

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                        ("Data4", ctypes.c_ubyte * 8)]

        for label, g in known.items():
            guid = GUID()
            ctypes.windll.ole32.CLSIDFromString(g, ctypes.byref(guid))
            ptr = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(ptr)) == 0:
                path = ptr.value
                ctypes.windll.ole32.CoTaskMemFree(ptr)
                if path and os.path.isdir(path):
                    out[label] = path
    else:
        for label in ("Downloads", "Desktop", "Documents"):
            p = os.path.join(os.path.expanduser("~"), label)
            if os.path.isdir(p):
                out[label] = p
    return out


def _str_list(value, name: str) -> list[str]:
    """Request fields are untrusted: a single string must never be read letter by letter."""
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{name} must be a list of strings")
    return value


def _str_dict(value, name: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        raise ValueError(f"{name} must map strings to strings")
    return value


def _automation_state() -> dict:
    from . import automation
    try:
        tasks = automation.list_tasks()
    except Exception as e:  # Task Scheduler unavailable: show it, don't fail the page
        tasks = [{"name": "(could not read Task Scheduler)", "status": str(e)}]
    return {"tasks": tasks, "context_menu": automation.context_menu_installed(),
            "run_alias": automation.run_alias_installed(), "pending": automation.pending()}


def _report_dict(report) -> dict:
    return {"run_id": report.run_id, "summary": report.summary(),
            "results": [asdict(r) for r in report.results]}


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "SlothFile"
        sys_version = ""

        def log_message(self, fmt, *args):  # keep the console quiet; no file names in logs
            pass

        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _host_ok(self) -> bool:
            return self.headers.get("Host", "") in app.allowed_hosts

        def _authorized(self) -> bool:
            if not self._host_ok():
                return False
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc not in app.allowed_hosts:
                return False
            return hmac.compare_digest(self.headers.get("X-SlothFile-Token", ""), app.token)

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, b"forbidden", "text/plain")
            url = urlparse(self.path)
            if url.path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            if url.path == "/":
                if not hmac.compare_digest(parse_qs(url.query).get("t", [""])[0], app.token):
                    return self._send(403, b"Open SlothFile using the link printed in the terminal.", "text/plain")
                nonce = secrets.token_urlsafe(16)
                html = resources.files("slothfile").joinpath("ui.html").read_text(encoding="utf-8")
                html = html.replace("{{NONCE}}", nonce)
                csp = (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                       "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; "
                       "frame-ancestors 'none'")
                return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8",
                                  {"Content-Security-Policy": csp})
            if not self._authorized():
                return self._json(403, {"error": "forbidden"})
            if url.path == "/api/state":
                from . import __version__
                return self._json(200, {"initial_folder": app.initial_folder, "default_model": DEFAULT_MODEL,
                                        "runs": list_runs(), "version": __version__,
                                        "home_folders": _home_folders()})
            if url.path == "/api/model":
                try:
                    status = OllamaClient(DEFAULT_MODEL).health()
                    return self._json(200, {"available": True, "status": status})
                except ModelError as e:
                    return self._json(200, {"available": False, "status": str(e)})
            if url.path == "/api/ai/status":
                from . import ai_setup
                return self._json(200, ai_setup.status())
            if url.path == "/api/settings":
                from .rules import CATEGORIES
                from .settings import load_settings
                return self._json(200, {"settings": load_settings(), "categories": CATEGORIES})
            if url.path == "/api/summary":
                from .summary import format_summary, summarize
                s = summarize(days=7)
                return self._json(200, {"summary": s, "text": format_summary(s)})
            if url.path == "/api/learned":
                from . import learning
                return self._json(200, {"rules": [asdict(r) for r in learning.learned_rules()]})
            if url.path == "/api/automation":
                return self._json(200, _automation_state())
            if url.path == "/api/predictor":
                from . import predict  # info() never imports torch
                return self._json(200, predict.info())
            if url.path.startswith("/api/job/"):
                job = app.jobs.get(url.path.rsplit("/", 1)[-1])
                return self._json(200, job) if job else self._json(404, {"error": "no such job"})
            return self._json(404, {"error": "not found"})

        def do_POST(self):
            if not self._authorized():
                return self._json(403, {"error": "forbidden"})
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                return self._json(415, {"error": "JSON required"})
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._json(413, {"error": "request too large"})
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
                return self._json(400, {"error": "bad JSON"})
            if not isinstance(body, dict):
                return self._json(400, {"error": "JSON object required"})
            try:
                return self._route(urlparse(self.path).path, body)
            except NeedsRecovery as e:
                return self._json(409, {"error": str(e), "needs_recovery": True})
            except (ValueError, TypeError, UnsafePath, FileNotFoundError, PermissionError, RuntimeError) as e:
                return self._json(400, {"error": str(e)})
            except Exception as e:
                traceback.print_exc()
                return self._json(500, {"error": f"unexpected error: {type(e).__name__}"})

        def _route(self, path: str, body: dict):
            if path == "/api/pick-folder" and getattr(sys, "frozen", False):
                # The installed app uses the native dialog (window.pywebview.api); there is no python.exe here.
                return self._json(200, {"folder": None})
            if path == "/api/pick-folder":
                # -I: isolated mode, so a planted tkinter.py in the working dir is never imported.
                r = subprocess.run([sys.executable, "-I", "-c", _PICK_FOLDER], capture_output=True, text=True,
                                   timeout=600, cwd=tempfile.gettempdir())
                picked = os.path.normpath(r.stdout.strip()) if r.stdout.strip() else None
                if picked and not (os.path.isabs(picked) and os.path.isdir(picked)):
                    picked = None
                return self._json(200, {"folder": picked})
            if path == "/api/plan":
                folder = str(body.get("folder") or "").strip()
                if not folder or not os.path.isdir(folder):
                    return self._json(400, {"error": "Choose an existing folder first."})
                pending = incomplete_runs(Path(folder))
                if pending:
                    return self._json(409, {"error": "A previous run on this folder was interrupted.",
                                            "needs_recovery": True})
                model = DEFAULT_MODEL if body.get("use_model") else None
                threshold = min(0.99, max(0.0, float(body.get("threshold", 0.6))))
                return self._json(200, {"job": app.start_plan_job(folder, model, threshold)})
            if path == "/api/apply":
                plan_id = str(body.get("plan_id", ""))
                approved = set(_str_list(body.get("approved", []), "approved"))
                renames = _str_dict(body.get("renames"), "renames")
                overrides = _str_dict(body.get("overrides"), "overrides")
                allow_recycle = body.get("allow_recycle") is True  # only a real JSON true counts
                if not app.lock.acquire(blocking=False):
                    return self._json(409, {"error": "another operation is running"})
                try:
                    report = execute(plan_id, approved=approved, renames=renames, overrides=overrides,
                                     allow_recycle=allow_recycle)
                finally:
                    app.lock.release()
                return self._json(200, _report_dict(report))
            if path == "/api/ai/setup":
                from . import ai_setup
                if body.get("confirm") is not True:
                    raise ValueError("confirmation required")
                if not app.ai_lock.acquire(blocking=False):
                    return self._json(409, {"error": "model setup is already running"})

                def locked_setup(report):
                    try:
                        return ai_setup.setup(report)
                    finally:
                        app.ai_lock.release()

                try:
                    job_id = app.start_job(locked_setup, progress=True)
                except BaseException:  # the thread never started, so it can't release the lock
                    app.ai_lock.release()
                    raise
                return self._json(200, {"job": job_id})
            if path == "/api/settings":
                from .settings import save_settings
                changes = body.get("changes")
                if not isinstance(changes, dict):
                    raise ValueError("changes must be an object")
                return self._json(200, {"settings": save_settings(changes)})
            if path == "/api/learned/forget":
                from . import learning
                key = body.get("key")
                if not isinstance(key, str):
                    raise ValueError("key must be a string")
                return self._json(200, {"forgotten": learning.forget_rule(key)})
            if path == "/api/learned/reset":
                from . import learning
                if body.get("confirm") is not True:
                    raise ValueError("confirmation required")
                learning.reset_learning()
                from . import reviewlog
                reviewlog.reset()  # also the history the predictor learns from
                return self._json(200, {"ok": True})
            if path == "/api/predictor/selftest":
                from . import predict  # fits on made-up rows, never the user's history
                return self._json(200, {"job": app.start_job(predict.selftest)})
            if path == "/api/predictor/backtest":
                from . import predict  # read-only test on the user's own review history
                return self._json(200, {"job": app.start_job(predict.backtest)})
            if path == "/api/cleanup/scan":
                from . import cleanup
                return self._json(200, {"job": app.start_job(lambda: cleanup.scan_cleanup().to_dict())})
            if path == "/api/cleanup/clean":
                from . import cleanup
                report_id = body.get("report_id")
                cats = _str_list(body.get("categories"), "categories")
                exclude = set(_str_list(body.get("exclude", []), "exclude"))
                mode = body.get("mode")
                if mode not in ("recycle", "delete") or not isinstance(report_id, str):
                    raise ValueError("mode must be 'recycle' or 'delete'")
                confirm = body.get("confirm_permanent") is True
                if not app.lock.acquire(blocking=False):
                    return self._json(409, {"error": "another operation is running"})
                try:
                    res = cleanup.clean(report_id, cats, mode=mode, confirm_permanent=confirm, exclude_paths=exclude)
                finally:
                    app.lock.release()
                return self._json(200, res.to_dict())
            if path == "/api/cleanup/empty-bin":
                from . import cleanup
                with app.lock:
                    return self._json(200, cleanup.empty_recycle_bin_confirmed(body.get("confirm") is True))
            if path == "/api/auto":
                from .automation import auto_run
                folder = body.get("folder")
                if not isinstance(folder, str) or not os.path.isdir(folder):
                    return self._json(400, {"error": "Choose an existing folder first."})
                # Same lock as manual operations, held for the whole background job,
                # so an automatic run never overlaps an apply, undo or cleanup.
                if not app.lock.acquire(blocking=False):
                    return self._json(409, {"error": "another operation is running"})

                def locked_auto():
                    try:
                        return auto_run(folder)
                    finally:
                        app.lock.release()

                return self._json(200, {"job": app.start_job(locked_auto)})
            if path == "/api/automation":
                from . import automation
                action = body.get("action")
                if action == "schedule_daily":
                    folder, at = body.get("folder"), body.get("at", "20:00")
                    if not isinstance(folder, str) or not os.path.isdir(folder) or not isinstance(at, str):
                        raise ValueError("choose an existing folder and a time like 20:00")
                    automation.schedule_daily(folder, at=at)
                elif action == "schedule_summary":
                    day, at = body.get("day", "SUN"), body.get("at", "18:00")
                    if not isinstance(day, str) or not isinstance(at, str):
                        raise ValueError("bad day or time")
                    automation.schedule_weekly_summary(day=day, at=at)
                elif action == "remove_task":
                    name = body.get("name")
                    if not isinstance(name, str):
                        raise ValueError("name must be a string")
                    automation.remove_task(name)
                elif action == "context_menu":
                    (automation.install_context_menu if body.get("on") is True
                     else automation.uninstall_context_menu)()
                elif action == "run_alias":
                    (automation.install_run_alias if body.get("on") is True else automation.uninstall_run_alias)()
                else:
                    raise ValueError("unknown action")
                return self._json(200, _automation_state())
            if path == "/api/undo":
                with app.lock:
                    report = undo_run(str(body.get("run_id", "")))
                return self._json(200, _report_dict(report))
            if path == "/api/recover":
                with app.lock:
                    reports = recover(Path(str(body.get("folder", ""))))
                return self._json(200, {"reports": [_report_dict(r) for r in reports]})
            if path == "/api/plan-get":
                return self._json(200, load_plan(str(body.get("plan_id", ""))).to_dict())
            return self._json(404, {"error": "not found"})

    return Handler


def serve(folder: str | None = None, *, port: int = 0, open_browser: bool = True) -> None:
    app = App(os.path.abspath(folder) if folder else None)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    real_port = httpd.server_address[1]
    app.allowed_hosts = {f"127.0.0.1:{real_port}", f"localhost:{real_port}"}
    url = f"http://127.0.0.1:{real_port}/?t={app.token}"
    print(f"SlothFile is running locally at:\n  {url}\nPress Ctrl+C to stop.", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
