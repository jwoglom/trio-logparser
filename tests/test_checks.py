"""Checks over synthetic logs. Every log line here is made up."""

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from trio_logparser import builds, checks, known
from trio_logparser.dataset import Day
from trio_logparser.events import extract, fields
from trio_logparser.parse import TORN, parse_tandem, parse_trio

DAY = "2026-09-20"


def trio(t: str, msg: str, level: str = "DEV", func: str = "fn()") -> str:
    return f"{DAY}T{t}-0400 [ApsManager] APSManager.swift - {func} - 1 - {level}: {msg}\n"


def tandem(t: str, msg: str, level: str = "INFO") -> str:
    return f"[{DAY}T{t}-0400 {level}] TandemPumpManager.swift - fn()#1: {msg}\n"


def write(t: str, name: str, body: str = "") -> str:
    return tandem(t, f"Writing message: {name}(cargo={', ' + body if body else ''})")


def recv(t: str, name: str, body: str = "") -> str:
    return tandem(t, f"Received message: {name}(cargo=00{', ' + body if body else ''})")


def day(tmp_path: Path, trio_lines: list[str] = (), tandem_lines: list[str] = ()) -> Day:
    d = Day("Pat Doe", DAY)
    if trio_lines:
        d.trio_path = tmp_path / f"{DAY}_log.txt"
        d.trio_path.write_text("".join(trio_lines))
    if tandem_lines:
        d.tandem_path = tmp_path / f"{DAY}_tandem_log.txt"
        d.tandem_path.write_text("".join(tandem_lines))
    return d.load()


def run(check: str, d: Day) -> list[checks.Finding]:
    return list(checks.CHECKS[check][1](d))


def kinds(findings) -> list[str]:
    return [f.check for f in findings]


# --------------------------------------------------------------------------- parsing

def test_trio_continuations_and_watch_lines(tmp_path):
    p = tmp_path / "log.txt"
    p.write_text(
        "  orphan continuation\n"
        + trio("00:00:01", "OREF DETERMINATION: {")
        + '  "rate": 1\n}\n'
        + f"[{DAY}T00:00:02-0400] [WatchState.swift:428] finalize() → ⌚️ msg\n"
    )
    recs = list(parse_trio(p))
    assert [r.level for r in recs] == ["ORPHAN", "DEV", "WATCH"]
    assert recs[1].extra == ['  "rate": 1', "}"]
    assert recs[2].lineno == 5


def test_tandem_torn_line_keeps_previous_timestamp(tmp_path):
    p = tmp_path / "tandem_log.txt"
    p.write_text(tandem("00:00:05", "Connected to pump: Tandem Mobi 123!") + "(30)\n")
    recs = list(parse_tandem(p))
    assert recs[1].level == TORN and recs[1].ts == recs[0].ts and recs[1].msg == "(30)"


def test_fields_unwrap_optionals_and_keep_message_name(tmp_path):
    assert fields("cargo=ab, status=0, nack=Optional(TandemKit.X.Y.permissionGranted), r=0.5") == {
        "status": 0, "nack": "permissionGranted", "r": 0.5}
    p = tmp_path / "tandem_log.txt"
    p.write_text(recv("00:00:01", "IDPSettingsResponse", "idpId=0, name=regular"))
    [e] = list(extract(parse_tandem(p)))
    assert e.data["name"] == "IDPSettingsResponse"


# --------------------------------------------------------------------------- temp basal

def basal_status(t: str, profile: float = 0.6) -> str:
    return recv(t, "CurrentBasalStatusResponse", f"profileBasalRate={profile}, currentBasalRate={profile}")


@pytest.mark.parametrize("trio_rate, pump_rate, expected", [
    (0.5, 0.5, []),
    (0.46, 0.45, ["temp_basal.rate_rounded_down"]),
    (0.05, 0.0, ["temp_basal.below_min_rate"]),
    (0.5, 0.8, ["temp_basal.rate_mismatch"]),
])
def test_temp_basal_rates(tmp_path, trio_rate, pump_rate, expected):
    d = day(tmp_path,
            [trio("00:05:02", f"Temp basal succeeded: {trio_rate} for: 1800.0")],
            [basal_status("00:05:00"), tandem("00:05:01", f"Setting temp basal at {pump_rate}U/hr for 1800.0s"),
             write("00:05:01", "SetTempRateRequest", "minutes=30, percent=75"),
             recv("00:05:01", "SetTempRateResponse", "status=0, tempRateId=1"),
             tandem("00:05:01", "Temp basal enacted!")])
    assert kinds(run("temp_basal", d)) == expected


