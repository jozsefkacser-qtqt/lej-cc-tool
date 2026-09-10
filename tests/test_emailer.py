"""Email notifications.

SMTP itself is faked; what matters here is who gets mailed, when, what the
mail says, and that a failure never reaches the polling cycle.
"""

from __future__ import annotations

import smtplib
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from lej_cc.config import Settings
from lej_cc.emailer import EmailNotifier, render_html, render_text
from lej_cc.model import ClearanceStatus, ShipmentRow, Snapshot, SnapshotDiff

TZ = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 9, 9, 14, 5, tzinfo=TZ)


def snap(cleared: int, total: int) -> Snapshot:
    rows = [
        ShipmentRow(
            hawb=f"0034043{i:013d}",
            mawb="93602927993",
            status=ClearanceStatus.CLEARED if i < cleared else ClearanceStatus.NOT_CLEARED,
            final_status_raw="cleared" if i < cleared else "not cleared",
            clearance_time=NOW - timedelta(hours=2) if i < cleared else None,
            items=6,
        )
        for i in range(total)
    ]
    return Snapshot(mawb="93602927993", rows=rows, generated_at=NOW, fetched_at=NOW)


class FakeSMTP:
    """Stands in for smtplib.SMTP; records what would have been sent."""

    sent: list = []
    fail_with: Exception | None = None

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        if FakeSMTP.fail_with:
            raise FakeSMTP.fail_with

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        pass

    def login(self, user, password):
        pass

    def send_message(self, message):
        FakeSMTP.sent.append(message)


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch):
    FakeSMTP.sent = []
    FakeSMTP.fail_with = None
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    return FakeSMTP


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
        email_always_to="ops@qtlogistics.eu",
    )


@pytest.fixture
def job(settings):
    from lej_cc.store import JobStore

    return JobStore(settings.database_path).create_job(
        "93602927993", "C1", "U1", email_to="candy@qtlogistics.eu"
    )


# --- rendering ----------------------------------------------------------


def test_html_leads_with_the_number():
    html = render_html(snap(1544, 1578))
    assert "97.8%" in html
    assert "1,544 of 1,578" in html
    assert "Declaration lines" in html
    assert "<table" in html  # email clients need tables, not flexbox


def test_html_bar_width_matches_the_percentage():
    assert "width:97.80%" in render_html(snap(1544, 1578)).replace(" ", "")
    assert "width:100.00%" in render_html(snap(10, 10)).replace(" ", "")


def test_html_colour_reflects_state():
    assert "#2e7d32" in render_html(snap(1544, 1578))  # green, nearly done
    assert "#c62828" in render_html(snap(1, 100))  # red, barely started


def test_completed_mail_says_when_it_finished():
    html = render_html(snap(10, 10), is_final=True)
    assert "COMPLETE" in html
    assert "Finished" in html


def test_unknown_statuses_are_called_out():
    rows = [ShipmentRow(hawb="a", mawb="x", status=ClearanceStatus.OTHER, items=1)]
    snapshot = Snapshot(mawb="93602927993", rows=rows, unknown_statuses={"blocked": 1})
    assert "do not recognise" in render_html(snapshot)
    assert "WARNING" in render_text(snapshot)


def test_text_alternative_carries_the_same_facts():
    text = render_text(snap(1544, 1578), diff=SnapshotDiff(newly_cleared=["a"] * 412))
    assert "97.8% cleared" in text
    assert "1,544 of 1,578" in text
    assert "412 newly cleared" in text


# --- recipients ---------------------------------------------------------


def test_job_recipients_come_before_the_standing_list(settings, job):
    assert EmailNotifier(settings).recipients_for(job) == [
        "candy@qtlogistics.eu",
        "ops@qtlogistics.eu",
    ]


def test_duplicate_recipients_are_collapsed(settings, job):
    settings.email_always_to = "CANDY@qtlogistics.eu, ops@qtlogistics.eu"
    assert EmailNotifier(settings).recipients_for(job) == [
        "candy@qtlogistics.eu",
        "ops@qtlogistics.eu",
    ]


def test_nothing_is_sent_without_recipients(settings, job):
    settings.email_always_to = ""
    job.email_to = None
    assert EmailNotifier(settings).send_update(job, snap(5, 10)) is None
    assert FakeSMTP.sent == []


def test_email_is_off_unless_configured(tmp_path, job):
    off = Settings(
        slack_bot_token="x",
        slack_app_token="x",
        portground_api_key="k",
        database_path=tmp_path / "j.sqlite3",
    )
    assert EmailNotifier(off).enabled is False
    assert EmailNotifier(off).send_update(job, snap(5, 10)) is None
    assert FakeSMTP.sent == []


# --- sending ------------------------------------------------------------


def test_send_produces_a_multipart_mail_with_both_bodies(settings, job):
    EmailNotifier(settings).send_update(job, snap(1544, 1578), next_run_at=NOW)

    assert len(FakeSMTP.sent) == 1
    message = FakeSMTP.sent[0]
    assert message["To"] == "candy@qtlogistics.eu, ops@qtlogistics.eu"
    assert "936-02927993" in message["Subject"]
    types = {part.get_content_type() for part in message.walk()}
    assert {"text/plain", "text/html"} <= types


