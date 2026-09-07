# lej-cc-tool

Slack bot that tracks customs-clearance completeness of master air waybills
at Leipzig/Halle (LEJ), using PortGround's shipment-status export.

Post an AWB in Slack, and the bot reports how much of it is cleared —
immediately, then after 15 minutes, then every 30 minutes until it reaches
100%, attaching the source workbook as it goes. Several AWBs are tracked in
parallel on independent clocks.

```
/awb 488-20744846
```

```
📦 MAWB 488-20744846 — customs clearance
████████████░░░░░░░░  60.0%
6 of 10 shipments cleared · 12/20 items (60%)

✅ Cleared 6        ⏳ Not cleared 4
Since last check: +4 cleared

Open (4): `0034…0007`, `0034…0008`, `0034…0009`, `0034…0010`

Data as of 15:05 · next check 15:20 · check #2 · started by @jozsef
[ Refresh now ]  [ Stop tracking ]
```

## How it works

```
Slack ──/awb──► Bolt (Socket Mode) ──► jobs table (SQLite, next_run_at)
                                             │
                       scheduler ticks 30s, claims due jobs, runs them in parallel
                                             ▼
        GET portground /download → parse xlsx → diff vs last poll
                     → post to thread + attach file → set next_run_at
```

The schedule lives in the database, not in memory. If the process restarts,
every AWB resumes on its own clock; an AWB started at 10:00 and one started
at 10:15 stay 15 minutes apart. See `src/lej_cc/scheduler.py`.

## Commands

| Command | Effect |
|---|---|
| `/awb 488-20744846` | Start tracking. Several numbers in one command are fine. |
| `/awb list` | What this channel is currently tracking. |
| `/awb stop 488-20744846` | Stop tracking. |
| `/awb help` | Usage. |
| `@bot 488-20744846` | Same as `/awb`, for people who prefer mentions. |

Buttons on each status card: **Refresh now**, **Stop tracking**.

## AWB numbers

Input is normalised to the bare 11 digits PortGround expects, so
`488-20744846`, `488 2074 4846` and `48820744846` are all accepted.

The last digit is an IATA check digit (`serial mod 7`, Resolution 600a), and
it is verified locally. A mistyped AWB is rejected in Slack in
milliseconds rather than becoming a polling job that never finds anything.
Both reference AWBs (488-20744846, 936-00333955) pass this check.

## The export format

Verified against two live files (225 rows all cleared, 1605 rows all not
cleared):

- one sheet, header in row 1, 17 columns, one row per house shipment
- **every cell is a string**, timestamps included (`YYYY-MM-DD HH:MM:SS`)
- the workbook's `docProps` carry `+02:00`, so timestamps are Europe/Berlin
- `Final Status` is the source of truth: `cleared` / `not cleared`
- `Clearance Time` agreed with `Final Status` on all 1830 reference rows; it
  is used as a corroborating signal and for lead-time history, not as the
  primary status

Columns are found by **name**, not position, so PortGround can reorder or
append columns without breaking the parser. Only three are required:
`HAWB / Tracking number`, `MAWB`, `Final Status`.

### Status mapping

`config/status_map.yaml` maps raw values onto `cleared` / `not_cleared`.
Anything unrecognised becomes **other**, is counted as *not* cleared, and is
named explicitly in the Slack message with its raw value.

That is deliberate. Silently counting an unknown status as cleared is the
one failure mode that costs money, so a new value such as `blocked` surfaces
the first time it appears and the mapping gets extended.

## Error handling

| Situation | Behaviour |
|---|---|
| Not 11 digits | Rejected in Slack, no job created |
| Check digit fails | Rejected as a probable typo |
| AWB unknown (404 or zero rows) on the **first** poll | Reported at once, job stops — with a hint to retry if it was only just filed |
| AWB disappears later | Retried up to `EMPTY_RESULT_GRACE_POLLS` times, then stopped |
| 401/403 | Ops channel alerted — the API key needs renewing |
| 5xx / timeout / rate limit | Retried with exponential backoff; Slack is told only once it looks persistent (3 failures), job stops after 5 |
| Response is not an xlsx | Treated as an outage, first bytes logged (key scrubbed) |
| A required column vanishes | Job stops, ops channel alerted — the format changed |
| Rows carry a different MAWB | Rejected |
| Still incomplete after 48 h | Job stops with a "still at X%" message |
| A bug in the polling cycle | Caught, reported, scheduler keeps running |

## Setup

### 1. Slack app

Everything the app needs is in `slack-app-manifest.yaml`, so there is
nothing to configure by hand:

1. <https://api.slack.com/apps> → **Create New App** → **From a manifest**
2. Pick the workspace → paste `slack-app-manifest.yaml` → **Create**
3. **Basic Information → App-Level Tokens → Generate Token and Scopes**:
   name it `socket`, add the `connections:write` scope, generate.
   Copy the `xapp-…` value → `SLACK_APP_TOKEN`
4. **Install App → Install to Workspace** → approve.
   Copy the `xoxb-…` Bot User OAuth Token → `SLACK_BOT_TOKEN`
5. In Slack: `/invite @AWB Tracker` in the channel you want to use

Socket Mode means no public URL and no inbound firewall rule. If the
workspace requires app approval, step 4 goes to a workspace admin —
creating the app in steps 1–3 does not.

### 2. Run

```bash
cp .env.example .env      # fill in the three tokens
make dev
make test
make run                  # or: docker compose up --build -d
```

To deploy it for free — on your own machine or an Always Free cloud VM —
follow [`docs/DEPLOY-FREE.md`](docs/DEPLOY-FREE.md). The bot needs no
inbound network access, so no ports are opened and no firewall rule is
required.

### 3. Smoke-test without Slack

```bash
python -m lej_cc.cli 488-20744846              # live call
python -m lej_cc.cli 488-20744846 --file x.xlsx --list-open
```

## Security

The PortGround key is a bearer credential passed in a URL query string.

- It lives only in `.env` / a secret manager — never in the repo
- It is scrubbed from logs and from anything posted to Slack (`config.scrub`)
- Rotate it if it has been circulated by email or pasted into a chat
- Real exports contain live customer invoice numbers, MRNs and tracking
  numbers. **Do not commit them.** Test fixtures under `tests/data/` are
  synthetic — regenerate with `make fixtures`.

## Layout

| Path | Purpose |
|---|---|
| `src/lej_cc/awb.py` | Normalisation, IATA check digit, extraction from text |
| `src/lej_cc/parser.py` | Workbook → `Snapshot` |
| `src/lej_cc/model.py` | `ShipmentRow`, `Snapshot`, snapshot diffing |
| `src/lej_cc/portground.py` | API client, typed failures |
| `src/lej_cc/store.py` | SQLite jobs + snapshot history |
| `src/lej_cc/scheduler.py` | Tick loop, parallel polling |
| `src/lej_cc/tracker.py` | One polling cycle, error policy |
| `src/lej_cc/formatting.py` | Block Kit messages |
| `src/lej_cc/slack_app.py` | Commands, buttons, mentions |
| `src/lej_cc/cli.py` | One-shot check, no Slack |

## Not built yet

- Google Drive archive of every workbook, Google Sheet dashboard
  (`GOOGLE_*` settings are wired but unused)
- Lead-time analytics over the `snapshots` table
- Pushing notification files back to `portground.notification@singular-it.de`
