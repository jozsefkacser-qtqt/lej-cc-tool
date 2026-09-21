"""Starting a whole month's AWBs at once.

The cases that matter are the ones that decide whether somebody trusts the
command: a header row must not look like a typo, a typo must not be silently
swallowed, an AWB already being tracked must not be started twice, and forty
starts must not all land on PortGround in the same second.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from lej_cc.bulk import (
    Plan,
    pick_awb_column,
    plan_starts,
    read_from_file,
    start_tracking,
)
from lej_cc.config import Settings
from lej_cc.store import JobStore, utcnow

CHANNEL = "C123"

#: Real shapes from Daily Report_LEJ: a waybill, two booking references, and
#: the header block that sits above them.
MONTH_TAB = [
    ["Legend", "", "", ""],
    ["", "", "", ""],
    ["AWB", "Client", "CC location", "Weight"],
    ["936-02927993", "Radiance Sea", "Arrived to Reg.", "2,543 kg"],
    ["OyTM202608137666", "Radiance Sea", "Arrived to Reg.", "7,563 kg"],
    ["488-20744846", "Radiance Sea", "Arrived to Reg.", "2,404 kg"],
]


@pytest.fixture
def store(tmp_path) -> JobStore:
    return JobStore(tmp_path / "jobs.sqlite3")


def csv_file(tmp_path, rows) -> object:
    path = tmp_path / "month.csv"
    path.write_text("\n".join(",".join(r) for r in rows), encoding="utf-8")
    return path


# --- finding the column -------------------------------------------------


def test_the_awb_column_is_found_by_its_header_not_by_position():
    rows = [["Week", "AWB", "Client"], ["W36", "488-20744846", "x"]]
    assert pick_awb_column(rows) == 1


def test_a_header_far_down_the_sheet_is_still_found():
    """Daily Report_LEJ puts its header on row 9, under a legend block."""
    assert pick_awb_column(MONTH_TAB) == 0


def test_the_first_awb_column_wins_when_there_are_two():
    """The report has an AWB key and a copy in the raw block. Keys are first."""
    rows = [["AWB", "Client", "AWB"], ["488-20744846", "x", "488-20744846"]]
    assert pick_awb_column(rows) == 0


def test_no_header_falls_back_to_the_first_column():
    assert pick_awb_column([["488-20744846"], ["936-02927993"]]) == 0


# --- reading -------------------------------------------------------------


def test_a_csv_month_tab_is_read(tmp_path):
    values = read_from_file(csv_file(tmp_path, MONTH_TAB))
    assert values == ["Legend", "AWB", "936-02927993", "OyTM202608137666", "488-20744846"]


# --- planning ------------------------------------------------------------


def test_headers_and_labels_are_ignored_not_reported_as_typos(store):
    plan = plan_starts(["Legend", "AWB", "", "Client", "W36"], store, CHANNEL)
    assert plan.to_start == []
    assert plan.typos == []
    assert plan.ignored == 5


def test_a_bad_check_digit_is_named_as_a_typo(store):
    """An 11-digit number that fails its checksum is somebody's mistake."""
    plan = plan_starts(["488-20744840"], store, CHANNEL)

    assert plan.to_start == []
    assert len(plan.typos) == 1
    raw, why = plan.typos[0]
    assert raw == "488-20744840"
    assert "check-digit" in why


def test_a_long_number_that_is_not_an_awb_is_a_typo_too(store):
    plan = plan_starts(["4882074484600"], store, CHANNEL)
    assert [raw for raw, _ in plan.typos] == ["4882074484600"]


def test_a_real_month_tab_is_sorted_correctly(store):
    values = [row[0] for row in MONTH_TAB if row[0]]
    plan = plan_starts(values, store, CHANNEL)

    assert plan.to_start == ["93602927993", "OyTM202608137666", "48820744846"]
    assert plan.typos == []
    assert plan.ignored == 2  # "Legend" and "AWB"


