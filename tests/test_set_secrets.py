"""Setting secrets in .env.

Written after this script destroyed a credential: the Slack bot token was
pasted into the PortGround prompt -- the clipboard still held it -- and the
64-character API key was silently replaced by a 58-character Slack token
with nothing to recover it from.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

SLACK_BOT = "xoxb-" + "1" * 53          # 58 chars, the shape that caused it
SLACK_APP = "xapp-" + "2" * 93
PORTGROUND = "B" + "J" * 63             # 64 chars


@pytest.fixture
def project(tmp_path):
    """A throwaway checkout with a populated .env."""
    root = tmp_path / "lej-cc-tool"
    (root / "deploy").mkdir(parents=True)
    shutil.copy(REPO / "deploy" / "set-secrets.sh", root / "deploy" / "set-secrets.sh")
    (root / ".env").write_text(
        f"SLACK_BOT_TOKEN={SLACK_BOT}\n"
        f"SLACK_APP_TOKEN={SLACK_APP}\n"
        f"PORTGROUND_API_KEY={PORTGROUND}\n"
        "AUTODETECT_CHANNELS=C09ABCDEF\n"
    )
    return root


def run(root: Path, answers: list[str]):
    """Drive the prompts. One line per prompt, in order."""
    return subprocess.run(
        ["bash", str(root / "deploy" / "set-secrets.sh")],
        input="\n".join(answers) + "\n",
        capture_output=True,
        text=True,
        cwd=root,
        env={**os.environ, "HOME": str(root.parent)},
        timeout=30,
    )


def value_of(root: Path, key: str) -> str:
    for line in (root / ".env").read_text().splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1]
    return ""


# --- the mistake that prompted this -------------------------------------


def test_a_slack_token_cannot_land_in_the_portground_field(project):
    """The clipboard still holds the last thing you copied."""
    result = run(project, ["", "", SLACK_BOT, "", ""])

    assert value_of(project, "PORTGROUND_API_KEY") == PORTGROUND
    assert "is a Slack token" in result.stdout


def test_a_slack_token_cannot_land_in_the_mailbox_field(project):
    run(project, ["", "", "", "", SLACK_BOT])
    assert value_of(project, "IMAP_PASSWORD") == ""


def test_a_length_change_has_to_be_confirmed(project):
    """A 64-character key replaced by a 58-character one is the wrong clipboard
    far more often than it is a real rotation."""
    shorter = "C" * 58
    result = run(project, ["", "", shorter, "no", "", ""])

    assert value_of(project, "PORTGROUND_API_KEY") == PORTGROUND
    assert "this replaces a 64-character value" in result.stdout


def test_a_confirmed_length_change_is_written(project):
    longer = "D" * 70
    run(project, ["", "", longer, "yes", "", ""])
    assert value_of(project, "PORTGROUND_API_KEY") == longer


def test_a_same_length_replacement_needs_no_confirmation(project):
    """Rotating a key for one of the same shape is the ordinary case."""
    fresh = "E" * 64
    run(project, ["", "", fresh, "", ""])
    assert value_of(project, "PORTGROUND_API_KEY") == fresh


# --- the safety net ------------------------------------------------------


def test_the_previous_env_is_backed_up(project):
    run(project, ["", "", "", "", ""])

    backups = list(project.glob(".env.backup-*"))
    assert len(backups) == 1
    assert PORTGROUND in backups[0].read_text()
    assert backups[0].stat().st_mode & 0o077 == 0  # not readable by anyone else


def test_backups_do_not_pile_up_forever(project):
    """They are files full of credentials, not an archive."""
    for _ in range(8):
        run(project, ["", "", "", "", ""])
    assert len(list(project.glob(".env.backup-*"))) <= 5


def test_the_backup_path_is_printed(project):
    result = run(project, ["", "", "", "", ""])
    assert ".env.backup-" in result.stdout


# --- the ordinary paths still work --------------------------------------


def test_pressing_enter_keeps_everything(project):
    before = (project / ".env").read_text()
    run(project, ["", "", "", "", ""])
    assert (project / ".env").read_text() == before


def test_an_unrelated_setting_is_never_touched(project):
    run(project, ["", "", "E" * 64, "", ""])
    assert value_of(project, "AUTODETECT_CHANNELS") == "C09ABCDEF"


def test_a_wrong_slack_prefix_is_refused(project):
    result = run(project, ["not-a-token-but-long-enough-to-pass-the-length-check", "", "", "", ""])
    assert value_of(project, "SLACK_BOT_TOKEN") == SLACK_BOT
    assert "should start with 'xoxb-'" in result.stdout


def test_nothing_is_ever_echoed_back(project):
    """The whole reason input is hidden."""
    result = run(project, ["", "", "E" * 64, "", ""])
    assert "E" * 64 not in result.stdout
    assert SLACK_BOT not in result.stdout
    assert PORTGROUND not in result.stdout
