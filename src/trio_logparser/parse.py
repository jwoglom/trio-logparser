"""Line-level parsing of Trio and TandemKit logs into records.

Trio log::

    2026-10-02T00:02:33-0400 [ApsManager] APSManager.swift - enactTempBasal(unitsPerHour:for:) - 1441 - DEV: msg
    [2026-08-23T23:53:04-0400] [WatchState.swift:428] finalizePendingData() → msg

Lines matching neither are continuations of the previous record (JSON, tables, HTTP dumps).

TandemKit log::

    [2026-10-02T00:02:33-0400 INFO] TandemPumpManager.swift - enactTempBasal(unitsPerHour:for:completion:)#739: msg

TandemKit's file logger can let two concurrent writes land at the same offset; the shorter
overwrites the start of the longer, leaving only the longer one's *tail* on its own line. Such
lines are kept as ``TORN`` records (timestamp borrowed from the previous record) so pairing
checks can tell "response missing" from "response possibly lost to a torn line".
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

_TRIO = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?P<tz>[+-]\d{4}) \[(?P<cat>[^\]]*)\] "
    r"(?P<file>\S+) - (?P<func>.*?) - (?P<line>\d+) - (?P<level>[A-Z]+): (?P<msg>.*)$"
)
_WATCH = re.compile(
    r"^\[(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?P<tz>[+-]\d{4})\] "
    r"\[(?P<file>[^:\]]+):(?P<line>\d+)\] (?P<func>.*?) → (?P<msg>.*)$"
)
_TANDEM = re.compile(
    r"^\[(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?P<tz>[+-]\d{4}) (?P<level>[A-Z]+)\] "
    r"(?P<file>\S+) - (?P<func>.*?)#(?P<line>\d+): (?P<msg>.*)$"
)

TORN = "TORN"
ORPHAN = "ORPHAN"  # continuation text before the first record of a file


@dataclass(slots=True)
class Record:
    src: str  # "trio" | "tandem"
    ts: datetime  # local wall-clock time as logged (naive)
    tz: str
    level: str  # DEV/WARN (trio), INFO/WARNING/ERROR (tandem), WATCH, TORN, ORPHAN
    category: str
    file: str
    func: str
    msg: str
    lineno: int  # 1-based line number in the source file
    extra: list[str] = field(default_factory=list)  # continuation lines

    @property
    def text(self) -> str:
        return "\n".join([self.msg, *self.extra]) if self.extra else self.msg


def _iter_lines(path: Path) -> Iterator[tuple[int, str]]:
    with open(path, encoding="utf-8", errors="replace", newline="") as f:
        for i, line in enumerate(f, 1):
            yield i, line.rstrip("\r\n")


def parse_trio(path: Path) -> Iterator[Record]:
    cur: Record | None = None
    pending: list[str] = []
    first_lineno = 1
    for i, line in _iter_lines(path):
        m = _TRIO.match(line) if line[:1] == "2" else None
        if m:
            level, cat = m["level"], m["cat"]
        elif line[:2] == "[2" and (m := _WATCH.match(line)):
            level, cat = "WATCH", "Watch"
        else:
            if cur is not None:
                cur.extra.append(line)
            elif line:
                pending.append(line)
            continue
        if cur is not None:
            yield cur
        elif pending:
            ts = datetime.fromisoformat(m["ts"])
            yield Record("trio", ts, m["tz"], ORPHAN, "", "", "", pending[0], first_lineno, pending[1:])
        cur = Record("trio", datetime.fromisoformat(m["ts"]), m["tz"], level, cat, m["file"],
                     m["func"], m["msg"], i)
    if cur is not None:
        yield cur


def parse_tandem(path: Path) -> Iterator[Record]:
    prev: Record | None = None
    for i, line in _iter_lines(path):
        if not line:
            continue
        m = _TANDEM.match(line) if line[:2] == "[2" else None
        if m:
            rec = Record("tandem", datetime.fromisoformat(m["ts"]), m["tz"], m["level"], "",
                         m["file"], m["func"], m["msg"], i)
        elif prev is not None:
            rec = Record("tandem", prev.ts, prev.tz, TORN, "", "", "", line, i)
        else:
            continue  # torn line before any timestamp: nothing to anchor it to
        yield rec
        prev = rec


def parse(path: Path) -> Iterator[Record]:
    return parse_tandem(path) if Path(path).name.endswith("_tandem_log.txt") else parse_trio(path)