def test_an_awb_being_tracked_right_now_is_not_started_again(store):
    store.create_job("48820744846", CHANNEL, "U1")
    plan = plan_starts(["488-20744846", "936-02927993"], store, CHANNEL)

    assert plan.to_start == ["93602927993"]
    assert plan.active == ["48820744846"]


def test_an_awb_that_already_completed_is_never_started_again(store):
    """The whole point: a finished AWB has nothing left to learn, and
    re-downloading it costs a hundred seconds and a duplicate card."""
    job = store.create_job("48820744846", CHANNEL, "U1")
    store.finish(job.id, "complete", "100% cleared")

    plan = plan_starts(["488-20744846"], store, CHANNEL)

    assert plan.to_start == []
    assert plan.settled == ["48820744846"]


def test_a_completed_awb_is_not_started_even_with_retry(store):
    job = store.create_job("48820744846", CHANNEL, "U1")
    store.finish(job.id, "complete", "100% cleared")

    assert plan_starts(["488-20744846"], store, CHANNEL, retry=True).to_start == []


@pytest.mark.parametrize("state", ["timeout", "stopped", "failed", "not_found"])
def test_an_unfinished_awb_waits_for_retry(store, state):
    job = store.create_job("48820744846", CHANNEL, "U1")
    store.finish(job.id, state, "whatever")

    plan = plan_starts(["488-20744846"], store, CHANNEL)
    assert plan.to_start == []
    assert plan.unfinished == [("48820744846", state)]

    retried = plan_starts(["488-20744846"], store, CHANNEL, retry=True)
    assert retried.to_start == ["48820744846"]


def test_the_latest_state_wins_when_an_awb_was_tracked_twice(store):
    """Tracked, timed out, tracked again, completed: it is complete."""
    first = store.create_job("48820744846", CHANNEL, "U1")
    store.finish(first.id, "timeout", "still 99.9%")
    second = store.create_job("48820744846", CHANNEL, "U1")
    store.finish(second.id, "complete", "100% cleared")

    plan = plan_starts(["488-20744846"], store, CHANNEL, retry=True)
    assert plan.to_start == []
    assert plan.settled == ["48820744846"]


def test_another_channels_history_does_not_count(store):
    job = store.create_job("48820744846", "C-OTHER", "U1")
    store.finish(job.id, "complete", "100% cleared")

    assert plan_starts(["488-20744846"], store, CHANNEL).to_start == ["48820744846"]


def test_the_report_distinguishes_completed_from_unfinished(tmp_path, monkeypatch, capsys):
    from lej_cc import bulk

    db = tmp_path / "j.sqlite3"
    store = JobStore(db)
    done = store.create_job("93602927993", CHANNEL, "U1")
    store.finish(done.id, "complete", "100% cleared")
    stuck = store.create_job("48820744846", CHANNEL, "U1")
    store.finish(stuck.id, "timeout", "still 99.9%")

    path = csv_file(tmp_path, MONTH_TAB)
    monkeypatch.setattr(
        bulk, "Settings", lambda: Settings(
            slack_bot_token="x", slack_app_token="x", portground_api_key="k",
            database_path=db, slack_ops_channel=CHANNEL,
        )
    )
    bulk.main(["--from-file", str(path), "--dry-run"])
    out = capsys.readouterr().out

    assert "1 already completed" in out
    assert "1 tracked before, unfinished (1 timeout)" in out
    assert "pass --retry" in out


def test_the_same_awb_twice_on_one_sheet_starts_once(store):
    plan = plan_starts(["488-20744846", "488 2074 4846"], store, CHANNEL)
    assert plan.to_start == ["48820744846"]
    assert plan.ignored == 1


# --- starting ------------------------------------------------------------


