"""The inbound email trigger.

Almost all of this is about *refusing*. A mailbox is an open door: anyone
who can forge a From header can knock, and what comes back out carries
invoice numbers and MRNs. The happy path is four tests; everything else
here is a way in that has to stay shut.
"""

from __future__ import annotations

from datetime import timedelta
from email.message import EmailMessage

import pytest

from lej_cc.config import Settings
from lej_cc.inbox import (
    EmailTrigger,
    extract_requests,
    parse_message,
    visible_body,
)
from lej_cc.store import JobStore, utcnow

SENDER = "miroslav@qtlogistics.eu"


def raw_mail(
    *,
    sender: str = SENDER,
    subject: str = "488-20744846",
    body: str = "Please check this one.",
    message_id: str | None = "<m1@qtlogistics.eu>",
    auth: str | None = "mx.qtlogistics.eu; spf=pass; dkim=pass",
    headers: dict[str, str] | None = None,
    html: str | None = None,
) -> bytes:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "awb@qtlogistics.eu"
    message["Subject"] = subject
    if message_id:
        message["Message-ID"] = message_id
    if auth:
        message["Authentication-Results"] = auth
    for key, value in (headers or {}).items():
        message[key] = value
    message.set_content(body)
    if html:
        message.add_alternative(html, subtype="html")
    return message.as_bytes()


class FakeMailbox:
    """An IMAP server reduced to a list. Records what was filed away."""

    def __init__(self, messages: list[bytes]) -> None:
        self.messages = list(messages)
        self.handled: list[str] = []
        self.closed = False

    def unseen(self, limit: int):
        return [(str(i), raw) for i, raw in enumerate(self.messages)][:limit]

    def mark_handled(self, uid: str) -> None:
        self.handled.append(uid)

    def close(self) -> None:
        self.closed = True


class FakeEmailer:
    enabled = True

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str, str | None]] = []

    def send_notice(self, to, subject, text, *, in_reply_to=None):  # noqa: ANN001
        self.sent.append((to, subject, text, in_reply_to))
        return True


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        slack_bot_token="xoxb-x",
        slack_app_token="xapp-x",
        portground_api_key="k",
        database_path=tmp_path / "jobs.sqlite3",
        download_dir=tmp_path / "downloads",
        smtp_host="smtp.example.com",
        email_from="awb@qtlogistics.eu",
        imap_host="imap.example.com",
        imap_user="awb@qtlogistics.eu",
        imap_allowed_senders="qtlogistics.eu",
        imap_target_channel="C999",
    )


@pytest.fixture
def store(tmp_path) -> JobStore:
    return JobStore(tmp_path / "jobs.sqlite3")


def build(settings, store, messages: list[bytes]):
    mailbox = FakeMailbox(messages)
    emailer = FakeEmailer()
    trigger = EmailTrigger(settings, store, emailer=emailer, mailbox_factory=lambda: mailbox)
    return trigger, mailbox, emailer


# --- switched off by default --------------------------------------------


def test_an_empty_allowlist_disables_the_trigger(settings, store):
    """The one setting that must never fail open."""
    off = settings.model_copy(update={"imap_allowed_senders": ""})
    trigger, mailbox, _ = build(off, store, [raw_mail()])
    assert not trigger.enabled
    assert trigger.poll().accepted == 0
    assert "IMAP_ALLOWED_SENDERS is empty" in (trigger.why_disabled() or "")


def test_no_slack_channel_disables_the_trigger(settings, store):
    off = settings.model_copy(update={"imap_target_channel": "", "slack_status_channel": ""})
    trigger, _, _ = build(off, store, [raw_mail()])
    assert not trigger.enabled
    assert "no Slack channel" in (trigger.why_disabled() or "")


def test_nothing_configured_is_not_a_misconfiguration(store):
    plain = Settings(slack_bot_token="x", slack_app_token="x", portground_api_key="k")
    trigger = EmailTrigger(plain, store)
    assert not trigger.enabled
    assert trigger.why_disabled() is None


# --- the happy path ------------------------------------------------------


def test_an_allowlisted_mail_starts_tracking(settings, store):
    trigger, mailbox, emailer = build(settings, store, [raw_mail()])
    result = trigger.poll()

    assert result.accepted == 1
    job = store.find_active("48820744846", "C999")
    assert job is not None
    assert job.source == "email"
    assert job.email_recipients == [SENDER]
    assert mailbox.handled == ["0"]
    assert mailbox.closed


def test_updates_thread_under_the_mail_that_asked(settings, store):
    """The requester gets one conversation, not a pile of similar mails."""
    trigger, _, _ = build(settings, store, [raw_mail()])
    trigger.poll()
    job = store.find_active("48820744846", "C999")
    assert job.email_message_id == "<m1@qtlogistics.eu>"


