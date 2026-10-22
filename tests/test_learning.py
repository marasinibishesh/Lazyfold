import json
from pathlib import Path

from slothfile import learning
from slothfile.journal import state_dir
from slothfile.learning import apply_learned, forget_rule, learned_rules, record_review, reset_learning
from slothfile.models import FileRecord, Plan, Proposal
from slothfile.rules import Decision


def rec(name: str, host: str | None = None, **kw) -> FileRecord:
    ext = "." + name.rsplit(".", 1)[1].lower() if "." in name else ""
    return FileRecord(rel_path=name, name=name, ext=ext, size=10, mtime_ns=0, mtime_iso="",
                      source_host=host, **kw)


def prop(name: str, category: str) -> Proposal:
    return Proposal(name, "move", category, None, f"X/{name}", 0.9, [], "rules", {})


def review(items: list[tuple[FileRecord, str]], approved=None, overrides=None) -> int:
    plan = Plan("p", "root", "", "rules", [prop(r.rel_path, c) for r, c in items])
    by_name = {r.rel_path: r for r, _ in items}
    approved = set(by_name) if approved is None else approved
    return record_review(plan, by_name, approved, overrides or {})


def test_corrections_become_rule_after_two(tmp_path):
    a = rec("statement1.pdf", "www.Bank.example")
    assert review([(a, "documents")], overrides={a.rel_path: "finance"}) == 1
    assert learned_rules() == []  # one correction is not a habit yet
    b = rec("statement2.pdf", "bank.example")
    review([(b, "documents")], overrides={b.rel_path: "finance"})
    rules = {r.key: r for r in learned_rules()}
    assert rules["host:bank.example"].outcome == "finance"
    assert rules["host:bank.example"].support == 2
    d = apply_learned(rec("new.pdf", "bank.example"), Decision("documents", 0.7, ["extension .pdf"]))
    assert d.category == "finance" and d.confidence == 0.92
    assert "you moved files from bank.example to Documents/Finance 2 times before" in d.reasons[-1]


def test_plain_agreement_creates_no_rule():
    for i in range(5):
        r = rec(f"pic{i}.png", "cdn.example")
        review([(r, "images")])
    assert learned_rules() == []
    d0 = Decision("images", 0.9, ["extension .png"])
    assert apply_learned(rec("x.png", "cdn.example"), d0) is d0


def test_confirmation_after_correction_strengthens_rule():
    for i in range(2):
        r = rec(f"s{i}.pdf", "bank.example")
        review([(r, "documents")], overrides={r.rel_path: "finance"})
    r = rec("s9.pdf", "bank.example")
    review([(r, "finance")])  # the learned proposal was simply approved
    rule = {x.key: x for x in learned_rules()}["host:bank.example"]
    assert rule.support == 3 and rule.contradictions == 0


def test_contradiction_disables():
    for i in range(2):
        r = rec(f"s{i}.pdf", "bank.example")
        review([(r, "documents")], overrides={r.rel_path: "finance"})
    r = rec("s3.pdf", "bank.example")
    review([(r, "documents")], overrides={r.rel_path: "travel"})
    assert "host:bank.example" not in {x.key for x in learned_rules()}


def test_host_beats_ext():
    for i in range(2):
        r = rec(f"b{i}.pdf", "bank.example")
        review([(r, "documents")], overrides={r.rel_path: "finance"})
    for i in range(3):  # later, host-less PDFs go to Manuals: the .pdf rule follows them
        r = rec(f"a{i}.pdf", None)
        review([(r, "documents")], overrides={r.rel_path: "manuals"})
    keys = [x.key for x in learned_rules()]
    assert keys.index("host:bank.example") < keys.index("ext:.pdf")
    assert apply_learned(rec("c.pdf", "bank.example"), Decision("documents", 0.7)).category == "finance"
    assert apply_learned(rec("c.pdf", "other.example"), Decision("documents", 0.7)).category == "manuals"


def test_invalid_json_and_outcomes_ignored():
    path = state_dir() / "learned.json"
    path.write_text("{not json", encoding="utf-8")
    assert learned_rules() == []
    obs = {"features": {"host": "evil.example", "ext": ".pdf"}, "proposed": "documents", "ts": ""}
    bad = [dict(obs, outcome="../../Windows"), dict(obs, outcome="../../Windows"),
           dict(obs, outcome="finance", features="nope"), "junk", 42,
           dict(obs, outcome="finance", features={"host": 5, "ext": "pdf"}),
           dict(obs, outcome="finance", features={"host": 5, "ext": "pdf"})]
    path.write_text(json.dumps({"observations": bad}), encoding="utf-8")
    assert learned_rules() == []
    path.write_text(json.dumps([1, 2]), encoding="utf-8")
    assert learned_rules() == []
    # Invalid overrides are never stored.
    r = rec("a.pdf", "x.example")
    assert review([(r, "documents")], overrides={r.rel_path: "C:/Windows"}) == 0
    # A record the rules refused to judge neither teaches nor receives rules.
    s = rec("b.pdf", "x.example", skip_reason="in use")
    assert review([(s, "documents")], overrides={s.rel_path: "finance"}) == 0
    m = rec("c.pdf", "x.example", ext_mismatch=True)
    d0 = Decision(None, 0.2, ["mismatch"])
    assert apply_learned(m, d0) is d0


def test_keep_rule_leaves_file_untouched():
    for i in range(2):
        r = rec(f"setup{i}.exe", "tools.example")
        review([(r, "installers")], approved=set())  # unticked twice
    rule = {x.key: x for x in learned_rules()}["host:tools.example"]
    assert rule.outcome == "keep"
    d = apply_learned(rec("setup9.exe", "tools.example"), Decision("installers", 0.9, ["extension .exe"]))
    assert d.category is None and d.confidence == 0.0
    assert "left files from tools.example where they were" in d.reasons[-1]


