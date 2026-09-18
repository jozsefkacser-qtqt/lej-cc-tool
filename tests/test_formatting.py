"""The status card is read on a phone, at a glance, by someone who wants one
number. These cover the parts where getting it subtly wrong misleads."""

from __future__ import annotations

from lej_cc.formatting import (
    BAR_COLOURS,
    BAR_WIDTH,
    bar_colour,
    build_status_blocks,
    progress_bar,
)
from lej_cc.model import ClearanceStatus, ShipmentRow, Snapshot


def snap(cleared: int, total: int) -> Snapshot:
    rows = [
        ShipmentRow(
            hawb=f"0034043{i:013d}",
            mawb="93602927993",
            status=ClearanceStatus.CLEARED if i < cleared else ClearanceStatus.NOT_CLEARED,
            final_status_raw="cleared" if i < cleared else "not cleared",
            items=1,
        )
        for i in range(total)
    ]
    return Snapshot(mawb="93602927993", rows=rows)


def test_bar_is_never_full_unless_actually_complete():
    """97.8% rounding to a full bar would say 'done' about 34 stuck shipments."""
    assert progress_bar(97.8).count("⬜") == 1
    assert progress_bar(99.99).count("⬜") == 1
    assert progress_bar(100.0) == "🟩" * BAR_WIDTH


def test_any_progress_at_all_is_visible():
    assert progress_bar(0.0).count("⬜") == BAR_WIDTH
    assert progress_bar(0.5).count("⬜") == BAR_WIDTH - 1


def test_colour_reflects_state():
    assert bar_colour(97.8) == "🟩"
    assert bar_colour(60.0) == "🟨"
    assert bar_colour(2.0) == "🟧"


def test_no_progress_colour_is_red():
    """Red means one thing: customs has taken the shipment. A barely-started
    AWB and a fully-inspected one used to render identically."""
    assert "🟥" not in {colour for _, colour in BAR_COLOURS}


def test_card_leads_with_the_state_then_the_number():
    """What someone scanning the channel wants first is whether it is done;
    the number says which AWB it is."""
    blocks = build_status_blocks(snap(1544, 1578))
    header = blocks[0]["text"]["text"]

    assert blocks[0]["type"] == "header"
    assert header.startswith("📦 CC In progress")
    assert "936-02927993" in header
    assert "97.8%" in blocks[2]["text"]["text"]


def test_a_finished_awb_says_so_in_the_header():
    assert build_status_blocks(snap(10, 10))[0]["text"]["text"].startswith("✅ CC Finished")


def test_a_stopped_awb_is_not_called_finished():
    blocks = build_status_blocks(snap(3, 10), is_final=True)
    assert blocks[0]["text"]["text"].startswith("⚠️ CC Stopped")


def test_completed_card_drops_the_buttons_and_the_open_list():
    blocks = build_status_blocks(snap(10, 10))
    types = [b["type"] for b in blocks]
    assert "actions" not in types
    assert "CC Finished" in blocks[0]["text"]["text"]


def test_open_count_includes_unrecognised_statuses():
    """An unknown status is open work, not a rounding error."""
    rows = [
        ShipmentRow(hawb="a", mawb="x", status=ClearanceStatus.CLEARED, items=1),
        ShipmentRow(hawb="b", mawb="x", status=ClearanceStatus.NOT_CLEARED, items=1),
        ShipmentRow(hawb="c", mawb="x", status=ClearanceStatus.OTHER, items=1),
    ]
    blocks = build_status_blocks(Snapshot(mawb="93602927993", rows=rows))
    fields = next(b for b in blocks if b.get("fields"))["fields"]
    assert "*⏳ Open*\n2" in [f["text"] for f in fields]


