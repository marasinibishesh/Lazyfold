"""Plain data structures shared by the scanner, planner, executor and UI."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class FileRecord:
    """Read-only facts about one file in the selected folder."""

    rel_path: str  # relative to the selected root, using the OS separator
    name: str
    ext: str  # lower-case, including the dot ("" if none)
    size: int
    mtime_ns: int
    mtime_iso: str
    sniffed: str | None = None  # type detected from magic bytes ("pdf", "zip", "exe", ...)
    ext_mismatch: bool = False  # extension contradicts the sniffed content type
    head_sha: str | None = None  # hash of the first 64 KiB, part of the change fingerprint
    sha256: str | None = None  # full hash, computed only for same-size candidates
    source_host: str | None = None  # from the Windows Mark-of-the-Web, if present
    text: str = ""  # bounded excerpt, never the whole file
    meta: dict[str, Any] = field(default_factory=dict)
    skip_reason: str | None = None  # set when the file must never be touched
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "FileRecord":
        return cls(**d)


@dataclass
class Proposal:
    """One proposed action for one file.

    `action` is "move", "keep" or "recycle" (send to the Recycle Bin; only ever
    proposed for old installers, unticked by default, confirmed separately)."""

    rel_path: str
    action: str
    category: str | None
    new_name: str | None
    dest_rel: str | None
    confidence: float
    reasons: list[str]
    engine: str  # "rules", "rules+model", "rules+learned"
    fingerprint: dict[str, Any]
    duplicate_of: str | None = None
    approved: bool = False
    extract_to: str | None = None  # unpack this zip into this new folder after moving it
    near_duplicate: bool = False  # similar (not identical) to another file: never pre-approved
    source_host: str | None = None  # download provenance, used to learn from corrections
    ext: str = ""
    accept_prob: float | None = None  # predicted P(user accepts unchanged); None = no prediction

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Proposal":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class Plan:
    plan_id: str
    root: str
    created: str
    engine: str
    proposals: list[Proposal]
    scan_stats: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["proposals"] = [p.to_dict() for p in self.proposals]
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Plan":
        d = dict(d)
        d["proposals"] = [Proposal.from_dict(p) for p in d["proposals"]]
        return cls(**d)