def test_the_sender_is_answered_in_their_own_thread(settings, store):
    trigger, _, emailer = build(settings, store, [raw_mail()])
    trigger.poll()

    to, subject, text, in_reply_to = emailer.sent[0]
    assert to == SENDER
    assert subject == "Re: 488-20744846"
    assert "tracking started" in text
    assert in_reply_to == "<m1@qtlogistics.eu>"


def test_a_booking_reference_in_the_subject_works(settings, store):
    trigger, _, _ = build(settings, store, [raw_mail(subject="OyTM202608137666")])
    assert trigger.poll().accepted == 1
    assert store.find_active("OyTM202608137666", "C999") is not None


def test_several_awbs_in_one_mail_all_start(settings, store):
    trigger, _, _ = build(
        settings, store, [raw_mail(subject="488-20744846 and 936-00333955 please")]
    )
    assert trigger.poll().accepted == 2


# --- who is allowed ------------------------------------------------------


@pytest.mark.parametrize(
    "sender",
    [
        "stranger@example.com",
        "attacker@evilqtlogistics.eu",  # merely ends with the same letters
        "",
    ],
)
def test_a_sender_outside_the_allowlist_is_ignored(settings, store, sender):
    trigger, _, emailer = build(settings, store, [raw_mail(sender=sender)])
    result = trigger.poll()

    assert result.accepted == 0
    assert result.refused == 1
    assert store.list_active() == []
    # No bounce: explaining the refusal explains how to get past it.
    assert emailer.sent == []


def test_a_subdomain_of_an_allowed_domain_is_allowed(settings, store):
    trigger, _, _ = build(settings, store, [raw_mail(sender="ops@mail.qtlogistics.eu")])
    assert trigger.poll().accepted == 1


def test_an_exact_address_allowlist_admits_only_that_address(settings, store):
    only = settings.model_copy(update={"imap_allowed_senders": "xin@qtlogistics.eu"})
    trigger, _, _ = build(only, store, [raw_mail(sender="someone.else@qtlogistics.eu")])
    assert trigger.poll().accepted == 0


def test_a_sender_we_may_not_mail_is_refused(settings, store):
    """Accepting a request we cannot answer would drop every update silently."""
    restricted = settings.model_copy(
        update={
            "imap_allowed_senders": "partner.example",
            "email_allowed_domains": "qtlogistics.eu",
        }
    )
    trigger, _, _ = build(restricted, store, [raw_mail(sender="ops@partner.example")])
    assert trigger.poll().accepted == 0


# --- proving the mail is real -------------------------------------------


def test_a_mail_that_failed_spf_is_ignored(settings, store):
    trigger, _, _ = build(
        settings, store, [raw_mail(auth="mx.example; spf=fail; dkim=none")]
    )
    assert trigger.poll().accepted == 0


def test_a_mail_with_no_authentication_header_is_ignored(settings, store):
    trigger, _, _ = build(settings, store, [raw_mail(auth=None)])
    assert trigger.poll().accepted == 0


def test_the_authentication_requirement_can_be_switched_off(settings, store):
    """For a server that does not write the header. Deliberate, not default."""
    lax = settings.model_copy(update={"imap_require_authentication": False})
    trigger, _, _ = build(lax, store, [raw_mail(auth=None)])
    assert trigger.poll().accepted == 1


# --- robots --------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        {"Auto-Submitted": "auto-replied"},
        {"Precedence": "bulk"},
        {"List-Id": "<announce.example.com>"},
        {"X-Autoreply": "yes"},
    ],
)
def test_automated_mail_is_never_answered(settings, store, headers):
    """An out-of-office answering our reply, forever, is the failure here."""
    trigger, _, emailer = build(settings, store, [raw_mail(headers=headers)])
    assert trigger.poll().accepted == 0
    assert emailer.sent == []


def test_our_own_address_cannot_trigger_us(settings, store):
    trigger, _, _ = build(settings, store, [raw_mail(sender="awb@qtlogistics.eu")])
    assert trigger.poll().accepted == 0


# --- replay and volume ---------------------------------------------------


def test_the_same_message_id_is_only_acted_on_once(settings, store):
    trigger, _, _ = build(settings, store, [raw_mail(), raw_mail()])
    result = trigger.poll()
    assert result.accepted == 1
    assert result.skipped == 1


def test_a_message_without_an_id_still_deduplicates(settings, store):
    """Some senders omit Message-ID; the mail must not replay forever."""
    mail = raw_mail(message_id=None)
    first, _, _ = build(settings, store, [mail])
    first.poll()
    store.finish(store.find_active("48820744846", "C999").id, "stopped", "test")

    second, _, _ = build(settings, store, [mail])
    assert second.poll().skipped == 1


def test_a_sender_is_rate_limited(settings, store):
    capped = settings.model_copy(update={"imap_max_per_sender_hourly": 2})
    messages = [
        raw_mail(subject=s, message_id=f"<m{i}@x>")
        for i, s in enumerate(["488-20744846", "936-00333955", "938-12345675"])
    ]
    trigger, _, _ = build(capped, store, messages)
    result = trigger.poll()

    assert result.accepted == 2
    assert result.refused == 1


