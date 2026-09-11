"""Entry point: wire everything together and run Socket Mode."""

from __future__ import annotations

import logging
import signal
import sys
import threading

from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient
from slack_sdk.http_retry.builtin_handlers import (
    ConnectionErrorRetryHandler,
    RateLimitErrorRetryHandler,
)

from .config import Settings, configure_logging
from .emailer import EmailNotifier
from .health import Heartbeat
from .parser import StatusMapper
from .portground import PortGroundClient
from .scheduler import PollScheduler
from .slack_app import build_app
from .slack_io import SlackNotifier
from .store import JobStore
from .tracker import Tracker


def main() -> int:
    settings = Settings()  # type: ignore[call-arg]
    configure_logging(
        settings.log_level,
        repeat_window_seconds=settings.log_repeat_window_seconds,
    )
    log = logging.getLogger("lej_cc")

    store = JobStore(settings.database_path)
    client = PortGroundClient(settings)
    # Slack rate-limits chat.postMessage to roughly one per second per
    # channel. Several AWBs completing at once would otherwise drop updates.
    notifier = SlackNotifier(
        WebClient(
            token=settings.slack_bot_token,
            retry_handlers=[
                ConnectionErrorRetryHandler(max_retry_count=2),
                RateLimitErrorRetryHandler(max_retry_count=3),
            ],
        )
    )
    mapper = StatusMapper.load(settings.status_map_path)
    email = EmailNotifier(settings)
    if email.enabled:
        log.info("email notifications on, always-to: %s", settings.email_always_to or "(none)")
    tracker = Tracker(settings, store, client, notifier, mapper, email=email)
    heartbeat = Heartbeat(settings.heartbeat_url, settings.heartbeat_interval_seconds)
    if heartbeat.enabled:
        log.info("heartbeat every %ss", settings.heartbeat_interval_seconds)
    scheduler = PollScheduler(store, tracker, heartbeat=heartbeat)

    app = build_app(settings, store, scheduler)
    handler = SocketModeHandler(app, settings.slack_app_token)

    store.recover_leases()
    scheduler.start()
    active = store.list_active()
    if active:
        log.info("resuming %d active job(s) after restart", len(active))

    def announce(text: str) -> None:
        """Tell the channel. Never let a failure here stop the bot."""
        if not settings.status_channel:
            return
        try:
            notifier.post(settings.status_channel, text=text)
        except Exception:  # noqa: BLE001
            log.exception("could not post the status announcement")

    resumed = f", resuming {len(active)} tracked AWB(s)" if active else ""
    announce(f":large_green_circle: *AWB Tracker is online*{resumed}.")
    heartbeat.ping(force=True)

    stopping = threading.Event()

    def shutdown(*_: object) -> None:
        # A second Ctrl-C while the first shutdown is still draining threads
        # must not start a second one.
        if stopping.is_set():
            log.info("already shutting down, be patient")
            return
        stopping.set()
        log.info("shutting down — waiting for in-flight polls")
        announce(
            ":red_circle: *AWB Tracker is going offline* — stopped on the server. "
            "Tracked AWBs are saved and resume when it starts again."
        )
        scheduler.stop()
        handler.close()
        client.close()
        store.close()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    log.info("lej-cc-tool ready")
    handler.start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
