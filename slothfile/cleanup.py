"""Storage Cleanup: find leftover temporary and cache files, let the user pick,
then recycle (default) or delete them, and report what was really reclaimed.

Safety model (each point has a test):
- Only user-level folders that hold machine-generated data are scanned; never
  system folders, never anything that needs admin rights.
- Scanning reads metadata only and never follows links, junctions or other
  reparse points. Inside TEMP only known throw-away types are candidates;
  personal-looking and unknown types are listed as "kept" and never touched.
- Execution uses the server-side report, not a list sent by the browser, and
  re-checks every file right before acting: same size and mtime as scanned,
  still a regular file inside a currently valid cleanup folder, still a type
  this category may remove, and not open in another program.
- Recycling is the default. Permanent deletion and emptying the Recycle Bin
  are refused without an explicit confirmation flag.
- Every removed file gets an fsync'ed journal line.

On macOS the same model applies to a separate, explicit set of user-level
folders ($TMPDIR, ~/Library/Logs, named browser and developer caches).
Removing goes to the Trash through the same winshell.recycle path. Roots must
be inside the home folder or the per-user $TMPDIR; ~/Library/Caches as a whole,
Safari, Mail, Photos, iCloud, /Library, /System and /private/var are refused.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import stat
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from . import winshell
from .executor import canonical
from .journal import Journal, now_iso, state_dir
from .pathsafe import UnsafePath, is_link_like, is_within, resolve_inside

MAX_FILES_PER_CATEGORY = 50_000  # keeps a pathological cache from exhausting memory
MAX_PROTECTED = 2_000  # the "kept" list is informational; totals are always exact
LARGEST_IN_SUMMARY = 20
_REPORT_ID = re.compile(r"[A-Za-z0-9-]{1,80}")

_REPARSE = 0x400
_SYSTEM = 0x4
# Cloud placeholders (OneDrive "online-only"): touching them can trigger a download.
_PLACEHOLDER = 0x1000 | 0x40000 | 0x400000  # OFFLINE | RECALL_ON_OPEN | RECALL_ON_DATA_ACCESS

TEMP_EXT = {".tmp", ".temp", "._mp", ".chk", ".gid", ".old"}
LOG_EXT = {".log", ".etl"}  # not .txt: people save notes in TEMP by accident
DUMP_EXT = {".dmp", ".mdmp", ".hdmp"}
INSTALLER_EXT = {".msi", ".msp", ".cab", ".exe", ".dll"}
PERSONAL_EXT = {
    ".pdf", ".doc", ".docx", ".docm", ".xls", ".xlsx", ".xlsm", ".ppt", ".pptx", ".odt", ".ods",
    ".odp", ".txt", ".rtf", ".csv", ".md", ".pages", ".numbers", ".key", ".epub",
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".heic", ".heif", ".raw",
    ".cr2", ".nef", ".arw", ".dng", ".svg", ".psd", ".ai", ".indd", ".xcf",
    ".mp4", ".mov", ".avi", ".mkv", ".wmv", ".webm", ".m4v", ".mp3", ".wav", ".flac", ".m4a",
    ".aac", ".ogg", ".wma", ".zip", ".7z", ".rar", ".tar", ".gz", ".eml", ".msg", ".pst", ".ost",
    ".vcf", ".ics", ".kdbx", ".json", ".xml", ".html", ".htm", ".py", ".js", ".ts", ".java",
    ".c", ".cpp", ".cs", ".sql", ".db", ".sqlite", ".accdb", ".mdb", ".blend", ".dwg", ".skp",
}
KEPT_PERSONAL = "looks like a personal file - kept"
KEPT_UNKNOWN = "unknown type - kept"
KEPT_SYSTEM = "system file - kept"
KEPT_UNUSUAL = "unusual name - kept"


def _unusual_name(name: str) -> bool:
    """A name ending in a space or dot can only be made through \\\\?\\ paths;
    ordinary Win32 calls strip the ending and act on a *different* file (the
    "~report.pdf " twin of "~report.pdf"). Such names are never touched."""
    return name != name.rstrip(" .")


# --------------------------------------------------------------------- locations

Matcher = Callable[[str, int], bool]  # (lower-case file name, depth below root) -> candidate?


@dataclass(frozen=True)
class Location:
    """One kind of removable data. `roots` are env templates ("%LOCALAPPDATA%\\x",
    one '*' segment allowed for browser profiles), concrete Paths (tests), or a
    callable returning Paths. Locations sharing a root are walked once and
    each file goes to the first whose `match` accepts it."""

    key: str
    label: str
    why: str
    roots: tuple[str | Path, ...] | Callable[[], list[Path]]
    match: Matcher
    min_age_hours: float = 24
    selected_by_default: bool = True
    blocked_by_processes: frozenset[str] = frozenset()
    app_name: str = ""
    protect_unmatched: bool = False  # list files nobody claims as "kept" (TEMP)
    prune_dirs: frozenset[str] = frozenset()  # lower-case folder names never entered


def _ext(name: str) -> str:
    return os.path.splitext(name)[1]


def _any_file(name: str, depth: int) -> bool:
    return True


def _is_temp(name: str, depth: int) -> bool:
    ext = _ext(name)
    if ext in INSTALLER_EXT:
        return False  # "~setup.exe" belongs to installer leftovers (7 days, unselected)
    if ext in TEMP_EXT or name.startswith("~$"):  # "~$x.docx" is an Office lock file
        return True
    return name.startswith("~") and ext not in PERSONAL_EXT


def _is_log(name: str, depth: int) -> bool:
    return _ext(name) in LOG_EXT


def _is_dump(name: str, depth: int) -> bool:
    return _ext(name) in DUMP_EXT


def _is_installer_leftover(name: str, depth: int) -> bool:
    # Top-level TEMP executables may be something the user saved there on purpose.
    return depth >= 1 and _ext(name) in INSTALLER_EXT


def _is_icon_cache(name: str, depth: int) -> bool:
    return depth == 0 and (fnmatch.fnmatchcase(name, "thumbcache_*.db")
                           or fnmatch.fnmatchcase(name, "iconcache_*.db"))


def _temp_roots() -> list[Path]:
    """TEMP/TMP, canonical. A TEMP pointing somewhere odd (the profile, a
    drive root, Documents) is ignored: the folder must be named Temp/Tmp, or be a
    numbered folder directly inside one (Temp\\2 for remote sessions)."""
    out = []
    for var in ("TEMP", "TMP"):
        val = os.environ.get(var)
        if not val:
            continue
        p = canonical(val)
        name, parent = p.name.lower(), p.parent.name.lower()
        if name in ("temp", "tmp") or (name.isdigit() and parent in ("temp", "tmp")):
            out.append(p)
    return out


def temp_locations(roots: tuple[str | Path, ...] | Callable[[], list[Path]] = _temp_roots) -> list[Location]:
    """The four TEMP categories; any file in TEMP they don't claim is kept."""
    return [
        Location("temp_files", "Temporary files",
                 "Scratch files programs create while working and often forget to remove. "
                 "Only files untouched for a day are included; nothing reads them afterwards.",
                 roots, _is_temp, protect_unmatched=True),
        Location("temp_logs", "Logs and traces",
                 "Diagnostic logs (.log, .etl) written by installers and apps. Useful only when "
                 "troubleshooting a recent problem.",
                 roots, _is_log, protect_unmatched=True),
        Location("temp_dumps", "Crash dumps",
                 "Memory snapshots saved when a program crashed. Only a developer investigating "
                 "that crash would need them.",
                 roots, _is_dump, protect_unmatched=True),
        Location("temp_installers", "Installer leftovers",
                 "Setup files unpacked into TEMP subfolders during installs. Older than a week; "
                 "a repair of that program may ask for its installer again.",
                 roots, _is_installer_leftover, min_age_hours=24 * 7, selected_by_default=False,
                 protect_unmatched=True),
    ]


