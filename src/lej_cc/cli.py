"""One-shot command-line check -- no Slack required.

    python -m lej_cc.cli 488-20744846

Useful for smoke-testing the API key and the parser from a machine that can
reach PortGround, and for confirming a status mapping before deploying.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .awb import format_display, normalize
from .config import Settings, configure_logging
from .errors import LejCcError
from .formatting import progress_bar
from .parser import StatusMapper, parse_workbook
from .portground import PortGroundClient


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check customs clearance for a master AWB")
    parser.add_argument("mawb", help="e.g. 488-20744846")
    parser.add_argument("--file", type=Path, help="parse a local workbook instead of downloading")
    parser.add_argument("--list-open", action="store_true", help="print every open HAWB")
    parser.add_argument(
        "--inspection-list",
        nargs="?",
        const="-",
        metavar="LOOKUP",
        help="write the printable pick list of parcels customs is holding, "
             "optionally joined to a CSV/XLSX of box ids keyed by tracking number",
    )
    parser.add_argument(
        "--statuses",
        action="store_true",
        help="list the raw Final Status values in this export and what they map to",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    configure_logging("DEBUG" if args.verbose else "WARNING")

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
    print(
        f"  {progress_bar(snapshot.percent, inspection=snapshot.percent_inspection)}"
        f"  {snapshot.percent:.1f}%"
        + (
            f" cleared + {snapshot.percent_inspection:.1f}% inspection"
            f" = {snapshot.percent_settled:.1f}%"
            if snapshot.inspection
            else ""
        )
    )
    print(f"  cleared      {snapshot.cleared:>6,} / {snapshot.total:,}")
    print(f"  open         {snapshot.open_count:>6,}")
    if snapshot.inspection:
        print(f"  inspection   {snapshot.inspection:>6,}   taken by customs")
    if snapshot.other:
        print(f"  other        {snapshot.other:>6,}   {snapshot.unknown_statuses}")
    print(f"  decl. lines  {snapshot.items_cleared:>6,} / {snapshot.items_total:,}")
    if snapshot.resolved_mawbs:
        shown = ", ".join(format_display(m) for m in snapshot.resolved_mawbs[:8])
        extra = f" (+{len(snapshot.resolved_mawbs) - 8} more)" if len(
            snapshot.resolved_mawbs) > 8 else ""
        print(f"  covers       {len(snapshot.resolved_mawbs)} MAWB(s): {shown}{extra}")
    if snapshot.generated_at:
        print(f"  data as of   {snapshot.generated_at:%Y-%m-%d %H:%M:%S %Z}")

    if args.inspection_list:
        from .picklist import build_inspection_list, load_lookup

        lookup = load_lookup(None if args.inspection_list == "-" else args.inspection_list)
        written = build_inspection_list(snapshot, Path.cwd(), lookup)
        if written is None:
            print("  no parcels are under inspection — nothing to pick")
        else:
            print(f"  pick list   {written}")

    if args.statuses:
        # What Final Status values this export actually contains. The one
        # question the card cannot answer, and the one you need before
        # adding a value to status_map.yaml.
        counts: dict[str, int] = {}
        for row in snapshot.rows:
            key = f"{row.final_status_raw!r} -> {row.status.value}"
            counts[key] = counts.get(key, 0) + 1
        print("  Final Status values:")
        for key, count in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"    {count:>6,}  {key}")

    if args.list_open:
        for row in snapshot.unsettled_rows:
            mark = "  [INSPECTION]" if row.is_inspection else ""
            print(f"    {row.hawb}  {row.final_status_raw}{mark}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
