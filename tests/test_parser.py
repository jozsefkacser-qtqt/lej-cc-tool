from pathlib import Path

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


def test_status_map_is_found_from_the_working_directory(tmp_path, monkeypatch):
    """In the container the package lives in site-packages, so the mapping is
    found next to the working directory instead."""
    config = tmp_path / "config"
    config.mkdir()
    (config / "status_map.yaml").write_text("cleared: [freigegeben]\nnot_cleared: [offen]\n")
    monkeypatch.chdir(tmp_path)

    mapper = StatusMapper.load()
    assert mapper.map("freigegeben") is ClearanceStatus.CLEARED
    assert mapper.map("offen") is ClearanceStatus.NOT_CLEARED
    # The reference vocabulary is no longer mapped, which is the point of the file.
    assert mapper.map("cleared") is ClearanceStatus.OTHER


def test_missing_configured_status_map_is_an_error_not_a_silent_default(tmp_path):
    with pytest.raises(SchemaDrift):
        StatusMapper.load(tmp_path / "nope.yaml")


def test_timezone_resolves_without_a_system_tz_database():
    """LOCAL_TZ is resolved at import time, and Debian slim images ship no
    /usr/share/zoneinfo — so a missing tz database crashes the container on
    startup rather than failing later. The tzdata package guards against it;
    PYTHONTZPATH="" simulates the stripped image."""
    import os
    import subprocess
    import sys

    import lej_cc

    src_dir = str(Path(lej_cc.__file__).resolve().parents[1])
    result = subprocess.run(
        [sys.executable, "-c", "import lej_cc.parser, lej_cc.formatting; print('ok')"],
        env={**os.environ, "PYTHONTZPATH": "", "PYTHONPATH": src_dir},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"import failed without a system tzdb:\n{result.stderr}"


REFERENCE = "OyTM202608137666"


def test_tracking_by_reference_accepts_rows_carrying_a_real_mawb(data_dir):
    """The rows come back under their actual waybill number. That is not a
    mismatch -- it is the answer to the question that was asked."""
    snap = parse_workbook(data_dir / "all_cleared.xlsx", REFERENCE)

    assert snap.total == 10
    assert snap.mawb == REFERENCE
    assert snap.resolved_mawbs == ["48820744846"]
    assert snap.rows[0].mawb == "48820744846"


def test_tracking_by_mawb_still_rejects_the_wrong_file(data_dir):
    with pytest.raises(MawbMismatch):
        parse_workbook(data_dir / "wrong_mawb.xlsx", "48820744846")


def test_a_mawb_request_reports_no_resolved_numbers(data_dir):
    """resolved_mawbs answers 'what did this reference turn out to be', which
    is not a question a waybill request asks."""
    snap = parse_workbook(data_dir / "all_cleared.xlsx", "48820744846")
    assert snap.resolved_mawbs == []