_LA = "%LOCALAPPDATA%"


def _browser(key: str, label: str, base: str, exe: str, app: str) -> Location:
    profile = f"{_LA}\\{base}\\User Data\\*"
    return Location(
        key, label,
        f"Copies of web pages, scripts and images {app} keeps to load sites faster. They are "
        f"re-downloaded as needed (sites load a bit slower at first). Cookies, history, saved "
        f"passwords and settings are not touched.",
        (f"{profile}\\Cache\\Cache_Data", f"{profile}\\Code Cache", f"{profile}\\GPUCache"),
        _any_file, min_age_hours=0, selected_by_default=False,
        blocked_by_processes=frozenset({exe}), app_name=app)


def _is_mac() -> bool:
    return sys.platform == "darwin"


# ------------------------------------------------------------- macOS locations

CRASH_REPORT_EXT = {".ips", ".crash", ".diag", ".spin", ".hang"}


def _is_crash_report(name: str, depth: int) -> bool:
    return _ext(name) in CRASH_REPORT_EXT


def _in_mac_tmpdir(p: Path) -> bool:
    """True for the per-user temporary folder /private/var/folders/xx/yyyy/T
    or anything inside it (checked on the canonical path's parts)."""
    parts = Path(p).parts
    for i in range(len(parts) - 3):
        if parts[i] == "var" and i + 4 < len(parts) and parts[i + 1] == "folders" and parts[i + 4] == "T":
            return True
    return False


def _mac_temp_roots() -> list[Path]:
    """$TMPDIR, canonical, and only when it really is the per-user
    /var/folders/../T folder (a TMPDIR pointing at home or / is ignored)."""
    val = os.environ.get("TMPDIR")
    if not val:
        return []
    p = canonical(val)
    parts = p.parts
    if len(parts) >= 5 and parts[-1] == "T" and parts[-4] == "folders" and parts[-5] == "var":
        return [p]
    return []


