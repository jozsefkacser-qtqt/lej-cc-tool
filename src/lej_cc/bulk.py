"""Start tracking every AWB on a list, instead of one at a time.

    lej-cc-track --from-sheet <id> --tab 2026.09 --dry-run
    lej-cc-track --from-file month.csv --yes

The tracker only ever knew the AWBs somebody typed `/awb` for. On a report
carrying forty a month that meant most rows had nothing behind them, and
every column downstream of the bot was empty for reasons no formula could
fix. This reads the list the operation already maintains and starts the
ones that are missing.

**Nothing here writes to the source.** It reads one column. Where that
column is a Google Sheet, share the file with the service account as
*Reader* -- the bot has no reason to be able to change an operational
report, and a read-only grant means it cannot.

**Starts are staggered.** Each AWB triggers an export that PortGround
generates on demand, measured at 96 seconds for 225 rows, and the scheduler
claims twenty jobs at a time. Forty AWBs all due at once is a thundering
herd against a system other people are using, so they are spread out.
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from .awb import format_display, normalize
from .config import Settings, configure_logging
from .errors import LejCcError
from .store import JobStore, utcnow

log = logging.getLogger(__name__)

#: A header that means "the AWB is in this column", lowercased.
AWB_HEADERS = ("awb", "mawb", "air waybill", "awb number", "waybill", "légifuvarlevél")

#: Minutes between one start and the next. See the module docstring.
DEFAULT_STAGGER = 2

#: Above this many, the preview says what the ongoing polling will cost.
BUSY_THRESHOLD = 20


# --- reading the list ---------------------------------------------------


def pick_awb_column(rows: list[list[str]]) -> int:
    """Which column holds the AWB. The first header that says so, else 0.

    Reports name the column, so look rather than make the caller count
    letters. Daily Report_LEJ has *two* columns headed AWB -- the key and a
    copy in the raw block -- and the first is the one rows are keyed on.
    """
    for row in rows[:20]:  # a header can sit well below row 1
        for index, cell in enumerate(row):
            if cell.strip().casefold() in AWB_HEADERS:
                return index
    return 0


def _column(rows: list[list[str]]) -> list[str]:
    index = pick_awb_column(rows)
    return [row[index].strip() for row in rows if len(row) > index and row[index].strip()]


def read_from_file(path: Path) -> list[str]:
    """Every value in the AWB column of a CSV, TSV or XLSX."""
    suffix = path.suffix.lower()
    if suffix in {".csv", ".tsv", ".txt"}:
        delimiter = "\t" if suffix == ".tsv" else ","
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = [[c or "" for c in row] for row in csv.reader(handle, delimiter=delimiter)]
    else:
        import openpyxl

        book = openpyxl.load_workbook(path, read_only=True, data_only=True)
        sheet = book.active
        rows = [
            [str(c).strip() if c is not None else "" for c in line]
            for line in sheet.iter_rows(values_only=True)
        ]
        book.close()
    return _column(rows)


def read_from_sheet(settings: Settings, spreadsheet_id: str, tab: str) -> list[str]:
    """Every value in the AWB column of one tab. Read-only."""
    from .sheets import build_service

    service = build_service(settings)
    values = (
        service.values()
        .get(spreadsheetId=spreadsheet_id, range=tab)
        .execute()
        .get("values", [])
    )
    return _column([[str(c) for c in row] for row in values])


# --- deciding what to do with it ----------------------------------------


@dataclass
class Plan:
    """What a run would do, before it does any of it."""

    to_start: list[str] = field(default_factory=list)
    already: list[str] = field(default_factory=list)
    #: AWB-shaped but rejected -- an 11-digit number failing its check digit
    #: is a typo worth naming, not a header to skip past.
    typos: list[tuple[str, str]] = field(default_factory=list)
    #: Headers, dates, blanks: everything that was never meant to be an AWB.
    ignored: int = 0

    @property
    def total(self) -> int:
        return len(self.to_start) + len(self.already) + len(self.typos) + self.ignored


def _looks_like_an_attempt(raw: str) -> bool:
    """Is this a failed AWB, or just a cell that was never one?

    Anything with eight or more digits was somebody typing a number at us.
    A header, a weight, a date or a depot name was not.
    """
    return sum(character.isdigit() for character in raw) >= 8


def plan_starts(raw_values: list[str], store: JobStore, channel: str) -> Plan:
    """Sort a column of cells into start / already tracked / typo / ignore."""
    plan = Plan()
    seen: set[str] = set()

    for raw in raw_values:
        try:
            mawb = normalize(raw)
        except LejCcError as exc:
            if _looks_like_an_attempt(raw):
                plan.typos.append((raw, exc.user_message or str(exc)))
            else:
                plan.ignored += 1
            continue

        if mawb in seen:
            plan.ignored += 1  # the same AWB twice on one sheet
            continue
        seen.add(mawb)

        if store.find_active(mawb, channel):
            plan.already.append(mawb)
        else:
            plan.to_start.append(mawb)

    return plan


def start_tracking(
    plan: Plan,
    store: JobStore,
    channel: str,
    *,
    requested_by: str | None = None,
    stagger_minutes: int = DEFAULT_STAGGER,
    source: str = "bulk",
) -> list[str]:
    """Create a job per AWB, spread over time. Returns the ones created."""
    started: list[str] = []
    now = utcnow()
    for position, mawb in enumerate(plan.to_start):
        run_at = now + timedelta(minutes=stagger_minutes * position)
        job = store.create_job(mawb, channel, requested_by, run_at=run_at, source=source)
        if job is None:
            # Raced with someone typing /awb between the plan and now.
            log.info("%s was already tracked by the time we got to it", mawb)
            continue
        started.append(mawb)
    return started


# --- the command --------------------------------------------------------


def _report(plan: Plan, stagger: int) -> None:
    print(f"{plan.total} cell(s) read\n")
    print(f"  {len(plan.to_start):>4} to start")
    print(f"  {len(plan.already):>4} already tracked")
    if plan.typos:
        print(f"  {len(plan.typos):>4} look like typos")
    print(f"  {plan.ignored:>4} not AWBs (headers, blanks, duplicates)")

    if plan.typos:
        print("\nProbably typos — these were not started:")
        for raw, why in plan.typos:
            print(f"  {raw}\n      {why.splitlines()[0]}")

    if plan.to_start:
        last = (len(plan.to_start) - 1) * stagger
        print(f"\nWould start {len(plan.to_start)}, one every {stagger} min "
              f"(the last in {last} min):")
        for mawb in plan.to_start:
            print(f"  {format_display(mawb)}")

    if len(plan.to_start) >= BUSY_THRESHOLD:
        _warn_about_load(len(plan.to_start))


def _warn_about_load(count: int) -> None:
    """Say what this costs before it is spent.

    Starting is staggered, but tracking is not: every one of these polls on
    the repeat interval until it clears or times out, and each poll is a
    full export PortGround generates on demand. Going from a handful of
    tracked AWBs to a whole month is a real step up in load on a system
    other people share, and it is better understood here than discovered.
    """
    settings = Settings()  # type: ignore[call-arg]
    per_hour = count * 60 / max(settings.repeat_interval_minutes, 1)
    print(
        f"\n  Note: once started, these poll every "
        f"{settings.repeat_interval_minutes} min for up to "
        f"{settings.max_tracking_hours} h — about {per_hour:.0f} exports an hour,\n"
        f"  against an API that takes ~100 s to build one. Consider --limit to "
        f"phase it in, or\n  a larger REPEAT_INTERVAL_MINUTES while this many are "
        f"being tracked."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Start tracking every AWB on a list, staggered so PortGround is not flooded"
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--from-file", type=Path, metavar="PATH", help="a CSV, TSV or XLSX")
    source.add_argument(
        "--from-sheet",
        metavar="ID",
        nargs="?",
        const="",
        help="a Google Sheet id; omit the value to use REPORT_SHEET_ID from .env",
    )
    parser.add_argument("--tab", help="which tab to read, e.g. 2026.09 (Google Sheet only)")
    parser.add_argument(
        "--channel", help="Slack channel to post to; defaults to the status channel"
    )
    parser.add_argument("--dry-run", action="store_true", help="show the plan, start nothing")
    parser.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    parser.add_argument("--limit", type=int, help="start at most this many")
    parser.add_argument(
        "--stagger-minutes",
        type=int,
        default=DEFAULT_STAGGER,
        help=f"minutes between starts (default {DEFAULT_STAGGER}); 0 starts them all at once",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    settings = Settings()  # type: ignore[call-arg]
    configure_logging("DEBUG" if args.verbose else "INFO")

    channel = args.channel or settings.status_channel
    if not channel:
        print(
            "No channel to post to. Pass --channel, or set SLACK_STATUS_CHANNEL "
            "or SLACK_OPS_CHANNEL in .env.",
            file=sys.stderr,
        )
        return 2

    # --- read the list
    try:
        if args.from_file is not None:
            if not args.from_file.is_file():
                print(f"No such file: {args.from_file}", file=sys.stderr)
                return 2
            raw_values = read_from_file(args.from_file)
            where = str(args.from_file)
        else:
            spreadsheet_id = args.from_sheet or settings.report_sheet_id
            if not spreadsheet_id:
                print(
                    "No spreadsheet id. Pass it to --from-sheet, or set "
                    "REPORT_SHEET_ID in .env.",
                    file=sys.stderr,
                )
                return 2
            if not args.tab:
                print("--tab is required with --from-sheet, e.g. --tab 2026.09", file=sys.stderr)
                return 2
            raw_values = read_from_sheet(settings, spreadsheet_id, args.tab)
            where = f"{spreadsheet_id} tab {args.tab!r}"
    except ImportError:
        print(
            "The Google client libraries are not installed. In the repo, run: "
            "\".venv/bin/pip install -e '.[google]'\"",
            file=sys.stderr,
        )
        return 2
    except Exception as exc:  # noqa: BLE001 - a bad id, no access, a missing tab
        print(f"Could not read the list: {exc}", file=sys.stderr)
        return 1

    print(f"Read {len(raw_values)} value(s) from {where}\n")

    store = JobStore(settings.database_path)
    plan = plan_starts(raw_values, store, channel)
    if args.limit is not None:
        plan.to_start = plan.to_start[: args.limit]

    _report(plan, args.stagger_minutes)

    if args.dry_run:
        print("\n(dry run — nothing was started)")
        return 0
    if not plan.to_start:
        print("\nNothing to start.")
        return 0

    if not args.yes:
        if not sys.stdin.isatty():
            print(
                f"\nRefusing to start {len(plan.to_start)} AWB(s) unattended. "
                "Pass --yes when running from a script, or --dry-run to preview.",
                file=sys.stderr,
            )
            return 2
        answer = input(f"\nStart {len(plan.to_start)} AWB(s) in {channel}? [y/N] ")
        if answer.strip().lower() not in {"y", "yes"}:
            print("Nothing started.")
            return 0

    started = start_tracking(
        plan, store, channel, stagger_minutes=args.stagger_minutes, source="bulk"
    )
    print(f"\n{len(started)} started in {channel}.")
    if len(started) != len(plan.to_start):
        print(f"{len(plan.to_start) - len(started)} were picked up by someone else meanwhile.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
