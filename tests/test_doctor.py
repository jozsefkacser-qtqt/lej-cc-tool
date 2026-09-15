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
