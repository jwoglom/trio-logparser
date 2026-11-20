"""What the web viewer (``trio-logs web``) shows: one selection of log days as JSON-ready tables.

A selection is one export file or a run of one person's organized days. It is parsed once: the
records are the log lines, and the same records give the events the checks use, so every derived
row (a CGM reading, an oref determination, a dose, a pump request) points at the line it came from.

Times are sent as epoch milliseconds plus each line's own UTC offset, so the browser shows the
wall-clock time the phone logged.
"""

from __future__ import annotations

import collections
import functools
import json
import math
import re
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from . import analysis, checks, known
from . import events as ev
from .dataset import Day, alias, from_export
from .export import Header
from .historylog import RECORD_BYTES
from .normalize import CARGO_MASKED, has_serial_field, mask
from .parse import Record, parse_export, parse_tandem, parse_trio

_EPOCH = datetime(1970, 1, 1)
_MS = timedelta(milliseconds=1)


@functools.cache
def utc_offset(tz: str) -> int:
    """``"-0500"`` -> -300 (minutes east of UTC)."""
    try:
        minutes = int(tz[1:3]) * 60 + int(tz[3:5])
    except (ValueError, IndexError):
        return 0
    return -minutes if tz[:1] == "-" else minutes


def epoch_ms(ts: datetime, tz: str) -> int:
    return (ts - timedelta(minutes=utc_offset(tz)) - _EPOCH) // _MS


@dataclass
class Selection:
    person: str
    days: list[Day]
    lines: list[tuple[int, Record]]  # (file index, record) in time order
    files: list[Path]
    file_refs: list[str]  # per file: the `trio-logs show` command prefix for its line numbers
    headers: list[Header]


def from_file(path: Path, person: str = "export") -> Selection:
    path = Path(path)
    records = list(parse_export(path))
    days, header = from_export(path, person, records)
    return Selection(person, days, [(0, r) for r in records], [path],
                     [f"trio-logs show --file {shlex.quote(str(path))}"], [header])


def from_days(days: list[Day], headers: list[Header], who: str) -> Selection:
    """Parse organized days (``dataset.discover``) once, keeping their records."""
    files: list[Path] = []
    refs: list[str] = []
    lines: list[tuple[int, Record]] = []
    for d in days:
        if d.export_path:
            parts = [("trio", d.export_path, parse_export)]
        else:
            parts = [(src, p, fn) for src, p, fn in (("trio", d.trio_path, parse_trio),
                                                     ("tandem", d.tandem_path, parse_tandem)) if p]
        split: dict[str, list[Record]] = {"trio": [], "tandem": []}
        for src, path, parse in parts:
            fi = len(files)
            files.append(path)
            refs.append(f"trio-logs show {shlex.quote(who)} {d.day} {src}")
            for r in parse(path):
                split[r.src].append(r)
                lines.append((fi, r))
        for src, rs in split.items():
            d._set(src, rs)
    lines.sort(key=lambda fr: epoch_ms(fr[1].ts, fr[1].tz))
    return Selection(days[0].person if days else "", days, lines, files, refs, headers)


# --------------------------------------------------------------------------- glucose

_G7_LIVE = re.compile(r"didRead G7GlucoseMessage\(")
_G7_BACKFILL = re.compile(r"didReadBackfill G7BackfillMessage\(")
_GLUCOSE = re.compile(r"\bglucose:Optional\((\d+)\)")
_MSG_TS = re.compile(r"\bmessageTimestamp:(\d+)")
_AGE = re.compile(r"\bage:(\d+)")
_SENSOR_TS = re.compile(r"\btimestamp:(\d+)")
_DISPLAY_ONLY = re.compile(r"glucoseIsDisplayOnly:true")
_LOOP_BG = re.compile(r"^Glucose ready: BG (?P<v>-?[\d.]+) (?P<unit>mg/dL|mmol/L)")


SAME_READING_MS = 90_000  # a backfilled reading this close to another is the same one, re-sent


