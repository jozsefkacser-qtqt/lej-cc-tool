"""Domain types: one parsed shipment line, an aggregated snapshot, and the
difference between two snapshots.

The diff is what the Slack updates are actually built from -- "+4 cleared
since 14:15" is the sentence ops acts on, not the running total.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class ClearanceStatus(StrEnum):
    """The buckets every shipment line falls into."""

    CLEARED = "cleared"
    NOT_CLEARED = "not_cleared"
    #: Customs has selected the shipment for examination. Not cleared, but
    #: also not something the team can chase: it is a customs decision on a
    #: customs timetable. Counted separately so an AWB waiting on three
    #: inspections does not read as an AWB with three unworked shipments.
    INSPECTION = "inspection"
    #: A Final Status value we have never seen before. Deliberately not
    #: folded into NOT_CLEARED: an unrecognised status must be visible, so a
    #: new value like "blocked" or "seized" surfaces instead of hiding.
    OTHER = "other"


@dataclass(frozen=True)
class ShipmentRow:
    """One line of the export -- one house shipment under the master AWB."""

    hawb: str
    mawb: str
    status: ClearanceStatus
    final_status_raw: str | None = None
    external_status: str | None = None
    clearance_time: datetime | None = None
    check_in: datetime | None = None
    declaration_sent: datetime | None = None
    notification_customs_office: datetime | None = None
    invoice_number: str | None = None
    mrn_id: str | None = None
    #: PortGround's "Number of items" column. Despite the name this counts
    #: declaration line items, not parcels: in the reference export it equals
    #: "Number of hs codes" on every one of the 1605 rows. Displayed as
    #: "Customs lines" so nobody reads it as a piece count.
    items: int = 0

    @property
    def is_cleared(self) -> bool:
        return self.status is ClearanceStatus.CLEARED

    @property
    def is_inspection(self) -> bool:
        return self.status is ClearanceStatus.INSPECTION

    @property
    def is_settled(self) -> bool:
        """Nothing further is expected of us: cleared, or with customs."""
        return self.is_cleared or self.is_inspection

    @property
    def inconsistent(self) -> bool:
        """Final Status and Clearance Time disagree.

        In both reference exports these agreed on every one of the 1830 rows
        (cleared <=> a clearance timestamp), so a row where they diverge means
        either PortGround changed something or we are reading a partial write.
        Worth surfacing, not worth failing on.
        """
        if self.is_inspection:
            return self.clearance_time is not None
        return self.is_cleared != (self.clearance_time is not None)


@dataclass
class Snapshot:
    """Aggregated state of one MAWB at one point in time."""

    mawb: str
    rows: list[ShipmentRow]
    #: When PortGround generated the workbook (from its docProps), not when
    #: we downloaded it -- that is the real freshness of the data.
    generated_at: datetime | None = None
    fetched_at: datetime | None = None
    source_filename: str | None = None
    #: Raw Final Status values that did not map to a known bucket, with counts.
    unknown_statuses: dict[str, int] = field(default_factory=dict)
    #: When tracking by a booking reference rather than an air waybill, the
    #: actual MAWB numbers the returned rows belong to. Empty when the thing
    #: being tracked is itself a MAWB.
    resolved_mawbs: list[str] = field(default_factory=list)

    # --- headline numbers ---

    @property
    def total(self) -> int:
        return len(self.rows)

    @property
    def cleared(self) -> int:
        return sum(1 for r in self.rows if r.status is ClearanceStatus.CLEARED)

    @property
    def not_cleared(self) -> int:
        return sum(1 for r in self.rows if r.status is ClearanceStatus.NOT_CLEARED)

    @property
    def other(self) -> int:
        return sum(1 for r in self.rows if r.status is ClearanceStatus.OTHER)

    @property
    def inspection(self) -> int:
        return sum(1 for r in self.rows if r.status is ClearanceStatus.INSPECTION)

    @property
    def open_count(self) -> int:
        """Shipments somebody can still do something about."""
        return self.total - self.cleared - self.inspection

    @property
    def percent(self) -> float:
        """Completeness by shipment count. An empty sheet is 0%, not 100%."""
        return round(100.0 * self.cleared / self.total, 1) if self.total else 0.0

    @property
    def percent_inspection(self) -> float:
        return round(100.0 * self.inspection / self.total, 1) if self.total else 0.0

    @property
    def percent_settled(self) -> float:
        """Cleared plus under inspection -- everything not still open.

        Computed from the counts rather than by adding the two rounded
        percentages, so 33.33 + 66.67 cannot print as 100.1.
        """
        if not self.total:
            return 0.0
        return round(100.0 * (self.cleared + self.inspection) / self.total, 1)

    # --- secondary measure: pieces ---

    @property
    def items_total(self) -> int:
        return sum(r.items for r in self.rows)

    @property
    def items_cleared(self) -> int:
        return sum(r.items for r in self.rows if r.is_cleared)

    @property
    def items_inspection(self) -> int:
        return sum(r.items for r in self.rows if r.is_inspection)

    @property
    def items_percent(self) -> float:
        return round(100.0 * self.items_cleared / self.items_total, 1) if self.items_total else 0.0

    @property
    def all_cleared(self) -> bool:
        """Every line genuinely released by customs."""
        return self.total > 0 and self.cleared == self.total

    @property
    def is_complete(self) -> bool:
        """Nothing left that anyone here can move.

        Every line is either cleared or held by customs for examination. An
        AWB in this state is finished as far as this tool is concerned: the
        remainder is a customs decision on a customs timetable, and polling
        it every thirty minutes for two days does not change it. The final
        message names the inspections so they are not lost.

        A sheet with zero rows is never complete.
        """
        return self.total > 0 and self.cleared + self.inspection == self.total

    @property
    def open_rows(self) -> list[ShipmentRow]:
        """The lines somebody here has to chase.

        Inspections are excluded on purpose: they are not unworked shipments
        waiting for someone, they are shipments customs has taken. Counting
        them as open makes a finished AWB look like a stuck one.
        """
        return [r for r in self.rows if not r.is_settled]

    @property
    def inspection_rows(self) -> list[ShipmentRow]:
        return [r for r in self.rows if r.is_inspection]

    @property
    def unsettled_rows(self) -> list[ShipmentRow]:
        """Everything not cleared -- open *and* inspection.

        What the chase sheet lists: an inspection needs no chasing, but it
        does need to stay visible, and a row that vanishes from the
        attachment is a row nobody looks at again.
        """
        return [r for r in self.rows if not r.is_cleared]

    def status_by_hawb(self) -> dict[str, str]:
        """Compact form persisted between polls so the next run can diff."""
        return {r.hawb: r.status.value for r in self.rows}

    @property
    def last_clearance(self) -> datetime | None:
        """When the final shipment cleared -- i.e. when this AWB finished."""
        stamps = [r.clearance_time for r in self.rows if r.clearance_time]
        return max(stamps) if stamps else None

    @property
    def first_clearance(self) -> datetime | None:
        stamps = [r.clearance_time for r in self.rows if r.clearance_time]
        return min(stamps) if stamps else None


@dataclass
class SnapshotDiff:
    """What changed between the previous poll and this one."""

    newly_cleared: list[str] = field(default_factory=list)
    newly_added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    #: Was cleared, now is not. Should never happen; if it does, say so loudly.
    regressed: list[str] = field(default_factory=list)
    #: Newly taken by customs for examination. Reported in its own right:
    #: it is the moment somebody may need to tell a customer.
    newly_inspected: list[str] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(
            self.newly_cleared
            or self.newly_added
            or self.removed
            or self.regressed
            or self.newly_inspected
        )

    @property
    def delta_cleared(self) -> int:
        return len(self.newly_cleared) - len(self.regressed)


def diff_snapshots(previous: dict[str, str] | None, current: Snapshot) -> SnapshotDiff:
    """Compare a persisted `status_by_hawb` map against a fresh snapshot."""
    diff = SnapshotDiff()
    if previous is None:
        return diff

    now = current.status_by_hawb()
    cleared = ClearanceStatus.CLEARED.value
    inspection = ClearanceStatus.INSPECTION.value

    for hawb, status in now.items():
        was = previous.get(hawb)
        if was is None:
            diff.newly_added.append(hawb)
        elif was != cleared and status == cleared:
            diff.newly_cleared.append(hawb)
        elif was == cleared and status != cleared:
            diff.regressed.append(hawb)
        elif was != inspection and status == inspection:
            diff.newly_inspected.append(hawb)

    diff.removed = [h for h in previous if h not in now]
    return diff
