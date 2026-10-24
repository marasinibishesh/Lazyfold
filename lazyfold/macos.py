"""macOS counterparts of the Windows shell helpers (Trash, notifications,
download origin, installed apps, OCR).

Import-safe on every OS: nothing here touches pyobjc, libc or a subprocess at
import time. pyobjc (Foundation/Vision) is optional and imported lazily inside
the functions that use it, with a plain fallback when it is missing. No path or
user text is ever pasted into a shell command or into AppleScript source: values
travel as separate argv entries (`on run argv`), so a hostile file name or
notification title stays data. Nothing leaves the computer.
"""

from __future__ import annotations

import errno
import os
import plistlib
import stat
import subprocess
import sys
from pathlib import Path

IS_MAC: bool = sys.platform == "darwin"

XATTR_NOFOLLOW = 0x0001
RENAME_EXCL = 0x00000004
WHERE_FROMS_XATTR = "com.apple.metadata:kMDItemWhereFroms"
MAX_XATTR = 64 * 1024  # a where-froms list is a few hundred bytes; refuse anything huge
MAX_URLS = 16
MAX_URL_LEN = 4096
_TIMEOUT = 30


def _is_mac() -> bool:
    return sys.platform == "darwin"


# --- libc ---------------------------------------------------------------------

_libc_handle = None


def _libc():
    """libc via ctypes, or None when unavailable (any non-mac OS, or a failure)."""
    global _libc_handle
    if _libc_handle is not None:
        return _libc_handle
    if not _is_mac():
        return None
    try:
        import ctypes
        import ctypes.util
        lib = ctypes.CDLL(ctypes.util.find_library("c") or "libc.dylib", use_errno=True)
    except (OSError, AttributeError, ImportError):
        return None
    _libc_handle = lib
    return lib


def rename_excl(src: str | Path, dst: str | Path) -> bool:
    """Atomic rename that fails if `dst` exists (renamex_np with RENAME_EXCL).
    True on success; False if renamex_np is unavailable here (caller falls back).
    Raises FileExistsError if `dst` exists, OSError for any other failure."""
    lib = _libc()
    if lib is None:
        return False
    try:
        import ctypes
        fn = lib.renamex_np
        fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        fn.restype = ctypes.c_int
    except (AttributeError, ImportError):
        return False
    rc = fn(os.fsencode(os.fspath(src)), os.fsencode(os.fspath(dst)), RENAME_EXCL)
    if rc == 0:
        return True
    err = ctypes.get_errno()
    if err == errno.EEXIST:
        raise FileExistsError(errno.EEXIST, "destination exists", str(dst))
    if err in (errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP), errno.ENOSYS, errno.EINVAL):
        return False  # filesystem without RENAME_EXCL support: let the caller use link+unlink
    raise OSError(err, os.strerror(err), str(src))


def _read_xattr(path: Path, name: str) -> bytes | None:
    """Raw value of extended attribute `name` (not following links), or None."""
    lib = _libc()
    if lib is None:
        return None
    try:
        import ctypes
        fn = lib.getxattr
        # darwin: ssize_t getxattr(path, name, value, size, u_int32_t position, int options)
        fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t,
                       ctypes.c_uint32, ctypes.c_int]
        fn.restype = ctypes.c_ssize_t
        p = os.fsencode(os.fspath(path))
        n = name.encode("utf-8")
        size = fn(p, n, None, 0, 0, XATTR_NOFOLLOW)
        if size <= 0 or size > MAX_XATTR:
            return None
        buf = ctypes.create_string_buffer(size)
        got = fn(p, n, buf, size, 0, XATTR_NOFOLLOW)
        if got <= 0 or got > size:
            return None
        return buf.raw[:got]
    except Exception:  # noqa: BLE001 - any ctypes/OS problem means "no origin known"
        return None


def where_froms(path: Path) -> list[str]:
    """URLs recorded by the browser in the kMDItemWhereFroms xattr (download URL
    first, then the referring page). Untrusted data; [] on any error."""
    if not _is_mac():
        return []
    raw = _read_xattr(Path(path), WHERE_FROMS_XATTR)
    if not raw:
        return []
    return _parse_where_froms(raw)


def _parse_where_froms(raw: bytes) -> list[str]:
    try:
        val = plistlib.loads(raw)
    except Exception:  # noqa: BLE001 - garbage, truncated or hostile plist
        return []
    if isinstance(val, str):
        val = [val]
    if not isinstance(val, list):
        return []
    return [s for s in val if isinstance(s, str) and s and len(s) <= MAX_URL_LEN][:MAX_URLS]


