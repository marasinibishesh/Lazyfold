import json
from datetime import datetime, timedelta

from slothfile import summary
from slothfile.journal import runs_dir, state_dir

NOW = datetime(2026, 10, 22, 18, 0, 0)


def _ts(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat(timespec="milliseconds")


def _write(path, recs, torn_tail: str | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(r) + "\n" for r in recs)
    if torn_tail is not None:
        text += torn_tail
    path.write_text(text, encoding="utf-8")


def _organize_run(name, days_ago, ops, undo=(), extra=()):
    """ops: list of (src, dst, outcome) with outcome in done/fail/skip/escaped."""
    ts = _ts(days_ago)
    recs = [{"ts": ts, "t": "begin", "root": "C:\\x", "plan_id": name, "ops": len(ops)}]
    for i, (src, dst, outcome) in enumerate(ops):
        if outcome == "skip":
            recs.append({"ts": ts, "t": "skip", "op": i, "src": src, "reason": "changed"})
            continue
        recs.append({"ts": ts, "t": "intent", "op": i, "src": src, "dst": dst, "fp": {}})
        if outcome == "done":
            recs.append({"ts": ts, "t": "done", "op": i, "verified": True})
        elif outcome == "escaped":
            recs.append({"ts": ts, "t": "done", "op": i, "verified": False, "escaped": True})
        else:
            recs.append({"ts": ts, "t": "fail", "op": i, "error": "x"})
    recs.extend(extra)
    recs.append({"ts": ts, "t": "end"})
    if undo:
        recs.append({"ts": ts, "t": "undo_begin"})
        for i in undo:
            recs.append({"ts": ts, "t": "undo_intent", "op": i})
            recs.append({"ts": ts, "t": "undone", "op": i})
        recs.append({"ts": ts, "t": "undo_end"})
    _write(runs_dir() / f"{name}.jsonl", recs)


def test_organize_counts_undone_and_renamed():
    _organize_run("r1", 1, [
        ("invoice.pdf", "Documents\\Finance\\invoice.pdf", "done"),
        ("scan0001.pdf", "Documents\\Finance\\Payslip - Acme - Sept 2026.pdf", "done"),
        ("setup.exe", "Installers/setup.exe", "done"),
        ("pic.png", "Images\\pic.png", "done"),          # undone below
        ("doc (3).pdf", "Documents\\Work\\Report.pdf", "done"),  # renamed, then undone
        ("gone.txt", "Documents\\gone.txt", "skip"),
        ("bad.txt", "Documents\\bad.txt", "fail"),
        ("esc.txt", "Documents\\esc.txt", "escaped"),
    ], undo=[3, 4])
    s = summary.summarize(7, now=NOW)
    assert s["runs"] == 1
    assert s["files_organized"] == 3
    assert s["files_renamed"] == 1
    assert s["undone"] == 2
    assert s["by_folder"] == {"Documents": 2, "Installers": 1}


def test_window_filtering_and_multiple_runs():
    _organize_run("old", 10, [("a.pdf", "Documents\\a.pdf", "done")])
    _organize_run("new", 2, [("b.pdf", "Documents\\b.pdf", "done")])
    _organize_run("future", -1, [("c.pdf", "Documents\\c.pdf", "done")])
    s = summary.summarize(7, now=NOW)
    assert s["runs"] == 1 and s["files_organized"] == 1
    s30 = summary.summarize(30, now=NOW)
    assert s30["runs"] == 2 and s30["files_organized"] == 2


def test_cleanup_journals_and_extras():
    c = state_dir() / "cleanup"
    _write(c / "c1.jsonl", [
        {"ts": _ts(1), "t": "begin"},
        {"ts": _ts(1), "t": "removed", "path": "C:\\t\\a.tmp", "size": 1024, "mode": "delete"},
        {"ts": _ts(1), "t": "removed", "path": "C:\\t\\b.tmp", "size": 2048, "mode": "recycle"},
        {"ts": _ts(1), "t": "removed", "path": "C:\\t\\c.tmp", "size": True, "mode": "weird"},
        {"ts": _ts(1), "t": "emptied_recycle_bin", "bytes": 4096, "items": 3},
        {"ts": _ts(1), "t": "recycled", "path": "C:\\d\\setup.exe"},
        {"ts": _ts(1), "t": "end"},
    ])
    _write(c / "old.jsonl", [
        {"ts": _ts(20), "t": "begin"},
        {"ts": _ts(20), "t": "removed", "path": "x", "size": 999999, "mode": "delete"},
    ])
    _organize_run("r", 1, [], extra=[{"ts": _ts(1), "t": "extracted", "src": "a.zip"}])
    s = summary.summarize(7, now=NOW)
    assert s["cleanup_runs"] == 1
    assert s["cleanup_files"] == 3
    assert s["cleanup_bytes"] == {"recycle": 2048, "delete": 1024}
    assert s["recycle_bin_emptied_bytes"] == 4096
    assert s["recycle_bin_emptied_items"] == 3
    assert s["recycled_installers"] == 1
    assert s["archives_extracted"] == 1
    text = summary.format_summary(s)
    assert "freed 7.0 KB" in text
    assert "Recycle Bin emptied: 4.0 KB" in text


def test_torn_and_unknown_records_tolerated():
    ts = _ts(1)
    recs = [
        {"ts": ts, "t": "begin", "root": "C:\\x", "plan_id": "p", "ops": 2},
        {"ts": ts, "t": "mystery", "foo": 1},
        {"ts": ts, "t": "intent", "op": 0, "src": "a.pdf", "dst": "Documents\\a.pdf"},
        {"ts": ts, "t": "done", "op": 0, "verified": True},
        {"ts": ts, "t": "intent", "op": 1, "src": "b.pdf", "dst": "Documents\\b.pdf"},
    ]
    _write(runs_dir() / "torn.jsonl", recs, torn_tail='{"ts": "x", "t": "do')
    _write(runs_dir() / "garbage.jsonl", [], torn_tail="\x00\x00not json\n")
    _write(runs_dir() / "nobegin.jsonl", [{"ts": ts, "t": "done", "op": 0}])
    _write(runs_dir() / "badts.jsonl", [{"ts": "yesterday", "t": "begin"}])
    (runs_dir() / "binary.jsonl").write_bytes(b"\xff\xfe\x00bad")
    s = summary.summarize(7, now=NOW)
    assert s["runs"] == 1
    assert s["files_organized"] == 1


def test_format_zero_and_headline():
    s = summary.summarize(7, now=NOW)
    assert summary.format_summary(s) == "Nothing to tidy this week - your sloth rested."
    s = dict(s, files_organized=140, files_renamed=22,
             cleanup_files=5, cleanup_bytes={"recycle": 0, "delete": int(3.2 * 1024 ** 3)},
             by_folder={"Documents": 100, "Images": 40})
    head = summary.format_summary(s).splitlines()[0]
    assert head == "This week SlothFile organized 140 files, renamed 22 and freed 3.2 GB."
    month = summary.format_summary(dict(s, days=30))
    assert month.startswith("This month SlothFile organized 140 files")
    assert "Documents 100, Images 40" in month


def test_human_bytes():
    assert summary.human_bytes(0) == "0 bytes"
    assert summary.human_bytes(1) == "1 byte"
    assert summary.human_bytes(1023) == "1023 bytes"
    assert summary.human_bytes(1536) == "1.5 KB"
    assert summary.human_bytes(5 * 1024 ** 2) == "5.0 MB"
    assert summary.human_bytes(int(3.2 * 1024 ** 3)) == "3.2 GB"
    assert summary.human_bytes(-5) == "0 bytes"


def test_unhashable_or_bool_op_values_are_skipped():
    ts = _ts(1)
    recs = [
        {"ts": ts, "t": "begin", "root": "C:\\x", "plan_id": "p", "ops": 3},
        {"ts": ts, "t": "intent", "op": [0], "src": "a.pdf", "dst": "Documents\\a.pdf"},
        {"ts": ts, "t": "done", "op": {"x": 1}},
        {"ts": ts, "t": "undone", "op": [1, 2]},
        {"ts": ts, "t": "intent", "op": True, "src": "b.pdf", "dst": "Documents\\b.pdf"},
        {"ts": ts, "t": "done", "op": 1},
        {"ts": ts, "t": "intent", "op": 2, "src": "c.pdf", "dst": "Images\\c.pdf"},
        {"ts": ts, "t": "done", "op": 2},
    ]
    _write(runs_dir() / "weird_ops.jsonl", recs)
    s = summary.summarize(7, now=NOW)
    assert s["runs"] == 1
    # Only op 2 has a real int intent + done; True must not alias op 1.
    assert s["files_organized"] == 1
    assert s["by_folder"] == {"Images": 1}
    assert s["undone"] == 0


def test_huge_days_is_clamped_not_overflow():
    _organize_run("r", 400, [("a.pdf", "Documents\\a.pdf", "done")])
    s = summary.summarize(10 ** 7, now=NOW)
    assert s["days"] == summary.MAX_DAYS
    assert s["files_organized"] == 1
    assert summary.summarize(-5, now=NOW)["days"] == 0
    assert "In the last 36500 days" in summary.format_summary(dict(s))


def test_human_bytes_rounding_boundary_and_non_finite():
    assert summary.human_bytes(1024 * 1024 - 1) == "1.0 MB"
    assert summary.human_bytes(1024 ** 3 - 1) == "1.0 GB"
    assert summary.human_bytes(1024 * 1023) == "1023.0 KB"
    assert summary.human_bytes(1024) == "1.0 KB"
    assert summary.human_bytes(float("inf")) == "0 bytes"
    assert summary.human_bytes(float("-inf")) == "0 bytes"
    assert summary.human_bytes(float("nan")) == "0 bytes"
    assert summary.human_bytes(5000 * 1024 ** 4) == "5000.0 TB"
