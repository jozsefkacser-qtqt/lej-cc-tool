"""Slack input: the /awb command, the buttons, and passive AWB detection.

Socket Mode is used deliberately -- it needs no public HTTPS endpoint and no
inbound firewall rule, which matters when this runs inside the corporate
network.
"""

from __future__ import annotations

import logging
import re

from slack_bolt import Ack, App, Respond
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from . import formatting
from .analytics import gather, summarise
from .awb import extract_all, format_display, normalize
from .config import Settings
from .errors import AwbChecksumFailed, InvalidAwbFormat
from .health import HEALTH
from .scheduler import PollScheduler
from .store import JobStore, utcnow

log = logging.getLogger(__name__)

#: Anything in the command that looks like an address is a recipient, so
#: `/awb 488-20744846 candy@example.com` tracks it and mails her the updates.
EMAIL_RE = re.compile(r"[^\s<>,;]+@[^\s<>,;]+\.[A-Za-z]{2,}")

#: A message that Slack posted as ordinary text although the person plainly
#: meant to run the command. `/ awb 936-02134215` -- a space after the slash --
#: is not a command to Slack, so it lands in the channel and, until this
#: existed, nothing answered it at all.
BOTCHED_COMMAND = re.compile(r"^\s*[/\\]\s*m?awb\b(.*)", re.IGNORECASE | re.DOTALL)

#: Verbs people reach for that the command does not have, and what to say.
NEAR_MISSES = {
    "ls": "list",
    "lst": "list",
    "active": "list",
    "tracking": "list",
    "show": "list",
    "cancel": "stop",
    "remove": "stop",
    "delete": "stop",
    "end": "stop",
    "health": "status",
    "alive": "status",
    "online": "status",
    "ping": "status",
    "statistics": "stats",
    "report": "stats",
}


HELP = (
    "*AWB customs-clearance tracking*\n"
    "• `/awb 488-20744846` — start tracking (several at once are fine)\n"
    "• `/awb OyTM202608137666` — booking references work too\n"
    "• `/awb 488-20744846 name@qtlogistics.eu` — and email the updates there\n"
    "• `/awb list` — what is currently being tracked in this channel\n"
    "• `/awb status` — is the tool running, and how healthy is it\n"
    "• `/awb stats [days]` — how long clearance has been taking\n"
    "• `/awb stop 488-20744846` — stop tracking\n"
    "• `/awb help` — this message\n\n"
    "The first check runs immediately, the next after 15 minutes, then every "
    "30 minutes until everything is cleared."
)


