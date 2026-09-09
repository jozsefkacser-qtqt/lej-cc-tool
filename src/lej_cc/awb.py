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
_HAS_LETTER = re.compile(r"[A-Za-z]")

#: Booking / consolidation references such as OyTM202608137666. Not air
#: waybills and not checksummed, so there is nothing to validate beyond the
#: shape -- PortGround decides whether the reference exists.
REFERENCE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{5,31}")


def is_mawb(value: str) -> bool:
    """True if `value` is a bare 11-digit master air waybill number."""
    return len(value) == 11 and value.isdigit()


def normalize(raw: str, *, verify_checksum: bool = True) -> str:
    """Return the identifier to send to PortGround.

    Two shapes are accepted, and they are validated very differently.

    A master air waybill is 11 digits however it was typed, and its check
    digit is verified:

    >>> normalize("488-20744846")
    '48820744846'
    >>> normalize(" 936 0033 3955 ")
    '93600333955'

    A booking or consolidation reference contains letters and is passed
    through **exactly** as given -- case included, since nothing here knows
    whether PortGround compares case-sensitively:

    >>> normalize("OyTM202608137666")
    'OyTM202608137666'

    There is no checksum to test on a reference, so a typo in one can only
    be caught by the API saying it has never heard of it.
    """
    text = (raw or "").strip()

    if _HAS_LETTER.search(text):
        if REFERENCE_PATTERN.fullmatch(text):
            return text
        raise InvalidAwbFormat(
            f"{text!r} is not a usable reference",
            user_message=(
                f"`{raw}` doesn't look like an AWB or a booking reference. "
                "Expected either 11 digits (`488-20744846`) or a reference "
                "like `OyTM202608137666`."
            ),
        )

    digits = _NON_DIGIT.sub("", text)

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
                "but a master air waybill has 11 (e.g. `488-20744846`). "
                "Booking references such as `OyTM202608137666` are also accepted."
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


def format_display(value: str) -> str:
    """Render an AWB as `488-20744846`; leave a reference exactly as it is."""
    digits = _NON_DIGIT.sub("", value or "")
    if is_mawb(digits) and not _HAS_LETTER.search(value or ""):
        return f"{digits[:3]}-{digits[3:]}"
    return value


def extract_all(text: str) -> list[str]:
    """Pull every plausible MAWB out of a free-text Slack message.

    Only checksum-valid numbers are returned, which is what makes passive
    channel-watching safe: random 11-digit strings are ignored, and order is
    preserved with duplicates removed.

    Booking references are deliberately **not** matched here. They have no
    checksum, so any pattern loose enough to catch `OyTM202608137666` would
    also catch order numbers, file names and half the words in a signature.
    They are accepted when someone types one as a command argument, where
    the intent is explicit.
    """
    found: list[str] = []
    for match in AWB_PATTERN.finditer(text or ""):
        candidate = "".join(match.groups())
        if checksum_ok(candidate) and candidate not in found:
            found.append(candidate)
    return found
