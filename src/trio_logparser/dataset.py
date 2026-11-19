"""The organized log tree: people, days, and their parsed events.

Per person, a day is either an export day file (``YYYY-MM-DD_trio_logs.txt``, both sides in one
file) or the older pair ``YYYY-MM-DD_log.txt`` / ``YYYY-MM-DD_tandem_log.txt``. When a day has
both, the export is analyzed and the old files are listed in ``Day.ignored``. Export headers
(``<Person>/headers/*.txt``) give the build running at each export, for builds.assign.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import events as ev
from .export import Header, read_header
from .parse import Record, parse, parse_export

_DAY_FILE = re.compile(r"^(?P<day>\d{4}-\d{2}-\d{2})_(?P<kind>tandem_log|log|trio_logs)\.txt$")
HEADERS_DIR = "headers"


def minute(ts) -> int:
    """Wall-clock minute index; the logs' own local time, no timezone conversion."""
    return ts.toordinal() * 1440 + ts.hour * 60 + ts.minute


def alias(name: str) -> str:
    """Short label for a tester, so reports don't carry full names by default."""
    words = re.findall(r"[A-Za-z]+", name)
    if len(words) > 1:
        return ".".join(w[0].upper() for w in words) + "."
    return (words[0][:2].upper() + ".") if words else name


@dataclass
class Day:
    person: str
    day: str
    trio_path: Path | None = None
    tandem_path: Path | None = None
    export_path: Path | None = None  # an export holding both sides; wins over the two above
    trio: list[ev.Event] = field(default_factory=list)
    tandem: list[ev.Event] = field(default_factory=list)
    # minutes (epoch-minute ints) in which each log has any line, to tell "event missing"
    # from "log not being written"
    trio_minutes: set[int] = field(default_factory=set)
    tandem_minutes: set[int] = field(default_factory=set)
    builds: list = field(default_factory=list)  # [(start ts, Build)], filled by builds.assign
    ignored: list[Path] = field(default_factory=list)  # old-format files shadowed by the export

    def path(self, src: str) -> Path | None:
        if self.export_path:
            return self.export_path
        return self.trio_path if src == "trio" else self.tandem_path

    @property
    def has_both(self) -> bool:
        return bool(self.trio_minutes and self.tandem_minutes)

    def covered(self, src: str, ts, slack_min: int = 2) -> bool:
        """Whether ``src``'s log has lines within ``slack_min`` minutes of ``ts``."""
        minutes = getattr(self, f"{src}_minutes")
        m = minute(ts)
        return any(m + d in minutes for d in range(-slack_min, slack_min + 1))

    def _set(self, src: str, records: Iterable[Record]) -> None:
        minutes: set[int] = getattr(self, f"{src}_minutes")

        def tracked():
            for r in records:
                minutes.add(minute(r.ts))
                yield r
        setattr(self, src, list(ev.extract(tracked())))

    def load(self) -> Day:
        if self.export_path:
            split: dict[str, list[Record]] = {"trio": [], "tandem": []}
            for r in parse_export(self.export_path):
                split[r.src].append(r)
            for src, records in split.items():
                self._set(src, records)
            return self
        for src in ("trio", "tandem"):
            if path := self.path(src):
                self._set(src, parse(path))
        return self


def from_export(path: Path, person: str,
                records: Iterable[Record] | None = None) -> tuple[list[Day], Header]:
    """Days of one export file (any number of days), split by local date, plus its header.
    Every day points at ``path``, so line numbers are the file's own. ``records``: the file's
    already-parsed records, if the caller has them."""
    path = Path(path)
    by_day: dict[str, dict[str, list[Record]]] = {}
    for r in parse_export(path) if records is None else records:
        by_day.setdefault(r.ts.date().isoformat(), {"trio": [], "tandem": []})[r.src].append(r)
    days = []
    for key in sorted(by_day):
        d = Day(person, key, export_path=path)
        for src, rs in by_day[key].items():
            d._set(src, rs)
        days.append(d)
    return days, read_header(path)


def _people(root: Path, people: list[str] | None) -> Iterator[Path]:
    for pdir in sorted(Path(root).iterdir()):
        if not pdir.is_dir() or pdir.name.startswith((".", "Upload")):
            continue
        if people and pdir.name not in people and alias(pdir.name) not in people:
            continue
        yield pdir


def discover(root: Path, people: list[str] | None = None) -> list[Day]:
    days: dict[tuple[str, str], Day] = {}
    for pdir in _people(root, people):
        for f in pdir.iterdir():
            if m := _DAY_FILE.match(f.name):
                d = days.setdefault((pdir.name, m["day"]), Day(pdir.name, m["day"]))
                setattr(d, {"log": "trio_path", "tandem_log": "tandem_path", "trio_logs": "export_path"}[m["kind"]], f)
    for d in days.values():
        if d.export_path:
            d.ignored = [p for p in (d.trio_path, d.tandem_path) if p]
    return [days[k] for k in sorted(days)]


def load_headers(root: Path, people: list[str] | None = None) -> dict[str, list[Header]]:
    """Each person's saved export headers, oldest first."""
    out: dict[str, list[Header]] = {}
    for pdir in _people(root, people):
        hdir = pdir / HEADERS_DIR
        if hdir.is_dir():
            hs = [read_header(f) for f in sorted(hdir.glob("*.txt"))]
            out[pdir.name] = sorted(hs, key=lambda h: h.created or datetime.min)
    return out


def _load(d: Day) -> Day:
    return d.load()


def load_all(days: list[Day], workers: int | None = None) -> Iterator[Day]:
    from concurrent.futures import ProcessPoolExecutor

    with ProcessPoolExecutor(max_workers=workers) as ex:
        yield from ex.map(_load, days, chunksize=1)
