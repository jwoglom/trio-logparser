"""Organize raw tester uploads into per-person folders of one-file-per-day logs.

Input is whatever testers upload: loose files, zips (possibly nested), or folders. Files are
Trio log exports (Trio 1.0.2+: ``Trio Logs <date> <time>.txt``, usually zipped in a folder of
the same name; also "Trio Pump Logs" / "Trio CGM Logs"), and from older builds Trio app logs
(``log.txt`` / ``log_prev.txt``) and TandemKit logs (``tandem_log.txt`` /
``tandem_log_prev.txt``). The upload form renames files to ``<name> - <Person>(N).<ext>``, and
macOS to ``log 2.txt``, ``log_prev 3.txt`` and so on. Exports are recognized by their first
line, whatever their name.

Output layout::

    <dest>/<Person>/YYYY-MM-DD_trio_logs.txt     # that day's lines of every export, merged
    <dest>/<Person>/headers/<created>_<variant>.txt   # each export's header, verbatim
    <dest>/<Person>/YYYY-MM-DD_log.txt           # older builds
    <dest>/<Person>/YYYY-MM-DD_tandem_log.txt    # older builds
    <dest>/<Person>/other/<original name>        # crash reports, Loop reports, named logs

Each output day is derived from the timestamps *inside* the file, not its name. ``_prev`` files
are simply the previous day's log, so they land on their own day naturally. An export covers
several days and is split into one file per local day; each day file is itself a valid export
(``Trio Logs`` / ``Day:`` / ``== Log ==``), so every tool reads it like a raw export.

Export days are reconciled per log entry (``export.merge``): a day's versions are unioned in
time order, ignoring per-export placeholder numbering, so overlapping exports, pump- or CGM-only
exports, and re-uploads add only what is new. Versions that overlap in time but share few
entries are reported as a conflict.

When several versions of the same person/day/kind of an *older* log exist (re-uploads, mid-day
snapshots, the already-organized file in ``dest``), they are reconciled:

* identical copies collapse into one;
* a version contained in a longer one is dropped: an earlier snapshot of the same day (a byte
  prefix), or a segment already merged into an existing file;
* remaining versions whose time ranges don't overlap (the log was reset mid-day, e.g. after a
  reinstall) are concatenated in time order;
* anything else is reported as a conflict and left untouched.

Sources are never modified.
"""

from __future__ import annotations

import collections
import io
import itertools
import os
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import export

KINDS = ("log", "tandem_log")
EXPORT_KIND = "trio_logs"
HEADERS_DIR = "headers"

# "<base> - <Person>(3).txt" as produced by the Google Form file-upload question, optionally
# with a macOS " 2" duplicate suffix ("Archive - <Person>(6) 2" when unzipped twice)
_PERSON_SUFFIX = re.compile(
    r"^(?P<base>.+?) - (?P<person>.+?)(?:\(\d+\))?(?: \d+)?(?P<ext>\.[A-Za-z]\w*)?$"
)
# log.txt, log_prev 2.txt, tandem_log_prev.txt, 2026-09-20_log_prev.txt, 2026-09-20_log.txt
_ROTATED_LOG = re.compile(r"^(?:\d{4}-\d{2}-\d{2}_)?(?P<kind>tandem_log|log)(?:_prev)?(?: \d+)?\.txt$")
# "log for X re battery.txt" -- a log a tester saved under a descriptive name
_NAMED_LOG = re.compile(r"^(?P<kind>tandem_log|log)[ _].+\.txt$")

_LINE_DAY = re.compile(rb"^\[?(\d{4}-\d{2}-\d{2})T", re.MULTILINE)
_LINE_TS = re.compile(rb"^\[?(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", re.MULTILINE)

_IGNORED_NAMES = {".DS_Store", "Thumbs.db"}

# A file may contain a few stray lines from the previous day (e.g. watch messages flushed just
# after midnight). More than this fraction of off-day lines means the file really spans days.
MAX_OFF_DAY_FRACTION = 0.01


class OrganizeError(Exception):
    pass


@dataclass
class Source:
    person: str | None
    name: str  # base file name with any upload-form person suffix removed
    data: bytes
    origin: str  # human-readable provenance, for reporting

    @property
    def kind(self) -> str | None:
        if export.is_export_bytes(self.data):
            return EXPORT_KIND
        m = _ROTATED_LOG.match(self.name) or _NAMED_LOG.match(self.name)
        return m.group("kind") if m else None

    @property
    def is_descriptively_named(self) -> bool:
        """A log saved under a custom name; also kept verbatim in ``other/`` for its label."""
        return self.kind in KINDS and not _ROTATED_LOG.match(self.name)


