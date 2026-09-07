"""Configuration, loaded from environment / .env.

Nothing secret is ever defaulted in code. The PortGround key is a bearer
credential that travels in a URL query string, so it is also scrubbed from
every log line (see `redact`).
"""

from __future__ import annotations

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
    http_timeout_seconds: float = 60.0

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
    #: How many open HAWBs to name inline before deferring to the attachment.
    max_listed_hawbs: int = 15

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


_KEY_IN_URL = re.compile(r"(apiKey=)[^&\s]+")


def scrub(text: str) -> str:
    """Redact an apiKey query parameter regardless of its value."""
    return _KEY_IN_URL.sub(r"\1***", text)
