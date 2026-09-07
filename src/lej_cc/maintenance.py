"""Housekeeping: keep the downloads directory from growing without bound.

A busy AWB is polled roughly 96 times over its 48-hour window, and each
workbook can be ~70 KB, so an unattended instance accumulates steadily.
That matters on a free-tier VM with a small disk.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)


def purge_old_downloads(directory: Path, retention_days: int) -> int:
    """Delete workbooks older than `retention_days`. Returns how many went.

    A retention of 0 disables cleanup. Files that vanish underneath us (a
    concurrent purge, a manual delete) are ignored rather than raising.
    """
    if retention_days <= 0 or not directory.exists():
        return 0

    cutoff = time.time() - retention_days * 86400
    removed = 0
    for path in directory.glob("*.xlsx"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError as exc:  # already gone, or permissions
            log.debug("could not remove %s: %s", path, exc)

    if removed:
        log.info("purged %d download(s) older than %d days", removed, retention_days)
    return removed
