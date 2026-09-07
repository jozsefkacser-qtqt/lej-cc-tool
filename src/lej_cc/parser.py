"""Reads PortGround's shipment-status workbook into `Snapshot` objects.

Format of the export, as verified against two live files:

  * one sheet ("Worksheet"), header in row 1, 17 columns
  * one row per house shipment / tracking number, no duplicates
  * every cell is a *string* -- numbers and timestamps included
  * timestamps are "YYYY-MM-DD HH:MM:SS" with no zone; the workbook's own
    docProps carry +02:00, i.e. Europe/Berlin
  * "Final Status" is the source of truth; "Clearance Time" agreed with it
    on all 1830 reference rows and is used as a corroborating signal

Columns are located by *name*, not position, so PortGround can reorder or
append columns without breaking us. Only three are required.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import openpyxl
import yaml

from .errors import AwbNotFound, MawbMismatch, SchemaDrift, UnexpectedPayload
from .model import ClearanceStatus, ShipmentRow, Snapshot

log = logging.getLogger(__name__)

LOCAL_TZ = ZoneInfo("Europe/Berlin")

COL_HAWB = "HAWB / Tracking number"
COL_MAWB = "MAWB"
COL_FINAL_STATUS = "Final Status"

REQUIRED_COLUMNS = (COL_HAWB, COL_MAWB, COL_FINAL_STATUS)

OPTIONAL_COLUMNS = (
    "Invoice Number",
    "MRN-ID",
    "ATX",
    "Check-In",
    "Internal Status",
    "External Statuses",
    "Declaration Sent",
    "Notification Customs Office",
    "Clearance Time",
    "Loaded",
    "MAWB Ready Outbound",
    "Impost",
    "Number of items",
    "Number of hs codes",
)

def _status_map_candidates() -> list[Path]:
    """Where to look for status_map.yaml, in order of preference.

    The installed package sits in site-packages, so a path relative to the
    module only works for a source checkout. In a container the file is
    mounted next to the working directory instead, which is what makes
    editing the mapping possible without rebuilding the image.
    """
    return [
        Path.cwd() / "config" / "status_map.yaml",
        Path(__file__).resolve().parents[2] / "config" / "status_map.yaml",
        Path("/app/config/status_map.yaml"),
    ]


class StatusMapper:
    """Turns a raw Final Status string into a `ClearanceStatus`."""

    def __init__(self, mapping: dict[str, list[str]]) -> None:
        self._lookup: dict[str, ClearanceStatus] = {}
        for bucket, values in mapping.items():
            try:
                status = ClearanceStatus(bucket)
            except ValueError as exc:  # pragma: no cover - config error
                raise SchemaDrift(f"unknown status bucket {bucket!r} in status map") from exc
            for value in values or []:
                self._lookup[self._key(value)] = status

    @staticmethod
    def _key(value: Any) -> str:
        return " ".join(str(value or "").split()).casefold()

    def map(self, raw: Any) -> ClearanceStatus:
        return self._lookup.get(self._key(raw), ClearanceStatus.OTHER)

    @classmethod
    def load(cls, path: Path | str | None = None) -> StatusMapper:
        if path:
            source: Path | None = Path(path)
            if not source.exists():  # type: ignore[union-attr]
                raise SchemaDrift(
                    f"status map {source} not found",
                    user_message=f"Configured status map {source} does not exist.",
                )
        else:
            source = next((p for p in _status_map_candidates() if p.exists()), None)

        if source is None:
            log.warning(
                "no status_map.yaml found in %s — using built-in defaults",
                [str(p) for p in _status_map_candidates()],
            )
            return cls({"cleared": ["cleared"], "not_cleared": ["not cleared", ""]})

        log.debug("loaded status map from %s", source)
        return cls(yaml.safe_load(source.read_text(encoding="utf-8")) or {})


def _clean(value: Any) -> str | None:
    """Trim a cell to a non-empty string, or None."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_dt(value: Any) -> datetime | None:
    """Parse an export timestamp, tagging it Europe/Berlin.

    Cells arrive as strings, but tolerate a real datetime in case PortGround
    ever starts writing typed cells.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=LOCAL_TZ)

    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%d.%m.%Y %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=LOCAL_TZ)
        except ValueError:
            continue
    log.warning("unparseable timestamp %r", text)
    return None


def _parse_int(value: Any) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def parse_workbook(
    path: Path | str,
    expected_mawb: str,
    *,
    mapper: StatusMapper | None = None,
    source_filename: str | None = None,
    fetched_at: datetime | None = None,
) -> Snapshot:
    """Parse `path` into a Snapshot, or raise a typed error explaining why not."""
    mapper = mapper or StatusMapper.load()
    path = Path(path)

    try:
        workbook = openpyxl.load_workbook(path, data_only=True, read_only=True)
    except Exception as exc:  # openpyxl raises a zoo of exception types
        head = path.read_bytes()[:200] if path.exists() else b""
        raise UnexpectedPayload(
            f"cannot open {path.name} as xlsx: {exc}; first bytes={head!r}",
            user_message=(
                "PortGround returned something that isn't an Excel file. "
                "The service may be down or the API key may have expired."
            ),
        ) from exc

    try:
        sheet = workbook.worksheets[0]
        rows = sheet.iter_rows(values_only=True)

        try:
            header = [_clean(c) for c in next(rows)]
        except StopIteration:
            raise UnexpectedPayload(
                f"{path.name} is empty",
                user_message="PortGround returned an empty file.",
            ) from None

        index = {name: i for i, name in enumerate(header) if name}
        missing = [c for c in REQUIRED_COLUMNS if c not in index]
        if missing:
            raise SchemaDrift(
                f"missing columns {missing} in {path.name}; header={header}",
                user_message=(
                    f"The export is missing the {', '.join(missing)} column(s). "
                    "PortGround appears to have changed the file format — "
                    "the tool needs updating."
                ),
            )

        def cell(row: tuple, column: str) -> Any:
            i = index.get(column)
            return row[i] if i is not None and i < len(row) else None

        shipments: list[ShipmentRow] = []
        unknown: dict[str, int] = {}
        foreign_mawbs: set[str] = set()

        for row in rows:
            if row is None or all(v in (None, "") for v in row):
                continue  # trailing blank rows

            hawb = _clean(cell(row, COL_HAWB))
            if not hawb:
                continue  # totals/footer lines have no tracking number

            row_mawb = (_clean(cell(row, COL_MAWB)) or "").replace("-", "").replace(" ", "")
            if row_mawb and row_mawb != expected_mawb:
                foreign_mawbs.add(row_mawb)
                continue

            raw_status = _clean(cell(row, COL_FINAL_STATUS))
            status = mapper.map(raw_status)
            if status is ClearanceStatus.OTHER:
                key = raw_status or "(blank)"
                unknown[key] = unknown.get(key, 0) + 1

            shipments.append(
                ShipmentRow(
                    hawb=hawb,
                    mawb=row_mawb or expected_mawb,
                    status=status,
                    final_status_raw=raw_status,
                    external_status=_clean(cell(row, "External Statuses")),
                    clearance_time=_parse_dt(cell(row, "Clearance Time")),
                    check_in=_parse_dt(cell(row, "Check-In")),
                    declaration_sent=_parse_dt(cell(row, "Declaration Sent")),
                    invoice_number=_clean(cell(row, "Invoice Number")),
                    mrn_id=_clean(cell(row, "MRN-ID")),
                    items=_parse_int(cell(row, "Number of items")),
                )
            )

        generated_at = getattr(workbook.properties, "created", None)
    finally:
        workbook.close()

    if not shipments:
        if foreign_mawbs:
            raise MawbMismatch(
                f"sheet contains {sorted(foreign_mawbs)}, expected {expected_mawb}",
                user_message=(
                    f"The file PortGround returned is for MAWB "
                    f"{', '.join(sorted(foreign_mawbs))}, not the one requested."
                ),
            )
        raise AwbNotFound(
            f"no shipment rows for {expected_mawb}",
            user_message=(
                "PortGround has no shipments on file for this MAWB yet. "
                "Either the number is wrong, or it hasn't been checked in."
            ),
        )

    if foreign_mawbs:
        log.warning("ignored rows for other MAWBs %s in %s", sorted(foreign_mawbs), path.name)
    if unknown:
        log.warning("unrecognised Final Status values in %s: %s", path.name, unknown)

    return Snapshot(
        mawb=expected_mawb,
        rows=shipments,
        generated_at=_parse_dt(generated_at),
        fetched_at=fetched_at or datetime.now(LOCAL_TZ),
        source_filename=source_filename or path.name,
        unknown_statuses=unknown,
    )
