"""Read text from scanned documents with the OCR engine built into Windows
(Windows.Media.Ocr). No extra download, nothing leaves the computer.

The WinRT API is reached through Windows PowerShell 5.1 (ocr.ps1), because the
stdlib has no WinRT bridge and we do not add third-party dependencies. Paths
are passed as separate argv entries to `-File`, which never evaluates them, and
the shell is never involved, so a hostile filename cannot inject commands."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from importlib import resources
from pathlib import Path

MAX_TEXT = 4000  # same order as the extractor's excerpt: enough to classify and name
MAX_CMDLINE = 24_000  # Windows caps a command line at 32,767 chars; keep headroom
# Formats the WinRT BitmapDecoder can open; anything else (e.g. JPEG 2000) is skipped.
DECODABLE = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff", ".jxr"}


def _powershell() -> str | None:
    """Windows PowerShell 5.1 specifically: pwsh 7 cannot load WinRT types this way."""
    root = os.environ.get("SystemRoot", r"C:\Windows")
    exe = Path(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    return str(exe) if exe.is_file() else shutil.which("powershell.exe")


def _run_capture(paths: list[str], timeout: float) -> tuple[int | None, bytes, bool]:
    """Run ocr.ps1 on `paths`. Returns (returncode, stdout, timed_out).
    returncode is None when PowerShell could not be started or was killed; on a
    timeout, stdout holds whatever the script printed before it was killed."""
    exe = _powershell()
    if exe is None:
        return None, b"", False
    with resources.as_file(resources.files("lazyfold") / "ocr.ps1") as script:
        cmd = [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
               "-File", str(script), *paths]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout, shell=False,
                                  stdin=subprocess.DEVNULL,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired as e:
            out = e.stdout if isinstance(e.stdout, bytes) else b""
            return None, out, True
        except (OSError, subprocess.SubprocessError):
            return None, b"", False
    return proc.returncode, proc.stdout or b"", False


def _run(paths: list[str], timeout: float) -> subprocess.CompletedProcess | None:
    """Compatibility wrapper: CompletedProcess, or None on failure/timeout."""
    code, out, _ = _run_capture(paths, timeout)
    if code is None:
        return None
    return subprocess.CompletedProcess(paths, code, stdout=out, stderr=b"")


_available: bool | None = None
_probe_timeouts = 0
MAX_PROBE_TIMEOUTS = 2  # a cold first PowerShell/WinRT start can be slow: retry once


def ocr_available() -> bool:
    """True when Windows has an OCR engine for one of the user's languages.
    The answer is cached, except that a False caused by the first probe timing
    out is not: the next call probes once more before giving up for good."""
    global _available, _probe_timeouts
    if _available is not None:
        return _available
    if sys.platform == "darwin":
        from . import macos
        _available = macos.ocr_available()  # Apple Vision, through pyobjc
        return _available
    if sys.platform != "win32":
        _available = False
        return False
    code, _, timed_out = _run_capture([], timeout=30)
    if timed_out:
        _probe_timeouts += 1
        if _probe_timeouts < MAX_PROBE_TIMEOUTS:
            return False  # not cached: allow one retry
    _available = code == 0
    return _available


def _cache_clear() -> None:
    global _available, _probe_timeouts
    _available, _probe_timeouts = None, 0


ocr_available.cache_clear = _cache_clear  # type: ignore[attr-defined]  # lru_cache-compatible


MAX_BATCH = 16  # images per PowerShell run: a timeout then loses at most one small batch


def _batches(paths: list[str]) -> list[list[str]]:
    out: list[list[str]] = [[]]
    length = 0
    for p in paths:
        cost = len(p) + 3  # quotes and a space
        if out[-1] and (length + cost > MAX_CMDLINE or len(out[-1]) >= MAX_BATCH):
            out.append([])
            length = 0
        out[-1].append(p)
        length += cost
    return [b for b in out if b]


def _parse(stdout: bytes, absolute: dict[str, str], out: dict[str, str]) -> None:
    """Read the script's JSON lines into `out`. Only complete, parseable lines
    count, so a line cut off by a kill is simply ignored."""
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        line = line.strip().lstrip("\ufeff")
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        key = absolute.get(obj.get("path") or "")
        text = obj.get("text")
        if key is None or obj.get("error") or not isinstance(text, str):
            continue
        out[key] = text.strip()[:MAX_TEXT]


def ocr_images(paths: list[Path], timeout: float = 30) -> dict[str, str]:
    """OCR images in a few PowerShell start-ups (about 0.5 s per image).
    Returns str(input path) -> text for images that were read; failures are
    simply missing. Images are sent in batches of at most MAX_BATCH; a batch
    that times out keeps whatever it finished, and the other batches still
    count. Returns {} when OCR is unavailable. On macOS each image is read
    in-process by Apple Vision instead."""
    if sys.platform == "darwin" and paths:
        return _ocr_images_mac(paths)
    if sys.platform != "win32" or not paths:
        return {}
    # Absolute paths: WinRT needs them, and an argument starting with "-" could
    # otherwise be taken for a PowerShell parameter.
    absolute = {os.path.abspath(p): str(p) for p in paths}
    out: dict[str, str] = {}
    for batch in _batches(list(absolute)):
        code, stdout, timed_out = _run_capture(batch, max(timeout, 10 + 2 * len(batch)))
        if timed_out:
            _parse(stdout, absolute, out)  # keep the images finished before the kill
            continue
        if code is None:
            return out  # PowerShell cannot be started: later batches would fail too
        if code != 0:
            if code == 2:
                return out  # no OCR engine for the user's languages
            continue
        _parse(stdout, absolute, out)
    return out


def _ocr_images_mac(paths: list[Path]) -> dict[str, str]:
    from . import macos
    if not macos.ocr_available():
        return {}
    out: dict[str, str] = {}
    for p in paths:
        text = macos.ocr_image(Path(os.path.abspath(p)))
        if text and text.strip():
            out[str(p)] = text.strip()[:MAX_TEXT]
    return out


MAX_IMAGE_PIXELS = 40_000_000  # a 600 dpi A4 page is ~35 MP; beyond that is not a scan
MAX_FORM_DEPTH = 3


def _image_refs(page) -> list[tuple[object, str | list[str], int, int]]:
    """(identity, key for page.images, width, height) of the image XObjects on a
    page, including images inside form XObjects, read from the dictionaries
    alone: no stream is decoded here."""
    from pypdf.generic import DictionaryObject, IndirectObject, StreamObject  # noqa: PLC0415
    found: list[tuple[object, str | list[str], int, int]] = []
    visited: set[object] = set()

    def walk(obj, ancest: list[str], depth: int) -> None:
        try:
            res = obj.get("/Resources")
            res = res.get_object() if res is not None else None
            xobjs = res.get("/XObject") if isinstance(res, DictionaryObject) else None
            xobjs = xobjs.get_object() if xobjs is not None else None
        except Exception:
            return
        if not isinstance(xobjs, DictionaryObject):
            return
        for name in list(xobjs.keys()):
            try:
                raw = xobjs.raw_get(name)
                ident = ((raw.idnum, raw.generation) if isinstance(raw, IndirectObject)
                         else ("direct", id(raw)))
                if ident in visited:
                    continue  # the same object under another name: decode it once
                visited.add(ident)
                entry = raw.get_object()
                if not isinstance(entry, StreamObject):
                    continue
                key = [*ancest, name] if ancest else name
                if entry.get("/Subtype") == "/Image":
                    found.append((ident, key, int(entry.get("/Width", 0) or 0),
                                  int(entry.get("/Height", 0) or 0)))
                elif entry.get("/Subtype") == "/Form" and depth < MAX_FORM_DEPTH:
                    walk(entry, [*ancest, name], depth + 1)
            except Exception:
                continue

    walk(page, [], 0)
    return found


def pdf_page_images(path: Path, max_pages: int = 2, max_bytes: int = 20_000_000,
                    max_images: int = 8) -> list[tuple[str, bytes]]:
    """Embedded images of the first pages of a PDF, as (extension, bytes).
    A scanned PDF is just one picture per page, so this is what we OCR.

    Bounded so a hostile PDF cannot make us decode gigabytes: at most
    `max_images` images, each XObject decoded once even when referenced many
    times, and images over MAX_IMAGE_PIXELS (by their declared /Width and
    /Height, checked before any data is touched) are skipped."""
    try:
        from pypdf import PdfReader  # optional dependency
    except ImportError:
        return []
    out: list[tuple[str, bytes]] = []
    total = 0
    seen: set[object] = set()
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            return []
        for page in reader.pages[:max_pages]:
            for ident, key, w, h in _image_refs(page):
                if len(out) >= max_images:
                    return out
                if ident in seen:
                    continue
                seen.add(ident)
                if w <= 0 or h <= 0 or w * h > MAX_IMAGE_PIXELS:
                    continue
                try:
                    img = page.images[key]
                    ext = os.path.splitext(img.name)[1].lower()
                    data = img.data
                except Exception:  # unsupported filter, missing Pillow for a filter, ...
                    continue
                if ext not in DECODABLE or not data:
                    continue
                if total + len(data) > max_bytes:
                    return out
                total += len(data)
                out.append((ext, data))
    except Exception:  # malformed PDF
        return out
    return out


def ocr_pdf(path: Path, workdir: Path) -> str:
    """OCR a scanned PDF. Page images are written into `workdir`, a private temp
    dir the caller creates and later removes; we delete only files we created."""
    images = pdf_page_images(path)
    if not images:
        return ""
    created: list[Path] = []
    try:
        for i, (ext, data) in enumerate(images):
            target = workdir / f"lazyfold-ocr-{i:03d}{ext}"
            with open(target, "xb") as fh:  # never overwrite something that is already there
                created.append(target)
                fh.write(data)
        texts = ocr_images(created)
        joined = "\n".join(texts[str(p)] for p in created if texts.get(str(p)))
        return joined.strip()[:MAX_TEXT]
    except OSError:
        return ""
    finally:
        for p in created:
            try:
                p.unlink()
            except OSError:
                pass
