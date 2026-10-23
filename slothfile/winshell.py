"""Thin, careful wrappers around the Windows shell for Storage Cleanup.

Why ctypes and not `cmd /c del` or PowerShell one-liners: the Recycle Bin is
only reachable through the shell API, and calling it directly means no path
is ever pasted into a command line where quoting mistakes could widen what
gets removed. Every function refuses links/junctions and anything that is not
a regular file. On macOS the same functions dispatch to `macos` (the Trash,
Finder, `ps`). On other operating systems the destructive functions raise
NotImplementedError and the informational ones return safe defaults.
"""

from __future__ import annotations

import base64
import csv
import io
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

from .pathsafe import UnsafePath, is_link_like

BATCH = 200  # paths per SHFileOperationW call; keeps the double-NUL list small
_SHELL_MAX_PATH = 259  # SHFileOperationW does not accept \\?\ long paths
_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW: no console flash from the web server

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _SHFILEOPSTRUCTW(ctypes.Structure):
        # shellapi.h packs this to 1 byte on 32-bit Windows only.
        _pack_ = 1 if ctypes.sizeof(ctypes.c_void_p) == 4 else 8
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
                    ("pFrom", ctypes.c_void_p), ("pTo", ctypes.c_void_p),
                    ("fFlags", wintypes.WORD), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

    class _SHQUERYRBINFO(ctypes.Structure):
        _pack_ = 1 if ctypes.sizeof(ctypes.c_void_p) == 4 else 8
        _fields_ = [("cbSize", wintypes.DWORD), ("i64Size", ctypes.c_int64),
                    ("i64NumItems", ctypes.c_int64)]

    _SHFileOperationW = _shell32.SHFileOperationW
    _SHFileOperationW.argtypes = [ctypes.POINTER(_SHFILEOPSTRUCTW)]
    _SHFileOperationW.restype = ctypes.c_int

    _SHQueryRecycleBinW = _shell32.SHQueryRecycleBinW
    _SHQueryRecycleBinW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(_SHQUERYRBINFO)]
    _SHQueryRecycleBinW.restype = ctypes.c_long

    # Module attribute on purpose: tests replace it so the real bin is never emptied.
    _SHEmptyRecycleBinW = _shell32.SHEmptyRecycleBinW
    _SHEmptyRecycleBinW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.DWORD]
    _SHEmptyRecycleBinW.restype = ctypes.c_long

    _CreateFileW = _k32.CreateFileW
    _CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                             wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _CreateFileW.restype = wintypes.HANDLE
    _CloseHandle = _k32.CloseHandle
    _CloseHandle.argtypes = [wintypes.HANDLE]
    _INVALID = wintypes.HANDLE(-1).value
else:  # pragma: no cover - non-Windows
    _SHEmptyRecycleBinW = None

FO_DELETE = 0x3
FOF_SILENT = 0x4
FOF_NOCONFIRMATION = 0x10
FOF_ALLOWUNDO = 0x40
FOF_NOERRORUI = 0x400
FOF_WANTNUKEWARNING = 0x4000  # ask instead of silently destroying a file the bin can't take
SHERB_NOCONFIRMATION = 0x1
SHERB_NOPROGRESSUI = 0x2
SHERB_NOSOUND = 0x4
S_OK = 0
E_UNEXPECTED = -2147418113  # 0x8000FFFF: what Windows returns when the bin is already empty


def _regular_file(p: Path) -> bool:
    try:
        st = os.lstat(p)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and not is_link_like(p)


def _twin_name(p: str | Path) -> bool:
    """True if the final component ends in a space or dot. Win32 path handling
    strips that ending, so acting on such a path would hit a different file
    (the "~report.pdf" behind a "~report.pdf " twin)."""
    s = os.fspath(p)
    if s.endswith(("\\", "/")):
        return True  # "x.pdf \" is the same trick with a trailing separator
    name = s.replace("/", "\\").rsplit("\\", 1)[-1]
    return name != name.rstrip(" .")


