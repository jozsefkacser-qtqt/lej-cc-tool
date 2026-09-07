"""The tick loop.

Every `tick_seconds` it claims the jobs whose `next_run_at` has passed and
runs them on a thread pool. Two AWBs submitted 15 minutes apart therefore
run on independent clocks and poll in parallel -- no per-job timers, no
sleeping threads, and nothing lost across a restart.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from .store import JobStore
from .tracker import Tracker

log = logging.getLogger(__name__)


class PollScheduler:
    def __init__(
        self,
        store: JobStore,
        tracker: Tracker,
        *,
        tick_seconds: int = 30,
        max_parallel: int = 8,
    ) -> None:
        self.store = store
        self.tracker = tracker
        self.tick_seconds = tick_seconds
        self.max_parallel = max_parallel
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self._run, name="poll-scheduler", daemon=True)
        self._thread.start()
        log.info("scheduler started (tick=%ss, parallel=%s)", self.tick_seconds, self.max_parallel)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)

    def tick(self) -> int:
        """Run one round of due jobs. Returns how many were polled."""
        jobs = self.store.claim_due(limit=self.max_parallel * 2)
        if not jobs:
            return 0
        log.info("polling %d due job(s): %s", len(jobs), ", ".join(j.mawb for j in jobs))
        with ThreadPoolExecutor(max_workers=self.max_parallel, thread_name_prefix="poll") as pool:
            for job in jobs:
                pool.submit(self.tracker.run_once, job)
        return len(jobs)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - the loop must survive anything
                log.exception("scheduler tick failed")
            self._stop.wait(self.tick_seconds)
