"""Checks over synthetic logs. Every log line here is made up."""

import struct
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


# --------------------------------------------------------------------------- dose dates & pump history

RAW_BOLUS = "0101" + "78563412" + "01010000"  # HistoryLogSyncIdentifier: bolus, serial 0x12345678, bolusId 257
RAW_EXTENDED = "0104" + "78563412" + "01010000"


def misdated(t: str, kind: str, raw: str, problem: str, sent: str) -> str:
    return tandem(t, f"Misdated {kind} {raw} {problem}; sent as {sent}", "ERROR")


def start_ahead(start: str, phone: str = "2026-09-20 04:10:00 +0000") -> tuple[str, str]:
    return f"starts at {start}, ahead of the phone's {phone}", f"{phone} to {start}"


def history_record(type_id: int, pump_time: datetime, seq: int, payload: bytes) -> bytes:
    secs = int((pump_time - datetime(2008, 1, 1)).total_seconds())
    return struct.pack("<HII", 0x1000 | type_id, secs, seq) + payload.ljust(16, b"\0")


def bolus_completed(bolus_id: int, units: float, pump_time: datetime, seq: int = 1) -> bytes:
    return history_record(20, pump_time, seq, struct.pack("<HHfff", 3, bolus_id, 0.0, units, units))


def bolex_activated(bolus_id: int, units: float, pump_time: datetime, seq: int = 2) -> bytes:
    return history_record(59, pump_time, seq, struct.pack("<HBxff", bolus_id, 1, 0.0, units))


def stream_message(*records: bytes) -> str:
    cargo = (bytes([len(records), 7]) + b"".join(records) + b"\xab\xcd").hex()
    return (f"Received stream message: HistoryLogStreamResponse(cargo={cargo}, "
            f"numberOfHistoryLogs={len(records)}, streamId=7, historyLogStreamBytes=[], historyLogs=[])")


def stream(t: str, *records: bytes) -> str:
    return tandem(t, stream_message(*records))


def test_bolus_records_decode_completions_and_extended_parts():
    from trio_logparser.historylog import bolus_records

    when = datetime(2026, 3, 1, 8, 30)
    basal = history_record(279, when, 3, b"")
    cargo = (bytes([3, 7]) + bolus_completed(257, 1.25, when) + basal + bolex_activated(258, 0.6, when)).hex()
    secs = int((when - datetime(2008, 1, 1)).total_seconds())
    assert bolus_records(cargo) == [
        {"type": 20, "bolus_id": 257, "pump_secs": secs, "seq": 1, "units": 1.25},
        {"type": 59, "bolus_id": 258, "pump_secs": secs, "seq": 2, "units": 0.6}]
    assert bolus_records(cargo[:-8]) == [] and bolus_records("zz") == []


@pytest.mark.parametrize("kind, start, expected", [
    ("bolus", "2030-01-01 00:00:00 +0000", ("misdated_dose.start_ahead", "high")),
    ("tempBasal", "2030-01-01 00:00:00 +0000", ("misdated_dose.start_ahead", "high")),
    ("bolus", "2026-09-20 04:15:00 +0000", ("misdated_dose.start_ahead", "warn")),  # a 5 min nudge
    ("suspend", "2030-01-01 00:00:00 +0000", ("misdated_dose.start_ahead", "warn")),
])
def test_misdated_dose_start_ahead(tmp_path, kind, start, expected):
    [f] = run("misdated_dose", day(tmp_path, [], [misdated("00:10:00", kind, RAW_BOLUS, *start_ahead(start))]))
    assert (f.check, f.severity) == expected
    assert "bolusId 257" in f.summary and "78563412" not in f.summary and RAW_BOLUS not in str(f.data)
    assert ("ending 2030-01-01" in f.summary) == start.startswith("2030")


def test_misdated_dose_is_one_finding_per_dose_however_often_resent(tmp_path):
    problem, sent = start_ahead("2030-01-01 00:00:00 +0000")
    d = day(tmp_path, [], [misdated("00:10:00", "bolus", RAW_BOLUS, problem, sent)]
            + [misdated(f"00:{m}:00", "bolus", RAW_EXTENDED, problem, sent) for m in (10, 15, 20)])
    found = run("misdated_dose", d)
    assert [(f.data["dose"], f.data["sends"]) for f in found] == [("bolusId 257", 1), ("extended bolusId 257", 3)]
    assert "(3 times)" in found[1].summary


def test_misdated_dose_other_repairs_and_unknown_wording(tmp_path):
    sent = "2026-09-20 04:00:00 +0000 to 2026-09-20 04:00:00 +0000"
    d = day(tmp_path, [], [
        misdated("00:10:00", "tempBasal", "", "ends at 2026-09-20 04:00:00 +0000, "
                 "before it starts at 2026-09-20 04:30:00 +0000", sent),
        misdated("00:11:00", "bolus", RAW_BOLUS, "delivered -0.5 U", sent),
        misdated("00:12:00", "resume", "0103" + "78563412" + "09000000", "is upside down", sent),
    ])
    found = run("misdated_dose", d)
    assert kinds(found) == ["misdated_dose.ends_before_start", "misdated_dose.negative_delivery", "misdated_dose.other"]
    assert {f.severity for f in found} == {"warn"}
    assert "ended 30 min before it started" in found[0].summary and "no pump id" in found[0].summary
    assert "history record 9" in found[2].summary


