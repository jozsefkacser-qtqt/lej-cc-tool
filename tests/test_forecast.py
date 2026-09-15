"""Completion forecasting.

The value of a forecast is that someone plans around it, which is exactly
why a wrong one is worse than none. These tests are mostly about when it
should refuse to answer.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from lej_cc.forecast import MIN_POINTS, estimate

TZ = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 9, 15, 15, 0, tzinfo=TZ)


def steady(points: int, per_poll: int, *, start: int = 0, minutes: int = 30):
    """A history clearing `per_poll` shipments every `minutes`."""
    return [
        (NOW - timedelta(minutes=minutes * (points - 1 - i)), start + per_poll * i)
        for i in range(points)
    ]


def test_a_steady_rate_predicts_the_finish():
    # 100 an hour, 200 to go -> two hours.
    history = steady(5, 50)
    found = estimate(history, total=400, cleared=200, now=NOW)

    assert found is not None
    assert abs(found.per_hour - 100) < 1
    assert abs((found.eta - NOW) - timedelta(hours=2)) < timedelta(minutes=5)
    assert found.remaining == 200


def test_a_faster_rate_finishes_sooner():
    slow = estimate(steady(5, 10), total=200, cleared=100, now=NOW)
    fast = estimate(steady(5, 100), total=1100, cleared=1000, now=NOW)
    assert slow and fast
    assert fast.eta < slow.eta


def test_it_refuses_with_too_little_history():
    """Two points can sit either side of one burst and predict nonsense."""
    assert estimate(steady(MIN_POINTS - 1, 50), total=400, cleared=200, now=NOW) is None


def test_it_refuses_when_nothing_has_moved():
    flat = [(NOW - timedelta(minutes=30 * i), 200) for i in range(5)][::-1]
    assert estimate(flat, total=400, cleared=200, now=NOW) is None


def test_it_refuses_when_the_count_went_backwards():
    falling = [(NOW - timedelta(hours=2 - i), 300 - i * 50) for i in range(3)]
    assert estimate(falling, total=400, cleared=200, now=NOW) is None


def test_it_refuses_when_the_answer_would_be_weeks_away():
    """The arithmetic still works at one shipment an hour with 5000 to go.
    The answer is not information."""
    crawl = steady(5, 1, minutes=60)
    assert estimate(crawl, total=5000, cleared=4, now=NOW) is None


def test_nothing_to_forecast_once_it_is_finished():
    assert estimate(steady(5, 50), total=400, cleared=400, now=NOW) is None
    assert estimate(steady(5, 50), total=0, cleared=0, now=NOW) is None


def test_only_the_recent_window_counts():
    """A burst yesterday says nothing about an AWB that is crawling now."""
    old_burst = [(NOW - timedelta(hours=20), 0), (NOW - timedelta(hours=19), 1000)]
    recent = steady(4, 5, start=1000, minutes=30)
    assert estimate(old_burst + recent, total=1100, cleared=1015, now=NOW) is not None

    # The burst alone is outside the window, so it cannot carry a forecast.
    assert estimate(old_burst, total=1100, cleared=1000, now=NOW) is None


def test_a_burst_does_not_dominate_the_estimate():
    """Least squares over the window, not the gap between the last two
    polls: one batch clearing would otherwise promise the moon."""
    history = [
        (NOW - timedelta(hours=3), 0),
        (NOW - timedelta(hours=2), 10),
        (NOW - timedelta(hours=1), 20),
        (NOW, 300),  # a batch lands
    ]
    found = estimate(history, total=1000, cleared=300, now=NOW)
    assert found is not None
    # The two-point rate would be 280/h and finish in 2.5 hours.
    assert found.eta - NOW > timedelta(hours=3)


def test_simultaneous_samples_do_not_divide_by_zero():
    same = [(NOW, 10), (NOW, 20), (NOW, 30)]
    assert estimate(same, total=100, cleared=30, now=NOW) is None
