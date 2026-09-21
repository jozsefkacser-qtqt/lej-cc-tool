"""The scheduler is what makes two AWBs submitted 15 minutes apart run in
parallel on independent clocks."""

from __future__ import annotations

import threading
import time
from datetime import timedelta

from lej_cc.scheduler import PollScheduler
from lej_cc.store import JobStore, utcnow


class SlowTracker:
    """Records concurrency: how many polls were in flight at the same time."""

    def __init__(self, delay: float = 0.15) -> None:
        self.delay = delay
        self.seen: list[str] = []
        self.peak = 0
        self._live = 0
        self._lock = threading.Lock()

    def run_once(self, job) -> None:  # noqa: ANN001
        with self._lock:
            self._live += 1
            self.peak = max(self.peak, self._live)
        time.sleep(self.delay)
        with self._lock:
            self.seen.append(job.mawb)
            self._live -= 1


def test_due_jobs_run_concurrently(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    for mawb in ("48820744846", "93600333955", "12345678901"):
        store.create_job(mawb, "C1", "U1")

    tracker = SlowTracker()
    polled = PollScheduler(store, tracker, max_parallel=4).tick()  # type: ignore[arg-type]

    assert polled == 3
    assert len(tracker.seen) == 3
    assert tracker.peak > 1, "jobs must overlap, not run one after another"


def test_jobs_not_yet_due_are_left_alone(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    store.create_job("48820744846", "C1", "U1")
    store.create_job("93600333955", "C1", "U1", run_at=utcnow() + timedelta(minutes=15))

    tracker = SlowTracker(delay=0)
    PollScheduler(store, tracker).tick()  # type: ignore[arg-type]

    assert tracker.seen == ["48820744846"]


def test_old_downloads_are_purged(tmp_path):
    """Free-tier disks are small; workbooks must not accumulate forever."""
    import os
    import time

    from lej_cc.maintenance import purge_old_downloads

    downloads = tmp_path / "downloads"
    downloads.mkdir()
    fresh = downloads / "48820744846_new.xlsx"
    stale = downloads / "48820744846_old.xlsx"
    fresh.write_bytes(b"PK")
    stale.write_bytes(b"PK")
    old = time.time() - 40 * 86400
    os.utime(stale, (old, old))

    assert purge_old_downloads(downloads, retention_days=30) == 1
    assert fresh.exists() and not stale.exists()


def test_retention_zero_disables_purging(tmp_path):
    import os
    import time

    from lej_cc.maintenance import purge_old_downloads

    downloads = tmp_path / "downloads"
    downloads.mkdir()
    ancient = downloads / "old.xlsx"
    ancient.write_bytes(b"PK")
    old = time.time() - 999 * 86400
    os.utime(ancient, (old, old))

    assert purge_old_downloads(downloads, retention_days=0) == 0
    assert ancient.exists()


def test_housekeeping_runs_at_most_once_per_interval(tmp_path):
    from lej_cc.config import Settings

    store = JobStore(tmp_path / "jobs.sqlite3")
    downloads = tmp_path / "downloads"
    downloads.mkdir()

    class StubTracker:
        settings = Settings(
            slack_bot_token="x",
            slack_app_token="x",
            portground_api_key="x",
            download_dir=downloads,
            download_retention_days=30,
        )

    scheduler = PollScheduler(store, StubTracker())  # type: ignore[arg-type]
    scheduler.housekeeping()
    first = scheduler._last_housekeeping
    scheduler.housekeeping()
    assert scheduler._last_housekeeping == first  # second call is a no-op


def test_nudge_returns_immediately(tmp_path):
    """A Slack command must not wait for the poll: PortGround takes minutes."""
    import time

    store = JobStore(tmp_path / "jobs.sqlite3")
    store.create_job("48820744846", "C1", "U1")

    tracker = SlowTracker(delay=0.5)
    scheduler = PollScheduler(store, tracker)  # type: ignore[arg-type]

    started = time.monotonic()
    scheduler.nudge()
    assert time.monotonic() - started < 0.2, "nudge blocked on the poll"

    deadline = time.monotonic() + 5
    while not tracker.seen and time.monotonic() < deadline:
        time.sleep(0.05)
    assert tracker.seen == ["48820744846"], "nudge never ran the poll"


# --- the daily import ----------------------------------------------------


class FakeTracker:
    """Just enough of a Tracker for daily_track: settings and a notifier."""

    def __init__(self, settings):
        self.settings = settings
        self.posts: list[tuple[str, str]] = []
        self.notifier = self

    def post(self, channel, *, text, **_kwargs):
        self.posts.append((channel, text))
        return "ts"

    def run_once(self, job):  # the nudge fires a tick
        pass


def _settings(tmp_path, **kwargs):
    from lej_cc.config import Settings

    return Settings(
        slack_bot_token="x", slack_app_token="x", portground_api_key="k",
        database_path=tmp_path / "jobs.sqlite3", download_dir=tmp_path / "dl",
        slack_ops_channel="C1", report_sheet_id="report-abc",
        **{"track_daily_at": "06:00", **kwargs},
    )


def test_the_daily_import_is_announced_in_the_channel(tmp_path, monkeypatch):
    """Cards appearing at dawn with no explanation read as a malfunction."""
    from lej_cc import bulk
    from lej_cc.scheduler import PollScheduler
    from lej_cc.store import JobStore

    settings = _settings(tmp_path)
    store = JobStore(settings.database_path)
    tracker = FakeTracker(settings)
    monkeypatch.setattr(bulk, "resolve_tabs", lambda *a, **k: ["2026.08", "2026.09"])
    monkeypatch.setattr(bulk, "read_from_sheet", lambda *a, **k: ["488-20744846"])

    PollScheduler(store, tracker).daily_track()  # type: ignore[arg-type]

    assert len(tracker.posts) == 1
    channel, text = tracker.posts[0]
    assert channel == "C1"
    assert "started 1" in text
    assert "2026.08, 2026.09" in text


def test_a_day_with_nothing_new_says_nothing(tmp_path, monkeypatch):
    """Silence is correct when there is nothing to report."""
    from lej_cc import bulk
    from lej_cc.scheduler import PollScheduler
    from lej_cc.store import JobStore

    settings = _settings(tmp_path)
    store = JobStore(settings.database_path)
    tracker = FakeTracker(settings)
    monkeypatch.setattr(bulk, "resolve_tabs", lambda *a, **k: ["2026.09"])
    monkeypatch.setattr(bulk, "read_from_sheet", lambda *a, **k: ["AWB", ""])

    PollScheduler(store, tracker).daily_track()  # type: ignore[arg-type]

    assert tracker.posts == []


def test_daily_tracking_off_posts_nothing(tmp_path):
    from lej_cc.scheduler import PollScheduler
    from lej_cc.store import JobStore

    settings = _settings(tmp_path, track_daily_at="")
    tracker = FakeTracker(settings)

    PollScheduler(JobStore(settings.database_path), tracker).daily_track()  # type: ignore[arg-type]

    assert tracker.posts == []
