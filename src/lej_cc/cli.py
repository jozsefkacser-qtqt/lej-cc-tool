"""One-shot command-line check -- no Slack required.

    python -m lej_cc.cli 488-20744846

Useful for smoke-testing the API key and the parser from a machine that can
reach PortGround, and for confirming a status mapping before deploying.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .awb import format_display, normalize
from .config import Settings
from .errors import LejCcError
from .formatting import progress_bar
from .parser import StatusMapper, parse_workbook
from .portground import PortGroundClient


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check customs clearance for a master AWB")
    parser.add_argument("mawb", help="e.g. 488-20744846")
    parser.add_argument("--file", type=Path, help="parse a local workbook instead of downloading")
    parser.add_argument("--list-open", action="store_true", help="print every open HAWB")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)

    try:
        mawb = normalize(args.mawb)
    except LejCcError as exc:
        print(f"error: {exc.user_message}", file=sys.stderr)
        return 2

    try:
        if args.file:
            snapshot = parse_workbook(args.file, mawb, mapper=StatusMapper.load())
        else:
            settings = Settings()  # type: ignore[call-arg]
            with PortGroundClient(settings) as client:
                path, name = client.download(mawb, settings.download_dir)
            snapshot = parse_workbook(path, mawb, source_filename=name)
    except LejCcError as exc:
        print(f"error: {exc.user_message}", file=sys.stderr)
        return 1

    print(f"MAWB {format_display(snapshot.mawb)}   {snapshot.source_filename}")
    print(f"  {progress_bar(snapshot.percent)}  {snapshot.percent:.1f}%")
    print(f"  cleared      {snapshot.cleared:>6,} / {snapshot.total:,}")
    print(f"  not cleared  {snapshot.not_cleared:>6,}")
    if snapshot.other:
        print(f"  other        {snapshot.other:>6,}   {snapshot.unknown_statuses}")
    print(f"  items        {snapshot.items_cleared:>6,} / {snapshot.items_total:,}")
    if snapshot.generated_at:
        print(f"  data as of   {snapshot.generated_at:%Y-%m-%d %H:%M:%S %Z}")

    if args.list_open:
        for row in snapshot.open_rows:
            print(f"    {row.hawb}  {row.final_status_raw}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
