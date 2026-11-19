"""Checks over parsed log days. Each yields Findings that point at a log line.

Severity:
  high  Trio and the pump disagree about insulin delivered, or a safety-relevant state.
  warn  Something failed or went missing that should not have.
  info  Expected-but-worth-counting behavior (disconnect-aborted requests, torn log lines).
"""

from __future__ import annotations

import bisect
import collections
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .dataset import Day
from .events import PUMP_EPOCH, Event, response_name


@dataclass
class Finding:
    check: str
    severity: str
    person: str
    day: str
    ts: datetime
    src: str  # which log `lineno` refers to
    lineno: int
    summary: str
    data: dict = field(default_factory=dict)


CHECKS: dict[str, tuple[str, Callable]] = {}  # name -> (description, fn)


def check(name: str, description: str):
    def deco(fn):
        CHECKS[name] = (description, fn)
        return fn
    return deco


class Index:
    """Time-sorted events of one kind for window lookups."""

    def __init__(self, events: list[Event]):
        self.events = sorted(events, key=lambda e: e.ts)
        self.ts = [e.ts for e in self.events]

    def window(self, ts: datetime, before: float, after: float) -> list[Event]:
        lo = bisect.bisect_left(self.ts, ts - timedelta(seconds=before))
        hi = bisect.bisect_right(self.ts, ts + timedelta(seconds=after))
        return self.events[lo:hi]

    def nearest(self, ts: datetime, before: float, after: float) -> Event | None:
        w = self.window(ts, before, after)
        return min(w, key=lambda e: abs((e.ts - ts).total_seconds())) if w else None


def pair(left: list[Event], right: list[Event], before: float, after: float, key: str,
         rkey: str | None = None) -> dict[int, Event]:
    """One-to-one match of ``left`` to ``right`` events from ``before`` s earlier to ``after`` s later.

    Each left event takes the closest unused right event with an equal ``key`` value, else the
    closest unused one. Returns ``{id(left event): right event}``.
    """
    rkey = rkey or key
    ix = Index(right)
    used: set[int] = set()
    out: dict[int, Event] = {}
    for e in sorted(left, key=lambda e: e.ts):
        cands = [r for r in ix.window(e.ts, before, after) if id(r) not in used]
        if not cands:
            continue
        same = [r for r in cands if abs(r.data[rkey] - e.data[key]) < 1e-6]
        r = min(same or cands, key=lambda r: abs((r.ts - e.ts).total_seconds()))
        used.add(id(r))
        out[id(e)] = r
    return out


def _by_kind(events: list[Event], *kinds: str) -> Index:
    return Index([e for e in events if e.kind in kinds])


def _f(check_: str, sev: str, d: Day, e: Event, summary: str, **data) -> Finding:
    return Finding(check_, sev, d.person, d.day, e.ts, e.src, e.lineno, summary, data)


# --------------------------------------------------------------------------- pump commands

CLAIM_WINDOW = 60  # s from a TandemKit "Setting temp basal"/"Enacting bolus" line to its wire request


def _lost(d: Day, e: Event, what: str) -> Finding:
    return _f("log_line_lost", "info", d, e, f"{e.data['name']} sent with no preceding '{what}' line "
              "(line lost from tandem log)", request=e.data["name"])


def pump_temp_basals(d: Day) -> tuple[list[Event], list[Finding]]:
    """``tb_set`` events, plus ones synthesized from SetTempRateRequests whose line was lost."""
    out: list[Event] = []
    lost: list[Finding] = []
    profile = None
    pending: Event | None = None
    for e in d.tandem:
        name = e.data.get("name")
        if e.kind == "pump_response" and name == "CurrentBasalStatusResponse":
            profile = e.data.get("profileBasalRate")
        elif e.kind == "tb_set":
            e.data["profile"] = profile
            out.append(e)
            pending = e
        elif e.kind == "pump_request" and name == "SetTempRateRequest":
            if pending and (e.ts - pending.ts).total_seconds() <= CLAIM_WINDOW:
                pending = None
                continue
            pct = e.data.get("percent")
            if isinstance(pct, int) and isinstance(profile, (int, float)):
                out.append(Event("tb_set", e.ts, e.src, e.lineno,
                                 {"rate": round(pct * profile / 100, 3), "synthetic": True,
                                  "tolerance": profile / 100, "profile": profile}))
            lost.append(_lost(d, e, "Setting temp basal"))
    return out, lost


