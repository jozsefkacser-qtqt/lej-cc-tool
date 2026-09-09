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
📦 936-02927993 — customs clearance
🟩🟩🟩🟩🟩🟩🟩🟩🟩⬜  97.8%

✅ Cleared            ⏳ Open
1,544 of 1,578        34

🧾 Declaration lines  ⏱ Tracking
9,264 of 9,434 (98%)  1h 15m

Since last check: ▲ 412 cleared
──────────────────────────────────────
34 shipments still open — see the attached OPEN…xlsx, oldest first.

📄 data 13:24 · next check 13:39 · check #3 · by @jozsef
[ 🔄 Refresh now ]  [ Stop tracking ]
```

The bar is coloured by state -- green above 95%, amber above 50%, red below
-- and only a genuine 100% fills it. 97.8% rounding up to ten green cells
would say "finished" about an AWB with 34 shipments still stuck.

Looking up an AWB that finished long ago answers the question people are
actually asking -- *is this the one from May?* -- rather than just "cleared":

```
✅ 488-20744846 — cleared
🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩  100.0%

✅ Cleared 225 of 225        🏁 Finished 20 May 2026 14:38
🧾 Decl. lines 2,480/2,480   ⏱ 111d 22h ago · cleared over 5h 09m

📄 data 15:05 · check #1 · by @colleague
```

…with PortGround's export attached to the channel beneath it.

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

Starting and stopping are announced **to the channel** -- everyone watching
needs to know an AWB is being tracked without asking who did it. `/awb list`
and `/awb help` stay private to whoever typed them.

## Email notifications

Optional and off until `SMTP_HOST` and `EMAIL_FROM` are set. When on, the
same updates go out by mail with both workbooks attached.

```
/awb 488-20744846 candy.tang@qtlogistics.eu
```

Any address in the command becomes a recipient for that AWB.
`EMAIL_ALWAYS_TO` adds standing recipients who get every AWB regardless of
who started it.

**Cadence is stricter than Slack**: the first check, real changes, and the
end. An unchanged poll never sends mail, whatever `EMAIL_ON_CHANGE` says --
a notifier that mails "nothing happened" is one people filter into a folder
they stop opening.

Every mail for an AWB keeps the same subject and references the first
mail's `Message-ID`, so a client collapses them into one conversation
rather than an inbox full of near-identical messages.

A mail failure is logged and swallowed: Slack has already carried the same
update, and an SMTP outage must not take down the polling cycle.

### Google Workspace

`smtp.gmail.com:587` with STARTTLS, using an **app password** on a
dedicated account -- the account password will not work with 2FA on.
`lej-cc-doctor` connects and authenticates without sending anything, so you
can verify the credentials before the first AWB depends on them.

## Attachments

Each update carries two files:

| File | What it is |
|---|---|
| `OPEN_488-20744846_34_shipments.xlsx` | **The chase sheet.** Only shipments still open, only the columns needed to chase them, oldest first, with a filter row and frozen header. |
| `shipment_status_….xlsx` | PortGround's original 17-column export, unmodified, as the audit trail. |

On a real master AWB that is 34 rows x 10 columns instead of 1578 x 17.
Set `ATTACH_FULL_WORKBOOK=false` to keep only the short one.

While tracking continues the files go into the card's thread, so the channel
stays readable. On the **final** update -- including an AWB that is already
100% cleared when someone asks about it -- they go to the channel instead: a
thread reply on a message with no other replies is a file nobody finds.

The columns of the chase sheet are defined by `COLUMNS` in
`src/lej_cc/report.py` -- that list is the whole definition of the report.
`Days open` is computed from the earliest timestamp a shipment has, and
rows with no timestamps at all sort last, since nothing has happened to
them yet.

## What you can track

Two kinds of identifier, validated very differently.

**Master air waybills** are normalised to the bare 11 digits PortGround
expects, so `488-20744846`, `488 2074 4846` and `48820744846` are all
accepted.

The last digit is an IATA check digit (`serial mod 7`, Resolution 600a), and
it is verified locally. A mistyped AWB is rejected in Slack in
milliseconds rather than becoming a polling job that never finds anything.
Both reference AWBs (488-20744846, 936-00333955) pass this check.

**Booking references** such as `OyTM202608137666` are passed through to
PortGround exactly as typed, case included, since nothing here knows whether
it compares case-sensitively. They carry no checksum, so a typo in one can
only be caught by the API saying it has never heard of it.

Rows returned for a reference carry their real waybill number in the `MAWB`
column. That is the answer, not a mismatch, so the file-identity check
applies only to waybill requests -- and the card reports which waybill the
reference turned out to be.

References are accepted when someone types one as a command argument, and
deliberately **not** matched when scanning message text: with no checksum,
any pattern loose enough to catch `OyTM202608137666` would also catch order
numbers, file names and half the words in a signature.

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

### "Declaration lines", not items

PortGround's export has a column called `Number of items`, and the obvious
reading -- parcels -- is wrong. In the reference export for 936-00333955 it
equals `Number of hs codes` on **all 1605 rows**, so it counts declaration
line items (goods positions), each carrying one HS code. Averages differ
sharply between AWBs too: 3.7 lines per shipment on one, 11.0 on the other,
which is declaration complexity rather than parcel count.

The card and the chase sheet therefore label it *Declaration lines*. The
percentage is a secondary measure of customs workload; the headline figure
is and stays shipments cleared, because a shipment is what gets released.

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

### 3. Check the setup

```bash
make doctor          # or: lej-cc-doctor
```

Validates the config, both Slack tokens, storage permissions, the status
map, Slack authentication and a live PortGround download — each failure
naming its fix. Add `--offline` to skip the two network checks.

### 4. Smoke-test without Slack

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
| `src/lej_cc/report.py` | The short "still open" chase sheet |
| `src/lej_cc/formatting.py` | Block Kit messages |
| `src/lej_cc/slack_app.py` | Commands, buttons, mentions |
| `src/lej_cc/cli.py` | One-shot check, no Slack |

## Where this is going

[`docs/ROADMAP.md`](docs/ROADMAP.md) holds the audit findings -- fixed and
outstanding, with honest severities -- the design for email in and out, and
the full map of what is worth building, sized and ranked.

## Not built yet

- Google Drive archive of every workbook, Google Sheet dashboard
  (`GOOGLE_*` settings are wired but unused)
- Lead-time analytics over the `snapshots` table
- Pushing notification files back to `portground.notification@singular-it.de`
