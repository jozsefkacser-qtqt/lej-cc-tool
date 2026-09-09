"""Slack input: the /awb command, the buttons, and passive AWB detection.

Socket Mode is used deliberately -- it needs no public HTTPS endpoint and no
inbound firewall rule, which matters when this runs inside the corporate
network.
"""

from __future__ import annotations

import logging

from slack_bolt import Ack, App, Respond
from slack_sdk import WebClient

from .awb import extract_all, format_display, normalize
from .config import Settings
from .errors import AwbChecksumFailed, InvalidAwbFormat
from .scheduler import PollScheduler
from .store import JobStore, utcnow

log = logging.getLogger(__name__)

HELP = (
    "*AWB customs-clearance tracking*\n"
    "• `/awb 488-20744846` — start tracking (several numbers at once are fine)\n"
    "• `/awb list` — what is currently being tracked in this channel\n"
    "• `/awb stop 488-20744846` — stop tracking\n"
    "• `/awb help` — this message\n\n"
    "The first check runs immediately, the next after 15 minutes, then every "
    "30 minutes until everything is cleared."
)


def build_app(settings: Settings, store: JobStore, scheduler: PollScheduler) -> App:
    app = App(token=settings.slack_bot_token, logger=log)

    def start_tracking(mawb: str, channel: str, user: str | None) -> str:
        existing = store.find_active(mawb, channel)
        if existing:
            percent = existing.last_percent
            state = f"{percent:.0f}% cleared" if percent is not None else "first check pending"
            return (
                f"⏳ `{format_display(mawb)}` is already being tracked here "
                f"({state}, check #{existing.poll_count})."
            )
        job = store.create_job(mawb, channel, user, run_at=utcnow())
        if job is None:  # lost a race against a duplicate command
            return f"⏳ `{format_display(mawb)}` is already being tracked here."
        log.info("tracking %s in %s for %s (job %s)", mawb, channel, user, job.id)
        return (
            f"🔎 Tracking `{format_display(mawb)}` — first check running now. "
            "PortGround takes a couple of minutes to build the export, so the "
            "first status will follow shortly."
        )

    def handle_numbers(raw_numbers: list[str], channel: str, user: str | None) -> str:
        replies: list[str] = []
        for raw in raw_numbers:
            try:
                mawb = normalize(raw)
            except AwbChecksumFailed as exc:
                replies.append(f"⚠️ {exc.user_message}")
                continue
            except InvalidAwbFormat as exc:
                replies.append(f"🚫 {exc.user_message}")
                continue
            replies.append(start_tracking(mawb, channel, user))
        return "\n".join(replies)

    # --- /awb ------------------------------------------------------------

    @app.command("/awb")
    def cmd_awb(ack: Ack, command: dict, respond: Respond) -> None:
        ack()
        text = (command.get("text") or "").strip()
        channel = command["channel_id"]
        user = command.get("user_id")

        if not text or text.lower() in {"help", "-h", "?"}:
            respond(HELP)
            return

        verb, _, argument = text.partition(" ")
        verb = verb.lower()

        if verb == "list":
            jobs = store.list_active(channel)
            if not jobs:
                respond("Nothing is being tracked in this channel.")
                return
            lines = []
            for job in jobs:
                percent = f"{job.last_percent:.0f}%" if job.last_percent is not None else "—"
                lines.append(
                    f"• `{format_display(job.mawb)}` — {percent} "
                    f"({job.last_cleared or 0}/{job.last_total or 0}), check #{job.poll_count}"
                )
            respond("*Currently tracking:*\n" + "\n".join(lines))
            return

        # Starting and stopping are channel events: everyone watching this
        # channel needs to know an AWB is being tracked, or has stopped being
        # tracked, without having to ask who did it. Queries stay private.
        if verb == "stop":
            respond(_in_channel(_stop(store, argument or "", channel, user)))
            return

        respond(_in_channel(handle_numbers(text.split() or [text], channel, user)))
        scheduler.nudge()  # after responding: the first poll takes minutes

    # --- buttons ---------------------------------------------------------

    @app.action("awb_refresh")
    def act_refresh(ack: Ack, body: dict, client: WebClient) -> None:
        ack()
        mawb = body["actions"][0]["value"]
        channel = body["channel"]["id"]
        job = store.find_active(mawb, channel)
        if not job:
            client.chat_postEphemeral(
                channel=channel,
                user=body["user"]["id"],
                text=f"`{format_display(mawb)}` is no longer being tracked.",
            )
            return
        store.reschedule(job.id, utcnow(), increment_poll=False)
        scheduler.nudge()

    @app.action("awb_stop")
    def act_stop(ack: Ack, body: dict, client: WebClient) -> None:
        ack()
        mawb = body["actions"][0]["value"]
        channel = body["channel"]["id"]
        user = body["user"]["id"]
        message = _stop(store, mawb, channel, stopped_by=user)
        # Visible to the channel: stopping is a state change others rely on.
        client.chat_postMessage(channel=channel, text=message)

    # --- passive detection ----------------------------------------------

    @app.event("app_mention")
    def on_mention(event: dict, say) -> None:  # noqa: ANN001
        numbers = extract_all(event.get("text", ""))
        if not numbers:
            say(text=HELP, thread_ts=event.get("thread_ts"))
            return
        say(
            text=handle_numbers(numbers, event["channel"], event.get("user")),
            thread_ts=event.get("thread_ts"),
        )
        scheduler.nudge()

    @app.event("message")
    def on_message(event: dict) -> None:
        # Subscribed so Bolt does not log unhandled-request warnings. Passive
        # tracking of every channel message is intentionally NOT enabled --
        # turn it on per channel once the team has agreed to it.
        return

    return app


def _in_channel(text: str) -> dict:
    """Wrap a slash-command reply so the whole channel sees it.

    Slack defaults command replies to ephemeral, which meant only the person
    who typed /awb knew an AWB was being tracked.
    """
    return {"response_type": "in_channel", "text": text}


def _stop(store: JobStore, raw: str, channel: str, stopped_by: str | None = None) -> str:
    try:
        mawb = normalize(raw.strip(), verify_checksum=False)
    except InvalidAwbFormat as exc:
        return f"🚫 {exc.user_message}"
    job = store.find_active(mawb, channel)
    if not job:
        return f"`{format_display(mawb)}` is not being tracked in this channel."
    store.finish(job.id, "stopped", f"stopped by {stopped_by or 'user'}")
    who = f" by <@{stopped_by}>" if stopped_by else ""
    return f"🛑 Stopped tracking `{format_display(mawb)}`{who}."
