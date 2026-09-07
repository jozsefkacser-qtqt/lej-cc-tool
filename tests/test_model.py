from lej_cc.model import ClearanceStatus, ShipmentRow, Snapshot, diff_snapshots


def snap(states: dict[str, str]) -> Snapshot:
    return Snapshot(
        mawb="48820744846",
        rows=[
            ShipmentRow(hawb=h, mawb="48820744846", status=ClearanceStatus(s), items=1)
            for h, s in states.items()
        ],
    )


def test_first_poll_has_no_diff():
    assert not diff_snapshots(None, snap({"a": "cleared"})).has_changes


def test_newly_cleared():
    previous = {"a": "not_cleared", "b": "not_cleared"}
    diff = diff_snapshots(previous, snap({"a": "cleared", "b": "not_cleared"}))
    assert diff.newly_cleared == ["a"]
    assert diff.delta_cleared == 1
    assert diff.has_changes


def test_new_and_removed_shipments():
    diff = diff_snapshots({"a": "cleared"}, snap({"a": "cleared", "b": "not_cleared"}))
    assert diff.newly_added == ["b"]
    assert diff.removed == []

    diff = diff_snapshots({"a": "cleared", "b": "cleared"}, snap({"a": "cleared"}))
    assert diff.removed == ["b"]


def test_regression_is_flagged():
    diff = diff_snapshots({"a": "cleared"}, snap({"a": "not_cleared"}))
    assert diff.regressed == ["a"]
    assert diff.delta_cleared == -1


def test_empty_snapshot_is_not_complete():
    """A zero-row sheet must never read as 100% done."""
    empty = Snapshot(mawb="48820744846", rows=[])
    assert empty.percent == 0.0
    assert not empty.is_complete
