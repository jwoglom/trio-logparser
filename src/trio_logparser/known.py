"""Registry of known TandemKit issues and their fixes (``known_issues.json``).

Each entry may carry:
  log_signature       regex over a log message (``signature_kind`` regex_tandem / regex_trio)
                      that indicates the buggy behavior
  fixed_marker_regex  regex for a line the fix introduced: evidence the fixed code is running
  fix_sha             commit that fixes it; a build contains the fix iff fix_sha is an ancestor
  checks              analyzer check ids whose findings are this issue
  strength            "definitive": the fix removed the signature's text (or added a handler for
                      that exact message), so any post-fix hit means old code ran. "suggestive":
                      the text can still be logged by fixed code, so post-fix hits are only leads.
  streak              {count, gap_s, min_span_s, reset_on_marker}: the symptom is a run of
                      signature hits, not a single line

Status of an occurrence in a build:
  fixed later   the build predates the fix: expected, already handled
  regression?   the build contains the fix yet the symptom appears
  open          no fix known
  unknown       build or fix commit not in the local TandemKit checkout
"""

from __future__ import annotations

import functools
import json
import re
from dataclasses import dataclass, field
from importlib import resources

from .builds import Repo


@dataclass
class Issue:
    id: str
    pr: int | None
    fix_sha: str | None
    title: str
    symptom: str
    kind: str
    signature: re.Pattern | None
    fixed_marker: re.Pattern | None
    checks: list[str] = field(default_factory=list)
    confidence: str = ""
    notes: str = ""
    strength: str = "suggestive"
    streak: dict | None = None
    issues: list[int] = field(default_factory=list)

    @property
    def src(self) -> str | None:
        return {"regex_tandem": "tandem", "regex_trio": "trio"}.get(self.kind)


@functools.cache
def load() -> tuple[Issue, ...]:
    raw = json.loads(resources.files(__package__).joinpath("known_issues.json").read_text())
    out = []
    for e in raw:
        sig = e.get("log_signature") if str(e.get("signature_kind", "")).startswith("regex") else None
        marker = e.get("fixed_marker_regex")
        out.append(Issue(
            id=e["id"], pr=e.get("pr"), fix_sha=e.get("fix_sha") or e.get("merge_sha"),
            title=e.get("title", ""), symptom=e.get("symptom", ""), kind=e.get("signature_kind", ""),
            signature=re.compile(sig) if sig else None, fixed_marker=re.compile(marker) if marker else None,
            checks=e.get("checks", []), confidence=e.get("confidence", ""), notes=e.get("notes", ""),
            strength=e.get("strength", "suggestive"), streak=e.get("streak"), issues=e.get("issues") or [],
        ))
    return tuple(out)


@functools.cache
def _scanner() -> tuple[re.Pattern | None, list[tuple[str, str, str | None]]]:
    """One alternation over every signature and marker; group i -> (issue id, what, src)."""
    parts, meta = [], []
    for issue in load():
        for what, rx, src in (("signature", issue.signature, issue.src),
                              ("marker", issue.fixed_marker, None)):
            if rx is not None:
                parts.append(f"(?P<g{len(meta)}>{rx.pattern})")
                meta.append((issue.id, what, src))
    if not parts:
        return None, meta
    return re.compile("|".join(f"(?:{p})" for p in parts)), meta


def scan(src: str, msg: str) -> list[tuple[str, str]]:
    """[(issue id, "signature" | "marker")] matched by a log message from ``src``."""
    rx, meta = _scanner()
    if rx is None or not (m := rx.match(msg)):
        return []
    hits = []
    for name, value in m.groupdict().items():
        if value is not None:
            issue_id, what, isrc = meta[int(name[1:])]
            if isrc is None or isrc == src:
                hits.append((issue_id, what))
    # alternation stops at the first matching branch; re-check the others individually
    for i, (issue_id, what, isrc) in enumerate(meta):
        if (issue_id, what) in hits or (isrc is not None and isrc != src):
            continue
        issue = by_id()[issue_id]
        rx_i = issue.signature if what == "signature" else issue.fixed_marker
        if rx_i.match(msg):
            hits.append((issue_id, what))
    return hits


@functools.cache
def by_id() -> dict[str, Issue]:
    return {i.id: i for i in load()}


def for_check(check: str) -> Issue | None:
    return next((i for i in load() if check in i.checks or check.split(".")[0] in i.checks), None)


def status(repo: Repo, issue: Issue, build_sha: str | None) -> str:
    if not issue.fix_sha:
        return "open"
    has_fix = repo.contains(build_sha, issue.fix_sha)
    if has_fix is None:
        return "unknown"
    return "regression?" if has_fix else "fixed later"


def episodes(issue: Issue, hits: list) -> list[list]:
    """Group one person's signature hits (sorted by time) into symptom occurrences.

    ``hits`` items need ``.what`` ("signature"/"marker") and ``.ts``. Without a streak rule every
    signature hit is its own occurrence.
    """
    sig = [h for h in hits if h.what == "signature"]
    rule = issue.streak
    if not rule:
        return [[h] for h in sig]
    out, run = [], []
    for h in hits:
        if h.what == "marker":
            if rule.get("reset_on_marker"):
                out.append(run)
                run = []
            continue
        if run and (h.ts - run[-1].ts).total_seconds() > rule.get("gap_s", 600):
            out.append(run)
            run = []
        run.append(h)
    out.append(run)
    return [r for r in out if len(r) >= rule.get("count", 1)
            and (r[-1].ts - r[0].ts).total_seconds() >= rule.get("min_span_s", 0)]
