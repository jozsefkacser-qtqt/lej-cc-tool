"""Slack output: the `Notifier` implementation used in production."""

from __future__ import annotations

import logging
from pathlib import Path

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

log = logging.getLogger(__name__)


class SlackNotifier:
    def __init__(self, client: WebClient) -> None:
        self.client = client

    def post(
        self,
        channel: str,
        *,
        text: str,
        blocks: list[dict] | None = None,
        thread_ts: str | None = None,
        broadcast: bool = False,
        colour: str | None = None,
    ) -> str | None:
        """Post a card. `colour` draws a stripe down its left edge.

        The stripe is the one piece of real colour Slack allows on a
        message, and it is what makes a channel of cards scannable from
        across the room -- it needs the older attachment wrapper, which is
        the only place Block Kit accepts a colour at all.
        """
        payload: dict = {"blocks": blocks} if blocks else {}
        if colour and blocks:
            payload = {"attachments": [{"color": colour, "blocks": blocks}]}
        try:
            response = self.client.chat_postMessage(
                channel=channel,
                text=text,  # fallback for notifications and screen readers
                thread_ts=thread_ts,
                reply_broadcast=broadcast if thread_ts else None,
                unfurl_links=False,
                **payload,
            )
            return response.get("ts")
        except SlackApiError as exc:
            log.error("chat.postMessage failed for %s: %s", channel, exc.response.get("error"))
            return None

    def upload(
        self,
        channel: str,
        path: Path,
        *,
        filename: str,
        title: str,
        thread_ts: str | None = None,
        comment: str | None = None,
    ) -> None:
        try:
            self.client.files_upload_v2(
                channel=channel,
                file=str(path),
                filename=filename,
                title=title,
                thread_ts=thread_ts,
                initial_comment=comment,
            )
        except SlackApiError as exc:
            error = exc.response.get("error", "unknown")
            log.error("files.upload failed for %s: %s", filename, error)
            # Never fail quietly: a status card with no attachment and no
            # explanation is indistinguishable from one that never had a file.
            self.post(
                channel,
                text=(
                    f":warning: Couldn't attach `{filename}` (Slack said `{error}`). "
                    "The status above is correct; only the file is missing."
                ),
                thread_ts=thread_ts,
            )
        except OSError as exc:
            log.error("could not read %s for upload: %s", path, exc)
            self.post(
                channel,
                text=f":warning: Couldn't attach `{filename}`: {exc}",
                thread_ts=thread_ts,
            )
