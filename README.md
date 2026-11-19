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

Testers upload **Trio log exports** (Trio 1.0.2+, Settings ▸ Share logs): `Trio Logs <date>
<time>.txt`, usually as a zip of a folder with the same name. One file holds a header
(`== App ==`, `== Phone ==`, `== Pump ==`, `== CGM ==`, `== Settings ==`, `== Alerts ==`) and,
under `== Log ==`, every source merged in time order with millisecond timestamps. "Trio Pump Logs"
and "Trio CGM Logs" exports hold one device's lines only. Exports are recognized by their first
line, whatever the file is called. Older builds' `log.txt` / `tandem_log.txt` files are still
organized as before.

Sources are never modified. Output:

```
$TRIO_LOGS_DIR/<Person>/YYYY-MM-DD_trio_logs.txt    # that local day's lines from every export
$TRIO_LOGS_DIR/<Person>/headers/<created>_<variant>.txt  # each export's header, verbatim
$TRIO_LOGS_DIR/<Person>/YYYY-MM-DD_log.txt          # older builds: Trio app log for that day
$TRIO_LOGS_DIR/<Person>/YYYY-MM-DD_tandem_log.txt   # older builds: TandemKit log for that day
$TRIO_LOGS_DIR/<Person>/other/...                   # Loop reports, crash reports, custom-named logs
```

- Each file's day comes from the timestamps inside it. An export covers several days and is
  split into one `_trio_logs.txt` per local day, itself a valid one-day export
  (`Trio Logs` / `Day:` / `== Log ==`), so every command reads it like a raw export.
  `log_prev` / `tandem_log_prev` are the previous day's log and land on that day.
- The person comes from the upload form's `"<file> - <Person>(N).ext"` suffix on the file, an
  enclosing zip, or folder; unzipped copies without a name are matched to their zip by content.
- Re-runs are safe and incremental.
  - **Export days** are merged per log entry: an entry is its instant plus its text, with the
    export's per-file redaction numbering ignored (`<pump-serial-1>` in one export can be
    `<pump-serial-2>` in the next). Successive overlapping exports, pump- or CGM-only exports,
    and re-uploads add only the entries the day file lacks, in time order; identical lines in
    the same millisecond count as repeats. Device-trace lines only reach exports made by the
    same app run, so a day file can hold traces a later export lacks. Versions that overlap in
    time but share under half their entries there (another phone?) are a `CONFLICT`.
  - **Older logs**, per person/day/kind: duplicates collapse, earlier mid-day snapshots are
    superseded by the full day, and non-overlapping segments (log reset mid-day, e.g.
    reinstall) are concatenated in time order. Overlapping, differing versions are reported as
    `CONFLICT` and left alone.
- Timestamps are rendered in the phone's time zone *at export time*. Exports made in different
  time zones split days differently and don't deduplicate across the boundary.

Statuses: `NEW`, `SAME`, `EXTEND` (existing file grew at its end), `MERGE` (versions joined),
`CONFLICT`, `ERROR` (e.g. an old-format file spanning several days). Exit code is 1 if any
conflict/error.

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