@pytest.mark.parametrize("pump_time, expected", [
    (datetime(2026, 3, 1, 8, 30), ["history_bolus.stale"]),   # 203 days before the phone's clock
    (datetime(2033, 1, 1, 0, 0), ["history_bolus.stale"]),    # a pump clock years ahead
    (datetime(2026, 9, 19, 22, 0), []),                       # a pump-button bolus two hours ago
])
def test_history_bolus_report_after_stale_records(tmp_path, pump_time, expected):
    d = day(tmp_path, [], [
        tandem("00:10:00", "Backfilling history 100-354 (255 entries)"),
        stream("00:10:01", bolus_completed(257, 2.0, pump_time), bolex_activated(257, 1.5, pump_time)),
        tandem("00:10:03", "Reporting 2 bolus event(s) from pump history"),
    ])
    found = run("history_bolus", d)
    assert kinds(found) == expected
    if found:
        assert found[0].severity == "high" and found[0].data["reported"] == 2
        assert "bolusId 257 2 U" in found[0].summary and "extended bolusId 257 1.5 U" in found[0].summary


def test_history_bolus_report_ignores_stale_records_read_long_before(tmp_path):
    d = day(tmp_path, [], [
        stream("00:10:01", bolus_completed(257, 2.0, datetime(2026, 3, 1, 8, 30))),
        tandem("00:30:00", "Reporting 1 bolus event(s) from pump history"),
    ])
    assert run("history_bolus", d) == []


def ratchet_step(hour: int, last: int) -> list[str]:
    return [tandem(f"{hour:02}:00:00", f"Backfilling history {5000 + hour}-{5000 + hour} (1 entries)"),
            tandem(f"{hour:02}:00:01", "Pruned 255 expired history logs from the session store"),
            tandem(f"{hour:02}:00:02", f"Backfilling history {last - 254}-{last} (255 entries)")]


def test_history_walk_stepping_deeper_after_each_prune(tmp_path):
    lines = [line for hour, last in ((1, 4000), (2, 3745), (3, 3490)) for line in ratchet_step(hour, last)]
    [f] = run("history_backfill", day(tmp_path, [], lines))
    assert (f.check, f.severity, f.data) == ("history_backfill.prune_ratchet", "warn", {"steps": 3, "lowest": 3236})
    assert "01:00–03:00" in f.summary
    assert run("history_backfill", day(tmp_path, [], lines[:6])) == []


def test_history_prune_followed_by_new_entries_is_no_step(tmp_path):
    lines = [tandem("00:55:00", "Backfilling history 5000-5010 (11 entries)")]
    for hour in (1, 2, 3, 4):  # e.g. the history viewer's old records pruned, then a long gap's worth read
        lines += [tandem(f"{hour:02}:00:00", "Pruned 40 expired history logs from the session store"),
                  tandem(f"{hour:02}:00:01", f"Backfilling history {5000 + 300 * hour}-{5299 + 300 * hour} (300 entries)")]
    assert run("history_backfill", day(tmp_path, [], lines)) == []


@pytest.mark.parametrize("line, expected", [
    ("didAdjustPumpClockBy 217067769.0", ("high", "+2512.36 days")),
    ("didAdjustPumpClockBy -300.4", ("warn", "-300s")),
    ("didAdjustPumpClockBy 3.0", None),
])
def test_pump_clock_adjusted(tmp_path, line, expected):
    found = [f for f in run("pump_clock", day(tmp_path, [trio("00:10:00", line)])) if f.check == "pump_clock.adjusted"]
    if expected is None:
        assert found == []
    else:
        assert (found[0].severity, found[0].src) == (expected[0], "trio") and expected[1] in found[0].summary


def test_mask_hides_the_pump_serial_inside_a_dose_raw():
    from trio_logparser.normalize import mask

    assert mask(f"Misdated bolus {RAW_BOLUS} starts at") == "Misdated bolus 0101<SN>01010000 starts at"
    assert mask(f"eventRaw {RAW_EXTENDED}, tempPercent 0%") == "eventRaw 0104<SN>01010000, tempPercent 0%"
    longer = "0101" + "78563412" * 4
    assert mask(f"bytes {longer}") == f"bytes {longer}"


def test_known_issue_ties_misdated_starts_to_the_new_checks():
    problem, sent = start_ahead("2030-01-01 00:00:00 +0000")
    msg = f"Misdated bolus {RAW_BOLUS} {problem}; sent as {sent}"
    assert known.scan("tandem", msg) == [("stale-history-dose-sent-as-now", "signature")]
    assert known.scan("tandem", f"Misdated bolus {RAW_BOLUS} delivered -1 U; sent as {sent}") == []
    assert known.for_check("misdated_dose.start_ahead").id == "stale-history-dose-sent-as-now"
    assert known.for_check("history_bolus.stale").id == "stale-history-dose-sent-as-now"
    assert known.for_check("misdated_dose.ends_before_start") is None


def test_export_flags_history_doses_sent_as_now(tmp_path):
    from trio_logparser.dataset import from_export

    problem, sent = start_ahead("2030-01-01 00:00:00 +0000", "2026-09-20 14:00:03 +0000")
    packet = stream_message(bolus_completed(257, 2.0, datetime(2026, 3, 1, 8, 30)))
    p = tmp_path / "Trio Logs.txt"
    p.write_text(export([
        xtrio("09:50:00", "Pump clock adjusted by 217067769 s", "Pump"),
        xpump("10:00:00", "Backfilling history 100-354 (255 entries)"),
        xpump("10:00:01", packet),
        xpump("10:00:03", "Reporting 1 bolus event(s) from pump history"),
        xpump("10:00:03", f"Misdated bolus {RAW_BOLUS} {problem}; sent as {sent}", "ERROR"),
    ]))
    (d,) = from_export(p, "Pat Doe")
    found = [f for name in ("pump_clock", "history_bolus", "misdated_dose") for f in run(name, d)]
    assert [(f.check, f.severity) for f in found] == [
        ("pump_clock.adjusted", "high"), ("history_bolus.stale", "high"), ("misdated_dose.start_ahead", "high")]
    assert [f.lineno for f in found] == [8, 11, 12]