def _mac_browser(key: str, label: str, cache: str, support: str, procs: frozenset[str], app: str) -> Location:
    """Chromium browsers on macOS keep the HTTP cache under ~/Library/Caches/<base>/<profile>/Cache
    and the compiled-script / GPU caches in the profile under Application Support."""
    return Location(
        key, label,
        f"Copies of web pages, scripts and images {app} keeps to load sites faster. They are "
        f"re-downloaded as needed (sites load a bit slower at first). Cookies, history, saved "
        f"passwords and settings are not touched.",
        (f"~/Library/Caches/{cache}/*/Cache/Cache_Data", f"~/Library/Caches/{cache}/*/Code Cache",
         f"~/Library/Application Support/{support}/*/Code Cache",
         f"~/Library/Application Support/{support}/*/GPUCache"),
        _any_file, min_age_hours=0, selected_by_default=False, blocked_by_processes=procs, app_name=app)


def mac_locations() -> list[Location]:
    """macOS user-level locations. Each root is a named folder below the home
    folder (or $TMPDIR); nothing broader is ever a root."""
    return [
        *temp_locations(_mac_temp_roots),
        Location("mac_crash_reports", "Crash reports",
                 "Reports macOS saved when an app crashed or hung (.ips, .crash, .diag, .spin, .hang). "
                 "Only needed to report that problem to a developer.",
                 ("~/Library/Logs/DiagnosticReports",), _is_crash_report),
        Location("mac_logs", "App logs",
                 "Diagnostic logs (.log) apps write in your Library. Useful only when troubleshooting "
                 "a recent problem.",
                 ("~/Library/Logs",), _is_log, prune_dirs=frozenset({"diagnosticreports"})),
        _mac_browser("chrome_cache", "Google Chrome cache", "Google/Chrome", "Google/Chrome",
                     frozenset({"google chrome"}), "Google Chrome"),
        _mac_browser("brave_cache", "Brave cache", "BraveSoftware/Brave-Browser", "BraveSoftware/Brave-Browser",
                     frozenset({"brave browser"}), "Brave"),
        _mac_browser("edge_cache", "Microsoft Edge cache", "Microsoft Edge", "Microsoft Edge",
                     frozenset({"microsoft edge"}), "Microsoft Edge"),
        Location("firefox_cache", "Firefox cache",
                 "Copies of web pages, scripts and images Firefox keeps to load sites faster. They are "
                 "re-downloaded as needed. Cookies, history, saved passwords and settings are not touched.",
                 ("~/Library/Caches/Firefox/Profiles/*/cache2",), _any_file, min_age_hours=0,
                 selected_by_default=False, blocked_by_processes=frozenset({"firefox"}), app_name="Firefox"),
        Location("dev_cache", "Developer package caches",
                 "Downloaded packages kept by pip, npm, Yarn and Homebrew. They are re-downloaded the "
                 "next time they are needed.",
                 ("~/Library/Caches/pip", "~/.npm/_cacache", "~/Library/Caches/Yarn",
                  "~/Library/Caches/Homebrew/downloads"),
                 _any_file, selected_by_default=False),
    ]


# Below the home folder: never a root, never inside a root (lower-case parts).
_MAC_FORBIDDEN_SUBTREES = (
    ("library", "safari"), ("library", "containers", "com.apple.safari"),
    ("library", "caches", "com.apple.safari"), ("library", "mail"), ("library", "containers", "com.apple.mail"),
    ("library", "mobile documents"), ("library", "cloudstorage"), ("library", "photos"),
    ("library", "application support", "clouddocs"), ("library", "keychains"), ("library", "messages"),
    ("pictures",), ("documents",), ("desktop",), ("downloads",), ("movies",), ("music",),
)
# Too broad to be a root by themselves.
_MAC_BROAD = {(), ("library",), ("library", "caches"), ("library", "application support"),
              ("library", "containers"), ("library", "group containers")}


def _mac_root_refusal(root: Path) -> str | None:
    """macOS roots must be a specific folder below the home folder, or the
    per-user $TMPDIR. /System, /Library, /private/var (other than TMPDIR),
    ~/Library/Caches as a whole and the folders of Safari, Mail, Photos and
    iCloud are always refused."""
    home = canonical(Path.home())
    nr, nh = os.path.normcase(str(root)), os.path.normcase(str(home))
    try:
        if os.path.commonpath([nr, nh]) == nr:
            return "contains the home folder"
    except ValueError:
        pass
    if is_within(home, root):
        rel = tuple(p.lower() for p in Path(os.path.relpath(root, home)).parts)
        if rel in _MAC_BROAD:
            return "too broad a folder"
        for bad in _MAC_FORBIDDEN_SUBTREES:
            if rel[:len(bad)] == bad:
                return "a personal or system app folder"
        return None
    if _in_mac_tmpdir(root):
        return None
    return "outside your user folders"


