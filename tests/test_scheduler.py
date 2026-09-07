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
