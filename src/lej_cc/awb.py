"""Master Air Waybill number handling.

A MAWB is 11 digits: a 3-digit airline prefix, a 7-digit serial, and a
check digit that must equal `serial mod 7` (IATA Resolution 600a). People
write them as 488-20744846, 488 2074 4846, or 48820744846; PortGround's
API and its Excel export both want the bare 11 digits.

Validating the check digit locally means a mistyped AWB is rejected in
Slack in milliseconds instead of turning into a 30-minute polling job that
never finds anything.
"""

from __future__ import annotations

import re

from .errors import AwbChecksumFailed, InvalidAwbFormat

#: Matches candidate AWBs in free text: 3 digits, optional separator, 8 digits.
#: The lookarounds stop us from grabbing 11 digits out of a longer number
#: (e.g. the 20-digit HAWB tracking numbers that appear in the same channel).
AWB_PATTERN = re.compile(r"(?<![\d-])(\d{3})[\s\-.]?(\d{4})[\s\-.]?(\d{4})(?![\d-])")

_NON_DIGIT = re.compile(r"\D")


def normalize(raw: str, *, verify_checksum: bool = True) -> str:
    """Return the bare 11-digit form of `raw`.

    >>> normalize("488-20744846")
    '48820744846'
    >>> normalize(" 936 0033 3955 ")
    '93600333955'

    Raises InvalidAwbFormat if it is not 11 digits, or AwbChecksumFailed if
    the check digit is wrong.
    """
    digits = _NON_DIGIT.sub("", raw or "")

    if not digits:
        raise InvalidAwbFormat(
            f"no digits in {raw!r}",
            user_message=f"`{raw}` doesn't contain an AWB number.",
        )
    if len(digits) != 11:
        raise InvalidAwbFormat(
            f"expected 11 digits, got {len(digits)} in {raw!r}",
            user_message=(
                f"`{raw}` is not a valid MAWB — it has {len(digits)} digits, "
                "but a master air waybill has 11 (e.g. `488-20744846`)."
            ),
        )
    if verify_checksum and not checksum_ok(digits):
        raise AwbChecksumFailed(
            f"check digit mismatch for {digits}",
            user_message=(
                f"`{format_display(digits)}` fails the IATA check-digit test, "
                "so it's probably a typo. Please double-check the number."
            ),
        )
    return digits


def checksum_ok(digits: str) -> bool:
    """True if the last digit equals serial mod 7 (IATA Resolution 600a)."""
    if len(digits) != 11 or not digits.isdigit():
        return False
    serial, check = int(digits[3:10]), int(digits[10])
    return serial % 7 == check


def format_display(digits: str) -> str:
    """Render as `488-20744846`, the form humans and airlines use."""
    d = _NON_DIGIT.sub("", digits or "")
    return f"{d[:3]}-{d[3:]}" if len(d) == 11 else digits


def extract_all(text: str) -> list[str]:
    """Pull every plausible MAWB out of a free-text Slack message.

    Only checksum-valid numbers are returned, which is what makes passive
    channel-watching safe: random 11-digit strings are ignored, and order is
    preserved with duplicates removed.
    """
    found: list[str] = []
    for match in AWB_PATTERN.finditer(text or ""):
        candidate = "".join(match.groups())
        if checksum_ok(candidate) and candidate not in found:
            found.append(candidate)
    return found
