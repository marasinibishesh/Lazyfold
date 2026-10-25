"""Bounded, read-only text and metadata extraction.

Only a small excerpt is ever read (MAX_CHARS), so neither the classifier nor a
local model sees whole documents. Parsers run on untrusted input, so every
extractor is wrapped: a failure yields an empty excerpt plus a warning, never a crash.
"""

from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

MAX_CHARS = 2000
MAX_TEXT_BYTES = 64 * 1024
MAX_PDF_BYTES = 50 * 1024 * 1024
MAX_PDF_PAGES = 2
MAX_XML_BYTES = 2 * 1024 * 1024

TEXT_EXTS = {".txt", ".md", ".csv", ".tsv", ".json", ".log", ".xml", ".html", ".htm",
             ".ini", ".cfg", ".yaml", ".yml", ".py", ".js", ".ts", ".sql", ".srt", ".ics",
             ".vcf", ".eml", ".rtf", ".tex", ".bib"}


def _clean(text: str) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()[:MAX_CHARS]


def _decode(raw: bytes) -> str:
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    return raw.decode("utf-8", errors="replace")


def extract_plain(path: Path) -> tuple[str, dict[str, Any]]:
    with open(path, "rb") as f:
        raw = f.read(MAX_TEXT_BYTES)
    text = _decode(raw)
    meta: dict[str, Any] = {}
    if path.suffix.lower() in (".html", ".htm") or text.lstrip()[:15].lower().startswith(("<!doctype html", "<html")):
        m = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
        if m:
            meta["title"] = _clean(m.group(1))[:200]
        text = re.sub(r"<(script|style)\b.*?</\1>", " ", text, flags=re.I | re.S)
        text = re.sub(r"<[^>]+>", " ", text)
    return _clean(text), meta


def extract_pdf(path: Path) -> tuple[str, dict[str, Any]]:
    meta: dict[str, Any] = {}
    if path.stat().st_size > MAX_PDF_BYTES:
        return "", {"note": "pdf too large for text extraction"}
    try:
        from pypdf import PdfReader  # optional dependency
    except ImportError:
        return "", {"note": "pypdf not installed; pdf text not read"}
    reader = PdfReader(str(path), strict=False)
    if reader.is_encrypted:
        return "", {"encrypted": True}
    meta["pages"] = len(reader.pages)
    info = reader.metadata or {}
    for key, out in (("/Title", "title"), ("/Author", "author"), ("/Subject", "subject"),
                     ("/Producer", "producer"), ("/CreationDate", "created")):
        val = info.get(key)
        if val:
            meta[out] = str(val)[:200]
    parts = []
    for page in reader.pages[:MAX_PDF_PAGES]:
        parts.append(page.extract_text() or "")
        if sum(map(len, parts)) >= MAX_CHARS:
            break
    return _clean("\n".join(parts)), meta


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_DC = "{http://purl.org/dc/elements/1.1/}"


def _read_member(zf: zipfile.ZipFile, name: str) -> bytes | None:
    try:
        info = zf.getinfo(name)
    except KeyError:
        return None
    if info.file_size > MAX_XML_BYTES:  # zip-bomb guard: check declared size first
        return None
    with zf.open(info) as f:
        return f.read(MAX_XML_BYTES)


def _parse_xml(raw: bytes) -> ElementTree.Element:
    # Office XML never needs a DTD; refuse any to rule out entity expansion attacks.
    if b"<!DOCTYPE" in raw[:2048] or b"<!ENTITY" in raw[:4096]:
        raise ValueError("DTD in office XML refused")
    return ElementTree.fromstring(raw)


def _core_meta(zf: zipfile.ZipFile) -> dict[str, Any]:
    """Title, author and subject from an Office file's docProps/core.xml."""
    meta: dict[str, Any] = {}
    core = _read_member(zf, "docProps/core.xml")
    if core:
        root = _parse_xml(core)
        for tag, out in (("title", "title"), ("creator", "author"), ("subject", "subject")):
            el = root.find(f"{_DC}{tag}")
            if el is not None and el.text:
                meta[out] = el.text.strip()[:200]
    return meta


_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
MAX_SLIDES = 20


