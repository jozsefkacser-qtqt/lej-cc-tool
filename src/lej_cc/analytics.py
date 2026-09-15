"""What the accumulated history says.

Every poll has been recording a snapshot, and since the sheet export the
clearance timestamps too. Nobody else in the chain has this data: how long
customs actually takes at LEJ, how that varies, and which AWBs sat longest.

Reported from what is measurable and no further. Per-shipment questions --
which consignees habitually block -- need per-HAWB history the snapshots
table does not keep, and are called out as such rather than approximated.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .awb import format_display
from .store import JobStore, utcnow

log = logging.getLogger(__name__)


def percentile(values: list[float], fraction: float) -> float | None:
    """Nearest-rank percentile. None for an empty sample."""
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round(fraction * (len(ordered) - 1))))
    return ordered[index]


@dataclass
class AwbOutcome:
    """One finished AWB, reduced to what can be compared across AWBs."""

    mawb: str
    state: str
    shipments: int
    clearance_hours: float | None
    finished_at: datetime | None


@dataclass
class Stats:
    window_days: int
    considered: int = 0
    outcomes: list[AwbOutcome] = field(default_factory=list)
    by_state: dict[str, int] = field(default_factory=dict)
    #: Outcomes with no usable clearance timestamps: tracked before those
    #: were recorded, or nothing ever cleared. Reported rather than dropped,
    #: so the sample size behind a median is visible.
    without_timings: int = 0

    @property
    def durations(self) -> list[float]:
        return [o.clearance_hours for o in self.outcomes if o.clearance_hours is not None]

    @property
    def completed(self) -> int:
        return self.by_state.get("complete", 0)

    @property
    def shipments_total(self) -> int:
        return sum(o.shipments for o in self.outcomes)

    @property
    def median_hours(self) -> float | None:
        return percentile(self.durations, 0.5)

    @property
    def p90_hours(self) -> float | None:
        return percentile(self.durations, 0.9)

    @property
    def slowest(self) -> list[AwbOutcome]:
        timed = [o for o in self.outcomes if o.clearance_hours is not None]
        return sorted(timed, key=lambda o: o.clearance_hours or 0, reverse=True)[:5]

    @property
    def has_enough_to_report(self) -> bool:
        """Three AWBs is an anecdote. Say so rather than printing a median."""
        return len(self.durations) >= 3


def gather(store: JobStore, *, window_days: int = 90, now: datetime | None = None) -> Stats:
    """Reduce the stored history to comparable per-AWB outcomes."""
    now = now or utcnow()
    cutoff = now - timedelta(days=window_days)
    stats = Stats(window_days=window_days)

    for job in store.list_all_jobs():
        if job.created_at < cutoff:
            continue
        stats.considered += 1
        stats.by_state[job.state] = stats.by_state.get(job.state, 0) + 1
        if job.state == "active":
            continue  # not an outcome yet

        record = store.latest_snapshot(job.id)
        hours = _clearance_hours(record)
        if hours is None:
            stats.without_timings += 1

        stats.outcomes.append(
            AwbOutcome(
                mawb=format_display(job.mawb),
                state=job.state,
                shipments=(record["total"] if record else 0) or 0,
                clearance_hours=hours,
                finished_at=job.last_polled_at,
            )
        )
    return stats


def _clearance_hours(record) -> float | None:  # noqa: ANN001 - a sqlite3.Row
    """Span from the first shipment clearing to the last, in hours."""
    if record is None:
        return None
    keys = record.keys()
    first = record["first_clearance"] if "first_clearance" in keys else None
    last = record["last_clearance"] if "last_clearance" in keys else None
    if not first or not last:
        return None
    delta = datetime.fromisoformat(last) - datetime.fromisoformat(first)
    hours = delta.total_seconds() / 3600
    return round(hours, 2) if hours >= 0 else None


def summarise(stats: Stats) -> list[str]:
    """Lines shared by the Slack report and the CLI, so they cannot drift."""
    lines = [
        f"*{stats.considered}* AWB(s) tracked in the last {stats.window_days} days"
        f" · *{stats.shipments_total:,}* shipments",
    ]

    if stats.by_state:
        order = ("complete", "active", "timeout", "not_found", "failed", "stopped")
        shown = [f"{s} {stats.by_state[s]}" for s in order if s in stats.by_state]
        lines.append("Outcomes: " + " · ".join(shown))

    if not stats.has_enough_to_report:
        lines.append(
            f"_Only {len(stats.durations)} AWB(s) have clearance timings so far"
            " — too few to average. They accumulate as AWBs finish._"
        )
        return lines

    median, p90 = stats.median_hours, stats.p90_hours
    lines += [
        "",
        f"*Clearance time* (first shipment cleared → last), {len(stats.durations)} AWBs",
        f"  median *{median:.1f} h* · 90th percentile *{p90:.1f} h*"
        f" · fastest {min(stats.durations):.1f} h · slowest {max(stats.durations):.1f} h",
    ]

    if stats.slowest:
        lines += ["", "*Longest to clear*"]
        lines += [
            f"  `{o.mawb}` — {o.clearance_hours:.1f} h over {o.shipments:,} shipments"
            for o in stats.slowest
        ]

    if stats.without_timings:
        lines.append(
            f"\n_{stats.without_timings} AWB(s) have no clearance timings —"
            " tracked before those were recorded, or nothing ever cleared —"
            " and are excluded from the averages._"
        )
    return lines