def glucose(lines: list[tuple[int, Record]], times: list[int]) -> list[dict]:
    """CGM readings, time-sorted: the sensor's own (Dexcom G7 live and backfill messages, dated by
    the sensor) or, when the log has none, the BG each loop started from (``Glucose ready``).

    A G7 live message gives the session start (line time - ``messageTimestamp``), which dates
    backfill messages (``timestamp`` is seconds into the session); a backfill logged before any
    live message uses the next one's.
    """
    found: list[tuple[int, str, int | None, int, Record]] = []  # (line, kind, session ms, sensor s, r)
    for i, (_, r) in enumerate(lines):
        if not (r.source == "CGM" or r.category.startswith("CGM")):
            continue
        if _G7_LIVE.search(r.msg):
            mts, age = _MSG_TS.search(r.msg), _AGE.search(r.msg)
            if mts and age:
                found.append((i, "live", times[i] - int(mts[1]) * 1000, int(mts[1]) - int(age[1]), r))
        elif _G7_BACKFILL.search(r.msg) and (sts := _SENSOR_TS.search(r.msg)):
            found.append((i, "backfill", None, int(sts[1]), r))
    session = next((f[2] for f in found if f[2] is not None), None)
    readings: list[dict] = []
    for i, kind, own, sensor_s, r in found:
        session = own if own is not None else session
        if not (g := _GLUCOSE.search(r.msg)):
            continue  # no reading (warm-up, sensor error)
        t = session + sensor_s * 1000 if session is not None else times[i]
        readings.append({"t": t, "v": int(g[1]), "kind": kind, "line": i,
                         **({"display_only": True} if _DISPLAY_ONLY.search(r.msg) else {})})
    readings.sort(key=lambda g: (g["t"], g["kind"] != "live"))
    out: list[dict] = []
    for g in readings:
        if out and g["t"] - out[-1]["t"] < SAME_READING_MS and g["v"] == out[-1]["v"]:
            continue
        if out and g["t"] - out[-1]["t"] < SAME_READING_MS and out[-1]["kind"] == "backfill" and g["kind"] == "live":
            out[-1] = g
            continue
        out.append(g)
    if not out:
        for i, (_, r) in enumerate(lines):
            if r.src == "trio" and (m := _LOOP_BG.match(r.msg)):
                out.append({"t": times[i], "v": float(m["v"]) if "." in m["v"] else int(m["v"]),
                            "kind": "loop", "line": i})
    return out


# --------------------------------------------------------------------------- oref

_ALGO = re.compile(r"^(?P<kind>suggested|enacted) · (?P<body>.*)$", re.DOTALL)
_ALGO_PART = re.compile(r"^(?P<k>BG|IOB|COB|rate|SMB|eventual) (?P<v>\S+)$")
_REASON = {
    "sens": re.compile(r"Autosens ratio: (-?\d+(?:\.\d+)?)"),
    "isf": re.compile(r"ISF: [\d.]+→(-?\d+(?:\.\d+)?)"),
    "cr": re.compile(r"\bCR: (-?\d+(?:\.\d+)?)"),
    "target": re.compile(r"Target: (-?\d+(?:\.\d+)?)"),
    "dev": re.compile(r"Dev: (-?\d+(?:\.\d+)?)"),
    "bgi": re.compile(r"BGI: (-?\d+(?:\.\d+)?)"),
    "min_pred": re.compile(r"minPredBG (-?\d+(?:\.\d+)?)"),
    "min_guard": re.compile(r"minGuardBG (-?\d+(?:\.\d+)?)"),
    "insulin_req": re.compile(r"insulinReq (-?\d+(?:\.\d+)?)"),
}
_RATE = re.compile(r"^(?P<rate>-?\d+(?:\.\d+)?)/(?P<min>\d+)m$")
_OUTCOMES = [
    (re.compile(r"^Determination not enacted: (.*)"), "not enacted: {}"),
    (re.compile(r"^Loop failed: (.*)"), "loop failed: {}"),
    (re.compile(r"^Temp basal enacted: ([\d.]+ U/h for \d+ min)"), "temp {}"),
    (re.compile(r"^Temp basal command failed: (.*)"), "temp failed: {}"),
    (re.compile(r"^SMB enacted: ([\d.]+ U)"), "SMB {}"),
    (re.compile(r"^Loop finished in ([\d.]+ s)"), "loop {}"),
]
OUTCOME_WINDOW_MS = 120_000


def _number(v: str) -> float | int | str:
    try:
        f = float(v)
    except ValueError:
        return v
    return int(f) if f.is_integer() and "." not in v else f


