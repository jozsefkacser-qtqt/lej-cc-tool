"""Configuration, loaded from environment / .env.

Nothing secret is ever defaulted in code. The PortGround key is a bearer
credential that travels in a URL query string, so it is also scrubbed from
every log line (see `redact`).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    # --- Slack ---
    slack_bot_token: str = Field(..., description="xoxb-… bot token")
    slack_app_token: str = Field(..., description="xapp-… app token for Socket Mode")
    #: Channel that gets alerted about global failures (bad key, schema drift).
    slack_ops_channel: str = ""

    # --- PortGround ---
    portground_api_key: str = Field(..., description="API key for the status export")
    portground_base_url: str = "https://ecommerce.portground.com"
    #: The export is generated on demand and is slow: a 225-row workbook
    #: measured 112 s from a client in Germany, and larger master AWBs take
    #: longer still. Anything under a couple of minutes fails on healthy calls.
    http_timeout_seconds: float = 300.0

    # --- polling schedule ---
    #: Delay before the second poll. The first poll happens immediately.
    first_interval_minutes: int = 15
    #: Steady-state cadence after that.
    repeat_interval_minutes: int = 30
    #: Give up on an AWB that is still incomplete after this long.
    max_tracking_hours: int = 48
    #: Consecutive empty results tolerated before concluding the AWB is unknown.
    empty_result_grace_polls: int = 3
    #: Consecutive API failures before the job stops and reports.
    max_consecutive_failures: int = 5
    #: Post a thread update even when nothing changed?
    post_unchanged_updates: bool = False
    #: Attach the workbook only when something changed (plus first + final).
    attach_file_on_change_only: bool = True
    #: List open HAWBs in the message only while there are at most this many.
    #: Beyond it the numbers are a wall of text nobody reads, and the chase
    #: sheet is the better answer.
    inline_list_threshold: int = 10
    #: Attach the short "still open" workbook: only open lines, only the
    #: columns needed to chase them, oldest first.
    attach_open_summary: bool = True
    #: Also attach PortGround's original 17-column export as the audit trail.
    attach_full_workbook: bool = True

    # --- storage ---
    database_path: Path = Path("data/lej_cc.sqlite3")
    download_dir: Path = Path("data/downloads")
    #: Delete downloaded workbooks older than this. 0 disables cleanup.
    download_retention_days: int = 30

    # --- Google Workspace (optional; archive + dashboard) ---
    google_credentials_file: Path | None = None
    google_drive_folder_id: str = ""
    google_sheet_id: str = ""

    status_map_path: Path | None = None
    log_level: str = "INFO"

    def download_url(self, mawb: str) -> str:
        return (
            f"{self.portground_base_url.rstrip('/')}"
            f"/api/status/air-waybills/{mawb}/download?apiKey={self.portground_api_key}"
        )

    def redact(self, text: str) -> str:
        """Strip the API key out of anything heading for a log or Slack."""
        if not self.portground_api_key:
            return text
        return text.replace(self.portground_api_key, "***")


_SECRET_PATTERNS = (
    # The PortGround key travels in the query string, so any library that
    # logs a request URL logs the credential with it.
    re.compile(r"(apiKey=)[^&\s\"\']+"),
    # Slack tokens, in case a client ever echoes one into an error.
    re.compile(r"\b(xox[baprs]-)[A-Za-z0-9-]+"),
    re.compile(r"\b(xapp-)[A-Za-z0-9-]+"),
)


def scrub(text: str) -> str:
    """Redact credentials from anything heading for a log or a message."""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(r"\1***", text)
    return text


class RedactingFilter(logging.Filter):
    """Scrubs secrets out of every log record, whoever emitted it.

    httpx logs the full request URL at INFO, which for this API means the
    key. Filtering centrally is the only version of this that stays true as
    dependencies change -- scrubbing at each call site does not.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - never lose a line to a format error
            return True
        cleaned = scrub(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = ()
        return True


def configure_logging(level: str = "INFO") -> None:
    """Set up logging with credential redaction on every handler."""
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)-20s %(message)s",
    )
    redactor = RedactingFilter()
    for handler in logging.getLogger().handlers:
        handler.addFilter(redactor)

    # Quiet the request-level chatter: the useful line is our own, which
    # reports size and duration without the URL.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