def pump_boluses(d: Day) -> tuple[list[Event], list[Finding]]:
    """``bolus_enact`` events (with the bolusID sent for them), plus ones synthesized from
    InitiateBolusRequests whose "Enacting bolus" line was lost.

    An InitiateBolusRequest is claimed by the latest unclaimed enact of the same volume (two
    enacts can be in flight at once), else by the latest unclaimed one.
    """
    out: list[Event] = []
    found: list[Finding] = []
    pending: list[Event] = []
    for e in d.tandem:
        name = e.data.get("name")
        pending = [p for p in pending if (e.ts - p.ts).total_seconds() <= CLAIM_WINDOW]
        if e.kind == "bolus_enact":
            if pending:
                found.append(_f("bolus.concurrent_enact", "warn", d, e,
                                f"bolus {e.data['units']:g} U enacted while bolus {pending[-1].data['units']:g} U "
                                "was still being initiated", units=e.data["units"],
                                other_units=pending[-1].data["units"]))
            e.data.pop("bolus_id", None)
            out.append(e)
            pending.append(e)
        elif e.kind == "pump_response" and name == "BolusPermissionResponse" and e.data.get("status"):
            if pending:  # permission refused: the most recent enact is over
                pending.pop()
        elif e.kind == "pump_request" and name == "InitiateBolusRequest":
            bid, vol = e.data.get("bolusID"), e.data.get("totalVolume")
            units = vol / 1000 if isinstance(vol, int) else None
            claim = next((p for p in reversed(pending) if units is not None
                          and abs(p.data["units"] - units) < 1e-6), pending[-1] if pending else None)
            if claim:
                claim.data["bolus_id"] = bid
                pending.remove(claim)
                continue
            if units is not None:
                out.append(Event("bolus_enact", e.ts, e.src, e.lineno,
                                 {"units": units, "bolus_id": bid, "synthetic": True}))
            found.append(_lost(d, e, "Enacting bolus"))
    return out, found


# --------------------------------------------------------------------------- temp basal

# Trio logs a temp basal / bolus outcome after TandemKit finishes the command, so the pump side
# precedes it: look back this far, and forward only by clock-granularity slack.
TB_WINDOW = 120
AFTER_SLACK = 2
MIN_TEMP_RATE = 0.1  # U/hr; lowest non-zero temp rate TandemKit sends
MAX_TEMP_PERCENT = 250  # Tandem temp rates are a percent of the profile rate, capped here


