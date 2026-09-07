import pytest

from lej_cc.errors import AwbNotFound, MawbMismatch, SchemaDrift, UnexpectedPayload
from lej_cc.model import ClearanceStatus
from lej_cc.parser import StatusMapper, parse_workbook


def test_all_cleared(data_dir):
    snap = parse_workbook(data_dir / "all_cleared.xlsx", "48820744846")
    assert (snap.total, snap.cleared, snap.not_cleared, snap.other) == (10, 10, 0, 0)
    assert snap.percent == 100.0
    assert snap.is_complete
    assert snap.open_rows == []


def test_none_cleared(data_dir):
    snap = parse_workbook(data_dir / "none_cleared.xlsx", "93600333955")
    assert (snap.total, snap.cleared, snap.not_cleared) == (12, 0, 12)
    assert snap.percent == 0.0
    assert not snap.is_complete
    assert len(snap.open_rows) == 12


def test_partial(data_dir):
    snap = parse_workbook(data_dir / "partial.xlsx", "48820744846")
    assert (snap.cleared, snap.not_cleared) == (6, 4)
    assert snap.percent == 60.0
    assert snap.items_percent == 60.0


def test_timestamps_are_berlin_local(data_dir):
    snap = parse_workbook(data_dir / "all_cleared.xlsx", "48820744846")
    row = snap.rows[0]
    assert row.clearance_time is not None
    assert row.clearance_time.utcoffset() is not None
    assert snap.generated_at is not None


def test_cleared_rows_are_internally_consistent(data_dir):
    snap = parse_workbook(data_dir / "partial.xlsx", "48820744846")
    assert not any(r.inconsistent for r in snap.rows)


def test_unknown_status_is_surfaced_not_swallowed(data_dir):
    snap = parse_workbook(data_dir / "unknown_status.xlsx", "48820744846")
    assert snap.other == 2
    assert snap.unknown_statuses == {"blocked": 1, "seized": 1}
    # Crucially, unknown values are never counted as cleared.
    assert snap.cleared == 3
    assert snap.percent == 60.0


def test_empty_sheet_raises_not_found(data_dir):
    with pytest.raises(AwbNotFound):
        parse_workbook(data_dir / "empty.xlsx", "48820744846")


def test_rows_for_another_mawb_are_rejected(data_dir):
    with pytest.raises(MawbMismatch):
        parse_workbook(data_dir / "wrong_mawb.xlsx", "48820744846")


def test_non_xlsx_payload(tmp_path):
    bogus = tmp_path / "error.xlsx"
    bogus.write_bytes(b"<html><body>502 Bad Gateway</body></html>")
    with pytest.raises(UnexpectedPayload):
        parse_workbook(bogus, "48820744846")


def test_missing_required_column(tmp_path):
    import openpyxl

    workbook = openpyxl.Workbook()
    workbook.active.append(["HAWB / Tracking number", "MAWB"])  # no Final Status
    workbook.active.append(["0034", "48820744846"])
    path = tmp_path / "drift.xlsx"
    workbook.save(path)
    with pytest.raises(SchemaDrift):
        parse_workbook(path, "48820744846")


def test_status_mapper_is_case_and_space_insensitive():
    mapper = StatusMapper({"cleared": ["cleared"], "not_cleared": ["not cleared"]})
    assert mapper.map("Cleared") is ClearanceStatus.CLEARED
    assert mapper.map("  NOT   cleared ") is ClearanceStatus.NOT_CLEARED
    assert mapper.map("blocked") is ClearanceStatus.OTHER
    assert mapper.map(None) is ClearanceStatus.OTHER
