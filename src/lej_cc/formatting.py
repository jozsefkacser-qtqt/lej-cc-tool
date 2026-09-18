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
#: Cells for shipments customs has taken for examination. They fill the bar
#: like cleared ones -- nothing here is going to move them -- but they are
#: plainly not the same thing, so they read as their own block.
BAR_INSPECTION = "🟥"
#: Filled colour by completeness, so the state of an AWB reads at a glance
#: from across the room: green nearly done, amber mid-flight, orange barely
#: started. Not red at the low end -- red is reserved for shipments customs
#: has taken, and a barely-started AWB looked identical to a fully inspected
#: one when both were red.
BAR_COLOURS = ((95.0, "🟩"), (50.0, "🟨"), (0.0, "🟧"))


def bar_colour(percent: float) -> str:
    return next(colour for threshold, colour in BAR_COLOURS if percent >= threshold)


def _cells(percent: float, width: int) -> int:
    """How many cells a percentage earns.

    Only a genuine 100% earns every cell: 97.8% rounding up to ten would say
    "finished" about an AWB with 34 shipments still stuck. Anything above
    zero earns at least one, because "some" and "none" must look different.
    """
    percent = max(0.0, min(100.0, percent))
    if percent >= 100.0:
        return width
    filled = min(width - 1, int(percent * width / 100))
    return max(1, filled) if percent > 0 else 0


def _split(total: int, percent: float, inspection: float) -> tuple[int, int, int]:
    """How many cells go to cleared, to inspection, and to still open.

    One place, so the bar, the grid and the slim rendering can never
    disagree about what a percentage looks like.
    """
    cleared = _cells(percent, total)
    settled = _cells(percent + inspection, total)
    # Never let rounding give inspection more room than there is left, and
    # never let it hide a cleared cell.
    held = max(0, min(total - cleared, settled - cleared))
    if inspection > 0 and held == 0 and cleared < total:
        held = 1  # one held shipment must still be visible
    return cleared, held, total - cleared - held


def cells_for(percent: float, override: tuple[str, str, str] | None = None) -> tuple[str, str, str]:
    """The three cell characters: cleared, inspection, open.

    An override lets a workspace swap in its own narrow custom emoji --
    the only way to have a slim bar that is still coloured, since Slack
    gives every standard emoji the same square box.
    """
    cleared, held, open_ = override or ("", "", "")
    return (cleared or bar_colour(percent), held or BAR_INSPECTION, open_ or BAR_EMPTY)


def progress_bar(
    percent: float,
    width: int = BAR_WIDTH,
    inspection: float = 0.0,
    override: tuple[str, str, str] | None = None,
) -> str:
    """Ten cells: cleared, then under inspection, then still open.

    The two filled blocks together say how much of the AWB is settled, which
    is the number that decides whether anyone has to do anything; keeping
    them as separate colours says how much of that is actually released.
    """
    cleared, held, empty = _split(width, percent, inspection)
    glyphs = cells_for(percent, override)
    return glyphs[0] * cleared + glyphs[1] * held + glyphs[2] * empty


#: A ten-by-ten grid of cells, one per percent. Ten cells cannot show 1.1%
#: as anything smaller than a tenth of the bar, which drew twelve held
#: shipments out of 1,079 as if they were a tenth of the AWB. A hundred
#: cells cost nothing to render and tell the truth to the nearest percent.
GRID_ROWS = 10
GRID_COLS = 10


def progress_grid(
    percent: float,
    inspection: float = 0.0,
    rows: int = GRID_ROWS,
    cols: int = GRID_COLS,
    override: tuple[str, str, str] | None = None,
) -> str:
    """The same three blocks as the bar, laid out as a square.

    Ten lines of ten, which fits a phone as well as a desktop -- a single
    row of a hundred would wrap differently on every screen.
    """
    total = rows * cols
    cleared, held, empty = _split(total, percent, inspection)
    glyphs = cells_for(percent, override)
    # A list, not a string: a custom emoji cell is ten characters long, and
    # slicing the joined text would cut `:cc-done:` in half at the row break.
    cells = [glyphs[0]] * cleared + [glyphs[1]] * held + [glyphs[2]] * empty
    return "\n".join("".join(cells[i : i + cols]) for i in range(0, total, cols))


#: The slim rendering: monospace block characters inside a code fence.
#: Slack gives no way to colour text, so a bar that is genuinely narrow
#: cannot also be coloured -- unless a workspace uploads narrow custom
#: emoji, which `override` exists for. Density carries the meaning instead:
#: solid is cleared, dark is held, light is still open.
SLIM_CLEARED = "█"
SLIM_INSPECTION = "▓"
SLIM_OPEN = "░"
SLIM_COLS = 50
SLIM_ROWS = 2


