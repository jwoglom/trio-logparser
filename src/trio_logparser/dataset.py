"""The organized log tree: people, days, and their parsed events."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from . import events as ev
from .parse import parse, parse_export

_DAY_FILE = re.compile(r"^(?P<day>\d{4}-\d{2}-\d{2})_(?P<kind>tandem_log|log)\.txt$")


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
    trio: list[ev.Event] = field(default_factory=list)
    tandem: list[ev.Event] = field(default_factory=list)
    # minutes (epoch-minute ints) in which each log has any line, to tell "event missing"
    # from "log not being written"
    trio_minutes: set[int] = field(default_factory=set)
    tandem_minutes: set[int] = field(default_factory=set)
    builds: list = field(default_factory=list)  # [(start ts, Build)], filled by builds.assign

    def path(self, src: str) -> Path | None:
        return self.trio_path if src == "trio" else self.tandem_path

    def load(self) -> Day:
        for src in ("trio", "tandem"):
            path = self.path(src)
            if not path:
                continue
            minutes: set[int] = getattr(self, f"{src}_minutes")
            def records(path=path, minutes=minutes):
                for r in parse(path):
                    minutes.add(minute(r.ts))
                    yield r
            setattr(self, src, list(ev.extract(records())))
        return self

    def covered(self, src: str, ts, slack_min: int = 2) -> bool:
        """Whether ``src``'s log has lines within ``slack_min`` minutes of ``ts``."""
        minutes = getattr(self, f"{src}_minutes")
        m = minute(ts)
        return any(m + d in minutes for d in range(-slack_min, slack_min + 1))


def discover(root: Path, people: list[str] | None = None) -> list[Day]:
    days: dict[tuple[str, str], Day] = {}
    for pdir in sorted(Path(root).iterdir()):
        if not pdir.is_dir() or pdir.name.startswith((".", "Upload")):
            continue
        if people and pdir.name not in people and alias(pdir.name) not in people:
            continue
        for f in pdir.iterdir():
            if m := _DAY_FILE.match(f.name):
                d = days.setdefault((pdir.name, m["day"]), Day(pdir.name, m["day"]))
                if m["kind"] == "log":
                    d.trio_path = f
                else:
                    d.tandem_path = f
    return [days[k] for k in sorted(days)]


def from_export(path: Path, person: str) -> list[Day]:
    """Days of one "Trio Logs" export file, split by local date; both logs point at the file."""
    path = Path(path)
    days: dict[str, Day] = {}
    records: dict[tuple[str, str], list] = {}
    for r in parse_export(path):
        key = r.ts.date().isoformat()
        d = days.get(key) or days.setdefault(key, Day(person, key))
        if r.src == "trio":
            d.trio_path = path
        else:
            d.tandem_path = path
        getattr(d, f"{r.src}_minutes").add(minute(r.ts))
        records.setdefault((key, r.src), []).append(r)
    for (key, src), rs in records.items():
        setattr(days[key], src, list(ev.extract(rs)))
    return [days[k] for k in sorted(days)]


def _load(d: Day) -> Day:
    return d.load()


def load_all(days: list[Day], workers: int | None = None) -> Iterator[Day]:
    from concurrent.futures import ProcessPoolExecutor

    with ProcessPoolExecutor(max_workers=workers) as ex:
        yield from ex.map(_load, days, chunksize=1)
