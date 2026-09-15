"""Clearance statistics from the accumulated history.

    lej-cc-stats                # the last 90 days
    lej-cc-stats --days 30
    lej-cc-stats --csv out.csv  # one row per finished AWB, for a spreadsheet
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from .analytics import gather, summarise
from .config import Settings, configure_logging
from .store import JobStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Clearance statistics")
    parser.add_argument("--days", type=int, default=90, help="window in days (default 90)")
    parser.add_argument("--csv", type=Path, help="also write one row per finished AWB")
    args = parser.parse_args(argv)

    configure_logging("WARNING")
    settings = Settings()  # type: ignore[call-arg]
    stats = gather(JobStore(settings.database_path), window_days=args.days)

    # The Slack report and this share one summariser so they cannot drift.
    for line in summarise(stats):
        print(line.replace("*", "").replace("`", "").replace("_", ""))

    if args.csv:
        with args.csv.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["AWB", "State", "Shipments", "Clearance hours", "Finished"])
            for outcome in stats.outcomes:
                writer.writerow(
                    [
                        outcome.mawb,
                        outcome.state,
                        outcome.shipments,
                        outcome.clearance_hours if outcome.clearance_hours is not None else "",
                        outcome.finished_at.isoformat() if outcome.finished_at else "",
                    ]
                )
        print(f"\nWrote {len(stats.outcomes)} row(s) to {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