@pytest.mark.parametrize("profile, percent, current, expected", [
    (1.0, 250, 2.5, ["temp_basal.clamped_max_percent"]),
    (0.0, 133, 0.0, ["temp_basal.pump_profile_differs"]),
    (1.0, 150, 1.5, []),
])
def test_temp_basal_delivery_readback(tmp_path, profile, percent, current, expected):
    rate = 3.0 if percent == 250 else 1.5
    d = day(tmp_path,
            [trio("00:05:02", f"Temp basal succeeded: {rate} for: 1800.0")],
            [basal_status("00:05:00", profile), tandem("00:05:01", f"Setting temp basal at {rate}U/hr for 1800.0s"),
             write("00:05:01", "SetTempRateRequest", f"minutes=30, percent={percent}"),
             tandem("00:05:01", "Temp basal enacted!"),
             recv("00:05:02", "CurrentBasalStatusResponse",
                  f"profileBasalRate={profile}, currentBasalRate={current}")])
    assert kinds(run("temp_basal", d)) == expected


def test_failed_attempt_is_not_credited_with_retry_enact(tmp_path):
    d = day(tmp_path,
            [trio("00:05:03", "Temp basal failed: 0.0 for: 3600.0"),
             trio("00:05:20", "Temp basal succeeded: 0.0 for: 3600.0")],
            [tandem("00:05:01", "Setting temp basal at 0.0U/hr for 3600.0s"),
             tandem("00:05:03", "Failed to connect for temp basal: Pump not connected", "ERROR"),
             tandem("00:05:18", "Setting temp basal at 0.0U/hr for 3600.0s"),
             write("00:05:19", "SetTempRateRequest", "minutes=60, percent=0"),
             tandem("00:05:19", "Temp basal enacted!")])
    assert kinds(run("temp_basal", d)) == []


def test_failed_but_enacted(tmp_path):
    d = day(tmp_path,
            [trio("00:05:03", "Temp basal failed: 0.5 for: 1800.0")],
            [tandem("00:05:01", "Setting temp basal at 0.5U/hr for 1800.0s"),
             write("00:05:01", "SetTempRateRequest", "minutes=30, percent=83"),
             tandem("00:05:02", "Temp basal enacted!")])
    assert kinds(run("temp_basal", d)) == ["temp_basal.failed_but_enacted"]


def test_lost_setting_line_falls_back_to_wire_request(tmp_path):
    d = day(tmp_path,
            [trio("00:05:02", "Temp basal succeeded: 1.5 for: 1800.0")],
            [basal_status("00:05:00", 1.0), write("00:05:01", "SetTempRateRequest", "minutes=30, percent=150"),
             tandem("00:05:01", "Temp basal enacted!")])
    assert kinds(run("temp_basal", d)) == ["log_line_lost"]


# --------------------------------------------------------------------------- bolus

def test_bolus_paired_by_amount_when_smb_follows_manual(tmp_path):
    d = day(tmp_path,
            [trio("00:10:02", "Bolus succeeded: 1.7"), trio("00:10:02", "Bolus failed: 0.3")],
            [tandem("00:10:00", "Enacting bolus - units 1.7U"),
             recv("00:10:00", "BolusPermissionResponse", "status=0, bolusId=7"),
             write("00:10:01", "InitiateBolusRequest", "totalVolume=1700, bolusID=7"),
             recv("00:10:01", "InitiateBolusResponse", "status=0, bolusId=7, statusType=Optional(X.success)"),
             tandem("00:10:01", "Enacting bolus - units 0.3U"),
             recv("00:10:01", "BolusPermissionResponse", "status=1, bolusId=0"),
             tandem("00:10:01", "Pump rejected bolus permission request", "ERROR")])
    assert kinds(run("bolus", d)) == []


def test_concurrent_bolus_enact(tmp_path):
    d = day(tmp_path, [], [
        tandem("00:10:00", "Enacting bolus - units 2.0U"),
        tandem("00:10:00", "Enacting bolus - units 0.3U"),
        recv("00:10:01", "BolusPermissionResponse", "status=0, bolusId=7"),
        write("00:10:01", "InitiateBolusRequest", "totalVolume=2000, bolusID=7"),
    ])
    assert kinds(run("bolus", d)) == ["bolus.concurrent_enact"]


@pytest.mark.parametrize("status, cancel, expected_sev", [(1, False, "warn"), (0, True, "info")])
def test_bolus_stopped_early(tmp_path, status, cancel, expected_sev):
    lines = [recv("00:20:00", "LastBolusStatusV2Response",
                  f"status=1, bolusId=9, deliveredVolume=500, bolusStatusId={status}, requestedVolume=2000")]
    if cancel:
        lines.insert(0, write("00:19:00", "CancelBolusRequest"))
    [f] = run("bolus", day(tmp_path, [], lines))
    assert f.check == "bolus.stopped_early" and f.severity == expected_sev
    assert ("alarm" in f.summary) == (status == 1)