def determinations(lines: list[tuple[int, Record]], times: list[int]) -> list[dict]:
    """oref's results as Trio logs them (``Algorithm suggested|enacted · BG … · reason``), each with
    what Trio did next: the temp basal / SMB it enacted, or why not, until the next determination."""
    out: list[dict] = []
    for i, (_, r) in enumerate(lines):
        if r.source != "Algorithm" or not (m := _ALGO.match(r.text)):
            continue
        parts = m["body"].split(" · ")
        d: dict = {"t": times[i], "line": i, "kind": m["kind"]}
        k = 0
        while k < len(parts) and (pm := _ALGO_PART.match(parts[k])):
            key, v = pm["k"].lower(), pm["v"]
            if key == "rate" and (rm := _RATE.match(v)):
                d["rate"], d["duration"] = _number(rm["rate"]), int(rm["min"])
            else:
                d[key] = _number(v)
            k += 1
        reason = " · ".join(parts[k:]).strip()
        d["reason"] = reason
        for key, rx in _REASON.items():
            if rm := rx.search(reason):
                d[key] = _number(rm[1])
        out.append(d)
    # what happened after each one
    starts = [d["line"] for d in out]
    for n, d in enumerate(out):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        got: list[str] = []
        for j in range(d["line"] + 1, end):
            if times[j] - d["t"] > OUTCOME_WINDOW_MS:
                break
            r = lines[j][1]
            if r.source != "Trio":
                continue
            for rx, fmt in _OUTCOMES:
                if om := rx.match(r.msg):
                    got.append(fmt.format(om[1][:160]))
                    break
        d["outcome"] = " · ".join(got)
    return out


# --------------------------------------------------------------------------- doses & pump

_STORED = [
    (re.compile(r"^Finalized temp basal (?P<raw>\S+): rate (?P<rate>[\d.]+) U/h for (?P<min>[\d.]+) min"),
     "stored temp basal"),
    (re.compile(r"^Finalized bolus (?P<raw>\S+): (?P<units>[\d.]+) U"), "stored bolus"),
]
_PUMP_STATUS = re.compile(r"^Pump status: bolusing (?P<bolusing>true|false), suspended (?P<suspended>true|false)"
                          r"(?:, battery (?P<battery>\d+)%)?")
_RESERVOIR = re.compile(r"^Reservoir (?P<units>[\d.]+) U")
# wire requests that change insulin delivery
DELIVERY_REQUESTS = {"SetTempRateRequest", "StopTempRateRequest", "InitiateBolusRequest", "CancelBolusRequest",
                     "SuspendPumpingRequest", "ResumePumpingRequest", "SetModesRequest"}
_OUTCOME_KINDS = {"timeout": "timeout", "write_cancelled": "write cancelled", "refused": "refused",
                  "disconnected": "disconnected"}


def _amount(e: ev.Event) -> str:
    d = e.data
    if "rate" in d:
        mins = d.get("duration_min") or (d["duration"] / 60 if e.kind == "tb_set" else d.get("duration"))
        return f"{d['rate']:g} U/h" + (f" × {mins:g} min" if mins else "")
    return f"{d['units']:g} U" if "units" in d else ""


def pump_comms(tandem: list[tuple[int, int, ev.Event]]) -> list[dict]:
    """Each TandemKit request with its one outcome. TandemKit serializes writes (see
    ``checks.pump_request``), so the outcome is the next response of the matching name, a timeout,
    a cancelled write, a refusal, a disconnect, or, if the next request comes first, a lost line."""
    out: list[dict] = []
    pending: dict | None = None

    def close(outcome: str, line: int | None = None, t: int | None = None, **extra) -> None:
        nonlocal pending
        if pending is not None:
            pending.update(outcome=outcome, rline=line, **extra)
            if t is not None:
                pending["ms"] = t - pending["t"]
        pending = None

    for t, line, e in tandem:
        if e.kind == "pump_request":
            close("no outcome logged")
            pending = {"t": t, "line": line, "request": e.data["name"],
                       "fields": {k: v for k, v in e.data.items() if k != "name"}}
            out.append(pending)
        elif pending is None:
            continue
        elif e.kind == "pump_response" and e.data["name"] == ev.response_name(pending["request"]):
            close("response", line, t, response=e.data["name"],
                  response_fields={k: v for k, v in e.data.items() if k != "name"})
        elif e.kind in _OUTCOME_KINDS:
            close(_OUTCOME_KINDS[e.kind], line, t)
    close("no outcome logged")
    return out


