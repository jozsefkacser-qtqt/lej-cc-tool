"""Shipments customs has taken for examination.

An inspection is not an open shipment. Nobody here can move it, so counting
it as open makes a finished AWB read as a stuck one -- the real case that
prompted this was 1,067 of 1,079 cleared, sitting at 98.9% with twelve
shipments that were never going to change without a customs decision.

The rule these tests hold to: cleared% + inspection% + open% = 100%.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from lej_cc.formatting import BAR_INSPECTION, build_status_blocks, progress_bar
from lej_cc.model import ClearanceStatus, ShipmentRow, Snapshot, diff_snapshots
from lej_cc.parser import StatusMapper

TZ = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 9, 17, 13, 33, tzinfo=TZ)


def snap(cleared: int = 0, inspection: int = 0, open_: int = 0, other: int = 0) -> Snapshot:
    rows: list[ShipmentRow] = []
    plan = [
        (cleared, ClearanceStatus.CLEARED, "cleared"),
        (inspection, ClearanceStatus.INSPECTION, "marked for inspection"),
        (open_, ClearanceStatus.NOT_CLEARED, "not cleared"),
        (other, ClearanceStatus.OTHER, "seized"),
    ]
    for count, status, raw in plan:
        for i in range(count):
            rows.append(
                ShipmentRow(
                    hawb=f"{status.value[:3]}{i:017d}",
                    mawb="93602928693",
                    status=status,
                    final_status_raw=raw,
                    clearance_time=NOW if status is ClearanceStatus.CLEARED else None,
                    items=2,
                )
            )
    unknown = {"seized": other} if other else {}
    return Snapshot(
        mawb="93602928693", rows=rows, generated_at=NOW, fetched_at=NOW,
        unknown_statuses=unknown,
    )


# --- the counting --------------------------------------------------------


def test_an_inspection_is_not_open():
    s = snap(cleared=1067, inspection=12)
    assert s.inspection == 12
    assert s.open_count == 0
    assert s.open_rows == []


def test_the_percentages_add_up_to_a_hundred():
    """The whole point. 1,067 cleared + 12 inspected of 1,079."""
    s = snap(cleared=1067, inspection=12)
    assert s.percent == 98.9
    assert s.percent_inspection == 1.1
    assert s.percent_settled == 100.0


@pytest.mark.parametrize(
    ("cleared", "inspection", "open_"),
    [(1, 1, 1), (100, 3, 7), (1067, 12, 0), (0, 5, 5), (333, 333, 334)],
)
def test_settled_plus_open_is_always_everything(cleared, inspection, open_):
    s = snap(cleared=cleared, inspection=inspection, open_=open_)
    assert s.cleared + s.inspection + s.open_count == s.total


def test_settled_is_computed_from_counts_not_rounded_percentages():
    """33.3 + 33.3 + 33.3 must not print as 99.9 when it is everything."""
    s = snap(cleared=1, inspection=1, open_=1)
    assert s.percent_settled == 66.7
    assert snap(cleared=1, inspection=2).percent_settled == 100.0


def test_an_unrecognised_status_is_still_open():
    """OTHER means "we do not know", which is never a reason to stop chasing."""
    s = snap(cleared=8, other=2)
    assert s.open_count == 2
    assert len(s.open_rows) == 2


# --- what counts as finished --------------------------------------------


def test_an_awb_held_only_by_customs_is_complete():
    """Nothing here can move it, and polling it for two days changes nothing."""
    s = snap(cleared=1067, inspection=12)
    assert s.is_complete
    assert not s.all_cleared


def test_one_open_shipment_means_not_complete():
    assert not snap(cleared=1067, inspection=11, open_=1).is_complete


def test_all_cleared_is_kept_separate_from_complete():
    """The sheet's CC Completed column hangs off this one: a shipment still
    being examined has not completed customs clearance."""
    s = snap(cleared=10)
    assert s.all_cleared and s.is_complete


def test_an_empty_sheet_is_never_complete():
    assert not snap().is_complete


# --- the chase sheet keeps them visible ---------------------------------


def test_inspections_stay_in_the_attachment():
    """Not open, but a row that vanishes is a row nobody looks at again."""
    s = snap(cleared=5, inspection=3, open_=2)
    assert len(s.unsettled_rows) == 5
    assert len(s.inspection_rows) == 3


def test_an_inspection_without_a_clearance_time_is_consistent():
    row = snap(inspection=1).rows[0]
    assert not row.inconsistent


# --- reading them out of the export -------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "inspection",
        "Marked for inspection",
        "MARKED FOR INSPECTION",
        "customs inspection",
        "Selected for Inspection",
        "  physical   inspection  ",
    ],
)
def test_inspection_wordings_are_recognised(raw):
    """PortGround's exact wording is not pinned down, and miscounting it as
    open is the failure this exists to prevent."""
    mapper = StatusMapper({"cleared": ["cleared"], "not_cleared": ["not cleared"]})
    assert mapper.map(raw) is ClearanceStatus.INSPECTION


def test_an_unrelated_status_is_still_unknown():
    mapper = StatusMapper({"cleared": ["cleared"]})
    assert mapper.map("seized") is ClearanceStatus.OTHER


def test_the_shipped_status_map_knows_inspection():
    assert StatusMapper.load().map("marked for inspection") is ClearanceStatus.INSPECTION


# --- the bar -------------------------------------------------------------


def test_the_bar_fills_to_a_hundred_when_everything_is_settled():
    bar = progress_bar(98.9, inspection=1.1)
    assert "⬜" not in bar
    assert bar.endswith(BAR_INSPECTION)


def test_one_held_shipment_is_still_visible_in_the_bar():
    """12 of 1,079 rounds to nothing; it must not round to invisible."""
    assert BAR_INSPECTION in progress_bar(99.5, inspection=0.5)


@pytest.mark.parametrize(
    ("cleared", "held"), [(98.9, 1.1), (50, 50), (0, 100), (99.9, 0.1), (10, 5), (0, 0)]
)
def test_inspection_never_eats_a_cleared_cell(cleared, held):
    """Width is fixed, the blocks are in order, and adding an inspection
    block never shortens the cleared one."""
    bar = progress_bar(cleared, inspection=held)
    alone = progress_bar(cleared)

    assert len(bar) == 10
    cleared_cells = len(bar) - len(bar.lstrip(bar[0])) if bar[0] not in "🟥⬜" else 0
    assert cleared_cells == (len(alone) - len(alone.lstrip(alone[0])) if alone[0] != "⬜" else 0)
    # cleared block, then inspection block, then empty. Never interleaved.
    stripped = bar.replace(BAR_INSPECTION, "").replace("⬜", "")
    assert bar.startswith(stripped)
    assert BAR_INSPECTION not in bar.split("⬜")[-1] or "⬜" not in bar


def test_a_barely_started_awb_does_not_look_inspected():
    """Both were red before, which made them identical at a glance."""
    assert BAR_INSPECTION not in progress_bar(2.0)


# --- the card ------------------------------------------------------------


def _text(blocks: list[dict]) -> str:
    out = []
    for block in blocks:
        if block.get("text"):
            out.append(block["text"].get("text", ""))
        for field in block.get("fields", []):
            out.append(field["text"])
        for element in block.get("elements", []):
            out.append(element.get("text", ""))
    return "\n".join(out)


def test_the_card_shows_both_percentages_and_their_sum():
    body = _text(build_status_blocks(snap(cleared=1067, inspection=12)))
    assert "98.9%" in body
    assert "1.1%" in body
    assert "100% completed" in body


def test_a_whole_percentage_drops_its_decimal():
    """100.0% is false precision on a number that cannot go higher."""
    from lej_cc.formatting import pct

    assert pct(100.0) == "100%"
    assert pct(75.0) == "75%"
    assert pct(98.6) == "98.6%"
    assert pct(0.0) == "0%"


def test_the_headline_number_is_its_own_header_block():
    """A section renders it at the size of everything else; this is the
    number people are looking for."""
    blocks = build_status_blocks(snap(cleared=54, inspection=21, open_=25))
    assert blocks[2]["type"] == "header"
    assert blocks[2]["text"]["text"] == "❌  75% completed"


def test_one_icon_says_whether_anything_needs_doing():
    from lej_cc.formatting import readiness

    assert readiness(snap(cleared=10))[0] == "✅"                       # nothing to do
    assert readiness(snap(cleared=9, inspection=1))[0] == "🟠"          # customs has it
    assert readiness(snap(cleared=9, open_=1))[0] == "❌"               # open work


def test_the_header_and_the_headline_never_contradict():
    for s in (snap(cleared=10), snap(cleared=9, inspection=1), snap(cleared=5, open_=5)):
        blocks = build_status_blocks(s)
        mark = blocks[2]["text"]["text"].split()[0]
        if not s.is_complete:
            continue  # in progress keeps its own 📦
        assert blocks[0]["text"]["text"].startswith(mark)


def test_the_card_names_the_held_shipments():
    body = _text(build_status_blocks(snap(cleared=5, inspection=2)))
    assert "❌" in body
    assert "taken by customs" in body


def test_a_clean_awb_says_nothing_about_inspections():
    body = _text(build_status_blocks(snap(cleared=10)))
    assert "inspection" not in body.lower()


def test_finished_never_reads_as_fully_cleared():
    """CC Finished is true -- nothing here can move it -- but the icon and
    the breakdown have to stop it reading as "everything was released"."""
    blocks = build_status_blocks(snap(cleared=1067, inspection=12))

    assert blocks[0]["text"]["text"].startswith("🟠 CC Finished")
    assert blocks[2]["text"]["text"] == "🟠  100% completed"
    breakdown = blocks[3]["text"]["text"]
    assert "98.9%* cleared" in breakdown
    assert "1.1%* inspection" in breakdown


def test_there_is_no_breakdown_when_nothing_is_held():
    """It would repeat the headline number with a different word beside it."""
    from lej_cc.formatting import breakdown_lines

    assert breakdown_lines(snap(cleared=5, open_=5)) == ""
    texts = [b["text"]["text"] for b in build_status_blocks(snap(cleared=5, open_=5))
             if b["type"] in ("header", "section") and "text" in b]
    assert not any("cleared" in t and "%" in t and "completed" not in t for t in texts)


def test_a_new_inspection_is_reported_as_a_change():
    before = {"cle00000000000000000": "cleared", "ins00000000000000000": "not_cleared"}
    current = snap(cleared=1, inspection=1)
    diff = diff_snapshots(before, current)
    assert diff.newly_inspected == ["ins00000000000000000"]
    assert diff.has_changes


# --- the grid ------------------------------------------------------------


def test_the_default_shape_is_one_row_of_ten():
    """Settled after looking at the alternatives in a real channel: ten rows
    of a hundred cells was too tall, monospace too drab."""
    from lej_cc.formatting import GRID_COLS, GRID_ROWS, progress_grid

    assert (GRID_ROWS, GRID_COLS) == (1, 10)
    rows = progress_grid(50.0).split("\n")
    assert len(rows) == 1
    assert len(rows[0]) == 10


def test_the_shape_is_tunable_without_touching_the_code():
    from lej_cc.formatting import progress_grid

    for rows, cols in ((5, 20), (4, 25), (1, 20), (10, 10)):
        drawn = progress_grid(50.0, 10.0, rows=rows, cols=cols)
        assert len(drawn.split("\n")) == rows
        assert all(len(r) == cols for r in drawn.split("\n"))


def test_a_hundred_cells_tell_the_truth_a_tenth_could_not():
    """Twelve held shipments of 1,079 is 1.1%. Ten cells had to draw that as
    a whole cell -- a tenth of the AWB, nine times too much."""
    from lej_cc.formatting import progress_bar, progress_grid

    assert progress_bar(98.9, inspection=1.1).count(BAR_INSPECTION) == 1  # 10%
    assert progress_grid(98.9, 1.1, rows=10, cols=10).count(BAR_INSPECTION) == 2  # 2%


def test_the_grid_fills_completely_only_when_everything_is_settled():
    from lej_cc.formatting import progress_grid

    assert "⬜" not in progress_grid(98.9, 1.1)
    assert "⬜" in progress_grid(99.9)  # 0.1% short is not finished
    assert "⬜" not in progress_grid(98.9, 1.1, rows=10, cols=10)
    assert "⬜" in progress_grid(99.9, rows=10, cols=10)


def test_a_single_held_shipment_still_shows_in_the_grid():
    from lej_cc.formatting import progress_grid

    assert BAR_INSPECTION in progress_grid(99.95, 0.05)


def test_the_grid_always_has_exactly_a_hundred_cells():
    from lej_cc.formatting import progress_grid

    for cleared, held in ((0, 0), (100, 0), (0, 100), (33.3, 33.3), (98.9, 1.1), (0.4, 0.3)):
        assert len(progress_grid(cleared, held, rows=10, cols=10).replace("\n", "")) == 100
        assert len(progress_grid(cleared, held).replace("\n", "")) == 10


def test_every_style_is_reachable_from_the_config():
    from lej_cc.formatting import progress_visual

    assert "\n" not in progress_visual(50.0, 10.0, style="bar")
    assert progress_visual(50.0, 10.0, style="grid").count("\n") == 0  # 1 x 10
    assert progress_visual(50.0, 10.0, style="grid", rows=10, cols=10).count("\n") == 9
    assert progress_visual(50.0, 10.0, style="slim").startswith("```")


# --- the slim rendering --------------------------------------------------


def test_slim_is_two_rows_of_fifty():
    from lej_cc.formatting import progress_slim

    body = progress_slim(50.0).strip("`").strip()
    rows = body.split("\n")
    assert len(rows) == 2
    assert all(len(r) == 50 for r in rows)


def test_slim_keeps_the_same_one_percent_resolution_as_the_grid():
    from lej_cc.formatting import SLIM_INSPECTION, progress_grid, progress_slim

    assert progress_slim(98.9, 1.1).count(SLIM_INSPECTION) == 2
    assert progress_grid(98.9, 1.1, rows=10, cols=10).count(BAR_INSPECTION) == 2


def test_slim_is_monospace_so_slack_does_not_reflow_it():
    from lej_cc.formatting import progress_slim

    drawn = progress_slim(30.0, 10.0)
    assert drawn.startswith("```\n") and drawn.endswith("\n```")


def test_slim_distinguishes_the_three_states_without_colour():
    """Slack cannot colour text, so density has to carry the meaning."""
    from lej_cc.formatting import SLIM_CLEARED, SLIM_INSPECTION, SLIM_OPEN, progress_slim

    drawn = progress_slim(40.0, 20.0)
    assert len({SLIM_CLEARED, SLIM_INSPECTION, SLIM_OPEN}) == 3
    for glyph in (SLIM_CLEARED, SLIM_INSPECTION, SLIM_OPEN):
        assert glyph in drawn


def test_every_style_agrees_about_what_a_percentage_looks_like():
    """One place decides the split, so the three renderings cannot drift."""
    from lej_cc.formatting import progress_bar, progress_grid, progress_slim

    for cleared, held in ((98.9, 1.1), (54.0, 21.0), (0.0, 0.0), (100.0, 0.0)):
        settled_grid = 100 - progress_grid(cleared, held, rows=10, cols=10).count("⬜")
        settled_slim = 100 - progress_slim(cleared, held).count("░")
        assert settled_grid == settled_slim
        assert len(progress_bar(cleared, inspection=held)) == 10


# --- your own cells ------------------------------------------------------


def test_custom_cells_replace_the_built_in_ones():
    """A workspace with narrow custom emoji gets slim *and* coloured."""
    from lej_cc.formatting import progress_grid

    drawn = progress_grid(50.0, 10.0, rows=10, cols=10, override=(":g:", ":r:", ":o:"))
    assert drawn.count(":g:") == 50
    assert drawn.count(":r:") == 10
    assert "🟩" not in drawn


def test_the_legend_uses_whatever_the_bar_is_drawn_with():
    """A green chip beside a monochrome bar explains nothing."""
    from lej_cc.formatting import SLIM_CLEARED, breakdown_lines

    s = snap(cleared=54, inspection=21, open_=25)
    assert SLIM_CLEARED in breakdown_lines(s, "slim")
    assert "🟩" in breakdown_lines(s, "grid")
    assert ":g:" in breakdown_lines(s, "grid", override=(":g:", ":r:", ":o:"))


def test_partly_set_overrides_fall_back_per_cell():
    from lej_cc.formatting import cells_for

    assert cells_for((":g:", "", "")) == (":g:", BAR_INSPECTION, "⬜")
    assert cells_for(None) == ("🟩", BAR_INSPECTION, "⬜")


def test_custom_cells_never_land_inside_a_code_fence():
    """A code block prints `:cc-done:` as text instead of rendering it, so
    the style has to give way to the emoji somebody uploaded."""
    from lej_cc.formatting import progress_visual

    drawn = progress_visual(
        50.0, 10.0, style="slim", override=(":g:", ":r:", ":o:"), rows=10, cols=10
    )
    assert "```" not in drawn
    assert drawn.count(":g:") == 50


def test_a_multi_character_cell_is_never_cut_at_a_row_break():
    from lej_cc.formatting import progress_grid

    grid = progress_grid(50.0, 10.0, rows=10, cols=10, override=(":g:", ":r:", ":o:"))
    for row in grid.split("\n"):
        assert row.count(":") == 20  # ten cells, two colons each


# --- one row of ten, three colours --------------------------------------


def test_the_default_is_one_row_of_ten():
    from lej_cc.formatting import GRID_COLS, GRID_ROWS

    assert (GRID_ROWS, GRID_COLS) == (1, 10)
    assert "\n" not in build_status_blocks(snap(cleared=50, open_=50))[1]["text"]["text"]


def test_green_only_when_everything_cleared():
    from lej_cc.formatting import BAR_CLEARED, progress_bar

    assert progress_bar(100.0) == BAR_CLEARED * 10
    for percent in (99.9, 98.9, 50.0, 0.1):
        assert progress_bar(percent) != BAR_CLEARED * 10


def test_a_held_shipment_always_puts_red_on_the_bar():
    """Ten cells make a cell worth 10%, so even 0.05% takes one. Coarse on
    purpose: the exact figure is printed underneath."""
    from lej_cc.formatting import BAR_INSPECTION, progress_bar

    for held in (0.05, 1.1, 21.0, 100.0):
        assert BAR_INSPECTION in progress_bar(100.0 - held, inspection=held)


def test_no_status_at_all_is_a_blank_bar():
    from lej_cc.formatting import BAR_EMPTY, progress_bar

    assert progress_bar(0.0) == BAR_EMPTY * 10


def test_the_three_blocks_never_overlap_or_leave_a_gap():
    from lej_cc.formatting import BAR_CLEARED, BAR_EMPTY, BAR_INSPECTION, progress_bar

    for cleared, held in ((98.9, 1.1), (54, 21), (0, 0), (100, 0), (0, 100), (33, 33)):
        drawn = progress_bar(cleared, inspection=held)
        assert len(drawn) == 10
        assert drawn == (
            BAR_CLEARED * drawn.count(BAR_CLEARED)
            + BAR_INSPECTION * drawn.count(BAR_INSPECTION)
            + BAR_EMPTY * drawn.count(BAR_EMPTY)
        )


# --- the listings ---------------------------------------------------------


def _job(**kw):
    from lej_cc.store import Job, utcnow

    base = dict(
        id=1, mawb="93602928693", channel_id="C1", thread_ts=None, requested_by="U1",
        state="active", created_at=utcnow(), next_run_at=utcnow(),
    )
    return Job(**{**base, **kw})


def test_a_listing_shows_both_figures_when_customs_holds_some():
    """A bare 54.0% reads as an AWB twenty-one percent worse off than it is
    -- the same mistake the card itself used to make."""
    from lej_cc.formatting import job_percent

    line = job_percent(_job(last_percent=54.0, last_cleared=54, last_inspection=21,
                            last_total=100))
    assert line == "54.0% + 21.0% inspection = 75.0%"


def test_a_listing_stays_short_when_nothing_is_held():
    from lej_cc.formatting import job_percent

    assert job_percent(_job(last_percent=98.6, last_cleared=3590, last_inspection=0,
                            last_total=3640)) == "98.6%"


def test_a_job_polled_before_the_column_existed_still_renders():
    """last_inspection is None on every row written before this landed."""
    from lej_cc.formatting import job_percent

    assert job_percent(_job(last_percent=93.5, last_cleared=935, last_total=1000)) == "93.5%"


def test_a_job_with_no_poll_yet_says_so():
    from lej_cc.formatting import job_percent

    assert job_percent(_job()) == "first check pending"


def test_the_status_card_lists_the_breakdown():
    from lej_cc.config import Settings
    from lej_cc.formatting import build_status_report
    from lej_cc.health import Health

    settings = Settings(slack_bot_token="x", slack_app_token="x", portground_api_key="k")
    job = _job(last_percent=54.0, last_cleared=54, last_inspection=21, last_total=100)
    body = build_status_report(Health(), [job], settings)[0]["text"]["text"]

    assert "54.0% + 21.0% inspection = 75.0%" in body
