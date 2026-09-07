"""Error taxonomy.

Every failure the bot can hit is one of these, and each one carries the
Slack-facing text with it. That keeps "what went wrong" and "what we tell
the user" in the same place, and lets the scheduler decide -- from the
exception type alone -- whether a job should retry, stop, or alert ops.
"""

from __future__ import annotations


class LejCcError(Exception):
    """Base class. `user_message` is what gets posted to Slack."""

    #: Should the tracking job keep polling after this error?
    retryable: bool = False
    #: Is this a global problem (bad API key, etc.) rather than one AWB's?
    global_failure: bool = False

    def __init__(self, message: str, *, user_message: str | None = None) -> None:
        super().__init__(message)
        self.user_message = user_message or message


# --- input errors: caught before we ever call the API -------------------


class InvalidAwbFormat(LejCcError):
    """Not 11 digits after stripping separators."""


class AwbChecksumFailed(LejCcError):
    """11 digits, but the IATA mod-7 check digit does not match -- likely a typo."""


# --- API errors ---------------------------------------------------------


class AwbNotFound(LejCcError):
    """API returned 404, or a well-formed sheet with zero shipment rows.

    Retryable: an AWB can be tracked before PortGround has checked it in, so
    an empty result early on is normal. The scheduler gives up after
    `settings.empty_result_grace_polls` consecutive empty responses.
    """

    retryable = True


class ApiUnauthorized(LejCcError):
    """401/403 -- the API key is wrong, expired or rotated."""

    global_failure = True


class ApiUnavailable(LejCcError):
    """5xx, timeout, or connection error. Transient; back off and retry."""

    retryable = True


class UnexpectedPayload(LejCcError):
    """200 OK but the body is not an xlsx (HTML error page, JSON error, empty)."""

    retryable = True


# --- data errors --------------------------------------------------------


class SchemaDrift(LejCcError):
    """A required column vanished or was renamed. Someone changed the export."""

    global_failure = True


class MawbMismatch(LejCcError):
    """The sheet's MAWB column does not match the AWB we asked for."""
