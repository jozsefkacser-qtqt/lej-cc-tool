"""The offline checking CLI.

It had no tests, and an edit that added the --statuses flag but not the code
behind it went unnoticed: argparse accepted the flag and nothing happened,
which looks exactly like a working command finding nothing to report.
"""

from __future__ import annotations

from lej_cc.cli import main


def run(capsys, *args) -> str:
    code = main(list(args))
    assert code == 0
    return capsys.readouterr().out


def test_statuses_lists_every_raw_value_with_its_bucket(capsys, data_dir):
    """The question the Slack card cannot answer: what does the export
    actually say? Needed before adding a value to status_map.yaml."""
    out = run(capsys, "48820744846", "--file", str(data_dir / "unknown_status.xlsx"),
              "--statuses")

    assert "Final Status values:" in out
    assert "'cleared' -> cleared" in out
    assert "'blocked' -> other" in out
    assert "'seized' -> other" in out


def test_statuses_counts_them(capsys, data_dir):
    out = run(capsys, "48820744846", "--file", str(data_dir / "all_cleared.xlsx"),
              "--statuses")
    line = next(li for li in out.splitlines() if "-> cleared" in li)
    assert line.split()[0] == "10"  # the fixture has ten rows, all cleared


def test_the_flag_is_off_by_default(capsys, data_dir):
    out = run(capsys, "48820744846", "--file", str(data_dir / "all_cleared.xlsx"))
    assert "Final Status values:" not in out


def test_the_summary_always_shows_the_headline_numbers(capsys, data_dir):
    out = run(capsys, "48820744846", "--file", str(data_dir / "partial.xlsx"))

    assert "cleared" in out
    assert "open" in out
    assert "decl. lines" in out


def test_list_open_names_the_shipments(capsys, data_dir):
    out = run(capsys, "48820744846", "--file", str(data_dir / "partial.xlsx"),
              "--list-open")
    assert "not cleared" in out


def test_a_fully_cleared_awb_lists_nothing_to_chase(capsys, data_dir):
    out = run(capsys, "48820744846", "--file", str(data_dir / "all_cleared.xlsx"),
              "--list-open")
    assert "100.0%" in out
    # Nothing unsettled, so no per-shipment lines follow the summary.
    assert not [li for li in out.splitlines() if li.startswith("    0034")]


def test_a_bad_awb_is_refused_before_anything_is_read(capsys):
    assert main(["not-an-awb"]) == 2
    assert "doesn't look like an AWB" in capsys.readouterr().err
