from __future__ import annotations

import argparse
import collections
import os
import sys
from pathlib import Path

from . import historylog, organize, report


def logs_root(args: argparse.Namespace) -> Path:
    dest = args.dest or os.environ.get("TRIO_LOGS_DIR")
    if not dest:
        sys.exit("error: pass --dest or set TRIO_LOGS_DIR")
    return Path(dest).expanduser()


def cmd_organize(args: argparse.Namespace) -> int:
    dest = logs_root(args)
    sources: list[organize.Source] = []
    for src in args.sources:
        sources += organize.collect(Path(src).expanduser(), person=args.person)
    sources, unresolved = organize.resolve_unknown_people(sources)
    if unresolved:
        print("Can't tell whose these are (no ' - <Person>' suffix); re-run with --person:")
        for s in unresolved:
            print(f"  {s.origin}")
        return 2

    actions = organize.plan(sources, dest)
    for a in actions:
        if a.status == "SAME" and not args.verbose:
            continue
        print(f"{a.status:8} {a.dest.relative_to(dest) if a.dest.is_relative_to(dest) else a.dest}")
        if args.verbose or a.status in {"CONFLICT", "ERROR", "MERGE"}:
            for n in a.notes:
                print(f"           {n}")
    counts = collections.Counter(a.status for a in actions)
    print(", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))

    if not args.apply:
        print("dry run; pass --apply to write")
    else:
        print(f"wrote {organize.apply(actions)} files to {dest}")
    return 1 if counts["CONFLICT"] or counts["ERROR"] else 0


def cmd_history_jsonl(args: argparse.Namespace) -> int:
    repo = args.tandemkit_repo or os.environ.get("TANDEMKIT_REPO")
    if not repo:
        sys.exit("error: pass --tandemkit-repo or set TANDEMKIT_REPO (the decoder is built from TandemKit)")
    try:
        exporter = historylog.build_exporter(Path(repo).expanduser(), args.tandemkit_rev,
                                             log=lambda m: print(m, file=sys.stderr))
        if args.output == "-":
            packets, logs = historylog.export(Path(args.log).expanduser(), sys.stdout, exporter)
        else:
            with open(Path(args.output).expanduser(), "w") as out:
                packets, logs = historylog.export(Path(args.log).expanduser(), out, exporter)
    except historylog.ExportError as e:
        sys.exit(f"error: {e}")
    print(f"{packets} stream packets, {logs} history logs -> {args.output}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="trio-logs")
    sub = p.add_subparsers(dest="cmd", required=True)

    o = sub.add_parser("organize", help="sort uploads into <dest>/<Person>/YYYY-MM-DD_<kind>.txt")
    o.add_argument("sources", nargs="+", help="upload folders, zips, or files (never modified)")
    o.add_argument("--dest", help="organized log root (default: $TRIO_LOGS_DIR)")
    o.add_argument("--person", help="person for files without a ' - <Person>' suffix")
    o.add_argument("--apply", action="store_true", help="write files (default is a dry run)")
    o.add_argument("-v", "--verbose", action="store_true")
    o.set_defaults(func=cmd_organize)

    h = sub.add_parser("history-jsonl",
                       help="export the pump history logs in a tandem_log as TandemKit/pumpX2 JSONL")
    h.add_argument("log", help="a tandem_log file")
    h.add_argument("-o", "--output", default="-", help="output .jsonl (default: stdout)")
    h.add_argument("--tandemkit-repo", help="TandemKit checkout (default: $TANDEMKIT_REPO)")
    h.add_argument("--tandemkit-rev", default="HEAD",
                   help="TandemKit revision whose decoders to use (default: HEAD; never checked out)")
    h.set_defaults(func=cmd_history_jsonl)

    report.add_commands(sub)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
