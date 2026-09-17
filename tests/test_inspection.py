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
    assert "100.0%" in body


def test_the_card_names_the_held_shipments():
    body = _text(build_status_blocks(snap(cleared=5, inspection=2)))
    assert "❌" in body
    assert "taken by customs" in body


def test_a_clean_awb_says_nothing_about_inspections():
    body = _text(build_status_blocks(snap(cleared=10)))
    assert "inspection" not in body.lower()


def test_the_headline_does_not_claim_a_clean_finish():
    blocks = build_status_blocks(snap(cleared=1067, inspection=12))
    assert "under inspection" in blocks[0]["text"]["text"]


def test_a_new_inspection_is_reported_as_a_change():
    before = {"cle00000000000000000": "cleared", "ins00000000000000000": "not_cleared"}
    current = snap(cleared=1, inspection=1)
    diff = diff_snapshots(before, current)
    assert diff.newly_inspected == ["ins00000000000000000"]
    assert diff.has_changes
