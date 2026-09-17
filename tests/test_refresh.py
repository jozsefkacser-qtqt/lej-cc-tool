"""The Check now button.

Reported as "when i click refresh nothing happens", and it was true twice
over: the download takes a couple of minutes with no acknowledgement, and a
poll that found no change posted nothing at all. Answering a button press
with silence is indistinguishable from a broken button.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest
from test_tracker import MAWB, FakeClient, FakeNotifier

from lej_cc.config import Settings
from lej_cc.formatting import build_status_blocks
from lej_cc.model import ClearanceStatus, ShipmentRow, Snapshot
from lej_cc.store import JobStore, _iso, utcnow
from lej_cc.tracker import Tracker


@pytest.fixture
def store(tmp_path) -> JobStore:
    return JobStore(tmp_path / "jobs.sqlite3")


# --- asking for a check --------------------------------------------------


def test_a_request_brings_the_next_poll_forward(store):
    job = store.create_job(MAWB, "C1", "U1")
    store.reschedule(job.id, utcnow() + timedelta(minutes=30))

    assert store.request_refresh(job.id) == "queued"

    reloaded = store.get(job.id)
    assert reloaded.next_run_at <= utcnow()
    assert reloaded.force_post is True


def test_a_requested_job_is_claimed_immediately(store):
    """The half-hour wait is the thing being skipped; prove it is skipped."""
    job = store.create_job(MAWB, "C1", "U1")
    store.reschedule(job.id, utcnow() + timedelta(minutes=30))
    assert store.claim_due() == []

    store.request_refresh(job.id)
    assert [j.id for j in store.claim_due()] == [job.id]


def test_a_check_already_running_is_not_disturbed(store):
    """Clearing the lease mid-poll would start a second download of the same
    AWB, and two polls racing means two contradictory updates."""
    job = store.create_job(MAWB, "C1", "U1")
    claimed = store.claim_due()
    assert claimed  # now leased
    before = store.get(job.id).next_run_at

    assert store.request_refresh(job.id) == "already_running"

    reloaded = store.get(job.id)
    assert reloaded.next_run_at == before
    assert store.claim_due() == []  # still nobody else's to take
    # But the poll in flight will now report whatever it finds.
    assert reloaded.force_post is True


def test_a_job_that_finished_says_so(store):
    job = store.create_job(MAWB, "C1", "U1")
    store.finish(job.id, "complete", "done")
    assert store.request_refresh(job.id) == "not_tracked"


def test_an_unknown_job_says_so(store):
    assert store.request_refresh(9999) == "not_tracked"


def test_the_flag_clears_once_the_poll_has_reported(store):
    job = store.create_job(MAWB, "C1", "U1")
    store.request_refresh(job.id)

    store.reschedule(job.id, utcnow() + timedelta(minutes=30))

    assert store.get(job.id).force_post is False


# --- an older database ---------------------------------------------------


def test_an_existing_database_gains_the_column(tmp_path):
    """The bot is running on a database created before this existed."""
    path = tmp_path / "old.sqlite3"
    old = sqlite3.connect(path)
    old.executescript(
        "CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, mawb TEXT NOT NULL,"
        " channel_id TEXT NOT NULL, thread_ts TEXT, requested_by TEXT,"
        " state TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL,"
        " next_run_at TEXT NOT NULL, leased_until TEXT, poll_count INTEGER NOT NULL"
        " DEFAULT 0, empty_polls INTEGER NOT NULL DEFAULT 0, consecutive_failures"
        " INTEGER NOT NULL DEFAULT 0, last_status_map TEXT, last_percent REAL,"
        " last_cleared INTEGER, last_total INTEGER, last_polled_at TEXT,"
        " finished_at TEXT, finish_reason TEXT);"
    )
    old.execute(
        "INSERT INTO jobs (mawb, channel_id, state, created_at, next_run_at)"
        " VALUES (?,?,?,?,?)",
        (MAWB, "C1", "active", _iso(utcnow()), _iso(utcnow())),
    )
    old.commit()
    old.close()

    store = JobStore(path)
    job = store.list_active()[0]

    assert job.force_post is False
    assert store.request_refresh(job.id) == "queued"
    assert store.get(job.id).force_post is True


# --- what the poll then posts -------------------------------------------


def _run(settings, store, job, workbook, notifier):
    tracker = Tracker(settings, store, FakeClient([workbook]), notifier)
    tracker.run_once(store.get(job.id))


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        slack_bot_token="xoxb-t",
        slack_app_token="xapp-t",
        portground_api_key="k",
        database_path=tmp_path / "jobs.sqlite3",
        download_dir=tmp_path / "downloads",
    )


def test_an_unchanged_poll_on_its_own_says_nothing_at_all(settings, store, data_dir):
    """Still right for a scheduled poll: a channel that says nothing means
    nothing is wrong. It is only wrong as the answer to a button press."""
    notifier = FakeNotifier()
    job = store.create_job(MAWB, "C1", "U1")
    _run(settings, store, job, data_dir / "partial.xlsx", notifier)  # first poll
    posts_after_first = len(notifier.posts)

    _run(settings, store, job, data_dir / "partial.xlsx", notifier)  # nothing moved

    assert len(notifier.posts) == posts_after_first


def test_a_talkative_operator_setting_still_gets_the_one_liner(settings, store, data_dir):
    chatty = settings.model_copy(update={"post_unchanged_updates": True})
    notifier = FakeNotifier()
    job = store.create_job(MAWB, "C1", "U1")
    _run(chatty, store, job, data_dir / "partial.xlsx", notifier)
    _run(chatty, store, job, data_dir / "partial.xlsx", notifier)

    assert "No change" in notifier.posts[-1]["text"]


def test_a_requested_check_answers_with_the_full_card(settings, store, data_dir):
    """The whole complaint: a press produced nothing to look at."""
    notifier = FakeNotifier()
    job = store.create_job(MAWB, "C1", "U1")
    _run(settings, store, job, data_dir / "partial.xlsx", notifier)
    before = len(notifier.posts)

    store.request_refresh(job.id)
    _run(settings, store, job, data_dir / "partial.xlsx", notifier)

    assert len(notifier.posts) == before + 1
    answer = notifier.posts[-1]
    assert answer["blocks"], "a requested check must show the card, not a one-liner"
    assert answer["thread_ts"] is not None


def test_a_quiet_operator_setting_cannot_silence_a_button_press(settings, store, data_dir):
    quiet = settings.model_copy(update={"post_unchanged_updates": False})
    notifier = FakeNotifier()
    job = store.create_job(MAWB, "C1", "U1")

    tracker = Tracker(quiet, store, FakeClient([data_dir / "partial.xlsx"]), notifier)
    tracker.run_once(store.get(job.id))
    # Make the second poll identical *and* unreported by default.
    store._conn.execute("UPDATE jobs SET force_post = 1 WHERE id = ?", (job.id,))
    before = len(notifier.posts)

    tracker2 = Tracker(quiet, store, FakeClient([data_dir / "partial.xlsx"]), notifier)
    tracker2.run_once(store.get(job.id))

    assert len(notifier.posts) > before


# --- the button itself ---------------------------------------------------


def _snapshot() -> Snapshot:
    rows = [
        ShipmentRow(hawb=f"h{i}", mawb=MAWB, status=ClearanceStatus.NOT_CLEARED)
        for i in range(3)
    ]
    return Snapshot(mawb=MAWB, rows=rows)


def test_the_check_button_is_green_and_stop_is_red():
    blocks = build_status_blocks(_snapshot())
    actions = next(b for b in blocks if b["type"] == "actions")
    buttons = {b["action_id"]: b for b in actions["elements"]}

    assert buttons["awb_refresh"]["style"] == "primary"
    assert buttons["awb_stop"]["style"] == "danger"


def test_the_button_carries_the_awb_it_belongs_to():
    blocks = build_status_blocks(_snapshot())
    actions = next(b for b in blocks if b["type"] == "actions")
    assert all(e["value"] == MAWB for e in actions["elements"])