# --- Trash --------------------------------------------------------------------

def _in_trash_dir(p: str | Path) -> bool:
    """True if `p` lies inside a Trash folder (~/.Trash or /Volumes/X/.Trashes/uid)."""
    parts = Path(os.path.abspath(p)).parts[:-1]
    return any(part in (".Trash", ".Trashes") for part in parts)


def _run(cmd: list[str], timeout: float = _TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, timeout=timeout, shell=False, stdin=subprocess.DEVNULL)


_FINDER_DELETE = [
    "on run argv",
    'tell application "Finder"',
    "set t to delete (POSIX file (item 1 of argv) as alias)",
    "end tell",
    "return POSIX path of (t as alias)",
    "end run",
]


def trash(path: Path) -> str:
    """Move one file or folder to the Trash. Returns its new path inside the
    Trash ("" if unknown). Raises OSError on failure."""
    full = os.path.abspath(os.fspath(path))
    if not os.path.lexists(full):
        raise FileNotFoundError(errno.ENOENT, "no such file", full)
    try:
        from Foundation import NSURL, NSFileManager  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 - pyobjc not installed (or not a Mac)
        NSFileManager = None  # noqa: N806
    if NSFileManager is not None:
        url = NSURL.fileURLWithPath_(full)
        ok, new_url, err = NSFileManager.defaultManager().trashItemAtURL_resultingItemURL_error_(url, None, None)
        if not ok:
            raise OSError(errno.EIO, f"could not move to the Trash: {err}", full)
        new = str(new_url.path()) if new_url is not None else ""
        return new if new and os.path.lexists(new) else ""
    cmd = ["osascript"]
    for line in _FINDER_DELETE:
        cmd += ["-e", line]
    cmd.append(full)
    try:
        proc = _run(cmd)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise OSError(errno.EIO, f"could not ask Finder to move it to the Trash ({e})", full) from e
    if os.path.lexists(full):
        msg = (proc.stderr or b"").decode("utf-8", "replace").strip()[:200]
        raise OSError(errno.EIO, f"still present after moving to the Trash{': ' + msg if msg else ''}", full)
    new = (proc.stdout or b"").decode("utf-8", "replace").strip()
    if new.endswith("/") and len(new) > 1:
        new = new[:-1]
    return new if new and _in_trash_dir(new) and os.path.lexists(new) else ""


def restore_from_trash(trashed: str | Path, original: Path) -> bool:
    """Move a trashed item back to `original`, never overwriting anything.
    False if the trashed item is gone, is not inside a Trash folder, or the
    original name is taken (or its folder no longer exists)."""
    src = os.path.abspath(os.fspath(trashed))
    dst = os.path.abspath(os.fspath(original))
    if not src or not _in_trash_dir(src):
        return False
    try:
        st = os.lstat(src)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return False
    if os.path.lexists(dst) or not os.path.isdir(os.path.dirname(dst)):
        return False
    try:
        if not rename_excl(src, dst):
            _link_move(src, dst)
    except OSError:
        return False
    return os.path.lexists(dst) and not os.path.lexists(src)


def _link_move(src: str, dst: str) -> None:
    """No-overwrite move without renamex_np: hard link + unlink for files; for
    folders (no hard links) rename after checking the name is free."""
    if os.path.isdir(src) and not os.path.islink(src):
        if os.path.lexists(dst):
            raise FileExistsError(errno.EEXIST, "destination exists", dst)
        os.rename(src, dst)
        return
    try:
        os.link(src, dst)
    except OSError as e:
        if e.errno == errno.EEXIST or os.path.lexists(dst):
            raise FileExistsError(errno.EEXIST, "destination exists", dst) from e
        os.rename(src, dst)  # filesystem without hard links: checked just above
        return
    os.unlink(src)


def _trash_dir() -> Path:
    return Path.home() / ".Trash"