def test_cap_at_2000(monkeypatch):
    items = [(rec(f"f{i}.txt", None), "documents") for i in range(2100)]
    assert review(items) == 2100
    data = json.loads((state_dir() / "learned.json").read_text(encoding="utf-8"))
    assert len(data["observations"]) == learning.MAX_OBSERVATIONS == 2000


def test_forget_and_reset():
    for i in range(2):
        r = rec(f"s{i}.pdf", "bank.example")
        review([(r, "documents")], overrides={r.rel_path: "finance"})
    keys = {x.key for x in learned_rules()}
    assert keys == {"host:bank.example", "ext:.pdf"}
    assert forget_rule("host:bank.example") is True
    assert {x.key for x in learned_rules()} == {"ext:.pdf"}
    assert forget_rule("host:bank.example") is False
    reset_learning()
    assert learned_rules() == []
    reset_learning()  # idempotent


# --- regressions -----------------------------------------------------------

import os  # noqa: E402
import time  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_learning_cache():
    """No rules cache carried over from another test (or another state dir)."""
    learning._invalidate()
    yield
    learning._invalidate()


def test_deeply_nested_json_yields_defaults():
    path = state_dir() / "learned.json"
    path.write_text("[" * 200000 + "]" * 200000, encoding="utf-8")
    assert learned_rules() == []
    d0 = Decision("documents", 0.7)
    assert apply_learned(rec("x.pdf", "bank.example"), d0) is d0
    path.write_text('{"observations": ' + "[" * 200000 + "]" * 200000 + "}", encoding="utf-8")
    assert learned_rules() == []


def test_apply_learned_is_fast_with_full_history():
    obs = [{"features": {"host": f"h{i % 50}.com", "ext": f".e{i % 30}"}, "proposed": "documents",
            "outcome": "keep", "ts": "2026"} for i in range(2000)]
    (state_dir() / "learned.json").write_text(json.dumps({"observations": obs}), encoding="utf-8")
    r = rec("x.pdf", "zz.com")
    d0 = Decision("documents", 0.5)
    t = time.perf_counter()
    for _ in range(500):
        assert apply_learned(r, d0) is d0
    assert time.perf_counter() - t < 1.0


def test_rules_cache_follows_every_change():
    d0 = Decision("documents", 0.7)
    probe = rec("n.pdf", "bank.example")
    assert apply_learned(probe, d0) is d0  # primes the cache with "no rules"
    for i in range(2):
        r = rec(f"s{i}.pdf", "bank.example")
        review([(r, "documents")], overrides={r.rel_path: "finance"})
    assert apply_learned(probe, d0).category == "finance"  # _save invalidated
    assert forget_rule("host:bank.example")
    assert forget_rule("ext:.pdf")
    assert apply_learned(probe, d0) is d0  # forget_rule invalidated
    for i in range(2):
        r = rec(f"t{i}.pdf", "bank.example")
        review([(r, "documents")], overrides={r.rel_path: "travel"})
    assert apply_learned(probe, d0).category == "travel"
    reset_learning()
    assert apply_learned(probe, d0) is d0  # reset invalidated
    # An outside rewrite of the file (different size/mtime) is noticed too.
    for i in range(2):
        r = rec(f"u{i}.pdf", "bank.example")
        review([(r, "documents")], overrides={r.rel_path: "career"})
    assert apply_learned(probe, d0).category == "career"
    path = state_dir() / "learned.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    for o in data["observations"]:
        o["outcome"] = "finance"
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")
    assert apply_learned(probe, d0).category == "finance"


def test_transient_lock_on_learned_json_never_erases_history(monkeypatch):
    """Windows AV/indexer can briefly lock learned.json. A failed read must not
    be mistaken for an empty history (the next save would wipe it)."""
    for i in range(2):
        r = rec(f"b{i}.pdf", "bank.example")
        review([(r, "documents")], overrides={r.rel_path: "finance"})
    monkeypatch.setattr(learning, "_RETRY_DELAY", 0)
    real_read = Path.read_text
    real_replace = os.replace
    fails = {"read": 3, "replace": 3}

    def flaky_read(self, *a, **kw):
        if self.name == "learned.json" and fails["read"]:
            fails["read"] -= 1
            raise PermissionError(13, "locked")
        return real_read(self, *a, **kw)

    def flaky_replace(src, dst):
        if str(dst).endswith("learned.json") and fails["replace"]:
            fails["replace"] -= 1
            raise PermissionError(13, "locked")
        return real_replace(src, dst)

    monkeypatch.setattr(Path, "read_text", flaky_read)
    monkeypatch.setattr(learning.os, "replace", flaky_replace)
    r = rec("a0.pdf", None)
    assert review([(r, "documents")], overrides={r.rel_path: "manuals"}) == 1
    assert fails == {"read": 0, "replace": 0}
    assert "host:bank.example" in {x.key for x in learned_rules()}
    # A lock that never goes away: nothing is stored and nothing is overwritten.
    fails["read"] = 10 ** 6
    before = real_read(state_dir() / "learned.json", encoding="utf-8")
    r = rec("a1.pdf", None)
    assert review([(r, "documents")], overrides={r.rel_path: "manuals"}) == 0
    assert forget_rule("host:bank.example") is False
    fails["read"] = 0
    assert real_read(state_dir() / "learned.json", encoding="utf-8") == before
    assert "host:bank.example" in {x.key for x in learned_rules()}