def doses(trio: list[tuple[int, int, ev.Event]], tandem: list[tuple[int, int, ev.Event]],
          comms: list[dict], lines: list[tuple[int, Record]], times: list[int]) -> list[dict]:
    """Every insulin-delivery action, from three vantage points: Trio (what the loop or the user
    asked for, and the doses Trio stored), the driver (TandemKit's own lines), and the wire
    (delivery-changing requests and how the pump answered)."""
    out: list[dict] = []

    def row(t, line, who, action, amount="", outcome="", **extra):
        out.append({"t": t, "line": line, "who": who, "action": action, "amount": amount,
                    "outcome": outcome, **extra})

    for t, line, e in trio:
        d = e.data
        if e.kind == "trio_temp_basal":
            row(t, line, "Trio", "temp basal", _amount(e), "enacted" if d["result"] == "succeeded" else "failed",
                rate=d["rate"], minutes=d.get("duration_min", d.get("duration")), ok=d["result"] == "succeeded")
        elif e.kind == "trio_bolus":
            smb = lines[line][1].msg.startswith("SMB")
            manual = d.get("manual")
            action = "SMB" if smb else "bolus" + (" (manual)" if manual else " (automatic)" if manual is False else "")
            ok = d["result"] in ("succeeded", "enacted")
            row(t, line, "Trio", action, _amount(e), "enacted" if ok else "failed", units=d["units"],
                ok=ok, smb=smb or manual is False)
        elif e.kind == "trio_bolus_request":
            row(t, line, "Trio", "bolus request", _amount(e), "manual" if d.get("manual") else "automatic")
        elif e.kind == "trio_bolus_cancel":
            row(t, line, "Trio", "cancel bolus")
        elif e.kind == "rate_not_on_table":
            row(t, line, "Trio", "temp basal", _amount(e), "not on the pump's rate table")
    for i, (_, r) in enumerate(lines):
        if r.source == "Trio" and r.category == "Pump":
            for rx, action in _STORED:
                if m := rx.match(r.msg):
                    amount = (f"{round(float(m['rate']), 3):g} U/h × {float(m['min']):g} min" if "rate" in m.groupdict()
                              else f"{round(float(m['units']), 3):g} U")
                    row(times[i], i, "Trio", action, amount, "", raw=m["raw"])
    driver = {
        "tb_set": ("set temp basal", None), "tb_enacted": ("temp basal", "enacted"),
        "tb_no_change": ("temp basal", "no rate change"), "tb_cancelled": ("temp basal", "cancelled"),
        "tb_blocked": ("temp basal", "blocked"), "bolus_enact": ("bolus", None),
        "bolus_cancel": ("cancel bolus", None), "suspended": ("suspend", "suspended"),
        "resumed": ("resume", "resumed"), "refused": ("pump refused", None),
    }
    for t, line, e in tandem:
        if e.kind in driver:
            action, outcome = driver[e.kind]
            if e.kind == "tb_blocked":
                outcome = f"blocked: {e.data.get('reason', '')}"
            row(t, line, "Driver", action, _amount(e), outcome or "")
        elif e.kind == "misdated_dose":
            d = e.data
            problems = ", ".join(f"{k} {v:g}" for k, v in d.get("problem", {}).items())
            row(t, line, "Driver", f"misdated {d['type']}", "", f"sent as {d['sent_start']:%Y-%m-%d %H:%M:%S} UTC",
                raw=d.get("raw"), problem=problems)
    for c in comms:
        if c["request"] in DELIVERY_REQUESTS:
            fields = ", ".join(f"{k}={v}" for k, v in c["fields"].items())
            status = c.get("response_fields", {}).get("status")
            outcome = c["outcome"] if c["outcome"] != "response" else (
                f"status {status}" if status is not None else c.get("response", ""))
            row(c["t"], c["line"], "Pump", c["request"].removesuffix("Request"), fields, outcome,
                rline=c.get("rline"), ms=c.get("ms"))
    out.sort(key=lambda r: (r["t"], r["line"]))
    return out


def pump_iob(comms: list[dict]) -> list[dict]:
    """The IOB the pump itself shows, from each ``ControlIQIOBResponse``: milliunits of the
    calculation ``iobType`` names (0 Mudaliar, 1 Swan 6 h, else the larger), as TandemKit's
    ``pumpDisplayedIOB``. The pump counts by its own rules, so it tracks Trio's IOB only roughly;
    what stands out is the two drifting apart."""
    out = []
    for c in comms:
        f = c.get("response_fields") or {}
        if c.get("response") != "ControlIQIOBResponse" or "mudaliarIOB" not in f:
            continue
        mudaliar, swan = f.get("mudaliarIOB"), f.get("swan6hrIOB")
        kind = f.get("iobType")
        milli = mudaliar if kind == 0 else swan if kind == 1 else max(v for v in (mudaliar, swan) if v is not None)
        if isinstance(milli, (int, float)):
            out.append({"t": c["t"] + (c.get("ms") or 0), "v": milli / 1000, "line": c.get("rline")})
    return out


