"""The web viewer's data and server, over synthetic logs. Every log line here is made up."""

import gzip
import http.client
import json
import struct
import threading
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path

from trio_logparser import viewer, web
from trio_logparser.dataset import discover
from trio_logparser.parse import Record

DAY = "2026-09-20"


def at(t: str) -> int:
    """Epoch ms of ``DAY t`` at -0400."""
    return viewer.epoch_ms(datetime.fromisoformat(f"{DAY} {t}"), "-0400")


def x(t: str, source: str, rest: str) -> str:
    return f"{DAY} {t}.000 -0400 {source} {rest}\n"


def xtrio(t: str, msg: str, cat: str = "Loop", level: str = "") -> str:
    return x(t, "Trio", f"{level + ': ' if level else ''}[{cat}] {msg}")


def xpump(t: str, msg: str) -> str:
    return x(t, "Pump", f"[delegate] [INFO] TandemPeripheralManager.swift - fn()#1: {msg}")


def g7_live(t: str, mg_dl: int, session_s: int, age: int) -> str:
    return x(t, "CGM", f"[receive] Sensor didRead G7GlucoseMessage(status:0 glucose:Optional({mg_dl}), sequence:9 "
                       f"glucoseIsDisplayOnly:false state:ok messageTimestamp:{session_s} age:{age}, data:00)")


def g7_backfill(t: str, mg_dl: int, sensor_s: int) -> str:
    return x(t, "CGM", f"[receive] Sensor didReadBackfill G7BackfillMessage(glucose:Optional({mg_dl}), "
                       f"glucoseIsDisplayOnly:false timestamp:{sensor_s}, data:00)")


def export_file(tmp_path: Path, lines: list[str], name: str = "Trio Logs.txt") -> Path:
    p = tmp_path / name
    p.write_text("Trio Logs\nCreated: 2026-09-21 00:00:00 -0400\n\n== App ==\nTrio: 1.0.2 (1)\n"
                 "Pump: Tandem Mobi 12345678\n\n== Log ==\n" + "".join(lines))
    return p


ALGO = ("Algorithm enacted · BG 120 · IOB 1.5 · COB 10 · rate 0.8/30m · SMB 0.2 · eventual 140 · Autosens ratio: 1.1, "
        "ISF: 50→45, COB: 10, Dev: 5, BGI: -1, CR: 10, Target: 100, minPredBG 90, minGuardBG 85; Eventual BG 140 >= 100,"
        "  insulinReq 0.4. Microbolusing 0.2U.")


def loop_export(tmp_path: Path) -> Path:
    return export_file(tmp_path, [
        xtrio("09:00:00", "Trio started: v1.0.2.9 (1) feat/x abc1234; TandemKit def5678", "Lifecycle"),
        g7_backfill("09:58:00", 110, 3240),  # before any live message: dated by the next one's session
        g7_live("10:00:00", 120, 3600, 60),  # session started 09:00:00, reading taken 09:59:00
        g7_backfill("10:00:01", 120, 3540),  # the live reading again
        x("10:00:02", "Algorithm", ALGO.removeprefix("Algorithm ")),
        xtrio("10:00:03", "Temp basal enacted: 0.8 U/h for 30 min"),
        xpump("10:00:03", "Writing message: SetTempRateRequest(cargo=00, minutes=30, percent=80)"),
        xpump("10:00:03", "Received message: SetTempRateResponse(cargo=00, status=0)"),
        xtrio("10:00:04", "SMB enacted: 0.2 U"),
        xtrio("10:00:05", "Loop finished in 1.2 s"),
        xtrio("10:00:06", "Finalized temp basal 0102ab: rate 0.80000000000000004 U/h for 30 min", "Pump"),
        xtrio("10:00:07", "Pump status: bolusing false, suspended true, battery 80%", "Pump"),
        xpump("10:00:08", "Writing message: CurrentStatusRequest(cargo=)"),
        xpump("10:00:38", "Timeout hit"),
        xtrio("10:05:00", "Pump status: bolusing false, suspended false, battery 80%", "Pump"),
        xpump("10:05:01", "Connected to pump: Tandem Mobi 12345678"),
        xpump("10:05:02", "Writing message: ControlIQIOBRequest(cargo=)"),
        xpump("10:05:02", "Received message: ControlIQIOBResponse(cargo=00, mudaliarIOB=1500, timeRemainingSeconds=0, "
                          "mudaliarTotalIOB=0, swan6hrIOB=2000, type=Optional(TandemCore.IOBType.MUDALIAR), iobType=0)"),
    ])


