"""Email notifications over SMTP.

Deliberately plainer than the Slack card. Email is read once, often on a
phone, often by someone who was not watching the channel -- so it leads with
the number, says what changed, and attaches the two workbooks. It never
sends a "nothing happened" message: that is what makes people filter a
notifier into a folder they stop opening.

Mails for one AWB thread into a single conversation: the first mail's
Message-ID is stored on the job and referenced by every later one.
"""

from __future__ import annotations

import logging
import mimetypes
import smtplib
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path

from .awb import format_display
from .formatting import LOCAL_TZ, bar_colour
from .model import Snapshot, SnapshotDiff

log = logging.getLogger(__name__)

# Slack's colours, as hex, so the two channels read as one system.
COLOURS = {"🟩": "#2e7d32", "🟨": "#ed6c02", "🟥": "#c62828"}
TRACK = "#e3e6ea"
INK = "#1f3b57"
MUTED = "#5b6b7a"


def _percent_colour(percent: float) -> str:
    return COLOURS[bar_colour(percent)]


def _hhmm(value: datetime | None) -> str:
    return value.astimezone(LOCAL_TZ).strftime("%H:%M") if value else "—"


def _stamp(value: datetime | None) -> str:
    return value.astimezone(LOCAL_TZ).strftime("%d %b %Y %H:%M") if value else "—"


def _stat(label: str, value: str) -> str:
    return (
        f'<td style="padding:10px 16px 10px 0;vertical-align:top">'
        f'<div style="font-size:11px;letter-spacing:.06em;text-transform:uppercase;'
        f'color:{MUTED};padding-bottom:3px">{label}</div>'
        f'<div style="font-size:17px;color:{INK};font-weight:600">{value}</div></td>'
    )


def render_html(
    snapshot: Snapshot,
    *,
    diff: SnapshotDiff | None = None,
    next_run_at: datetime | None = None,
    is_final: bool = False,
) -> str:
    """The mail body. Tables and inline styles, because email clients."""
    mawb = format_display(snapshot.mawb)
    done = snapshot.is_complete
    colour = _percent_colour(snapshot.percent)
    filled = max(0.0, min(100.0, snapshot.percent))

    if done:
        heading, badge = f"{mawb} — cleared", "COMPLETE"
    elif is_final:
        heading, badge = f"{mawb} — stopped, still open", "STOPPED"
    else:
        heading, badge = f"{mawb} — customs clearance", "IN PROGRESS"

    stats = [
        _stat("Cleared", f"{snapshot.cleared:,} of {snapshot.total:,}"),
        _stat("Open", f"{snapshot.not_cleared + snapshot.other:,}"),
        _stat(
            "Declaration lines",
            f"{snapshot.items_cleared:,} of {snapshot.items_total:,}",
        ),
    ]
    if done and snapshot.last_clearance:
        stats.append(_stat("Finished", _stamp(snapshot.last_clearance)))
    elif next_run_at:
        stats.append(_stat("Next check", _hhmm(next_run_at)))

    change = ""
    if diff and diff.has_changes:
        parts = []
        if diff.newly_cleared:
            parts.append(f"<b>{len(diff.newly_cleared):,} newly cleared</b>")
        if diff.newly_added:
            parts.append(f"{len(diff.newly_added):,} new shipments")
        if diff.removed:
            parts.append(f"{len(diff.removed):,} removed")
        if diff.regressed:
            parts.append(
                f'<b style="color:{COLOURS["🟥"]}">'
                f"{len(diff.regressed):,} reverted to not cleared</b>"
            )
        change = (
            f'<p style="margin:0 0 18px;font-size:14px;color:{INK}">'
            f'Since the last check: {" · ".join(parts)}.</p>'
        )

    unknown = ""
    if snapshot.other:
        values = ", ".join(f"{k} ×{v:,}" for k, v in sorted(snapshot.unknown_statuses.items()))
        unknown = (
            f'<p style="margin:0 0 18px;padding:12px 14px;border-radius:6px;'
            f'background:#fff4e5;font-size:13px;color:#7a4100">'
            f"<b>{snapshot.other:,} shipment(s) carry a status we do not recognise</b> "
            f"({values}). They are counted as not cleared. Please report them so the "
            f"mapping can be extended.</p>"
        )

    open_note = ""
    if not done and snapshot.open_rows:
        open_note = (
            f'<p style="margin:0 0 18px;font-size:14px;color:{INK}">'
            f"<b>{len(snapshot.open_rows):,} shipments are still open.</b> "
            f"The attached <i>OPEN…xlsx</i> lists them, oldest first, with the "
            f"columns needed to chase them.</p>"
        )

    return f"""\
<!doctype html><html><body style="margin:0;padding:24px;background:#f4f6f8;
 font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
 style="max-width:620px;margin:0 auto;background:#fff;border-radius:10px;
 border:1px solid #dde3e9"><tr><td style="padding:22px 26px">

<div style="font-size:11px;letter-spacing:.1em;color:{colour};font-weight:700">{badge}</div>
<h1 style="margin:4px 0 18px;font-size:21px;color:{INK};font-weight:650">{heading}</h1>

<div style="font-size:34px;font-weight:700;color:{colour};line-height:1">
  {snapshot.percent:.1f}%</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
 style="margin:10px 0 22px;height:10px;border-radius:5px;overflow:hidden;background:{TRACK}">
 <tr><td style="width:{filled:.2f}%;background:{colour};font-size:0;line-height:0">&nbsp;</td>
     <td style="font-size:0;line-height:0">&nbsp;</td></tr></table>

<table role="presentation" cellpadding="0" cellspacing="0" style="margin:0 0 20px">
 <tr>{stats[0]}{stats[1]}</tr><tr>{stats[2]}{stats[3] if len(stats) > 3 else ""}</tr></table>

{change}{unknown}{open_note}

<p style="margin:22px 0 0;padding-top:14px;border-top:1px solid #e8edf1;
 font-size:12px;color:{MUTED}">
 Data generated by PortGround at {_hhmm(snapshot.generated_at)}.
 Automatic notification from the LEJ customs-clearance tracker.</p>

</td></tr></table></body></html>"""


