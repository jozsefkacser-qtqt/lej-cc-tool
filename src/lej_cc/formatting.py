"""Slack message construction (Block Kit).

Two constraints shape everything here:

  * A master AWB can carry well over a thousand house shipments -- the
    reference file for 936-00333955 has 1605 -- so the open list is always
    truncated and the workbook attachment carries the full detail.
  * Slack caps a section's text at 3000 characters and a message at 50
    blocks, so we build a fixed small number of blocks and never loop
    unboundedly over rows.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .awb import format_display
from .model import Snapshot, SnapshotDiff

LOCAL_TZ = ZoneInfo("Europe/Berlin")

BAR_WIDTH = 20
BAR_FULL = "█"
BAR_EMPTY = "░"


def progress_bar(percent: float, width: int = BAR_WIDTH) -> str:
    filled = int(round(width * max(0.0, min(100.0, percent)) / 100.0))
    return BAR_FULL * filled + BAR_EMPTY * (width - filled)


def _hhmm(value: datetime | None) -> str:
    return value.astimezone(LOCAL_TZ).strftime("%H:%M") if value else "—"


def _truncate_list(hawbs: list[str], limit: int) -> str:
    if not hawbs:
        return "—"
    shown = ", ".join(f"`{h}`" for h in hawbs[:limit])
    if len(hawbs) > limit:
        shown += f"  _…and {len(hawbs) - limit:,} more (see attached file)_"
    return shown


def summary_line(snapshot: Snapshot) -> str:
    """One-line form, used for `/awb list` and for the channel fallback text."""
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
    max_listed: int = 15,
    is_final: bool = False,
    poll_count: int = 0,
) -> list[dict]:
    """Full status card. `is_final` switches the wording to a completion note."""
    mawb = format_display(snapshot.mawb)
    done = snapshot.is_complete

    if done:
        headline = f"✅ MAWB {mawb} — customs clearance complete"
    elif is_final:
        headline = f"⚠️ MAWB {mawb} — tracking stopped, still incomplete"
    else:
        headline = f"📦 MAWB {mawb} — customs clearance"

    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": headline, "emoji": True}},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"`{progress_bar(snapshot.percent)}`  *{snapshot.percent:.1f}%*\n"
                    f"*{snapshot.cleared:,}* of *{snapshot.total:,}* shipments cleared "
                    f"· {snapshot.items_cleared:,}/{snapshot.items_total:,} items "
                    f"({snapshot.items_percent:.0f}%)"
                ),
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*✅ Cleared*\n{snapshot.cleared:,}"},
                {"type": "mrkdwn", "text": f"*⏳ Not cleared*\n{snapshot.not_cleared:,}"},
            ],
        },
    ]

    if snapshot.other:
        unknown = ", ".join(f"`{k}` ×{v:,}" for k, v in sorted(snapshot.unknown_statuses.items()))
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*❓ Other ({snapshot.other:,})* — unrecognised status values: {unknown}\n"
                        "_These are counted as not cleared. Please report them so the "
                        "status mapping can be extended._"
                    ),
                },
            }
        )

    if diff and diff.has_changes:
        parts = []
        if diff.newly_cleared:
            parts.append(f"*+{len(diff.newly_cleared):,} cleared*")
        if diff.newly_added:
            parts.append(f"+{len(diff.newly_added):,} new shipment(s)")
        if diff.removed:
            parts.append(f"−{len(diff.removed):,} removed")
        if diff.regressed:
            parts.append(f":rotating_light: {len(diff.regressed):,} reverted to *not cleared*")
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "Since last check: " + " · ".join(parts)}],
            }
        )

    if not done and snapshot.open_rows:
        open_hawbs = [r.hawb for r in snapshot.open_rows]
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*Open ({len(open_hawbs):,}):*\n"
                        f"{_truncate_list(open_hawbs, max_listed)}"
                    ),
                },
            }
        )

    context: list[str] = []
    if snapshot.generated_at:
        context.append(f"Data as of {_hhmm(snapshot.generated_at)}")
    if next_run_at and not done and not is_final:
        context.append(f"next check {_hhmm(next_run_at)}")
    if poll_count:
        context.append(f"check #{poll_count}")
    if requested_by:
        context.append(f"started by <@{requested_by}>")
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
                        "text": {"type": "plain_text", "text": "Refresh now"},
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
                "text": f"{icon} *MAWB {format_display(mawb)}*\n{message}{suffix}",
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
