"""Trio's log export ("Trio Logs" files, Trio 1.0.2+): header metadata, per-day slices, merging.

An export is a header (``== App ==``, ``== Phone ==``, ``== Pump ==``, ``== CGM ==``, ``== Settings ==``,
``== Alerts ==``) followed by ``== Log ==`` and every log source merged in time order, one line per
entry with continuation lines indented by four spaces (see ``parse.parse_export``). Settings ▸
Share logs makes three variants: "Trio Logs" (everything), "Trio Pump Logs" and "Trio CGM Logs"
(one device's lines only). Each covers the last N days up to its creation, so a tester's
successive exports overlap.

Two exports of the same moment don't agree byte for byte, so versions are merged per entry:

* redaction placeholders are numbered per export in order of first appearance
  (``<pump-serial-1>`` in one may be ``<pump-serial-2>`` in the next); keys normalize them;
* device trace lines come from the exporting process only, so a later export lacks the traces
  of earlier app runs;
* a pump- or CGM-only export holds a subset of a full one.

An entry's key is its instant (UTC milliseconds) plus its text with placeholders normalized.
Merging is a multiset union (identical lines in the same millisecond are real repeats), in time
order. Versions that overlap in time yet share few entries are not the same phone's logs and
are reported as a conflict instead.
"""

from __future__ import annotations

import collections
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .builds import Build

TITLES = ("Trio Logs", "Trio Pump Logs", "Trio CGM Logs")
LOG_HEADER = "== Log =="
DAY_FILE_TITLE = "Trio Logs"  # an organized day file is itself an export of that one day

# Overlap check between two versions of one day: with at least this many entries in their common
# time range, the smaller side must find this fraction of them in the other.
MIN_OVERLAP_ENTRIES = 20
MIN_SHARED_FRACTION = 0.5

_ENTRY = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2}) (?P<time>\d{2}:\d{2}:\d{2}(?:\.\d+)?) (?P<tz>[+-]\d{4}) ")
_PLACEHOLDER = re.compile(r"<([a-z]+(?:-[a-z]+)*)-\d+>")
_CREATED = re.compile(r"^Created: (?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (?P<tz>[+-]\d{4})$", re.MULTILINE)
_COVERS = re.compile(r"^Covers: last (?:(?P<days>\d+) days|24 hours)$", re.MULTILINE)
_APP_TRIO = re.compile(r"^Trio: (?P<version>\S+) \(\S*\)(?:, dev version (?P<dev>\S+))?$", re.MULTILINE)
_APP_BRANCH = re.compile(r"^Branch: (?P<v>\S+)$", re.MULTILINE)
_APP_COMMIT = re.compile(r"^Commit: (?P<v>[0-9a-f]+)$", re.MULTILINE)
_APP_TANDEMKIT = re.compile(r"^TandemKit: \S+ (?P<v>[0-9a-f]+)$", re.MULTILINE)


def is_export_text(first_line: str) -> bool:
    return first_line.rstrip("\r\n") in TITLES


def is_export_bytes(data: bytes) -> bool:
    return is_export_text(data[:64].split(b"\n", 1)[0].decode("utf-8", "replace"))


@dataclass
class Header:
    title: str
    text: str  # everything before "== Log =="
    created: datetime | None = None  # local wall clock, like the log lines
    covers: timedelta | None = None
    build: Build | None = None

    @property
    def window_start(self) -> datetime | None:
        return self.created - self.covers if self.created and self.covers else None

    @property
    def file_name(self) -> str:
        """Name for this header in the organized tree: creation time + export variant."""
        slug = self.title.lower().replace(" ", "_")
        return f"{self.created:%Y-%m-%d_%H%M%S}_{slug}.txt" if self.created else f"{slug}.txt"


def parse_header(text: str) -> Header:
    title = text.split("\n", 1)[0].rstrip("\r")
    h = Header(title, text)
    if m := _CREATED.search(text):
        h.created = datetime.fromisoformat(m["ts"])
    if m := _COVERS.search(text):
        h.covers = timedelta(days=int(m["days"])) if m["days"] else timedelta(hours=24)
    app = text.split("\n== Phone ==", 1)[0]  # submodule lines live in the App section
    trio, branch, commit = _APP_TRIO.search(app), _APP_BRANCH.search(app), _APP_COMMIT.search(app)
    if trio and branch and commit:
        version = trio["dev"] if trio["dev"] and trio["dev"] != "unknown" else trio["version"]
        tk = _APP_TANDEMKIT.search(app)
        h.build = Build(version, branch["v"], commit["v"], tk["v"] if tk else None)
    return h


