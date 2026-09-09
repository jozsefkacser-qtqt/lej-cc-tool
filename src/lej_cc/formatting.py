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
                f"*📦 Items*\n{snapshot.items_cleared:,} of {snapshot.items_total:,} "
                f"({snapshot.items_percent:.0f}%)"
            ),
        },
    ]
    if tracking_since:
        reference = snapshot.fetched_at or datetime.now(LOCAL_TZ)
        fields.append(
            {"type": "mrkdwn", "text": f"*⏱ Tracking*\n{_duration(reference - tracking_since)}"}
        )
    blocks.append({"type": "section", "fields": fields})

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
    if snapshot.generated_at:
        context.append(f"📄 data {_hhmm(snapshot.generated_at)}")
    if next_run_at and not done and not is_final:
        context.append(f"next check {_hhmm(next_run_at)}")
    if poll_count:
        context.append(f"check #{poll_count}")
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
