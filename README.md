# trio-logparser

Tooling to organize, process, and validate Trio + TandemKit beta-test logs.

> **Tester logs are sensitive health data.** Keep them outside this repository (the
> `.gitignore` blocks `*.txt`, `*.zip`, `*.ips`, `*.log` as a backstop). Never commit logs,
> excerpts, tester names, or test fixtures derived from real logs; tests use synthetic data.

## Setup

```sh
uv sync
export TRIO_LOGS_DIR=~/Documents/Trio_TandemKit_Logs/TandemKitTestingLogs   # organized log root
```

## Organize uploads

```sh
uv run trio-logs organize <upload folder | zip | file>...           # dry run
uv run trio-logs organize <upload folder | zip | file>... --apply   # write
uv run trio-logs organize ~/logs/mine --person <name> --apply      # files without a "- Name" suffix
```

Sources are never modified. Output:

```
$TRIO_LOGS_DIR/<Person>/YYYY-MM-DD_log.txt          # Trio app log for that local day
$TRIO_LOGS_DIR/<Person>/YYYY-MM-DD_tandem_log.txt   # TandemKit log for that local day
$TRIO_LOGS_DIR/<Person>/other/...                   # Loop reports, crash reports, custom-named logs
```

- Each file's day comes from the timestamps inside it. `log_prev` / `tandem_log_prev` are the
  previous day's log and land on that day.
- The person comes from the upload form's `"<file> - <Person>(N).ext"` suffix on the file, an
  enclosing zip, or folder; unzipped copies without a name are matched to their zip by content.
- Re-runs are safe and incremental. Per person/day/kind, versions are reconciled: duplicates
  collapse, earlier mid-day snapshots are superseded by the full day, and non-overlapping
  segments (log reset mid-day, e.g. reinstall) are concatenated in time order. Overlapping,
  differing versions are reported as `CONFLICT` and left alone.

Statuses: `NEW`, `SAME`, `EXTEND` (existing file grew), `MERGE` (segments joined), `CONFLICT`,
`ERROR` (e.g. a file spanning several days). Exit code is 1 if any conflict/error.

## Analyze

```sh
export TANDEMKIT_REPO=~/repos/TandemKit   # read-only; for build dates and fix ancestry

uv run trio-logs scan                       # findings per check: count, last build seen, known-issue status
uv run trio-logs findings --check bolus --severity warn --refs
uv run trio-logs findings --recent --unresolved   # what still happens on current builds
uv run trio-logs issues                     # each known TandemKit fix: symptom before vs after it
uv run trio-logs templates --recent --src tandem  # warning/error message templates on recent builds
uv run trio-logs builds                     # which build each tester ran when
uv run trio-logs show J.D. 2026-09-20 tandem 1234 -C 10   # masked context around a finding
```

A single **"Trio Logs" export** (Trio 1.0.2+: one file with `== App ==` … `== Log ==` sections,
every source merged in time order) is analyzed directly with `--file`; findings' line numbers
point into that file:

```sh
uv run trio-logs scan --file "Trio Logs 2026-11-18 1301.txt"
uv run trio-logs findings --file "Trio Logs 2026-11-18 1301.txt" --refs
uv run trio-logs show --file "Trio Logs 2026-11-18 1301.txt" 10509 -C 10
```

`Pump` lines carry TandemKit's own text (`[INFO] File.swift - func#line: msg`) and are the
`tandem` side; `Trio`/`Watch`/`CGM`/`Algorithm` lines are the `trio` side. Trio 1.0.2 reworded its
loop/dose messages (`Loop finished in`, `Temp basal enacted: … U/h`, `SMB enacted:`), mapped in
`events.py`. `organize` and `history-jsonl` don't read exports yet.

Testers are shown as initials by default (`--names` for full names). `show` masks pump serials,
transmitter IDs and emails; still treat its output as health data. A TandemKit dose's `raw`
(`0101…`–`0105…`, 20 hex digits) holds the pump serial in bytes 2–5, which Trio's export leaves in:
`show` masks it and findings name the dose by its pump id (`bolusId 123`) instead.

### How it works

