# The central sheet

The bot keeps one row per AWB in a spreadsheet of its own, **CC Bot Central**.
Daily Report_LEJ pulls from it. Nothing writes into the report.

    https://docs.google.com/spreadsheets/d/1KNZfv8keVDz0DI3ruFhInLsTtkKnuEWsbFdBXb--1ss/edit

## Why a separate file

Daily Report_LEJ is twenty tabs of merged headers, SLA formulas and a chain of
milestones that people edit all day. A service account writing into that is one
column offset away from corrupting a live operational document, and the damage
would be silent.

So the bot owns one file outright. The worst a bug in it can do is spoil the
bot's own tab, and the report reaches across with `IMPORTRANGE` — a read. The
same central sheet can serve LGG, PRG, OTP and the rest later; only the bot's
data source is LEJ-specific, not this arrangement.

## What the bot writes

One row per AWB in the tab `CC_BOT`, created on first write. The header row is
the contract the report's lookups depend on, so columns are **appended, never
reordered**.

| Col | Header | Notes |
|-----|--------|-------|
| A | AWB | `936-02927610` for a waybill, `OyTM202608277404` for a booking reference |
| B | Tracked as | the booking reference, when that is what was tracked |
| C | Resolved MAWBs | the waybills a booking reference turned out to cover |
| D | State | `tracking`, `complete`, `escalated` |
| E | Cleared % | |
| F | Shipments | |
| G | Cleared | |
| H | Open | |
| I | Under inspection | held by customs for examination |
| J | Unrecognised | a status the map has no rule for |
| K | Customs lines | |
| L | Lines cleared | |
| M | Tracking started | |
| N | First clearance | |
| O | **CC Completed** | **only when every line cleared** — see below |
| P | Clearance duration (h) | first clearance → last |
| Q | Last checked | |
| R | Checks | |
| S | Escalated | |
| T | Requested by | |
| U | Source | |
| V | Unknown statuses | |
| W | Updated | |

### CC Completed is blank while customs is still holding something

Column O carries a timestamp only when **all** lines cleared. An AWB that is
"100% settled" because the remainder is under inspection has not completed
customs clearance, whatever the bot has stopped chasing. A date there that is
not true would feed the SLA chain a number nobody can defend, so it stays
blank and column I says how many parcels are held.

## Setting it up

### 1. A service account

Google Cloud console → **IAM & Admin → Service accounts → Create**.

- Name it something identifiable, e.g. `lej-cc-bot`.
- Skip the role grants — it needs no project permissions, only file access.
- Open it → **Keys → Add key → Create new key → JSON**. The file downloads once.
- Enable the **Google Sheets API** for the project
  (APIs & Services → Library → Google Sheets API → Enable).

Put the key on the machine that runs the bot, readable only by that user:

    mkdir -p ~/lej-cc-tool/secrets
    mv ~/Downloads/lej-cc-bot-*.json ~/lej-cc-tool/secrets/service-account.json
    chmod 600 ~/lej-cc-tool/secrets/service-account.json

`secrets/` is gitignored. The key is a credential: it never goes in the repo,
in Slack, or in an email.

### 2. Share the sheet with it

Open the key file and copy the `client_email` value — it looks like
`lej-cc-bot@<project>.iam.gserviceaccount.com`. Share **CC Bot Central** with
that address as **Editor**. Nothing else needs sharing; the service account
must not have access to the reports.

### 3. Install the Google client

It is an optional extra, so a plain install does not carry it:

    cd ~/lej-cc-tool && make google

Skip this and `awb doctor` fails the sheet check with
`the Google client libraries are not installed`.

### 4. Point the bot at it

In `.env`:

    GOOGLE_CREDENTIALS_FILE=secrets/service-account.json
    GOOGLE_SHEET_ID=1KNZfv8keVDz0DI3ruFhInLsTtkKnuEWsbFdBXb--1ss
    GOOGLE_SHEET_TAB=CC_BOT

Then:

    awb doctor      # the "sheet" line should name CC Bot Central
    awb restart

### 5. Backfill what is already tracked

    ~/lej-cc-tool/.venv/bin/lej-cc-sheet-sync --dry-run   # look first
    ~/lej-cc-tool/.venv/bin/lej-cc-sheet-sync

From then on each poll keeps its own row current.

## Connecting Daily Report_LEJ

In **every tab** of the report the column headers sit on **row 10** and data
starts on **row 11**. Column A is the AWB.

### Once per report file

Put this somewhere out of the way — a scratch cell on the first tab — and
click **Allow access** when it asks. `IMPORTRANGE` needs authorising once per
pair of files; after that every formula below works.

    =IMPORTRANGE("1KNZfv8keVDz0DI3ruFhInLsTtkKnuEWsbFdBXb--1ss", "CC_BOT!A1")

### The lookup

Column A of the report holds both shapes — `936-…` waybills and `OyTM…`
booking references — and the bot writes the same shape in its column A, with
the booking reference also in column B. So match on either:

    =LET(
       key,   $A11,
       bot,   IMPORTRANGE("1KNZfv8keVDz0DI3ruFhInLsTtkKnuEWsbFdBXb--1ss","CC_BOT!$A:$W"),
       hit,   IFERROR(VLOOKUP(key, bot, 15, FALSE),
              IFERROR(VLOOKUP(key, CHOOSECOLS(bot, 2, 15), 2, FALSE), "")),
       hit)

`15` is column **O, CC Completed**. Change that one number for any other
column: `5` = Cleared %, `9` = Under inspection, `4` = State.

Fill it down the tab. It returns blank — not `#N/A` — for an AWB the bot has
never been asked to track, so a half-populated column stays readable.

### Filling a whole column at once

One formula per tab instead of one per row:

    =ARRAYFORMULA(
       IF($A11:$A="", "",
          IFNA(VLOOKUP($A11:$A,
               IMPORTRANGE("1KNZfv8keVDz0DI3ruFhInLsTtkKnuEWsbFdBXb--1ss","CC_BOT!$A:$W"),
               15, FALSE), "")))

Put it in row 11 of a spare column and leave the rest of the column empty.

### Suggested placement

The report already has **Inspections** and **%** columns in the raw block on
the right. Those map straight onto the bot's columns `9` and `5`. `CC
Completed` in the left block is a computed SLA duration with a colour dot, not
a timestamp — feed the bot's column `15` into the **raw timestamp** the SLA
formula reads, not into the formula cell itself.

## Checking it works

    awb doctor

The `sheet` line names the spreadsheet the bot is actually pointed at. It
fails with the distinction that matters — wrong id versus not shared — and
warns if the id resolves to a title like "Daily Report", because that would
mean the bot is about to add a tab to a document people maintain by hand.
