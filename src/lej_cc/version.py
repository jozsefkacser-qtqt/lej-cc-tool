"""What code is actually running.

"Which version is live?" is not answerable from a package version string
that nobody remembers to bump. It is answerable from git, and the bot runs
from a checkout, so it can simply look.

The more useful question is the one this also answers: whether the running
process is still the code that is checked out. Pulling and forgetting to
restart is the normal way to end up debugging a bug that was fixed hours
ago.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import __version__

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Version:
    commit: str
    committed_at: str
    dirty: bool
    package: str = __version__

    def __str__(self) -> str:
        suffix = " (uncommitted changes)" if self.dirty else ""
        if self.commit == "unknown":
            return f"v{self.package}{suffix}"
        return f"{self.commit} · {self.committed_at}{suffix}"


def _git(*args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), *args],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def current() -> Version:
    """Read the checked-out version. Falls back gracefully off a checkout."""
    commit = _git("rev-parse", "--short", "HEAD") or "unknown"
    committed_at = _git("log", "-1", "--format=%cd", "--date=format:%d %b %H:%M") or ""
    status = _git("status", "--porcelain")
    return Version(commit=commit, committed_at=committed_at, dirty=bool(status))