@check("temp_basal", "Trio temp basal outcome vs what TandemKit sent / the pump confirmed")
def temp_basal(d: Day) -> Iterator[Finding]:
    if not (d.trio and d.tandem):
        return
    set_events, lost = pump_temp_basals(d)
    yield from lost
    sets = Index(set_events)
    outcomes = _by_kind(d.tandem, "tb_enacted", "tb_no_change", "tb_blocked")
    basal_status = Index([e for e in d.tandem if e.kind == "pump_response"
                          and e.data.get("name") == "CurrentBasalStatusResponse"])
    trio_tb = _by_kind(d.trio, "trio_temp_basal")
    matched = pair(trio_tb.events, sets.events, TB_WINDOW, AFTER_SLACK, "rate")
    for e in trio_tb.events:
        s = matched.get(id(e))
        ok = e.data["result"] == "succeeded"
        if s is None:
            if ok and d.covered("tandem", e.ts):
                yield _f("temp_basal.unconfirmed", "warn", d, e,
                         f"Trio logged temp basal {e.data['rate']} U/hr succeeded; TandemKit never set one",
                         rate=e.data["rate"])
            continue
        trio_rate, pump_rate = e.data["rate"], s.data["rate"]
        if abs(pump_rate - trio_rate) > s.data.get("tolerance", 1e-6):
            diff = round(trio_rate - pump_rate, 3)
            step = max(0.01, (s.data.get("profile") or 0) / 100)  # one whole percent of the profile rate
            info = {"trio_rate": trio_rate, "pump_rate": pump_rate, "diff": diff,
                    "result": e.data["result"], "profile": s.data.get("profile")}
            if pump_rate == 0 and 0 < trio_rate < MIN_TEMP_RATE:
                yield _f("temp_basal.below_min_rate", "info", d, e,
                         f"Trio {e.data['result']} {trio_rate:g} U/hr; TandemKit set and reported 0 (below the "
                         f"pump's {MIN_TEMP_RATE} U/hr minimum)", **info)
            elif 0 < diff <= step + 1e-9:
                yield _f("temp_basal.rate_rounded_down", "warn", d, e,
                         f"Trio {e.data['result']} {trio_rate:g} U/hr; TandemKit set {pump_rate:g} "
                         f"(rounded down ≤ one step of {step:.3g})", **info)
            else:
                yield _f("temp_basal.rate_mismatch", "high" if diff > 0 else "warn", d, e,
                         f"Trio {e.data['result']} {trio_rate:g} U/hr but TandemKit set {pump_rate:g} U/hr "
                         f"(Trio {'higher' if diff > 0 else 'lower'} by {abs(diff):g})", **info)
        nxt = next((x.lineno for x in sets.events if x.lineno > s.lineno), None)
        enacted = next((o for o in outcomes.events if s.lineno < o.lineno
                        and (nxt is None or o.lineno < nxt) and o.kind == "tb_enacted"), None)
        if ok and enacted and trio_rate >= MIN_TEMP_RATE:
            status = next((r for r in basal_status.window(enacted.ts, 0, TB_WINDOW)
                           if r.lineno > enacted.lineno and (nxt is None or r.lineno < nxt)), None)
            delivered = status.data.get("currentBasalRate") if status else None
            profile = status.data.get("profileBasalRate") if status else None
            if isinstance(delivered, (int, float)) and trio_rate - delivered > max(0.05, trio_rate * 0.1):
                if isinstance(profile, (int, float)) and profile and \
                        abs(delivered - MAX_TEMP_PERCENT / 100 * profile) < 0.01:
                    sub, why = "clamped_max_percent", f"clamped to {MAX_TEMP_PERCENT}% of the pump profile"
                else:
                    # delivered == percent x pump profile, but the percent was computed from another rate
                    sub, why = "pump_profile_differs", "percent computed against a different basal rate"
                # TandemKit reports the clamped rate as the dose, so only the request went unmet;
                # with a differing profile (pre-#500) the reported dose itself was wrong
                yield _f(f"temp_basal.{sub}", "warn" if sub == "clamped_max_percent" else "high", d, e,
                         f"Trio recorded temp basal {trio_rate:g} U/hr; pump then reported delivering "
                         f"{delivered:g} U/hr (pump profile {profile} U/hr: {why})",
                         trio_rate=trio_rate, pump_current=delivered, pump_profile=profile)
        if not ok and enacted:
            yield _f("temp_basal.failed_but_enacted", "high", d, e,
                     f"Trio logged temp basal {e.data['rate']} U/hr FAILED but TandemKit logged "
                     f"'Temp basal enacted!' for it at {enacted.ts:%H:%M:%S}", rate=e.data["rate"])
    paired = {id(s) for s in matched.values()}
    for s in sets.events:
        if id(s) not in paired and d.covered("trio", s.ts, 0):
            yield _f("temp_basal.no_trio_outcome", "warn", d, s,
                     f"TandemKit set temp basal {s.data['rate']} U/hr; Trio logged no outcome",
                     rate=s.data["rate"])
    for r in d.tandem:
        if r.kind == "pump_response" and r.data.get("name") == "SetTempRateResponse" and r.data.get("status"):
            yield _f("temp_basal.pump_rejected", "warn", d, r,
                     f"SetTempRateResponse status={r.data['status']}", status=r.data["status"])


# --------------------------------------------------------------------------- bolus