def extract_pptx(path: Path) -> tuple[str, dict[str, Any]]:
    """Slide text in slide order (the first MAX_SLIDES slides)."""
    with zipfile.ZipFile(path) as zf:
        meta = _core_meta(zf)
        slides = sorted((n for n in zf.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                        key=lambda n: int(re.search(r"\d+", n.rsplit("/", 1)[1]).group()))
        meta["slides"] = len(slides)
        lines = []
        for name in slides[:MAX_SLIDES]:
            raw = _read_member(zf, name)
            if not raw:
                continue
            for p in _parse_xml(raw).iter(f"{_A}p"):
                line = "".join(t.text or "" for t in p.iter(f"{_A}t"))
                if line.strip():
                    lines.append(line)
            if sum(map(len, lines)) >= MAX_CHARS:
                break
    return _clean("\n".join(lines)), meta


def extract_xlsx(path: Path) -> tuple[str, dict[str, Any]]:
    """Sheet names and the workbook's text cells (shared strings)."""
    with zipfile.ZipFile(path) as zf:
        meta = _core_meta(zf)
        parts = []
        wb = _read_member(zf, "xl/workbook.xml")
        if wb:
            names = [s.get("name") for s in _parse_xml(wb).iter(f"{_S}sheet") if s.get("name")]
            if names:
                meta["sheets"] = names[:20]
                parts.append("Sheets: " + ", ".join(names[:20]))
        shared = _read_member(zf, "xl/sharedStrings.xml")
        if shared:
            for si in _parse_xml(shared).iter(f"{_S}si"):
                cell = "".join(t.text or "" for t in si.iter(f"{_S}t")).strip()
                if cell:
                    parts.append(cell)
                if sum(map(len, parts)) >= MAX_CHARS:
                    break
    return _clean("\n".join(parts)), meta


def extract_docx(path: Path) -> tuple[str, dict[str, Any]]:
    with zipfile.ZipFile(path) as zf:
        meta = _core_meta(zf)
        body = _read_member(zf, "word/document.xml")
        if not body:
            return "", meta
        root = _parse_xml(body)
        paras = []
        for p in root.iter(f"{_W}p"):
            line = "".join(t.text or "" for t in p.iter(f"{_W}t"))
            if line.strip():
                paras.append(line)
            if sum(map(len, paras)) >= MAX_CHARS:
                break
    return _clean("\n".join(paras)), meta


def extract_image(path: Path) -> tuple[str, dict[str, Any]]:
    try:
        from PIL import Image  # optional dependency
    except ImportError:
        return "", {}
    Image.MAX_IMAGE_PIXELS = 100_000_000  # decompression-bomb guard
    meta: dict[str, Any] = {}
    with Image.open(path) as im:  # lazy: reads header, not pixel data
        meta["width"], meta["height"] = im.size
        exif = im.getexif()
        if exif:
            sub_ifd = exif.get_ifd(0x8769)
            # Capture date first (DateTimeOriginal, then DateTimeDigitized). Tag 306 is the
            # last time an editor saved the file, so it is kept separately and never used
            # as "when the photo was taken".
            taken = sub_ifd.get(36867) or sub_ifd.get(36868)
            if taken:
                meta["taken"] = str(taken)[:19]
            if exif.get(306):
                meta["modified"] = str(exif.get(306))[:19]
            model = exif.get(272)
            if model:
                meta["camera"] = str(model)[:60]
    return "", meta


PREVIEW_PX = 768
MAX_PREVIEW_BYTES = 40 * 1024 * 1024
PREVIEW_KINDS = {"png", "jpeg", "gif", "bmp", "webp"}


def image_preview(path: Path) -> str:
    """A small JPEG of an image (base64) for the local vision model to look
    at, or "" if it can't be made. Runs in the isolated worker like any parser."""
    try:
        from PIL import Image  # optional dependency
    except ImportError:
        return ""
    import base64
    import io
    Image.MAX_IMAGE_PIXELS = 100_000_000  # decompression-bomb guard
    if path.stat().st_size > MAX_PREVIEW_BYTES:
        return ""
    with Image.open(path) as im:
        im.draft("RGB", (PREVIEW_PX, PREVIEW_PX))  # JPEG: decode at reduced size
        im.seek(0)
        small = im.convert("RGB")
        small.thumbnail((PREVIEW_PX, PREVIEW_PX))
        buf = io.BytesIO()
        small.save(buf, "JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def preview_or_empty(path: Path) -> tuple[str, dict[str, Any], list[str]]:
    """image_preview in the (text, meta, warnings) shape the worker returns."""
    try:
        return image_preview(path), {}, []
    except Exception as e:  # untrusted input
        return "", {}, [f"could not make a preview ({type(e).__name__})"]


PARSED_KINDS = {"pdf", "docx", "pptx", "xlsx", "png", "jpeg", "gif", "bmp", "webp"}
PARSE_TIMEOUT = 8.0


CLI_EXE_NAME = "LazyfoldCLI"  # the console program next to the window program in a frozen build
IN_PROCESS_NOTE = "read without the isolated worker (it could not start)"


def worker_executable() -> str | None:
    """The program that hosts the parser worker in a frozen macOS app. The
    window program (Contents/MacOS/Lazyfold) is a GUI app that macOS may not
    start as a background worker; the console program next to it is. None
    means "use sys.executable" (source runs, Windows)."""
    if sys.platform != "darwin" or not getattr(sys, "frozen", False):
        return None
    cli = Path(sys.executable).with_name(CLI_EXE_NAME)
    return str(cli) if cli.is_file() else None


class IsolatedExtractor:
    """Runs third-party parsers (pypdf, Pillow, XML) in a worker process with a
    timeout, so a hostile file can hang or exhaust only a process we can kill.
    Plain text is read in-process (bounded read, no parser). If the worker
    cannot start at all, parsing falls back to this process (the same size and
    page limits apply) and every result says so, instead of silently reading
    nothing."""

    def __init__(self, timeout: float = PARSE_TIMEOUT):
        self.timeout = timeout
        self._pool = None
        self._worker_ok = False  # has the worker returned at least one result?
        self._in_process = False  # the worker could not start; parse here

    def _get_pool(self):
        if self._pool is None:
            import multiprocessing
            from concurrent.futures import ProcessPoolExecutor
            ctx = multiprocessing.get_context()
            exe = worker_executable()
            if exe:
                ctx.set_executable(exe)
            self._pool = ProcessPoolExecutor(max_workers=1, mp_context=ctx)
        return self._pool

    def _kill_pool(self) -> None:
        if self._pool is not None:
            for proc in list(getattr(self._pool, "_processes", {}).values()):
                proc.kill()
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None

    def __call__(self, path: Path, kind: str | None, ext: str) -> tuple[str, dict[str, Any], list[str]]:
        if kind not in PARSED_KINDS:
            return extract(path, kind, ext)
        return self.run(extract, path, kind, ext)

    def preview(self, path: Path) -> str:
        """A small JPEG preview (base64) of an image, made in the worker."""
        return self.run(preview_or_empty, path)[0]

    def run(self, fn, *args) -> tuple[str, dict[str, Any], list[str]]:
        """fn(*args) in the worker, with the timeout and fallback rules."""
        if self._in_process:
            text, meta, warns = fn(*args)
            return text, meta, warns + [IN_PROCESS_NOTE]
        from concurrent.futures import TimeoutError as FutTimeout
        from concurrent.futures.process import BrokenProcessPool
        try:
            fut = self._get_pool().submit(fn, *args)
            out = fut.result(timeout=self.timeout)
            self._worker_ok = True
            return out
        except FutTimeout:
            self._kill_pool()
            return "", {}, [f"content parsing timed out after {self.timeout:.0f}s"]
        except (BrokenProcessPool, OSError, RuntimeError) as e:
            self._kill_pool()
            if not self._worker_ok:
                # The worker never started (not a hostile file: it failed before
                # reading anything). Without it no document would be read at all.
                self._in_process = True
                text, meta, warns = fn(*args)
                return text, meta, warns + [IN_PROCESS_NOTE]
            return "", {}, [f"content parser crashed ({type(e).__name__})"]

    def close(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=True)
            self._pool = None

    def __enter__(self) -> "IsolatedExtractor":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def extract(path: Path, kind: str | None, ext: str) -> tuple[str, dict[str, Any], list[str]]:
    """Return (excerpt, metadata, warnings). Never raises."""
    try:
        if kind == "pdf":
            text, meta = extract_pdf(path)
        elif kind == "docx":
            text, meta = extract_docx(path)
        elif kind == "pptx":
            text, meta = extract_pptx(path)
        elif kind == "xlsx":
            text, meta = extract_xlsx(path)
        elif kind in ("png", "jpeg", "gif", "bmp", "webp"):
            text, meta = extract_image(path)
        elif kind is None and ext in TEXT_EXTS:
            text, meta = extract_plain(path)
        else:
            return "", {}, []
        return text, meta, []
    except Exception as e:  # untrusted input: any parser error is just "unreadable"
        return "", {}, [f"could not read content ({type(e).__name__})"]