def basal_timeline(dose_rows: list[dict]) -> list[dict]:
    """Enacted temp basals for the chart: each from its start for its duration."""
    return [{"t": r["t"], "rate": r["rate"], "minutes": r["minutes"], "line": r["line"]}
            for r in dose_rows if r["who"] == "Trio" and r["action"] == "temp basal" and r.get("ok")
            and r.get("minutes") is not None]


def pump_state(lines: list[tuple[int, Record]], times: list[int]) -> list[dict]:
    """Trio's view of the pump from its status lines: suspended / bolusing changes, battery, reservoir."""
    out: list[dict] = []
    last: dict = {}
    for i, (_, r) in enumerate(lines):
        if r.source != "Trio" or r.category != "Pump":
            continue
        if m := _PUMP_STATUS.match(r.msg):
            cur = {"suspended": m["suspended"] == "true", "bolusing": m["bolusing"] == "true"}
            if m["battery"]:
                cur["battery"] = int(m["battery"])
        elif m := _RESERVOIR.match(r.msg):
            cur = {**last, "reservoir": float(m["units"])}
        else:
            continue
        merged = {**last, **cur}
        if merged != last:
            out.append({"t": times[i], "line": i, **merged})
            last = merged
    return out


# --------------------------------------------------------------------------- markers

_MARKERS = {
    "app_start": ("app", "Trio started"),
    "loop_failed": ("loop", "Loop failed"),
    "loop_success": ("loop", "Loop finished"),
    "connected": ("pump", "Connected"),
    "disconnected": ("pump", "Disconnected"),
    "timeout": ("pump", "Request timed out"),
    "pump_clock_adjusted": ("pump", "Pump clock adjusted"),
    "suspended": ("pump", "Delivery suspended"),
    "resumed": ("pump", "Delivery resumed"),
}


def markers(events: list[tuple[int, int, ev.Event]]) -> list[dict]:
    out = []
    for t, line, e in events:
        if e.kind in _MARKERS:
            lane, label = _MARKERS[e.kind]
            detail = e.data.get("error") or e.data.get("version") or ""
            if e.kind == "pump_clock_adjusted":
                detail = f"{e.data['seconds']:+g} s"
            out.append({"t": t, "line": line, "lane": lane, "kind": e.kind, "label": label,
                        "detail": str(detail)[:300]})
    return out


# --------------------------------------------------------------------------- bundle

def _jsonable(v):
    if isinstance(v, datetime):
        return v.isoformat(sep=" ")
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, float) and not math.isfinite(v):  # NaN, inf: not JSON
        return None
    return v


# --------------------------------------------------------------------------- pump line topics

# Every Pump line in an export has the device-log category "delegate", and older tandem logs have
# none, so the viewer files each one under what it is about instead: from the pump message it
# carries, else TandemKit's function, else its file. Function names change between builds; a line
# nothing here matches falls back to its file's name.
_MESSAGE_NAME = re.compile(r"\b([A-Z]\w+?)(?:Request|Response)\b")
_MESSAGE_TOPICS = [
    (re.compile(r"^HistoryLog"), "History"),
    (re.compile(r"^(SetTempRate|StopTempRate|InitiateBolus|CancelBolus|BolusPermission|SuspendPumping|ResumePumping)"),
     "Delivery"),
    (re.compile(r"^((Enter|Exit)(ChangeCartridge|FillTubing)|FillCannula|DetectingCartridge|\w*StateStream)"),
     "Cartridge & site"),
    (re.compile(r"^(Dismiss|Acknowledge)"), "Alerts"),
    (re.compile(r"^(SetMax|Change|SetIDP|CreateIDP|DeleteIDP|RenameIDP|SetActiveIDP|SetModes|SetQuickBolus)"), "Settings"),
    (re.compile(r"^(Jpake|CentralChallenge|PumpChallenge)"), "Connection"),
    (re.compile(r"^(Set|Stop|Start|Initiate|Cancel|Suspend|Resume|Enter|Exit|Fill|Remote|Create|Delete|Rename|Play)"),
     "Other commands"),
]
_FUNC_TOPICS = {
    "handleHistoryLogStreamPacket": "History",
    "handleQualifyingEvents": "Status reads", "ensureCurrentPumpData": "Status reads",
    "syncDeliveryLimits": "Settings",
    "handleControlStreamPacket": "Cartridge & site",
    "performInitialSetup": "Connection", "peripheral": "Connection", "linkWentDown": "Connection",
    "didReconnect": "Connection", "read": "Connection",
}
_FUNC_PATTERNS = [
    (re.compile(r"(?i)cartridge|tubing|cann?ull?a|sitechange"), "Cartridge & site"),
    (re.compile(r"(?i)alert|alarm|notification"), "Alerts"),
    (re.compile(r"(?i)history"), "History"),
    (re.compile(r"(?i)bolus|basal|deliver|suspend|resume|pendingpumpevents"), "Delivery"),
]
_FILE_TOPICS = [
    (re.compile(r"History"), "History"),
    (re.compile(r"HostDoseDates|BasalReconciliation|DeliveryTransitions"), "Delivery"),
    (re.compile(r"Bluetooth|Peripheral"), "Connection"),
    (re.compile(r"Alert|Notification"), "Alerts"),
]