def test_starts_are_spread_out_so_portground_is_not_flooded(store):
    """Each AWB is a slow export and the scheduler claims twenty at a time."""
    plan = Plan(to_start=["48820744846", "93602927993", "93602927971"])
    before = utcnow()

    started = start_tracking(plan, store, CHANNEL, stagger_minutes=5)

    assert started == plan.to_start
    runs = sorted(job.next_run_at for job in store.list_all_jobs())
    assert runs[1] - runs[0] >= timedelta(minutes=4, seconds=59)
    assert runs[2] - runs[0] >= timedelta(minutes=9, seconds=59)
    assert runs[0] - before < timedelta(minutes=1)


def test_zero_stagger_starts_everything_at_once(store):
    plan = Plan(to_start=["48820744846", "93602927993"])
    start_tracking(plan, store, CHANNEL, stagger_minutes=0)

    runs = [job.next_run_at for job in store.list_all_jobs()]
    assert max(runs) - min(runs) < timedelta(seconds=2)


def test_a_race_with_slack_is_survived_not_crashed(store):
    """Somebody typing /awb between the plan and the write is not an error."""
    store.create_job("48820744846", CHANNEL, "U1")
    plan = Plan(to_start=["48820744846", "93602927993"])

    started = start_tracking(plan, store, CHANNEL)

    assert started == ["93602927993"]


def test_the_source_is_recorded_so_bulk_starts_are_identifiable(store):
    start_tracking(Plan(to_start=["48820744846"]), store, CHANNEL)
    assert store.list_all_jobs()[0].source == "bulk"


# --- the command ---------------------------------------------------------


def test_dry_run_starts_nothing(tmp_path, monkeypatch, capsys):
    from lej_cc import bulk

    path = csv_file(tmp_path, MONTH_TAB)
    db = tmp_path / "jobs.sqlite3"
    monkeypatch.setattr(
        bulk, "Settings", lambda: Settings(
            slack_bot_token="x", slack_app_token="x", portground_api_key="k",
            database_path=db, slack_ops_channel=CHANNEL,
        )
    )
    code = bulk.main(["--from-file", str(path), "--dry-run"])

    assert code == 0
    assert "dry run" in capsys.readouterr().out
    assert JobStore(db).list_all_jobs() == []


def test_without_a_channel_it_refuses(tmp_path, monkeypatch, capsys):
    from lej_cc import bulk

    monkeypatch.setattr(
        bulk, "Settings", lambda: Settings(
            slack_bot_token="x", slack_app_token="x", portground_api_key="k",
            database_path=tmp_path / "j.sqlite3",
        )
    )
    assert bulk.main(["--from-file", "whatever.csv", "--dry-run"]) == 2
    assert "SLACK_STATUS_CHANNEL" in capsys.readouterr().err


def test_one_watched_channel_is_suggested_not_silently_used(tmp_path, monkeypatch, capsys):
    """Naming it makes the fix obvious; using it would post a month of cards
    into a channel nobody chose."""
    from lej_cc import bulk

    path = csv_file(tmp_path, MONTH_TAB)
    db = tmp_path / "j.sqlite3"
    monkeypatch.setattr(
        bulk, "Settings", lambda: Settings(
            slack_bot_token="x", slack_app_token="x", portground_api_key="k",
            database_path=db, autodetect_channels="C0C05GFT40H",
        )
    )
    assert bulk.main(["--from-file", str(path), "--yes"]) == 2

    err = capsys.readouterr().err
    assert "C0C05GFT40H" in err
    assert "SLACK_STATUS_CHANNEL=C0C05GFT40H" in err
    assert JobStore(db).list_all_jobs() == []


def test_several_watched_channels_are_not_guessed_between(tmp_path, monkeypatch, capsys):
    from lej_cc import bulk

    monkeypatch.setattr(
        bulk, "Settings", lambda: Settings(
            slack_bot_token="x", slack_app_token="x", portground_api_key="k",
            database_path=tmp_path / "j.sqlite3", autodetect_channels="C1,C2",
        )
    )
    assert bulk.main(["--from-file", "x.csv", "--dry-run"]) == 2
    assert "probably the one you want" not in capsys.readouterr().err


