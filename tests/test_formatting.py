"""The status card is read on a phone, at a glance, by someone who wants one
number. These cover the parts where getting it subtly wrong misleads."""

from __future__ import annotations

from lej_cc.formatting import BAR_WIDTH, bar_colour, build_status_blocks, progress_bar
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
    assert bar_colour(2.0) == "🟥"


def test_card_leads_with_the_number():
    blocks = build_status_blocks(snap(1544, 1578))
    assert blocks[0]["type"] == "header"
    assert "936-02927993" in blocks[0]["text"]["text"]
    assert "97.8%" in blocks[1]["text"]["text"]


def test_completed_card_drops_the_buttons_and_the_open_list():
    blocks = build_status_blocks(snap(10, 10))
    types = [b["type"] for b in blocks]
    assert "actions" not in types
    assert "cleared" in blocks[0]["text"]["text"]


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
