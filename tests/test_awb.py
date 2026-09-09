import pytest

from lej_cc.awb import checksum_ok, extract_all, format_display, normalize
from lej_cc.errors import AwbChecksumFailed, InvalidAwbFormat

# Both taken from real PortGround exports; both satisfy IATA Resolution 600a.
REAL = ["48820744846", "93600333955"]


@pytest.mark.parametrize("mawb", REAL)
def test_real_awbs_pass_checksum(mawb):
    assert checksum_ok(mawb)


@pytest.mark.parametrize(
    "raw",
    ["488-20744846", "48820744846", "488 2074 4846", " 488-2074-4846 ", "488.20744846"],
)
def test_normalize_accepts_the_ways_people_type_it(raw):
    assert normalize(raw) == "48820744846"


def test_normalize_rejects_wrong_length():
    with pytest.raises(InvalidAwbFormat):
        normalize("488-2074484")


def test_normalize_rejects_typo_via_check_digit():
    # 48820744845 is one digit off the real number and must not reach the API.
    with pytest.raises(AwbChecksumFailed):
        normalize("48820744845")


def test_checksum_can_be_skipped():
    assert normalize("48820744845", verify_checksum=False) == "48820744845"


def test_format_display():
    assert format_display("48820744846") == "488-20744846"


def test_extract_all_finds_valid_numbers_only():
    text = "please track 488-20744846 and 93600333955, ignore 48820744845"
    assert extract_all(text) == ["48820744846", "93600333955"]


def test_extract_ignores_long_tracking_numbers():
    # 20-digit HAWBs appear in the same channels and must not be mistaken for AWBs.
    assert extract_all("00340434762562881069") == []


def test_extract_deduplicates():
    assert extract_all("488-20744846 488 2074 4846") == ["48820744846"]


# --- booking / consolidation references ---------------------------------
# Not air waybills: alphanumeric, no checksum, and PortGround takes them
# verbatim. OyTM202608137666 is a real one.

REFERENCE = "OyTM202608137666"


def test_reference_is_passed_through_unchanged():
    assert normalize(REFERENCE) == REFERENCE


def test_reference_case_is_preserved():
    """Nothing here knows whether PortGround compares case-sensitively, so
    the safe move is not to touch it."""
    assert normalize("oytm202608137666") == "oytm202608137666"
    assert normalize("OYTM202608137666") == "OYTM202608137666"


def test_reference_surrounded_by_whitespace_is_trimmed():
    assert normalize(f"  {REFERENCE}\n") == REFERENCE


def test_reference_display_is_left_alone():
    assert format_display(REFERENCE) == REFERENCE


def test_mawb_formatting_still_applies():
    assert format_display("48820744846") == "488-20744846"


@pytest.mark.parametrize("bad", ["ab", "with space 123", "much@too#odd", ""])
def test_unusable_input_is_still_rejected(bad):
    with pytest.raises(InvalidAwbFormat):
        normalize(bad)


def test_references_are_not_scraped_from_free_text():
    """No checksum means any pattern loose enough to catch a reference also
    catches order numbers and file names, so scanning stays numbers-only."""
    assert extract_all(f"check {REFERENCE} and 488-20744846") == ["48820744846"]


def test_is_mawb_separates_the_two_shapes():
    from lej_cc.awb import is_mawb

    assert is_mawb("48820744846")
    assert not is_mawb(REFERENCE)
    assert not is_mawb("4882074484")
