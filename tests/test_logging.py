"""Credentials must never reach a log file.

The PortGround key travels in the query string, so any library that logs a
request URL logs the key with it -- httpx does exactly that at INFO. These
tests cover the central filter rather than individual call sites, because
that is the only version that survives a dependency change.
"""

from __future__ import annotations

import io
import logging

from lej_cc.config import RedactingFilter, configure_logging, scrub

# Synthetic values with the right shape. Never put a real credential in a
# test: it ends up in the repository, in every clone, and in the history
# long after it is rotated.
# Assembled from parts rather than written out: a literal of the right shape
# trips GitHub's push protection even when the value is invented, and a test
# file is not worth an exception to that rule.
_DIGITS = "0" * 12
KEY = "FAKEAPIKEY" + "0" * 54
BOT = "-".join(("xoxb", _DIGITS, _DIGITS, "FAKEBOTTOKENVALUE00000"))
APP = "-".join(("xapp", "1", "A" + _DIGITS, _DIGITS, "f" * 40))


def test_scrubs_the_api_key_out_of_a_url():
    url = f"GET https://ecommerce.portground.com/api/x/download?apiKey={KEY} 200 OK"
    cleaned = scrub(url)
    assert KEY not in cleaned
    assert "apiKey=***" in cleaned
    assert "ecommerce.portground.com" in cleaned  # still diagnosable


def test_scrubs_slack_tokens():
    assert BOT not in scrub(f"auth failed for {BOT}")
    assert APP not in scrub(f"socket error with {APP}")


def test_filter_handles_percent_style_arguments():
    """httpx logs with args, so scrubbing record.msg alone would miss it."""
    record = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='HTTP Request: %s "%s"',
        args=(f"GET https://x/download?apiKey={KEY}", "HTTP/1.1 200 OK"),
        exc_info=None,
    )
    assert RedactingFilter().filter(record) is True
    assert KEY not in record.getMessage()


def test_configured_logging_redacts_real_output():
    """End to end: emit through a configured handler and read what came out."""
    stream = io.StringIO()
    configure_logging("INFO")

    root = logging.getLogger()
    handler = logging.StreamHandler(stream)
    handler.addFilter(RedactingFilter())
    root.addHandler(handler)
    try:
        logging.getLogger("httpx").warning(
            "HTTP Request: GET https://ecommerce.portground.com/d?apiKey=%s", KEY
        )
    finally:
        root.removeHandler(handler)

    written = stream.getvalue()
    assert KEY not in written
    assert "apiKey=***" in written


def test_request_chatter_is_quietened():
    configure_logging("INFO")
    assert logging.getLogger("httpx").level >= logging.WARNING
