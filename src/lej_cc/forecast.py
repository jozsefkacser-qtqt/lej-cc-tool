"""When will this AWB be finished?

A percentage tells you where something is; a time tells you whether to
wait for it or go and do something else. That is the whole reason this
exists -- it turns a status into a decision.

The estimate is a least-squares fit over recent history rather than the
gap between the last two polls: clearance arrives in bursts, and two points
either side of a burst predict everything finishing in twenty minutes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

#: Only the recent past is predictive -- a rate from twelve hours ago says
#: little about an AWB that has just started moving again.
DEFAULT_WINDOW = timedelta(hours=6)
#: Two points can sit either side of one burst. Three is the minimum that
#: can disagree with itself.
MIN_POINTS = 3
#: Beyond this the arithmetic still works and the answer is meaningless.
MAX_HORIZON = timedelta(days=7)


@dataclass(frozen=True)
class Forecast:
    """An estimated finish time, and enough context to judge it."""

    eta: datetime
    per_hour: float
    points: int
    remaining: int

    @property
    def within_the_hour(self) -> bool:
        return self.eta - datetime.now(self.eta.tzinfo) < timedelta(hours=1)


def estimate(
    history: list[tuple[datetime, int]],
    *,
    total: int,
    cleared: int,
    now: datetime,
    window: timedelta = DEFAULT_WINDOW,
) -> Forecast | None:
    """Estimate when `cleared` reaches `total`, or None when it cannot say.

    Returns None rather than guessing when the history is too short, when
    nothing has moved, or when the answer would be weeks away. A missing
    forecast is honest; a made-up one gets planned around.
    """
    remaining = total - cleared
    if total <= 0 or remaining <= 0:
        return None

    points = [(t, c) for t, c in history if t >= now - window]
    if len(points) < MIN_POINTS:
        return None
    if points[-1][1] <= points[0][1]:
        return None  # nothing cleared in the window; the escalation covers this

    per_hour = _slope_per_hour(points)
    if per_hour <= 0:
        return None

    eta = now + timedelta(hours=remaining / per_hour)
    if eta - now > MAX_HORIZON:
        log.debug("forecast for %d remaining at %.1f/h is beyond the horizon", remaining, per_hour)
        return None

    return Forecast(eta=eta, per_hour=per_hour, points=len(points), remaining=remaining)


def _slope_per_hour(points: list[tuple[datetime, int]]) -> float:
    """Least-squares slope of cleared-against-time, in shipments per hour."""
    origin = points[0][0]
    xs = [(t - origin).total_seconds() / 3600 for t, _ in points]
    ys = [float(c) for _, c in points]
    n = len(xs)

    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    variance = sum((x - mean_x) ** 2 for x in xs)
    if variance == 0:  # every sample at the same instant
        return 0.0
    covariance = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    return covariance / variance
