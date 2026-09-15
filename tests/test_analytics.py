"""Clearance statistics over the accumulated history.

The risk with a statistics feature is that it reports a number when it has
no business doing so. Most of these tests are about refusing.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from lej_cc.analytics import gather, percentile, summarise
from lej_cc.store import JobStore, _iso, utcnow


@pytest.fixture
def store(tmp_path) -> JobStore:
    return JobStore(tmp_path / "j.sqlite3")


def finished_awb(store, mawb, *, hours, shipments=100, state="complete", days_ago=1):
    """A completed AWB whose clearance took `hours`."""
    job = store.create_job(mawb, "C1", "U1")
    end = utcnow() - timedelta(days=days_ago)
    store._conn.execute(
        "INSERT INTO snapshots (job_id, mawb, taken_at, total, cleared, not_cleared,"
        " other, items_total, items_cleared, percent, first_clearance, last_clearance)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (job.id, mawb, _iso(end), shipments, shipments, 0, 0, shipments * 2,
         shipments * 2, 100.0, _iso(end - timedelta(hours=hours)), _iso(end)),
    )
    store.finish(job.id, state, "done")
    return job


# --- the maths ----------------------------------------------------------


def test_percentile_of_nothing_is_nothing():
    assert percentile([], 0.5) is None


def test_percentile_picks_the_nearest_rank():
    assert percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.5) == 3.0
    assert percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.9) == 5.0
    assert percentile([10.0], 0.5) == 10.0


# --- gathering ----------------------------------------------------------


def test_clearance_durations_are_measured(store):
    for mawb, hours in (("48820744846", 2.0), ("93602927993", 6.0), ("93600333955", 4.0)):
        finished_awb(store, mawb, hours=hours)

    stats = gather(store)
    assert stats.completed == 3
    assert sorted(stats.durations) == [2.0, 4.0, 6.0]
    assert stats.median_hours == 4.0


def test_active_awbs_are_counted_but_are_not_outcomes(store):
    store.create_job("48820744846", "C1", "U1")
    finished_awb(store, "93602927993", hours=3.0)

    stats = gather(store)
    assert stats.considered == 2
    assert stats.by_state["active"] == 1
    assert len(stats.outcomes) == 1


def test_awbs_outside_the_window_are_ignored(store):
    old = store.create_job("48820744846", "C1", "U1")
    store._conn.execute(
        "UPDATE jobs SET created_at = ? WHERE id = ?",
        (_iso(utcnow() - timedelta(days=200)), old.id),
    )
    finished_awb(store, "93602927993", hours=3.0)

    assert gather(store, window_days=90).considered == 1


def test_slowest_lists_the_worst_first(store):
    for mawb, hours in (("48820744846", 2.0), ("93602927993", 19.0), ("93600333955", 7.0)):
        finished_awb(store, mawb, hours=hours)

    assert [o.clearance_hours for o in gather(store).slowest] == [19.0, 7.0, 2.0]


def test_an_awb_without_timings_is_counted_and_excluded(store):
    """Rows predating clearance-time recording must not silently vanish."""
    job = store.create_job("48820744846", "C1", "U1")
    store._conn.execute(
        "INSERT INTO snapshots (job_id, mawb, taken_at, total, cleared, not_cleared,"
        " other, items_total, items_cleared, percent) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (job.id, "48820744846", _iso(utcnow()), 50, 50, 0, 0, 100, 100, 100.0),
    )
    store.finish(job.id, "complete", "done")

    stats = gather(store)
    assert stats.without_timings == 1
    assert stats.durations == []
    assert len(stats.outcomes) == 1  # still counted as an outcome


def test_a_negative_span_is_rejected(store):
    """Clocks and partial writes; a negative duration is not a fast AWB."""
    job = store.create_job("48820744846", "C1", "U1")
    now = utcnow()
    store._conn.execute(
        "INSERT INTO snapshots (job_id, mawb, taken_at, total, cleared, not_cleared,"
        " other, items_total, items_cleared, percent, first_clearance, last_clearance)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (job.id, "48820744846", _iso(now), 5, 5, 0, 0, 10, 10, 100.0,
         _iso(now), _iso(now - timedelta(hours=3))),
    )
    store.finish(job.id, "complete", "done")
    assert gather(store).durations == []


# --- reporting ----------------------------------------------------------


def test_it_refuses_to_average_an_anecdote(store):
    """Two AWBs is not a median, and printing one invites a decision."""
    finished_awb(store, "48820744846", hours=2.0)
    finished_awb(store, "93602927993", hours=40.0)

    report = "\n".join(summarise(gather(store)))
    assert "too few to average" in report
    assert "median" not in report


def test_the_report_names_the_numbers_once_there_are_enough(store):
    for i, hours in enumerate([1.0, 2.0, 3.0, 4.0, 20.0]):
        finished_awb(store, f"4882074484{i}", hours=hours)

    report = "\n".join(summarise(gather(store)))
    assert "median" in report
    assert "90th percentile" in report
    assert "Longest to clear" in report
    assert "20.0 h" in report


def test_an_empty_history_reports_nothing_rather_than_zeroes(store):
    report = "\n".join(summarise(gather(store))).replace("*", "")
    assert "0 AWB(s) tracked" in report
    assert "median" not in report