BOLUS_WINDOW = 180


def _last_bolus_status(e: Event) -> dict | None:
    d = e.data
    if d.get("name") == "LastBolusStatusV2Response" and "bolusId" in d:
        return {"id": d["bolusId"], "delivered": d.get("deliveredVolume"),
                "requested": d.get("requestedVolume"), "status": d.get("bolusStatusId"),
                "end_reason": None}
    if d.get("name") == "LastBolusStatusV3Response" and "standardBolusId" in d:
        return {"id": d["standardBolusId"], "delivered": d.get("standardBolusDeliveredVolume"),
                "requested": d.get("standardBolusRequestedVolume"),
                "status": d.get("standardBolusStatusId"), "end_reason": d.get("standardBolusEndReasonId")}
    return None


# LastBolusStatusAbstractResponseBolusStatus; LastBolusStatusV3's endReason uses the same codes.
BOLUS_END = {0: "stopped by user", 1: "stopped by alarm", 2: "stopped by malfunction", 3: "complete",
             4: "stopped over BLE", 5: "rejected over BLE", 6: "terminated by PLGS"}


@check("bolus", "Trio bolus outcome vs TandemKit enact / pump-reported delivery")
def bolus(d: Day) -> Iterator[Finding]:
    if d.tandem and d.trio:
        enacts, lost = pump_boluses(d)
        trio_bolus = [e for e in d.trio if e.kind == "trio_bolus"]
        matched = pair(trio_bolus, enacts, BOLUS_WINDOW, AFTER_SLACK, "units")
        for e in trio_bolus:
            s = matched.get(id(e))
            if s is None:
                if e.data["result"] == "succeeded" and d.covered("tandem", e.ts):
                    yield _f("bolus.unconfirmed", "warn", d, e,
                             f"Trio logged bolus {e.data['units']} U succeeded; TandemKit never enacted one",
                             units=e.data["units"])
                continue
            if abs(s.data["units"] - e.data["units"]) > 1e-6:
                yield _f("bolus.units_mismatch", "high", d, e,
                         f"Trio bolus {e.data['units']} U {e.data['result']} but TandemKit enacted {s.data['units']} U",
                         trio_units=e.data["units"], pump_units=s.data["units"])
            if e.data["result"] == "failed" and s.data.get("bolus_id") is not None:
                resp = next((r for r in d.tandem if r.kind == "pump_response" and r.lineno > s.lineno
                             and r.data.get("name") == "InitiateBolusResponse"
                             and r.data.get("bolusId") == s.data["bolus_id"]), None)
                if resp and resp.data.get("status") == 0 and "succ" in str(resp.data.get("statusType", "")).lower():
                    yield _f("bolus.failed_but_initiated", "high", d, e,
                             f"Trio logged bolus {e.data['units']} U FAILED but the pump accepted it "
                             f"(InitiateBolusResponse bolusId={resp.data.get('bolusId')})", units=e.data["units"])
    else:
        lost = pump_boluses(d)[1] if d.tandem else []
    yield from lost
    if not d.tandem:
        return
    final: dict[int, tuple[Event, dict]] = {}  # last pump-reported status per bolus id
    cancels = []
    for e in d.tandem:
        if e.kind == "pump_response":
            if st := _last_bolus_status(e):
                final[st["id"]] = (e, st)
            if e.data.get("name") == "BolusPermissionResponse" and e.data.get("nackReason") not in (
                    None, "permissionGranted"):
                yield _f("bolus.permission_denied", "info", d, e,
                         f"BolusPermission nack: {e.data.get('nackReason')}", nack=e.data.get("nackReason"))
        if e.kind == "bolus_cancel" or (e.kind == "pump_request" and e.data.get("name") == "CancelBolusRequest"):
            cancels.append(e.ts)
    cancels += [e.ts for e in d.trio if e.kind == "trio_bolus_cancel"]
    for bid, (e, st) in final.items():
        if st["delivered"] is None or st["requested"] is None or st["delivered"] >= st["requested"]:
            continue
        code = st["end_reason"] if st["end_reason"] is not None else st["status"]
        reason = BOLUS_END.get(code, f"code {code}")
        cancelled = any(abs((c - e.ts).total_seconds()) < 3600 for c in cancels)
        sev = "info" if code in (0, 4) and cancelled else "warn"
        yield _f("bolus.stopped_early", sev, d, e,
                 f"bolusId={bid} delivered {st['delivered'] / 1000:g} of {st['requested'] / 1000:g} U: "
                 f"{reason}{'' if cancelled else ' (no cancel in logs)'}",
                 **st, reason=reason, cancelled=cancelled)