def build_app(
    settings: Settings,
    store: JobStore,
    scheduler: PollScheduler,
    running_version=None,  # noqa: ANN001
) -> App:
    app = App(token=settings.slack_bot_token, logger=log)

    help_text = HELP
    if settings.autodetect_channel_ids:
        where = ", ".join(f"<#{c}>" for c in settings.autodetect_channel_ids)
        help_text += (
            f"\n\nIn {where} you can skip the command entirely — *post the "
            "number on its own* and tracking starts by itself."
        )
    if settings.imap_enabled:
        help_text += (
            f"\n\nYou can also *email* `{settings.imap_user}` with the number in "
            "the subject line — updates come back in that mail thread."
        )

    def start_tracking(
        mawb: str,
        channel: str,
        user: str | None,
        email_to: str | None = None,
        started: list[int] | None = None,
    ) -> str:
        started = started if started is not None else []
        existing = store.find_active(mawb, channel)
        if existing:
            percent = existing.last_percent
            state = f"{percent:.0f}% cleared" if percent is not None else "first check pending"
            return (
                f"⏳ `{format_display(mawb)}` is already being tracked here "
                f"({state}, check #{existing.poll_count})."
            )
        job = store.create_job(mawb, channel, user, run_at=utcnow(), email_to=email_to)
        if job is None:  # lost a race against a duplicate command
            return f"⏳ `{format_display(mawb)}` is already being tracked here."
        started.append(job.id)
        log.info("tracking %s in %s for %s (job %s)", mawb, channel, user, job.id)
        mailed = f" Updates also go to {email_to}." if email_to else ""
        return (
            f"🔎 Tracking `{format_display(mawb)}` — first check running now. "
            "PortGround takes a couple of minutes to build the export, so the "
            f"first status will follow shortly.{mailed}"
        )

    def handle_numbers(
        raw_numbers: list[str],
        channel: str,
        user: str | None,
        email_to: str | None = None,
        started: list[int] | None = None,
    ) -> str:
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
            replies.append(start_tracking(mawb, channel, user, email_to, started))
        return "\n".join(replies)

    # --- /awb ------------------------------------------------------------

    @app.command("/awb")
    def cmd_awb(ack: Ack, command: dict, respond: Respond, client: WebClient) -> None:
        ack()
        text = (command.get("text") or "").strip()
        channel = command["channel_id"]
        user = command.get("user_id")
        # People type the command name twice: `/awb awb 936-02134215`.
        text = strip_command_echo(text)

        if not text or text.lower() in {"help", "-h", "?"}:
            respond(help_text)
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
                lines.append(
                    f"• `{format_display(job.mawb)}` — {formatting.job_percent(job)} "
                    f"({job.last_cleared or 0}/{job.last_total or 0}), poll {job.poll_count}"
                )
            respond("*Currently tracking:*\n" + "\n".join(lines))
            return

        # Starting and stopping are channel events: everyone watching this
        # channel needs to know an AWB is being tracked, or has stopped being
        # tracked, without having to ask who did it. Queries stay private.
        if verb == "stats":
            days = int(argument) if argument.strip().isdigit() else 90
            report = summarise(gather(store, window_days=days))
            respond(_in_channel("*Clearance statistics*\n" + "\n".join(report)))
            return

        if verb == "status":
            respond(
                {
                    "response_type": "in_channel",
                    "text": "AWB Tracker status",
                    "blocks": formatting.build_status_report(
                        HEALTH, store.list_active(), settings, running_version
                    ),
                }
            )
            return

        if verb == "stop":
            respond(_in_channel(_stop(store, argument or "", channel, user)))
            return

        # A verb we do not have, typed where an AWB should be. Say so, rather
        # than reporting that "status" is not a valid air waybill.
        if verb in NEAR_MISSES:
            respond(
                f"🤔 There is no `/awb {verb}`. Did you mean "
                f"`/awb {NEAR_MISSES[verb]}`?"
            )
            return

        emails = EMAIL_RE.findall(text)
        numbers = [t for t in text.split() if not EMAIL_RE.fullmatch(t)]
        allowed = [a for a in emails if settings.email_allowed(a)]
        refused = [a for a in emails if a not in allowed]

        started: list[int] = []
        reply = handle_numbers(
            numbers or [text], channel, user, ",".join(allowed) or None, started
        )
        if refused:
            reply += (
                f"\n🚫 Not mailing {', '.join(f'`{a}`' for a in refused)} — outside the "
                f"allowed domains (`{settings.email_allowed_domains}`). "
                "These updates carry customs data, so recipients are restricted."
            )
        if not started:
            # Nothing was started, so nothing will follow. The reply is the
            # whole answer and goes back over the response_url, which works
            # even where the bot cannot post.
            respond(_in_channel(reply))
            return
        if not say_in_channel(client, channel, reply, respond):
            for job_id in started:
                store.finish(job_id, "stopped", "bot cannot post in this channel")
            return
        scheduler.nudge()  # after responding: the first poll takes minutes

    # --- buttons ---------------------------------------------------------

    @app.action("awb_refresh")
    def act_refresh(ack: Ack, body: dict, client: WebClient) -> None:
        """Poll now, and say so straight away.

        The download takes a couple of minutes, so without an immediate
        answer the button looks dead -- and it looked deader still, because a
        poll that found no change used to post nothing at all.
        """
        ack()
        mawb = body["actions"][0]["value"]
        channel = body["channel"]["id"]
        user = body["user"]["id"]
        shown = format_display(mawb)

        job = store.find_active(mawb, channel)
        outcome = store.request_refresh(job.id) if job else "not_tracked"

        if outcome == "not_tracked":
            message = (
                f"`{shown}` is no longer being tracked here. "
                f"Start it again with `/awb {shown}`."
            )
        elif outcome == "already_running":
            message = (
                f"⏳ A check of `{shown}` is *already running* — PortGround is "
                "building the export now. The result will appear in this "
                "card's thread in a minute or two."
            )
        else:
            message = (
                f"🔄 Checking `{shown}` *now*. PortGround takes a couple of "
                "minutes to build the export, so the result appears in this "
                "card's thread shortly — even if nothing has changed."
            )
            scheduler.nudge()

        log.info("refresh of %s requested by %s: %s", mawb, user, outcome)
        try:
            client.chat_postEphemeral(channel=channel, user=user, text=message)
        except SlackApiError as exc:
            log.warning("could not acknowledge the refresh: %s", exc.response.get("error"))

    @app.action("awb_track")
    def act_track(ack: Ack, body: dict, client: WebClient, respond: Respond) -> None:
        """Start tracking from the "did you mean" nudge."""
        ack()
        mawb = body["actions"][0]["value"]
        channel = body["channel"]["id"]
        user = body["user"]["id"]
        started: list[int] = []
        reply = start_tracking(mawb, channel, user, None, started)
        if not started:
            respond(reply)  # already tracked, or lost a race
            return
        if not say_in_channel(client, channel, reply, respond):
            for job_id in started:
                store.finish(job_id, "stopped", "bot cannot post in this channel")
            return
        # Clear the nudge: it has been acted on and would only confuse later.
        respond({"delete_original": True})
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

    def _nudge_about_command(
        client: WebClient, channel: str, user: str, argument: str
    ) -> None:
        """Ephemeral: only the person who mistyped it sees this."""
        text, blocks = build_command_nudge(argument)
        try:
            client.chat_postEphemeral(channel=channel, user=user, text=text, blocks=blocks)
        except SlackApiError as exc:
            # In a channel the bot is not a member of, an ephemeral is refused.
            # Nothing else to try: a public message would be worse than silence.
            log.warning(
                "could not hint at %s in %s: %s", user, channel, exc.response.get("error")
            )

    def _autodetect(client: WebClient, event: dict, channel: str, user: str) -> bool:
        """Track any AWB posted here without a command. True if it acted.

        Silence when the AWB is already tracked is deliberate: people mention
        the same number all day in a channel like this, and "already being
        tracked" under every mention is the noise that gets a bot muted. The
        card is already in the channel; that is the answer.
        """
        fresh, seen = autodetect_targets(
            event.get("text", ""), channel, store, settings.autodetect_max_per_message
        )
        if not fresh:
            return bool(seen)  # seen, already tracked, nothing to say

        started: list[int] = []
        replies = [start_tracking(m, channel, user, None, started) for m in fresh]
        if not started:
            return True
        # Threaded under the message that named it, so the channel does not
        # carry two lines for every one somebody writes.
        try:
            client.chat_postMessage(
                channel=channel,
                text="\n".join(replies),
                thread_ts=event.get("thread_ts") or event.get("ts"),
            )
        except SlackApiError as exc:
            log.error("auto-detect could not confirm in %s: %s", channel, exc)
        log.info("auto-detected %s in %s from %s", ", ".join(fresh), channel, user)
        scheduler.nudge()
        return True

    # --- passive detection ----------------------------------------------

    @app.event("app_mention")
    def on_mention(event: dict, say) -> None:  # noqa: ANN001
        numbers = extract_all(event.get("text", ""))
        if not numbers:
            say(text=help_text, thread_ts=event.get("thread_ts"))
            return
        emails = ",".join(
            a for a in EMAIL_RE.findall(event.get("text", "")) if settings.email_allowed(a)
        ) or None
        say(
            text=handle_numbers(numbers, event["channel"], event.get("user"), emails),
            thread_ts=event.get("thread_ts"),
        )
        scheduler.nudge()

    @app.event("message")
    def on_message(event: dict, client: WebClient) -> None:
        """Answer a command Slack turned into an ordinary message.

        `/ awb 936-02134215` -- one space after the slash -- is not a command
        to Slack, so it is posted as text and, before this existed, absolutely
        nothing happened: no error, no hint, no trace in the log. The person
        who typed it has no way to tell that from a bot that is switched off.

        Passive tracking of *every* message is still deliberately not enabled.
        This only answers a message that is unmistakably an attempt to run the
        command, and it answers only the person who typed it.
        """
        # Never react to ourselves, to other bots, or to edits and deletions:
        # a bot answering its own message is how a loop starts.
        if event.get("bot_id") or event.get("subtype"):
            return
        text = event.get("text") or ""
        channel = event.get("channel", "")
        user = event.get("user")
        if not channel or not user:
            return

        # A direct message to the bot needs no ceremony: there is nobody else
        # in the conversation, so the intent of a bare number is not in doubt.
        if event.get("channel_type") == "im":
            numbers = extract_all(text) or [t for t in text.split() if len(t) > 5]
            reply = handle_numbers(numbers, channel, user) if numbers else help_text
            client.chat_postMessage(channel=channel, text=reply)
            scheduler.nudge()
            return

        # In a channel set aside for this, a bare AWB is the command. The
        # IATA check digit is what makes that safe: only a number that passes
        # it is acted on, so nothing else in the message can start a job.
        if settings.autodetect_in(channel) and _autodetect(client, event, channel, user):
            return

        match = BOTCHED_COMMAND.match(text)
        if not match:
            return
        _nudge_about_command(client, channel, user, match.group(1))

    return app


