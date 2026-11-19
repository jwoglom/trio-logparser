"""Export the pump history logs captured in a Trio log export (or an older tandem_log) as JSONL.

TandemKit logs every history-log stream packet it receives; in a Trio log export (raw, or an
organized ``YYYY-MM-DD_trio_logs.txt`` day) as a Pump line::

    2026-11-17 15:53:38.218 -0500 Pump [delegate] [INFO] TandemPeripheralManager.swift -
        handleHistoryLogStreamPacket(_:)#677: Received stream message: HistoryLogStreamResponse(cargo=0301…, …)

and in an older tandem_log as::

    [2026-10-06T17:42:41-0400 INFO] TandemPeripheralManager.swift - handleHistoryLogStreamPacket(_:)#578:
        Received stream message: HistoryLogStreamResponse(cargo=0301…, numberOfHistoryLogs=3, streamId=1, …)

The output is TandemKit's own history-log export format (``HistoryLogExport.jsonlLine``, the
format its history-log viewer writes and that mirrors pumpX2's cliparser ``jsonlines``). Rather
than re-implementing the ~180 history-log decoders, the cargo of each packet is decoded by a small
helper compiled from TandemKit's own ``Sources/TandemCore`` (git-archived at a chosen revision, so
the working tree is never touched) and cached per TandemKit commit.
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from collections.abc import Iterator
from importlib import resources
from pathlib import Path

_STREAM = re.compile(
    r"^(?:\[(?P<date>\d{4}-\d{2}-\d{2})T(?P<time>\d{2}:\d{2}:\d{2})[+-]\d{4} \w+\]"  # tandem_log
    r"|(?P<xdate>\d{4}-\d{2}-\d{2}) (?P<xtime>\d{2}:\d{2}:\d{2})(?:\.(?P<ms>\d+))? [+-]\d{4} Pump)"  # export
    r" .*?Received (?:stream )?message: HistoryLogStreamResponse\(cargo=(?P<cargo>[0-9a-fA-F]*), "
    r"numberOfHistoryLogs=(?P<n>\d+)"
)

# Starting point for the compiled subset; further files are pulled in as the compiler reports
# missing types, so the set adapts to whatever TandemKit revision is used.
_SEED = [
    "Messages/HistoryLog/*.swift",
    "Messages/Message.swift",
    "CharacteristicUUID.swift",
    "Common/Bytes.swift",
    "Common/CRC16.swift",
    "Common/Dates.swift",
    "Common/Extensions/*.swift",  # extension members never show up as a missing type
]
_UNAVAILABLE = re.compile(r"^import (?:LoopKit|LoopKitUI|UIKit|SwiftUI)\b", re.MULTILINE)  # Xcode-workspace only
_MISSING = re.compile(r"error: cannot find (?:type )?'(\w+)' in scope")
_DECL = r"\b(?:class|struct|enum|protocol|typealias|func|let|var)\s+{}\b"


class ExportError(Exception):
    pass


def extract_streams(path: Path) -> Iterator[tuple[str, str]]:
    """``(timestamp, cargo hex)`` for every complete HistoryLogStreamResponse line, in log order.

    The timestamp is the log line's local wall-clock time in the JSONL export's
    ``yyyy-MM-dd HH:mm:ss.SSSSSS`` format (milliseconds from a Trio log export; an older
    tandem_log only has whole seconds). Lines whose cargo doesn't hold the advertised number of
    logs (e.g. torn by the old logger race) are skipped.
    """
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if "HistoryLogStreamResponse" not in line:
                continue
            m = _STREAM.match(line)
            if not m:
                continue
            cargo = m["cargo"]
            n = int(m["n"])
            if len(cargo) < 4 or int(cargo[:2], 16) != n or len(cargo) < 2 * (2 + 26 * n):
                continue
            if m["xdate"]:
                yield f"{m['xdate']} {m['xtime']}.{(m['ms'] or '')[:6]:0<6}", cargo.lower()
            else:
                yield f"{m['date']} {m['time']}.000000", cargo.lower()


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise ExportError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


def _cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "trio-logparser" / "historylog-export"


def build_exporter(repo: Path, rev: str = "HEAD", log=print) -> Path:
    """Compile (once per TandemKit commit) the helper that decodes stream cargo with TandemKit."""
    sha = _git(repo, "rev-parse", "--verify", f"{rev}^{{commit}}")
    out = _cache_dir() / sha[:12] / "historylog-export"
    if out.exists():
        return out
    if not shutil.which("xcrun") and not shutil.which("swiftc"):
        raise ExportError("needs the Swift toolchain (xcrun swiftc)")
    swiftc = ["xcrun", "swiftc"] if shutil.which("xcrun") else ["swiftc"]
    log(f"building the TandemKit history-log decoder at {sha[:7]} (once per commit)…")

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        archive = tmp / "src.tar"
        with open(archive, "wb") as f:
            subprocess.run(["git", "-C", str(repo), "archive", sha, "Sources/TandemCore"], stdout=f, check=True)
        with tarfile.open(archive) as t:
            t.extractall(tmp, filter="data")
        core = tmp / "Sources" / "TandemCore"
        main = tmp / "main.swift"
        main.write_text(resources.files(__package__).joinpath("swift/historylog_export_main.swift").read_text())

        every = sorted(glob.glob(str(core / "**" / "*.swift"), recursive=True))
        sources = {f: Path(f).read_text(errors="ignore") for f in every}
        every = [f for f in every if not _UNAVAILABLE.search(sources[f])]
        files = [str(main)]
        optional: set[str] = set()  # glob-seeded files; dropped if they need something outside TandemCore
        for pattern in _SEED:
            matched = [f for f in sorted(glob.glob(str(core / pattern))) if f in every and f not in files]
            files += matched
            if "*" in pattern and "HistoryLog" not in pattern:
                optional.update(matched)
        for _ in range(40):
            r = subprocess.run([*swiftc, "-typecheck", *files], capture_output=True, text=True, check=False)
            if r.returncode == 0:
                break
            added, dropped = [], set()
            for line in r.stderr.splitlines():
                m = _MISSING.search(line)
                if not m:
                    continue
                where = line.split(":", 1)[0]
                found = next((f for f in every if f not in files and re.search(_DECL.format(m[1]), sources[f])), None)
                if found and found not in added:
                    added.append(found)
                elif not found and where in optional:
                    dropped.add(where)
            if not added and not dropped:
                errors = "\n".join(line for line in r.stderr.splitlines() if "error:" in line)[:2000]
                raise ExportError(f"TandemKit subset does not compile:\n{errors}")
            files = [f for f in files if f not in dropped] + added
        else:
            raise ExportError("could not resolve the TandemKit sources needed")

        out.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run([*swiftc, "-O", "-o", str(out), *files], capture_output=True, text=True, check=False)
        if r.returncode != 0:
            raise ExportError(f"swiftc failed:\n{r.stderr[-2000:]}")
    return out


def export(log_path: Path, out, exporter: Path) -> tuple[int, int]:
    """Write JSONL for ``log_path`` to the text stream ``out``. Returns (stream packets, history logs)."""
    packets = logs = 0
    proc = subprocess.Popen([str(exporter)], stdin=subprocess.PIPE, stdout=out, text=True)
    assert proc.stdin
    for ts, cargo in extract_streams(log_path):
        proc.stdin.write(f"{ts}\t{cargo}\n")
        packets += 1
        logs += int(cargo[:2], 16)
    proc.stdin.close()
    if proc.wait() != 0:
        raise ExportError(f"exporter exited with {proc.returncode}")
    return packets, logs