def test_refusals_do_not_eat_the_rate_limit(settings, store):
    """Otherwise one noisy hour locks out a legitimate sender who keeps writing."""
    capped = settings.model_copy(update={"imap_max_per_sender_hourly": 1})
    store.record_email("<old1@x>", SENDER, "no-awb")
    store.record_email("<old2@x>", SENDER, "sender-not-allowed")

    trigger, _, _ = build(capped, store, [raw_mail()])
    assert trigger.poll().accepted == 1


def test_the_rate_limit_window_is_an_hour(settings, store):
    capped = settings.model_copy(update={"imap_max_per_sender_hourly": 1})
    store._conn.execute(
        "INSERT INTO processed_emails (message_id, sender, seen_at, outcome)"
        " VALUES (?,?,?,?)",
        ("<yesterday@x>", SENDER, (utcnow() - timedelta(hours=25)).isoformat(), "tracked"),
    )
    trigger, _, _ = build(capped, store, [raw_mail()])
    assert trigger.poll().accepted == 1


# --- finding the number --------------------------------------------------


def test_a_quoted_reply_does_not_retrigger_the_thread():
    """Answering "thanks" to an update must not restart everything below it."""
    body = (
        "Thanks!\n"
        "\n"
        "On Tue, 9 Sep 2026 at 14:05, LEJ customs tracker wrote:\n"
        "> MAWB 936-00333955 - 100% cleared\n"
    )
    assert extract_requests("Re: thanks", body) == []


def test_quoted_lines_are_cut_off():
    assert visible_body("keep\n> dropped\nalso dropped") == "keep"


def test_an_awb_in_the_body_is_found():
    assert extract_requests("clearance status", "Could you check 488-20744846 today?") == [
        "48820744846"
    ]


def test_a_reference_is_only_taken_from_the_subject():
    """No checksum to lean on, so a body full of order numbers stays out."""
    assert extract_requests("customs", "our reference is OyTM202608137666") == []
    assert extract_requests("OyTM202608137666", "") == ["OyTM202608137666"]


def test_ordinary_subject_words_are_not_references():
    assert extract_requests("Please check clearance status urgently", "") == []


def test_a_valid_awb_wins_over_a_reference_in_the_same_subject():
    assert extract_requests("OyTM202608137666 / 488-20744846", "") == ["48820744846"]


def test_one_mail_cannot_queue_unlimited_work():
    subject = " ".join(f"REF2026{i:08d}" for i in range(40))
    assert len(extract_requests(subject, "")) == 10


def test_html_only_mail_is_readable():
    mail = parse_message("1", raw_mail(body="", html="<p>Check <b>488-20744846</b></p>"))
    assert extract_requests(mail.subject, mail.body) == ["48820744846"]


def test_a_mail_with_no_number_gets_told_so(settings, store):
    trigger, _, emailer = build(settings, store, [raw_mail(subject="hello", body="hi")])
    result = trigger.poll()

    assert result.accepted == 0
    assert "No AWB number" in emailer.sent[0][2]


def test_a_mistyped_awb_is_explained_not_tracked(settings, store):
    trigger, _, emailer = build(settings, store, [raw_mail(subject="488-20744840")])
    assert trigger.poll().accepted == 0
    assert "check-digit" in emailer.sent[0][2]
    assert "`" not in emailer.sent[0][2]  # Slack markup does not belong in mail


def test_an_awb_already_tracked_is_reported_not_duplicated(settings, store):
    store.create_job("48820744846", "C999", None)
    trigger, _, emailer = build(settings, store, [raw_mail()])
    result = trigger.poll()

    assert result.accepted == 0
    assert "already being tracked" in emailer.sent[0][2]


# --- the poll survives things --------------------------------------------


def test_a_broken_message_does_not_stop_the_others(settings, store):
    trigger, mailbox, _ = build(settings, store, [b"\xff\xfe not a mail at all", raw_mail()])
    result = trigger.poll()

    assert result.accepted == 1
    assert mailbox.handled == ["0", "1"]  # both filed away, neither replayed


def test_a_mailbox_that_will_not_open_is_survivable(settings, store):
    def explode():
        raise OSError("connection refused")

    trigger = EmailTrigger(settings, store, mailbox_factory=explode)
    assert trigger.poll().accepted == 0


def test_the_mailbox_is_closed_even_when_a_fetch_fails(settings, store):
    mailbox = FakeMailbox([])

    def fail(limit):
        raise OSError("dropped")

    mailbox.unseen = fail  # type: ignore[assignment]
    trigger = EmailTrigger(settings, store, mailbox_factory=lambda: mailbox)
    trigger.poll()
    assert mailbox.closed
