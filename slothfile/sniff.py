"""Identify file content from its first bytes so a misleading extension
(e.g. an executable named photo.jpg) is detected. Files are only read, never run."""

from __future__ import annotations

import zipfile
from pathlib import Path

# (offset, magic bytes, kind)
_SIGNATURES: list[tuple[int, bytes, str]] = [
    (0, b"%PDF-", "pdf"),
    (0, b"\x89PNG\r\n\x1a\n", "png"),
    (0, b"\xff\xd8\xff", "jpeg"),
    (0, b"GIF87a", "gif"),
    (0, b"GIF89a", "gif"),
    (0, b"BM", "bmp"),
    (0, b"MZ", "exe"),
    (0, b"\x7fELF", "elf"),
    (0, b"PK\x03\x04", "zip"),
    (0, b"PK\x05\x06", "zip"),
    (0, b"Rar!\x1a\x07", "rar"),
    (0, b"7z\xbc\xaf\x27\x1c", "7z"),
    (0, b"\x1f\x8b", "gzip"),
    (0, b"BZh", "bzip2"),
    (0, b"\xfd7zXZ\x00", "xz"),
    (0, b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "ole"),  # legacy Office / MSI
    (0, b"ID3", "mp3"),
    (0, b"OggS", "ogg"),
    (0, b"fLaC", "flac"),
    (0, b"\x1a\x45\xdf\xa3", "mkv"),
    (0, b"wOFF", "woff"),
    (0, b"wOF2", "woff2"),
    (0, b"\x00\x01\x00\x00\x00", "ttf"),
    (0, b"OTTO", "otf"),
    (0, b"{\\rtf", "rtf"),
    (0, b"d8:announce", "torrent"),
    (257, b"ustar", "tar"),
]

# Which sniffed kinds are consistent with which extensions.
COMPATIBLE: dict[str, set[str]] = {
    "pdf": {".pdf"},
    "png": {".png"},
    "jpeg": {".jpg", ".jpeg", ".jfif"},
    "gif": {".gif"},
    "bmp": {".bmp"},
    "webp": {".webp"},
    "exe": {".exe", ".dll", ".sys", ".scr", ".com", ".msi", ".efi", ".cpl", ".ocx"},
    "elf": {"", ".so", ".bin", ".run", ".appimage"},
    "zip": {".zip", ".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp", ".epub", ".jar",
            ".apk", ".msix", ".appx", ".nupkg", ".whl", ".vsix", ".xpi", ".kmz", ".3mf", ".cbz"},
    "docx": {".docx"}, "xlsx": {".xlsx"}, "pptx": {".pptx"}, "epub": {".epub"},
    "rar": {".rar", ".cbr"},
    "7z": {".7z"},
    "gzip": {".gz", ".tgz"},
    "bzip2": {".bz2", ".tbz2"},
    "xz": {".xz", ".txz"},
    "tar": {".tar"},
    "ole": {".doc", ".xls", ".ppt", ".msi", ".msg"},
    "mp3": {".mp3"},
    "ogg": {".ogg", ".oga", ".opus"},
    "flac": {".flac"},
    "wav": {".wav"},
    "avi": {".avi"},
    "mp4": {".mp4", ".m4a", ".m4v", ".mov", ".3gp", ".heic", ".avif"},
    "mkv": {".mkv", ".webm"},
    "woff": {".woff"}, "woff2": {".woff2"}, "ttf": {".ttf"}, "otf": {".otf"},
    "rtf": {".rtf"},
    "torrent": {".torrent"},
}

# Content kinds that can execute code. A file whose bytes say "executable" but whose
# name says otherwise is the classic disguised-malware pattern: never touch it.
EXECUTABLE_KINDS = {"exe", "elf"}


def sniff_bytes(head: bytes) -> str | None:
    for offset, magic, kind in _SIGNATURES:
        if head[offset:offset + len(magic)] == magic:
            return kind
    if head[:4] == b"RIFF" and len(head) >= 12:
        return {b"WEBP": "webp", b"WAVE": "wav", b"AVI ": "avi"}.get(head[8:12])
    if head[4:8] == b"ftyp":
        return "mp4"
    return None


def refine_zip(path: Path) -> str:
    """Distinguish Office Open XML / EPUB from a plain zip by listing names only.
    Nothing is extracted to disk."""
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist()[:2000])
    except (zipfile.BadZipFile, OSError, RuntimeError, ValueError):
        return "zip"
    if "word/document.xml" in names:
        return "docx"
    if "xl/workbook.xml" in names:
        return "xlsx"
    if "ppt/presentation.xml" in names:
        return "pptx"
    if "mimetype" in names and "META-INF/container.xml" in names:
        return "epub"
    return "zip"


def looks_like_text(head: bytes) -> bool:
    if not head:
        return True
    if b"\x00" in head[:4096]:
        return head[:2] in (b"\xff\xfe", b"\xfe\xff")  # UTF-16 BOM
    try:
        head[:4096].decode("utf-8")
        return True
    except UnicodeDecodeError as e:
        # A multibyte char cut at the end of the sample is still text.
        return e.start >= len(head[:4096]) - 3


def mismatch(kind: str | None, ext: str) -> bool:
    """True when the content type is known and the extension contradicts it."""
    if kind is None:
        return False
    allowed = COMPATIBLE.get(kind)
    if allowed is None:
        return False
    if kind == "zip" and ext in COMPATIBLE["zip"]:
        return False
    return ext not in allowed
