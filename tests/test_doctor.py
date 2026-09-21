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


# --- the sheet ----------------------------------------------------------


def _sheet_settings(tmp_path, **kwargs):
    from lej_cc.config import Settings

    return Settings(
        slack_bot_token="xoxb-a",
        slack_app_token="xapp-a",
        portground_api_key="k",
        database_path=tmp_path / "x.sqlite3",
        download_dir=tmp_path / "downloads",
        **kwargs,
    )


def test_sheet_check_is_quiet_when_the_export_is_off(tmp_path):
    result = doctor.check_sheet(_sheet_settings(tmp_path))
    assert not result.failed
    assert "off" in result.detail


def test_missing_credentials_file_is_named(tmp_path):
    settings = _sheet_settings(
        tmp_path,
        google_sheet_id="sheet-abc",
        google_credentials_file=tmp_path / "nope.json",
    )
    result = doctor.check_sheet(settings)
    assert result.failed
    assert "nope.json" in result.detail


def test_a_404_is_reported_as_wrong_id_or_not_shared(tmp_path, monkeypatch):
    """The two causes look identical from the API and need different fixes."""
    key = tmp_path / "sa.json"
    key.write_text("{}")
    settings = _sheet_settings(
        tmp_path, google_sheet_id="sheet-abc", google_credentials_file=key
    )

    def boom(self):
        raise RuntimeError("<HttpError 404 when requesting ...>")

    monkeypatch.setattr("lej_cc.sheets.SheetExporter.describe", boom)
    result = doctor.check_sheet(settings)

    assert result.failed
    assert "shared with the service account" in result.detail


def test_pointing_at_a_hand_maintained_report_warns(tmp_path, monkeypatch):
    """A GOOGLE_SHEET_ID left on a live report is the mistake worth catching."""
    key = tmp_path / "sa.json"
    key.write_text("{}")
    settings = _sheet_settings(
        tmp_path, google_sheet_id="sheet-abc", google_credentials_file=key
    )
    monkeypatch.setattr(
        "lej_cc.sheets.SheetExporter.describe",
        lambda self: ("Daily Report_LEJ", {"W33", "W34"}),
    )
    result = doctor.check_sheet(settings)

    assert result.status is doctor.WARN
    assert not result.failed  # a warning, not a stop
    assert "CENTRAL-SHEET.md" in result.detail


def test_the_central_sheet_passes(tmp_path, monkeypatch):
    key = tmp_path / "sa.json"
    key.write_text("{}")
    settings = _sheet_settings(
        tmp_path, google_sheet_id="sheet-abc", google_credentials_file=key
    )
    monkeypatch.setattr(
        "lej_cc.sheets.SheetExporter.describe",
        lambda self: ("CC Bot Central", {"CC_BOT"}),
    )
    result = doctor.check_sheet(settings)

    assert not result.failed
    assert result.status is doctor.OK
    assert "CC Bot Central" in result.detail


def test_a_tab_that_does_not_exist_yet_is_not_a_failure(tmp_path, monkeypatch):
    key = tmp_path / "sa.json"
    key.write_text("{}")
    settings = _sheet_settings(
        tmp_path, google_sheet_id="sheet-abc", google_credentials_file=key
    )
    monkeypatch.setattr(
        "lej_cc.sheets.SheetExporter.describe",
        lambda self: ("CC Bot Central", {"Sheet1"}),
    )
    result = doctor.check_sheet(settings)

    assert not result.failed
    assert "created on first write" in result.detail


def test_a_missing_google_client_says_how_to_install_it(tmp_path, monkeypatch):
    """The client is an optional extra; "No module named 'google'" is not a fix."""
    key = tmp_path / "sa.json"
    key.write_text("{}")
    settings = _sheet_settings(
        tmp_path, google_sheet_id="sheet-abc", google_credentials_file=key
    )

    def missing(self):
        raise ModuleNotFoundError("No module named 'google'")

    monkeypatch.setattr("lej_cc.sheets.SheetExporter.describe", missing)
    result = doctor.check_sheet(settings)

    assert result.failed
    # The command must name the virtualenv's pip: a plain `pip install` from
    # an unactivated shell lands in the system Python and changes nothing.
    assert ".venv/bin/pip install -e '.[google]'" in result.detail
    assert "No module named" not in result.detail