@functools.cache
def _pump_topic(file: str, func: str, message: str | None) -> str:
    if message:
        return next((t for rx, t in _MESSAGE_TOPICS if rx.match(message)), "Status reads")
    if t := _FUNC_TOPICS.get(func):
        return t
    stem = file.removesuffix(".swift")
    if t := next((t for rx, t in _FILE_TOPICS if rx.search(stem)), None):
        return t
    if t := next((t for rx, t in _FUNC_PATTERNS if rx.search(func)), None):
        return t
    return stem.removeprefix("TandemPumpManager+") or "TandemKit"


def pump_topic(r: Record) -> str:
    """What a pump driver line is about (Delivery, Status reads, History, Connection, ...)."""
    if not r.file:  # another driver's free-form line: nothing to go on but its own category
        return r.category or "Pump"
    m = _MESSAGE_NAME.search(r.msg) if "message" in r.msg else None
    return _pump_topic(r.file, r.func.split("(")[0], m.group(1) if m else None)


def _mask_cargo(fields: dict) -> dict:
    """A parsed message's fields, with its cargo hidden if it carries a serial number."""
    if "cargo" in fields and has_serial_field(fields):
        return {**fields, "cargo": CARGO_MASKED}
    return fields


class _Interner:
    def __init__(self):
        self.items: list[str] = []
        self.ix: dict[str, int] = {}

    def __call__(self, s: str) -> int:
        if (i := self.ix.get(s)) is None:
            i = self.ix[s] = len(self.items)
            self.items.append(s)
        return i


