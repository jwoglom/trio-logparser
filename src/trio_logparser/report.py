"""Terminal reports over an analysis run."""

from __future__ import annotations

import argparse
import collections
import shlex
import sys
from pathlib import Path

from . import analysis, checks, known
from .builds import Repo, build_at
from .dataset import alias, discover, from_export
from .export import TITLES
from .normalize import mask
from .parse import is_export

SEV_ORDER = {"high": 0, "warn": 1, "info": 2}
RECENT_DAYS = 7  # builds committed within this many days of the newest one count as "recent"


EXPORT_PERSON = "export"


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--dest", help="organized log root (default: $TRIO_LOGS_DIR)")
    p.add_argument("--file", help="analyze one Trio log export file instead of the organized tree")
    p.add_argument("--person", action="append",
                   help="limit to a person (name or alias); repeatable. With --file: label for its person")
    p.add_argument("--since", help="first day, YYYY-MM-DD")
    p.add_argument("--until", help="last day, YYYY-MM-DD")
    p.add_argument("--tandemkit-repo", help="TandemKit checkout (default: $TANDEMKIT_REPO)")
    p.add_argument("--names", action="store_true", help="show full tester names instead of aliases")


class Ctx:
    """An analysis run plus the build bookkeeping every report needs."""

    def __init__(self, args, only=None):
        from .cli import logs_root

        self.args = args
        if args.file:
            path = Path(args.file).expanduser()
            if not is_export(path):
                sys.exit(f"error: {path} is not a Trio log export (first line must be one of {', '.join(TITLES)})")
            person = args.person[0] if args.person else EXPORT_PERSON
            days, header = from_export(path, person)
            self.res = analysis.run(None, since=args.since, until=args.until, only=only,
                                    loaded=days, headers={person: [header]})
        else:
            self.res = analysis.run(logs_root(args), args.person, args.since, args.until, only)
        self.repo = Repo(args.tandemkit_repo)
        tks = {b.tandemkit for d in self.res.days for _, b in d.builds if b.tandemkit}
        self.newest = max(tks, key=self.repo.time, default=None)
        cutoff = self.repo.time(self.newest) - RECENT_DAYS * 86400
        self.recent = {t for t in tks if self.repo.time(t) >= cutoff and self.repo.time(t)}

    def who(self, person: str) -> str:
        return person if self.args.names else alias(person)

    def label(self, sha: str | None) -> str:
        if not sha:
            return "?"
        return f"{sha[:7]} {self.repo.date(sha) or '????-??-??'}"

    def tk(self, f: checks.Finding) -> str | None:
        d = self.res.day(f.person, f.day)
        b = build_at(d, f.ts) if d else None
        return b.tandemkit if b else None

    def latest(self, shas) -> str | None:
        return max((s for s in shas if s), key=self.repo.time, default=None)

    def issue_status(self, check: str, sha: str | None) -> tuple[known.Issue | None, str]:
        issue = known.for_check(check)
        return issue, (known.status(self.repo, issue, sha) if issue else "")


def _issue_tag(issue: known.Issue | None) -> str:
    if not issue:
        return ""
    if issue.pr:
        return f"#{issue.pr}"
    return f"issue #{issue.issues[0]}" if issue.issues else issue.id


# ----------------------------------------------------------------------------- scan

def cmd_scan(args) -> int:
    c = Ctx(args, args.check)
    res = c.res
    if not res.days:
        print("no log days found")
        return 0
    both = sum(1 for d in res.days if d.has_both)
    exports = sum(1 for d in res.days if d.export_path)
    print(f"{len({d.person for d in res.days})} people, {len(res.days)} days ({both} with both logs, "
          f"{exports} from exports), {min(d.day for d in res.days)} → {max(d.day for d in res.days)}")
    if shadowed := [d for d in res.days if d.ignored]:
        print(f"{len(shadowed)} day(s) also have old-format logs; analyzed the export only: "
              + ", ".join(f"{c.who(d.person)} {d.day}" for d in shadowed[:5]) + (" …" if len(shadowed) > 5 else ""))
    print(f"newest TandemKit build: {c.label(c.newest)}; 'recent' = builds within {RECENT_DAYS} days of it "
          f"({len(c.recent)})\n")

    groups = collections.defaultdict(list)
    for f in res.findings:
        groups[f.check].append(f)
    print(f"{'check':30} {'n':>5} {'high':>4} {'warn':>4} {'ppl':>3} {'days':>4}  "
          f"{'last seen in TK build':20} {'recent':>6}  known issue")
    for name, fs in sorted(groups.items(), key=lambda kv: (min(SEV_ORDER[f.severity] for f in kv[1]), kv[0])):
        sev = collections.Counter(f.severity for f in fs)
        shas = [c.tk(f) for f in fs]
        statuses = collections.Counter(c.issue_status(name, s)[1] for s in shas)
        issue = known.for_check(name)
        tag = (f"{_issue_tag(issue)}: " + ", ".join(f"{k} {v}" for k, v in statuses.most_common())) if issue else ""
        print(f"{name:30} {len(fs):5} {sev['high']:4} {sev['warn']:4} {len({f.person for f in fs}):3} "
              f"{len({(f.person, f.day) for f in fs}):4}  {c.label(c.latest(shas)):20} "
              f"{sum(1 for s in shas if s in c.recent):6}  {tag}")
    return 0


