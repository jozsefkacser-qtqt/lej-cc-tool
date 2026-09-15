"""One row per AWB in a Google Sheet.

Deliberately writes to its **own tab**, never into the hand-maintained
report. Daily Report_LEJ carries hundreds of rows, merged headers, formulas
and an SLA chain that people edit; a bot inferring column positions and
writing into that is one offset away from corrupting a live operational
document. A tab the bot owns outright can be pulled into the report by
lookup, and the worst a bug can do is spoil the bot's own tab.

The AWB is written in the same `936-02927610` form the report already uses,
so a VLOOKUP against it needs no massaging.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .awb import format_display, is_mawb
from .model import Snapshot

log = logging.getLogger(__name__)

LOCAL_TZ = ZoneInfo("Europe/Berlin")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

#: (header, how to get it). The header row is written on first use and is
#: the contract the report's lookups depend on, so append rather than
#: reorder when this grows.
COLUMNS: list[tuple[str, str]] = [
    ("AWB", "awb"),
    ("Tracked as", "tracked_as"),
    ("Resolved MAWBs", "resolved_mawbs"),
    ("State", "state"),
    ("Cleared %", "percent"),
    ("Shipments", "total"),
    ("Cleared", "cleared"),
    ("Open", "open"),
    ("Unrecognised", "other"),
    ("Declaration lines", "lines_total"),
    ("Lines cleared", "lines_cleared"),
    ("Tracking started", "started_at"),
    ("First clearance", "first_clearance"),
    ("CC Completed", "cc_completed"),
    ("Clearance duration (h)", "clearance_hours"),
    ("Last checked", "last_checked"),
    ("Checks", "checks"),
    ("Escalated", "escalated_at"),
    ("Requested by", "requested_by"),
    ("Source", "source"),
    ("Unknown statuses", "unknown_statuses"),
    ("Updated", "updated_at"),
]

HEADERS = [header for header, _ in COLUMNS]


def _stamp(value: datetime | None) -> str:
    """The report's own timestamp format, so the two read alike."""
    return value.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M") if value else ""


@dataclass
class SheetRow:
    """Everything known about one AWB, flattened to a line."""

    values: dict[str, Any]

    @property
    def key(self) -> str:
        return str(self.values.get("awb", ""))

    def as_list(self) -> list[Any]:
        return [self.values.get(field, "") for _, field in COLUMNS]


def build_row(job, snapshot: Snapshot | None = None) -> SheetRow:  # noqa: ANN001
    """Flatten a job, and its latest snapshot when there is one."""
    tracked = job.mawb
    display = format_display(tracked)

    values: dict[str, Any] = {
        "awb": display,
        # Only a booking reference belongs here. A waybill's raw digits are
        # just the display form without its dash, which says nothing.
        "tracked_as": "" if is_mawb(tracked) else tracked,
        "state": job.state,
        "started_at": _stamp(job.created_at),
        "last_checked": _stamp(job.last_polled_at),
        "checks": job.poll_count,
        "escalated_at": _stamp(job.escalated_at),
        "requested_by": job.requested_by or "",
        "source": job.source,
        "updated_at": _stamp(datetime.now(LOCAL_TZ)),
    }

    if snapshot is not None:
        first, last = snapshot.first_clearance, snapshot.last_clearance
        values.update(
            {
                "resolved_mawbs": ", ".join(
                    format_display(m) for m in snapshot.resolved_mawbs
                ),
                "percent": snapshot.percent,
                "total": snapshot.total,
                "cleared": snapshot.cleared,
                "open": snapshot.not_cleared + snapshot.other,
                "other": snapshot.other,
                "lines_total": snapshot.items_total,
                "lines_cleared": snapshot.items_cleared,
                "first_clearance": _stamp(first),
                # The column the report's SLA chain already has a slot for.
                "cc_completed": _stamp(last) if snapshot.is_complete else "",
                "clearance_hours": (
                    round((last - first).total_seconds() / 3600, 2)
                    if first and last and last > first
                    else ""
                ),
                "unknown_statuses": ", ".join(
                    f"{k} x{v}" for k, v in sorted(snapshot.unknown_statuses.items())
                ),
            }
        )
    else:
        # No successful poll yet: carry what the store remembers rather than
        # writing a row of blanks over a row that had numbers in it.
        values.update(
            {
                "percent": job.last_percent if job.last_percent is not None else "",
                "total": job.last_total or "",
                "cleared": job.last_cleared or "",
            }
        )

    return SheetRow(values)


