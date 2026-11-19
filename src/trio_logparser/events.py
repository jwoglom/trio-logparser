"""Records -> typed events.

Matching is on message text only: function names and line numbers change between TandemKit
builds (e.g. ``write(message:)`` became ``writeUntyped(_:)``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime

from . import known
from .historylog import bolus_records
from .parse import TORN, Record


@dataclass(slots=True)
class Event:
    kind: str
    ts: datetime
    src: str
    lineno: int
    data: dict = field(default_factory=dict)


def _num(s: str) -> float | int | str:
    try:
        return int(s)
    except ValueError:
        try:
            return float(s)
        except ValueError:
            return s


_KV = re.compile(r"(\w+)=(Optional\([^()]*\)|\[[^\]]*\]|[^,()\s]+)")


def _value(v: str) -> float | int | str:
    if v.startswith("Optional("):
        v = v[9:-1].rsplit(".", 1)[-1]  # Optional(TandemKit.X.Y.permissionGranted) -> permissionGranted
    return _num(v)


def fields(body: str) -> dict:
    """``"cargo=.., status=0, tempRateId=6028"`` -> ``{"status": 0, "tempRateId": 6028}``."""
    return {k: _value(v) for k, v in _KV.findall(body) if k != "cargo"}


_SWIFT_DATE = r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \+0000"  # Date's description: always UTC


def swift_date(s: str) -> datetime:
    """``"2026-09-20 04:05:06 +0000"`` -> that UTC instant, naive."""
    return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")


_MISDATED = [
    ("start_ahead", re.compile(rf"starts at (?P<a>{_SWIFT_DATE}), ahead of the phone's (?P<b>{_SWIFT_DATE})")),
    ("ends_before_start", re.compile(rf"ends at (?P<b>{_SWIFT_DATE}), before it starts at (?P<a>{_SWIFT_DATE})")),
]
_NEGATIVE_UNITS = re.compile(r"delivered (-\d+(?:\.\d+)?) U")


def misdated_problems(text: str) -> dict:
    """What HostDoseDates found wrong with a dose: ``{"start_ahead": 3600.0}`` is a start an hour
    ahead of the phone, ``ends_before_start`` the seconds its end fell short of its start, and
    ``negative_delivery`` the units."""
    out = {kind: (swift_date(m["a"]) - swift_date(m["b"])).total_seconds()
           for kind, rx in _MISDATED if (m := rx.search(text))}
    if m := _NEGATIVE_UNITS.search(text):
        out["negative_delivery"] = float(m[1])
    return out


# (kind, regex, converter) applied to message text. First match wins.
_TRIO_RULES: list[tuple[str, re.Pattern, dict]] = [
    ("app_start", re.compile(r"^Trio Started: v(?P<version>\S+) .*?\[Branch: (?P<branch>\S+) (?P<commit>\w+)\]"
                             r"(?:.*?TandemKit: \w+ (?P<tandemkit>[0-9a-f]+))?"), {}),
    ("loop_start", re.compile(r"^Start determine basal$"), {}),
    ("loop_success", re.compile(r"^Loop succeeded$"), {}),
    ("loop_skipped", re.compile(r"^Loop skipped"), {}),
    ("enacted", re.compile(r"^Determination enacted\. Enacted: (?P<ok>true|false)"), {"ok": "bool"}),
    ("trio_temp_basal", re.compile(r"^Temp basal (?P<result>succeeded|failed): (?P<rate>[\d.]+) for: (?P<duration>[\d.]+)"),
     {"rate": float, "duration": float}),
    ("rate_not_on_table", re.compile(r"^Temp basal (?P<rate>[\d.]+) U/hr is not on the pump's rate table"),
     {"rate": float}),
    ("trio_bolus_request", re.compile(r"^Enact bolus (?P<units>[\d.]+), manual (?P<manual>true|false)"),
     {"units": float, "manual": "bool"}),
    ("trio_bolus", re.compile(r"^Bolus (?P<result>succeeded|failed): (?P<units>[\d.]+)"), {"units": float}),
    ("trio_bolus_cancel", re.compile(r"^(?:Cancel bolus|Bolus cancelled|Cancel Bolus failed|Bolus cancellation failed)"), {}),
    ("glucose", re.compile(r"^New glucose found"), {}),
    ("pump_status_basal", re.compile(r"^New pump status Basal: .*BasalDeliveryState\.(?P<state>\w+)"), {}),
    # Trio 1.0.2+ wording (the "Trio Logs" export; APSManager.swift on Trio dev)
    ("app_start", re.compile(r"^Trio started: v(?P<version>\S+) \(\d+\) (?P<branch>\S+) (?P<commit>[0-9a-f]+)"
                             r"(?:;.*?\bTandemKit (?P<tandemkit>[0-9a-f]+))?"), {}),
    ("loop_start", re.compile(r"^Loop started, mode \w+"), {}),
    ("loop_success", re.compile(r"^Loop finished in [\d.]+ s$"), {}),
    ("loop_failed", re.compile(r"^Loop failed: (?P<error>.*)"), {}),
    ("enacted", re.compile(r"^Determination marked as (?P<ok>enacted|not enacted)$"),
     {"ok": lambda v: v == "enacted"}),
    # "Temp basal command failed" is logged by the PumpManager wrapper, "enacted" after it returns
    ("trio_temp_basal", re.compile(r"^Temp basal (?P<result>enacted|command failed): (?P<rate>[\d.]+) U/h "
                                   r"for (?P<duration_min>\d+) min"),
     {"result": lambda v: "succeeded" if v == "enacted" else "failed", "rate": float,
      "duration_min": int}),
    ("rate_not_on_table", re.compile(r"^Temp basal (?P<rate>[\d.]+) U/h is not on the pump's rate table"),
     {"rate": float}),
    ("trio_bolus_request", re.compile(r"^Enacting bolus (?P<units>[\d.]+) U, (?P<manual>manual|automatic)$"),
     {"units": float, "manual": lambda v: v == "manual"}),
    ("trio_bolus", re.compile(r"^Bolus (?P<result>enacted): (?P<units>[\d.]+) U, (?P<manual>manual|automatic)$"),
     {"result": lambda v: "succeeded", "units": float, "manual": lambda v: v == "manual"}),
    # SMBs go straight to the pump manager: no "Enacting"/"Bolus enacted" line, and a failure
    # surfaces only as "Loop failed: ..."
    ("trio_bolus", re.compile(r"^SMB (?P<result>enacted): (?P<units>[\d.]+) U$"),
     {"result": lambda v: "succeeded", "units": float}),
    ("trio_bolus", re.compile(r"^Bolus of (?P<units>[\d.]+) U (?P<result>failed)"), {"units": float}),
    ("trio_bolus_cancel", re.compile(r"^Cancelling bolus$"), {}),
    ("glucose", re.compile(r"^Storing [1-9]\d* new glucose readings"), {}),
    # the pump manager's didAdjustPumpClockBy; before 1.0.2 the raw TimeInterval
    ("pump_clock_adjusted", re.compile(r"^(?:Pump clock adjusted by|didAdjustPumpClockBy) "
                                       r"(?P<seconds>-?\d+(?:\.\d+)?)(?: s)?$"), {"seconds": float}),
]
_TRIO_LOOP_FAILED = re.compile(r'Loop failed with error: (?P<error>.*?)"?\)?$')

_TANDEM_RULES: list[tuple[str, re.Pattern, dict]] = [
    # ahead of pump_response: older builds log a stream packet as a plain "Received message"
    ("history_stream", re.compile(r"^Received (?:stream )?message: HistoryLogStreamResponse\("
                                  r"cargo=(?P<boluses>[0-9a-fA-F]*), numberOfHistoryLogs=(?P<count>\d+)"),
     {"boluses": bolus_records, "count": int}),
    ("pump_request", re.compile(r"^Writing message: (?P<name>\w+)\((?P<body>.*)\)\s*$"), {"body": fields}),
    ("pump_response", re.compile(r"^Received message: (?P<name>\w+)\((?P<body>.*)\)\s*$"), {"body": fields}),
    ("tb_set", re.compile(r"^Setting temp basal at (?P<rate>[\d.]+)U/hr for (?P<duration>[\d.]+)s"),
     {"rate": float, "duration": float}),
    ("tb_enacted", re.compile(r"^Temp basal enacted!"), {}),
    ("tb_no_change", re.compile(r"^Enact completed! \(no basal rate change\)"), {}),
    ("tb_cancelled", re.compile(r"^Current temp basal cancelled!"), {}),
    ("tb_blocked", re.compile(r"^\[enactTempBasal\] Blocked: (?P<reason>.*)"), {}),
    ("bolus_enact", re.compile(r"^Enacting bolus - units (?P<units>[\d.]+)U"), {"units": float}),
    ("bolus_cancel", re.compile(r"^Cancelling bolus"), {}),
    ("connected", re.compile(r"^Connected to pump: "), {}),
    ("ready", re.compile(r"^Connected to pump!$"), {}),
    ("disconnected", re.compile(r"^Device disconnected, name: .*?, error: (?P<error>.*)"), {}),
    ("timeout", re.compile(r"^Timeout hit"), {}),
    ("write_cancelled", re.compile(r"^Write cancelled before a response arrived"), {}),
    ("refused", re.compile(r"^Pump refused"), {}),
    ("suspended", re.compile(r"^Insulin Delivery Suspended!"), {}),
    ("resumed", re.compile(r"^Insulin Delivery Resumed!"), {}),
    # HostDoseDates: a dose whose dates the host could not store, and the dates sent in their place.
    # `raw` is the dose's id as Trio and TandemKit log it (it embeds the pump serial in bytes 2-5).
    ("misdated_dose", re.compile(rf"^Misdated (?P<type>\w+) (?P<raw>[0-9a-f]*) ?(?P<problem>.*); "
                                 rf"sent as (?P<sent_start>{_SWIFT_DATE}) to (?P<sent_end>{_SWIFT_DATE})$"),
     {"problem": misdated_problems, "sent_start": swift_date, "sent_end": swift_date}),
    ("history_bolus_report", re.compile(r"^Reporting (?P<count>\d+) bolus event\(s\) from pump history"),
     {"count": int}),
    ("history_backfill", re.compile(r"^Backfilling history (?P<first>\d+)-(?P<last>\d+) \((?P<count>\d+) entries\)"),
     {"first": int, "last": int, "count": int}),
    ("history_pruned", re.compile(r"^Pruned (?P<count>\d+) expired history logs"), {"count": int}),
]


def _convert(m: re.Match, conv: dict) -> dict:
    d = {k: v for k, v in m.groupdict().items() if v is not None}
    for k, c in conv.items():
        if k in d:
            if c == "bool":
                d[k] = d[k] == "true"
            elif k == "body":
                d = {**c(d.pop("body")), **d}  # message identity (name) wins over body fields
            else:
                d[k] = c(d[k])
    return d


PROBLEM_LEVELS = ("WARN", "WARNING", "ERROR", "FAULT")


def extract(records: Iterable[Record]) -> Iterator[Event]:
    for r in records:
        if r.level == TORN:
            yield Event("torn", r.ts, r.src, r.lineno, {"tail": r.msg[:200]})
            continue
        rules = _TRIO_RULES if r.src == "trio" else _TANDEM_RULES
        msg = r.msg
        for issue_id, what in known.scan(r.src, msg):
            yield Event("known", r.ts, r.src, r.lineno, {"issue": issue_id, "what": what})
        for kind, rx, conv in rules:
            if m := rx.search(msg):
                yield Event(kind, r.ts, r.src, r.lineno, _convert(m, conv))
                break
        else:
            if r.src == "trio" and "Loop failed with error" in msg and "finalizeLoop" in r.func:
                m = _TRIO_LOOP_FAILED.search(msg)
                yield Event("loop_failed", r.ts, r.src, r.lineno, {"error": m["error"] if m else msg})
        if r.level in PROBLEM_LEVELS:
            yield Event("problem", r.ts, r.src, r.lineno,
                        {"level": r.level, "file": r.file, "func": r.func, "msg": r.msg[:400]})


def response_name(request: str) -> str:
    return {"MalfunctionStatusRequest": "MalfunctionBitmaskStatusResponse"}.get(
        request, request.removesuffix("Request") + "Response"
    )


# Seconds since 2008-01-01 00:00 in pump-local time (TimeSinceResetResponse.currentTime etc.)
PUMP_EPOCH = datetime(2008, 1, 1)
