"""Getting a command wrong, and being told so.

The bug these cover: someone typed `/ AWB 93602134215` -- one space after
the slash -- and Slack posted it as an ordinary message. Nothing tracked it,
nothing answered, and from where they were standing that is indistinguishable
from a bot that is switched off.
"""

from __future__ import annotations

import pytest
from slack_sdk.errors import SlackApiError

from lej_cc.slack_app import (
    BOTCHED_COMMAND,
    NEAR_MISSES,
    build_command_nudge,
    say_in_channel,
    strip_command_echo,
    usable_identifiers,
)

AWB = "93602134215"  # the real one from the report; its check digit passes


# --- recognising the attempt --------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        f"/ AWB {AWB}",  # exactly what was typed
        f"/ awb {AWB}",
        f"/awb {AWB}",  # pasted rather than typed, so posted as text
        f"/AWB {AWB}",
        f"  /  awb  {AWB}",
        f"\\awb {AWB}",  # the other slash, easy to hit on a German keyboard
        f"/mawb {AWB}",
        "/awb help",
    ],
)
def test_a_botched_command_is_recognised(text):
    assert BOTCHED_COMMAND.match(text) is not None


@pytest.mark.parametrize(
    "text",
    [
        f"{AWB} is still stuck in customs",  # ordinary talk about an AWB
        "can someone check /the awb please",
        "/cc @miroslav — this is a carbon copy, not an AWB command",
        "http://example.com/awb/936",
        "",
    ],
)
def test_ordinary_messages_are_left_alone(text):
    """Passive tracking of every message is still deliberately off."""
    assert BOTCHED_COMMAND.match(text) is None


def test_the_argument_survives_the_match():
    assert BOTCHED_COMMAND.match(f"/ awb {AWB}").group(1).strip() == AWB


# --- what the nudge says -------------------------------------------------


def test_the_nudge_shows_the_right_and_wrong_form():
    text, _ = build_command_nudge(AWB)
    assert "/awb 936-02134215" in text
    assert "space after the slash" in text


def test_a_usable_number_gets_a_one_click_button():
    _, blocks = build_command_nudge(f"{AWB} please")
    button = blocks[1]["elements"][0]
    assert button["action_id"] == "awb_track"
    assert button["value"] == AWB
    assert button["text"]["text"] == "Track 936-02134215"


def test_no_number_means_no_button_but_still_an_answer():
    text, blocks = build_command_nudge("help")
    assert len(blocks) == 1
    assert "/awb help" in text


def test_a_mistyped_number_does_not_offer_to_track_it():
    """A failed check digit is a typo, not something to start on one click."""
    _, blocks = build_command_nudge("936-02134210")
    assert len(blocks) == 1


# --- what counts as an identifier ---------------------------------------


def test_a_valid_awb_is_found():
    assert usable_identifiers(f"check {AWB} today") == [AWB]


def test_a_booking_reference_is_found():
    assert usable_identifiers("OyTM202608137666") == ["OyTM202608137666"]


def test_an_ordinary_word_is_not_a_booking_reference():
    """`/awb 936-… please check` used to start a 48-hour job called "please"."""
    assert usable_identifiers("please check urgently") == []


def test_an_email_address_is_not_an_identifier():
    assert usable_identifiers("miroslav@qtlogistics.eu") == []


def test_duplicates_collapse():
    assert usable_identifiers(f"{AWB} {AWB}") == [AWB]


# --- the label people repeat --------------------------------------------


@pytest.mark.parametrize(
    "typed",
    [f"AWB {AWB}", f"awb: {AWB}", f"MAWB {AWB}", f"Nr. {AWB}", f"no {AWB}"],
)
def test_a_repeated_label_is_stripped(typed):
    assert strip_command_echo(typed) == AWB


def test_a_bare_number_is_untouched():
    assert strip_command_echo(AWB) == AWB


# --- verbs the command does not have ------------------------------------


def test_near_misses_point_at_a_real_verb():
    assert NEAR_MISSES["ls"] == "list"
    assert NEAR_MISSES["cancel"] == "stop"
    assert NEAR_MISSES["health"] == "status"
    assert set(NEAR_MISSES.values()) <= {"list", "stop", "status", "stats"}


# --- a channel the bot cannot post in -----------------------------------


