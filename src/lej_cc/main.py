"""Entry point: wire everything together and run Socket Mode."""

from __future__ import annotations

import logging
import signal
import sys

from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient
from slack_sdk.http_retry.builtin_handlers import (
    ConnectionErrorRetryHandler,
    RateLimitErrorRetryHandler,
)

from .config import Settings, configure_logging
from .parser import StatusMapper
from .portground import PortGroundClient
from .scheduler import PollScheduler
from .slack_app import build_app
from .slack_io import SlackNotifier
from .store import JobStore
from .tracker import Tracker


def main() -> int:
    settings = Settings()  # type: ignore[call-arg]
    configure_logging(settings.log_level)
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
    tracker = Tracker(settings, store, client, notifier, mapper)
    scheduler = PollScheduler(store, tracker)

    app = build_app(settings, store, scheduler)
    handler = SocketModeHandler(app, settings.slack_app_token)

    store.recover_leases()
    scheduler.start()
    active = store.list_active()
    if active:
        log.info("resuming %d active job(s) after restart", len(active))

    def shutdown(*_: object) -> None:
        log.info("shutting down")
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