| Stage | Module | |
|---|---|---|
| parse | `parse.py` | Lines → records. Trio continuation lines (JSON, tables) attach to the record above. Tandem lines without a header are **torn**: TandemLogger's unlocked seek-then-write lets a shorter concurrent write overwrite a longer one's start (a fully overwritten line vanishes: **lost**). |
| events | `events.py` | Records → typed events by message text (function names and line numbers change per build). Every `Writing message:` / `Received message:` becomes `pump_request` / `pump_response` with parsed fields. |
| builds | `builds.py` | `Trio Started … [Branch: <branch> <commit>] [submodules: … TandemKit: … <sha>]` gives the build per launch; days without a launch inherit the previous one. |
| checks | `checks.py` | Each finding points at a log line. Severity `high` = Trio and the pump disagree about insulin, `warn` = something failed or went missing, `info` = expected but counted. |
| known issues | `known.py`, `known_issues.json` | TandemKit fixes with log signatures. A symptom on a build without the fix is *fixed later*; on a build with it, *regression?* — flagged only for **definitive** signatures (the fix removed that log text). |

Checks:

- `temp_basal.*` — Trio's temp-basal outcome vs TandemKit's command (falls back to the
  `SetTempRateRequest` percent × profile when the "Setting temp basal" line was lost):
  `rate_rounded_down` (≤ one percent step, pre-#357), `below_min_rate` (< 0.1 U/hr sent as 0),
  `rate_mismatch`, `failed_but_enacted`, `unconfirmed`, `no_trio_outcome`, `pump_rejected`.
- `bolus.*` — Trio's bolus outcome vs TandemKit's enact / `InitiateBolusRequest` (paired 1:1 by
  amount): `units_mismatch`, `failed_but_initiated`, `concurrent_enact`, `stopped_early` (pump's
  `LastBolusStatus` delivered < requested, with the pump's stop reason), `permission_denied`.
- `pump_request.*` — each request's response: `late` (> 30 s), `duplicate_in_flight`, `missing`,
  `timeout`, `disconnect`, `torn_line`.
- `loop_gap` — > 15 min without a successful loop, annotated with failures, missing glucose,
  pump disconnects, and silent-log stretches.
- `misdated_dose.*` — TandemKit's `Misdated …` errors: a dose whose dates it replaced before sending
  it to the host, one finding per dose however often it was re-sent. `start_ahead` (`high` for a bolus
  or temp basal stamped an hour or more ahead of the phone: the host was told it starts now),
  `ends_before_start`, `negative_delivery`.
- `history_bolus.stale` — "Reporting N bolus event(s) from pump history" right after the pass read
  bolus records the pump stamped more than 36 h from the phone's clock (`BolusCompleted` and
  `BolexActivated` are decoded from the logged stream packets, so no Swift toolchain is needed).
- `history_backfill.prune_ratchet` — the walk backward reading a slice deeper each time the session
  store prunes the records it had stopped at.
- `pump_clock.*` — `drift` (pump vs phone at each `TimeSinceResetResponse`), `set_time`, and
  `adjusted`: Trio's own line for a clock write, the only trace of setup's, which happens before Trio
  records the pump's log lines (`high` past a day: history from before it carries the old clock's stamps).
- `reconnect_storm`, `torn_lines`, `log_line_lost`, `app_restart`.

Adding a known issue: append to `known_issues.json` (`fix_sha`, `log_signature`,
`signature_kind`, optional `fixed_marker_regex`, `checks`, `strength`, `streak`) and check
`trio-logs issues`.

## Export pump history logs

```sh
uv run trio-logs history-jsonl <tandem_log.txt> -o history.jsonl [--tandemkit-rev origin/dev]
```

Writes one line per `HistoryLogStreamResponse` packet the tandem log recorded, in TandemKit's
history-log export format (`HistoryLogExport.jsonlLine` — what the in-app history-log viewer
exports, mirroring pumpX2 cliparser `jsonlines`). Decoding uses TandemKit's own history-log
classes: the first run for a TandemKit commit git-archives `Sources/TandemCore` at
`--tandemkit-rev` (the checkout is never touched), compiles the needed subset with `swiftc`
(a few minutes; needs Xcode's toolchain), and caches the binary in
`~/.cache/trio-logparser/historylog-export/<commit>/`. `raw.ts` is the log line's local time; the
log only records whole seconds.

## Development

```sh
uv run pytest
uv run ruff check .
```
