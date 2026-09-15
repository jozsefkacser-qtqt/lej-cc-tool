"""Push every AWB the bot knows about into the sheet.

    lej-cc-sheet-sync            # everything
    lej-cc-sheet-sync --dry-run  # print what would be written

The live path keeps rows current as it polls; this is for the first run,
for AWBs tracked before the export existed, and for putting the tab back
after someone edits it by hand.
"""

from __future__ import annotations

import argparse
import logging
import sys

from .config import Settings, configure_logging
from .sheets import HEADERS, SheetExporter, apply_snapshot_record, build_row
from .store import JobStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write every tracked AWB into the sheet")
    parser.add_argument("--dry-run", action="store_true", help="print rows, write nothing")
    parser.add_argument("--limit", type=int, help="only the most recent N jobs")
    args = parser.parse_args(argv)

    settings = Settings()  # type: ignore[call-arg]
    configure_logging("INFO")
    log = logging.getLogger("lej_cc.sheet_sync")

    exporter = SheetExporter(settings)
    if not exporter.enabled and not args.dry_run:
        print(
            "Sheet export is off. Set GOOGLE_SHEET_ID and GOOGLE_CREDENTIALS_FILE "
            "in .env, or pass --dry-run.",
            file=sys.stderr,
        )
        return 2

    store = JobStore(settings.database_path)
    jobs = store.list_all_jobs()
    if args.limit:
        jobs = jobs[-args.limit :]

    if not jobs:
        print("Nothing tracked yet — no rows to write.")
        return 0

    print(f"{len(jobs)} AWB(s) to write into tab {settings.google_sheet_tab!r}\n")
    written = failed = 0
    for job in jobs:
        row = apply_snapshot_record(build_row(job), store.latest_snapshot(job.id))
        if args.dry_run:
            shown = zip(HEADERS, row.as_list(), strict=True)
            print(" | ".join(f"{h}={v}" for h, v in shown if v != ""))
            continue
        if exporter.upsert(row):
            written += 1
        else:
            failed += 1
            log.warning("could not write %s", row.key)

    if args.dry_run:
        return 0
    print(f"\n{written} written, {failed} failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