# ----------------------------------------------------------------------------- findings

def cmd_findings(args) -> int:
    c = Ctx(args, args.check)
    fs = c.res.findings
    if args.severity:
        fs = [f for f in fs if SEV_ORDER[f.severity] <= SEV_ORDER[args.severity]]
    if args.build:
        fs = [f for f in fs if (c.tk(f) or "").startswith(args.build)]
    if args.recent:
        fs = [f for f in fs if c.tk(f) in c.recent]
    if args.unresolved:
        fs = [f for f in fs if c.issue_status(f.check, c.tk(f))[1] not in ("fixed later",)]
    fs.sort(key=lambda f: (f.person, f.ts))
    shown = fs if args.all else fs[: args.limit]
    for f in shown:
        sha = c.tk(f)
        issue, st = c.issue_status(f.check, sha)
        tag = f" [{_issue_tag(issue)} {st}]" if issue else ""
        print(f"{f.severity:4} {f.check:28} {c.who(f.person):5} {f.ts:%Y-%m-%d %H:%M:%S}  "
              f"TK {(sha or '?')[:7]}  {mask(f.summary)}{tag}")
        if args.refs:
            d = c.res.day(f.person, f.day)
            path = d.path(f.src) if d else None
            if path and c.args.file:
                print(f"     trio-logs show --file {shlex.quote(c.args.file)} {f.lineno}")
            elif path:
                print(f"     trio-logs show {c.who(f.person)} {f.day} {f.src} {f.lineno}")
    if len(shown) < len(fs):
        print(f"... {len(fs) - len(shown)} more (--all or --limit)")
    print(f"{len(fs)} findings")
    return 0


# ----------------------------------------------------------------------------- issues

def cmd_issues(args) -> int:
    c = Ctx(args, ["none"])
    by_issue_person = collections.defaultdict(list)
    for h in sorted(c.res.known_hits, key=lambda h: (h.person, h.ts, h.lineno)):
        by_issue_person[(h.issue, h.person)].append(h)
    print(f"{'issue':40} {'PR':>4} {'fix':7} {'sig':4} {'pre-fix':>7} {'post-fix':>8} {'marker':>6}  "
          "post-fix occurrences (person day ×n)")
    for issue in known.load():
        occ = [ep for (iid, _), hs in by_issue_person.items() if iid == issue.id
               for ep in known.episodes(issue, hs)]
        markers = sum(1 for (iid, _), hs in by_issue_person.items() if iid == issue.id
                      for h in hs if h.what == "marker")
        if not occ and not markers and not args.all:
            continue
        post = [ep for ep in occ if known.status(c.repo, issue, ep[0].tandemkit) == "regression?"]
        pre = len(occ) - len(post)
        days = collections.Counter((c.who(ep[0].person), ep[0].day) for ep in post)
        definitive = issue.strength == "definitive"
        flag = "  <-- REGRESSION?" if post and definitive else ""
        where = ", ".join(f"{p} {d} ×{n}" for (p, d), n in sorted(days.items())[:3])
        print(f"{issue.id[:40]:40} {issue.pr or '':>4} {(issue.fix_sha or 'unfixed')[:7]:7} "
              f"{'def' if definitive else 'sug':4} {pre:7} {len(post):8} {markers:6}  "
              f"{where}{' …' if len(days) > 3 else ''}{flag}")
    print("\nsig: def = the fix removed this log text, so post-fix hits mean old code ran; "
          "sug = fixed code can still log it, post-fix hits are leads only.\n"
          "marker: lines the fix introduced (evidence the fixed path runs).")
    if not args.all:
        print("issues with no hits hidden; --all to list every registry entry")
    return 0


# ----------------------------------------------------------------------------- templates