def default_locations() -> list[Location]:
    """User-level locations only. Fresh list each call (env is read at scan time)."""
    if _is_mac():
        return mac_locations()
    return [
        *temp_locations(),
        Location("crashdumps", "App crash dumps",
                 "Crash snapshots apps saved in your profile. Only needed to report that crash.",
                 (f"{_LA}\\CrashDumps",), _is_dump),
        Location("wer", "Windows error reports",
                 "Copies of problem reports Windows has already sent or queued. Windows does "
                 "not need them to keep working.",
                 (f"{_LA}\\Microsoft\\Windows\\WER\\ReportArchive",
                  f"{_LA}\\Microsoft\\Windows\\WER\\ReportQueue"), _any_file),
        Location("thumbcache", "Thumbnail and icon cache",
                 "Pictures Explorer saves to show thumbnails quickly. Windows rebuilds them "
                 "(folders show thumbnails slowly for a while); files in use are skipped.",
                 (f"{_LA}\\Microsoft\\Windows\\Explorer",), _is_icon_cache,
                 min_age_hours=0, selected_by_default=False),
        Location("shader", "Graphics shader caches",
                 "Compiled graphics shaders. Games and apps rebuild them; the first launch "
                 "afterwards may stutter briefly.",
                 (f"{_LA}\\D3DSCache", f"{_LA}\\NVIDIA\\DXCache", f"{_LA}\\NVIDIA\\GLCache",
                  f"{_LA}\\AMD\\DxCache"), _any_file),
        _browser("edge_cache", "Microsoft Edge cache", "Microsoft\\Edge", "msedge.exe", "Microsoft Edge"),
        _browser("chrome_cache", "Google Chrome cache", "Google\\Chrome", "chrome.exe", "Google Chrome"),
        _browser("brave_cache", "Brave cache", "BraveSoftware\\Brave-Browser", "brave.exe", "Brave"),
        Location("inetcache", "Internet Explorer / legacy web cache",
                 "Web content cached by older Windows components. Re-downloaded when needed. "
                 "Outlook's attachment folder inside it is never touched.",
                 (f"{_LA}\\Microsoft\\Windows\\INetCache",), _any_file,
                 prune_dirs=frozenset({"content.outlook"})),
        Location("dev_cache", "Developer package caches",
                 "Downloaded packages kept by pip, npm and Yarn. They are re-downloaded the "
                 "next time a project needs them.",
                 (f"{_LA}\\pip\\Cache", f"{_LA}\\npm-cache", "%APPDATA%\\npm-cache", f"{_LA}\\Yarn\\Cache"),
                 _any_file, selected_by_default=False),
    ]


_LEADING_VAR = re.compile(r"%([^%\\/]+)%(.*)\Z", re.S)


def _walk_parts(base: Path, parts: tuple[str, ...]) -> list[Path]:
    """Folders below `base` matching `parts` (glob segments allowed), never
    entering a link, junction or other reparse point on the way."""
    current = [base]
    for part in parts:
        if part in ("..", "."):
            return []
        nxt = []
        for cur in current:
            if any(c in part for c in "*?["):
                try:
                    entries = list(os.scandir(cur))
                except OSError:
                    continue
                nxt += [Path(e.path) for e in entries
                        if fnmatch.fnmatch(e.name, part) and not _unusual_name(e.name)
                        and not is_link_like(e.path) and e.is_dir(follow_symlinks=False)]
            else:
                p = cur / part
                if not is_link_like(p):
                    nxt.append(p)
        current = nxt
    return current


def _expand_with_base(template: str) -> list[tuple[Path, Path]]:
    """(trusted base, folder) pairs for an env template. Only the env var's own
    value is canonicalized (it may legitimately be redirected); every component
    below it must be a real folder, so a junction such as %LOCALAPPDATA%\\NVIDIA
    pointing into Documents is refused rather than followed."""
    m = _LEADING_VAR.match(template)
    if template.startswith("~/"):
        # macOS templates: the home folder is the trusted base, like an env var's value.
        base = canonical(Path.home())
        rest = template[2:]
    elif m:
        val = os.environ.get(m.group(1))
        rest = os.path.expandvars(m.group(2))
        if not val or "%" in rest:
            return []  # variable not set: location doesn't exist on this machine
        base = canonical(val)
    else:
        s = os.path.expandvars(template)
        if "%" in s:
            return []
        full = Path(os.path.abspath(s))
        base = Path(full.anchor)
        rest = str(full)[len(full.anchor):]
    parts = tuple(p for p in Path(rest.strip("\\/")).parts if p not in ("\\", "/"))
    return [(base, p) for p in _walk_parts(base, parts)]


def _expand(template: str) -> list[Path]:
    """Expand env vars and glob segments (profile folders), never through links."""
    return [p for _, p in _expand_with_base(template)]


def _absolute_with_base(p: str | Path) -> tuple[Path, Path]:
    """A concrete root (tests, TEMP): the trusted base is the drive anchor, so
    no component of the path may be a link or junction."""
    full = Path(os.path.abspath(p))
    return Path(full.anchor), full