def strip_command_echo(text: str) -> str:
    """Drop a label people repeat after the command: `/awb AWB 936-…`."""
    return re.sub(r"^(?:awb|mawb|nr\.?|no\.?)[\s:]+", "", text, flags=re.IGNORECASE)


def usable_identifiers(text: str) -> list[str]:
    """Every identifier in `text` that could actually be tracked."""
    candidates = extract_all(text) or [
        t for t in text.split() if len(t) > 5 and not EMAIL_RE.fullmatch(t)
    ]
    found: list[str] = []
    for raw in candidates:
        try:
            value = normalize(raw)
        except (AwbChecksumFailed, InvalidAwbFormat):
            continue
        if value not in found:
            found.append(value)
    return found


def autodetect_targets(
    text: str, channel: str, store: JobStore, limit: int
) -> tuple[list[str], list[str]]:
    """Split the AWBs in `text` into (not yet tracked here, already tracked).

    Only checksum-valid master air waybills are considered. Booking
    references are deliberately excluded: they carry no checksum, so a
    pattern loose enough to catch one would also catch order numbers and
    file names -- the same reasoning that keeps them out of email bodies.
    """
    found = extract_all(text)[:limit]
    fresh: list[str] = []
    seen: list[str] = []
    for mawb in found:
        (seen if store.find_active(mawb, channel) else fresh).append(mawb)
    return fresh, seen