def test_unattended_without_yes_refuses_rather_than_hanging(tmp_path, monkeypatch, capsys):
    """A cron job must not block on input(), and must not start silently."""
    from lej_cc import bulk

    path = csv_file(tmp_path, MONTH_TAB)
    db = tmp_path / "jobs.sqlite3"
    monkeypatch.setattr(
        bulk, "Settings", lambda: Settings(
            slack_bot_token="x", slack_app_token="x", portground_api_key="k",
            database_path=db, slack_ops_channel=CHANNEL,
        )
    )
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    assert bulk.main(["--from-file", str(path)]) == 2
    assert "--yes" in capsys.readouterr().err
    assert JobStore(db).list_all_jobs() == []


def test_yes_starts_them(tmp_path, monkeypatch, capsys):
    from lej_cc import bulk

    path = csv_file(tmp_path, MONTH_TAB)
    db = tmp_path / "jobs.sqlite3"
    monkeypatch.setattr(
        bulk, "Settings", lambda: Settings(
            slack_bot_token="x", slack_app_token="x", portground_api_key="k",
            database_path=db, slack_ops_channel=CHANNEL,
        )
    )
    assert bulk.main(["--from-file", str(path), "--yes"]) == 0
    assert len(JobStore(db).list_all_jobs()) == 3
    assert "3 started" in capsys.readouterr().out


def test_limit_caps_what_is_started(tmp_path, monkeypatch):
    from lej_cc import bulk

    path = csv_file(tmp_path, MONTH_TAB)
    db = tmp_path / "jobs.sqlite3"
    monkeypatch.setattr(
        bulk, "Settings", lambda: Settings(
            slack_bot_token="x", slack_app_token="x", portground_api_key="k",
            database_path=db, slack_ops_channel=CHANNEL,
        )
    )
    assert bulk.main(["--from-file", str(path), "--yes", "--limit", "1"]) == 0
    assert len(JobStore(db).list_all_jobs()) == 1


def test_a_big_batch_says_what_the_polling_will_cost(tmp_path, monkeypatch, capsys):
    """Staggering spreads the starts; it does not reduce the ongoing load."""
    from lej_cc import bulk

    rows = [["AWB"]] + [[f"OyTM20260901{n:04d}"] for n in range(25)]
    path = csv_file(tmp_path, rows)
    monkeypatch.setattr(
        bulk, "Settings", lambda: Settings(
            slack_bot_token="x", slack_app_token="x", portground_api_key="k",
            database_path=tmp_path / "j.sqlite3", slack_ops_channel=CHANNEL,
            repeat_interval_minutes=30,
        )
    )
    bulk.main(["--from-file", str(path), "--dry-run"])
    out = capsys.readouterr().out

    assert "25 to start" in out
    assert "exports an hour" in out
    assert "--limit" in out


def test_a_small_batch_does_not_lecture(tmp_path, monkeypatch, capsys):
    from lej_cc import bulk

    path = csv_file(tmp_path, MONTH_TAB)
    monkeypatch.setattr(
        bulk, "Settings", lambda: Settings(
            slack_bot_token="x", slack_app_token="x", portground_api_key="k",
            database_path=tmp_path / "j.sqlite3", slack_ops_channel=CHANNEL,
        )
    )
    bulk.main(["--from-file", str(path), "--dry-run"])
    assert "exports an hour" not in capsys.readouterr().out


# --- which tabs, as the months turn -------------------------------------

TABS = {"2026.07", "2026.08", "2026.09", "TEMPLATE", "SLAs", "CC_BOT_IMPORT"}


def test_the_newest_month_tab_is_picked_without_anybody_typing_it():
    from lej_cc.bulk import month_tabs

    assert month_tabs(TABS, 1) == ["2026.09"]


