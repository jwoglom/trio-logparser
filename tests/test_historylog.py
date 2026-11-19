from trio_logparser.historylog import extract_streams

LOG = "TandemPeripheralManager.swift - handleHistoryLogStreamPacket(_:)#578: Received stream message: "
REC = "2c118fb04b23275e0d00d7000000000000000000000000000000"  # one synthetic 26-byte history record


def test_extract_streams(tmp_path):
    good = f"0101{REC}8aea"
    p = tmp_path / "tandem_log.txt"
    p.write_text(
        f"[2026-10-06T17:42:41-0400 INFO] {LOG}HistoryLogStreamResponse(cargo={good}, numberOfHistoryLogs=1, "
        "streamId=1, historyLogStreamBytes=[...])\n"
        # cargo shorter than its advertised record count (e.g. a torn line) is skipped
        f"[2026-10-06T17:42:42-0400 INFO] {LOG}HistoryLogStreamResponse(cargo=0201{REC}, numberOfHistoryLogs=2, "
        "streamId=1)\n"
        "[2026-10-06T17:42:43-0400 INFO] TandemPumpManager.swift - fn()#1: unrelated\n"
    )
    assert list(extract_streams(p)) == [("2026-10-06 17:42:41.000000", good)]


def test_extract_streams_from_export(tmp_path):
    good = f"0101{REC}8aea"
    p = tmp_path / "2026-11-17_trio_logs.txt"
    p.write_text(
        "Trio Logs\nDay: 2026-11-17\n\n== Log ==\n"
        f"2026-11-17 15:53:38.218 -0500 Pump [delegate] [INFO] {LOG}HistoryLogStreamResponse(cargo={good}, "
        "numberOfHistoryLogs=1, streamId=1)\n"
        f"2026-11-17 15:53:39.000 -0500 Trio [Pump] HistoryLogStreamResponse(cargo={good}, numberOfHistoryLogs=1)\n"
    )
    assert list(extract_streams(p)) == [("2026-11-17 15:53:38.218000", good)]
