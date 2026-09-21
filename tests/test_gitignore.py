"""Credentials must be invisible to git.

This is a behaviour test, not a string check: it asks git itself, because
the question that matters is "would `git add -A` stage this", and only git
answers that. It exists because `secrets/` was documented as ignored for a
while before it actually was, and the file it holds is a private key whose
filename looks harmless.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

#: Paths that must never reach a commit, relative to the repo root.
SECRETS = [
    ".env",
    ".env.backup-20260921",
    "secrets/service-account.json",
    "secrets/lej-cc-bot-f2116487ff2b.json",
    "lej-cc-bot-service-account.json",
    "config/key.pem",
    "certs/client.p12",
]


def _ignored(path: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(REPO), "check-ignore", "-q", path],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


@pytest.mark.parametrize("path", SECRETS)
def test_a_credential_is_ignored(path: str):
    if not (REPO / ".git").exists():
        pytest.skip("not a git checkout")
    assert _ignored(path), f"{path} would be committed — add it to .gitignore"


def test_ordinary_source_is_not_ignored():
    """A rule broad enough to hide real files would be its own bug."""
    if not (REPO / ".git").exists():
        pytest.skip("not a git checkout")
    for path in ("src/lej_cc/sheets.py", "tests/test_sheets.py", ".env.example"):
        assert not _ignored(path), f"{path} is ignored and should not be"
