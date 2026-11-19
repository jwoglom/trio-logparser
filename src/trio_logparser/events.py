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
]
_TRIO_LOOP_FAILED = re.compile(r'Loop failed with error: (?P<error>.*?)"?\)?$')

_TANDEM_RULES: list[tuple[str, re.Pattern, dict]] = [
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
        if r.level in ("WARN", "WARNING", "ERROR"):
            yield Event("problem", r.ts, r.src, r.lineno,
                        {"level": r.level, "file": r.file, "func": r.func, "msg": r.msg[:400]})


def response_name(request: str) -> str:
    return {"MalfunctionStatusRequest": "MalfunctionBitmaskStatusResponse"}.get(
        request, request.removesuffix("Request") + "Response"
    )


# Seconds since 2008-01-01 00:00 in pump-local time (TimeSinceResetResponse.currentTime etc.)
PUMP_EPOCH = datetime(2008, 1, 1)
