"""Generates the test workbooks.

Deliberately synthetic: the real exports contain live customer invoice
numbers, MRNs and tracking numbers, which must not go into version control.
The schema, cell types (everything is a string) and status vocabulary match
the two reference files exactly.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import openpyxl

HEADER = [
    "HAWB / Tracking number", "Invoice Number", "MRN-ID", "ATX", "MAWB", "Check-In",
    "Internal Status", "External Statuses", "Final Status", "Declaration Sent",
    "Notification Customs Office", "Clearance Time", "Loaded", "MAWB Ready Outbound",
    "Impost", "Number of items", "Number of hs codes",
]


def _row(i: int, mawb: str, status: str, items: int = 2) -> list[str | None]:
    hawb = f"0034043{i:013d}"
    cleared = status == "cleared"
    return [
        hawb,
        f"BG-TEST{i:08d}",
        "26DE02000TESTFIXTURE",
        f"ATX{i:018d}" if cleared else None,
        mawb,
        "2026-05-20 09:11:58" if cleared else None,
        None,
        "pre_cleared" if cleared else None,
        status,
        "2026-05-18 08:02:31" if cleared else None,
        "2026-05-18 08:04:13" if cleared else None,
        "2026-05-20 09:36:43" if cleared else None,
        None,
        "2026-05-20 14:38:17" if cleared else None,
        "Y" if cleared else None,
        str(items),
        None if cleared else str(items),
    ]


def write(path: Path, mawb: str, statuses: list[str]) -> None:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Worksheet"
    sheet.append(HEADER)
    for i, status in enumerate(statuses, start=1):
        sheet.append(_row(i, mawb, status))
    # Fixed creation stamp so `generated_at` parsing is deterministic.
    workbook.properties.created = datetime(2026, 9, 7, 15, 5, 33)
    workbook.save(path)


def write_all(out_dir: Path) -> Path:
    """Write the full fixture set into `out_dir` and return it.

    Called by conftest at the start of every test session, so the workbooks
    are never committed. They are generated data, and a binary in git is a
    binary somebody eventually edits by hand.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    write(out_dir / "all_cleared.xlsx", "48820744846", ["cleared"] * 10)
    write(out_dir / "none_cleared.xlsx", "93600333955", ["not cleared"] * 12)
    write(out_dir / "partial.xlsx", "48820744846", ["cleared"] * 6 + ["not cleared"] * 4)
    write(out_dir / "unknown_status.xlsx", "48820744846", ["cleared"] * 3 + ["blocked", "seized"])
    write(out_dir / "empty.xlsx", "48820744846", [])
    # A file whose rows belong to a different master AWB.
    write(out_dir / "wrong_mawb.xlsx", "12345678901", ["cleared"] * 3)
    return out_dir


def main() -> None:
    """Write them to tests/data too, for eyeballing in Excel."""
    out = write_all(Path(__file__).parent / "data")
    print("fixtures written to", out)


if __name__ == "__main__":
    main()
