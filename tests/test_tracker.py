"""End-to-end polling cycles with Slack and PortGround replaced by fakes."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import pytest

from lej_cc.config import Settings
from lej_cc.errors import ApiUnavailable, AwbNotFound
from lej_cc.store import JobStore, utcnow
from lej_cc.tracker import Tracker

MAWB = "48820744846"


@dataclass
class FakeNotifier:
    posts: list[dict] = field(default_factory=list)
    uploads: list[dict] = field(default_factory=list)
    _ts: int = 1000

    def post(self, channel, *, text, blocks=None, thread_ts=None, broadcast=False):
        self._ts += 1
        self.posts.append(
            {
                "channel": channel,
                "text": text,
                "blocks": blocks,
                "thread_ts": thread_ts,
                "broadcast": broadcast,
            }
        )
        return f"{self._ts}.0001"

    def upload(self, channel, path, *, filename, title, thread_ts=None, comment=None):
        self.uploads.append({"channel": channel, "filename": filename, "thread_ts": thread_ts})


class FakeClient:
    """Serves a queue of fixtures; a queued exception is raised instead."""

    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.calls = 0

    def download(self, mawb: str, dest_dir: Path):
        self.calls += 1
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / f"{mawb}_{self.calls}.xlsx"
        shutil.copy(item, target)
        return target, f"shipment_status_{mawb}_20260907_15_05_33.xlsx"


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        slack_bot_token="xoxb-test",
        slack_app_token="xapp-test",
        portground_api_key="test-key",
        database_path=tmp_path / "jobs.sqlite3",
        download_dir=tmp_path / "downloads",
        slack_ops_channel="C-OPS",
    )


@pytest.fixture
def store(settings) -> JobStore:
    return JobStore(settings.database_path)


def build(settings, store, responses, notifier=None):
    notifier = notifier or FakeNotifier()
    return Tracker(settings, store, FakeClient(responses), notifier), notifier


# --- the happy path ------------------------------------------------------


def test_first_poll_posts_to_channel_and_attaches_the_file(settings, store, data_dir):
    tracker, notifier = build(settings, store, [data_dir / "partial.xlsx"])
    job = store.create_job(MAWB, "C1", "U1")

    tracker.run_once(job)

    assert len(notifier.posts) == 1
    post = notifier.posts[0]
    assert post["thread_ts"] is None  # goes to the channel, becomes the thread root
    assert "60% cleared" in post["text"]
    assert len(notifier.uploads) == 1

    reloaded = store.get(job.id)
    assert reloaded.state == "active"
    assert reloaded.thread_ts is not None
    assert reloaded.poll_count == 1
    assert reloaded.last_percent == 60.0


def test_second_poll_is_scheduled_15_minutes_out_then_30(settings, store, data_dir):
    tracker, _ = build(settings, store, [data_dir / "partial.xlsx"])
    job = store.create_job(MAWB, "C1", "U1")

    tracker.run_once(job)
    after_first = store.get(job.id)
    gap = after_first.next_run_at - utcnow()
    assert timedelta(minutes=14) < gap <= timedelta(minutes=15)

    tracker.run_once(after_first)
    gap = store.get(job.id).next_run_at - utcnow()
    assert timedelta(minutes=29) < gap <= timedelta(minutes=30)


def test_progress_produces_a_threaded_delta_update(settings, store, data_dir):
    tracker, notifier = build(
        settings, store, [data_dir / "none_cleared.xlsx", data_dir / "none_cleared.xlsx"]
    )
    job = store.create_job("93600333955", "C1", "U1")
    tracker.run_once(job)

    # Same content again -> nothing changed -> stays quiet by default.
    tracker.run_once(store.get(job.id))
    assert len(notifier.posts) == 1

    settings.post_unchanged_updates = True
    tracker.run_once(store.get(job.id))
    assert len(notifier.posts) == 2
    assert notifier.posts[1]["thread_ts"] is not None
    assert "No change" in notifier.posts[1]["text"]


def test_completion_finishes_the_job_and_broadcasts(settings, store, data_dir):
    tracker, notifier = build(
        settings, store, [data_dir / "partial.xlsx", data_dir / "all_cleared.xlsx"]
    )
    job = store.create_job(MAWB, "C1", "U1")
    tracker.run_once(job)
    tracker.run_once(store.get(job.id))

    final = notifier.posts[-1]
    assert final["broadcast"] is True
    assert "100% cleared" in final["text"]

    done = store.get(job.id)
    assert done.state == "complete"
    assert done.finish_reason == "100% cleared"
    assert len(notifier.uploads) == 2  # first poll and completion


def test_snapshot_history_is_recorded(settings, store, data_dir):
    tracker, _ = build(settings, store, [data_dir / "partial.xlsx"])
    job = store.create_job(MAWB, "C1", "U1")
    tracker.run_once(job)
    rows = store._conn.execute("SELECT percent, cleared, total FROM snapshots").fetchall()
    assert [tuple(r) for r in rows] == [(60.0, 6, 10)]


# --- error handling ------------------------------------------------------


def test_unknown_awb_is_reported_immediately_and_stops(settings, store, data_dir):
    """The typo case: tell the user now, do not poll for 48 hours."""
    tracker, notifier = build(settings, store, [data_dir / "empty.xlsx"])
    job = store.create_job(MAWB, "C1", "U1")

    tracker.run_once(job)

    assert store.get(job.id).state == "not_found"
    assert "no shipments on file" in notifier.posts[0]["text"].lower()
    assert notifier.uploads == []


def test_api_404_is_reported_immediately(settings, store):
    tracker, notifier = build(settings, store, [AwbNotFound("404", user_message="Not known.")])
    job = store.create_job(MAWB, "C1", "U1")
    tracker.run_once(job)
    assert store.get(job.id).state == "not_found"
    assert len(notifier.posts) == 1


def test_transient_failures_stay_quiet_then_report_then_give_up(settings, store, data_dir):
    outage = ApiUnavailable("503", user_message="PortGround is down.")
    tracker, notifier = build(settings, store, [data_dir / "partial.xlsx", outage])

    job = store.create_job(MAWB, "C1", "U1")
    tracker.run_once(job)  # first poll succeeds
    assert len(notifier.posts) == 1

    for _ in range(2):  # failures 1 and 2: silent
        tracker.run_once(store.get(job.id))
    assert len(notifier.posts) == 1
    assert store.get(job.id).consecutive_failures == 2

    tracker.run_once(store.get(job.id))  # failure 3: report, keep going
    assert len(notifier.posts) == 2
    assert store.get(job.id).state == "active"

    for _ in range(2):  # failure 5: give up
        tracker.run_once(store.get(job.id))
    assert store.get(job.id).state == "failed"


def test_failures_back_off(settings, store):
    outage = ApiUnavailable("503", user_message="down")
    tracker, _ = build(settings, store, [outage])
    job = store.create_job(MAWB, "C1", "U1")

    tracker.run_once(job)
    first_gap = store.get(job.id).next_run_at - utcnow()
    tracker.run_once(store.get(job.id))
    second_gap = store.get(job.id).next_run_at - utcnow()
    assert second_gap > first_gap


def test_schema_drift_alerts_ops_and_stops(settings, store, tmp_path):
    import openpyxl

    workbook = openpyxl.Workbook()
    workbook.active.append(["HAWB / Tracking number", "MAWB"])
    workbook.active.append(["0034", MAWB])
    broken = tmp_path / "drift.xlsx"
    workbook.save(broken)

    tracker, notifier = build(settings, store, [broken])
    job = store.create_job(MAWB, "C1", "U1")
    tracker.run_once(job)

    assert store.get(job.id).state == "failed"
    assert any(p["channel"] == "C-OPS" for p in notifier.posts)


def test_tracking_times_out(settings, store, data_dir):
    settings.max_tracking_hours = 0  # everything is instantly "too old"
    tracker, notifier = build(settings, store, [data_dir / "partial.xlsx"])
    job = store.create_job(MAWB, "C1", "U1")

    tracker.run_once(job)

    finished = store.get(job.id)
    assert finished.state == "timeout"
    assert "still 60%" in finished.finish_reason


def test_unknown_status_values_reach_slack(settings, store, data_dir):
    tracker, notifier = build(settings, store, [data_dir / "unknown_status.xlsx"])
    job = store.create_job(MAWB, "C1", "U1")
    tracker.run_once(job)

    rendered = str(notifier.posts[0]["blocks"])
    assert "blocked" in rendered and "seized" in rendered
    assert store.get(job.id).last_percent == 60.0  # 3 of 5, unknowns not counted as cleared


def test_a_bug_in_the_cycle_does_not_kill_the_scheduler(settings, store):
    tracker, notifier = build(settings, store, [RuntimeError("boom")])
    job = store.create_job(MAWB, "C1", "U1")

    tracker.run_once(job)  # must not raise

    assert store.get(job.id).state == "active"
    assert store.get(job.id).consecutive_failures == 1


def test_timeout_default_allows_for_a_slow_export():
    """A live call measured 112s for the smallest reference AWB, so a default
    under two minutes fails on healthy responses."""
    from lej_cc.config import Settings

    settings = Settings(slack_bot_token="x", slack_app_token="x", portground_api_key="x")
    assert settings.http_timeout_seconds >= 180


def test_claim_lease_outlasts_the_slowest_poll(tmp_path):
    """The lease must exceed timeout x retries, or a slow poll gets claimed
    twice and one AWB is downloaded and posted about in parallel."""
    import inspect

    from lej_cc.config import Settings
    from lej_cc.store import JobStore

    settings = Settings(slack_bot_token="x", slack_app_token="x", portground_api_key="x")
    lease = inspect.signature(JobStore.claim_due).parameters["lease_seconds"].default
    worst_case_poll = settings.http_timeout_seconds * 2  # two attempts
    assert lease > worst_case_poll
