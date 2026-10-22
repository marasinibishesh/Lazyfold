"""Combine the deterministic rules with optional local-model advice.

Policy (deliberately conservative):
- Files the rules refuse (skipped, disguised, unknown type) never reach the model.
- The model is consulted only for documents, where sub-category and naming
  genuinely need reading comprehension; installers, media, archives etc. are
  decided by type alone.
- Agreement raises confidence; disagreement falls back to the general
  "documents" folder; "unsure" defers to the rules.
- A model-suggested name is used only if the current name is generic and the
  suggestion is grounded in the file's own text.
- Any model error falls back to the rules for that file; repeated errors
  switch the model off for the rest of the run.
"""

from __future__ import annotations

import re
from typing import Callable

from .llm import DEFAULT_MODEL, DEFAULT_URL, ModelAdvice, ModelError, OllamaClient
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


class HybridClassifier:
    MAX_FAILURES = 3

    def __init__(self, client: OllamaClient):
        self.client = client
        self.failures = 0
        self.calls = 0
        self.seconds = 0.0
        self.disabled_reason: str | None = None

    def __call__(self, rec: FileRecord) -> Decision:
        base = classify(rec)
        if base.category not in DOC_CATEGORIES or self.disabled_reason or not (rec.text or rec.meta.get("title")):
            return base
        try:
            advice = self.client.advise(rec)
        except ModelError as e:
            self.failures += 1
            if self.failures >= self.MAX_FAILURES:
                self.disabled_reason = str(e)
            base.reasons.append(f"model unavailable ({e}); used rules")
            return base
        self.failures = 0
        self.calls += 1
        self.seconds += advice.seconds
        return merge(base, advice, rec)


def merge(base: Decision, advice: ModelAdvice, rec: FileRecord) -> Decision:
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

    stem = rec.name.rsplit(".", 1)[0] if "." in rec.name else rec.name
    name = strip_ungrounded_dates(advice.name, rec) if advice.name else ""
    if is_generic_name(stem) and name:
        if grounded(name, rec):
            d.new_stem = name
            d.reasons = [r for r in d.reasons if not r.startswith("name is generic")]
            d.reasons.append("name is generic; Gemma suggested one from the content")
        else:
            d.reasons.append("ignored model name not found in the file's text")
    return d


def make_classifier(model: str | None, *, base_url: str | None = None,
                    timeout: float = 60.0) -> tuple[Callable[[FileRecord], Decision], str, str]:
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
    return HybridClassifier(client), "rules+model", status


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
