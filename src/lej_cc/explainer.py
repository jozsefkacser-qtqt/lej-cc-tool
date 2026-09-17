"""The pinned "how to use this" message in an auto-detect channel.

A channel where posting a number is enough only works if people know that.
The instruction is one sentence, so it belongs where they already are --
pinned at the top of the channel -- rather than in a document nobody opens.

The whole design problem here is restarts. The bot restarts often, and a bot
that pins a fresh copy every restart is a bot whose pins nobody reads. So the
message it pinned is remembered in the database and *rewritten in place* on
every later start: the pin stays the same message, the text stays current
with the version running, and the channel gets exactly one of these ever.
"""

from __future__ import annotations

import logging

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from .store import JobStore

log = logging.getLogger(__name__)

#: Errors that mean "the message we remembered is gone" -- somebody deleted
#: it, or the database was carried to a different workspace. Post a new one.
GONE = {"message_not_found", "cant_update_message", "channel_not_found"}


def build_blocks(version=None) -> list[dict]:  # noqa: ANN001
    """The explainer itself. Short on purpose: a pinned wall of text is wallpaper."""
    lines = [
        "*Post an AWB number here and it is tracked. That is the whole thing.*",
        "",
        "> 936-02134215",
        "",
        "I reply under your message, then post how much of it has cleared "
        "customs — first check straight away, again after 15 minutes, then "
        "every 30 minutes until it is at 100%. Each update carries a "
        "spreadsheet of the shipments still open, oldest first.",
        "",
        "*Also useful*",
        "• `/awb list` — what this channel is tracking now",
        "• `/awb stop 936-02134215` — stop one",
        "• `/awb status` — am I running, and am I healthy",
        "• `/awb OyTM202608137666` — booking references need the command "
        "(they carry no check digit, so I will not pick one out of a sentence)",
        "• `/awb help` — everything else",
    ]
    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "📦 Tracking customs clearance", "emoji": True},
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}},
    ]
    if version is not None:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            f"_Kept up to date automatically · `{version}`. "
                            "If I stop answering, `/awb status` says nothing at all — "
                            "that is the answer._"
                        ),
                    }
                ],
            }
        )
    return blocks


def _key(channel: str) -> str:
    return f"explainer_ts:{channel}"


def ensure_pinned(
    client: WebClient,
    store: JobStore,
    channel: str,
    version=None,  # noqa: ANN001
) -> str | None:
    """Post and pin the explainer, or refresh the one already there.

    Returns the message timestamp, or None if nothing could be posted. Never
    raises: a channel the bot was not invited to must not stop it starting.
    """
    known = store.get_meta(_key(channel))
    if known:
        try:
            client.chat_update(channel=channel, ts=known, blocks=build_blocks(version), text="")
            log.info("refreshed the pinned explainer in %s", channel)
            return known
        except SlackApiError as exc:
            code = exc.response.get("error", "")
            if code not in GONE:
                log.warning("could not refresh the explainer in %s: %s", channel, code)
                return known
            log.info("the pinned explainer in %s is gone, posting a new one", channel)
            store.clear_meta(_key(channel))

    try:
        posted = client.chat_postMessage(
            channel=channel,
            blocks=build_blocks(version),
            text="Post an AWB number here and it is tracked.",
        )
    except SlackApiError as exc:
        log.error(
            "could not post the explainer in %s: %s — invite me with "
            "/invite @AWB Tracker, or set PIN_EXPLAINER=false",
            channel,
            exc.response.get("error", ""),
        )
        return None

    ts = posted["ts"]
    store.set_meta(_key(channel), ts)
    try:
        client.pins_add(channel=channel, timestamp=ts)
        log.info("pinned the explainer in %s", channel)
    except SlackApiError as exc:
        code = exc.response.get("error", "")
        if code != "already_pinned":
            # The message is up and remembered; only the pin failed, which is
            # a missing scope, not a reason to lose the message.
            log.warning(
                "posted the explainer in %s but could not pin it: %s "
                "(the pins:write scope is needed)",
                channel,
                code,
            )
    return ts
