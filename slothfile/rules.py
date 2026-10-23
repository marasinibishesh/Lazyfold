"""Deterministic classifier and namer: the baseline that always works offline.

Every decision carries human-readable reasons so the preview can explain itself.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from .models import FileRecord

# Destination folders, relative to the selected root. The model may only pick
# from these keys; it never produces paths.
CATEGORIES: dict[str, str] = {
    "installers": "Installers",
    "archives": "Archives",
    "finance": "Documents/Finance",
    "travel": "Documents/Travel",
    "career": "Documents/Career",
    "manuals": "Documents/Manuals",
    "documents": "Documents",
    "spreadsheets": "Spreadsheets",
    "presentations": "Presentations",
    "screenshots": "Images/Screenshots",
    "images": "Images",
    "videos": "Videos",
    "audio": "Audio",
    "code": "Code & Data",
    "ebooks": "eBooks",
    "fonts": "Fonts",
    "torrents": "Torrents",
    "duplicates": "Duplicates",
}
DOC_CATEGORIES = {"finance", "travel", "career", "manuals", "documents"}

_EXT_CATEGORY: dict[str, str] = {}
for _cat, _exts in {
    "installers": ".exe .msi .msix .msixbundle .appx .appxbundle .iso .img .dmg .pkg .mpkg .deb .rpm .appimage .apk",
    "archives": ".zip .7z .rar .tar .gz .tgz .bz2 .xz .tbz2 .txz .zst .cab",
    "documents": ".pdf .doc .docx .odt .rtf .txt .md .pages .tex",
    "spreadsheets": ".xls .xlsx .ods .csv .tsv .numbers",
    "presentations": ".ppt .pptx .odp .key",
    "images": ".jpg .jpeg .png .gif .bmp .webp .heic .tif .tiff .svg .avif .jfif .raw .cr2 .nef .psd",
    "videos": ".mp4 .mkv .mov .avi .webm .wmv .m4v .flv .3gp",
    "audio": ".mp3 .wav .flac .m4a .aac .ogg .opus .wma .mid .midi",
    "code": ".py .js .ts .json .xml .yaml .yml .sql .ipynb .html .htm .css .java .c .cpp .h .cs .go .rs .sh .ps1 .bat .ini .cfg .log",
    "ebooks": ".epub .mobi .azw .azw3 .djvu .fb2 .cbz .cbr",
    "fonts": ".ttf .otf .woff .woff2",
    "torrents": ".torrent",
}.items():
    for _e in _exts.split():
        _EXT_CATEGORY[_e] = _cat

# What the content says it is overrides a missing or odd extension.
_SNIFF_CATEGORY = {"pdf": "documents", "docx": "documents", "rtf": "documents",
                   "xlsx": "spreadsheets", "pptx": "presentations", "epub": "ebooks",
                   "png": "images", "jpeg": "images", "gif": "images", "bmp": "images", "webp": "images",
                   "mp4": "videos", "mkv": "videos", "avi": "videos",
                   "mp3": "audio", "ogg": "audio", "flac": "audio", "wav": "audio",
                   "zip": "archives", "rar": "archives", "7z": "archives", "gzip": "archives",
                   "tar": "archives", "torrent": "torrents", "ttf": "fonts", "otf": "fonts",
                   "dmg": "installers", "xar": "installers"}

# Keyword evidence for document sub-categories: (pattern, weight).
_KEYWORDS: dict[str, list[tuple[str, int]]] = {
    "finance": [(r"\binvoice\b", 3), (r"\breceipt\b", 3), (r"\bamount due\b", 3), (r"\btotal due\b", 3),
                (r"\bsubtotal\b", 2), (r"\bbilling\b", 1), (r"\bpayment\b", 1), (r"\border (?:no|number|#)", 2),
                (r"\b(?:bank|account) statement\b", 3), (r"\bstatement period\b", 3), (r"\btax\b", 1),
                (r"\bvat\b", 1), (r"\bpaid\b", 1), (r"\bbalance\b", 1), (r"\b1099\b|\bw-2\b|\bw2\b", 3)],
    "travel": [(r"\bboarding pass\b", 4), (r"\bitinerary\b", 3), (r"\bflight\b", 2), (r"\be-?ticket\b", 3),
               (r"\bbooking (?:confirmation|reference)\b", 3), (r"\breservation\b", 2), (r"\bcheck-?in\b", 1),
               (r"\bhotel\b", 2), (r"\bgate\b", 1), (r"\bdeparture\b", 2), (r"\bpnr\b", 3), (r"\bseat\b", 1)],
    "career": [(r"\bresume\b|\brésumé\b", 3), (r"\bcurriculum vitae\b", 4), (r"\bcv\b", 2),
               (r"\bwork experience\b|\bprofessional experience\b", 3), (r"\beducation\b", 1),
               (r"\bskills\b", 1), (r"\boffer letter\b", 4), (r"\bcover letter\b", 4), (r"\bjob offer\b", 3)],
    "manuals": [(r"\buser (?:manual|guide)\b", 4), (r"\bdatasheet\b", 4), (r"\binstallation guide\b", 4),
                (r"\bquick ?start\b", 3), (r"\bowner'?s manual\b", 4), (r"\bsafety instructions\b", 2),
                (r"\bspecifications\b", 1), (r"\btroubleshooting\b", 2)],
}
_LABEL = {"finance": "Invoice", "travel": "Travel", "career": "Resume", "manuals": "Manual"}

_SCREENSHOT = re.compile(r"^(screenshot|screen shot|capture|snip|scr)[\s_\-]|screenshot", re.I)
# Names that carry no meaning: "document (3)", "download", "scan0001", hex blobs...
# A document word followed by a long number ("invoice_10234") carries an ID and is kept.
_GENERIC = re.compile(
    r"^(?:(?:document|doc|download|downloads|file|untitled|scan|scanned|new|attachment|print|output|"
    r"export|getfile|view|fetch|default|temp|unnamed|img|image|dsc|pxl)[\s_\-]*\d*"
    r"|(?:invoice|receipt|statement|report)"
    r"|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f_\-]{16,}|\d{3,})"
    r"[\s_\-]*(?:\(\d+\))?$", re.I)
_LABEL_ONLY = re.compile(r"^(invoice|receipt|statement|boarding pass|resume|cv|itinerary|e-?ticket|quick start)$", re.I)
_MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()


@dataclass
class Decision:
    category: str | None
    confidence: float
    reasons: list[str] = field(default_factory=list)
    new_stem: str | None = None


def is_screenshot_name(name: str) -> bool:
    return bool(_SCREENSHOT.search(name))


def is_generic_name(stem: str) -> bool:
    stem = re.sub(r"\s*\(\d+\)$", "", stem.strip())
    # Hex blobs from download servers ("c0ffee12ab34"): 8+ hex chars with a digit.
    if re.fullmatch(r"[0-9a-f]{8,}", stem, re.I) and re.search(r"\d", stem):
        return True
    return bool(_GENERIC.match(stem)) or len(re.sub(r"[^A-Za-z]", "", stem)) < 2


def find_dates(text: str) -> list[str]:
    """Unambiguous dates in the text, in order, normalised to YYYY-MM-DD.
    Numeric day/month orders (03/04/2026) are ambiguous and ignored."""
    found: list[tuple[int, str]] = []
    for m in re.finditer(r"\b(20\d\d|19\d\d)[-/.](0[1-9]|1[0-2])[-/.](0[1-9]|[12]\d|3[01])\b", text):
        found.append((m.start(), f"{m[1]}-{m[2]}-{m[3]}"))
    for m in re.finditer(r"\b([A-Za-z]{3,9})\.? (\d{1,2}),? (20\d\d)\b", text):
        if m[1][:3].lower() in _MONTHS and 1 <= int(m[2]) <= 31:
            found.append((m.start(), f"{m[3]}-{_MONTHS.index(m[1][:3].lower()) + 1:02d}-{int(m[2]):02d}"))
    for m in re.finditer(r"\b(\d{1,2}) ([A-Za-z]{3,9})\.? (20\d\d)\b", text):
        if m[2][:3].lower() in _MONTHS and 1 <= int(m[1]) <= 31:
            found.append((m.start(), f"{m[3]}-{_MONTHS.index(m[2][:3].lower()) + 1:02d}-{int(m[1]):02d}"))
    return [d for _, d in sorted(found)]


def find_date(text: str) -> str | None:
    """First unambiguous date in the text."""
    dates = find_dates(text)
    return dates[0] if dates else None


def _score_keywords(text: str) -> dict[str, tuple[int, list[str]]]:
    low = text.lower()
    out = {}
    for cat, pats in _KEYWORDS.items():
        score, hits = 0, []
        for pat, w in pats:
            m = re.search(pat, low)
            if m:
                score += w
                hits.append(m.group(0))
        if score:
            out[cat] = (score, hits)
    return out


def _label_from_hit(category: str, hits: list[str]) -> str:
    """Name label from the strongest matched keyword ("receipt" -> "Receipt")."""
    pats = dict(_KEYWORDS[category])
    weighted = sorted(hits, key=lambda h: -max((w for p, w in pats.items() if re.fullmatch(p, h)), default=0))
    for h in weighted:
        if re.fullmatch(r"[a-zé' -]+", h) and h not in ("tax", "paid", "balance", "payment", "seat", "gate", "skills", "education"):
            return h[:1].upper() + h[1:]
    return _LABEL[category]


def _title_candidate(rec: FileRecord) -> str | None:
    title = (rec.meta.get("title") or "").strip()
    if title and not re.match(r"^(untitled|microsoft word|document\d*|slide \d+|powerpoint)", title, re.I) \
            and len(re.sub(r"[^A-Za-z]", "", title)) >= 4:
        return title[:70]
    for line in rec.text.splitlines():
        line = line.strip(" -:#*\t")
        letters = len(re.sub(r"[^A-Za-z]", "", line))
        if 6 <= letters and len(line) <= 70 and len(line.split()) <= 10 and not _LABEL_ONLY.match(line):
            return line
    return None


def suggest_stem(rec: FileRecord, category: str, label: str | None = None) -> str | None:
    """Readable name for a file whose current name is meaningless, else None."""
    stem = os.path.splitext(rec.name)[0]
    if category not in DOC_CATEGORIES or not is_generic_name(stem):
        return None
    title = _title_candidate(rec)
    date = find_date(rec.text) or find_date(rec.meta.get("title", ""))
    label = label or _LABEL.get(category)
    parts = []
    if label and not (title and label.lower() in title.lower()):
        parts.append(label)
    if title:
        parts.append(title)
    elif rec.source_host:
        parts.append(rec.source_host.removeprefix("www."))
    if date and not (title and date in title):
        parts.append(date)
    if not parts or parts == [label]:
        return None
    return " - ".join(parts)


def classify(rec: FileRecord) -> Decision:
    if rec.skip_reason:
        return Decision(None, 0.0, [rec.skip_reason])
    if rec.ext_mismatch:
        return Decision(None, 0.2, rec.warnings[:1] + ["left for you to check"])
    reasons: list[str] = []
    label = None
    cat = _EXT_CATEGORY.get(rec.ext)
    conf = 0.0
    if cat:
        conf = 0.9
        reasons.append(f"extension {rec.ext}")
        sniff_cat = _SNIFF_CATEGORY.get(rec.sniffed or "")
        if sniff_cat and sniff_cat != cat and not (sniff_cat == "archives" and cat != "archives"):
            conf = 0.5
            reasons.append(f"but content looks like {sniff_cat}")
    elif rec.sniffed in _SNIFF_CATEGORY:
        cat = _SNIFF_CATEGORY[rec.sniffed]
        conf = 0.75
        reasons.append(f"content is {rec.sniffed} (extension '{rec.ext or 'none'}' unknown)")
    if cat is None:
        return Decision(None, 0.3, [f"unrecognised type '{rec.ext or 'no extension'}'"])

    if cat == "images" and _SCREENSHOT.search(rec.name):
        cat, conf = "screenshots", 0.95
        reasons.append("name looks like a screenshot")
    elif cat == "images" and rec.meta.get("ocr") and rec.text:
        # A photo of a document (receipt, ticket...): only strong, unambiguous evidence counts.
        scores = _score_keywords(rec.text)
        if scores:
            best = max(scores, key=lambda c: scores[c][0])
            score, hits = scores[best]
            runner_up = max((s for c, (s, _) in scores.items() if c != best), default=0)
            if score >= 4 and score >= 2 * runner_up:
                cat, conf = best, min(0.9, 0.6 + 0.05 * score)
                label = _label_from_hit(best, hits)
                reasons.append("photo of a document: text read with Windows OCR mentions "
                               + ", ".join(f"'{h}'" for h in hits[:3]))

    if cat == "documents":
        scores = _score_keywords(f"{rec.name}\n{rec.meta.get('title', '')}\n{rec.text}")
        if scores:
            best = max(scores, key=lambda c: scores[c][0])
            score, hits = scores[best]
            runner_up = max((s for c, (s, _) in scores.items() if c != best), default=0)
            if score >= 3 and score >= 2 * runner_up:
                cat = best
                conf = min(0.95, 0.65 + 0.05 * score)
                label = _label_from_hit(best, hits)
                reasons.append("mentions " + ", ".join(f"'{h}'" for h in hits[:3]))
            elif score >= 3:
                conf = 0.7
                reasons.append(f"mixed signals ({', '.join(sorted(scores))}); kept as general document")
        if rec.ext in (".txt", ".md") and not rec.text and rec.size > 0:
            conf = min(conf, 0.6)
        if rec.meta.get("ocr"):
            reasons.append("text read with Windows OCR (scanned document)")
        if rec.meta.get("note"):
            reasons.append(rec.meta["note"])
        elif not rec.text and rec.sniffed == "pdf":
            reasons.append("no extractable text (scanned or image PDF)")
        if rec.source_host:
            reasons.append(f"downloaded from {rec.source_host}")

    if rec.size == 0:
        conf = min(conf, 0.4)
        reasons.append("empty file")
    stem = suggest_stem(rec, cat, label)
    if stem:
        reasons.append("name is generic; suggested a descriptive one")
    return Decision(cat, round(conf, 2), reasons, stem)
