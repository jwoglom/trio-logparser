import io
import zipfile

import pytest

from trio_logparser import organize
from trio_logparser.cli import main


def trio_log(day: str, times: list[str]) -> bytes:
    return b"".join(
        f"{day}T{t}-0400 [Default] File.swift - fn() - 1 - DEV: line {t}\n".encode() for t in times
    )


def tandem_log(day: str, times: list[str]) -> bytes:
    return b"".join(f"[{day}T{t}-0400 INFO] File.swift - fn()#1: msg {t}\n".encode() for t in times)


def zip_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


@pytest.mark.parametrize(
    "filename, base, person",
    [
        ("log_prev 2 - Pat Doe(1).txt", "log_prev 2.txt", "Pat Doe"),
        ("tandem_log - Pat Doe.txt", "tandem_log.txt", "Pat Doe"),
        ("Trio logs 3 - Pat Doe.zip", "Trio logs 3.zip", "Pat Doe"),
        ("Archive - Pat Doe(6) 2", "Archive", "Pat Doe"),
        ("Archive - Pat Doe 2", "Archive", "Pat Doe"),
        ("log.txt", "log.txt", None),
    ],
)
def test_split_person(filename, base, person):
    assert organize.split_person(filename) == (base, person)


@pytest.mark.parametrize(
    "name, kind",
    [
        ("log.txt", "log"),
        ("log_prev 3.txt", "log"),
        ("tandem_log_prev.txt", "tandem_log"),
        ("2026-09-20_tandem_log_prev.txt", "tandem_log"),
        ("log after reconnect.txt", "log"),
        ("Loop Report.md", None),
        ("Trio.diskwrites_resource.ips", None),
    ],
)
def test_kind(name, kind):
    assert organize.Source("p", name, b"", "o").kind == kind


def test_day_from_content_not_name(tmp_path):
    up = tmp_path / "up"
    up.mkdir()
    (up / "log - Pat Doe.txt").write_bytes(trio_log("2026-09-20", ["00:00:01", "23:00:00"]))
    (up / "log_prev - Pat Doe.txt").write_bytes(trio_log("2026-09-19", ["00:00:01", "23:00:00"]))
    (up / "tandem_log_prev - Pat Doe.txt").write_bytes(tandem_log("2026-09-19", ["00:00:01"]))
    dest = tmp_path / "out"
    assert main(["organize", str(up), "--dest", str(dest), "--apply"]) == 0
    assert sorted(p.name for p in (dest / "Pat Doe").iterdir()) == [
        "2026-09-19_log.txt", "2026-09-19_tandem_log.txt", "2026-09-20_log.txt",
    ]
    assert list(up.iterdir())  # sources untouched


def test_dedup_snapshot_and_unnamed_folder_copies(tmp_path):
    full = trio_log("2026-09-20", ["00:00:01", "12:00:00", "23:59:00"])
    snapshot = full[: full.index(b"2026-09-20T23")]
    up = tmp_path / "up"
    (up / "Archive").mkdir(parents=True)
    (up / "Archive - Pat Doe.zip").write_bytes(zip_bytes({"log.txt": snapshot, "log_prev 2.txt": full}))
    (up / "Archive" / "log.txt").write_bytes(snapshot)  # unzipped copy, no person
    sources, unresolved = organize.resolve_unknown_people(organize.collect(up))
    assert not unresolved
    [action] = organize.plan(sources, tmp_path / "out")
    assert action.status == "NEW" and action.data == full
    assert any("superseded" in n for n in action.notes)


def test_nested_zip_person_inherited(tmp_path):
    inner = zip_bytes({"Trio logs/log.txt": trio_log("2026-09-20", ["01:00:00"])})
    (tmp_path / "drive.zip").write_bytes(zip_bytes({"Trio logs 6 - Pat Doe.zip": inner}))
    [s] = organize.collect(tmp_path / "drive.zip")
    assert (s.person, s.name, s.kind) == ("Pat Doe", "log.txt", "log")