def render_text(
    snapshot: Snapshot,
    *,
    diff: SnapshotDiff | None = None,
    next_run_at: datetime | None = None,
    is_final: bool = False,
) -> str:
    """Plain-text alternative, for clients that refuse HTML."""
    mawb = format_display(snapshot.mawb)
    lines = [
        f"MAWB {mawb} — {snapshot.percent:.1f}% cleared",
        "",
        f"Cleared:           {snapshot.cleared:,} of {snapshot.total:,}",
        f"Open:              {snapshot.not_cleared + snapshot.other:,}",
        f"Declaration lines: {snapshot.items_cleared:,} of {snapshot.items_total:,}",
    ]
    if snapshot.is_complete and snapshot.last_clearance:
        lines.append(f"Finished:          {_stamp(snapshot.last_clearance)}")
    elif next_run_at:
        lines.append(f"Next check:        {_hhmm(next_run_at)}")

    if diff and diff.newly_cleared:
        lines += ["", f"Since the last check: {len(diff.newly_cleared):,} newly cleared."]
    if snapshot.other:
        lines += [
            "",
            f"WARNING: {snapshot.other:,} shipment(s) carry an unrecognised status "
            f"({snapshot.unknown_statuses}). Counted as not cleared.",
        ]
    if not snapshot.is_complete and snapshot.open_rows:
        lines += [
            "",
            f"{len(snapshot.open_rows):,} shipments still open. See the attached "
            "OPEN...xlsx, oldest first.",
        ]
    lines += ["", f"Data generated by PortGround at {_hhmm(snapshot.generated_at)}."]
    return "\n".join(lines)