def test_subject_is_stable_so_clients_thread_the_conversation(settings, job):
    notifier = EmailNotifier(settings)
    notifier.send_update(job, snap(5, 10))
    notifier.send_update(job, snap(10, 10), is_final=True)
    assert FakeSMTP.sent[0]["Subject"] == FakeSMTP.sent[1]["Subject"]


def test_later_mails_reference_the_first(settings, job):
    notifier = EmailNotifier(settings)
    first = notifier.send_update(job, snap(5, 10))
    job.email_message_id = first  # the tracker persists this

    notifier.send_update(job, snap(8, 10))

    assert FakeSMTP.sent[1]["In-Reply-To"] == first
    assert FakeSMTP.sent[1]["References"] == first
    assert FakeSMTP.sent[0]["In-Reply-To"] is None


def test_attachments_are_included(settings, job, tmp_path):
    sheet = tmp_path / "OPEN_936-02927993_34_shipments.xlsx"
    sheet.write_bytes(b"PK\x03\x04fake")
    EmailNotifier(settings).send_update(job, snap(5, 10), attachments=[sheet])

    names = [p.get_filename() for p in FakeSMTP.sent[0].walk() if p.get_filename()]
    assert names == ["OPEN_936-02927993_34_shipments.xlsx"]


def test_a_missing_attachment_does_not_stop_the_mail(settings, job):
    EmailNotifier(settings).send_update(job, snap(5, 10), attachments=[Path("/no/such.xlsx")])
    assert len(FakeSMTP.sent) == 1


def test_smtp_failure_is_swallowed_not_raised(settings, job):
    """Slack has already carried the same update; a mail outage must not
    take down the polling cycle."""
    FakeSMTP.fail_with = smtplib.SMTPServerDisconnected("connection lost")

    assert EmailNotifier(settings).send_update(job, snap(5, 10)) is None
    assert FakeSMTP.sent == []


def test_error_mail_is_sent_and_threaded(settings, job):
    job.email_message_id = "<first@example.com>"
    EmailNotifier(settings).send_error(job, "PortGround doesn't know this MAWB.", fatal=True)

    message = FakeSMTP.sent[0]
    assert "Tracking has stopped" in message.get_content()
    assert message["In-Reply-To"] == "<first@example.com>"


# --- who may receive ----------------------------------------------------
# The mails carry invoice numbers, MRNs and consignee tracking numbers, so a
# mistyped address is a data disclosure rather than a wasted message.


def test_no_allowlist_means_no_restriction(settings, job):
    """Unset stays permissive so switching email on does not silently break;
    the preflight is what nags about it."""
    settings.email_allowed_domains = ""
    assert settings.email_allowed("anyone@anywhere.example") is True


def test_allowlisted_domain_is_accepted(settings):
    settings.email_allowed_domains = "qtlogistics.eu, skyqt.eu"
    assert settings.email_allowed("candy.tang@qtlogistics.eu") is True
    assert settings.email_allowed("miroslav@skyqt.eu") is True


def test_outside_domain_is_refused(settings):
    settings.email_allowed_domains = "qtlogistics.eu"
    assert settings.email_allowed("someone@gmail.com") is False


def test_a_lookalike_domain_does_not_slip_through(settings):
    """endswith() alone would accept this, which is the whole trap."""
    settings.email_allowed_domains = "qtlogistics.eu"
    assert settings.email_allowed("attacker@evilqtlogistics.eu") is False
    assert settings.email_allowed("ops@mail.qtlogistics.eu") is True  # real subdomain


def test_matching_ignores_case_and_a_leading_at(settings):
    settings.email_allowed_domains = "@QTLogistics.EU"
    assert settings.email_allowed("Candy.Tang@qtlogistics.eu") is True


def test_refused_recipients_are_dropped_before_sending(settings, job):
    settings.email_allowed_domains = "qtlogistics.eu"
    job.email_to = "candy@qtlogistics.eu,outsider@gmail.com"

    notifier = EmailNotifier(settings)
    assert notifier.recipients_for(job) == ["candy@qtlogistics.eu", "ops@qtlogistics.eu"]

    notifier.send_update(job, snap(5, 10))
    assert "outsider@gmail.com" not in FakeSMTP.sent[0]["To"]


def test_the_standing_list_is_filtered_too(settings, job):
    """A domain policy added after EMAIL_ALWAYS_TO was set must still apply."""
    settings.email_allowed_domains = "qtlogistics.eu"
    settings.email_always_to = "partner@example.com"
    job.email_to = None
    assert EmailNotifier(settings).recipients_for(job) == []


def test_nothing_is_sent_when_every_recipient_is_refused(settings, job):
    settings.email_allowed_domains = "qtlogistics.eu"
    settings.email_always_to = ""
    job.email_to = "outsider@gmail.com"

    assert EmailNotifier(settings).send_update(job, snap(5, 10)) is None
    assert FakeSMTP.sent == []