def read_header(path) -> Header:
    lines = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.rstrip("\r\n") == LOG_HEADER:
                break
            lines.append(line)
    return parse_header("".join(lines))


@dataclass(slots=True)
class Entry:
    ms: int  # UTC milliseconds; -1 for continuation text before the first entry
    day: str  # local date as rendered in the export
    lines: list[str] = field(default_factory=list)  # first line + continuations, verbatim

    @property
    def key(self) -> tuple[int, str]:
        return self.ms, _PLACEHOLDER.sub(r"<\1>", "\n".join(self.lines))


def _instant_ms(m: re.Match) -> int:
    tz = m["tz"]
    return round(datetime.fromisoformat(f"{m['date']} {m['time']}{tz[:3]}:{tz[3:]}").timestamp() * 1000)


def split(text: str) -> tuple[Header, list[Entry]]:
    """An export's header and its log entries in file order."""
    head, sep, log = text.partition(f"\n{LOG_HEADER}\n")
    if not sep:
        raise ValueError(f"no '{LOG_HEADER}' section")
    entries: list[Entry] = []
    for line in log.split("\n"):
        if m := _ENTRY.match(line):
            entries.append(Entry(_instant_ms(m), m["date"], [line]))
        elif entries:
            entries[-1].lines.append(line)
        elif line:
            entries.append(Entry(-1, "", [line]))
    if entries and entries[-1].lines[-1] == "":  # the file's final newline
        entries[-1].lines.pop()
    return parse_header(head + "\n"), entries


def by_day(entries: list[Entry]) -> dict[str, list[Entry]]:
    out: dict[str, list[Entry]] = collections.defaultdict(list)
    day = None
    for e in entries:
        day = e.day or day  # leading orphan text goes with the first day
        if day:
            out[day].append(e)
    return dict(out)


def day_file(day: str, entries: list[Entry]) -> bytes:
    body = "".join(line + "\n" for e in entries for line in e.lines)
    return f"{DAY_FILE_TITLE}\nDay: {day}\n\n{LOG_HEADER}\n{body}".encode("utf-8", "surrogateescape")


def decode(data: bytes) -> str:
    return data.decode("utf-8", "surrogateescape")


def _shared(a: list[Entry], b: list[Entry]) -> tuple[int, int]:
    """(entries both share, entries on the smaller side) within the time range both cover."""
    ta = [e.ms for e in a if e.ms >= 0]
    tb = [e.ms for e in b if e.ms >= 0]
    if not ta or not tb:
        return 0, 0
    lo, hi = max(min(ta), min(tb)), min(max(ta), max(tb))
    if lo > hi:
        return 0, 0
    ka = collections.Counter(e.key for e in a if lo <= e.ms <= hi)
    kb = collections.Counter(e.key for e in b if lo <= e.ms <= hi)
    return sum((ka & kb).values()), min(sum(ka.values()), sum(kb.values()))


@dataclass
class Merged:
    entries: list[Entry]
    added: dict[str, int]  # origin -> entries it contributed
    conflict: str | None = None


def merge(versions: list[tuple[list[Entry], str]]) -> Merged:
    """Multiset union of one day's versions; the first version is the base (put the existing
    file first so its lines, and their placeholder numbers, stay as they are)."""
    (base, base_origin), rest = versions[0], versions[1:]
    out = list(base)
    have = collections.Counter(e.key for e in out)
    added = {base_origin: len(base)}
    for entries, origin in rest:
        common, smaller = _shared(out, entries)
        if smaller >= MIN_OVERLAP_ENTRIES and common < MIN_SHARED_FRACTION * smaller:
            return Merged(out, added, f"{origin} overlaps {base_origin} in time but shares only "
                                      f"{common} of {smaller} entries there (another phone or person?)")
        seen: collections.Counter = collections.Counter()
        n = 0
        for e in entries:
            seen[e.key] += 1
            if seen[e.key] > have[e.key]:
                out.append(e)
                have[e.key] += 1
                n += 1
        added[origin] = n
    out.sort(key=lambda e: e.ms)  # stable: at equal instants the base's lines come first
    return Merged(out, added)
