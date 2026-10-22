"""Pin a folder chain against rename/replace while a move happens.

Checking "is this folder a junction?" and then renaming by path leaves a gap in
which another process could swap the folder for a junction. On Windows we
close that gap by opening every folder from the root down to the destination
*without* FILE_SHARE_DELETE: while those handles are open, nobody can rename,
delete or replace those folders, so the path we checked is the path the
rename uses. Each handle is opened with FILE_FLAG_OPEN_REPARSE_POINT and
rejected if it is a reparse point. Elsewhere this is a no-op and the
executor's post-move real-path check is the safeguard.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .pathsafe import UnsafePath, is_link_like

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _CreateFileW = _k32.CreateFileW
    _CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                             wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _CreateFileW.restype = wintypes.HANDLE
    _CloseHandle = _k32.CloseHandle
    _CloseHandle.argtypes = [wintypes.HANDLE]

    class _BHFI(ctypes.Structure):
        _fields_ = [("dwFileAttributes", wintypes.DWORD), ("ftCreationTime", wintypes.FILETIME),
                    ("ftLastAccessTime", wintypes.FILETIME), ("ftLastWriteTime", wintypes.FILETIME),
                    ("dwVolumeSerialNumber", wintypes.DWORD), ("nFileSizeHigh", wintypes.DWORD),
                    ("nFileSizeLow", wintypes.DWORD), ("nNumberOfLinks", wintypes.DWORD),
                    ("nFileIndexHigh", wintypes.DWORD), ("nFileIndexLow", wintypes.DWORD)]

    _GetInfo = _k32.GetFileInformationByHandle
    _GetInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_BHFI)]

    _GENERIC_READ = 0x80000000
    _SHARE_READ_WRITE = 0x1 | 0x2  # deliberately no FILE_SHARE_DELETE
    _OPEN_EXISTING = 3
    _FLAGS = 0x02000000 | 0x00200000  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
    _REPARSE = 0x400
    _INVALID = wintypes.HANDLE(-1).value


@contextmanager
def pinned_dirs(root: Path, rel_dir: Path) -> Iterator[None]:
    """Hold root and every folder of root/rel_dir open (no delete sharing)."""
    chain = [Path(root)]
    for part in rel_dir.parts:
        chain.append(chain[-1] / part)
    if os.name != "nt":
        for d in chain[1:]:
            if is_link_like(d):
                raise UnsafePath(f"{d} is a link or junction")
        yield
        return
    handles = []
    try:
        for d in chain:
            h = _CreateFileW(str(d), _GENERIC_READ, _SHARE_READ_WRITE, None, _OPEN_EXISTING, _FLAGS, None)
            if h in (None, _INVALID):
                err = ctypes.get_last_error()
                raise UnsafePath(f"cannot lock folder {d} for the move (error {err})")
            handles.append(h)
            info = _BHFI()
            if not _GetInfo(h, ctypes.byref(info)):
                raise UnsafePath(f"cannot inspect folder {d}")
            if info.dwFileAttributes & _REPARSE:
                raise UnsafePath(f"{d} is a link or junction; refusing to follow it")
        yield
    finally:
        for h in reversed(handles):
            _CloseHandle(h)


def real_inside(root: Path, path: Path) -> bool:
    """After a move: does the file's real (link-resolved) location sit inside
    the real root? Catches any escape the pinning could not prevent."""
    real_root = os.path.normcase(os.path.realpath(root))
    real = os.path.normcase(os.path.realpath(path))
    try:
        return os.path.commonpath([real_root, real]) == real_root and real != real_root
    except ValueError:
        return False
