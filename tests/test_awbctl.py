"""The restart shortcut.

Bash, so this drives the real script against a fake bot: a stand-in binary
that logs the same "ready" line and honours SIGTERM. The failures worth
catching here are the ones that would be discovered in production at 19:44 --
a restart that kills itself, or a start that quietly leaves two bots posting
to Slack.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

FAKE_BOT = """#!/usr/bin/env bash
# Stands in for the real bot: says it is ready, then waits to be told to stop.
trap 'echo "shutting down"; sleep 0.2; echo "bye"; exit 0' TERM
echo "starting up"
sleep 0.3
echo "lej-cc-tool ready"
while true; do sleep 0.2; done
"""

STUBBORN_BOT = """#!/usr/bin/env bash
# Ignores SIGTERM, the way a wedged process does.
trap '' TERM
echo "lej-cc-tool ready"
while true; do sleep 0.2; done
"""

CRASHING_BOT = """#!/usr/bin/env bash
echo "pydantic_core._pydantic_core.ValidationError: SLACK_BOT_TOKEN missing" >&2
exit 1
"""


@pytest.fixture
def install(tmp_path):
    """A throwaway checkout: the real script, a fake venv, a fake .env."""

    def _install(bot: str = FAKE_BOT) -> tuple[Path, dict]:
        root = tmp_path / "lej-cc-tool"
        (root / "deploy").mkdir(parents=True)
        (root / ".venv" / "bin").mkdir(parents=True)
        shutil.copy(REPO / "deploy" / "awbctl", root / "deploy" / "awbctl")
        binary = root / ".venv" / "bin" / "lej-cc"
        binary.write_text(bot)
        binary.chmod(0o755)
        (root / ".env").write_text("SLACK_BOT_TOKEN=x\n")
        env = {
            **os.environ,
            "HOME": str(tmp_path),
            "LEJ_CC_LOG": str(tmp_path / "bot.log"),
            "LEJ_CC_STOP_TIMEOUT": "3",
            "LEJ_CC_START_TIMEOUT": "10",
        }
        return root, env

    return _install


def run(root: Path, env: dict, *args: str, timeout: int = 40):
    return subprocess.run(
        [str(root / "deploy" / "awbctl"), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )


def pids_running(root: Path) -> list[str]:
    found = subprocess.run(
        ["pgrep", "-f", f"{root}/.venv/bin/lej-cc( |$)"], capture_output=True, text=True
    )
    return [p for p in found.stdout.split() if p]


@pytest.fixture
def cleanup():
    started: list[Path] = []
    yield started
    for root in started:
        for pid in pids_running(root):
            subprocess.run(["kill", "-9", pid], capture_output=True)


# --- the everyday path ---------------------------------------------------


def test_start_waits_until_it_is_really_up(install, cleanup):
    root, env = install()
    cleanup.append(root)

    result = run(root, env, "start")
    assert result.returncode == 0, result.stderr
    assert "running" in result.stdout
    assert len(pids_running(root)) == 1
    # Nothing on stderr. A stray shell error here is how the first-ever start
    # failed on one bash version and passed on another: the log file did not
    # exist yet, and a failed input redirect is reported before the
    # 2>/dev/null on the same line applies.
    assert result.stderr == ""


def test_the_very_first_start_has_no_log_to_read(install, cleanup):
    root, env = install()
    cleanup.append(root)
    assert not Path(env["LEJ_CC_LOG"]).exists()

    result = run(root, env, "start")

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


def test_status_knows_the_difference(install, cleanup):
    root, env = install()
    cleanup.append(root)

    assert "not running" in run(root, env, "status").stdout
    run(root, env, "start")
    assert "running" in run(root, env, "status").stdout
    assert "not running" not in run(root, env, "status").stdout


def test_restart_leaves_exactly_one_running(install, cleanup):
    """The bug this guards: `pkill -f lej-cc` matches the script's own command
    line, so the restart kills itself and the bot never comes back."""
    root, env = install()
    cleanup.append(root)

    run(root, env, "start")
    first = pids_running(root)
    result = run(root, env, "restart")

    assert result.returncode == 0, result.stderr
    second = pids_running(root)
    assert len(second) == 1
    assert second != first  # it really was restarted


def test_stopping_is_graceful(install, cleanup):
    """SIGTERM, not SIGKILL: the bot drains its polls and says goodbye in Slack."""
    root, env = install()
    cleanup.append(root)

    run(root, env, "start")
    run(root, env, "stop")

    assert pids_running(root) == []
    assert "bye" in (Path(env["LEJ_CC_LOG"])).read_text()


def test_stopping_twice_is_harmless(install, cleanup):
    root, env = install()
    cleanup.append(root)
    run(root, env, "start")
    run(root, env, "stop")
    assert run(root, env, "stop").returncode == 0


def test_a_long_checkout_path_does_not_look_dead(tmp_path, cleanup):
    """`ps -o args=` cuts at 80 columns when stdout is not a terminal.

    The liveness check used to grep that output for "lej-cc", so a checkout
    deep enough to push the name past the cut made a running bot look dead:
    status said "not running", and start would then have started a second
    one -- two bots, every Slack message doubled. CI found it because this
    file's longest test name made pytest's own temp path long enough.
    """
    deep = tmp_path
    for part in ("a_directory_with_quite_a_long_name", "and_another_one_here"):
        deep = deep / part
    root = deep / "lej-cc-tool"
    (root / "deploy").mkdir(parents=True)
    (root / ".venv" / "bin").mkdir(parents=True)
    shutil.copy(REPO / "deploy" / "awbctl", root / "deploy" / "awbctl")
    binary = root / ".venv" / "bin" / "lej-cc"
    binary.write_text(FAKE_BOT)
    binary.chmod(0o755)
    (root / ".env").write_text("SLACK_BOT_TOKEN=x\n")
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "LEJ_CC_LOG": str(tmp_path / "bot.log"),
        "LEJ_CC_START_TIMEOUT": "10",
        "LEJ_CC_STOP_TIMEOUT": "3",
    }
    cleanup.append(root)
    assert len(f"/usr/bin/env bash {binary}") > 80  # past where ps would cut

    assert run(root, env, "start").returncode == 0
    assert "not running" not in run(root, env, "status").stdout
    assert len(pids_running(root)) == 1


# --- the ways it can go wrong -------------------------------------------


def test_a_second_start_does_not_start_a_second_bot(install, cleanup):
    """Two bots would double every message in the channel."""
    root, env = install()
    cleanup.append(root)

    run(root, env, "start")
    result = run(root, env, "start")

    assert "already running" in result.stdout
    assert len(pids_running(root)) == 1


def test_a_bot_started_by_hand_is_adopted_not_duplicated(install, cleanup):
    """Exactly how it is running right now: nohup, no pid file."""
    root, env = install()
    cleanup.append(root)

    subprocess.Popen(
        [str(root / ".venv" / "bin" / "lej-cc")],
        stdout=open(env["LEJ_CC_LOG"], "a"),
        stderr=subprocess.STDOUT,
    )
    time.sleep(1)
    assert len(pids_running(root)) == 1

    result = run(root, env, "start")
    assert "already running" in result.stdout
    assert len(pids_running(root)) == 1


def test_a_wedged_process_is_forced(install, cleanup):
    root, env = install(STUBBORN_BOT)
    cleanup.append(root)

    run(root, env, "start")
    result = run(root, env, "stop")

    assert "forcing it" in result.stdout
    assert pids_running(root) == []


def test_a_bot_that_dies_at_startup_shows_why(install, cleanup):
    """Silence on a failed start is the thing that wastes an afternoon."""
    root, env = install(CRASHING_BOT)
    cleanup.append(root)

    result = run(root, env, "start")

    assert result.returncode != 0
    assert "SLACK_BOT_TOKEN missing" in result.stdout
    assert not (root / "data" / "lej-cc.pid").exists()


def test_a_missing_venv_says_how_to_fix_it(install):
    root, env = install()
    (root / ".venv" / "bin" / "lej-cc").unlink()

    result = run(root, env, "start")
    assert result.returncode != 0
    assert "make dev" in result.stderr


def test_a_missing_env_file_says_how_to_fix_it(install):
    root, env = install()
    (root / ".env").unlink()

    result = run(root, env, "start")
    assert result.returncode != 0
    assert "cp .env.example .env" in result.stderr


def test_an_unknown_command_is_refused(install):
    root, env = install()
    result = run(root, env, "reboot")
    assert result.returncode != 0
    assert "unknown command" in result.stderr


# --- installing the shortcut --------------------------------------------


def test_install_adds_the_alias_once(install, tmp_path):
    root, env = install()
    rc = tmp_path / ".bashrc"
    rc.write_text("# existing content\n")

    run(root, env, "install")
    run(root, env, "install")

    assert rc.read_text().count("alias awb=") == 1
    assert "# existing content" in rc.read_text()
    assert str(root / "deploy" / "awbctl") in rc.read_text()


def test_install_replaces_an_alias_from_another_checkout(install, tmp_path):
    root, env = install()
    rc = tmp_path / ".bashrc"
    rc.write_text("alias awb='/old/path/awbctl'\n")

    run(root, env, "install")

    assert "/old/path/awbctl" not in rc.read_text()
    assert rc.read_text().count("alias awb=") == 1


def test_installing_from_a_second_checkout_leaves_one_block(install, tmp_path):
    root, env = install()
    rc = tmp_path / ".bashrc"

    run(root, env, "install")
    run(root, env, "install")
    run(root, env, "install")

    body = rc.read_text()
    assert body.count("# >>> AWB customs tracker >>>") == 1
    assert sum(line.endswith("autostart || true") for line in body.splitlines()) == 1


def test_install_wires_up_autostart_safely(install, tmp_path):
    """It runs on every terminal, so it must be interactive-only and never fatal."""
    root, env = install()
    rc = tmp_path / ".bashrc"

    run(root, env, "install")
    line = next(line for line in rc.read_text().splitlines() if "autostart" in line)

    assert "$- == *i*" in line  # not in scripts, not in scp
    assert "-x" in line  # a moved checkout is skipped, not an error
    assert line.rstrip().endswith("|| true")  # never breaks the shell


def test_autostart_can_be_declined(install, tmp_path):
    root, env = install()
    rc = tmp_path / ".bashrc"

    run(root, env, "install", "--no-autostart")

    body = rc.read_text()
    assert "alias awb=" in body
    # Not a substring check: pytest's own temp directory is named after this
    # test, so the word appears in the path on the alias line.
    assert not any(line.endswith("autostart || true") for line in body.splitlines())


# --- autostart itself ----------------------------------------------------


def test_autostart_starts_it_when_it_is_not_running(install, cleanup):
    root, env = install()
    cleanup.append(root)

    result = run(root, env, "autostart")

    assert result.returncode == 0
    time.sleep(1)
    assert len(pids_running(root)) == 1
    assert "started it" in result.stdout


def test_autostart_is_silent_and_instant_when_it_is_running(install, cleanup):
    """It runs on every terminal open. A pause or a line of noise each time
    is how a helpful thing becomes the thing everybody deletes."""
    root, env = install()
    cleanup.append(root)
    run(root, env, "start")

    began = time.monotonic()
    result = run(root, env, "autostart")
    elapsed = time.monotonic() - began

    assert result.stdout == ""
    assert elapsed < 2
    assert len(pids_running(root)) == 1


def test_autostart_never_starts_a_second_bot(install, cleanup):
    root, env = install()
    cleanup.append(root)
    run(root, env, "start")

    for _ in range(3):
        run(root, env, "autostart")

    assert len(pids_running(root)) == 1


def test_autostart_says_nothing_when_there_is_nothing_to_start(install):
    """No venv yet, or no .env: opening a terminal must not complain."""
    root, env = install()
    (root / ".env").unlink()

    result = run(root, env, "autostart")

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""
