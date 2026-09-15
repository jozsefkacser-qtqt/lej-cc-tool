"""Which code is actually running.

The question that matters is not the package version but whether the
process is still the code that is checked out: pulling and forgetting to
restart is the normal way to debug a bug that was fixed hours ago.
"""

from __future__ import annotations

from lej_cc import version


def test_current_reads_the_checkout():
    found = version.current()
    assert found.commit
    assert isinstance(found.dirty, bool)
    assert str(found)


def test_it_degrades_off_a_checkout(monkeypatch):
    monkeypatch.setattr(version, "_git", lambda *args: None)
    found = version.current()
    assert found.commit == "unknown"
    assert version.__name__  # still renders something
    assert str(found).startswith("v")


def test_status_names_the_running_version(tmp_path):
    from lej_cc.config import Settings
    from lej_cc.formatting import build_status_report
    from lej_cc.health import Health

    settings = Settings(
        slack_bot_token="x",
        slack_app_token="x",
        portground_api_key="k",
        database_path=tmp_path / "j.sqlite3",
    )
    running = version.Version(commit="abc1234", committed_at="15 Sep 09:00", dirty=False)
    text = build_status_report(Health(), [], settings, running)[0]["text"]["text"]
    assert "abc1234" in text


def test_status_warns_when_a_newer_version_is_checked_out(tmp_path, monkeypatch):
    """The exact confusion this exists for: code pulled, bot not restarted."""
    from lej_cc.config import Settings
    from lej_cc.formatting import build_status_report
    from lej_cc.health import Health

    settings = Settings(
        slack_bot_token="x",
        slack_app_token="x",
        portground_api_key="k",
        database_path=tmp_path / "j.sqlite3",
    )
    stale = version.Version(commit="old1111", committed_at="11 Sep 06:41", dirty=False)
    monkeypatch.setattr(
        version,
        "current",
        lambda: version.Version(commit="new2222", committed_at="15 Sep 09:00", dirty=False),
    )

    text = build_status_report(Health(), [], settings, stale)[0]["text"]["text"]
    assert "new2222" in text and "Restart to pick it up" in text