def is_alias_path(p: str | Path) -> bool:
    """True if `p` names a file indirectly: an NTFS stream suffix ("x.pdf::$DATA"),
    an 8.3 short name ("~PHOTO~1.JPE"), or a trailing space/dot twin. Any of these
    would let a type check see one name while Windows acts on another file."""
    s = os.fspath(p)
    if _twin_name(s) or ":" in os.path.splitdrive(s)[1]:
        return True
    if os.name != "nt":
        return False
    try:
        real = os.path.realpath(s)  # long names, as stored on disk
    except (OSError, ValueError):
        return True
    return os.path.normcase(real) != os.path.normcase(os.path.abspath(s))


def _long_form(p: Path) -> Path:
    """Absolute path with the parent expanded from 8.3/subst spelling, so the
    Recycle Bin records the same 'Original Location' we later search for. The
    file itself is not resolved (it was already checked not to be a link)."""
    p = Path(os.path.abspath(p))
    return Path(os.path.realpath(p.parent)) / p.name


def _require_windows() -> None:
    if os.name != "nt":
        raise NotImplementedError("the Recycle Bin is only available on Windows")


def is_in_use(path: Path) -> bool:
    """True if another program holds the file open without delete sharing,
    i.e. deleting or recycling it would fail with a sharing violation. Asking
    first avoids a shell 'File In Use' prompt during a batch."""
    if os.name != "nt":
        return False
    DELETE, SHARE_ALL, OPEN_EXISTING, OPEN_REPARSE = 0x10000, 0x7, 3, 0x00200000
    h = _CreateFileW(str(path), DELETE, SHARE_ALL, None, OPEN_EXISTING, OPEN_REPARSE, None)
    if h in (None, _INVALID):
        return ctypes.get_last_error() == 32  # ERROR_SHARING_VIOLATION
    _CloseHandle(h)
    return False


_MAX_TRASHED = 1000
_trashed: dict[str, str] = {}  # str(path) -> its path inside the macOS Trash
_trashed_lock = threading.Lock()


def _is_mac() -> bool:
    return sys.platform == "darwin"


def take_trash_path(path: str | Path) -> str | None:
    """macOS: where the last recycle() put `path` inside the Trash (once), so the
    caller can journal it and undo can put back exactly that item."""
    with _trashed_lock:
        return _trashed.pop(str(path), None) or None


def _recycle_mac(paths: list[Path]) -> dict[str, str | None]:
    from . import macos
    result: dict[str, str | None] = {}
    for p in paths:
        key = str(p)
        p = Path(p)
        if is_link_like(p):
            result[key] = "is a link or alias; refusing"
            continue
        if not _regular_file(p):
            result[key] = "not a regular file"
            continue
        try:
            where = macos.trash(p)
        except OSError as e:
            result[key] = str(e) or type(e).__name__
            continue
        if os.path.lexists(p):
            result[key] = "still present after moving to the Trash"
            continue
        result[key] = None
        if where:
            with _trashed_lock:
                _trashed[key] = where
                while len(_trashed) > _MAX_TRASHED:
                    _trashed.pop(next(iter(_trashed)))
    return result


def recycle_paths(paths: list[Path]) -> dict[str, tuple[str | None, str | None]]:
    """Like recycle(), but returns {str(path): (trash_path or None, error or None)}.
    The trash path is only known on macOS."""
    res = recycle(paths)
    return {k: (take_trash_path(k) if err is None else None, err) for k, err in res.items()}