# --------------------------------------------------------------------------- requests

def test_pump_request_outcomes(tmp_path):
    d = day(tmp_path, [], [
        write("01:00:00", "PumpGlobalsRequest"), recv("01:00:50", "PumpGlobalsResponse"),          # late
        write("02:00:00", "HistoryLogStatusRequest"),                                              # outcome lost
        write("02:00:01", "HistoryLogStatusRequest"), recv("02:00:01", "HistoryLogStatusResponse"),
        write("03:00:00", "AlertStatusRequest"),
        tandem("03:00:30", "Timeout hit, no response from pump...", "ERROR"),                      # timeout
        write("04:00:00", "InsulinStatusRequest"),
        tandem("04:00:01", "Device disconnected, name: x, error: timed out"),                      # disconnect
        write("05:00:00", "MalfunctionStatusRequest"), recv("05:00:00", "MalfunctionBitmaskStatusResponse"),
        write("06:00:00", "AlarmStatusRequest"),                                                   # missing
    ])
    assert kinds(run("pump_request", d)) == [
        "pump_request.late", "pump_request.outcome_line_lost", "pump_request.timeout",
        "pump_request.disconnect", "pump_request.missing"]


# --------------------------------------------------------------------------- misc checks

def test_reconnect_storm_and_pump_clock(tmp_path):
    lines = [tandem(f"0{h // 60}:{h % 60:02}:00", "Connected to pump: x!") for h in range(0, 60, 8)]
    # pump reports 2026-09-20 00:30:00 minus 2^32 ms
    pump_secs = int((datetime(2026, 9, 20, 0, 30) - datetime(2008, 1, 1)).total_seconds()) - 4294967
    lines.append(recv("00:30:00", "TimeSinceResetResponse", f"currentTime={pump_secs}, pumpTimeSinceReset=300"))
    d = day(tmp_path, [], sorted(lines))
    assert kinds(run("reconnect_storm", d)) == ["reconnect_storm"]
    [f] = run("pump_clock", d)
    assert f.check == "pump_clock.drift" and f.severity == "high" and "49.71 days" in f.summary


def test_loop_gap_reports_causes(tmp_path):
    d = day(tmp_path, [
        trio("01:00:00", "Loop succeeded"),
        trio("01:05:00", 'error("Loop failed with error: connection(pumpNotConnected)")', "WARN",
             "finalizeLoop(error:)"),
        trio("01:40:00", "Loop succeeded"),
    ])
    [f] = checks.loop_gaps([d])
    assert f.check == "loop_gap" and f.data["minutes"] == 40 and "pumpNotConnected" in f.summary


# --------------------------------------------------------------------------- builds & known issues

def test_builds_inherit_across_days(tmp_path):
    start = trio("08:00:00", "Trio Started: v1.0(1) [buildDate: x] [Branch: dev abc1234] "
                 "[submodules: LoopKit: detached 1111111, TandemKit: detached 2222222]")
    d1 = day(tmp_path, [trio("01:00:00", "Loop succeeded"), start])
    d2 = Day("Pat Doe", "2026-09-21")
    builds.assign([d1, d2])
    b = builds.build_at(d1, d1.trio[-1].ts)
    assert (b.branch, b.trio_commit, b.tandemkit) == ("dev", "abc1234", "2222222")
    assert builds.build_at(d1, d1.trio[0].ts) is None
    assert builds.build_at(d2, d1.trio[-1].ts).tandemkit == "2222222"


def test_registry_loads_and_scans():
    issues = known.load()
    assert all(i.strength in ("definitive", "suggestive") for i in issues if i.signature)
    hits = known.scan("tandem", "Not handler for message - data: 00aaa7aa07 characteristic: 7B83FFFC-xx")
    assert ("suspend-30s-stall-wrong-response-type", "signature") in hits
    other = known.scan("tandem", "Not handler for message - data: 00aaa7aa07 characteristic: 7B83FFF6-xx")
    assert other == [("unsolicited-current-status-responses", "signature")]


def test_streak_episodes():
    from datetime import datetime
    from types import SimpleNamespace as H

    issue = known.by_id()["stale-fast-path-reconnect"]
    t0 = datetime(2026, 9, 20)
    hits = [H(what="signature", ts=t0 + timedelta(minutes=m)) for m in (0, 1, 2)]
    hits += [H(what="signature", ts=t0 + timedelta(minutes=5)), H(what="marker", ts=t0 + timedelta(minutes=6)),
             H(what="signature", ts=t0 + timedelta(minutes=7))]
    assert [len(ep) for ep in known.episodes(issue, hits)] == [4]


