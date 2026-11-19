from __future__ import annotations

import collections
from dataclasses import dataclass, field
from pathlib import Path

from . import builds as bld
from . import checks
from .dataset import Day, discover, load_all, load_headers
from .normalize import template


@dataclass
class TemplateStat:
    src: str
    level: str
    template: str
    count: int = 0
    days: set = field(default_factory=set)  # (person, day)
    builds: collections.Counter = field(default_factory=collections.Counter)  # tandemkit -> count
    example: tuple | None = None  # (person, day, src, lineno)
    sample: str = ""  # one raw message, for matching against known-issue signatures


@dataclass
class KnownHit:
    issue: str
    what: str  # "signature" | "marker"
    person: str
    day: str
    ts: object
    src: str
    lineno: int
    tandemkit: str | None


@dataclass
class Result:
    days: list[Day]
    findings: list[checks.Finding]
    templates: dict[tuple, TemplateStat]
    known_hits: list[KnownHit]

    def day(self, person: str, day: str) -> Day | None:
        return next((d for d in self.days if d.person == person and d.day == day), None)


def build_of(d: Day, ts) -> bld.Build | None:
    return bld.build_at(d, ts)


def run(root: Path | None, people: list[str] | None = None, since: str | None = None,
        until: str | None = None, only: list[str] | None = None,
        loaded: list[Day] | None = None, headers: dict | None = None) -> Result:
    """Analyze the organized tree at ``root``, or already-loaded days (``loaded``, e.g. from
    ``dataset.from_export``) with their export ``headers`` (person -> [Header])."""
    def in_range(d: Day) -> bool:
        return (not since or d.day >= since) and (not until or d.day <= until)

    if loaded is not None:
        days = sorted((d for d in loaded if in_range(d)), key=lambda d: (d.person, d.day))
    else:
        days = sorted(load_all([d for d in discover(root, people) if in_range(d)]),
                      key=lambda d: (d.person, d.day))
        headers = load_headers(root, people)
    bld.assign(days, headers)

    findings: list[checks.Finding] = []
    families = {o.split(".")[0] for o in only or ()}
    registered = {n.split(".")[0] for n in [*checks.CHECKS, *checks.PERSON_CHECKS]}
    # findings like log_line_lost come from several checks: asking for one runs them all
    run_all = not only or bool(families - registered - {"none"})

    def selected(name: str) -> bool:
        return run_all or name.split(".")[0] in families

    for d in days:
        for name, (_, fn) in checks.CHECKS.items():
            if selected(name):
                findings += fn(d)
    by_person = collections.defaultdict(list)
    for d in days:
        by_person[d.person].append(d)
    for name, (_, fn) in checks.PERSON_CHECKS.items():
        if selected(name):
            for pdays in by_person.values():
                findings += fn(pdays)
    findings = [f for f in findings if not only or any(f.check == o or f.check.startswith(o + ".") for o in only)]

    templates: dict[tuple, TemplateStat] = {}
    hits: list[KnownHit] = []
    for d in days:
        for src in ("trio", "tandem"):
            for e in getattr(d, src):
                if e.kind == "known":
                    b = bld.build_at(d, e.ts)
                    hits.append(KnownHit(e.data["issue"], e.data["what"], d.person, d.day, e.ts, src,
                                         e.lineno, b.tandemkit if b else None))
                    continue
                if e.kind != "problem":
                    continue
                key = (src, e.data["level"], template(e.data["msg"]))
                t = templates.get(key) or templates.setdefault(key, TemplateStat(*key))
                t.count += 1
                t.days.add((d.person, d.day))
                b = bld.build_at(d, e.ts)
                t.builds[b.tandemkit if b else None] += 1
                if t.example is None:
                    t.example = (d.person, d.day, src, e.lineno)
                    t.sample = e.data["msg"]
    return Result(days, findings, templates, hits)