# --------------------------------------------------------------------------- requests

RESPONSE_TIMEOUT = 30  # TandemKit's own write timeout


@check("pump_request", "Pump request outcomes: late, timed out, lost log lines, missing")
def pump_request(d: Day) -> Iterator[Finding]:
    """TandemKit serializes pump writes (one command queue, one pending-write slot), and every write
    ends in exactly one logged outcome: a response, "Timeout hit", "Write cancelled", or "Pump
    refused". So a request followed by the next request with no outcome line in between had its
    outcome line lost from the log, not a concurrent duplicate.
    """
    pending: Event | None = None
    for e in d.tandem:
        if e.kind == "pump_request":
            if pending is not None:
                yield _f("pump_request.outcome_line_lost", "info", d, pending,
                         f"{pending.data['name']}: next request written with no outcome logged for this one",
                         request=pending.data["name"])
            pending = e
        elif pending is None:
            continue
        elif e.kind == "pump_response" and e.data["name"] == response_name(pending.data["name"]):
            latency = (e.ts - pending.ts).total_seconds()
            if latency > RESPONSE_TIMEOUT:
                yield _f("pump_request.late", "warn", d, pending,
                         f"{pending.data['name']} answered after {latency:.0f}s with no timeout logged",
                         request=pending.data["name"], latency_s=latency)
            pending = None
        elif e.kind in ("timeout", "write_cancelled", "refused"):
            if e.kind == "timeout":
                yield _f("pump_request.timeout", "warn", d, pending,
                         f"{pending.data['name']} timed out after {(e.ts - pending.ts).total_seconds():.0f}s",
                         request=pending.data["name"])
            pending = None
        elif e.kind == "disconnected":
            yield _f("pump_request.disconnect", "info", d, pending,
                     f"{pending.data['name']} cut off by a disconnect", request=pending.data["name"])
            pending = None
    if pending is not None:
        yield _f("pump_request.missing", "warn", d, pending,
                 f"{pending.data['name']}: no outcome logged before the log ends", request=pending.data["name"])


# --------------------------------------------------------------------------- connection

STORM_CONNECTS = 6
STORM_WINDOW = timedelta(minutes=60)


@check("reconnect_storm", f">= {STORM_CONNECTS} pump connects within {STORM_WINDOW}")
def reconnect_storm(d: Day) -> Iterator[Finding]:
    conns = [e for e in d.tandem if e.kind == "connected"]
    i = 0
    while i < len(conns):
        j = i
        while j + 1 < len(conns) and conns[j + 1].ts - conns[i].ts <= STORM_WINDOW:
            j += 1
        if j - i + 1 >= STORM_CONNECTS:
            # extend the episode while the rate stays high
            end = j
            while end + 1 < len(conns) and conns[end + 1].ts - conns[end + 1 - STORM_CONNECTS + 1].ts <= STORM_WINDOW:
                end += 1
            errs = collections.Counter(
                e.data["error"] for e in d.tandem
                if e.kind == "disconnected" and conns[i].ts <= e.ts <= conns[end].ts)
            top = ", ".join(f"{k} ×{v}" for k, v in errs.most_common(2))
            yield _f("reconnect_storm", "warn", d, conns[i],
                     f"{end - i + 1} connects {conns[i].ts:%H:%M}–{conns[end].ts:%H:%M}; disconnects: {top}",
                     connects=end - i + 1, minutes=round((conns[end].ts - conns[i].ts).total_seconds() / 60))
            i = end + 1
        else:
            i += 1


# --------------------------------------------------------------------------- pump clock

CLOCK_DRIFT_S = 120