def recycle(paths: list[Path]) -> dict[str, str | None]:
    """Send regular files to the Recycle Bin. Returns {str(path): None if gone,
    else a readable reason}. Success is judged by the disk afterwards, not by
    the API's return code, which can be nonzero after partial success."""
    if _is_mac():
        return _recycle_mac(paths)
    _require_windows()
    result: dict[str, str | None] = {}
    todo: list[tuple[str, Path]] = []
    for p in paths:
        key = str(p)
        if _twin_name(p):
            result[key] = "name ends in a space or dot; refusing"
            continue
        if is_alias_path(p):
            result[key] = "alias name (stream or short name); refusing"
            continue
        p = Path(p)
        if is_link_like(p):
            result[key] = "is a link or junction; refusing"
        elif not _regular_file(p):
            result[key] = "not a regular file"
        else:
            full = _long_form(p)
            s = str(full)
            if len(s) > _SHELL_MAX_PATH:
                result[key] = "path too long for the Recycle Bin"
            elif any(c in s for c in "*?\0"):
                result[key] = "unsupported characters in path"  # the shell would expand wildcards
            else:
                todo.append((key, full))
    flags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI | FOF_WANTNUKEWARNING
    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        buf = ctypes.create_unicode_buffer("\0".join(str(f) for _, f in chunk) + "\0\0")
        op = _SHFILEOPSTRUCTW(hwnd=None, wFunc=FO_DELETE, pFrom=ctypes.addressof(buf),
                              pTo=None, fFlags=flags)
        rc = _SHFileOperationW(ctypes.byref(op))
        for key, full in chunk:
            if not os.path.lexists(full):
                result[key] = None
            elif op.fAnyOperationsAborted:
                result[key] = "cancelled"
            else:
                result[key] = f"still present (shell error 0x{rc & 0xFFFFFFFF:X})"
    return result


_RESTORE_PS = r"""
$ErrorActionPreference = 'Stop'
$target = $env:SLOTHFILE_RESTORE_TARGET
if (-not $target) { exit 3 }
$dir  = [System.IO.Path]::GetDirectoryName($target).TrimEnd('\')
$leaf = [System.IO.Path]::GetFileName($target)
$stem = [System.IO.Path]::GetFileNameWithoutExtension($target)
$ext  = [System.IO.Path]::GetExtension($target)
$shell = New-Object -ComObject Shell.Application
$bin = $shell.Namespace(10)
$best = $null; $bestDate = [datetime]::MinValue
foreach ($item in @($bin.Items())) {
  $from = $null
  try { $from = [string]$item.ExtendedProperty('System.Recycle.DeletedFrom') } catch {}
  if (-not $from) { try { $from = [string]$bin.GetDetailsOf($item, 1) } catch {} }
  if (-not $from -or ($from.TrimEnd('\') -ine $dir)) { continue }
  $name = [string]$item.Name
  # Explorer may hide extensions in Name; the bin's own $R file keeps the real one.
  $realExt = [System.IO.Path]::GetExtension([string]$item.Path)
  if (-not (($name -ieq $leaf) -or (($name -ieq $stem) -and ($realExt -ieq $ext)))) { continue }
  $d = [datetime]::MinValue
  try { $d = [datetime]$item.ExtendedProperty('System.Recycle.DateDeleted') } catch {}
  if ($null -eq $best -or $d -gt $bestDate) { $best = $item; $bestDate = $d }
}
if ($null -eq $best) { exit 2 }
foreach ($v in @($best.Verbs())) {
  $n = ([string]$v.Name).Replace('&', '').Trim()
  if ($n -match '^(restore|undelete)$') { $v.DoIt(); exit 0 }
}
$best.InvokeVerb('undelete')  # language-neutral canonical verb
exit 0
"""


def _restore_mac(original_path: Path, trashed: str | Path | None) -> bool:
    from . import macos
    target = Path(os.path.abspath(original_path))
    if os.path.lexists(target):
        return False
    if not trashed:
        # Unknown location (e.g. recovered after a crash): the Trash keeps the
        # name unless it was taken. The caller verifies the fingerprint.
        trashed = Path.home() / ".Trash" / target.name
    return macos.restore_from_trash(trashed, target)


