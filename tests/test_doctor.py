from lej_cc import doctor


def test_tokens_are_masked_never_printed():
    masked = doctor._mask("xoxb-super-secret-value-1234567890")
    assert "secret" not in masked
    assert masked.startswith("xoxb-sup")


def test_empty_token_is_labelled():
    assert doctor._mask("") == "(empty)"


def test_swapped_slack_tokens_are_caught(tmp_path):
    """The most common setup mistake: bot and app tokens the wrong way round."""
    from lej_cc.config import Settings

    settings = Settings(
        slack_bot_token="xapp-this-is-the-app-token",
        slack_app_token="xoxb-this-is-the-bot-token",
        portground_api_key="k",
        database_path=tmp_path / "x.sqlite3",
        download_dir=tmp_path / "downloads",
    )
    results = doctor.check_tokens(settings)
    assert [r.failed for r in results] == [True, True, False]
    assert "xoxb-" in results[0].detail


def test_unwritable_storage_is_reported(tmp_path):
    from lej_cc.config import Settings

    blocked = tmp_path / "blocked"
    blocked.write_text("this is a file, not a directory")
    settings = Settings(
        slack_bot_token="xoxb-a",
        slack_app_token="xapp-a",
        portground_api_key="k",
        database_path=blocked / "sub" / "x.sqlite3",
        download_dir=tmp_path / "downloads",
    )
    results = doctor.check_storage(settings)
    assert results[0].failed
    assert not results[1].failed


def test_a_mailbox_without_an_allowlist_fails_the_preflight(tmp_path):
    """Configured but unusable is the failure that is hardest to notice."""
    from lej_cc.config import Settings

    settings = Settings(
        slack_bot_token="xoxb-a",
        slack_app_token="xapp-a",
        portground_api_key="k",
        database_path=tmp_path / "x.sqlite3",
        download_dir=tmp_path / "downloads",
        imap_host="imap.example.com",
        imap_user="awb@qtlogistics.eu",
    )
    result = doctor.check_inbox(settings)
    assert result.failed
    assert "IMAP_ALLOWED_SENDERS" in result.detail


def test_no_mailbox_configured_is_not_a_failure(tmp_path):
    from lej_cc.config import Settings

    settings = Settings(
        slack_bot_token="xoxb-a",
        slack_app_token="xapp-a",
        portground_api_key="k",
        database_path=tmp_path / "x.sqlite3",
        download_dir=tmp_path / "downloads",
    )
    assert not doctor.check_inbox(settings).failed


# --- the auto-detect channel --------------------------------------------


class FakeSlack:
    """Stands in for WebClient; fails conversations_history on demand."""

    def __init__(self, errors: dict[str, str] | None = None) -> None:
        self.errors = errors or {}
        self.read: list[str] = []

    def __call__(self, token=None):  # noqa: ANN001 - used as the class itself
        return self

    def conversations_history(self, channel, limit=1):  # noqa: ANN001
        from slack_sdk.errors import SlackApiError

        if channel in self.errors:
            raise SlackApiError("no", {"ok": False, "error": self.errors[channel]})
        self.read.append(channel)
        return {"ok": True, "messages": []}


def _settings(tmp_path, channels: str):
    from lej_cc.config import Settings

    return Settings(
        slack_bot_token="xoxb-a",
        slack_app_token="xapp-a",
        portground_api_key="k",
        database_path=tmp_path / "x.sqlite3",
        download_dir=tmp_path / "downloads",
        autodetect_channels=channels,
    )


def test_auto_detect_off_is_not_a_problem(tmp_path):
    assert not doctor.check_autodetect(_settings(tmp_path, "")).failed


def test_a_reachable_channel_passes(tmp_path, monkeypatch):
    import slack_sdk

    fake = FakeSlack()
    monkeypatch.setattr(slack_sdk, "WebClient", fake)
    result = doctor.check_autodetect(_settings(tmp_path, "C0AWB, C0LEJ"))

    assert not result.failed
    assert fake.read == ["C0AWB", "C0LEJ"]


def test_a_placeholder_channel_id_is_caught_before_restarting(tmp_path, monkeypatch):
    """The real case: C0XXXXXXX went into .env and only showed up as a log
    line after a restart."""
    import slack_sdk

    monkeypatch.setattr(
        slack_sdk, "WebClient", FakeSlack({"C0XXXXXXX": "channel_not_found"})
    )
    result = doctor.check_autodetect(_settings(tmp_path, "C0XXXXXXX"))

    assert result.failed
    assert "Copy link" in result.detail  # says where to find the real one


def test_a_channel_the_bot_is_not_in_says_to_invite(tmp_path, monkeypatch):
    import slack_sdk

    monkeypatch.setattr(slack_sdk, "WebClient", FakeSlack({"C0AWB": "not_in_channel"}))
    result = doctor.check_autodetect(_settings(tmp_path, "C0AWB"))

    assert result.failed
    assert "/invite" in result.detail


def test_a_missing_scope_says_to_reinstall(tmp_path, monkeypatch):
    import slack_sdk

    monkeypatch.setattr(slack_sdk, "WebClient", FakeSlack({"C0AWB": "missing_scope"}))
    result = doctor.check_autodetect(_settings(tmp_path, "C0AWB"))

    assert result.failed
    assert "Reinstall" in result.detail


def test_every_bad_channel_is_named_not_just_the_first(tmp_path, monkeypatch):
    import slack_sdk

    monkeypatch.setattr(
        slack_sdk,
        "WebClient",
        FakeSlack({"C0AAA": "channel_not_found", "C0BBB": "not_in_channel"}),
    )
    result = doctor.check_autodetect(_settings(tmp_path, "C0AAA,C0BBB"))

    assert "C0AAA" in result.detail and "C0BBB" in result.detail