def test_bundle_from_export(tmp_path):
    b = viewer.bundle(viewer.from_file(loop_export(tmp_path)))
    lines = b["lines"]
    assert len(lines["t"]) == 18 and set(lines["sources"]) == {"Trio", "CGM", "Algorithm", "Pump"}
    assert lines["o"][0] == -240 and lines["n"][0] == 9  # first log line follows the header

    assert [(g["t"], g["v"], g["kind"]) for g in b["glucose"]] == [
        (at("09:54:00"), 110, "backfill"), (at("09:59:00"), 120, "live")]

    (d,) = b["determinations"]
    assert (d["kind"], d["bg"], d["iob"], d["cob"], d["rate"], d["duration"], d["smb"], d["eventual"]) == \
        ("enacted", 120, 1.5, 10, 0.8, 30, 0.2, 140)
    assert (d["sens"], d["isf"], d["cr"], d["target"], d["min_pred"], d["insulin_req"]) == (1.1, 45, 10, 100, 90, 0.4)
    assert d["outcome"] == "temp 0.8 U/h for 30 min · SMB 0.2 U · loop 1.2 s"

    doses = {(r["who"], r["action"]): r for r in b["doses"]}
    assert doses[("Trio", "temp basal")]["ok"] and doses[("Trio", "temp basal")]["amount"] == "0.8 U/h × 30 min"
    assert doses[("Trio", "SMB")]["units"] == 0.2
    assert doses[("Trio", "stored temp basal")]["amount"] == "0.8 U/h × 30 min"
    assert doses[("Trio", "stored temp basal")]["raw"] == "0102ab"
    assert doses[("Pump", "SetTempRate")]["outcome"] == "status 0" and doses[("Pump", "SetTempRate")]["ms"] == 0
    assert b["basal"] == [{"t": at("10:00:03"), "rate": 0.8, "minutes": 30, "line": 5}]

    assert [(c["request"], c["outcome"], c.get("ms")) for c in b["comms"]] == [
        ("SetTempRateRequest", "response", 0), ("CurrentStatusRequest", "timeout", 30_000),
        ("ControlIQIOBRequest", "response", 0)]
    assert b["pump_iob"] == [{"t": at("10:05:02"), "v": 1.5, "line": 17}]  # milliunits of the iobType's calculation
    assert [p["suspended"] for p in b["pump_state"]] == [True, False]
    assert {m["kind"] for m in b["markers"]} >= {"app_start", "loop_success", "timeout", "connected"}
    assert b["builds"][0]["label"].endswith("TandemKit def5678")

    # identifiers masked by default, in lines and the header alike
    assert "12345678" not in json.dumps(b)
    unmasked = viewer.bundle(viewer.from_file(loop_export(tmp_path)), unmasked=True)
    assert "Tandem Mobi 12345678" in unmasked["lines"]["m"][15] and "12345678" in unmasked["headers"][0]


def test_cgm_falls_back_to_the_bg_each_loop_used(tmp_path):
    p = export_file(tmp_path, [xtrio("10:00:00", "Glucose ready: BG 150 mg/dL, 3 recent readings"),
                               xtrio("10:05:00", "Glucose ready: BG 6.1 mmol/L, 3 recent readings")])
    b = viewer.bundle(viewer.from_file(p))
    assert [(g["v"], g["kind"]) for g in b["glucose"]] == [(150, "loop"), (6.1, "loop")]


def test_findings_point_at_their_lines(tmp_path):
    p = export_file(tmp_path, [
        xtrio("10:00:00", "Loop started, mode closed"),
        xtrio("10:00:01", "Loop finished in 0.5 s"),
        xtrio("11:00:00", "Loop started, mode closed"),
        xtrio("11:00:01", "Loop finished in 0.5 s"),
    ])
    b = viewer.bundle(viewer.from_file(p))
    gap = next(f for f in b["findings"] if f["check"] == "loop_gap")
    assert b["lines"]["n"][gap["line"]] in (10, 12)