def trash_info() -> tuple[int, int]:
    """(items, bytes) in ~/.Trash; (0, 0) when unreadable (macOS privacy)."""
    root = _trash_dir()
    try:
        with os.scandir(root) as it:
            entries = list(it)
    except OSError:
        return (0, 0)
    items = 0
    total = 0
    for e in entries:
        if e.name == ".DS_Store":
            continue
        items += 1
        try:
            st = e.stat(follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISDIR(st.st_mode):
            for dirpath, dirnames, filenames in os.walk(e.path, followlinks=False):
                for f in filenames:
                    try:
                        total += os.lstat(os.path.join(dirpath, f)).st_size
                    except OSError:
                        pass
        else:
            total += st.st_size
    return (items, total)


def empty_trash() -> None:
    """Permanently empty the Trash through Finder. Irreversible: callers must have
    an explicit, separate confirmation. Raises OSError on failure."""
    try:
        proc = _run(["osascript", "-e", 'tell application "Finder" to empty trash'], timeout=600)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise OSError(errno.EIO, f"emptying the Trash failed ({e})") from e
    if proc.returncode != 0:
        msg = (proc.stderr or b"").decode("utf-8", "replace").strip()[:200]
        raise OSError(errno.EIO, f"emptying the Trash failed (exit {proc.returncode}){': ' + msg if msg else ''}")


# --- processes, notifications, apps -----------------------------------------

def running_processes() -> set[str]:
    """Lower-case executable names of running processes. Raises OSError if the
    list can't be read, so callers can block rather than guess."""
    try:
        proc = _run(["ps", "-axco", "comm="])
    except subprocess.TimeoutExpired as e:
        raise OSError("ps timed out") from e
    if proc.returncode != 0:
        raise OSError(f"ps failed with exit code {proc.returncode}")
    text = (proc.stdout or b"").decode("utf-8", errors="replace")
    return {line.strip().lower() for line in text.splitlines() if line.strip()}


def notify(title: str, body: str) -> bool:
    """Show a notification. Title and body are argv items, never AppleScript source."""
    cmd = ["osascript",
           "-e", "on run argv",
           "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
           "-e", "end run",
           str(title)[:200], str(body)[:1000]]
    try:
        proc = _run(cmd, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _app_dirs() -> list[Path]:
    return [Path("/Applications"), Path("/Applications/Utilities"), Path.home() / "Applications"]


def installed_apps() -> frozenset[str]:
    """Lower-case names (without ".app") of apps in /Applications and
    ~/Applications, including apps one folder down (e.g. "Adobe X/Adobe X.app")."""
    names: set[str] = set()
    for d in _app_dirs():
        try:
            with os.scandir(d) as it:
                entries = list(it)
        except OSError:
            continue
        for e in entries:
            if e.name.lower().endswith(".app"):
                names.add(e.name[:-4].strip().lower())
                continue
            try:
                if not e.is_dir(follow_symlinks=False) or e.name.startswith("."):
                    continue
                with os.scandir(e.path) as sub:
                    for s in sub:
                        if s.name.lower().endswith(".app"):
                            names.add(s.name[:-4].strip().lower())
            except OSError:
                continue
    names.discard("")
    return frozenset(names)


# --- OCR (Apple Vision) -----------------------------------------------------

def ocr_available() -> bool:
    """True when Apple Vision text recognition is reachable through pyobjc."""
    if not _is_mac():
        return False
    try:
        import Vision  # type: ignore[import-not-found]  # noqa: F401
        from Foundation import NSURL  # type: ignore[import-not-found]  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return hasattr(Vision, "VNRecognizeTextRequest")


def ocr_image(path: Path) -> str:
    """Text in an image (accurate recognition, lines joined by newlines); "" on failure."""
    if not ocr_available():
        return ""
    try:
        import Vision  # type: ignore[import-not-found]
        from Foundation import NSURL  # type: ignore[import-not-found]
        try:
            import objc  # type: ignore[import-not-found]
            pool = objc.autorelease_pool()
        except Exception:  # noqa: BLE001
            import contextlib
            pool = contextlib.nullcontext()
        with pool:
            url = NSURL.fileURLWithPath_(os.path.abspath(os.fspath(path)))
            handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(url, {})
            req = Vision.VNRecognizeTextRequest.alloc().init()
            req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
            req.setUsesLanguageCorrection_(True)
            res = handler.performRequests_error_([req], None)
            ok = res[0] if isinstance(res, tuple) else bool(res)
            if not ok:
                return ""
            lines = []
            for obs in req.results() or []:
                cands = obs.topCandidates_(1)
                if cands:
                    lines.append(str(cands[0].string()))
            return "\n".join(lines).strip()
    except Exception:  # noqa: BLE001 - unreadable image or Vision error
        return ""