def _root_refusal(root: Path) -> str | None:
    """Folders a cleanup root may never be, whatever a template or env says."""
    nr = os.path.normcase(str(root))
    if root.parent == root:
        return "drive root"
    if _is_mac():
        return _mac_root_refusal(root)
    for name, guard in (("home", Path.home()), ("Windows", Path(os.environ.get("SystemRoot", r"C:\Windows")))):
        ng = os.path.normcase(str(canonical(guard)))
        try:
            if os.path.commonpath([nr, ng]) == nr:
                return f"contains the {name} folder"
            if name == "Windows" and os.path.commonpath([nr, ng]) == ng:
                return "inside the Windows folder"
        except ValueError:
            pass
    return None


def resolve_roots(loc: Location) -> list[Path]:
    """Existing, canonical, non-link root folders for `loc`; others are skipped."""
    raw: list[tuple[Path, Path]] = []
    if callable(loc.roots):
        raw = [_absolute_with_base(r) for r in loc.roots()]
    else:
        for r in loc.roots:
            raw += _expand_with_base(r) if isinstance(r, str) else [_absolute_with_base(r)]
    out, seen = [], set()
    for base, r in raw:
        r = Path(os.path.abspath(r))
        if is_link_like(r) or not r.is_dir():
            continue
        if os.path.normcase(str(r)) != os.path.normcase(str(base)):
            try:
                rel = os.path.relpath(r, base)
                resolve_inside(base, rel)  # no link/junction anywhere below the base
            except (UnsafePath, ValueError):
                continue
            if any(_unusual_name(part) for part in Path(rel).parts):
                continue
        c = canonical(r)
        key = os.path.normcase(str(c))
        if key in seen or _root_refusal(c):
            continue
        seen.add(key)
        out.append(c)
    return out


# --------------------------------------------------------------------- data

@dataclass
class Candidate:
    path: str
    size: int
    mtime_ns: int
    root: str


@dataclass
class ProtectedItem:
    path: str
    size: int
    reason: str


@dataclass
class Category:
    key: str
    label: str
    why: str
    roots: list[str]
    selected_by_default: bool
    blocked_reason: str | None
    min_age_hours: float
    files: list[Candidate] = field(default_factory=list)
    total_bytes: int = 0
    truncated: bool = False

    @property
    def root(self) -> str | None:
        return self.roots[0] if self.roots else None

    def summary(self) -> dict[str, Any]:
        largest = sorted(self.files, key=lambda c: c.size, reverse=True)[:LARGEST_IN_SUMMARY]
        return {"key": self.key, "label": self.label, "why": self.why, "roots": self.roots,
                "selected_by_default": self.selected_by_default, "blocked_reason": self.blocked_reason,
                "min_age_hours": self.min_age_hours, "count": len(self.files),
                "total_bytes": self.total_bytes, "truncated": self.truncated,
                "largest": [{"path": c.path, "size": c.size} for c in largest]}