def build_command_nudge(argument: str) -> tuple[str, list[dict]]:
    """The "that was not a command" hint, and a button if we can act on it."""
    usable = usable_identifiers(argument)
    lines = [
        "👋 That looked like a command, but Slack posted it as a normal "
        "message — so nothing was tracked.",
        "",
        "A slash command takes *no space after the slash*:",
        "",
        "  ✅  `/awb 936-02134215`",
        "  🚫  `/ awb 936-02134215`  ← space after the slash",
    ]
    if not usable:
        lines += ["", "Type `/awb help` to see everything it understands."]
        text = "\n".join(lines)
        return text, [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]

    lines += ["", f"Want me to start `{format_display(usable[0])}` now?"]
    text = "\n".join(lines)
    return text, [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": f"Track {format_display(usable[0])}",
                        "emoji": True,
                    },
                    "style": "primary",
                    "action_id": "awb_track",
                    "value": usable[0],
                }
            ],
        },
    ]


def say_in_channel(client: WebClient, channel: str, text: str, respond) -> bool:  # noqa: ANN001
    """Post to the channel, and prove the later updates can get there.

    The slash-command reply travels back over a response_url, which works in
    a channel the bot cannot post in -- so a confirmation could appear and
    then every status update after it vanish silently. Sending the
    confirmation with the same call the tracker uses turns that into an error
    somebody can act on, while they are still there to act on it.
    """
    try:
        client.chat_postMessage(channel=channel, text=text)
    except SlackApiError as exc:
        code = exc.response.get("error", "")
        if code not in {"channel_not_found", "not_in_channel", "is_archived"}:
            raise
        log.warning("cannot post in %s: %s", channel, code)
        respond(
            "🚫 *I can't post in this channel*, so you would never see the updates.\n"
            "Invite me first — type `/invite @AWB Tracker` here — then run the "
            "command again."
            + (" _(This channel is archived.)_" if code == "is_archived" else "")
        )
        return False
    return True


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