# --------------------------------------------------------------------------- "Trio Logs" export

def export(lines: list[str]) -> str:
    """A made-up Trio 1.0.2 log export: header sections, then the merged log."""
    return "Trio Logs\nCreated: 2026-09-21 00:00:00 -0400\n\n== App ==\nTrio: 1.0.2 (1)\n\n== Log ==\n" + "".join(lines)


def xtrio(t: str, msg: str, cat: str = "Loop", level: str = "") -> str:
    return f"{DAY} {t}.000 -0400 Trio {level + ': ' if level else ''}[{cat}] {msg}\n"


def xpump(t: str, msg: str, level: str = "INFO") -> str:
    return f"{DAY} {t}.000 -0400 Pump [delegate] [{level}] TandemPumpManager.swift - fn()#1: {msg}\n"


def test_export_parses_sources_levels_and_continuations(tmp_path):
    from trio_logparser.dataset import from_export
    from trio_logparser.parse import is_export, parse_export

    p = tmp_path / "Trio Logs.txt"
    p.write_text(export([
        xtrio("10:00:00", "Trio started: v1.0.2.9 (1) feat/x abc1234; LoopKit 1111111, TandemKit def5678", "Lifecycle"),
        xtrio("10:00:01", "Loop failed: deviceState(x)", level="WARNING"),
        "    continued\n",
        xpump("10:00:02", "Writing message: SetTempRateRequest(cargo=00, minutes=30, percent=50)"),
        f"{DAY} 10:00:03.000 -0400 CGM [connection] Scanning for sensor\n",
        f"{DAY} 10:00:04.000 -0400 Algorithm enacted · BG 100\n",
    ]))
    assert is_export(p)
    recs = list(parse_export(p))
    assert [(r.src, r.level) for r in recs] == [("trio", "INFO"), ("trio", "WARNING"), ("tandem", "INFO"),
                                                ("trio", "INFO"), ("trio", "INFO")]
    assert recs[1].extra == ["continued"] and recs[2].lineno == 11
    (d,) = from_export(p, "Pat Doe")
    start = next(e for e in d.trio if e.kind == "app_start")
    assert (start.data["branch"], start.data["commit"], start.data["tandemkit"]) == ("feat/x", "abc1234", "def5678")
    assert [e.kind for e in d.trio if e.kind in ("loop_failed", "problem")] == ["loop_failed", "problem"]
    assert d.tandem[0].data["name"] == "SetTempRateRequest"


def test_export_temp_basal_and_bolus_outcomes(tmp_path):
    from trio_logparser.dataset import from_export

    p = tmp_path / "Trio Logs.txt"
    p.write_text(export([
        xpump("10:00:00", "Setting temp basal at 0.8U/hr for 1800.0s"),
        xpump("10:00:01", "Temp basal enacted!"),
        xtrio("10:00:01", "Temp basal enacted: 0.85 U/h for 30 min"),
        xpump("10:00:02", "Enacting bolus - units 0.3U"),
        xtrio("10:00:05", "SMB enacted: 0.3 U"),
        xpump("10:05:00", "Setting temp basal at 1.0U/hr for 1800.0s"),
        xpump("10:05:00", "Temp basal enacted!"),
        xtrio("10:05:00", "Temp basal command failed: 1.0 U/h for 30 min", "Pump"),
        xtrio("10:06:00", "Enacting bolus 2.0 U, manual", "Bolus"),
        xpump("10:06:00", "Enacting bolus - units 2.0U"),
        xtrio("10:06:02", "Bolus enacted: 2.0 U, manual", "Bolus"),
        xtrio("10:07:00", "Loop finished in 1.2 s"),
    ]))
    (d,) = from_export(p, "Pat Doe")
    tb = [e.data for e in d.trio if e.kind == "trio_temp_basal"]
    assert [(t["result"], t["rate"], t["duration_min"]) for t in tb] == [("succeeded", 0.85, 30), ("failed", 1.0, 30)]
    assert [(e.data["units"], e.data.get("manual")) for e in d.trio if e.kind == "trio_bolus"] == [(0.3, None), (2.0, True)]
    found = kinds(run("temp_basal", d))
    assert "temp_basal.rate_mismatch" in found or "temp_basal.rate_rounded_down" in found
    assert "temp_basal.failed_but_enacted" in found
    assert kinds(run("bolus", d)) == []
    assert [e.kind for e in d.trio if e.kind == "loop_success"] == ["loop_success"]