class FakeClient:
    def __init__(self, error: str | None = None) -> None:
        self.error = error
        self.posted: list[dict] = []

    def chat_postMessage(self, **kwargs):  # noqa: ANN003, N802
        if self.error:
            raise SlackApiError("nope", {"ok": False, "error": self.error})
        self.posted.append(kwargs)
        return {"ok": True}


class FakeRespond:
    def __init__(self) -> None:
        self.said: list = []

    def __call__(self, payload) -> None:  # noqa: ANN001
        self.said.append(payload)


def test_a_normal_post_reports_success():
    client, respond = FakeClient(), FakeRespond()
    assert say_in_channel(client, "C1", "hello", respond) is True
    assert client.posted[0]["channel"] == "C1"
    assert respond.said == []


@pytest.mark.parametrize("error", ["channel_not_found", "not_in_channel", "is_archived"])
def test_a_channel_we_cannot_post_in_says_how_to_fix_it(error):
    """Otherwise the confirmation lands and every update after it vanishes."""
    client, respond = FakeClient(error), FakeRespond()
    assert say_in_channel(client, "C1", "hello", respond) is False
    assert "/invite @AWB Tracker" in respond.said[0]


def test_an_unexpected_slack_error_is_not_swallowed():
    """A rate limit or a bad token is a different problem and must be seen."""
    client, respond = FakeClient("ratelimited"), FakeRespond()
    with pytest.raises(SlackApiError):
        say_in_channel(client, "C1", "hello", respond)


# --- auto-detect: a bare AWB in one dedicated channel -------------------


@pytest.fixture
def store(tmp_path):
    from lej_cc.store import JobStore

    return JobStore(tmp_path / "jobs.sqlite3")


def settings_with(**kwargs):
    from lej_cc.config import Settings

    base = dict(slack_bot_token="xoxb-x", slack_app_token="xapp-x", portground_api_key="k")
    return Settings(**{**base, **kwargs})


def test_auto_detect_is_off_everywhere_by_default():
    """A bot that reacts to numbers in a general channel is a bot people mute."""
    s = settings_with()
    assert s.autodetect_channel_ids == []
    assert not s.autodetect_in("C123")


def test_auto_detect_is_confined_to_the_named_channels():
    s = settings_with(autodetect_channels="C0AWB, C0LEJ")
    assert s.autodetect_in("C0AWB")
    assert s.autodetect_in("C0LEJ")
    assert not s.autodetect_in("C0GENERAL")


def test_a_bare_awb_is_picked_up(store):
    from lej_cc.slack_app import autodetect_targets

    fresh, seen = autodetect_targets(f"{AWB} came in on LH1234", "C1", store, 10)
    assert fresh == [AWB]
    assert seen == []


def test_a_number_that_fails_the_check_digit_is_ignored(store):
    """The checksum is the whole reason watching a channel is safe."""
    from lej_cc.slack_app import autodetect_targets

    assert autodetect_targets("invoice 93602134210 attached", "C1", store, 10) == ([], [])


def test_a_longer_number_is_not_mined_for_an_awb(store):
    """A 20-digit consignee tracking number contains 11-digit substrings."""
    from lej_cc.slack_app import autodetect_targets

    assert autodetect_targets("00340431234567890123", "C1", store, 10) == ([], [])


def test_an_already_tracked_awb_is_reported_separately(store):
    """People name the same AWB all day; a reply under each one is noise."""
    from lej_cc.slack_app import autodetect_targets

    store.create_job(AWB, "C1", "U1")
    fresh, seen = autodetect_targets(f"any news on {AWB}?", "C1", store, 10)
    assert fresh == []
    assert seen == [AWB]


def test_tracking_in_another_channel_does_not_count(store):
    from lej_cc.slack_app import autodetect_targets

    store.create_job(AWB, "C-OTHER", "U1")
    fresh, _ = autodetect_targets(AWB, "C1", store, 10)
    assert fresh == [AWB]


def test_a_pasted_manifest_cannot_queue_unlimited_work(store):
    from lej_cc.slack_app import autodetect_targets

    text = " ".join(["93602134215", "48820744846", "93600333955", "93602927993"])
    fresh, _ = autodetect_targets(text, "C1", store, 2)
    assert len(fresh) == 2


def test_a_booking_reference_is_not_auto_detected(store):
    """No checksum, so in free text it could be anything: an order number,
    a file name, half a signature. It still works when typed as a command."""
    from lej_cc.slack_app import autodetect_targets

    assert autodetect_targets("shipment OyTM202608137666 arrived", "C1", store, 10) == ([], [])
