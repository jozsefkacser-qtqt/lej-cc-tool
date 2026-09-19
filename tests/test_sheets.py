"""The one-row-per-AWB sheet export.

The Google API is faked. What matters here is that a row carries the right
facts, that an AWB updates its existing line rather than accumulating
duplicates, and that a sheet failure can never break a poll -- the sheet is
a convenience, SQLite is the record.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from lej_cc.config import Settings
from lej_cc.model import ClearanceStatus, ShipmentRow, Snapshot
from lej_cc.sheets import COLUMNS, HEADERS, SheetExporter, build_row
from lej_cc.store import JobStore

TZ = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 9, 15, 14, 0, tzinfo=TZ)


def snap(cleared: int, total: int, *, mawb: str = "93602927993") -> Snapshot:
    rows = [
        ShipmentRow(
            hawb=f"0034043{i:013d}",
            mawb=mawb,
            status=ClearanceStatus.CLEARED if i < cleared else ClearanceStatus.NOT_CLEARED,
            final_status_raw="cleared" if i < cleared else "not cleared",
            clearance_time=NOW - timedelta(hours=6 - i * 0.1) if i < cleared else None,
            items=2,
        )
        for i in range(total)
    ]
    return Snapshot(mawb=mawb, rows=rows, generated_at=NOW, fetched_at=NOW)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        slack_bot_token="x",
        slack_app_token="x",
        portground_api_key="k",
        database_path=tmp_path / "j.sqlite3",
        google_sheet_id="sheet-abc",
        google_credentials_file=tmp_path / "sa.json",
        google_sheet_tab="CC_BOT",
    )


@pytest.fixture
def store(settings) -> JobStore:
    return JobStore(settings.database_path)


class FakeValues:
    def __init__(self, sheet):
        self.sheet = sheet

    def get(self, spreadsheetId, range):  # noqa: N803
        if range.endswith("!1:1"):
            return _Exec({"values": [self.sheet.header]} if self.sheet.header else {})
        return _Exec({"values": [[k] for k in self.sheet.keys]})

    def update(self, spreadsheetId, range, valueInputOption, body):  # noqa: N803
        if range.endswith("A1"):
            self.sheet.header = body["values"][0]  # not a row update
        else:
            # Sheet row 1 is the header, so data row N lives at rows[N-2].
            index = int(range.split("!A")[1]) - 2
            self.sheet.rows[index] = body["values"][0]
            self.sheet.updates += 1
        return _Exec({})

    def append(self, spreadsheetId, range, valueInputOption, insertDataOption, body):  # noqa: N803
        self.sheet.rows.append(body["values"][0])
        self.sheet.appends += 1
        return _Exec({})


class _Exec:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class FakeSheets:
    """Stands in for the Sheets API. `rows` includes the header at index 0."""

    def __init__(self, tabs=("CC_BOT",), fail=False, title="CC Bot Central"):
        self.header: list | None = None
        self.rows: list[list] = []
        self.tabs = list(tabs)
        self.updates = self.appends = 0
        self.fail = fail
        self.title = title

    @property
    def keys(self):
        return ([self.header[0]] if self.header else []) + [r[0] for r in self.rows]

    def get(self, spreadsheetId):  # noqa: N803
        if self.fail:
            raise RuntimeError("403 permission denied")
        return _Exec(
            {
                "properties": {"title": self.title},
                "sheets": [{"properties": {"title": t}} for t in self.tabs],
            }
        )

    def batchUpdate(self, spreadsheetId, body):  # noqa: N802, N803
        for request in body["requests"]:
            self.tabs.append(request["addSheet"]["properties"]["title"])
        return _Exec({})

    def values(self):
        return FakeValues(self)


def exporter_with(settings, fake) -> SheetExporter:
    exporter = SheetExporter(settings)
    exporter._service = fake
    return exporter


# --- the row ------------------------------------------------------------


def test_row_carries_the_facts(settings, store):
    job = store.create_job("93602927993", "C1", "U1")
    row = build_row(job, snap(8, 10))
    values = dict(zip([f for _, f in COLUMNS], row.as_list(), strict=True))

    assert values["awb"] == "936-02927993"
    assert values["percent"] == 80.0
    assert values["total"] == 10
    assert values["cleared"] == 8
    assert values["open"] == 2
    assert values["lines_total"] == 20
    assert values["state"] == "active"


def test_the_key_matches_the_format_the_report_already_uses(settings, store):
    """The report writes 936-02927610, so a VLOOKUP needs no massaging."""
    job = store.create_job("93602927993", "C1", "U1")
    assert build_row(job, snap(1, 2)).key == "936-02927993"


def test_cc_completed_is_only_set_once_everything_cleared(settings, store):
    """The report's SLA chain reads that column as "customs is done". A
    timestamp there while shipments are open would be a false milestone."""
    job = store.create_job("93602927993", "C1", "U1")
    fields = [f for _, f in COLUMNS]

    partial = dict(zip(fields, build_row(job, snap(8, 10)).as_list(), strict=True))
    assert partial["cc_completed"] == ""

    done = dict(zip(fields, build_row(job, snap(10, 10)).as_list(), strict=True))
    assert done["cc_completed"]
    assert done["clearance_hours"]


def test_a_booking_reference_is_recorded_separately(settings, store):
    job = store.create_job("OyTM202608137666", "C1", "U1")
    fields = [f for _, f in COLUMNS]
    values = dict(
        zip(fields, build_row(job, snap(3, 3, mawb="OyTM202608137666")).as_list(), strict=True)
    )
    assert values["awb"] == "OyTM202608137666"
    assert values["tracked_as"] == "OyTM202608137666"


def test_a_waybills_raw_digits_are_not_repeated(settings, store):
    """"Tracked as" exists to flag a booking reference. 93602927993 next to
    936-02927993 is the same fact written twice."""
    job = store.create_job("93602927993", "C1", "U1")
    fields = [f for _, f in COLUMNS]
    values = dict(zip(fields, build_row(job, snap(1, 2)).as_list(), strict=True))
    assert values["tracked_as"] == ""


def test_a_row_without_a_snapshot_keeps_what_the_store_knows(settings, store):
    """A poll that failed must not blank a line that had numbers in it."""
    from lej_cc.store import utcnow

    job = store.create_job("93602927993", "C1", "U1")
    store.reschedule(job.id, utcnow(), percent=42.0, cleared=42, total=100)
    fields = [f for _, f in COLUMNS]
    values = dict(zip(fields, build_row(store.get(job.id)).as_list(), strict=True))
    assert values["percent"] == 42.0
    assert values["cleared"] == 42


# --- upsert -------------------------------------------------------------


def test_first_write_creates_the_tab_and_the_header(settings, store):
    fake = FakeSheets(tabs=())
    job = store.create_job("93602927993", "C1", "U1")

    assert exporter_with(settings, fake).upsert(build_row(job, snap(5, 10))) is True
    assert "CC_BOT" in fake.tabs
    assert fake.header == HEADERS
    assert fake.appends == 1


def test_the_same_awb_updates_its_line_rather_than_adding_one(settings, store):
    fake = FakeSheets()
    exporter = exporter_with(settings, fake)
    job = store.create_job("93602927993", "C1", "U1")

    exporter.upsert(build_row(job, snap(5, 10)))
    exporter.upsert(build_row(job, snap(9, 10)))
    exporter.upsert(build_row(job, snap(10, 10)))

    assert len(fake.rows) == 1, "one AWB must occupy one line"
    assert fake.appends == 1 and fake.updates == 2
    assert fake.rows[0][HEADERS.index("Cleared")] == 10


def test_different_awbs_get_their_own_lines(settings, store):
    fake = FakeSheets()
    exporter = exporter_with(settings, fake)
    for mawb in ("93602927993", "48820744846"):
        job = store.create_job(mawb, "C1", "U1")
        exporter.upsert(build_row(job, snap(1, 2, mawb=mawb)))

    assert [r[0] for r in fake.rows] == ["936-02927993", "488-20744846"]


def test_a_sheet_failure_never_propagates(settings, store):
    """Slack has already carried the update; a Sheets outage must not fail
    the poll."""
    fake = FakeSheets(fail=True)
    job = store.create_job("93602927993", "C1", "U1")

    assert exporter_with(settings, fake).upsert(build_row(job, snap(5, 10))) is False


def test_export_is_off_until_it_is_configured(tmp_path, store):
    off = Settings(
        slack_bot_token="x",
        slack_app_token="x",
        portground_api_key="k",
        database_path=tmp_path / "j.sqlite3",
    )
    job = store.create_job("93602927993", "C1", "U1")
    assert SheetExporter(off).enabled is False
    assert SheetExporter(off).upsert(build_row(job, snap(1, 2))) is False


# --- describe: what preflight asks the sheet ----------------------------


def test_describe_names_the_spreadsheet_and_its_tabs(settings):
    fake = FakeSheets(tabs=("Sheet1", "CC_BOT"), title="CC Bot Central")
    title, tabs = exporter_with(settings, fake).describe()

    assert title == "CC Bot Central"
    assert tabs == {"Sheet1", "CC_BOT"}


def test_describe_raises_so_preflight_can_say_why(settings):
    fake = FakeSheets(fail=True)
    with pytest.raises(RuntimeError):
        exporter_with(settings, fake).describe()


# --- the column contract ------------------------------------------------


def test_the_documented_column_numbers_still_point_where_the_docs_say():
    """README.md and docs/CENTRAL-SHEET.md quote these positions in formulas.

    Adding a column in the middle would silently move every VLOOKUP in the
    report onto the wrong field -- which is exactly what happened once:
    `Under inspection` was inserted and the documented `CC Completed` index
    of 14 quietly started returning `First clearance`. Append, never insert.
    """
    fields = [field for _, field in COLUMNS]
    one_based = {field: i for i, field in enumerate(fields, start=1)}

    assert len(COLUMNS) == 23, "the documented range is CC_BOT!$A:$W"
    assert one_based["cc_completed"] == 15
    assert one_based["percent"] == 5
    assert one_based["inspection"] == 9
    assert one_based["state"] == 4
    # The lookup keys the report matches on.
    assert one_based["awb"] == 1
    assert one_based["tracked_as"] == 2


def test_the_docs_quote_the_indexes_this_test_pins():
    """Catches a formula edited in the docs without the test being updated."""
    from pathlib import Path

    docs = Path(__file__).resolve().parents[1]
    readme = (docs / "README.md").read_text()
    guide = (docs / "docs" / "CENTRAL-SHEET.md").read_text()

    for text in (readme, guide):
        assert 'CC_BOT!$A:$W' in text
        assert ", 15, FALSE" in text