@dataclass
class CleanupReport:
    report_id: str
    created: str
    categories: list[Category]
    protected: list[ProtectedItem]
    recycle_bin: tuple[int, int]
    disk_free: int
    stats: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Small summary for the UI; the full file list stays on the server."""
        by_reason: dict[str, int] = {}
        for p in self.protected:
            by_reason[p.reason] = by_reason.get(p.reason, 0) + 1
        largest_kept = sorted(self.protected, key=lambda p: p.size, reverse=True)[:LARGEST_IN_SUMMARY]
        return {"report_id": self.report_id, "created": self.created,
                "categories": [c.summary() for c in self.categories],
                "reclaimable_bytes": sum(c.total_bytes for c in self.categories),
                "protected": {"count": self.stats.get("protected_count", len(self.protected)),
                              "bytes": self.stats.get("protected_bytes", 0), "by_reason": by_reason,
                              "largest": [asdict(p) for p in largest_kept]},
                "recycle_bin": {"bytes": self.recycle_bin[0], "items": self.recycle_bin[1]},
                "disk_free": self.disk_free, "stats": self.stats}

    def full_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CleanupReport":
        cats = []
        for c in d["categories"]:
            c = dict(c)
            c["files"] = [Candidate(**f) for f in c["files"]]
            cats.append(Category(**c))
        return cls(report_id=d["report_id"], created=d["created"], categories=cats,
                   protected=[ProtectedItem(**p) for p in d["protected"]],
                   recycle_bin=tuple(d["recycle_bin"]), disk_free=d["disk_free"],
                   stats=d.get("stats", {}))


@dataclass
class CleanupResult:
    report_id: str
    mode: str
    removed_count: int = 0
    removed_bytes: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)
    disk_free_before: int = 0
    disk_free_after: int = 0
    recycle_bin_after: tuple[int, int] = (0, 0)
    journal: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["skipped"] = [{"path": p, "reason": r} for p, r in self.skipped]
        d["errors"] = [{"path": p, "reason": r} for p, r in self.errors]
        d["recycle_bin_after"] = {"bytes": self.recycle_bin_after[0], "items": self.recycle_bin_after[1]}
        return d


# --------------------------------------------------------------------- scanning

def _cleanup_dir() -> Path:
    d = state_dir() / "cleanup"
    d.mkdir(exist_ok=True)
    return d


def _processes() -> set[str] | None:
    try:
        return winshell.running_processes()
    except OSError:
        return None  # unknown: browser categories will be blocked, not guessed


def _blocked_reason(loc: Location, procs: set[str] | None) -> str | None:
    if not loc.blocked_by_processes:
        return None
    app = loc.app_name or ", ".join(sorted(loc.blocked_by_processes))
    if procs is None:
        return f"could not check whether {app} is running; close it and scan again"
    if loc.blocked_by_processes & procs:
        return f"{app} is running - close {app} first"
    return None


def _kept_reason(name: str) -> str:
    return KEPT_PERSONAL if _ext(name) in PERSONAL_EXT else KEPT_UNKNOWN


def scan_cleanup(locations: list[Location] | None = None, now: float | None = None,
                 *, max_files: int = MAX_FILES_PER_CATEGORY, save: bool = True) -> CleanupReport:
    """Build (and save) a cleanup report. Reads metadata only."""
    locations = default_locations() if locations is None else locations
    now = time.time() if now is None else now
    procs = _processes() if any(l.blocked_by_processes for l in locations) else set()
    stats = {"files_seen": 0, "too_new": 0, "links_skipped": 0, "permission_errors": 0,
             "placeholders_skipped": 0, "protected_count": 0, "protected_bytes": 0}
    state = os.path.normcase(str(canonical(state_dir())))

    categories: dict[str, Category] = {}
    groups: dict[str, tuple[Path, list[Location]]] = {}
    for loc in locations:
        roots = resolve_roots(loc)
        if not roots:
            continue
        cat = categories.get(loc.key)
        if cat is None:
            cat = categories[loc.key] = Category(loc.key, loc.label, loc.why, [], loc.selected_by_default,
                                                 _blocked_reason(loc, procs), loc.min_age_hours)
        for r in roots:
            cat.roots.append(str(r))
            groups.setdefault(os.path.normcase(str(r)), (r, []))[1].append(loc)

    protected: list[ProtectedItem] = []
    seen: set[str] = set()

    def on_error(e: OSError) -> None:
        stats["permission_errors"] += 1

    for root, locs in groups.values():
        protect = any(l.protect_unmatched for l in locs)
        prune = frozenset().union(*(l.prune_dirs for l in locs))
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=on_error):
            keep = []
            for d in dirnames:
                full = os.path.join(dirpath, d)
                if _unusual_name(d):
                    continue  # Win32 would open its trailing-space/dot twin instead
                if is_link_like(full):
                    stats["links_skipped"] += 1
                elif d.lower() in prune or os.path.normcase(full) == state:
                    continue
                else:
                    keep.append(d)
            dirnames[:] = keep
            rel_dir = os.path.relpath(dirpath, root)
            depth = 0 if rel_dir == "." else len(Path(rel_dir).parts)
            for name in filenames:
                full = os.path.join(dirpath, name)
                nkey = os.path.normcase(full)
                if nkey in seen:
                    continue
                seen.add(nkey)
                if _unusual_name(name):
                    # Never lstat or match it: Win32 calls would act on its twin.
                    stats["protected_count"] += 1
                    if len(protected) < MAX_PROTECTED:
                        protected.append(ProtectedItem(full, 0, KEPT_UNUSUAL))
                    continue
                try:
                    st = os.lstat(full)
                except OSError:
                    stats["permission_errors"] += 1
                    continue
                attrs = getattr(st, "st_file_attributes", 0)
                if stat.S_ISLNK(st.st_mode) or attrs & _REPARSE:
                    stats["links_skipped"] += 1
                    continue
                if not stat.S_ISREG(st.st_mode) or not is_within(root, Path(full)):
                    continue
                stats["files_seen"] += 1
                lname = name.lower()
                if attrs & _PLACEHOLDER:
                    stats["placeholders_skipped"] += 1
                    continue
                if lname == "desktop.ini":
                    continue
                if attrs & _SYSTEM:
                    owner = None
                else:
                    owner = next((l for l in locs if l.match(lname, depth)), None)
                if owner is None:
                    if protect:
                        reason = KEPT_SYSTEM if attrs & _SYSTEM else _kept_reason(lname)
                        stats["protected_count"] += 1
                        stats["protected_bytes"] += st.st_size
                        if len(protected) < MAX_PROTECTED:
                            protected.append(ProtectedItem(full, st.st_size, reason))
                    continue
                if now - st.st_mtime < owner.min_age_hours * 3600:
                    stats["too_new"] += 1
                    continue
                cat = categories[owner.key]
                if len(cat.files) >= max_files:
                    cat.truncated = True
                    continue
                cat.files.append(Candidate(full, st.st_size, st.st_mtime_ns, str(root)))
                cat.total_bytes += st.st_size

    report = CleanupReport(
        report_id=f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}",
        created=now_iso(), categories=list(categories.values()), protected=protected,
        recycle_bin=_bin_info(), disk_free=winshell.disk_free(Path.home()), stats=stats)
    if save:
        save_report(report)
    return report


def _bin_info() -> tuple[int, int]:
    try:
        return winshell.recycle_bin_info()
    except OSError:
        return (0, 0)


def _report_path(report_id: str) -> Path:
    if not isinstance(report_id, str) or not _REPORT_ID.fullmatch(report_id):
        raise FileNotFoundError(f"no cleanup report named {report_id!r}")
    return _cleanup_dir() / f"{report_id}.json"


def save_report(report: CleanupReport) -> Path:
    """Atomic write: a crash leaves the old report or the new one, never half."""
    path = _report_path(report.report_id)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(report.full_dict(), f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return path


def load_report(report_id: str) -> CleanupReport:
    path = _report_path(report_id)
    if not path.exists():
        raise FileNotFoundError(f"no cleanup report named {report_id!r}")
    with open(path, encoding="utf-8") as f:
        return CleanupReport.from_dict(json.load(f))


# --------------------------------------------------------------------- execution

def _readable(e: OSError) -> str:
    if getattr(e, "winerror", None) == 32:
        return "in use by another program"
    if isinstance(e, FileNotFoundError):
        return "already gone"
    if isinstance(e, PermissionError):
        return "permission denied"
    return e.strerror or type(e).__name__


def _revalidate(c: Candidate, loc: Location, allowed_roots: set[str]) -> str | None:
    """Why this file must not be removed now, or None if it may."""
    root = Path(c.root)
    if os.path.normcase(str(root)) not in allowed_roots:
        return "its folder is no longer a cleanup location"
    path = Path(c.path)
    if any(_unusual_name(part) for part in Path(c.path).parts[1:]):
        return "unusual name"
    if winshell.is_alias_path(c.path):
        return "alias name (stream or short name)"
    if not is_within(root, path):
        return "outside its cleanup folder"
    try:
        rel = os.path.relpath(path, root)
        resolve_inside(root, rel)  # no link or junction anywhere below the root
    except (UnsafePath, ValueError):
        return "reached through a link or junction"
    try:
        st = os.lstat(path)
    except OSError as e:
        return _readable(e)
    attrs = getattr(st, "st_file_attributes", 0)
    if not stat.S_ISREG(st.st_mode) or attrs & (_REPARSE | _SYSTEM | _PLACEHOLDER):
        return "no longer a regular file"
    if st.st_size != c.size or st.st_mtime_ns != c.mtime_ns:
        return "changed since scan"
    if not loc.match(path.name.lower(), len(Path(rel).parts) - 1):
        return "not a file type this category may remove"
    if winshell.is_in_use(path):
        return "in use by another program"
    return None


def _journal_path(report_id: str) -> Path:
    _report_path(report_id)  # validates the id
    d = _cleanup_dir()
    n = 1
    while (d / f"{report_id}-{n}.jsonl").exists():
        n += 1
    return d / f"{report_id}-{n}.jsonl"


def _str_list(items: Iterable[str], what: str, *, allow_pathlike: bool = False) -> list[str]:
    """A single string would be iterated per character (an unticked file would
    silently not be excluded), so only real collections of strings pass."""
    bad = TypeError(f"{what} must be a list of strings, not {type(items).__name__}")
    if items is None or isinstance(items, (str, bytes, bytearray, memoryview)):
        raise bad
    try:
        out = list(items)
    except TypeError:
        raise bad from None
    for it in out:
        if isinstance(it, str):
            continue
        if allow_pathlike and isinstance(it, os.PathLike) and isinstance(os.fspath(it), str):
            continue
        raise TypeError(f"{what} must contain only strings, got {type(it).__name__}")
    return [os.fspath(it) for it in out]


def clean(report_id: str, category_keys: Iterable[str], mode: str = "recycle",
          confirm_permanent: bool = False, exclude_paths: Iterable[str] = (),
          *, locations: list[Location] | None = None) -> CleanupResult:
    """Remove the files of the chosen categories from a saved report.
    `locations` must match the ones the report was scanned with (tests)."""
    category_keys = _str_list(category_keys, "category_keys")
    exclude_paths = _str_list(exclude_paths, "exclude_paths", allow_pathlike=True)
    if mode not in ("recycle", "delete"):
        raise ValueError(f"unknown cleanup mode {mode!r}")
    if mode == "delete" and confirm_permanent is not True:
        raise ValueError("permanent deletion needs explicit confirmation")
    report = load_report(report_id)
    locs = {l.key: l for l in (default_locations() if locations is None else locations)}
    cats = {c.key: c for c in report.categories}
    excluded = {os.path.normcase(os.path.abspath(p)) for p in exclude_paths}
    keys = list(dict.fromkeys(category_keys))
    procs = _processes() if any(locs[k].blocked_by_processes for k in keys if k in locs) else set()

    result = CleanupResult(report_id, mode)
    first_root = next((cats[k].root for k in keys if k in cats and cats[k].root), None)
    free_probe = Path(first_root) if first_root else Path.home()
    result.disk_free_before = winshell.disk_free(free_probe)
    jpath = _journal_path(report_id)
    result.journal = str(jpath)
    with Journal(jpath, new=True) as j:
        j.write({"t": "begin", "report_id": report_id, "mode": mode, "categories": keys})
        for key in keys:
            cat, loc = cats.get(key), locs.get(key)
            if cat is None or loc is None:
                result.skipped.append((key, "unknown category"))
                continue
            blocked = _blocked_reason(loc, procs)
            if blocked:
                result.skipped.append((cat.label, f"{blocked} ({len(cat.files)} files not touched)"))
                j.write({"t": "skip_category", "category": key, "reason": blocked})
                continue
            allowed = {os.path.normcase(str(r)) for r in resolve_roots(loc)}
            ready: list[Candidate] = []
            for c in cat.files:
                if os.path.normcase(os.path.abspath(c.path)) in excluded:
                    continue  # the user unticked it
                why = _revalidate(c, loc, allowed)
                if why:
                    result.skipped.append((c.path, why))
                else:
                    ready.append(c)
            if mode == "delete":
                for c in ready:
                    _delete_one(c, key, j, result)
            else:
                _recycle_many(ready, key, j, result)
        result.disk_free_after = winshell.disk_free(free_probe)
        result.recycle_bin_after = _bin_info()
        j.write({"t": "end", "removed": result.removed_count, "bytes": result.removed_bytes,
                 "skipped": len(result.skipped), "errors": len(result.errors)})
    return result


def _removed(c: Candidate, key: str, mode: str, j: Journal, result: CleanupResult) -> None:
    j.write({"t": "removed", "path": c.path, "size": c.size, "mode": mode, "category": key})
    result.removed_count += 1
    result.removed_bytes += c.size


def _delete_one(c: Candidate, key: str, j: Journal, result: CleanupResult) -> None:
    try:
        winshell.delete_permanently(Path(c.path))
    except UnsafePath as e:
        result.skipped.append((c.path, str(e)))
        return
    except OSError as e:
        reason = _readable(e)
        bucket = result.skipped if reason in ("in use by another program", "permission denied",
                                              "already gone") else result.errors
        bucket.append((c.path, reason))
        return
    if os.path.lexists(c.path):
        result.errors.append((c.path, "still present after delete"))
    else:
        _removed(c, key, "delete", j, result)


def _recycle_many(cands: list[Candidate], key: str, j: Journal, result: CleanupResult) -> None:
    for i in range(0, len(cands), winshell.BATCH):
        chunk = cands[i:i + winshell.BATCH]
        try:
            res = winshell.recycle([Path(c.path) for c in chunk])
        except (OSError, NotImplementedError) as e:
            result.errors += [(c.path, str(e)) for c in chunk]
            continue
        failed = []
        for c in chunk:
            if res.get(str(Path(c.path)), "no result") is None:
                _removed(c, key, "recycle", j, result)
            else:
                failed.append(c)
        # One stubborn file can abort the rest of a batch: retry the others singly.
        for c in failed:
            k = str(Path(c.path))
            err = res.get(k) if len(chunk) == 1 else winshell.recycle([Path(c.path)]).get(k)
            if err is None:
                _removed(c, key, "recycle", j, result)
            elif winshell.is_in_use(Path(c.path)):
                result.skipped.append((c.path, "in use by another program"))
            else:
                result.errors.append((c.path, err or "could not recycle"))


def empty_recycle_bin_confirmed(confirm: bool) -> dict[str, int]:
    """Empty every Recycle Bin (macOS: Empty Trash). Irreversible, so it is its
    own action with its own explicit confirmation and journal entry."""
    if confirm is not True:
        what = "the Trash" if _is_mac() else "the Recycle Bin"
        raise ValueError(f"emptying {what} is permanent and needs explicit confirmation")
    before = _bin_info()
    path = _cleanup_dir() / "recycle-bin.jsonl"
    path.touch(exist_ok=True)
    with Journal(path) as j:
        j.write({"t": "empty_recycle_bin_intent", "bytes": before[0], "items": before[1]})
        try:
            winshell.empty_recycle_bin()
        except (OSError, NotImplementedError) as e:
            j.write({"t": "empty_recycle_bin_failed", "error": str(e)})
            raise
        after = _bin_info()
        j.write({"t": "emptied_recycle_bin", "bytes": before[0], "items": before[1],
                 "bytes_after": after[0], "items_after": after[1]})
    return {"bytes_before": before[0], "items_before": before[1],
            "bytes_after": after[0], "items_after": after[1]}
