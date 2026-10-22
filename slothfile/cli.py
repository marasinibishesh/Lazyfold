"""Command line interface.

Organize
    slothfile demo                      create a disposable messy folder to try things on
    slothfile plan  FOLDER [--model M]  scan (read-only) and preview a plan
    slothfile apply PLAN_ID [--recycle] execute a plan after you confirm
    slothfile undo  RUN_ID|--last       put every file from a run back
    slothfile runs                      list past runs
    slothfile recover FOLDER            reconcile an interrupted run
    slothfile ui [FOLDER]               open the local web interface

Be lazier
    slothfile auto FOLDER               plan, move trusted kinds of file, notify about the rest
    slothfile watch FOLDER              tidy new downloads as they settle (Ctrl+C to stop)
    slothfile schedule add FOLDER --at 20:00 | summary | list | remove NAME
    slothfile summary [--days 7] [--notify]
    slothfile learned [--forget KEY | --reset]
    slothfile settings [KEY=VALUE ...]
    slothfile shell-install [--run-alias] | shell-uninstall

Storage cleanup
    slothfile cleanup scan
    slothfile cleanup clean REPORT_ID --categories a,b [--delete]
    slothfile cleanup empty-bin
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

from . import __version__
from .demo import create_demo_folder
from .executor import NeedsRecovery, RunReport
from .llm import DEFAULT_MODEL
from .service import execute, list_runs, load_plan, make_plan, recover, undo_run


def _safe(s: str) -> str:
    """File names and model reasons are untrusted: never send raw control
    characters (ANSI escapes, bidi overrides) to the terminal."""
    return "".join(c if c.isprintable() else f"<U+{ord(c):04X}>" for c in str(s))


def _confirm(prompt: str, word: str) -> bool:
    try:
        return input(f"{prompt} Type {word} to proceed: ").strip() == word
    except EOFError:
        return False


def _print_plan(plan) -> None:
    groups = defaultdict(list)
    keeps, recycles = [], []
    for p in plan.proposals:
        if p.action == "move":
            groups[str(Path(p.dest_rel).parent)].append(p)
        elif p.action == "recycle":
            recycles.append(p)
        else:
            keeps.append(p)
    for folder in sorted(groups):
        print(f"\n  {folder}{os.sep}")
        for p in groups[folder]:
            rename = f"  ->  {p.new_name}" if p.new_name else ""
            flag = "" if p.approved else "  (unticked: review first)"
            print(_safe(f"    [{p.confidence:.2f}] {p.rel_path}{rename}{flag}"))
            print(_safe(f"           because: {'; '.join(p.reasons)}"))
    if recycles:
        print("\n  Suggested for the Recycle Bin (only with apply --recycle and a separate confirmation):")
        for p in recycles:
            print(_safe(f"    {p.rel_path}: {'; '.join(p.reasons)}"))
    if keeps:
        print("\n  Left untouched:")
        for p in keeps:
            print(_safe(f"    {p.rel_path}: {'; '.join(p.reasons)}"))
    s = plan.scan_stats
    moves = sum(1 for p in plan.proposals if p.action == "move")
    print(f"\n  {moves} to organize, {len(recycles)} recycle suggestions, {len(keeps)} left untouched, "
          f"{s.get('folders_untouched', 0)} subfolders ignored.\n  Engine: {s.get('model_status')}"
          + (f"; OCR read {s['ocr_files']} file(s)" if s.get("ocr_files") else ""))


def _print_report(report: RunReport, verb: str) -> None:
    for r in report.results:
        if r.status not in ("moved", "restored", "recycled") or r.detail.startswith(("unpacked", "moved;", "not ")):
            print(_safe(f"  {r.status.upper()}: {r.src} - {r.detail}"))
    print(f"  {verb}: {report.summary()}   (run id {report.run_id})")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="slothfile", description="Preview-first, undoable folder organizer.")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("demo", help="create a disposable messy Downloads folder")
    d.add_argument("target", nargs="?", default=None)

    pl = sub.add_parser("plan", help="scan a folder (read-only) and preview a plan")
    pl.add_argument("folder")
    pl.add_argument("--model", nargs="?", const=DEFAULT_MODEL, default=None,
                    help=f"use a local Ollama model (default {DEFAULT_MODEL})")
    pl.add_argument("--threshold", type=float, default=0.6)
    pl.add_argument("--no-rename", action="store_true")

    a = sub.add_parser("apply", help="execute a saved plan")
    a.add_argument("plan_id")
    a.add_argument("--yes", action="store_true", help="skip the confirmation prompt for moves")
    a.add_argument("--skip", action="append", default=[], help="file name to leave out (repeatable)")
    a.add_argument("--include", action="append", default=[], help="tick an unticked file (repeatable)")
    a.add_argument("--recycle", action="store_true",
                   help="also send suggested old installers to the Recycle Bin (asks separately)")

    u = sub.add_parser("undo", help="undo a run")
    u.add_argument("run_id", nargs="?")
    u.add_argument("--last", action="store_true")

    sub.add_parser("runs", help="list past runs")
    r = sub.add_parser("recover", help="reconcile interrupted runs for a folder")
    r.add_argument("folder")

    ui = sub.add_parser("ui", help="open the local web interface")
    ui.add_argument("folder", nargs="?")
    ui.add_argument("--port", type=int, default=0)
    ui.add_argument("--no-browser", action="store_true")

    au = sub.add_parser("auto", help="plan, move trusted kinds of file, notify about the rest")
    au.add_argument("folder")
    au.add_argument("--plan-only", action="store_true", help="move nothing; just prepare a plan for review")

    w = sub.add_parser("watch", help="tidy new downloads once they have settled")
    w.add_argument("folder")
    w.add_argument("--interval", type=float, default=30)

    sc = sub.add_parser("schedule", help="run SlothFile automatically with Windows Task Scheduler")
    scs = sc.add_subparsers(dest="sched", required=True)
    s_add = scs.add_parser("add", help="tidy FOLDER every day")
    s_add.add_argument("folder")
    s_add.add_argument("--at", default="20:00")
    s_sum = scs.add_parser("summary", help="weekly summary notification")
    s_sum.add_argument("--day", default="SUN")
    s_sum.add_argument("--at", default="18:00")
    scs.add_parser("list")
    s_rm = scs.add_parser("remove")
    s_rm.add_argument("name")

    sm = sub.add_parser("summary", help="what SlothFile did recently")
    sm.add_argument("--days", type=int, default=7)
    sm.add_argument("--notify", action="store_true")

    le = sub.add_parser("learned", help="rules SlothFile learned from your corrections")
    le.add_argument("--forget")
    le.add_argument("--reset", action="store_true")

    st = sub.add_parser("settings", help="show or change settings")
    st.add_argument("pairs", nargs="*", help="KEY=VALUE (JSON values, e.g. ocr=false)")

    si = sub.add_parser("shell-install", help='add "Tidy with SlothFile" to the Explorer right-click menu')
    si.add_argument("--run-alias", action="store_true", help='also let Windows+R open SlothFile by typing "slothfile"')
    sub.add_parser("shell-uninstall", help="remove the right-click menu entry and the Windows+R shortcut")

    cl = sub.add_parser("cleanup", help="find temporary files and caches that can be removed")
    cls = cl.add_subparsers(dest="clean_cmd", required=True)
    cls.add_parser("scan")
    c_clean = cls.add_parser("clean")
    c_clean.add_argument("report_id")
    c_clean.add_argument("--categories", required=True, help="comma-separated category keys from the scan")
    c_clean.add_argument("--delete", action="store_true", help="delete permanently instead of using the Recycle Bin")
    cls.add_parser("empty-bin", help="empty the Recycle Bin (cannot be undone; asks first)")

    args = ap.parse_args(argv)
    try:
        return _dispatch(args)
    except NeedsRecovery as e:
        print(f"Stopped: {e}\nRun: slothfile recover <folder>", file=sys.stderr)
        return 2
    except (ValueError, FileNotFoundError, FileExistsError, RuntimeError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def _dispatch(args) -> int:
    if args.cmd == "demo":
        target = Path(args.target) if args.target else Path(tempfile.mkdtemp(prefix="slothfile-demo-")) / "Downloads"
        expected = create_demo_folder(target)
        print(f"Created a messy demo folder with {len(expected)} items at:\n  {target}\n"
              f"Next: slothfile plan \"{target}\"")
        return 0

    if args.cmd == "plan":
        print(f"Scanning {os.path.abspath(args.folder)} (read-only)...")

        def progress(n, total, name):
            print(f"\r  analysing {n}/{total}", end="", flush=True)

        plan, status = make_plan(args.folder, model=args.model, threshold=args.threshold,
                                 rename=not args.no_rename, progress=progress if args.model else None)
        print()
        _print_plan(plan)
        print(f"\n  Nothing has been changed. To execute:  slothfile apply {plan.plan_id}")
        return 0

    if args.cmd == "apply":
        plan = load_plan(args.plan_id)
        approved = {p.rel_path for p in plan.proposals
                    if p.action == "move" and (p.approved or p.rel_path in args.include)
                    and p.rel_path not in args.skip}
        recycles = [p for p in plan.proposals if p.action == "recycle" and p.rel_path not in args.skip]
        print(f"About to move {len(approved)} files inside {plan.root}. Nothing is deleted or overwritten,"
              " and the run can be undone.")
        if not args.yes and not _confirm("", "YES"):
            print("Cancelled; nothing changed.")
            return 1
        allow_recycle = False
        if args.recycle and recycles:
            print(f"Also send {len(recycles)} old installer(s) to the Recycle Bin? You can restore them from there.")
            for p in recycles:
                print(_safe(f"    {p.rel_path}"))
            allow_recycle = _confirm("", "RECYCLE")
            if allow_recycle:
                approved |= {p.rel_path for p in recycles}
        report = execute(args.plan_id, approved=approved, allow_recycle=allow_recycle)
        _print_report(report, "Done")
        print(f"  To undo: slothfile undo {report.run_id}")
        return 0 if report.count("failed") == 0 else 3

    if args.cmd == "undo":
        run_id = args.run_id
        if args.last or not run_id:
            runs = [r for r in list_runs() if r["moved"] or r.get("recycled")]
            if not runs:
                print("No runs to undo.")
                return 1
            run_id = runs[0]["run_id"]
        _print_report(undo_run(run_id), "Undone")
        return 0

    if args.cmd == "runs":
        for r in list_runs():
            print(f"  {r['run_id']}  {r['started']}  moved {r['moved']:3}  undone {r['undone']:3}  {r['root']}"
                  + ("" if r["complete"] else "  (INTERRUPTED - run recover)"))
        return 0

    if args.cmd == "recover":
        reports = recover(Path(args.folder))
        if not reports:
            print("Nothing to recover.")
        for rep in reports:
            _print_report(rep, "Recovered")
        return 0

    if args.cmd == "ui":
        from .web import serve
        serve(args.folder, port=args.port, open_browser=not args.no_browser)
        return 0

    if args.cmd == "auto":
        from .automation import auto_run
        res = auto_run(args.folder, apply_trusted=not args.plan_only)
        print(json.dumps(res, indent=2))
        return 0 if not res.get("error") else 2

    if args.cmd == "watch":
        from .automation import watch
        print(f"Watching {os.path.abspath(args.folder)}. New files are tidied once they have settled. Ctrl+C stops.")
        try:
            watch(args.folder, interval=args.interval)
        except KeyboardInterrupt:
            pass
        return 0

    if args.cmd == "schedule":
        from . import automation
        if args.sched == "add":
            print("Created task:", automation.schedule_daily(args.folder, at=args.at))
        elif args.sched == "summary":
            print("Created task:", automation.schedule_weekly_summary(day=args.day, at=args.at))
        elif args.sched == "list":
            for t in automation.list_tasks():
                print(_safe(f"  {t.get('name')}  next: {t.get('next_run')}  {t.get('status')}"))
        elif args.sched == "remove":
            automation.remove_task(args.name)
            print("Removed", args.name)
        return 0

    if args.cmd == "summary":
        from .summary import format_summary, summarize
        s = summarize(days=args.days)
        text = format_summary(s)
        print(text)
        if args.notify:
            from .automation import notify
            notify("SlothFile summary", text.splitlines()[0])
        return 0

    if args.cmd == "learned":
        from . import learning
        if args.reset:
            learning.reset_learning()
            print("Forgot everything SlothFile learned.")
        elif args.forget:
            print("Forgotten." if learning.forget_rule(args.forget) else "No such rule.")
        rules = learning.learned_rules()
        if not rules:
            print("No learned rules yet. Correct a plan's folder choice twice and SlothFile will remember.")
        for rule in rules:
            print(_safe(f"  {rule.key:30} -> {rule.outcome:12} (seen {rule.support}x)"))
        return 0

    if args.cmd == "settings":
        from .settings import load_settings, save_settings
        if args.pairs:
            changes = {}
            for pair in args.pairs:
                key, _, raw = pair.partition("=")
                try:
                    changes[key] = json.loads(raw)
                except json.JSONDecodeError:
                    changes[key] = raw
            save_settings(changes)
        print(json.dumps(load_settings(), indent=2))
        return 0

    if args.cmd == "shell-install":
        from . import automation
        automation.install_context_menu()
        print('Added "Tidy with SlothFile" to the right-click menu of folders.')
        if args.run_alias:
            automation.install_run_alias()
            print('Press Windows+R and type "slothfile" to open SlothFile.')
        return 0

    if args.cmd == "shell-uninstall":
        from . import automation
        automation.uninstall_context_menu()
        automation.uninstall_run_alias()
        print("Removed the right-click menu entry and the Windows+R shortcut.")
        return 0

    if args.cmd == "cleanup":
        return _cleanup(args)
    return 1


def _cleanup(args) -> int:
    from . import cleanup
    from .summary import human_bytes
    if args.clean_cmd == "scan":
        rep = cleanup.scan_cleanup()
        d = rep.to_dict()
        print(f"Storage cleanup scan {d['report_id']} (nothing was changed):")
        for c in d["categories"]:
            state = f"BLOCKED: {c['blocked_reason']}" if c.get("blocked_reason") else (
                "suggested" if c.get("selected_by_default") else "optional")
            print(_safe(f"  {c['key']:16} {human_bytes(c['total_bytes']):>10}  {c['count']:6} files  [{state}]  {c['label']}"))
            print(_safe(f"  {'':16} {c['why']}"))
        print(f"  Protected (never cleaned): {d['protected']['count']} files.  Recycle Bin: "
              f"{human_bytes(d['recycle_bin']['bytes'])} in {d['recycle_bin']['items']} items.")
        print(f"\nTo clean: slothfile cleanup clean {d['report_id']} --categories "
              + ",".join(c["key"] for c in d["categories"] if c.get("selected_by_default") and not c.get("blocked_reason")))
        return 0
    if args.clean_cmd == "clean":
        keys = [k.strip() for k in args.categories.split(",") if k.strip()]
        if args.delete:
            if not _confirm("Delete these files PERMANENTLY? This cannot be undone.", "DELETE"):
                print("Cancelled; nothing changed.")
                return 1
            res = cleanup.clean(args.report_id, keys, mode="delete", confirm_permanent=True)
        else:
            if not _confirm("Move the selected files to the Recycle Bin?", "YES"):
                print("Cancelled; nothing changed.")
                return 1
            res = cleanup.clean(args.report_id, keys, mode="recycle")
        d = res.to_dict()
        where = "deleted" if args.delete else "moved to the Recycle Bin (space is freed when you empty it)"
        print(f"  {d['removed_count']} files, {human_bytes(d['removed_bytes'])} {where}.")
        print(f"  Skipped {len(d['skipped'])}, errors {len(d['errors'])}. Free space now "
              f"{human_bytes(d['disk_free_after'])} (was {human_bytes(d['disk_free_before'])}).")
        for item in (d["skipped"] + d["errors"])[:15]:
            print(_safe(f"    {item['reason']}: {item['path']}"))
        return 0
    if args.clean_cmd == "empty-bin":
        from . import winshell
        size, items = winshell.recycle_bin_info()
        if not _confirm(f"Empty the Recycle Bin ({human_bytes(size)}, {items} items)? This cannot be undone.", "EMPTY"):
            print("Cancelled; nothing changed.")
            return 1
        res = cleanup.empty_recycle_bin_confirmed(True)
        print(f"  Recycle Bin emptied: freed {human_bytes(res['bytes_before'] - res['bytes_after'])}.")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