def progress_slim(
    percent: float,
    inspection: float = 0.0,
    cols: int = SLIM_COLS,
    rows: int = SLIM_ROWS,
    override: tuple[str, str, str] | None = None,
) -> str:
    """Two rows of fifty, still one cell per percent, a fraction of the height."""
    total = cols * rows
    cleared, held, empty = _split(total, percent, inspection)
    glyphs = override or (SLIM_CLEARED, SLIM_INSPECTION, SLIM_OPEN)
    cells = [glyphs[0]] * cleared + [glyphs[1]] * held + [glyphs[2]] * empty
    body = "\n".join("".join(cells[i : i + cols]) for i in range(0, total, cols))
    return f"```\n{body}\n```"


def progress_visual(
    percent: float,
    inspection: float = 0.0,
    style: str = "slim",
    override: tuple[str, str, str] | None = None,
) -> str:
    if style == "bar":
        return progress_bar(percent, inspection=inspection, override=override)
    # Custom cells are emoji, and a code fence prints `:cc-done:` as literal
    # text rather than rendering it. Someone who has gone to the trouble of
    # uploading narrow emoji wants to see them, so the grid carries them.
    if style == "grid" or override:
        return progress_grid(percent, inspection, override=override)
    return progress_slim(percent, inspection, override=override)


def breakdown_lines(
    snapshot: Snapshot,
    style: str = "slim",
    override: tuple[str, str, str] | None = None,
) -> str:
    """Cleared, held, and the sum of the two, one per line.

    Each line is marked with the very cell used to draw it above, so the
    bar needs no separate legend whichever style is in use. With nothing
    under inspection the sum would only repeat the first line, so there is
    just the one number.
    """
    if style == "slim" and not override:
        done, held = f"`{SLIM_CLEARED}`", f"`{SLIM_INSPECTION}`"
    else:
        glyphs = cells_for(snapshot.percent, override)
        done, held = glyphs[0], glyphs[1]

    if not snapshot.inspection:
        return f"{done}  *{snapshot.percent:.1f}%* cleared"
    return "\n".join(
        [
            f"{done}  *{snapshot.percent:.1f}%* cleared",
            f"{held}  *{snapshot.percent_inspection:.1f}%* inspection",
            f"　  *= {snapshot.percent_settled:.1f}% completed*",
        ]
    )


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


def _inspection_list(snapshot: Snapshot, threshold: int) -> str:
    """Name the held shipments while naming them is useful."""
    hawbs = [r.hawb for r in snapshot.inspection_rows]
    if len(hawbs) <= threshold:
        return ", ".join(f"`{h}`" for h in hawbs)
    return "_Listed in the attached workbook, marked_ `inspection`."


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
    style: str = "slim",
    cells: tuple[str, str, str] | None = None,
) -> list[dict]:
    """The status card. `is_final` switches the wording to a closing note."""
    mawb = format_display(snapshot.mawb)
    done = snapshot.is_complete
    held = snapshot.inspection

    # The state of the clearance leads, because that is what someone
    # scanning the channel is looking for; the number identifies which one.
    if done:
        headline = f"✅ CC Finished — {mawb}"
    elif is_final:
        headline = f"⚠️ CC Stopped — {mawb}, still open"
    else:
        headline = f"📦 CC In progress — {mawb}"

    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": headline, "emoji": True}},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": progress_visual(
                    snapshot.percent, snapshot.percent_inspection, style, cells
                ),
            },
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": breakdown_lines(snapshot, style, cells)},
        },
    ]

    fields = [
        {
            "type": "mrkdwn",
            "text": f"*✅ Cleared*\n{snapshot.cleared:,} of {snapshot.total:,}",
        },
        {"type": "mrkdwn", "text": f"*⏳ Open*\n{snapshot.open_count:,}"},
    ]
    if held:
        fields.append(
            {
                "type": "mrkdwn",
                "text": (
                    f"*❌ Under inspection*\n{held:,}"
                    f"  ({snapshot.percent_inspection:.1f}%)"
                ),
            }
        )
    fields += [
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

    if held:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*❌ {held:,} shipment(s) taken by customs for examination.*\n"
                        + _inspection_list(snapshot, inline_threshold)
                        + (
                            "\n_Tracking stops here: the rest is a customs decision "
                            "on a customs timetable, and re-checking every 30 minutes "
                            f"does not change it. Run `/awb {mawb}` when you want a "
                            "fresh look._"
                            if done
                            else "\n_Not counted as open — nobody here can move them._"
                        )
                    ),
                },
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
        if diff.newly_inspected:
            parts.append(f"❌ {len(diff.newly_inspected):,} taken for *inspection*")
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
                        # Green: it is the safe, useful action on this card,
                        # and it sat next to a red one looking like neither.
                        "style": "primary",
                        "text": {"type": "plain_text", "text": "🔄 Check now"},
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