def test_midday_reset_segments_are_merged(tmp_path):
    a = trio_log("2026-08-26", ["05:34:54", "05:44:19"])
    b = trio_log("2026-08-26", ["11:16:44", "23:59:48"])
    srcs = [organize.Source("Pat", "log_prev 2.txt", b, "b"), organize.Source("Pat", "log.txt", a, "a")]
    [action] = organize.plan(srcs, tmp_path)
    assert action.status == "MERGE" and action.data == a + b


def test_already_merged_segment_is_same(tmp_path):
    a = trio_log("2026-08-26", ["05:34:54", "05:44:19"])
    b = trio_log("2026-08-26", ["11:16:44", "23:59:48"])
    (tmp_path / "Pat").mkdir()
    (tmp_path / "Pat" / "2026-08-26_log.txt").write_bytes(a + b)
    for seg in (a, b):
        [action] = organize.plan([organize.Source("Pat", "log.txt", seg, "s")], tmp_path)
        assert action.status == "SAME"


def test_overlapping_versions_conflict(tmp_path):
    a = trio_log("2026-08-26", ["05:00:00", "06:00:00"])
    b = trio_log("2026-08-26", ["05:30:00", "07:00:00"])
    srcs = [organize.Source("Pat", "log.txt", a, "a"), organize.Source("Pat", "log 2.txt", b, "b")]
    [action] = organize.plan(srcs, tmp_path)
    assert action.status == "CONFLICT" and action.data is None


def test_incremental_run_extends_existing(tmp_path):
    full = trio_log("2026-09-19", ["00:00:01", "12:00:00", "23:59:00"])
    out = tmp_path / "Pat" / "2026-09-19_log.txt"
    out.parent.mkdir()
    out.write_bytes(full[: full.index(b"2026-09-19T12")])
    [action] = organize.plan([organize.Source("Pat", "log_prev.txt", full, "new")], tmp_path)
    assert action.status == "EXTEND"
    out.write_bytes(full)
    older = full[: full.index(b"2026-09-19T12")]
    [action] = organize.plan([organize.Source("Pat", "log.txt", older, "x")], tmp_path)
    assert action.status == "SAME"  # an older snapshot never shrinks the existing file


def test_stray_previous_day_lines_tolerated():
    data = b"[2026-08-23T23:53:04-0400] [WatchState.swift:428] fn \xe2\x86\x92 msg\n" + trio_log(
        "2026-08-24", [f"{h:02}:00:00" for h in range(24)] * 10
    )
    assert organize.file_day(data)[0] == "2026-08-24"
    assert organize.time_span(data)[0].startswith("2026-08-24T00")


def test_multi_day_file_is_error(tmp_path):
    data = trio_log("2026-08-23", ["22:00:00"] * 10) + trio_log("2026-08-24", ["01:00:00"] * 10)
    [action] = organize.plan([organize.Source("Pat", "log.txt", data, "x")], tmp_path)
    assert action.status == "ERROR"


def test_other_files_and_descriptive_logs(tmp_path):
    named = trio_log("2026-08-21", ["11:15:48"])
    srcs = [
        organize.Source("Pat", "Loop Report.md", b"# report", "r"),
        organize.Source("Pat", "log for battery check.txt", named, "n"),
    ]
    dests = {a.dest.relative_to(tmp_path).as_posix(): a.status for a in organize.plan(srcs, tmp_path)}
    assert dests == {
        "Pat/2026-08-21_log.txt": "NEW",
        "Pat/other/Loop Report.md": "NEW",
        "Pat/other/log for battery check.txt": "NEW",
    }


def test_unresolved_person_requires_flag(tmp_path, capsys):
    (tmp_path / "2026-09-21_log_prev.txt").write_bytes(trio_log("2026-09-20", ["01:00:00"]))
    assert main(["organize", str(tmp_path), "--dest", str(tmp_path / "out")]) == 2
    assert main(["organize", str(tmp_path), "--dest", str(tmp_path / "out"), "--person", "me",
                 "--apply"]) == 0
    assert (tmp_path / "out" / "me" / "2026-09-20_log.txt").exists()
