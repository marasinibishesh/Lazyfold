"""Automatic tidying: notifications, trusted auto-approval, folder watching,
scheduled runs, the Explorer right-click entry and the Win+R shortcut.

Automation never gets more power than the reviewed path. It builds the same
plan, pre-approves only rule-based moves of kinds the user trusts, and hands
them to the same executor (journaled, re-fingerprinted, undoable). Anything
the model influenced, and every document by default, waits for review.

Everything that touches Windows (toasts, Task Scheduler, registry) goes
through subprocess with argument lists or winreg under HKCU: no shell
strings, no elevation.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable
from xml.sax.saxutils import escape as xml_escape

from . import executor, service
from .journal import now_iso, state_dir
from .pathsafe import UnsafePath
from .settings import load_settings, validate

log = logging.getLogger(__name__)

# AUMID of Windows PowerShell: always registered, so toasts show without
# installing a Start-menu shortcut of our own.
TOAST_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
TASK_FOLDER = "SlothFile"
MAX_TR = 261  # schtasks /TR limit
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW

# Static script: the toast XML arrives through an environment variable, so
# file names in the message can never become PowerShell code.
_TOAST_PS = (
    "$ErrorActionPreference='Stop';"
    "[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime]|Out-Null;"
    "[Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom.XmlDocument,ContentType=WindowsRuntime]|Out-Null;"
    "$x=New-Object Windows.Data.Xml.Dom.XmlDocument;"
    "$x.LoadXml($env:SLOTHFILE_TOAST_XML);"
    "$t=[Windows.UI.Notifications.ToastNotification]::new($x);"
    f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{TOAST_APP_ID}').Show($t)"
)


def _system32(exe: str) -> str:
    root = os.environ.get("SystemRoot", r"C:\Windows")
    return os.path.join(root, "System32", exe)


def _powershell() -> str:
    return os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell",
                        "v1.0", "powershell.exe")


def _plain(s: str, limit: int) -> str:
    """Drop control/format characters (illegal in XML 1.0, or bidi tricks)
    and keep the text short enough for a toast."""
    s = "".join(c if c.isprintable() else " " for c in str(s))
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= limit else s[: limit - 1] + "\u2026"


def toast_xml(title: str, body: str) -> str:
    return ("<toast><visual><binding template='ToastGeneric'>"
            f"<text>{xml_escape(_plain(title, 120))}</text>"
            f"<text>{xml_escape(_plain(body, 400))}</text>"
            "</binding></visual></toast>")


def notify(title: str, body: str) -> bool:
    """Show a Windows toast. Returns False on any failure; never raises,
    because a missing notification must never break a run."""
    if os.name != "nt":
        return False
    try:
        env = {**os.environ, "SLOTHFILE_TOAST_XML": toast_xml(title, body)}
        r = subprocess.run([_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                            "-Command", _TOAST_PS],
                           env=env, capture_output=True, timeout=30, creationflags=_NO_WINDOW,
                           stdin=subprocess.DEVNULL)
        return r.returncode == 0
    except Exception:  # noqa: BLE001 - notification is best effort by design
        log.debug("toast failed", exc_info=True)
        return False


# --------------------------------------------------------------------------
# Pending review record


def _pending_path() -> Path:
    return state_dir() / "pending.json"


def _write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)  # our own state file, not a user file


def pending() -> dict | None:
    """The last automatic plan still waiting for review, if any."""
    try:
        d = json.loads(_pending_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or not isinstance(d.get("plan_id"), str):
        return None
    return d


def clear_pending(plan_id: str | None = None) -> None:
    """Forget the pending review (only if it is `plan_id`, when given, so a
    newer automatic plan is not dismissed by reviewing an older one)."""
    cur = pending()
    if cur is None or plan_id is None or cur.get("plan_id") == plan_id:
        try:
            _pending_path().unlink()
        except FileNotFoundError:
            pass


def _clear_stale_pending(root: Path) -> None:
    """Nothing in `root` waits any more: an older review record for the same
    folder is stale (the user dealt with those files), so drop it. A record
    for another folder is left alone."""
    cur = pending()
    if cur is None:
        return
    try:
        same = isinstance(cur.get("folder"), str) and executor.same_root(cur["folder"], root)
    except (OSError, ValueError):
        return
    if same:
        clear_pending(cur["plan_id"])


# --------------------------------------------------------------------------
# One automatic run


class _RunLock:
    """Stops the watcher and a scheduled task from tidying at the same time.
    A lock older than an hour is assumed to be left over from a crash."""

    STALE = 3600

    def __init__(self) -> None:
        self.path = state_dir() / "auto.lock"
        self.held = False

    def __enter__(self) -> "_RunLock":
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                self.held = True
                break
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > self.STALE:
                        self.path.unlink()
                        continue
                except OSError:
                    pass
                break
        return self

    def __exit__(self, *exc: object) -> None:
        if self.held:
            try:
                self.path.unlink()
            except OSError:
                pass


def trusted(p, settings: dict) -> bool:
    """Would this proposal be moved without review? Model-influenced
    decisions always wait, whatever the category, and so do moves the
    approval predictor thinks this user would probably change."""
    prob = getattr(p, "accept_prob", None)
    if prob is not None and prob < settings["auto_min_confidence"]:
        return False
    return (p.action == "move" and p.category in settings["auto_approve_categories"]
            and p.confidence >= settings["auto_min_confidence"] and p.engine == "rules")


def _message(folder: Path, moved: int, waiting: int) -> tuple[str, str]:
    name = folder.name or str(folder)
    files = lambda n: f"{n} file{'s' if n != 1 else ''}"  # noqa: E731
    if moved and waiting:
        body = (f"SlothFile tidied {files(moved)} in {name}. {files(waiting)} "
                f"{'is' if waiting == 1 else 'are'} waiting for your review - open SlothFile.")
    elif moved:
        body = f"SlothFile tidied {files(moved)} in {name}. You can undo this in SlothFile."
    else:
        body = f"{files(waiting)} in {name} {'is' if waiting == 1 else 'are'} ready for your review - open SlothFile."
    return "SlothFile", body


def auto_run(folder: str | Path, *, apply_trusted: bool = True, settings: dict | None = None,
             notify_fn: Callable[[str, str], bool] = notify) -> dict:
    """Plan `folder`, move the trusted part, and leave the rest for review."""
    settings = validate(settings) if settings is not None else load_settings()
    root = Path(os.path.abspath(folder))
    out: dict[str, Any] = {"plan_id": None, "run_id": None, "moved": 0, "waiting": 0, "skipped": 0,
                           "notified": False}
    with _RunLock() as lock:
        if not lock.held:
            out["error"] = "another automatic SlothFile run is in progress"
            return out
        try:
            executor.check_root(root)
            from .llm import DEFAULT_MODEL
            plan, _status = service.make_plan(root, model=DEFAULT_MODEL if settings["use_model"] else None)
        except (OSError, ValueError, UnsafePath) as e:
            out["error"] = f"could not scan {root}: {e}"
            out["notified"] = notify_fn("SlothFile could not tidy a folder", f"{root.name or root}: {e}")
            return out
        out["plan_id"] = plan.plan_id
        approved = {p.rel_path for p in plan.proposals if trusted(p, settings)} if apply_trusted else set()
        moved_paths: set[str] = set()
        needs_recovery = False
        if approved:
            service.apply_edits(plan, approved=approved)
            try:
                report = executor.apply_plan(plan)
            except executor.NeedsRecovery as e:
                report = None
                needs_recovery = True
                out["error"] = str(e)
            except (OSError, UnsafePath) as e:
                report = None
                out["error"] = f"could not tidy {root}: {e}"
            if report is not None:
                out["run_id"] = report.run_id
                moved_paths = {r.src for r in report.results if r.status == "moved"}
                out["moved"] = len(moved_paths)
                out["skipped"] = report.count("skipped") + report.count("failed")
        # The saved plan becomes the review list: files already filed are
        # dropped, and nothing in it stays pre-approved.
        plan.proposals = [p for p in plan.proposals if p.rel_path not in moved_paths]
        for p in plan.proposals:
            p.approved = False
        plan.scan_stats["auto"] = {"run_id": out["run_id"], "moved": sorted(moved_paths)}
        service.save_plan(plan)
        out["waiting"] = sum(1 for p in plan.proposals if p.action == "move")
        if out["waiting"]:
            _write_json(_pending_path(), {"plan_id": plan.plan_id, "folder": str(root),
                                          "waiting": out["waiting"], "created": now_iso()})
        else:
            _clear_stale_pending(root)
        if "error" in out:
            if needs_recovery:
                body = (f"An earlier run in {root.name or root} was interrupted. "
                        "Open SlothFile to finish or undo it; nothing was moved this time.")
            else:
                body = out["error"]
            out["notified"] = notify_fn("SlothFile needs your attention", body)
            return out
        if out["moved"] + out["waiting"] >= max(1, settings["notify_min_files"]):
            out["notified"] = notify_fn(*_message(root, out["moved"], out["waiting"]))
    return out


# --------------------------------------------------------------------------
# Watching a folder


def listing(folder: str | Path) -> dict[str, tuple[int, int]]:
    """Cheap snapshot of the top-level files: name -> (size, mtime_ns).
    Links and folders are ignored, like the scanner ignores them."""
    out: dict[str, tuple[int, int]] = {}
    try:
        with os.scandir(folder) as it:
            for e in it:
                try:
                    if e.is_file(follow_symlinks=False) and not e.is_symlink():
                        st = e.stat(follow_symlinks=False)
                        out[e.name] = (st.st_size, st.st_mtime_ns)
                except OSError:
                    continue
    except OSError:
        pass
    return out


def watch(folder: str | Path, *, interval: float = 30, settle_seconds: float = 150,
          stop_event: threading.Event | None = None, run_fn: Callable[[Any], Any] = auto_run,
          clock: Callable[[], float] = time.monotonic) -> int:
    """Poll `folder`; once the listing has changed and then stayed still for
    `settle_seconds`, call run_fn(folder) once. A download that is still
    growing keeps changing the listing, so it is never caught half-written
    (the scanner also skips partial downloads). Returns the number of runs."""
    stop = stop_event or threading.Event()
    handled = listing(folder)  # what is there at start is not "new"
    last = handled
    changed_at: float | None = None
    runs = 0
    while not stop.wait(interval):
        cur = listing(folder)
        if cur != last:
            last, changed_at = cur, clock()
        if changed_at is None or clock() - changed_at < settle_seconds:
            continue
        changed_at = None
        if cur == handled:  # changed and changed back
            continue
        try:
            run_fn(folder)
        except Exception:  # noqa: BLE001 - one bad run must not end the watcher
            log.exception("automatic run failed")
        runs += 1
        # Our own moves change the listing; they are not a new download.
        handled = last = listing(folder)
    return runs


# --------------------------------------------------------------------------
# Launch commands


def frozen() -> bool:
    """True inside the installed SlothFile.exe (PyInstaller build)."""
    return bool(getattr(sys, "frozen", False))


def gui_python(python_exe: str | None = None) -> str:
    """pythonw.exe next to the interpreter (no console window), else the
    interpreter itself. In the installed app: SlothFile.exe itself."""
    if frozen() and python_exe is None:
        # Prefer the windowless SlothFile.exe even when running from SlothFileCLI.exe,
        # so scheduled runs and the right-click menu never flash a console.
        gui = Path(sys.executable).with_name("SlothFile.exe")
        return str(gui if gui.is_file() else sys.executable)
    exe = Path(python_exe or sys.executable)
    w = exe.with_name("pythonw.exe")
    return str(w if w.is_file() else exe)


def launch_prefix(python_exe: str | None = None) -> str:
    """How to start SlothFile from Task Scheduler, the right-click menu or
    Win+R: the installed exe directly, or pythonw -m slothfile for a source checkout."""
    if frozen() and python_exe is None:
        return _quote_arg(gui_python())
    return f"{_quote_arg(gui_python(python_exe))} -m slothfile"


def _quote_arg(path: str) -> str:
    """Quote for the Windows command line; a trailing backslash would
    otherwise escape the closing quote (C:\\ -> "C:\\\\")."""
    if '"' in path:
        raise ValueError("paths cannot contain quotes")
    if path.endswith("\\"):
        path += "\\"
    return f'"{path}"'


# --------------------------------------------------------------------------
# Task Scheduler (current user, no elevation)


_HHMM = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
DAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


def _schtasks(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([_system32("schtasks.exe"), *args], capture_output=True, text=True,
                          encoding="oem" if os.name == "nt" else None, errors="replace",
                          timeout=60, creationflags=_NO_WINDOW, stdin=subprocess.DEVNULL)


def _check_ok(r: subprocess.CompletedProcess, what: str) -> None:
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip() or f"exit code {r.returncode}"
        raise RuntimeError(f"Task Scheduler could not {what}: {msg}")


def _check_at(at: str) -> None:
    if not isinstance(at, str) or not _HHMM.fullmatch(at):
        raise ValueError(f"time must be HH:MM (24-hour), got {at!r}")


def tidy_task_name(folder: str | Path) -> str:
    key = os.path.normcase(str(executor.canonical(folder)))
    return f"{TASK_FOLDER}\\Tidy {hashlib.sha256(key.encode('utf-8')).hexdigest()[:8]}"


def _no_percent(path: str) -> None:
    """Task Scheduler expands %VAR% inside /TR (quoted or not), so a path
    with % would run against a different folder than the one chosen."""
    if "%" in str(path):
        raise ValueError(f"paths containing % cannot be scheduled: {path}")


def _tr(args: str, python_exe: str | None = None) -> str:
    _no_percent(gui_python(python_exe))
    _no_percent(args)
    tr = f"{launch_prefix(python_exe)} {args}"
    if len(tr) > MAX_TR:
        raise ValueError(f"the task command is {len(tr)} characters; Task Scheduler allows at most {MAX_TR}. "
                         "Move SlothFile or the folder to a shorter path.")
    return tr


def schedule_daily(folder: str | Path, at: str = "20:00", *, python_exe: str | None = None) -> str:
    """Tidy `folder` every day at `at`, as the current user, only while
    logged on (so the toast has someone to show to)."""
    _check_at(at)
    root = executor.canonical(folder)
    _no_percent(str(root))
    executor.check_root(root)
    name = tidy_task_name(root)
    tr = _tr(f"auto {_quote_arg(str(root))}", python_exe)
    _check_ok(_schtasks(["/Create", "/TN", name, "/TR", tr, "/SC", "DAILY", "/ST", at, "/RL", "LIMITED", "/F"]),
              "create the daily task")
    return name


def schedule_weekly_summary(day: str = "SUN", at: str = "18:00", *, python_exe: str | None = None) -> str:
    _check_at(at)
    day = day.upper() if isinstance(day, str) else day
    if day not in DAYS:
        raise ValueError(f"day must be one of {', '.join(DAYS)}")
    name = f"{TASK_FOLDER}\\Weekly summary"
    tr = _tr("summary --notify", python_exe)
    _check_ok(_schtasks(["/Create", "/TN", name, "/TR", tr, "/SC", "WEEKLY", "/D", day, "/ST", at,
                         "/RL", "LIMITED", "/F"]), "create the weekly summary task")
    return name


def _own_task_name(name: str) -> str:
    """Accept 'SlothFile\\X' or '\\SlothFile\\X'; refuse anything else."""
    if not isinstance(name, str):
        raise ValueError("task name must be text")
    n = name.lstrip("\\")
    head, _, leaf = n.partition("\\")
    if head.lower() != TASK_FOLDER.lower() or not leaf or "\\" in leaf or '"' in leaf or not leaf.strip():
        raise ValueError(f"{name!r} is not a SlothFile task")
    return f"{TASK_FOLDER}\\{leaf}"


def remove_task(name: str) -> None:
    n = _own_task_name(name)
    _check_ok(_schtasks(["/Delete", "/TN", n, "/F"]), f"remove {n}")


def _task_command(name: str) -> str | None:
    """Command line of one task, read from its XML (the CSV 'Task To Run'
    column is not escaped and cannot be parsed reliably)."""
    r = _schtasks(["/Query", "/TN", name, "/XML"])
    if r.returncode != 0:
        return None
    try:
        # Text is already decoded; drop the declaration that names UTF-16.
        xml = re.sub(r"^\s*<\?xml[^>]*\?>", "", r.stdout[r.stdout.index("<"):])
        tree = ET.fromstring(xml)
    except (ValueError, ET.ParseError):
        return None
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    cmd, args = tree.find(".//t:Exec/t:Command", ns), tree.find(".//t:Exec/t:Arguments", ns)
    if cmd is None:
        return None
    return " ".join(x for x in (f'"{cmd.text}"' if cmd.text else "", (args.text if args is not None else "") or "") if x)


def list_tasks(*, with_commands: bool = True) -> list[dict]:
    """Tasks under \\SlothFile\\ only: name, next run, status (and command)."""
    r = _schtasks(["/Query", "/TN", f"\\{TASK_FOLDER}\\", "/FO", "CSV", "/NH"])
    if r.returncode != 0:  # folder does not exist yet: no tasks
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for row in csv.reader(io.StringIO(r.stdout)):
        if len(row) < 3 or not row[0].lower().startswith(f"\\{TASK_FOLDER.lower()}\\"):
            continue
        if row[0] in seen:  # one row per trigger
            continue
        seen.add(row[0])
        item = {"name": row[0].lstrip("\\"), "next_run": row[1], "status": row[2]}
        if with_commands:
            item["command"] = _task_command(row[0])
        out.append(item)
    return out


# --------------------------------------------------------------------------
# Explorer right-click entry and Win+R alias (HKCU only)


CLASSES = r"Software\Classes"
APP_PATHS = r"Software\Microsoft\Windows\CurrentVersion\App Paths"
_MENU_KEYS = (r"Directory\shell\SlothFile", r"Directory\Background\shell\SlothFile")
MENU_TEXT = "Tidy with SlothFile"


def _winreg():
    import winreg  # Windows only; imported lazily so the module loads anywhere
    return winreg


def menu_command(python_exe: str | None = None) -> str:
    return f'{launch_prefix(python_exe)} ui "%V"'


def install_context_menu(python_exe: str | None = None, *, base: str = CLASSES) -> None:
    """Add 'Tidy with SlothFile' to folder and folder-background menus. On
    Windows 11 it appears under 'Show more options'."""
    wr = _winreg()
    cmd = menu_command(python_exe)
    icon = gui_python(python_exe)
    for key in _MENU_KEYS:
        with wr.CreateKeyEx(wr.HKEY_CURRENT_USER, f"{base}\\{key}", 0, wr.KEY_WRITE) as k:
            wr.SetValueEx(k, "", 0, wr.REG_SZ, MENU_TEXT)
            wr.SetValueEx(k, "Icon", 0, wr.REG_SZ, icon)
        with wr.CreateKeyEx(wr.HKEY_CURRENT_USER, f"{base}\\{key}\\command", 0, wr.KEY_WRITE) as k:
            wr.SetValueEx(k, "", 0, wr.REG_SZ, cmd)


def _is_ours(wr, path: str) -> bool:
    try:
        with wr.OpenKey(wr.HKEY_CURRENT_USER, path + "\\command") as k:
            value, _ = wr.QueryValueEx(k, "")
    except OSError:
        return False
    return isinstance(value, str) and ("-m slothfile" in value
                                       or ("slothfile" in value.lower() and value.rstrip().endswith('ui "%V"')))


def _subkeys(wr, path: str) -> list[str]:
    out: list[str] = []
    try:
        with wr.OpenKey(wr.HKEY_CURRENT_USER, path) as k:
            i = 0
            while True:
                try:
                    out.append(wr.EnumKey(k, i))
                except OSError:
                    break
                i += 1
    except OSError:
        pass
    return out


def uninstall_context_menu(*, base: str = CLASSES) -> list[str]:
    """Remove exactly the two keys install_context_menu made. Our "command"
    subkey is always removed; a SlothFile key that also holds subkeys someone
    else added is kept (DeleteKey would refuse it anyway) and reported, and
    the other key is still handled. Returns the keys that were left behind."""
    wr = _winreg()
    kept: list[str] = []
    for key in _MENU_KEYS:
        path = f"{base}\\{key}"
        try:
            if not _is_ours(wr, path):
                continue
            try:
                wr.DeleteKey(wr.HKEY_CURRENT_USER, path + "\\command")
            except FileNotFoundError:
                pass
            extra = [s for s in _subkeys(wr, path) if s.lower() != "command"]
            if extra:
                log.warning("kept %s: it has other subkeys (%s)", path, ", ".join(extra))
                kept.append(path)
                continue
            try:
                wr.DeleteKey(wr.HKEY_CURRENT_USER, path)
            except FileNotFoundError:
                pass
        except OSError as e:
            log.warning("could not remove %s: %s", path, e)
            kept.append(path)
    return kept


def context_menu_installed(*, base: str = CLASSES) -> bool:
    wr = _winreg()
    return all(_is_ours(wr, f"{base}\\{key}") for key in _MENU_KEYS)


def _launcher_path() -> Path:
    d = state_dir() / "launch"
    d.mkdir(exist_ok=True)
    return d / "slothfile.cmd"


_BATCH_UNSAFE = '%"^!'


def launcher_script(py: str) -> bytes:
    """The Win+R launcher batch file, as UTF-8 bytes (no BOM). cmd decodes
    each line with the console code page, so the first lines switch it to
    UTF-8 before the line that holds the path. Characters cmd still
    interprets inside quotes (% always, ! and ^ under delayed expansion,
    and " itself) are refused rather than escaped."""
    bad = sorted({c for c in str(py) if c in _BATCH_UNSAFE})
    if bad:
        raise ValueError(f"python path cannot contain {' '.join(bad)}: {py}")
    return ("@echo off\r\nchcp 65001>nul\r\n"
            f'start "" "{py}" -m slothfile ui\r\n').encode("utf-8")


def install_run_alias(python_exe: str | None = None, *, base: str = APP_PATHS) -> Path:
    """Let the user type 'slothfile' in Win+R. App Paths points at a tiny
    launcher in SlothFile's own folder that starts the UI without a console."""
    wr = _winreg()
    if frozen() and python_exe is None:
        # The installed app is a real exe: point App Paths straight at it.
        with wr.CreateKeyEx(wr.HKEY_CURRENT_USER, f"{base}\\slothfile.exe", 0, wr.KEY_WRITE) as k:
            wr.SetValueEx(k, "", 0, wr.REG_SZ, gui_python())
        return Path(gui_python())
    data = launcher_script(gui_python(python_exe))
    launcher = _launcher_path()
    launcher.write_bytes(data)  # bytes: no newline translation, no code page
    with wr.CreateKeyEx(wr.HKEY_CURRENT_USER, f"{base}\\slothfile.exe", 0, wr.KEY_WRITE) as k:
        wr.SetValueEx(k, "", 0, wr.REG_SZ, str(launcher))
    return launcher


def uninstall_run_alias(*, base: str = APP_PATHS) -> None:
    wr = _winreg()
    path = f"{base}\\slothfile.exe"
    try:
        with wr.OpenKey(wr.HKEY_CURRENT_USER, path) as k:
            value, _ = wr.QueryValueEx(k, "")
    except OSError:
        return
    if os.path.normcase(str(value)) in _alias_targets():
        wr.DeleteKey(wr.HKEY_CURRENT_USER, path)


def _alias_targets() -> set[str]:
    targets = {os.path.normcase(str(_launcher_path()))}
    if frozen():
        targets.add(os.path.normcase(gui_python()))
    return targets


def run_alias_installed(*, base: str = APP_PATHS) -> bool:
    wr = _winreg()
    try:
        with wr.OpenKey(wr.HKEY_CURRENT_USER, f"{base}\\slothfile.exe") as k:
            value, _ = wr.QueryValueEx(k, "")
    except OSError:
        return False
    return os.path.normcase(str(value)) in _alias_targets() and Path(value).is_file()


__all__ = ["notify", "toast_xml", "auto_run", "trusted", "pending", "clear_pending", "listing", "watch",
           "gui_python", "schedule_daily", "schedule_weekly_summary", "list_tasks", "remove_task",
           "tidy_task_name", "install_context_menu", "uninstall_context_menu", "context_menu_installed",
           "menu_command", "launcher_script", "install_run_alias", "uninstall_run_alias", "run_alias_installed"]