def apply_snapshot_record(row: SheetRow, record) -> SheetRow:  # noqa: ANN001
    """Fill a row from a stored snapshot, for AWBs no longer being tracked.

    The live path has the parsed workbook; a backfill has only what the
    database kept, which is why the clearance timestamps are now recorded.
    """
    if record is None:
        return row

    def stamp(key: str) -> str:
        raw = record[key] if key in record.keys() else None
        return _stamp(datetime.fromisoformat(raw)) if raw else ""

    first_raw = record["first_clearance"] if "first_clearance" in record.keys() else None
    last_raw = record["last_clearance"] if "last_clearance" in record.keys() else None
    complete = record["total"] and record["cleared"] == record["total"]

    row.values.update(
        {
            "percent": record["percent"],
            "total": record["total"],
            "cleared": record["cleared"],
            "open": record["not_cleared"] + record["other"],
            "other": record["other"],
            "lines_total": record["items_total"],
            "lines_cleared": record["items_cleared"],
            "first_clearance": stamp("first_clearance"),
            "cc_completed": stamp("last_clearance") if complete else "",
            "clearance_hours": (
                round(
                    (
                        datetime.fromisoformat(last_raw) - datetime.fromisoformat(first_raw)
                    ).total_seconds()
                    / 3600,
                    2,
                )
                if first_raw and last_raw and last_raw > first_raw
                else ""
            ),
        }
    )
    return row


class SheetExporter:
    """Upserts rows into one tab. Never raises into the polling cycle."""

    def __init__(self, settings) -> None:  # noqa: ANN001 - avoids a circular import
        self.settings = settings
        self._service: Any = None
        self._header_checked = False

    @property
    def enabled(self) -> bool:
        return bool(
            self.settings.google_sheet_id and self.settings.google_credentials_file
        )

    # --- Google plumbing -------------------------------------------------

    def _connect(self) -> Any:
        if self._service is not None:
            return self._service
        # Imported lazily so the bot runs without the Google libraries when
        # the export is switched off, which is the default.
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        credentials = service_account.Credentials.from_service_account_file(
            str(self.settings.google_credentials_file), scopes=SCOPES
        )
        self._service = build("sheets", "v4", credentials=credentials).spreadsheets()
        return self._service

    def _ensure_tab(self, service: Any) -> None:
        """Create the tab and its header row the first time."""
        if self._header_checked:
            return
        tab = self.settings.google_sheet_tab
        meta = service.get(spreadsheetId=self.settings.google_sheet_id).execute()
        titles = {s["properties"]["title"] for s in meta.get("sheets", [])}

        if tab not in titles:
            service.batchUpdate(
                spreadsheetId=self.settings.google_sheet_id,
                body={"requests": [{"addSheet": {"properties": {"title": tab}}}]},
            ).execute()
            log.info("created sheet tab %r", tab)

        existing = (
            service.values()
            .get(spreadsheetId=self.settings.google_sheet_id, range=f"{tab}!1:1")
            .execute()
            .get("values", [[]])
        )
        if not existing or existing[0][: len(HEADERS)] != HEADERS:
            service.values().update(
                spreadsheetId=self.settings.google_sheet_id,
                range=f"{tab}!A1",
                valueInputOption="RAW",
                body={"values": [HEADERS]},
            ).execute()
            log.info("wrote header row to %r", tab)
        self._header_checked = True

    def _row_number(self, service: Any, key: str) -> int | None:
        """Find the sheet row holding `key`, or None. Row 1 is the header."""
        tab = self.settings.google_sheet_tab
        column = (
            service.values()
            .get(spreadsheetId=self.settings.google_sheet_id, range=f"{tab}!A:A")
            .execute()
            .get("values", [])
        )
        for index, row in enumerate(column, start=1):
            if row and row[0].strip() == key:
                return index
        return None

    # --- the operation ---------------------------------------------------

    def upsert(self, row: SheetRow) -> bool:
        """Write one AWB's line. Returns whether it reached the sheet."""
        if not self.enabled or not row.key:
            return False
        try:
            service = self._connect()
            self._ensure_tab(service)
            tab = self.settings.google_sheet_tab
            body = {"values": [row.as_list()]}

            existing = self._row_number(service, row.key)
            if existing:
                service.values().update(
                    spreadsheetId=self.settings.google_sheet_id,
                    range=f"{tab}!A{existing}",
                    valueInputOption="USER_ENTERED",
                    body=body,
                ).execute()
            else:
                service.values().append(
                    spreadsheetId=self.settings.google_sheet_id,
                    range=f"{tab}!A:A",
                    valueInputOption="USER_ENTERED",
                    insertDataOption="INSERT_ROWS",
                    body=body,
                ).execute()
        except Exception as exc:  # noqa: BLE001
            # The sheet is a convenience; Slack has already carried the
            # update and SQLite is the record. Never fail a poll over it.
            log.error("could not write %s to the sheet: %s", row.key, exc)
            return False

        log.info("wrote %s to sheet tab %r", row.key, self.settings.google_sheet_tab)
        return True