def history_record(type_id: int, pump_secs: int, seq: int) -> bytes:
    return struct.pack("<HII", 0x1000 | type_id, pump_secs, seq) + bytes(16)


def stream_line(t: str, *records: bytes) -> str:
    cargo = (bytes([len(records), 7]) + b"".join(records) + b"\xab\xcd").hex()
    return xpump(t, f"Received stream message: HistoryLogStreamResponse(cargo={cargo}, "
                    f"numberOfHistoryLogs={len(records)}, streamId=7, historyLogStreamBytes=[], historyLogs=[])")


def test_pump_lines_are_filed_by_topic():
    def rec(file: str, func: str, msg: str) -> Record:
        return Record("tandem", datetime(2026, 9, 20), "-0400", "INFO", "delegate", file, func, msg, 1, source="Pump")

    peripheral = "TandemPeripheralManager.swift"
    cases = [
        (rec(peripheral, "sendUntyped(_:)", "Writing message: SetTempRateRequest(cargo=00, minutes=30)"), "Delivery"),
        (rec(peripheral, "sendUntyped(_:)", "Received message: ControlIQIOBResponse(cargo=00, iobType=0)"),
         "Status reads"),
        (rec(peripheral, "sendUntyped(_:)", "Writing message: HistoryLogRequest(cargo=00)"), "History"),
        (rec(peripheral, "sendUntyped(_:)", "Writing message: DismissNotificationRequest(cargo=00)"), "Alerts"),
        (rec(peripheral, "sendUntyped(_:)", "Writing message: Jpake1aRequest(cargo=00)"), "Connection"),
        (rec(peripheral, "handleHistoryLogStreamPacket(_:)", "Received stream message: HistoryLogStreamResponse("
             "cargo=00, numberOfHistoryLogs=0)"), "History"),
        (rec("TandemPumpManager.swift", "enactTempBasal(unitsPerHour:for:)", "Temp basal enacted!"), "Delivery"),
        (rec("TandemPumpManager.swift", "recordSiteChange(at:)", "Recording site change"), "Cartridge & site"),
        (rec("TandemBluetoothManager.swift", "startScan()", "Started scanning"), "Connection"),
        (rec("TandemPumpManager+BasalReconciliation.swift", "reconcileBasalHistory()", "Basal difference"), "Delivery"),
        (rec("TandemPumpManager+Widgets.swift", "update()", "Updated"), "Widgets"),
        (rec("", "", "another driver's free-form line"), "delegate"),
    ]
    assert [viewer.pump_topic(r) for r, _ in cases] == [want for _, want in cases]


def test_cargo_shows_except_where_it_carries_a_serial():
    from trio_logparser.normalize import mask

    line = "Received message: HighestAamResponse(cargo=0a1b2c3d4e5f60718293a4b5c6, aamId=3)"
    assert mask(line) == line
    masked = mask("Received message: PumpVersionBResponse(cargo=0a0b0c0d0e0f10111213, serialNumber=12345678, "
                  "pcbSerialNumberA=87654321, modelNumber=1)")
    assert "0a0b0c0d" not in masked and "12345678" not in masked and "87654321" not in masked
    assert "modelNumber=1" in masked


def test_findings_carry_their_kind_and_the_severity_scale(tmp_path):
    p = export_file(tmp_path, [xtrio("01:00:00", "Loop succeeded"), xtrio("01:40:00", "Loop succeeded")])
    b = viewer.bundle(viewer.from_file(p))
    [gap] = [f for f in b["findings"] if f["check"] == "loop_gap"]
    kind = b["finding_kinds"]["loop_gap"]
    assert kind["title"] and kind["what"] and kind["why"][gap["severity"]]
    assert set(b["severity_scale"]) == {"high", "warn", "info"}


def test_history_without_the_decoder_lists_record_headers(tmp_path):
    a, b = history_record(20, 600_000_000, 41), history_record(3, 600_000_060, 42)
    p = export_file(tmp_path, [stream_line("10:00:00", a, b), stream_line("10:00:05", b)])
    sel = viewer.from_file(p)
    assert viewer.bundle(sel)["history_packets"] == 2
    h = viewer.history(sel, None)
    assert not h["decoded"] and h["packets"] == 2
    assert [(r["seq"], r["type"], r["reads"], r["line"]) for r in h["records"]] == [
        (41, "type 20", 1, 0), (42, "type 3", 2, 0)]
    assert h["records"][0]["pump_time"] == "2027-01-05 10:40:00"


