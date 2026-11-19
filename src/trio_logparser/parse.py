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

from .export import LOG_HEADER, is_export_text

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
    source: str = ""  # the line's own source: Trio, Watch, Pump, CGM, Algorithm, ...

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
            yield Record("trio", ts, m["tz"], ORPHAN, "", "", "", pending[0], first_lineno, pending[1:],
                         "Trio")
        cur = Record("trio", datetime.fromisoformat(m["ts"]), m["tz"], level, cat, m["file"],
                     m["func"], m["msg"], i, source="Watch" if level == "WATCH" else "Trio")
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
                         m["file"], m["func"], m["msg"], i, source="Pump")
        elif prev is not None:
            rec = Record("tandem", prev.ts, prev.tz, TORN, "", "", "", line, i, source="Pump")
        else:
            continue  # torn line before any timestamp: nothing to anchor it to
        yield rec
        prev = rec


# Trio's log export (Trio 1.0.2+, see ``export.py``): a header (== App ==, == Pump ==, ...), then
# ``== Log ==`` and every source merged in time order, continuation lines indented 4::
#
#     2026-11-17 15:10:31.486 -0500 Trio [Lifecycle] Trio started: v1.0.2.76 (1) ...
#     2026-11-17 15:47:09.671 -0500 Trio WARNING: [Loop] Loop failed: ...
#     2026-11-17 15:53:38.007 -0500 Pump [delegate] [INFO] TandemPeripheralManager.swift - f()#476: msg
#     2026-11-17 15:44:17.481 -0500 CGM [connection] Scanning for sensor
#     2026-11-17 15:47:09.685 -0500 Algorithm suggested · BG 149 · ...
#
# Pump lines come from Trio's device-log store, not TandemLogger's file, so they are never torn.
_EXPORT = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?) (?P<tz>[+-]\d{4}) (?P<source>\S+) (?P<rest>.*)$"
)
_EXPORT_APP = re.compile(r"^(?:(?P<level>[A-Z]+): )?\[(?P<cat>[^\]]*)\] (?P<msg>.*)$")
_EXPORT_DEVICE = re.compile(r"^\[(?P<cat>[^\]]*)\] (?P<msg>.*)$")
_EXPORT_TANDEM = re.compile(r"^\[(?P<level>[A-Z]+)\] (?P<file>\S+) - (?P<func>.*?)#(?P<line>\d+): (?P<msg>.*)$")


def is_export(path: Path) -> bool:
    """Whether ``path`` is a Trio log export (any variant) or an organized export day file."""
    with open(path, encoding="utf-8", errors="replace") as f:
        return is_export_text(f.readline())


def parse_export(path: Path) -> Iterator[Record]:
    """Records of a Trio log export: Trio/Watch/CGM/Algorithm lines as ``trio``, Pump as ``tandem``."""
    cur: Record | None = None
    in_log = False
    for i, line in _iter_lines(path):
        if not in_log:
            in_log = line == LOG_HEADER
            continue
        m = _EXPORT.match(line) if line[:1] == "2" else None
        if not m:
            if cur is not None:
                cur.extra.append(line.removeprefix("    "))
            continue
        if cur is not None:
            yield cur
        ts, tz, source, rest = datetime.fromisoformat(m["ts"]), m["tz"], m["source"], m["rest"]
        if source == "Pump":
            dm = _EXPORT_DEVICE.match(rest)
            body = dm["msg"] if dm else rest
            if tm := _EXPORT_TANDEM.match(body):
                cur = Record("tandem", ts, tz, tm["level"], dm["cat"], tm["file"], tm["func"], tm["msg"], i,
                             source=source)
            else:  # another pump driver's free-form line
                cur = Record("tandem", ts, tz, "INFO", dm["cat"] if dm else "", "", "", body, i, source=source)
        elif source in ("Trio", "Watch") and (am := _EXPORT_APP.match(rest)):
            level = am["level"] or ("WATCH" if source == "Watch" else "INFO")
            cur = Record("trio", ts, tz, level, am["cat"], "", "", am["msg"], i, source=source)
        else:  # CGM / Algorithm / other device sources: kept as trio-side context
            dm = _EXPORT_DEVICE.match(rest)
            cur = Record("trio", ts, tz, "INFO", source if not dm else f"{source}/{dm['cat']}", "", "",
                         dm["msg"] if dm else rest, i, source=source)
    if cur is not None:
        yield cur


def parse(path: Path) -> Iterator[Record]:
    """Records of any supported log: an export (both sides), a tandem_log, or a Trio log."""
    if Path(path).name.endswith("_tandem_log.txt"):
        return parse_tandem(path)
    return parse_export(path) if is_export(path) else parse_trio(path)
