"""Slack message construction (Block Kit).

Three constraints shape the layout:

  * A master AWB can carry well over a thousand house shipments, so the open
    list is only ever named when naming it is useful; past that the chase
    sheet carries it.
  * Slack caps a section at 3000 characters and a message at 50 blocks, so
    the block count is fixed and nothing loops unboundedly over rows.
  * Most people read this on a phone. The bar is ten cells wide, not twenty,
    and the numbers come before the detail.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import version
from .awb import format_display
from .model import Snapshot, SnapshotDiff

LOCAL_TZ = ZoneInfo("Europe/Berlin")

BAR_WIDTH = 10
BAR_EMPTY = "⬜"
#: Filled colour by completeness, so the state of an AWB reads at a glance
#: from across the room: green nearly done, amber mid-flight, red barely started.
BAR_COLOURS = ((95.0, "🟩"), (50.0, "🟨"), (0.0, "🟥"))


def bar_colour(percent: float) -> str:
    return next(colour for threshold, colour in BAR_COLOURS if percent >= threshold)


def progress_bar(percent: float, width: int = BAR_WIDTH) -> str:
    """Ten cells, coloured by state.

    Only a genuine 100% shows a full bar: 97.8% rounding up to ten green
    cells would say "finished" about an AWB with 34 shipments still stuck.
    """
    percent = max(0.0, min(100.0, percent))
    if percent >= 100.0:
        filled = width
    else:
        filled = min(width - 1, int(percent * width / 100))
        if percent > 0:
            filled = max(1, filled)  # any progress at all should be visible
    return bar_colour(percent) * filled + BAR_EMPTY * (width - filled)


def _hhmm(value: datetime | None) -> str:
    return value.astimezone(LOCAL_TZ).strftime("%H:%M") if value else "—"


def _duration(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def _stamp(value: datetime) -> str:
    return value.astimezone(LOCAL_TZ).strftime("%d %b %Y %H:%M")


def _completion_note(snapshot: Snapshot, reference: datetime) -> str:
    """How long ago it finished, and how long it took.

    Someone looking up a finished AWB is usually asking "is this the one from
    May?" -- an age answers that; the word "cleared" on its own does not.
    """
    last = snapshot.last_clearance
    assert last is not None
    age = reference - last
    when = "just now" if age < timedelta(minutes=2) else f"{_duration(age)} ago"

    note = f"⏱ {when}"
    first = snapshot.first_clearance
    if first and last > first:
        note += f" · cleared over {_duration(last - first)}"
    return note


def _eta_text(eta: datetime, now: datetime) -> str:
    """Say the time the way someone reading it would.

    "~17:40" when that is today, because the date is noise; the day when it
    is not, because "~09:15" about tomorrow morning is a trap.
    """
    local_eta = eta.astimezone(LOCAL_TZ)
    local_now = now.astimezone(LOCAL_TZ)
    days = (local_eta.date() - local_now.date()).days
    if days <= 0:
        return f"~{local_eta:%H:%M}"
    if days == 1:
        return f"~{local_eta:%H:%M} tomorrow"
    return f"~{local_eta:%a %d %b %H:%M}"


def _open_section(hawbs: list[str], threshold: int) -> str:
    """Name the open shipments only while naming them is useful.

    Fifteen tracking numbers followed by "and 19 more" is not a work list --
    it is noise that hides the count. Past the threshold the message states
    the number and hands the detail to the attached chase sheet.
    """
    if not hawbs:
        return "—"
    if len(hawbs) <= threshold:
        return ", ".join(f"`{h}`" for h in hawbs)
    return (
        f"{len(hawbs):,} shipments still open — see the attached "
        "*OPEN…xlsx*, oldest first."
    )


def summary_line(snapshot: Snapshot) -> str:
    """One-line form, used for `/awb list` and the notification fallback."""
    return (
        f"{format_display(snapshot.mawb)} — {snapshot.percent:.0f}% cleared "
        f"({snapshot.cleared:,}/{snapshot.total:,})"
    )


def build_status_blocks(
    snapshot: Snapshot,
    *,
    diff: SnapshotDiff | None = None,
    next_run_at: datetime | None = None,
    requested_by: str | None = None,
    inline_threshold: int = 10,
    is_final: bool = False,
    poll_count: int = 0,
    tracking_since: datetime | None = None,
    forecast=None,  # noqa: ANN001 - a forecast.Forecast
) -> list[dict]:
    """The status card. `is_final` switches the wording to a closing note."""
    mawb = format_display(snapshot.mawb)
    done = snapshot.is_complete

    if done:
        headline = f"✅ {mawb} — cleared"
    elif is_final:
        headline = f"⚠️ {mawb} — stopped, still open"
    else:
        headline = f"📦 {mawb} — customs clearance"

    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": headline, "emoji": True}},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{progress_bar(snapshot.percent)}  *{snapshot.percent:.1f}%*",
            },
        },
    ]

    fields = [
        {
            "type": "mrkdwn",
            "text": f"*✅ Cleared*\n{snapshot.cleared:,} of {snapshot.total:,}",
        },
        {"type": "mrkdwn", "text": f"*⏳ Open*\n{snapshot.not_cleared + snapshot.other:,}"},
        {
            "type": "mrkdwn",
            "text": (
                f"*🧾 Declaration lines*\n{snapshot.items_cleared:,} of "
                f"{snapshot.items_total:,} "
                f"({snapshot.items_percent:.0f}%)"
            ),
        },
    ]
    reference = snapshot.fetched_at or datetime.now(LOCAL_TZ)
    if forecast is not None and not done:
        fields.append(
            {
                "type": "mrkdwn",
                "text": (
                    f"*🔮 Expected done*\n{_eta_text(forecast.eta, reference)}"
                    f"  ·  {forecast.per_hour:,.0f}/h"
                ),
            }
        )
    if done and snapshot.last_clearance:
        fields.append(
            {
                "type": "mrkdwn",
                "text": f"*🏁 Finished*\n{_stamp(snapshot.last_clearance)}",
            }
        )
    elif tracking_since:
        fields.append(
            {"type": "mrkdwn", "text": f"*⏱ Tracking*\n{_duration(reference - tracking_since)}"}
        )
    blocks.append({"type": "section", "fields": fields})

    if done and snapshot.last_clearance:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": _completion_note(snapshot, reference)}
                ],
            }
        )

    if snapshot.other:
        unknown = ", ".join(f"`{k}` ×{v:,}" for k, v in sorted(snapshot.unknown_statuses.items()))
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*❓ Unrecognised status ({snapshot.other:,})* — {unknown}\n"
                        "_Counted as not cleared. Please report these so the mapping "
                        "can be extended._"
                    ),
                },
            }
        )

    if diff and diff.has_changes:
        parts = []
        if diff.newly_cleared:
            parts.append(f"*▲ {len(diff.newly_cleared):,} cleared*")
        if diff.newly_added:
            parts.append(f"+{len(diff.newly_added):,} new")
        if diff.removed:
            parts.append(f"−{len(diff.removed):,} removed")
        if diff.regressed:
            parts.append(f":rotating_light: {len(diff.regressed):,} back to *not cleared*")
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "Since last check: " + " · ".join(parts)}],
            }
        )

    if not done and snapshot.open_rows:
        blocks.append({"type": "divider"})
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": _open_section([r.hawb for r in snapshot.open_rows], inline_threshold),
                },
            }
        )

    context: list[str] = []
    if snapshot.resolved_mawbs:
        shown = ", ".join(format_display(m) for m in snapshot.resolved_mawbs[:3])
        if len(snapshot.resolved_mawbs) > 3:
            shown += f" +{len(snapshot.resolved_mawbs) - 3} more"
        context.append(f"✈️ MAWB {shown}")
    if snapshot.generated_at:
        context.append(f"📄 data {_hhmm(snapshot.generated_at)}")
    if next_run_at and not done and not is_final:
        context.append(f"next check {_hhmm(next_run_at)}")
    if poll_count:
        # No leading "#": Slack treats #something as a channel reference and
        # renders "check #1" as "check 🔒Private channel".
        context.append(f"poll {poll_count}")
    if requested_by:
        context.append(f"by <@{requested_by}>")
    if context:
        blocks.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": " · ".join(context)}]}
        )

    if not done and not is_final:
        blocks.append(
            {
                "type": "actions",
                "block_id": f"awb_actions_{snapshot.mawb}",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "awb_refresh",
                        "text": {"type": "plain_text", "text": "🔄 Refresh now"},
                        "value": snapshot.mawb,
                    },
                    {
                        "type": "button",
                        "action_id": "awb_stop",
                        "style": "danger",
                        "text": {"type": "plain_text", "text": "Stop tracking"},
                        "value": snapshot.mawb,
                    },
                ],
            }
        )

    return blocks


def _looks_unhealthy(health, active_jobs: list) -> bool:  # noqa: ANN001
    """Amber means "running but not working".

    With nothing being tracked there is nothing to poll, so no successful
    poll is the correct state rather than a warning. And a bot that started
    a minute ago has not had time yet.
    """
    if not active_jobs:
        return False
    if health.last_poll_ok_at is None:
        return health.uptime > timedelta(minutes=10)
    return (datetime.now(LOCAL_TZ) - health.last_poll_ok_at) > timedelta(minutes=90)


def build_status_report(
    health, active_jobs: list, settings, running_version=None
) -> list[dict]:  # noqa: ANN001
    """The answer to `/awb status`.

    Note what the absence of this message means: if the bot is down, Slack
    answers "the app did not respond" instead. No answer is an answer.
    """
    last_poll = health.last_poll_ok_at
    icon = "🟠" if _looks_unhealthy(health, active_jobs) else "🟢"

    lines = [
        f"{icon} *AWB Tracker is online* — up {_duration(health.uptime)}",
        "",
        f"*Tracking:* {len(active_jobs)} AWB(s)",
    ]
    for job in active_jobs[:10]:
        percent = (
            f"{job.last_percent:.1f}%" if job.last_percent is not None else "first check"
        )
        lines.append(f"  • `{format_display(job.mawb)}` — {percent}, next {_hhmm(job.next_run_at)}")
    if len(active_jobs) > 10:
        lines.append(f"  _…and {len(active_jobs) - 10} more_")

    lines += [
        "",
        f"*Last successful check:* {_hhmm(last_poll)}" + (
            f" ({_duration(datetime.now(LOCAL_TZ) - last_poll)} ago)"
            if last_poll
            else " — none yet"
        ),
        f"*Checks:* {health.polls_ok} ok · {health.polls_failed} failed",
    ]
    if health.last_api_seconds:
        lines.append(f"*PortGround response:* {health.last_api_seconds:.0f}s last time")
    if health.last_poll_error:
        lines.append(
            f"*Last error:* {health.last_poll_error} at {_hhmm(health.last_poll_error_at)}"
        )
    lines.append(
        "*Email:* " + ("on" if settings.email_enabled else "off (Slack only)")
    )
    if settings.autodetect_channel_ids:
        where = ", ".join(f"<#{c}>" for c in settings.autodetect_channel_ids)
        lines.append(f"*Auto-detect:* on in {where} — post a number, no command needed")
    if settings.imap_enabled:
        seen = _hhmm(health.last_inbox_poll_at) if health.last_inbox_poll_at else "not yet"
        lines.append(
            f"*Email trigger:* on ({settings.imap_user}) — last checked {seen}, "
            f"{health.emails_accepted} accepted · {health.emails_refused} ignored"
        )

    if running_version is not None:
        lines.append(f"*Running:* `{running_version}`")
        checked_out = version.current()
        if checked_out.commit != running_version.commit:
            lines.append(
                f"⚠️ *`{checked_out.commit}` is checked out but not running* — "
                "the code was updated after the bot started. Restart to pick it up."
            )

    return [{"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}}]


def build_escalation_blocks(
    snapshot: Snapshot,
    stalled_for: timedelta,
    *,
    mention: str = "",
    last_progress_at: datetime | None = None,
) -> list[dict]:
    """The one message that breaks the silence when an AWB stops moving.

    Sent once per stall, not once per poll. Without it, "nothing to report"
    and "nothing has happened for six hours" look identical in a channel.
    """
    open_rows = snapshot.open_rows
    since = (
        f" Nothing has cleared since {_stamp(last_progress_at)}."
        if last_progress_at
        else ""
    )
    who = f"\n{mention}" if mention else ""

    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"⚠️ *{format_display(snapshot.mawb)} has not moved in "
                    f"{_duration(stalled_for)}*\n"
                    f"Still at {snapshot.percent:.1f}% — *{len(open_rows):,} shipment(s) "
                    f"still open*.{since}{who}"
                ),
            },
        }
    ]


def build_error_blocks(mawb: str, message: str, *, fatal: bool = True) -> list[dict]:
    icon = "🚫" if fatal else "⚠️"
    suffix = "" if fatal else "\n_Tracking continues; will retry at the next check._"
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{icon} *{format_display(mawb)}*\n{message}{suffix}",
            },
        }
    ]


def unchanged_text(snapshot: Snapshot, next_run_at: datetime | None) -> str:
    """The compact thread reply used when a poll found no change."""
    nxt = f" · next check {_hhmm(next_run_at)}" if next_run_at else ""
    return (
        f"No change — still {snapshot.percent:.1f}% "
        f"({snapshot.cleared:,}/{snapshot.total:,} cleared){nxt}"
    )