def split_person(filename: str) -> tuple[str, str | None]:
    """``"log_prev 2 - Pat Doe(1).txt"`` -> ``("log_prev 2.txt", "Pat Doe")``."""
    m = _PERSON_SUFFIX.match(filename)
    if not m:
        return filename, None
    return m.group("base") + (m.group("ext") or ""), m.group("person").strip()


def _skip(name: str) -> bool:
    parts = Path(name).parts
    return not parts or "__MACOSX" in parts or parts[-1] in _IGNORED_NAMES or parts[-1].startswith("._")


def _collect_zip(blob: bytes, origin: str, person: str | None, out: list[Source]) -> None:
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        for info in z.infolist():
            if info.is_dir() or _skip(info.filename):
                continue
            _collect_blob(Path(info.filename).name, z.read(info), f"{origin} :: {info.filename}",
                          person, out)


def _collect_blob(filename: str, data: bytes, origin: str, person: str | None,
                  out: list[Source]) -> None:
    base, p = split_person(filename)
    person = p or person
    if filename.lower().endswith(".zip"):
        _collect_zip(data, origin, person, out)
    else:
        out.append(Source(person=person, name=base, data=data, origin=origin))


def collect(path: Path, person: str | None = None) -> list[Source]:
    """Read every file under ``path`` (a file, zip, or directory), recursing into zips.

    The person comes from the nearest ``" - <Person>"`` suffix on the file, an enclosing zip, or
    an enclosing directory, falling back to ``person``.
    """
    out: list[Source] = []
    path = Path(path)
    if path.is_file():
        _collect_blob(path.name, path.read_bytes(), path.name, person, out)
        return out
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames[:] = sorted(d for d in dirnames if not _skip(d))
        rel = Path(dirpath).relative_to(path)
        dir_person = person
        for part in rel.parts:
            dir_person = split_person(part)[1] or dir_person
        for f in sorted(filenames):
            if _skip(f):
                continue
            _collect_blob(f, (Path(dirpath) / f).read_bytes(), str(rel / f), dir_person, out)
    return out


def resolve_unknown_people(sources: list[Source]) -> tuple[list[Source], list[Source]]:
    """Drop person-less sources that duplicate a source with a known person.

    Upload folders often contain both ``Archive - Pat.zip`` and the unzipped ``Archive`` folder;
    the folder copy carries no name. Returns ``(sources, unresolved)``.
    """
    known = {s.data for s in sources if s.person}
    kept, unresolved = [], []
    for s in sources:
        if s.person:
            kept.append(s)
        elif s.data not in known:
            unresolved.append(s)
    return kept, unresolved


def file_day(data: bytes) -> tuple[str, dict[str, int]]:
    """Return the day a log file covers, plus per-day line counts."""
    counts = collections.Counter(d.decode() for d in _LINE_DAY.findall(data))
    if not counts:
        raise OrganizeError("no timestamped lines")
    day, n = counts.most_common(1)[0]
    total = sum(counts.values())
    if (total - n) / total > MAX_OFF_DAY_FRACTION:
        raise OrganizeError(f"spans multiple days: {dict(sorted(counts.items()))}")
    return day, dict(sorted(counts.items()))


def time_span(data: bytes) -> tuple[str, str]:
    """First and last timestamp, ignoring the stray off-day lines ``file_day`` tolerates."""
    day = file_day(data)[0]
    ts = [t.decode() for t in _LINE_TS.findall(data) if t.decode().startswith(day)]
    return ts[0], ts[-1]


@dataclass
class Version:
    data: bytes
    origins: list[str]
    superseded: list[str] = field(default_factory=list)


def reconcile(versions: list[tuple[bytes, str]]) -> tuple[bytes | None, list[Version], str | None]:
    """Combine versions of one person/day/kind log. Returns ``(data, kept, conflict)``."""
    uniq: dict[bytes, Version] = {}
    for data, origin in versions:
        uniq.setdefault(data, Version(data, [])).origins.append(origin)
    vs = list(uniq.values())
    kept = []
    for v in vs:
        longer = [w for w in vs if len(w.data) > len(v.data) and v.data in w.data]
        if longer:
            longer[0].superseded.extend(v.origins)
        else:
            kept.append(v)
    kept.sort(key=lambda v: time_span(v.data)[0])
    for a, b in itertools.pairwise(kept):
        if time_span(a.data)[1] > time_span(b.data)[0]:
            return None, kept, (
                f"overlapping versions: {a.origins[0]} {'..'.join(time_span(a.data))} vs "
                f"{b.origins[0]} {'..'.join(time_span(b.data))}"
            )
    data = b"".join(v.data if v.data.endswith(b"\n") else v.data + b"\n" for v in kept)
    return data, kept, None


@dataclass
class Action:
    status: str  # NEW, SAME, EXTEND, MERGE, CONFLICT, ERROR
    dest: Path
    data: bytes | None = None
    notes: list[str] = field(default_factory=list)