@check("pump_clock", f"Pump clock vs phone clock off by > {CLOCK_DRIFT_S}s")
def pump_clock(d: Day) -> Iterator[Finding]:
    flagged = False
    for e in d.tandem:
        if e.kind == "pump_response" and e.data.get("name") == "TimeSinceResetResponse":
            t = e.data.get("currentTime")
            if not isinstance(t, int):
                continue
            drift = ((PUMP_EPOCH + timedelta(seconds=t)) - e.ts).total_seconds()
            if abs(drift) > CLOCK_DRIFT_S:
                if not flagged:
                    big = abs(drift) > 86400
                    yield _f("pump_clock.drift", "high" if big else "warn", d, e,
                             f"pump clock off by {drift / 86400:.2f} days" if big else
                             f"pump clock off by {drift:+.0f}s",
                             drift_s=drift, since_reset=e.data.get("pumpTimeSinceReset"))
                flagged = True
            else:
                flagged = False
        elif e.kind == "pump_request" and e.data.get("name") == "ChangeTimeDateRequest":
            yield _f("pump_clock.set_time", "info", d, e, "TandemKit set the pump's time/date")


# --------------------------------------------------------------------------- log health

@check("torn_lines", "TandemKit log lines overwritten by a concurrent write")
def torn_lines(d: Day) -> Iterator[Finding]:
    torn = [e for e in d.tandem if e.kind == "torn"]
    if torn:
        yield _f("torn_lines", "info", d, torn[0], f"{len(torn)} torn line(s) in tandem log",
                 count=len(torn))


@check("app_restart", "Trio launched more than 3 times in a day")
def app_restart(d: Day) -> Iterator[Finding]:
    starts = [e for e in d.trio if e.kind == "app_start"]
    if len(starts) > 3:
        yield _f("app_restart", "info", d, starts[0], f"Trio started {len(starts)} times",
                 count=len(starts))


# --------------------------------------------------------------------------- per-person

LOOP_GAP = timedelta(minutes=15)


def loop_gaps(days: list[Day]) -> Iterator[Finding]:
    """Gaps between successful loops, with what was going on during them."""
    prev: tuple[Day, Event] | None = None
    for d in days:
        if not d.trio:
            prev = None
            continue
        failures = Index([e for e in d.trio if e.kind == "loop_failed"])
        glucose = Index([e for e in d.trio if e.kind == "glucose"])
        disc = Index([e for e in d.tandem if e.kind == "disconnected"])
        for e in d.trio:
            if e.kind != "loop_success":
                continue
            if prev and LOOP_GAP < e.ts - prev[1].ts < timedelta(hours=24):
                start = prev[1].ts
                gap = e.ts - start
                secs = gap.total_seconds()
                mid = start + gap / 2
                fails = collections.Counter(f.data["error"][:120] for f in failures.window(mid, secs / 2, secs / 2))
                notes = []
                if fails:
                    notes.append("failures: " + "; ".join(f"{k} ×{v}" for k, v in fails.most_common(2)))
                if not glucose.window(mid, secs / 2 - 60, secs / 2 - 60):
                    notes.append("no new glucose")
                if n := len(disc.window(mid, secs / 2, secs / 2)):
                    notes.append(f"{n} pump disconnects")
                silent = [m for m in range(int(secs // 60)) if not d.covered("trio", start + timedelta(minutes=m), 0)]
                if len(silent) > 10:
                    notes.append(f"Trio log silent ~{len(silent)} min (app not running?)")
                mins = round(secs / 60)
                yield Finding("loop_gap", "warn" if mins >= 30 else "info", d.person, d.day, e.ts, "trio",
                              e.lineno, f"no successful loop for {mins} min "
                              f"({start:%H:%M}–{e.ts:%H:%M}); {'; '.join(notes) or 'no obvious cause'}",
                              {"minutes": mins, "failures": dict(fails)})
            prev = (d, e)


PERSON_CHECKS: dict[str, tuple[str, Callable]] = {
    "loop_gap": (f"No successful loop for > {LOOP_GAP}", loop_gaps),
}