def test_parse_history_log():
    s = ("BolusCompletedHistoryLog[completionStatusId=3,bolusId=257,insulinDelivered=0.3,"
         "names={a,b},cargo={20,16,-30,0},pumpTimeSec=595788002,sequenceNum=2974101]")
    assert viewer.parse_history_log(s) == {
        "type": "BolusCompleted", "cargo": "1410e200", "pump_secs": 595788002, "seq": 2974101,
        "fields": {"completionStatusId": "3", "bolusId": "257", "insulinDelivered": "0.3", "names": "{a,b}"}}


def old_day(tmp_path: Path) -> Path:
    person = tmp_path / "Pat Doe"
    person.mkdir(parents=True)
    (person / f"{DAY}_log.txt").write_text(
        f"{DAY}T10:00:00-0400 [ApsManager] APSManager.swift - fn() - 1 - DEV: Temp basal succeeded: 0.5 for: 30\n"
        f"{DAY}T10:00:02-0400 [ApsManager] APSManager.swift - fn() - 1 - DEV: Loop succeeded\n")
    (person / f"{DAY}_tandem_log.txt").write_text(
        f"[{DAY}T10:00:01-0400 INFO] TandemPumpManager.swift - fn()#1: Setting temp basal at 0.5U/hr for 1800.0s\n")
    return tmp_path


def test_old_format_days_merge_both_logs_in_time_order(tmp_path):
    root = old_day(tmp_path)
    sel = viewer.from_days(discover(root), [], "P.D.")
    b = viewer.bundle(sel)
    assert b["lines"]["sources"] == ["Trio", "Pump"]
    assert [b["lines"]["s"][i] for i in range(3)] == [0, 1, 0]  # trio, tandem, trio by time
    assert [f["ref"] for f in b["files"]] == [f"trio-logs show P.D. {DAY} trio", f"trio-logs show P.D. {DAY} tandem"]
    assert [(r["who"], r["action"]) for r in b["doses"]] == [("Trio", "temp basal"), ("Driver", "set temp basal")]
    assert b["person"] == "P.D."


def test_host_check():
    assert web._host_ok("127.0.0.1:8765") and web._host_ok("localhost:8765") and web._host_ok("[::1]:8765")
    assert not web._host_ok("evil.example:8765") and not web._host_ok("127.0.0.1.evil.example")


def serve(app: web.App):
    server = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(app))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def get(server, path: str, host: str | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1])
    conn.request("GET", path, headers={"Host": host or f"127.0.0.1:{server.server_address[1]}",
                                       "Accept-Encoding": "gzip"})
    r = conn.getresponse()
    body = r.read()
    if r.getheader("Content-Encoding") == "gzip":
        body = gzip.decompress(body)
    return r.status, dict(r.getheaders()), body


def test_server_serves_catalog_views_and_refuses_other_hosts(tmp_path):
    export = loop_export(tmp_path)
    server = serve(web.App([export], old_day(tmp_path / "tree")))
    try:
        status, headers, body = get(server, "/")
        assert status == 200 and b"/app.js" in body
        assert "default-src 'self'" in headers["Content-Security-Policy"] and headers["Cache-Control"] == "no-store"

        status, _, body = get(server, "/api/catalog")
        catalog = json.loads(body)
        assert catalog["files"] == [{"id": "f0", "label": "Trio Logs.txt"}]
        assert catalog["people"] == [{"id": "p0", "label": "P.D.", "days": [{"day": DAY, "kind": "old"}]}]

        status, _, body = get(server, "/api/view?key=f0")
        assert status == 200 and len(json.loads(body)["glucose"]) == 2
        status, _, body = get(server, f"/api/view?key=p0/{DAY}/1")
        assert status == 200 and json.loads(body)["person"] == "P.D."
        assert get(server, "/api/view?key=p0/2020-01-01/1")[0] == 404
        assert get(server, "/api/view?key=../../etc")[0] == 404
        assert get(server, "/api/catalog", host="evil.example")[0] == 403
    finally:
        server.shutdown()
