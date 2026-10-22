"""Read-only lookup of installed programs (Windows "Apps & features" list), used
to suggest that an installer in Downloads has already done its job."""

from __future__ import annotations

import os
import re
from functools import lru_cache

_UNINSTALL_KEYS = [
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
]
# Words in installer names that say nothing about *which* program it is.
_NOISE = {"setup", "installer", "install", "win", "win32", "win64", "windows", "x64", "x86", "amd64",
          "arm64", "msi", "exe", "full", "offline", "online", "web", "stable", "release", "latest",
          "user", "system", "portable", "beta", "lts", "bit", "the", "for", "and", "version", "update",
          # Generic words that name no particular program (would match many unrelated apps).
          "driver", "drivers", "client", "launcher", "app", "apps", "tool", "tools", "helper", "host",
          "service", "runtime", "agent", "amd", "arm", "intel", "pro", "free", "plus", "lite", "desktop",
          "manager", "utility", "suite", "studio", "player", "pack", "package", "redist", "redistributable"}
_ARCH = re.compile(r"(?i)(amd64|x86_64|x64|x86|arm64|aarch64|win(32|64))")


@lru_cache(maxsize=1)
def installed_programs() -> frozenset[str]:
    """Lower-cased DisplayName of every installed program (HKLM + HKCU), read only."""
    if os.name != "nt":
        return frozenset()
    import winreg
    names: set[str] = set()
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for path in _UNINSTALL_KEYS:
            try:
                key = winreg.OpenKey(hive, path)
            except OSError:
                continue
            with key:
                for i in range(winreg.QueryInfoKey(key)[0]):
                    try:
                        with winreg.OpenKey(key, winreg.EnumKey(key, i)) as sub:
                            name, _ = winreg.QueryValueEx(sub, "DisplayName")
                    except OSError:
                        continue
                    if isinstance(name, str) and name.strip():
                        names.add(name.strip().lower())
    return frozenset(names)


def name_tokens(filename: str) -> list[str]:
    """'ChromeSetup.exe' -> ['chrome'], 'vlc-3.0.21-win64.exe' -> ['vlc']."""
    stem = _ARCH.sub(" ", os.path.splitext(filename)[0])
    stem = re.sub(r"([a-z])([A-Z])", r"\1 \2", stem)  # split CamelCase
    words = re.findall(r"[A-Za-z]{3,}", stem)
    return [w.lower() for w in words if w.lower() not in _NOISE]


def matching_program(filename: str, programs: frozenset[str] | set[str] | None = None) -> str | None:
    """Name of an installed program this installer seems to belong to, else None.
    Requires every distinctive word of the file name to appear as a whole word."""
    programs = installed_programs() if programs is None else programs
    tokens = name_tokens(filename)
    if not tokens:
        return None
    for prog in sorted(programs):
        words = set(re.findall(r"[a-z]{3,}", prog))
        if all(t in words for t in tokens):
            return prog
    return None