def bundle(sel: Selection, *, names: bool = False, unmasked: bool = False) -> dict:
    """Everything the viewer shows for ``sel`` except the decoded pump history (see ``history``)."""
    m: Callable[[str], str] = (lambda s: s) if unmasked else mask
    lines = sel.lines
    times = [epoch_ms(r.ts, r.tz) for _, r in lines]
    where = {(fi, r.lineno): i for i, (fi, r) in enumerate(lines)}
    fidx = {p: i for i, p in enumerate(sel.files)}
    sources, levels, cats = _Interner(), _Interner(), _Interner()
    offsets = collections.Counter()
    col = {k: [] for k in ("t", "o", "s", "l", "c", "m", "f", "n")}
    extra: dict[int, str] = {}
    for i, (fi, r) in enumerate(lines):
        off = utc_offset(r.tz)
        offsets[off] += 1
        col["t"].append(times[i])
        col["o"].append(off)
        col["s"].append(sources(r.source or ("Pump" if r.src == "tandem" else "Trio")))
        col["l"].append(levels(r.level))
        col["c"].append(cats(pump_topic(r) if r.src == "tandem" else r.category))
        col["m"].append(m(r.msg))
        col["f"].append(fi)
        col["n"].append(r.lineno)
        if r.extra:
            extra[i] = m("\n".join(r.extra))

    def located(d: Day, src: str) -> list[tuple[int, int, ev.Event]]:
        p = d.path(src)
        fi = fidx.get(p) if p else None
        out = []
        for e in getattr(d, src):
            line = where.get((fi, e.lineno)) if fi is not None else None
            if line is not None:
                out.append((times[line], line, e))
        return out

    trio, tandem = [], []
    for d in sel.days:
        trio += located(d, "trio")
        tandem += located(d, "tandem")
    trio.sort(key=lambda x: x[1])
    tandem.sort(key=lambda x: x[1])

    comms = pump_comms(tandem)
    dose_rows = doses(trio, tandem, comms, lines, times)
    for c in comms:
        c["fields"] = _jsonable(c["fields"] if unmasked else _mask_cargo(c["fields"]))
        if "response_fields" in c:
            c["response_fields"] = _jsonable(c["response_fields"] if unmasked else _mask_cargo(c["response_fields"]))

    res = analysis.run(None, loaded=sel.days, headers={sel.person: sel.headers} if sel.headers else None)
    findings = []
    issues: dict[str, dict] = {}
    for f in res.findings:
        d = res.day(f.person, f.day)
        p = d.path(f.src) if d else None
        line = where.get((fidx.get(p), f.lineno)) if p in fidx else None
        issue = known.for_check(f.check)
        if issue:
            issues[issue.id] = {"title": issue.title, "pr": issue.pr, "symptom": issue.symptom}
        findings.append({"t": times[line] if line is not None else epoch_ms(f.ts, lines[0][1].tz if lines else "+0000"),
                         "line": line, "check": f.check, "severity": f.severity, "summary": m(f.summary),
                         "issue": issue.id if issue else None, "data": _jsonable(f.data)})
    findings.sort(key=lambda f: f["t"])

    tz = offsets.most_common(1)[0][0] if offsets else 0
    builds = []
    for d in sel.days:
        for ts, b in d.builds:
            label = f"Trio v{b.version} {b.branch} {b.trio_commit[:7]} · TandemKit {(b.tandemkit or '?')[:7]}"
            t = (ts - timedelta(minutes=tz) - _EPOCH) // _MS
            if not builds or builds[-1]["label"] != label:
                builds.append({"t": t, "label": label})

    event_rows = [[line, e.kind, _jsonable(e.data)] for _, line, e in sorted(trio + tandem, key=lambda x: x[1])
                  if e.kind not in ("problem",)]
    for row in event_rows:
        data = row[2]
        for k in ("msg", "what"):
            if isinstance(data.get(k), str):
                data[k] = m(data[k])
        if not unmasked:
            row[2] = _mask_cargo(data)

    who = sel.person if names else alias(sel.person)
    return {
        "person": who,
        "days": [d.day for d in sel.days],
        "tz": tz,
        "files": [{"name": p.name, "ref": ref if names else ref.replace(shlex.quote(sel.person), shlex.quote(who))}
                  for p, ref in zip(sel.files, sel.file_refs, strict=True)],
        "headers": [m(h.text) for h in sel.headers],
        "builds": builds,
        "masked": not unmasked,
        "lines": {**col, "sources": sources.items, "levels": levels.items, "cats": cats.items,
                  "extra": {str(k): v for k, v in extra.items()}},
        "events": event_rows,
        "glucose": glucose(lines, times),
        "determinations": [_jsonable(d) for d in determinations(lines, times)],
        "doses": [_jsonable(r) for r in dose_rows],
        "basal": basal_timeline(dose_rows),
        "pump_state": pump_state(lines, times),
        "pump_iob": pump_iob(comms),
        "comms": comms,
        "markers": markers(sorted(trio + tandem, key=lambda x: x[1])),
        "findings": findings,
        # what each finding's check means and why it got its severity (checks.KINDS / SEVERITY)
        "finding_kinds": {k: {"title": v.title, "what": v.what, "why": v.why} for k, v in checks.KINDS.items()},
        "severity_scale": checks.SEVERITY,
        "issues": issues,
        "history_packets": sum(1 for _, r in lines if "HistoryLogStreamResponse" in r.msg),
    }


# --------------------------------------------------------------------------- pump history

_STREAM_CARGO = re.compile(r"HistoryLogStreamResponse\(cargo=(?P<cargo>[0-9a-fA-F]*), numberOfHistoryLogs=(?P<n>\d+)")
PUMP_EPOCH = datetime(2008, 1, 1)


def stream_packets(sel: Selection) -> list[tuple[int, str, str]]:
    """``(line, receivedAt, cargo hex)`` of each complete history-log stream packet in the selection."""
    out = []
    for i, (_, r) in enumerate(sel.lines):
        if r.src != "tandem" or "HistoryLogStreamResponse" not in r.msg:
            continue
        m = _STREAM_CARGO.search(r.msg)
        if not m:
            continue
        cargo, n = m["cargo"].lower(), int(m["n"])
        if len(cargo) < 4 or int(cargo[:2], 16) != n or len(cargo) < 2 * (2 + RECORD_BYTES * n):
            continue
        out.append((i, f"{r.ts:%Y-%m-%d %H:%M:%S}.{r.ts.microsecond:06d}", cargo))
    return out


def _split_top(body: str) -> list[str]:
    parts, depth, start = [], 0, 0
    for i, ch in enumerate(body):
        if ch in "{[(":
            depth += 1
        elif ch in "}])":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(body[start:i])
            start = i + 1
    parts.append(body[start:])
    return [p for p in parts if p]