def restore_from_recycle_bin(original_path: Path, timeout: float = 30, *,
                             trashed: str | Path | None = None) -> bool:
    """Best-effort: put back the most recently recycled item that came from
    `original_path`. The path travels in an environment variable, never inside
    the script text, so no file name can change what the script does. True
    only if the file exists again afterwards. On macOS `trashed` names the
    item inside the Trash (from recycle_paths / take_trash_path)."""
    if _is_mac():
        return _restore_mac(original_path, trashed)
    _require_windows()
    target = _long_form(Path(original_path))
    if os.path.lexists(target):
        return False  # restoring would ask to overwrite; let the user decide
    env = dict(os.environ, SLOTHFILE_RESTORE_TARGET=str(target))
    encoded = base64.b64encode(_RESTORE_PS.encode("utf-16-le")).decode("ascii")
    deadline = time.monotonic() + timeout
    try:
        subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                        "-EncodedCommand", encoded],
                       env=env, capture_output=True, timeout=timeout, creationflags=_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return os.path.exists(target)
    # The shell may finish the restore just after the verb returns.
    poll_end = min(deadline, time.monotonic() + 5)
    while not os.path.exists(target) and time.monotonic() < poll_end:
        time.sleep(0.1)
    return os.path.exists(target)


def recycle_bin_info() -> tuple[int, int]:
    """(bytes, items) in the Recycle Bins of all drives; (0, 0) if unknown.
    On macOS: the user's Trash."""
    if _is_mac():
        from . import macos
        items, size = macos.trash_info()
        return (size, items)
    if os.name != "nt":
        return (0, 0)
    info = _SHQUERYRBINFO(cbSize=ctypes.sizeof(_SHQUERYRBINFO))
    if _SHQueryRecycleBinW(None, ctypes.byref(info)) != S_OK:
        return (0, 0)
    return (int(info.i64Size), int(info.i64NumItems))


def empty_recycle_bin() -> None:
    """Permanently empty every Recycle Bin. Irreversible: callers must have an
    explicit, separate confirmation (see cleanup.empty_recycle_bin_confirmed).
    On macOS this empties the Trash through Finder."""
    if _is_mac():
        from . import macos
        macos.empty_trash()
        return
    _require_windows()
    hr = _SHEmptyRecycleBinW(None, None, SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND)
    if hr not in (S_OK, E_UNEXPECTED):
        raise OSError(f"emptying the Recycle Bin failed (HRESULT 0x{hr & 0xFFFFFFFF:08X})")


def delete_permanently(path: Path) -> None:
    """os.remove, but only for a regular file that is not a link or junction."""
    if _twin_name(path):
        raise UnsafePath(f"{path} ends in a space or dot; refusing")
    if is_alias_path(path):
        raise UnsafePath(f"{path} is an alias name (stream or short name); refusing")
    path = Path(path)
    if is_link_like(path):
        raise UnsafePath(f"{path} is a link or junction; refusing")
    if not _regular_file(path):
        raise UnsafePath(f"{path} is not a regular file")
    os.remove(path)


def disk_free(path: str | Path) -> int:
    """Free bytes on the volume holding `path` (nearest existing ancestor)."""
    p = Path(os.path.abspath(path))
    while not p.exists() and p.parent != p:
        p = p.parent
    try:
        return shutil.disk_usage(p).free
    except OSError:
        return 0


def running_processes() -> set[str]:
    """Lower-case executable names of running processes. Raises OSError if the
    list can't be read, so callers can block rather than guess."""
    if _is_mac():
        from . import macos
        return macos.running_processes()
    if os.name != "nt":
        return set()
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, timeout=30,
                             creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired as e:
        raise OSError("tasklist timed out") from e
    if out.returncode != 0:
        raise OSError(f"tasklist failed with exit code {out.returncode}")
    text = out.stdout.decode("mbcs", errors="replace")
    names = set()
    for row in csv.reader(io.StringIO(text)):
        if row and row[0].strip():
            names.add(row[0].strip().lower())
    return names