class EmailNotifier:
    """Sends the update by SMTP. Never raises into the polling cycle."""

    def __init__(self, settings) -> None:  # noqa: ANN001 - avoids a circular import
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return self.settings.email_enabled

    def recipients_for(self, job) -> list[str]:  # noqa: ANN001
        """Job-specific addresses plus the standing list, order preserved.

        Addresses outside the allowed domains are dropped here as well as at
        the point they were typed: the two checks guard different mistakes,
        one a person's typo and one a job created before the policy existed.
        """
        seen: list[str] = []
        for address in list(job.email_recipients) + self.settings.email_always_recipients:
            if address.lower() in {a.lower() for a in seen}:
                continue
            if not self.settings.email_allowed(address):
                log.warning(
                    "refusing to mail %s: outside the allowed domains (%s)",
                    address,
                    self.settings.email_allowed_domains,
                )
                continue
            seen.append(address)
        return seen

    def send_update(
        self,
        job,  # noqa: ANN001
        snapshot: Snapshot,
        *,
        diff: SnapshotDiff | None = None,
        next_run_at: datetime | None = None,
        is_final: bool = False,
        attachments: list[Path] | None = None,
    ) -> str | None:
        """Send one update. Returns the Message-ID, or None if nothing was sent."""
        if not self.enabled:
            return None
        recipients = self.recipients_for(job)
        if not recipients:
            return None

        mawb = format_display(snapshot.mawb)
        message = EmailMessage()
        message["Subject"] = f"[AWB] {mawb} — customs clearance"
        message["From"] = formataddr(("LEJ customs tracker", self.settings.email_from))
        message["To"] = ", ".join(recipients)

        message_id = make_msgid(domain=self.settings.email_from.split("@")[-1] or None)
        message["Message-ID"] = message_id
        # Later mails point at the first one, so clients collapse them into a
        # single conversation instead of an inbox full of near-identical mails.
        if job.email_message_id:
            message["In-Reply-To"] = job.email_message_id
            message["References"] = job.email_message_id

        message.set_content(
            render_text(snapshot, diff=diff, next_run_at=next_run_at, is_final=is_final)
        )
        message.add_alternative(
            render_html(snapshot, diff=diff, next_run_at=next_run_at, is_final=is_final),
            subtype="html",
        )
        for path in attachments or []:
            self._attach(message, path)

        return message_id if self._send(message, recipients) else None

    def send_error(self, job, text: str, *, fatal: bool) -> None:  # noqa: ANN001
        if not self.enabled:
            return
        recipients = self.recipients_for(job)
        if not recipients:
            return

        mawb = format_display(job.mawb)
        message = EmailMessage()
        message["Subject"] = f"[AWB] {mawb} — customs clearance"
        message["From"] = formataddr(("LEJ customs tracker", self.settings.email_from))
        message["To"] = ", ".join(recipients)
        message["Message-ID"] = make_msgid(
            domain=self.settings.email_from.split("@")[-1] or None
        )
        if job.email_message_id:
            message["In-Reply-To"] = job.email_message_id
            message["References"] = job.email_message_id

        prefix = "Tracking has stopped" if fatal else "A check failed"
        message.set_content(f"MAWB {mawb}\n\n{prefix}: {text}\n")
        self._send(message, recipients)

    def _attach(self, message: EmailMessage, path: Path) -> None:
        try:
            data = path.read_bytes()
        except OSError as exc:
            log.error("could not attach %s: %s", path, exc)
            return
        guessed, _ = mimetypes.guess_type(path.name)
        maintype, _, subtype = (guessed or "application/octet-stream").partition("/")
        message.add_attachment(
            data, maintype=maintype, subtype=subtype, filename=path.name
        )

    def _send(self, message: EmailMessage, recipients: list[str]) -> bool:
        settings = self.settings
        try:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
                if settings.smtp_starttls:
                    smtp.starttls()
                if settings.smtp_user:
                    smtp.login(settings.smtp_user, settings.smtp_password)
                smtp.send_message(message)
        except (smtplib.SMTPException, OSError) as exc:
            # A mail failure must never take down the polling cycle, and the
            # Slack update has already gone out with the same information.
            log.error("could not send mail to %s: %s", ", ".join(recipients), exc)
            return False
        log.info("emailed %s to %s", message["Subject"], ", ".join(recipients))
        return True