def parse_history_log(s: str) -> dict:
    """``"BolusCompletedHistoryLog[bolusId=1,…,cargo={20,16,…},pumpTimeSec=5,sequenceNum=9]"`` ->
    ``{"type": "BolusCompleted", "seq": 9, "pump_secs": 5, "fields": {"bolusId": "1", …}, "cargo": "1410…"}``."""
    name, _, body = s.partition("[")
    fields: dict[str, str] = {}
    for part in _split_top(body.removesuffix("]")):
        k, _, v = part.partition("=")
        fields[k.strip()] = v
    out: dict = {"type": name.removesuffix("HistoryLog") or name}
    cargo = fields.pop("cargo", "")
    if cargo.startswith("{"):
        try:
            out["cargo"] = bytes(int(b) & 0xFF for b in cargo.strip("{}").split(",") if b).hex()
        except ValueError:
            pass
    for key, field in (("pump_secs", "pumpTimeSec"), ("seq", "sequenceNum")):
        try:
            out[key] = int(fields.pop(field))
        except (KeyError, ValueError):
            pass
    out["fields"] = fields
    return out


def raw_history(cargo: str) -> list[dict]:
    """Records of a stream packet without TandemKit's decoders: type id, pump time, sequence, payload."""
    data = bytes.fromhex(cargo)
    out = []
    for k in range(data[0]):
        rec = data[2 + RECORD_BYTES * k: 2 + RECORD_BYTES * (k + 1)]
        type_id = int.from_bytes(rec[0:2], "little") & 0x0FFF
        out.append({"type": f"type {type_id}", "pump_secs": int.from_bytes(rec[2:6], "little"),
                    "seq": int.from_bytes(rec[6:10], "little"), "fields": {"payload": rec[10:].hex()},
                    "cargo": rec.hex()})
    return out


def history(sel: Selection, exporter: Path | None, *, unmasked: bool = False) -> dict:
    """The pump history logs the selection's stream packets carry, one row per record (by sequence
    number, with how often it was read). Decoded by TandemKit's own classes when ``exporter`` (see
    ``historylog.build_exporter``) is given; otherwise only each record's header is shown."""
    m: Callable[[str], str] = (lambda s: s) if unmasked else mask
    packets = stream_packets(sel)
    times = {line: epoch_ms(sel.lines[line][1].ts, sel.lines[line][1].tz) for line, _, _ in packets}
    decoded: list[tuple[int, list[dict]]] = []
    if exporter is not None and packets:
        proc = subprocess.run([str(exporter)], input="".join(f"{ts}\t{c}\n" for _, ts, c in packets),
                              capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise RuntimeError(f"history decoder exited with {proc.returncode}: {proc.stderr[-500:]}")
        by_cargo: dict[str, collections.deque] = collections.defaultdict(collections.deque)
        for line, _, cargo in packets:
            by_cargo[cargo].append(line)
        for out_line in proc.stdout.splitlines():
            try:
                parsed = json.loads(out_line)["parsed"]
            except (ValueError, KeyError):
                continue
            queue = by_cargo.get(parsed.get("cargoHex", "").lower())
            if not queue:
                continue
            line = queue.popleft()
            decoded.append((line, [parse_history_log(s) for s in parsed.get("params", {}).get("historyLogs", [])]))
    else:
        decoded = [(line, raw_history(cargo)) for line, _, cargo in packets]

    records: dict[tuple, dict] = {}
    for line, recs in decoded:
        for rec in recs:
            key = (rec.get("seq"), rec["type"]) if rec.get("seq") is not None else (line, id(rec))
            if key in records:
                records[key]["reads"] += 1
                continue
            fields = {}
            for k, v in rec["fields"].items():
                fields[k] = m(f"{k}={v}").partition("=")[2]
            pump_time = (PUMP_EPOCH + timedelta(seconds=rec["pump_secs"])).strftime("%Y-%m-%d %H:%M:%S") \
                if rec.get("pump_secs") is not None else None
            records[key] = {"seq": rec.get("seq"), "type": rec["type"], "pump_time": pump_time,
                            "t": times[line], "line": line, "reads": 1, "fields": fields,
                            "cargo": rec.get("cargo", "")}
    rows = sorted(records.values(), key=lambda r: (r["seq"] is None, r["seq"] or 0, r["t"]))
    return {"decoded": exporter is not None, "packets": len(packets), "records": rows}
