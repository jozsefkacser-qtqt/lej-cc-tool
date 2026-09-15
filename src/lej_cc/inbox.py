"""Inbound email trigger: start a check by mailing a shared mailbox.

Why IMAP polling and not something cleverer. Gmail push needs a Cloud
project, a Pub/Sub topic and a public endpoint to receive on; inbound SMTP
needs an MX record and a port open to the internet. Polling a mailbox needs
a username and a password, works from behind the corporate firewall exactly
like the Slack side does, and fails safe -- if the poller is down, the mail
sits in the mailbox and is picked up when it comes back.

The security posture, which is the whole of the interesting design here:

* **The sender allowlist is mandatory.** An empty allowlist disables the
  trigger; there is no default-open mode. From addresses are trivially
  forged, and a reply from this bot carries invoice numbers, MRNs and
  consignee tracking numbers.
* **SPF/DKIM must have passed**, read from the `Authentication-Results`
  header the receiving server wrote. A message with no such header is
  refused too, unless the operator turns the requirement off because their
  server does not add one.
* **Replies only ever go to the verified envelope sender**, never to
  `Reply-To` and never to an address quoted in the body. An attacker who
  gets a message through must not be able to redirect the answer.
* **Deduplicated on `Message-ID`**, so a mailbox re-delivering or an
  operator moving mail back cannot start the same job twice.
* **Rate limited per sender**, because each accepted mail costs a
  multi-minute PortGround export.
* **Never answers a robot.** Auto-replies, list mail and our own address are
  skipped, and every mail this bot sends is marked auto-generated, so an
  out-of-office cannot start a loop.
"""

from __future__ import annotations

import email
import hashlib
import imaplib
import logging
import re
from dataclasses import dataclass, field
from datetime import timedelta
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parseaddr
from typing import Protocol

from .awb import AWB_PATTERN, REFERENCE_PATTERN, extract_all, format_display, normalize
from .config import Settings
from .errors import AwbChecksumFailed, InvalidAwbFormat
from .store import JobStore, utcnow

log = logging.getLogger(__name__)

#: Most identifiers one mail may start. A forwarded digest can mention
#: dozens; each one is a multi-minute export, so the tail is dropped and
#: said so rather than quietly queued.
MAX_PER_EMAIL = 10

#: Most messages fetched in one poll, so a mailbox that filled up over a
#: weekend is worked through in batches instead of in one long stall.
MAX_PER_POLL = 25

#: A reference has to look like one: letters *and* digits, and long enough
#: that ordinary words cannot reach it.
_MIN_REFERENCE = 8

_QUOTE_MARKERS = (
    re.compile(r"^\s*-{2,}\s*original message\s*-{2,}", re.I),
    re.compile(r"^\s*_{5,}\s*$"),
    re.compile(r"^\s*from:\s", re.I),
    re.compile(r"^\s*von:\s", re.I),
    re.compile(r"^\s*on .{4,80}\bwrote:\s*$", re.I),
    re.compile(r"^\s*am .{4,80}\bschrieb\b.*:\s*$", re.I),
    re.compile(r"^\s*sent from my ", re.I),
)

_SUBJECT_PREFIX = re.compile(r"^\s*(?:re|fw|fwd|aw|wg|antw)\s*(?:\[\d+\])?\s*:\s*", re.I)


@dataclass
class InboundEmail:
    """One message, reduced to the parts that decide what happens to it."""

    uid: str
    message_id: str
    sender: str
    subject: str
    body: str
    #: True if SPF or DKIM passed, False if either explicitly failed, None if
    #: the server wrote no Authentication-Results header at all.
    authenticated: bool | None = None
    automated: bool = False


@dataclass
class PollResult:
    accepted: int = 0
    refused: int = 0
    skipped: int = 0
    notes: list[str] = field(default_factory=list)


# --- parsing -------------------------------------------------------------


