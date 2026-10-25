"""Combine the deterministic rules with optional local-model advice.

Policy (deliberately conservative):
- Files the rules refuse (skipped, disguised, unknown type) never reach the model.
- The model is consulted only for documents, where sub-category and naming
  genuinely need reading comprehension; installers, media, archives etc. are
  decided by type alone.
- Agreement raises confidence; disagreement falls back to the general
  "documents" folder; "unsure" defers to the rules.
- A model-suggested name for a document is used only if it is grounded in the
  file's own text. With content naming on (the default when Gemma is used),
  Gemma also names slides, spreadsheets and pictures, and any file whose name
  doesn't already say what it is; otherwise only generic names are replaced.
  Every name is only a suggestion the user approves.
- Any model error falls back to the rules for that file; repeated errors
  switch the model off for the rest of the run.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from .llm import DEFAULT_MODEL, DEFAULT_URL, ModelAdvice, ModelError, NameAdvice, OllamaClient
from .models import FileRecord
from .rules import DOC_CATEGORIES, Decision, classify, find_dates, is_generic_name

_LABEL_WORDS = {"invoice", "receipt", "statement", "resume", "manual", "guide", "ticket", "booking",
                "confirmation", "letter", "offer", "travel", "document", "notes", "boarding", "pass"}


def grounded(name: str, rec: FileRecord) -> bool:
    """At least half of the distinctive words in `name` must occur in the file's
    text, title or source. Blocks hallucinated or injected names."""
    source = " ".join([rec.text, rec.meta.get("title", ""), rec.source_host or ""]).lower()
    words = [w for w in re.findall(r"[a-z][a-z0-9]{2,}", name.lower()) if w not in _LABEL_WORDS]
    if not words:
        return False
    return sum(1 for w in words if w in source) * 2 >= len(words)


def strip_ungrounded_dates(name: str, rec: FileRecord) -> str:
    """Remove dates the model put in a name that the document itself never
    mentions (e.g. it borrowed the file's modified date)."""
    source = " ".join([rec.text, rec.meta.get("title", "")])
    allowed = set(find_dates(source))

    def keep(m: re.Match) -> str:
        d = m.group(0)
        return d if d in allowed or d in source or any(a.startswith(d) for a in allowed) else ""
    out = re.sub(r"\b(?:19|20)\d\d-\d\d(?:-\d\d)?\b", keep, name)
    return re.sub(r"(?:\s*[-_,]\s*){2,}", " - ", out).strip(" -_,")


NAMED_BY_CONTENT = {"presentations", "spreadsheets", "images", "screenshots"}
PICTURE_KINDS = {"png", "jpeg", "gif", "bmp", "webp"}
MAX_PICTURES = 40  # vision calls per scan: pictures are slow to look at


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{3,}", s.lower()) if w not in _LABEL_WORDS}


def says_the_same(name: str, stem: str) -> bool:
    """True when the current name already carries the suggestion's words, so a
    rename would add nothing ("Q3 review" vs "Q3_review_final", "Logo final
    version 2" vs "logo-final-v2")."""
    from difflib import SequenceMatcher
    new = _words(name)
    if not new or len(new & _words(stem)) * 10 >= len(new) * 7:
        return True
    a, b = re.sub(r"[^a-z0-9]", "", name.lower()), re.sub(r"[^a-z0-9]", "", stem.lower())
    return SequenceMatcher(None, a, b).ratio() >= 0.75


def _date_in(stem: str) -> str | None:
    """A date in a camera/screenshot name (2026-10-12, 20261012, 2026_10_12)."""
    m = re.search(r"(20\d\d)[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])", stem)
    return f"{m[1]}-{m[2]}-{m[3]}" if m else None


def accept_name(name: str, rec: FileRecord, *, any_name: bool, seen: bool = False) -> tuple[str | None, str | None]:
    """(stem to suggest, reason) or (None, why not / None). `seen`: Gemma looked
    at the picture itself, so its words can't be checked against text."""
    stem = rec.name.rsplit(".", 1)[0] if "." in rec.name else rec.name
    name = strip_ungrounded_dates(name, rec) if name else ""
    if not name:
        return None, None
    generic = is_generic_name(stem)
    if not generic and (not any_name or says_the_same(name, stem)):
        return None, None
    if not seen and not grounded(name, rec):
        return None, "ignored model name not found in the file's text"
    if seen:
        when = _date_in(stem)  # keep the date a camera or screenshot name carried
        if when and when not in name:
            name = f"{name} - {when}"
    what = "the picture" if seen else "the content"
    if generic:
        return name, f"name is generic; Gemma suggested one from {what}"
    return name, f"Gemma suggested a clearer name from {what}"


class HybridClassifier:
    MAX_FAILURES = 3

    def __init__(self, client: OllamaClient, *, root: Path | None = None, content_names: bool = False):
        self.client = client
        self.root = root
        self.content_names = content_names
        self.failures = 0
        self.calls = 0
        self.seconds = 0.0
        self.pictures = 0
        self.disabled_reason: str | None = None
        self._previews = None

    def __call__(self, rec: FileRecord) -> Decision:
        base = classify(rec)
        if self.disabled_reason:
            return base
        if base.category in DOC_CATEGORIES:
            if not (rec.text or rec.meta.get("title")):
                return base
            advice = self._ask(base, lambda: self.client.advise(rec))
            return merge(base, advice, rec, any_name=self.content_names) if advice else base
        if self.content_names and base.category in NAMED_BY_CONTENT:
            return self._name_only(base, rec)
        return base

    def _ask(self, base: Decision, call):
        try:
            advice = call()
        except ModelError as e:
            self.failures += 1
            if self.failures >= self.MAX_FAILURES:
                self.disabled_reason = str(e)
            base.reasons.append(f"model unavailable ({e}); used rules")
            return None
        self.failures = 0
        self.calls += 1
        self.seconds += advice.seconds
        return advice

    def _name_only(self, base: Decision, rec: FileRecord) -> Decision:
        """Slides, spreadsheets and pictures keep the rules' folder; Gemma only names them."""
        image = None
        if base.category in ("images", "screenshots"):
            if self.pictures >= MAX_PICTURES or self.root is None or rec.sniffed not in PICTURE_KINDS:
                return base
            image = self._preview(self.root / rec.rel_path)
            if not image:
                return base
            self.pictures += 1
        elif not (rec.text or rec.meta.get("title")):
            return base
        advice: NameAdvice | None = self._ask(base, lambda: self.client.suggest_name(rec, image))
        if advice is None:
            return base
        name, why = accept_name(advice.name, rec, any_name=True, seen=image is not None)
        if name:
            base.new_stem = name
            base.reasons = [r for r in base.reasons if not r.startswith("name is generic")]
            base.reasons.append(f"Gemma: {advice.reason}" if advice.reason else "Gemma")
        if why:
            base.reasons.append(why)
        return base

    def _preview(self, path: Path) -> str:
        from .extract import IsolatedExtractor
        if self._previews is None:
            self._previews = IsolatedExtractor()
        return self._previews.preview(path)

    def close(self) -> None:
        if self._previews is not None:
            self._previews.close()
            self._previews = None


def merge(base: Decision, advice: ModelAdvice, rec: FileRecord, *, any_name: bool = False) -> Decision:
    d = Decision(base.category, base.confidence, list(base.reasons), base.new_stem)
    why = f"Gemma: {advice.reason}" if advice.reason else "Gemma"
    if advice.category == "unsure":
        d.reasons.append("Gemma unsure; used rules")
    elif advice.category == base.category:
        d.confidence = round(max(base.confidence, min(0.95, advice.confidence)), 2)
        d.reasons.append(f"{why} (agrees)")
    elif base.category == "documents" and advice.confidence >= 0.7:
        d.category = advice.category
        d.confidence = round(min(0.9, advice.confidence * 0.9), 2)
        d.reasons = [r.replace("; kept as general document", "") for r in d.reasons]
        d.reasons.append(f"{why} (decided {advice.category})")
    elif base.category == "documents":
        d.reasons.append(f"model suggested {advice.category} with low confidence; kept as general document")
    else:
        d.reasons.append(f"rules said {base.category}, model said {advice.category}; filed as general document")
        d.category, d.confidence = "documents", min(base.confidence, 0.75)

    name, why = accept_name(advice.name, rec, any_name=any_name)
    if name:
        d.new_stem = name
        d.reasons = [r for r in d.reasons if not r.startswith("name is generic")]
    if why:
        d.reasons.append(why)
    return d


def make_classifier(model: str | None, *, base_url: str | None = None, timeout: float = 60.0,
                    root: Path | None = None,
                    content_names: bool = False) -> tuple[Callable[[FileRecord], Decision], str, str]:
    """Return (classifier, engine label, human status). Never raises: if the
    model can't be used, the rules engine is returned with an explanation."""
    if not model:
        return classify, "rules", "rules only (no model selected)"
    try:
        client = OllamaClient(model or DEFAULT_MODEL, base_url or DEFAULT_URL, timeout=timeout)
        try:
            status = client.health()
        except ModelError:
            # Only our own default local server is worth starting; a custom URL is the user's business.
            if base_url not in (None, DEFAULT_URL) or not _autostart_ollama():
                raise
            status = client.health()
        client.warm_up()
    except ModelError as e:
        return classify, "rules", f"model unavailable - {e}; using rules only"
    return HybridClassifier(client, root=root, content_names=content_names), "rules+model", status


def _autostart_ollama() -> bool:
    """Start an installed-but-stopped Ollama once, if the user wants the model.
    WHY: after a reboot Ollama is often not running, and users of the installed
    app have no terminal to type `ollama serve` into. Never raises."""
    try:
        from . import ai_setup
        from .settings import load_settings
        if not load_settings().get("use_model"):
            return False
        path = ai_setup.find_ollama()
        if not path or ai_setup.server_running():
            return False  # nothing to start, or it is running and the problem is something else
        return ai_setup.start_server(path, wait=15.0)  # a cold start measured ~5 s
    except Exception:
        return False