A single export can also be analyzed without organizing it, with `--file` (any report command;
findings' line numbers then point into that file):

```sh
uv run trio-logs scan --file "Trio Logs 2026-11-18 1301.txt"
uv run trio-logs findings --file "Trio Logs 2026-11-18 1301.txt" --refs
uv run trio-logs show --file "Trio Logs 2026-11-18 1301.txt" 10509 -C 10
```

In an export, `Pump` lines carry TandemKit's own text (`[INFO] File.swift - func#line: msg`) and
are the `tandem` side; `Trio`/`Watch`/`CGM`/`Algorithm` lines are the `trio` side. Trio 1.0.2
reworded its loop/dose messages (`Loop finished in`, `Temp basal enacted: … U/h`, `SMB enacted:`);
`events.py` maps both wordings. Where a day has an export and old-format files, the export is
analyzed and `scan` says so. Builds come from each `Trio started` line and, for export windows
with no launch in them, from the saved export header's `== App ==` section.

Testers are shown as initials by default (`--names` for full names). `show` masks pump serials,
transmitter IDs and emails; still treat its output as health data. A TandemKit dose's `raw`
(`0101…`–`0105…`, 20 hex digits) is left as Trio's export prints it, so findings and `show` use the
same dose ids as the log; findings add the pump id it names (`bolusId 123`). It embeds the pump
serial in bytes 2–5: keep real ones out of anything public.

## Browse in a web page

```sh
uv run trio-logs web --file "Trio Logs 2026-11-18 1301.txt"   # one export (repeat --file for more)
uv run trio-logs web                                          # the organized tree: pick a tester, a day, how many days
```

Opens `http://127.0.0.1:8765/`. At the top is a timeline: CGM readings, enacted temp basals, SMBs
and boluses (✕ = failed), pump suspensions, Trio's IOB and COB, the pump's own IOB, and rows of
loop, pump-connection and finding markers. Drag to zoom, scroll to zoom around the pointer,
double-click for everything; clicking moves the table below to that time. Tabs:

| Tab | Rows |
|---|---|
| Log | Every line, all sources merged in time order; filter by source, level, category, text or `/regex/`. |
| Insulin | Each delivery action as Trio (request, outcome, the dose it stored), the driver (TandemKit's lines) and the pump (the request on the wire and its status) saw it. |
| CGM | Readings. Dexcom G7 live and backfill messages are dated by the sensor; for other CGMs, the BG each loop started from. |
| Loop & oref | Each `Algorithm` determination (BG, IOB, COB, eventual BG, temp, SMB, ISF, CR, target, autosens, insulinReq, reason) and what Trio did next. |
| Pump comms | Each request with its outcome (response, timeout, cancelled, refused, disconnect, none logged) and latency. |
| Pump history | The records the logged history-log stream packets carry, by sequence number, with how often each was read. |
| Findings | The checks' findings (as `findings`). |
| Export & builds | The export header and the builds running. |

Every row opens its log line, with the lines around it and its `trio-logs show` command. By
default "chart window only" limits the tables to the zoomed time window.

- **Pump history** is decoded with TandemKit's own classes, as `history-jsonl` does: with
  `--tandemkit-repo` (or `$TANDEMKIT_REPO`) the decoder is built for `--tandemkit-rev` on first use;
  otherwise the newest one already built is used; failing both, only each record's header (type id,
  pump time, sequence number) is shown.
- **Pump IOB** is the IOB the pump displays (`ControlIQIOBResponse`, the calculation its `iobType`
  names, in milliunits). The pump counts by its own rules, so it follows Trio's IOB only roughly;
  look for the two drifting apart, not for equality.
- **Privacy.** The server listens on loopback only and answers only requests addressed to
  `127.0.0.1`/`localhost`/`[::1]`, so other web pages can't read it through DNS rebinding. Pages
  load nothing from other hosts (a `Content-Security-Policy` enforces it) and aren't cached. Text
  is masked as `show` masks it (`--unmasked` shows it as logged) and testers are initials unless
  `--names`. What's on screen is still health data: keep screenshots out of public issues.
- Views are parsed when first opened and kept while the server runs (the last 4). Restart it to
  pick up newly organized logs.

### How it works

| Stage | Module | |
|---|---|---|
| parse | `parse.py`, `export.py` | Lines → records. Exports: `Pump` lines are the tandem side, the rest the trio side; indented continuation lines attach to the record above; `export.py` reads headers and splits and merges export days. Older logs: Trio continuation lines (JSON, tables) attach to the record above; tandem lines without a header are **torn** (TandemLogger's unlocked seek-then-write let a shorter concurrent write overwrite a longer one's start; a fully overwritten line vanishes: **lost**). Exports come from Trio's own stores and are never torn. |
| events | `events.py` | Records → typed events by message text (function names and line numbers change per build). Every `Writing message:` / `Received message:` becomes `pump_request` / `pump_response` with parsed fields. |
| builds | `builds.py` | Each launch line (`Trio started: v… <branch> <commit>; … TandemKit <sha>`, or the older `Trio Started … [Branch: …]`) starts a build; an export header's `== App ==` build covers its window when no launch is logged in it; days inherit the build running when they begin. |
| checks | `checks.py` | Each finding points at a log line. Severity `high` = Trio and the pump disagree about insulin, `warn` = something failed or went missing, `info` = expected but counted. |
| known issues | `known.py`, `known_issues.json` | TandemKit fixes with log signatures. A symptom on a build without the fix is *fixed later*; on a build with it, *regression?* — flagged only for **definitive** signatures (the fix removed that log text). |
| web | `viewer.py`, `web.py`, `web/` | `viewer.py` parses a view once and derives its tables (CGM, oref, doses, pump comms, pump IOB, markers, findings, pump history), each row pointing at its line; `web.py` serves them as JSON on loopback; `web/` is the page (plain JS, no dependencies; log text only ever enters it as DOM text). |

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
uv run trio-logs history-jsonl "Trio Logs 2026-11-18 1301.txt" -o history.jsonl [--tandemkit-rev origin/dev]
uv run trio-logs history-jsonl $TRIO_LOGS_DIR/<Person>/2026-11-18_trio_logs.txt -o history.jsonl
uv run trio-logs history-jsonl <tandem_log.txt> -o history.jsonl      # older builds
```

Writes one line per `HistoryLogStreamResponse` packet the log recorded, in TandemKit's
history-log export format (`HistoryLogExport.jsonlLine` — what the in-app history-log viewer
exports, mirroring pumpX2 cliparser `jsonlines`). Decoding uses TandemKit's own history-log
classes: the first run for a TandemKit commit git-archives `Sources/TandemCore` at
`--tandemkit-rev` (the checkout is never touched), compiles the needed subset with `swiftc`
(a few minutes; needs Xcode's toolchain), and caches the binary in
`~/.cache/trio-logparser/historylog-export/<commit>/`. `raw.ts` is the log line's local time, to
the millisecond from an export (an older tandem_log only records whole seconds).

## Development

```sh
uv run pytest
uv run ruff check .
```
