"""Client for the PortGround shipment-status export.

    GET /api/status/air-waybills/{MAWB}/download?apiKey=…   ->  .xlsx

The key travels as a query parameter, so no URL is ever logged unscrubbed.
Every failure is translated into the typed errors in `errors.py`, which is
what lets the scheduler decide retry-vs-stop without inspecting HTTP codes.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from pathlib import Path

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .config import Settings, scrub
from .errors import ApiUnauthorized, ApiUnavailable, AwbNotFound, UnexpectedPayload

log = logging.getLogger(__name__)

#: xlsx files are zip archives; anything else is an error page in disguise.
ZIP_MAGIC = b"PK\x03\x04"

_FILENAME_RE = re.compile(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', re.IGNORECASE)


class PortGroundClient:
    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self.settings = settings
        self._client = client or httpx.Client(
            timeout=settings.http_timeout_seconds,
            follow_redirects=True,
            headers={"Accept": "*/*", "User-Agent": "lej-cc-tool/0.1"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PortGroundClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @retry(
        retry=retry_if_exception_type(ApiUnavailable),
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        reraise=True,
    )
    def download(self, mawb: str, dest_dir: Path) -> tuple[Path, str]:
        """Download the workbook for `mawb`. Returns (local path, server filename).

        Retries transient failures three times with backoff; anything else is
        raised immediately so the caller can report it to Slack.
        """
        url = self.settings.download_url(mawb)
        started = time.monotonic()
        try:
            response = self._client.get(url)
        except httpx.TimeoutException as exc:
            raise ApiUnavailable(
                f"timeout fetching {mawb}",
                user_message="PortGround didn't respond in time. Retrying at the next check.",
            ) from exc
        except httpx.HTTPError as exc:
            raise ApiUnavailable(
                f"connection error fetching {mawb}: {scrub(str(exc))}",
                user_message="Couldn't reach PortGround. Retrying at the next check.",
            ) from exc

        self._raise_for_status(response, mawb)

        body = response.content
        if not body:
            raise UnexpectedPayload(
                f"empty body for {mawb}",
                user_message="PortGround returned an empty response.",
            )
        if not body.startswith(ZIP_MAGIC):
            preview = body[:200].decode("utf-8", "replace")
            raise UnexpectedPayload(
                f"non-xlsx body for {mawb}: {scrub(preview)}",
                user_message=(
                    "PortGround returned something that isn't an Excel file "
                    "— the service may be having problems."
                ),
            )

        filename = self._server_filename(response, mawb)
        dest_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = dest_dir / f"{mawb}_{stamp}.xlsx"
        path.write_bytes(body)
        log.info(
            "downloaded %s (%d bytes in %.1fs) -> %s",
            mawb, len(body), time.monotonic() - started, path,
        )
        return path, filename

    @staticmethod
    def _raise_for_status(response: httpx.Response, mawb: str) -> None:
        code = response.status_code
        if code == 200:
            return
        if code in (401, 403):
            raise ApiUnauthorized(
                f"HTTP {code} for {mawb} — API key rejected",
                user_message=(
                    "PortGround rejected our API key (HTTP "
                    f"{code}). Tracking is paused until the key is renewed."
                ),
            )
        if code == 404:
            raise AwbNotFound(
                f"HTTP 404 for {mawb}",
                user_message="PortGround doesn't know this MAWB (HTTP 404).",
            )
        if code == 429:
            raise ApiUnavailable(
                f"rate limited on {mawb}",
                user_message="PortGround is rate-limiting us. Backing off.",
            )
        if code >= 500:
            raise ApiUnavailable(
                f"HTTP {code} for {mawb}",
                user_message=f"PortGround returned a server error (HTTP {code}). Will retry.",
            )
        raise UnexpectedPayload(
            f"HTTP {code} for {mawb}",
            user_message=f"Unexpected response from PortGround (HTTP {code}).",
        )

    @staticmethod
    def _server_filename(response: httpx.Response, mawb: str) -> str:
        """PortGround names files shipment_status_{MAWB}_{date}_{H}_{M}_{S}.xlsx."""
        disposition = response.headers.get("content-disposition", "")
        match = _FILENAME_RE.search(disposition)
        if match:
            return Path(match.group(1)).name
        return f"shipment_status_{mawb}_{datetime.now():%Y%m%d_%H_%M_%S}.xlsx"