def test_two_months_covers_the_boundary_oldest_first():
    """On 1 October, September's AWBs are still clearing."""
    from lej_cc.bulk import month_tabs

    assert month_tabs(TABS, 2) == ["2026.08", "2026.09"]


def test_a_year_boundary_sorts_correctly():
    from lej_cc.bulk import month_tabs

    assert month_tabs({"2026.11", "2026.12", "2027.01"}, 2) == ["2026.12", "2027.01"]


def test_non_month_tabs_are_never_read():
    """TEMPLATE and SLAs hold no AWBs and must not be swept up."""
    from lej_cc.bulk import month_tabs

    assert "TEMPLATE" not in month_tabs(TABS, 99)
    assert "CC_BOT_IMPORT" not in month_tabs(TABS, 99)


def test_named_tabs_are_used_verbatim_without_asking_the_api(tmp_path):
    from lej_cc.bulk import resolve_tabs

    settings = Settings(
        slack_bot_token="x", slack_app_token="x", portground_api_key="k",
        database_path=tmp_path / "j.sqlite3",
    )
    assert resolve_tabs(settings, "id", ["2026.09", "2026.08"], None) == ["2026.09", "2026.08"]


def test_months_resolves_against_the_real_file(tmp_path, monkeypatch):
    from lej_cc import bulk

    settings = Settings(
        slack_bot_token="x", slack_app_token="x", portground_api_key="k",
        database_path=tmp_path / "j.sqlite3",
    )
    monkeypatch.setattr(
        "lej_cc.sheets.describe_spreadsheet", lambda *a, **k: ("Daily Report_LEJ", TABS)
    )
    assert bulk.resolve_tabs(settings, "id", [], 2) == ["2026.08", "2026.09"]


def test_a_file_with_no_month_tabs_says_what_it_does_have(tmp_path, monkeypatch):
    from lej_cc import bulk

    settings = Settings(
        slack_bot_token="x", slack_app_token="x", portground_api_key="k",
        database_path=tmp_path / "j.sqlite3",
    )
    monkeypatch.setattr(
        "lej_cc.sheets.describe_spreadsheet", lambda *a, **k: ("Something", {"Sheet1", "Notes"})
    )
    with pytest.raises(LookupError, match="Sheet1"):
        bulk.resolve_tabs(settings, "id", [], 1)


def test_comma_separated_tabs_are_split(tmp_path, monkeypatch, capsys):
    from lej_cc import bulk

    seen: dict = {}
    monkeypatch.setattr(
        bulk, "Settings", lambda: Settings(
            slack_bot_token="x", slack_app_token="x", portground_api_key="k",
            database_path=tmp_path / "j.sqlite3", slack_ops_channel=CHANNEL,
            report_sheet_id="report-abc",
        )
    )
    monkeypatch.setattr(
        bulk, "read_from_sheet", lambda s, i, tabs: seen.setdefault("tabs", tabs) and []
    )
    bulk.main(["--from-sheet", "--tab", "2026.08,2026.09", "--dry-run"])

    assert seen["tabs"] == ["2026.08", "2026.09"]


def test_neither_tab_nor_months_is_refused_with_both_ways_out(tmp_path, monkeypatch, capsys):
    from lej_cc import bulk

    monkeypatch.setattr(
        bulk, "Settings", lambda: Settings(
            slack_bot_token="x", slack_app_token="x", portground_api_key="k",
            database_path=tmp_path / "j.sqlite3", slack_ops_channel=CHANNEL,
            report_sheet_id="report-abc",
        )
    )
    assert bulk.main(["--from-sheet", "--dry-run"]) == 2

    err = capsys.readouterr().err
    assert "--tab 2026.09" in err
    assert "--months 2" in err


def test_the_same_awb_in_two_months_starts_once(store):
    """Overlapping tabs must not double-start a straggler."""
    plan = plan_starts(["488-20744846", "936-02927993", "488-20744846"], store, CHANNEL)

    assert plan.to_start == ["48820744846", "93602927993"]
    assert plan.ignored == 1