def cmd_templates(args) -> int:
    c = Ctx(args, ["none"])
    ts = [t for t in c.res.templates.values()
          if (not args.src or t.src == args.src) and (not args.level or t.level == args.level)
          and t.count >= args.min_count]
    if args.recent:
        ts = [t for t in ts if any(s in c.recent for s in t.builds)]
    ts.sort(key=lambda t: -t.count)
    print(f"{'count':>6} {'ppl':>3} {'days':>4} {'last TK build':18} {'recent':>6}  {'known':12} template")
    for t in ts[: args.limit]:
        recent = sum(n for s, n in t.builds.items() if s in c.recent)
        issues = {i for i, _ in known.scan(t.src, t.sample)}
        tag = ",".join(_issue_tag(known.by_id()[i]) for i in sorted(issues))
        print(f"{t.count:6} {len({p for p, _ in t.days}):3} {len(t.days):4} {c.label(c.latest(t.builds)):18} "
              f"{recent:6}  {tag:12} {t.src[:2]} {t.level[:4]:4} {mask(t.template)[:140]}")
    return 0


# ----------------------------------------------------------------------------- builds

def cmd_builds(args) -> int:
    c = Ctx(args, ["none"])
    spans: dict[tuple, list] = {}
    for d in c.res.days:
        for _, b in d.builds:
            spans.setdefault((d.person, b), [d.day, d.day])[1] = d.day
    for (person, b), (a, z) in sorted(spans.items(), key=lambda kv: (kv[0][0], kv[1][0])):
        print(f"{c.who(person):5} {a} → {z}  TK {c.label(b.tandemkit):18} {b.branch} {b.trio_commit} v{b.version}")
    return 0


# ----------------------------------------------------------------------------- show

def cmd_show(args) -> int:
    from .cli import logs_root

    if args.file:
        if len(args.ref) != 1:
            sys.exit("with --file, give only the line number")
        path, line = Path(args.file).expanduser(), int(args.ref[0])
    else:
        if len(args.ref) != 4:
            sys.exit("usage: trio-logs show <person> <day> <trio|tandem> <line>  (or --file <export> <line>)")
        person, day_, src, line = args.ref[0], args.ref[1], args.ref[2], int(args.ref[3])
        if src not in ("trio", "tandem"):
            sys.exit("src must be trio or tandem")
        day = next((d for d in discover(logs_root(args), [person]) if d.day == day_), None)
        path = day and day.path(src)
        if not path:
            sys.exit(f"no {src} log for {person} on {day_}")
    args.line = line
    lo, hi = args.line - args.context, args.line + args.context
    with open(path, encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh, 1):
            if i > hi:
                break
            if i >= lo:
                print(f"{'>' if i == args.line else ' '}{i:7} {mask(line.rstrip(chr(10)))[: args.width]}")
    return 0


def add_commands(sub) -> None:
    names = ", ".join(sorted([*checks.CHECKS, *checks.PERSON_CHECKS, "log_line_lost"]))

    p = sub.add_parser("scan", help="summary of findings per check, by build and known issue")
    _common(p)
    p.add_argument("--check", action="append", help=f"limit to check(s): {names}")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("findings", help="list findings")
    _common(p)
    p.add_argument("--check", action="append", help="check or check.subtype; repeatable")
    p.add_argument("--severity", choices=SEV_ORDER, help="minimum severity")
    p.add_argument("--build", help="TandemKit commit prefix")
    p.add_argument("--recent", action="store_true", help="only findings on recent TandemKit builds")
    p.add_argument("--unresolved", action="store_true", help="hide findings whose fix landed later")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--all", action="store_true")
    p.add_argument("--refs", action="store_true", help="print a `trio-logs show` command per finding")
    p.set_defaults(func=cmd_findings)

    p = sub.add_parser("issues", help="known TandemKit fixes: symptom before vs after each fix")
    _common(p)
    p.add_argument("--all", action="store_true")
    p.set_defaults(func=cmd_issues)

    p = sub.add_parser("templates", help="warning/error message templates, by build")
    _common(p)
    p.add_argument("--src", choices=["trio", "tandem"])
    p.add_argument("--level", help="WARN, WARNING, ERROR")
    p.add_argument("--min-count", type=int, default=1)
    p.add_argument("--recent", action="store_true", help="only templates seen on recent builds")
    p.add_argument("--limit", type=int, default=60)
    p.set_defaults(func=cmd_templates)

    p = sub.add_parser("builds", help="which build each person ran when")
    _common(p)
    p.set_defaults(func=cmd_builds)

    p = sub.add_parser("show", help="print masked log lines around a finding")
    p.add_argument("ref", nargs="+", metavar="REF",
                   help="<person> <day> <trio|tandem> <line>, or just <line> with --file")
    p.add_argument("-C", "--context", type=int, default=8)
    p.add_argument("--width", type=int, default=240)
    p.add_argument("--dest")
    p.add_argument("--file", help="a Trio log export file (findings run with --file point into it)")
    p.set_defaults(func=cmd_show)