def _decode(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except (UnicodeDecodeError, LookupError, ValueError):
        return raw


def body_text(message: Message) -> str:
    """The plain-text body. HTML-only mail is de-tagged crudely on purpose.

    Anything scraped out of a body is checksum-validated before it becomes a
    job, so a sloppy strip cannot do worse than find nothing.
    """
    if message.is_multipart():
        plain: list[str] = []
        html: list[str] = []
        for part in message.walk():
            if part.get_content_maintype() != "text":
                continue
            if str(part.get("Content-Disposition") or "").startswith("attachment"):
                continue
            if part.get_content_subtype() == "plain":
                plain.append(_payload(part))
            elif part.get_content_subtype() == "html":
                html.append(_payload(part))
        if plain:
            return "\n".join(plain)
        return _strip_html("\n".join(html))
    text = _payload(message)
    return _strip_html(text) if message.get_content_subtype() == "html" else text


def _payload(part: Message) -> str:
    try:
        raw = part.get_payload(decode=True)
    except Exception:  # noqa: BLE001 - a malformed part must not stop the poll
        return ""
    if not isinstance(raw, bytes):
        return str(part.get_payload() or "")
    charset = part.get_content_charset() or "utf-8"
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _strip_html(text: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"[ \t]+", " ", text)


def visible_body(text: str) -> str:
    """Drop the quoted history from a reply.

    Without this, answering "thanks" to an update re-triggers every AWB
    quoted underneath it -- and a long thread re-triggers all of them, every
    time anyone replies.
    """
    lines: list[str] = []
    for line in (text or "").splitlines():
        if line.lstrip().startswith(">"):
            break
        if any(marker.search(line) for marker in _QUOTE_MARKERS):
            break
        lines.append(line)
    return "\n".join(lines)


def _is_automated(message: Message) -> bool:
    auto = (message.get("Auto-Submitted") or "").strip().lower()
    if auto and auto != "no":
        return True
    precedence = (message.get("Precedence") or "").strip().lower()
    if precedence in {"bulk", "list", "junk", "auto_reply"}:
        return True
    return bool(
        message.get("List-Id")
        or message.get("List-Unsubscribe")
        or message.get("X-Autoreply")
        or message.get("X-Autorespond")
    )


def _authentication(message: Message) -> bool | None:
    """Read SPF/DKIM from Authentication-Results. None if absent."""
    headers = message.get_all("Authentication-Results") or []
    if not headers:
        return None
    joined = " ".join(headers).lower()
    if re.search(r"\bdkim=pass\b", joined) or re.search(r"\bspf=pass\b", joined):
        return True
    if re.search(r"\b(?:dkim|spf|dmarc)=(?:fail|softfail|permerror|temperror)\b", joined):
        return False
    # Present but says neither -- "none" for a domain that publishes nothing.
    return False


def parse_message(uid: str, raw: bytes) -> InboundEmail:
    message = email.message_from_bytes(raw)
    sender = parseaddr(message.get("From") or "")[1].strip().lower()
    subject = _decode(message.get("Subject"))
    message_id = (message.get("Message-ID") or "").strip()
    if not message_id:
        # Some senders omit it. A digest of the identifying headers keeps
        # deduplication working rather than letting the mail replay forever.
        digest = hashlib.sha256(
            "|".join([sender, subject, message.get("Date") or "", uid]).encode()
        ).hexdigest()[:32]
        message_id = f"<no-id-{digest}@lej-cc-tool>"
    return InboundEmail(
        uid=uid,
        message_id=message_id,
        sender=sender,
        subject=subject,
        body=body_text(message),
        authenticated=_authentication(message),
        automated=_is_automated(message),
    )


# --- what the mail is asking for -----------------------------------------


def extract_requests(subject: str, body: str) -> list[str]:
    """Identifiers to track, most explicit first.

    A checksum-valid MAWB is safe to pick out of anything, so subject and
    body are both scanned for those. Everything else is only read out of the
    **subject**, where writing it is a deliberate act:

    * an AWB-shaped number that fails the check digit, so the sender is told
      they have a typo rather than that nothing was found -- the answer they
      would get in Slack for the same number;
    * a booking reference, which has no checksum at all.

    That keeps a signature, a file name or an order number in a body from
    becoming a tracking job nobody asked for.
    """
    visible = visible_body(body)
    found = extract_all(f"{subject}\n{visible}")
    if found:
        return found[:MAX_PER_EMAIL]

    cleaned = _SUBJECT_PREFIX.sub("", subject or "")
    candidates: list[str] = [
        "".join(match.groups()) for match in AWB_PATTERN.finditer(cleaned)
    ]
    for token in re.split(r"[\s,;/|()\[\]<>]+", cleaned):
        token = token.strip().strip(".:")
        if len(token) < _MIN_REFERENCE or not REFERENCE_PATTERN.fullmatch(token):
            continue
        if not (any(c.isdigit() for c in token) and any(c.isalpha() for c in token)):
            continue
        if token not in candidates:
            candidates.append(token)
    return candidates[:MAX_PER_EMAIL]


# --- the mailbox ---------------------------------------------------------


class Mailbox(Protocol):
    """Just enough of IMAP to be faked in a test."""

    def unseen(self, limit: int) -> list[tuple[str, bytes]]: ...

    def mark_handled(self, uid: str) -> None: ...

    def close(self) -> None: ...


class ImapMailbox:
    """A real IMAP connection, opened per poll and closed after it.

    Reconnecting each minute costs one TLS handshake and removes every bug
    class that comes from a socket left open for weeks on a laptop that
    sleeps -- which is the machine this actually runs on.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._conn = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port)
        self._conn.login(settings.imap_user, settings.imap_password)
        self._conn.select(settings.imap_folder)

    def unseen(self, limit: int) -> list[tuple[str, bytes]]:
        typ, data = self._conn.uid("SEARCH", None, "UNSEEN")  # type: ignore[arg-type]
        if typ != "OK" or not data or not data[0]:
            return []
        messages: list[tuple[str, bytes]] = []
        for uid in data[0].split()[:limit]:
            # PEEK, so a message is only marked read once it is dealt with:
            # a crash mid-poll leaves it to be picked up next time.
            typ, payload = self._conn.uid("FETCH", uid, "(BODY.PEEK[])")
            if typ != "OK" or not payload or not isinstance(payload[0], tuple):
                log.warning("could not fetch message %s", uid.decode())
                continue
            messages.append((uid.decode(), payload[0][1]))
        return messages

    def mark_handled(self, uid: str) -> None:
        self._conn.uid("STORE", uid, "+FLAGS", "(\\Seen)")
        folder = self.settings.imap_processed_folder
        if not folder:
            return
        typ, _ = self._conn.uid("MOVE", uid, folder)
        if typ == "OK":
            return
        # RFC 6851 MOVE is not universal; copy-then-delete is.
        typ, _ = self._conn.uid("COPY", uid, folder)
        if typ != "OK":
            log.warning("could not file message %s into %r", uid, folder)
            return
        self._conn.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
        self._conn.expunge()

    def close(self) -> None:
        try:
            self._conn.logout()
        except Exception:  # noqa: BLE001 - closing must never raise
            pass


# --- the trigger ---------------------------------------------------------


def why_disabled(settings: Settings) -> str | None:
    """Why a configured mailbox is not being polled, or None if it is.

    Separate from the trigger itself so the preflight can ask without
    opening a database: a mailbox that logs in fine but whose mail is
    refused or unanswerable is the failure that is hardest to notice.
    """
    if not settings.imap_host or not settings.imap_user:
        return None  # deliberately off
    if not settings.allowed_senders:
        return (
            "email trigger is off: IMAP_ALLOWED_SENDERS is empty. Anyone who "
            "can spoof a From address could otherwise start checks and be "
            "mailed customs data."
        )
    if not settings.email_target_channel:
        return (
            "email trigger is off: no Slack channel to post to. Set "
            "IMAP_TARGET_CHANNEL or SLACK_STATUS_CHANNEL."
        )
    return None


class EmailTrigger:
    """Turns allowlisted mail into tracking jobs."""

    def __init__(
        self,
        settings: Settings,
        store: JobStore,
        emailer=None,  # noqa: ANN001 - EmailNotifier; avoids a circular import
        mailbox_factory=None,  # noqa: ANN001
    ) -> None:
        self.settings = settings
        self.store = store
        self.emailer = emailer
        self._open_mailbox = mailbox_factory or (lambda: ImapMailbox(settings))

    @property
    def enabled(self) -> bool:
        return self.settings.imap_enabled and bool(self.settings.email_target_channel)

    def why_disabled(self) -> str | None:
        """A sentence for the log, or None when the trigger is usable."""
        return why_disabled(self.settings)

    def poll(self) -> PollResult:
        result = PollResult()
        if not self.enabled:
            return result
        try:
            mailbox = self._open_mailbox()
        except (imaplib.IMAP4.error, OSError) as exc:
            log.error("could not open the mailbox: %s", exc)
            return result
        try:
            messages = mailbox.unseen(MAX_PER_POLL)
            for uid, raw in messages:
                try:
                    self._handle(parse_message(uid, raw), result)
                except Exception:  # noqa: BLE001 - one bad mail, not the poll
                    log.exception("failed to handle message %s", uid)
                    result.skipped += 1
                mailbox.mark_handled(uid)
        except (imaplib.IMAP4.error, OSError) as exc:
            log.error("mailbox poll failed: %s", exc)
        finally:
            mailbox.close()
        return result

    # -- one message ------------------------------------------------------

    def _handle(self, mail: InboundEmail, result: PollResult) -> None:
        if self.store.email_already_handled(mail.message_id):
            result.skipped += 1
            return

        refusal = self._refuse(mail)
        if refusal:
            log.info("ignoring mail from %s: %s", mail.sender or "(no sender)", refusal)
            self.store.record_email(mail.message_id, mail.sender, refusal)
            result.refused += 1
            # No bounce. Telling an unverified sender why they were refused
            # tells them how to get through, and mailing an address that
            # never wrote to us makes this bot a spam relay.
            return

        requests = extract_requests(mail.subject, mail.body)
        if not requests:
            self.store.record_email(mail.message_id, mail.sender, "no-awb")
            result.refused += 1
            self._reply(
                mail,
                "No AWB number or booking reference was found in your message.\n\n"
                "Put the number in the subject line, for example:\n"
                "  488-20744846\n"
                "  OyTM202608137666\n",
            )
            return

        lines, started = self._start(requests, mail)
        self.store.record_email(
            mail.message_id, mail.sender, "tracked" if started else "nothing-started"
        )
        result.accepted += started
        if not started:
            result.refused += 1
        self._reply(mail, "\n".join(lines))

    def _refuse(self, mail: InboundEmail) -> str | None:
        """Why this mail is not acted on, or None to go ahead."""
        settings = self.settings
        if not mail.sender:
            return "no-sender"
        if settings.email_from and mail.sender == settings.email_from.strip().lower():
            return "own-mail"
        if mail.automated:
            return "automated"
        if not settings.sender_allowed(mail.sender):
            return "sender-not-allowed"
        if not settings.email_allowed(mail.sender):
            # Accepting a request we are not permitted to answer would start
            # a job whose every update is dropped on the way out.
            return "cannot-reply"
        if settings.imap_require_authentication and mail.authenticated is not True:
            return "spf-dkim-not-passed" if mail.authenticated is False else "unauthenticated"
        recent = self.store.emails_from(
            mail.sender, utcnow() - timedelta(hours=1), outcomes=("tracked",)
        )
        if recent >= settings.imap_max_per_sender_hourly:
            return "rate-limited"
        return None

    def _start(self, requests: list[str], mail: InboundEmail) -> tuple[list[str], int]:
        channel = self.settings.email_target_channel
        lines: list[str] = []
        started = 0
        for raw in requests:
            try:
                mawb = normalize(raw)
            except AwbChecksumFailed as exc:
                lines.append(f"{raw}: {_plain(exc.user_message)}")
                continue
            except InvalidAwbFormat as exc:
                lines.append(f"{raw}: {_plain(exc.user_message)}")
                continue

            shown = format_display(mawb)
            existing = self.store.find_active(mawb, channel)
            if existing:
                percent = existing.last_percent
                state = f"{percent:.0f}% cleared" if percent is not None else "first check pending"
                lines.append(f"{shown}: already being tracked ({state}).")
                continue

            job = self.store.create_job(
                mawb,
                channel,
                None,
                run_at=utcnow(),
                email_to=mail.sender,
                source="email",
            )
            if job is None:
                lines.append(f"{shown}: already being tracked.")
                continue
            # Thread every later update under the mail that asked for it, so
            # the whole exchange stays one conversation in the requester's
            # inbox rather than a pile of near-identical mails.
            self.store.set_email_message_id(job.id, mail.message_id)
            log.info(
                "email from %s started tracking %s (job %s)", mail.sender, mawb, job.id
            )
            lines.append(f"{shown}: tracking started, updates follow by email.")
            started += 1

        if len(requests) == MAX_PER_EMAIL:
            lines.append(
                f"(Only the first {MAX_PER_EMAIL} identifiers in the subject were used.)"
            )
        return lines, started

    def _reply(self, mail: InboundEmail, text: str) -> None:
        """Answer the verified sender. Never Reply-To, never the body."""
        if not self.emailer or not self.emailer.enabled:
            return
        if not self.settings.sender_allowed(mail.sender):  # belt and braces
            return
        subject = mail.subject.strip() or "AWB tracking"
        if not _SUBJECT_PREFIX.match(subject):
            subject = f"Re: {subject}"
        self.emailer.send_notice(
            mail.sender,
            subject,
            f"{text}\n\n"
            "The first check runs now; PortGround takes a couple of minutes to\n"
            "build the export. Updates follow after 15 minutes, then every 30\n"
            "until everything is cleared.\n",
            in_reply_to=mail.message_id,
        )


def _plain(text: str) -> str:
    """Slack markup out, plain text in -- these strings are shared."""
    return text.replace("`", "").replace("*", "")
