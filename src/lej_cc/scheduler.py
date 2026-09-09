"""The tick loop.

Every `tick_seconds` it claims the jobs whose `next_run_at` has passed and
runs them on a thread pool. Two AWBs submitted 15 minutes apart therefore
run on independent clocks and poll in parallel -- no per-job timers, no
sleeping threads, and nothing lost across a restart.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .maintenance import purge_old_downloads
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
        self._last_housekeeping = 0.0

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

    def nudge(self) -> None:
        """Run a tick in the background, without waiting for it.

        Used when a Slack command asks for an immediate poll: a download can
        take minutes, and the handler has to reply long before that.
        """
        threading.Thread(target=self._safe_tick, name="poll-nudge", daemon=True).start()

    def _safe_tick(self) -> None:
        try:
            self.tick()
        except Exception:  # noqa: BLE001 - a nudge must never take the app down
            log.exception("nudged tick failed")

    def housekeeping(self, interval_seconds: int = 3600) -> None:
        """Purge stale downloads, at most once per `interval_seconds`."""
        now = time.monotonic()
        if now - self._last_housekeeping < interval_seconds:
            return
        self._last_housekeeping = now
        settings = self.tracker.settings
        purge_old_downloads(settings.download_dir, settings.download_retention_days)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
                self.housekeeping()
            except Exception:  # noqa: BLE001 - the loop must survive anything
                log.exception("scheduler tick failed")
            self._stop.wait(self.tick_seconds)