# --- awb track ------------------------------------------------------------


def test_track_is_ok_with_a_channel_and_no_report(tmp_path):
    settings = _sheet_settings(tmp_path, slack_status_channel="C0C05GFT40H")
    result = doctor.check_track(settings)

    assert result.status is doctor.OK
    assert "C0C05GFT40H" in result.detail
    assert "--from-file" in result.detail


def test_a_missing_channel_warns_and_names_the_watched_one(tmp_path):
    """The exact stumble this check exists to prevent."""
    settings = _sheet_settings(tmp_path, autodetect_channels="C0C05GFT40H")
    result = doctor.check_track(settings)

    assert result.status is doctor.WARN
    assert not result.failed  # track still runs with --channel
    assert "SLACK_STATUS_CHANNEL" in result.detail
    assert "C0C05GFT40H" in result.detail


def test_with_several_watched_channels_none_is_named(tmp_path):
    settings = _sheet_settings(tmp_path, autodetect_channels="C1,C2")
    assert "names" not in doctor.check_track(settings).detail


def test_a_report_id_without_credentials_fails(tmp_path):
    settings = _sheet_settings(
        tmp_path, slack_status_channel="C1", report_sheet_id="report-abc"
    )
    result = doctor.check_track(settings)

    assert result.failed
    assert "GOOGLE_CREDENTIALS_FILE" in result.detail


def test_an_unreadable_report_says_wrong_id_or_not_shared(tmp_path, monkeypatch):
    key = tmp_path / "sa.json"
    key.write_text("{}")
    settings = _sheet_settings(
        tmp_path,
        slack_status_channel="C1",
        report_sheet_id="report-abc",
        google_credentials_file=key,
    )

    def boom(*_args, **_kwargs):
        raise RuntimeError("<HttpError 404 when requesting ...>")

    monkeypatch.setattr("lej_cc.sheets.describe_spreadsheet", boom)
    result = doctor.check_track(settings)

    assert result.failed
    assert "Reader is enough" in result.detail


def test_a_readable_report_names_the_tab_to_pass(tmp_path, monkeypatch):
    """Knowing the tab exists is most of what --tab gets wrong."""
    key = tmp_path / "sa.json"
    key.write_text("{}")
    settings = _sheet_settings(
        tmp_path,
        slack_status_channel="C1",
        report_sheet_id="report-abc",
        google_credentials_file=key,
    )
    monkeypatch.setattr(
        "lej_cc.sheets.describe_spreadsheet",
        lambda *a, **k: ("Daily Report_LEJ", {"2026.08", "2026.09", "TEMPLATE", "SLAs"}),
    )
    result = doctor.check_track(settings)

    assert result.status is doctor.OK
    assert "Daily Report_LEJ" in result.detail
    assert "--tab 2026.09" in result.detail


def test_a_missing_google_client_is_reported_here_too(tmp_path, monkeypatch):
    key = tmp_path / "sa.json"
    key.write_text("{}")
    settings = _sheet_settings(
        tmp_path,
        slack_status_channel="C1",
        report_sheet_id="report-abc",
        google_credentials_file=key,
    )

    def missing(*_args, **_kwargs):
        raise ModuleNotFoundError("No module named 'google'")

    monkeypatch.setattr("lej_cc.sheets.describe_spreadsheet", missing)
    result = doctor.check_track(settings)

    assert result.failed
    assert ".venv/bin/pip install -e '.[google]'" in result.detail


def test_the_latest_month_tab_is_chosen_chronologically():
    from lej_cc.bulk import latest_month_tab

    assert latest_month_tab({"2026.09", "2026.12", "2027.01"}) == "2027.01"
    assert latest_month_tab({"TEMPLATE", "SLAs"}) is None
