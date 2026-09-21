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

from .health import HEALTH, Heartbeat
from .inbox import EmailTrigger
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
        heartbeat: Heartbeat | None = None,
        inbox: EmailTrigger | None = None,
        inbox_seconds: int = 60,
    ) -> None:
        self.store = store
        self.tracker = tracker
        self.heartbeat = heartbeat
        self.tick_seconds = tick_seconds
        self.max_parallel = max_parallel
        # The mailbox gets its own thread rather than a slot in the tick: an
        # IMAP server that stops answering would otherwise stall the polling
        # of AWBs somebody is waiting on.
        self.inbox = inbox
        self.inbox_seconds = max(15, inbox_seconds)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._inbox_thread: threading.Thread | None = None
        self._last_housekeeping = 0.0

    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self._run, name="poll-scheduler", daemon=True)
        self._thread.start()
        log.info("scheduler started (tick=%ss, parallel=%s)", self.tick_seconds, self.max_parallel)
        if self.inbox and self.inbox.enabled:
            self._inbox_thread = threading.Thread(
                target=self._run_inbox, name="inbox-poller", daemon=True
            )
            self._inbox_thread.start()
            log.info("email trigger started (every %ss)", self.inbox_seconds)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        if self._inbox_thread:
            self._inbox_thread.join(timeout=10)

    def tick(self) -> int:
        """Run one round of due jobs. Returns how many were polled."""
        HEALTH.tick()
        if self.heartbeat:
            self.heartbeat.ping()
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

    def daily_track(self) -> None:
        """Read the report and start whatever is new, once a day.

        The bot does this itself rather than through cron because it already
        runs all day and cron does not: under WSL the daemon is not started
        unless somebody remembered, and a schedule that silently never fires
        is worse than no schedule. Here it is visible in the log, it catches
        up after a restart, and it moves to the server with everything else.
        """
        from .bulk import scheduled_import

        summary = scheduled_import(self.tracker.settings, self.store)
        if summary:
            self.nudge()  # do not wait a full tick to poll what was just started

    def housekeeping(self, interval_seconds: int = 3600) -> None:
        """Purge stale downloads, at most once per `interval_seconds`."""
        now = time.monotonic()
        if now - self._last_housekeeping < interval_seconds:
            return
        self._last_housekeeping = now
        settings = self.tracker.settings
        purge_old_downloads(settings.download_dir, settings.download_retention_days)

    def poll_inbox(self) -> None:
        """One pass over the mailbox. Nudges the tick if anything started."""
        if not self.inbox:
            return
        result = self.inbox.poll()
        HEALTH.inbox_polled(result.accepted, result.refused)
        if result.accepted:
            self.nudge()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
                self.housekeeping()
                self.daily_track()
            except Exception:  # noqa: BLE001 - the loop must survive anything
                log.exception("scheduler tick failed")
            self._stop.wait(self.tick_seconds)

    def _run_inbox(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_inbox()
            except Exception:  # noqa: BLE001 - a bad mailbox must not end the loop
                log.exception("inbox poll failed")
            self._stop.wait(self.inbox_seconds)