def test_finished_awb_says_when_it_finished():
    """Looking up an old AWB, "cleared" answers the wrong question. The
    useful answer is cleared, on this date, this long ago."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Europe/Berlin")
    cleared_at = datetime(2026, 5, 20, 14, 38, tzinfo=tz)
    now = datetime(2026, 9, 9, 13, 24, tzinfo=tz)

    rows = [
        ShipmentRow(
            hawb=f"h{i}",
            mawb="48820744846",
            status=ClearanceStatus.CLEARED,
            clearance_time=cleared_at - timedelta(hours=i),
            items=1,
        )
        for i in range(3)
    ]
    snapshot = Snapshot(mawb="48820744846", rows=rows, fetched_at=now)

    blocks = build_status_blocks(snapshot, is_final=True)
    rendered = str(blocks)
    assert "20 May 2026 14:38" in rendered
    assert "111d" in rendered and "ago" in rendered  # finished this long ago
    assert "cleared over 2h" in rendered  # first to last clearance


def test_in_flight_awb_shows_tracking_time_not_a_finish_stamp():
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Europe/Berlin")
    now = datetime(2026, 9, 9, 13, 24, tzinfo=tz)
    snapshot = snap(5, 10)
    snapshot.fetched_at = now

    rendered = str(build_status_blocks(snapshot, tracking_since=now - timedelta(hours=2)))
    assert "Tracking" in rendered and "🏁" not in rendered


def test_reference_card_shows_what_it_resolved_to():
    snapshot = snap(5, 10)
    snapshot.mawb = "OyTM202608137666"
    snapshot.resolved_mawbs = ["48820744846"]

    blocks = build_status_blocks(snapshot)
    assert "OyTM202608137666" in blocks[0]["text"]["text"]
    assert "488-20744846" in str(blocks)  # the waybill it turned out to be


def test_many_resolved_mawbs_are_truncated():
    snapshot = snap(5, 10)
    snapshot.resolved_mawbs = [f"4882074484{i}" for i in range(6)]
    assert "+3 more" in str(build_status_blocks(snapshot))


# --- /awb status --------------------------------------------------------


def _health(**overrides):
    from datetime import timedelta

    from lej_cc.health import Health
    from lej_cc.store import utcnow

    health = Health()
    health.started_at = utcnow() - timedelta(hours=4)
    health.last_poll_ok_at = utcnow() - timedelta(minutes=8)
    health.polls_ok = 41
    for key, value in overrides.items():
        setattr(health, key, value)
    return health


def _settings(tmp_path):
    from lej_cc.config import Settings

    return Settings(
        slack_bot_token="x",
        slack_app_token="x",
        portground_api_key="k",
        database_path=tmp_path / "j.sqlite3",
    )


def test_status_says_it_is_online_and_for_how_long(tmp_path):
    from lej_cc.formatting import build_status_report

    text = build_status_report(_health(), [], _settings(tmp_path))[0]["text"]["text"]
    assert "online" in text
    assert "4h 00m" in text
    assert "🟢" in text


def test_status_turns_amber_when_tracked_awbs_are_not_being_polled(tmp_path):
    """Running but not working is a different state from running."""
    from datetime import timedelta

    from lej_cc.formatting import build_status_report
    from lej_cc.store import JobStore, utcnow

    store = JobStore(tmp_path / "j.sqlite3")
    store.create_job("93602927993", "C1", "U1")

    stale = _health(last_poll_ok_at=utcnow() - timedelta(hours=3))
    text = build_status_report(stale, store.list_active(), _settings(tmp_path))[0]["text"][
        "text"
    ]
    assert "🟠" in text


def test_status_is_green_when_there_is_simply_nothing_to_track(tmp_path):
    """Nothing to poll is not a failure to poll. A freshly started bot with
    no AWBs showed amber, which reads as a fault when nothing is wrong."""
    from lej_cc.formatting import build_status_report
    from lej_cc.health import Health

    fresh = Health()  # never polled, just started
    text = build_status_report(fresh, [], _settings(tmp_path))[0]["text"]["text"]
    assert "🟢" in text


def test_a_just_started_bot_is_green_even_with_awbs_pending(tmp_path):
    """It has not had time to poll yet."""
    from lej_cc.formatting import build_status_report
    from lej_cc.health import Health
    from lej_cc.store import JobStore

    store = JobStore(tmp_path / "j.sqlite3")
    store.create_job("93602927993", "C1", "U1")

    text = build_status_report(Health(), store.list_active(), _settings(tmp_path))[0][
        "text"
    ]["text"]
    assert "🟢" in text


def test_status_never_rounds_a_stuck_awb_up_to_100(tmp_path):
    """The same trap as the progress bar: 99.9% is not finished."""
    from lej_cc.formatting import build_status_report
    from lej_cc.store import JobStore, utcnow

    store = JobStore(tmp_path / "j.sqlite3")
    job = store.create_job("93602927993", "C1", "U1")
    store.reschedule(job.id, utcnow(), percent=99.9, cleared=1544, total=1546)

    text = build_status_report(_health(), store.list_active(), _settings(tmp_path))[0][
        "text"
    ]["text"]
    assert "99.9%" in text
    assert "100%" not in text


def test_status_surfaces_the_last_error(tmp_path):
    from lej_cc.formatting import build_status_report
    from lej_cc.store import utcnow

    health = _health(last_poll_error="936-02927971: ApiUnavailable", polls_failed=2)
    health.last_poll_error_at = utcnow()
    text = build_status_report(health, [], _settings(tmp_path))[0]["text"]["text"]
    assert "ApiUnavailable" in text
    assert "41 ok · 2 failed" in text


def test_poll_number_is_not_written_as_a_hash(tmp_path):
    """Slack renders "check #1" as "check 🔒Private channel": a leading # in
    mrkdwn is a channel reference."""
    rendered = str(build_status_blocks(snap(5, 10), poll_count=3))
    assert "poll 3" in rendered
    assert "#3" not in rendered


def test_the_card_shows_the_expected_finish_time():
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    from lej_cc.forecast import Forecast

    tz = ZoneInfo("Europe/Berlin")
    now = datetime(2026, 9, 15, 15, 0, tzinfo=tz)
    snapshot = snap(800, 1000)
    snapshot.fetched_at = now

    forecast = Forecast(eta=now + timedelta(hours=2, minutes=40), per_hour=75, points=5,
                        remaining=200)
    rendered = str(build_status_blocks(snapshot, forecast=forecast))
    assert "Expected done" in rendered
    assert "~17:40" in rendered
    assert "75/h" in rendered


def test_tomorrows_finish_says_tomorrow():
    """"~09:15" about tomorrow morning is a trap."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    from lej_cc.forecast import Forecast

    tz = ZoneInfo("Europe/Berlin")
    now = datetime(2026, 9, 15, 22, 0, tzinfo=tz)
    snapshot = snap(800, 1000)
    snapshot.fetched_at = now

    forecast = Forecast(eta=now + timedelta(hours=11), per_hour=20, points=5, remaining=200)
    assert "tomorrow" in str(build_status_blocks(snapshot, forecast=forecast))


def test_a_finished_awb_shows_no_forecast():
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    from lej_cc.forecast import Forecast

    tz = ZoneInfo("Europe/Berlin")
    now = datetime(2026, 9, 15, 15, 0, tzinfo=tz)
    forecast = Forecast(eta=now + timedelta(hours=1), per_hour=10, points=5, remaining=0)
    assert "Expected done" not in str(build_status_blocks(snap(10, 10), forecast=forecast))
