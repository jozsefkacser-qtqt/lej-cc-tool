"""What the bot knows about its own condition.

Split into three because a process cannot report its own death:

  * announcements  -- it says when it comes up and when it is asked to stop
  * `/awb status`  -- anyone can ask, and no answer is itself an answer
  * a heartbeat    -- an outside service notices when the pings stop, which
                      is the only part that survives the power being cut
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .store import utcnow

log = logging.getLogger(__name__)


@dataclass
class Health:
    """Live counters, updated as the bot works. Safe across threads."""

    started_at: datetime = field(default_factory=utcnow)
    last_tick_at: datetime | None = None
    last_poll_ok_at: datetime | None = None
    last_poll_error: str | None = None
    last_poll_error_at: datetime | None = None
    polls_ok: int = 0
    polls_failed: int = 0
    last_api_seconds: float | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def tick(self) -> None:
        with self._lock:
            self.last_tick_at = utcnow()

    def poll_succeeded(self, seconds: float | None = None) -> None:
        with self._lock:
            self.last_poll_ok_at = utcnow()
            self.polls_ok += 1
            if seconds is not None:
                self.last_api_seconds = seconds

    def poll_failed(self, reason: str) -> None:
        with self._lock:
            self.last_poll_error = reason
            self.last_poll_error_at = utcnow()
            self.polls_failed += 1

    @property
    def uptime(self) -> timedelta:
        return utcnow() - self.started_at


#: Process-wide instance. A module-level object rather than something threaded
#: through every constructor: it is genuinely global state -- there is one
#: process and one answer to "are you alive".
HEALTH = Health()


class Heartbeat:
    """Pings a dead-man's-switch URL so an outside service can miss it.

    The bot cannot tell you it died -- the PC was switched off, or WSL went
    to sleep with it. Something outside has to notice the silence, and this
    is the cheapest version of that: a URL pinged on a schedule, and a free
    service that alerts when a ping does not arrive.
    """

    def __init__(self, url: str, interval_seconds: int = 300) -> None:
        self.url = url
        self.interval = interval_seconds
        # None, not 0.0: time.monotonic() counts from boot, so on a machine
        # up for less than `interval` a zero sentinel makes the very first
        # ping look recent and skips it -- on a freshly booted server, which
        # is exactly when the ping matters most.
        self._last: float | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def ping(self, *, force: bool = False) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        if not force and self._last is not None and now - self._last < self.interval:
            return
        self._last = now
        try:
            import httpx

            httpx.get(self.url, timeout=10)
            log.debug("heartbeat sent")
        except Exception as exc:  # noqa: BLE001 - never let this matter
            log.warning("heartbeat ping failed: %s", exc)
