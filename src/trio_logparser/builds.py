"""Which Trio / TandemKit build produced each part of a log, and whether a fix is in it.

Every Trio launch logs ``Trio Started: v<version> ... [Branch: <branch> <commit>]`` followed by
the commit of each submodule, including TandemKit. A day without a launch inherits the build
from the person's previous day.
"""

from __future__ import annotations

import bisect
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .dataset import Day


@dataclass(frozen=True)
class Build:
    version: str
    branch: str
    trio_commit: str
    tandemkit: str | None

    @property
    def label(self) -> str:
        return f"TK {self.tandemkit or '?'} ({self.branch} v{self.version})"


def assign(days: list[Day], headers: dict[str, list] | None = None) -> None:
    """Fill ``day.builds``: each launch starts a build, and a day inherits the build running when
    it began.

    ``headers`` (person -> export ``Header``s) add what an export says ran when it was made: with
    no launch logged in its window, that build ran for the whole window (Trio starts are rare,
    so most export windows have none).
    """
    by_person: dict[str, list[Day]] = {}
    for d in days:
        by_person.setdefault(d.person, []).append(d)
    for person, pdays in by_person.items():
        anchors = [(e.ts, Build(e.data["version"], e.data["branch"], e.data["commit"], e.data.get("tandemkit")))
                   for d in pdays for e in d.trio if e.kind == "app_start"]
        starts = [t for t, _ in anchors]
        for h in (headers or {}).get(person, ()):
            lo = h.window_start
            if h.build and lo and not any(lo <= t <= h.created for t in starts):
                anchors.append((lo, h.build))
        anchors.sort(key=lambda a: a[0])
        times = [t for t, _ in anchors]
        for d in pdays:
            day_start = datetime.fromisoformat(d.day)
            day_end = day_start + timedelta(days=1)
            d.builds = []
            i = bisect.bisect_left(times, day_start)
            if i:
                d.builds.append((datetime.min, anchors[i - 1][1]))
            for t, b in anchors[i:bisect.bisect_left(times, day_end)]:
                if not d.builds or d.builds[-1][1] != b:
                    d.builds.append((t, b))


def build_at(day: Day, ts: datetime) -> Build | None:
    if not day.builds:
        return None
    i = bisect.bisect_right([t for t, _ in day.builds], ts) - 1
    return day.builds[i][1] if i >= 0 else None  # before this person's first logged launch


class Repo:
    """Read-only git queries against a TandemKit checkout (``$TANDEMKIT_REPO``)."""

    def __init__(self, path: str | os.PathLike | None = None):
        p = path or os.environ.get("TANDEMKIT_REPO")
        self.path = Path(p).expanduser() if p else None
        self._cache: dict[tuple, object] = {}

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        key = ("git", *args)
        if key not in self._cache:
            self._cache[key] = subprocess.run(["git", "-C", str(self.path), *args],
                                              capture_output=True, text=True, check=False)
        return self._cache[key]

    def resolve(self, rev: str | None) -> str | None:
        if not self.path or not rev:
            return None
        return self._git("rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}").stdout.strip() or None

    def contains(self, build: str | None, fix: str | None) -> bool | None:
        """True if commit ``fix`` is in ``build``'s history; None if either is unknown."""
        b, f = self.resolve(build), self.resolve(fix)
        if not b or not f:
            return None
        return self._git("merge-base", "--is-ancestor", f, b).returncode == 0

    def time(self, rev: str | None) -> int:
        """Commit timestamp of ``rev``; 0 if unknown (sorts first)."""
        if not self.resolve(rev):
            return 0
        return int(self._git("log", "-1", "--format=%ct", rev).stdout.strip() or 0)

    def date(self, rev: str | None) -> str | None:
        """Commit date (YYYY-MM-DD) of ``rev``."""
        if not self.resolve(rev):
            return None
        return self._git("log", "-1", "--format=%cs", rev).stdout.strip()