EXISTING = "<existing>"


def plan(sources: list[Source], dest: Path) -> list[Action]:
    """Work out what to write under ``dest``. Reads ``dest`` but never writes."""
    dest = Path(dest)
    actions: list[Action] = []
    groups: dict[tuple[str, str, str], list[tuple[bytes, str]]] = collections.defaultdict(list)
    export_days: dict[tuple[str, str], list[tuple[list[export.Entry], str]]] = collections.defaultdict(list)
    others: dict[Path, list[Source]] = collections.defaultdict(list)

    for s in sources:
        assert s.person, s.origin
        kind = s.kind
        if kind == EXPORT_KIND:
            try:
                header, entries = export.split(export.decode(s.data))
            except ValueError as e:
                actions.append(Action("ERROR", dest / s.person, notes=[f"{s.origin}: {e}"]))
                continue
            name = header.file_name
            others[dest / s.person / HEADERS_DIR / name].append(
                Source(s.person, name, header.text.encode("utf-8", "surrogateescape"), s.origin))
            for day, day_entries in export.by_day(entries).items():
                export_days[(s.person, day)].append((day_entries, s.origin))
            continue
        if kind is None or s.is_descriptively_named:
            others[dest / s.person / "other" / s.name].append(s)
        if kind is None:
            continue
        try:
            day, _ = file_day(s.data)
        except OrganizeError as e:
            actions.append(Action("ERROR", dest / s.person, notes=[f"{s.origin}: {e}"]))
            continue
        groups[(s.person, day, kind)].append((s.data, s.origin))

    for (person, day, kind), versions in sorted(groups.items()):
        out = dest / person / f"{day}_{kind}.txt"
        existing = out.read_bytes() if out.exists() else None
        if existing is not None:
            versions = [(existing, EXISTING), *versions]
        data, kept, conflict = reconcile(versions)
        notes = [f"from {', '.join(v.origins)}" for v in kept]
        notes += [f"superseded (earlier snapshot): {o}" for v in kept for o in v.superseded]
        if conflict:
            actions.append(Action("CONFLICT", out, notes=[conflict, *notes]))
        elif existing is None:
            actions.append(Action("MERGE" if len(kept) > 1 else "NEW", out, data, notes))
        elif data == existing:
            actions.append(Action("SAME", out))
        elif data.startswith(existing):
            actions.append(Action("EXTEND", out, data, notes))
        else:
            actions.append(Action("MERGE", out, data, notes))

    for (person, day), versions in sorted(export_days.items()):
        actions.append(_plan_export_day(dest / person / f"{day}_{EXPORT_KIND}.txt", day, versions))

    for out, srcs in sorted(others.items()):
        datas = {s.data for s in srcs}
        if out.exists():
            datas.add(out.read_bytes())
        origins = [f"from {s.origin}" for s in srcs]
        if len(datas) > 1:
            actions.append(Action("CONFLICT", out, notes=["differing files with this name", *origins]))
        elif out.exists():
            actions.append(Action("SAME", out))
        else:
            actions.append(Action("NEW", out, srcs[0].data, origins))
    return actions


def _plan_export_day(out: Path, day: str, versions: list[tuple[list[export.Entry], str]]) -> Action:
    existing = out.read_bytes() if out.exists() else None
    if existing is not None:
        try:
            _, current = export.split(export.decode(existing))
        except ValueError as e:
            return Action("ERROR", out, notes=[f"existing file: {e}"])
        versions = [(current, EXISTING), *versions]
    else:
        versions = sorted(versions, key=lambda v: -len(v[0]))  # the fullest version is the base
    merged = export.merge(versions)
    base = versions[0][1]
    notes = [f"from {base}" if base != EXISTING else "into the existing file"]
    for origin, n in list(merged.added.items())[1:]:
        notes.append(f"+{n} entries from {origin}" if n else f"superseded (already contained): {origin}")
    if merged.conflict:
        return Action("CONFLICT", out, notes=[merged.conflict, *notes])
    data = export.day_file(day, merged.entries)
    if existing is None:
        contributing = sum(1 for n in merged.added.values() if n)
        return Action("MERGE" if contributing > 1 else "NEW", out, data, notes)
    if data == existing:
        return Action("SAME", out)
    return Action("EXTEND" if data.startswith(existing) else "MERGE", out, data, notes)


WRITES = {"NEW", "EXTEND", "MERGE"}


def apply(actions: list[Action]) -> int:
    """Write planned files atomically. Returns the number written."""
    n = 0
    for a in actions:
        if a.status not in WRITES:
            continue
        a.dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = a.dest.with_name(a.dest.name + ".partial")
        tmp.write_bytes(a.data)
        tmp.replace(a.dest)
        n += 1
    return n
