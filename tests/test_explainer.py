"""The pinned explainer.

The interesting behaviour is not the first post -- it is the second, third
and hundredth start. A bot that pins a fresh copy every restart is a bot
whose pins nobody reads.
"""

from __future__ import annotations

import pytest
from slack_sdk.errors import SlackApiError

from lej_cc.explainer import build_blocks, ensure_pinned
from lej_cc.store import JobStore

CHANNEL = "C0AWBTRACK"


class FakeClient:
    """Records what would have been sent; can be told to fail any call."""

    def __init__(self, **fail: str) -> None:
        self.fail = fail
        self.posted: list[dict] = []
        self.updated: list[dict] = []
        self.pinned: list[str] = []
        self.next_ts = "1700000000.000100"

    def _maybe_fail(self, call: str) -> None:
        if call in self.fail:
            raise SlackApiError("nope", {"ok": False, "error": self.fail[call]})

    def chat_postMessage(self, **kwargs):  # noqa: ANN003, N802
        self._maybe_fail("post")
        self.posted.append(kwargs)
        return {"ok": True, "ts": self.next_ts}

    def chat_update(self, **kwargs):  # noqa: ANN003, N802
        self._maybe_fail("update")
        self.updated.append(kwargs)
        return {"ok": True}

    def pins_add(self, **kwargs):  # noqa: ANN003
        self._maybe_fail("pin")
        self.pinned.append(kwargs["timestamp"])
        return {"ok": True}


@pytest.fixture
def store(tmp_path) -> JobStore:
    return JobStore(tmp_path / "jobs.sqlite3")


# --- the message itself --------------------------------------------------


def test_it_leads_with_the_one_instruction_that_matters():
    text = build_blocks()[1]["text"]["text"]
    assert text.startswith("*Post an AWB number here and it is tracked.")


def test_it_says_references_need_the_command():
    """Auto-detect cannot see them, and a pinned message that implies it can
    is worse than one that says nothing."""
    text = build_blocks()[1]["text"]["text"]
    assert "OyTM202608137666" in text and "/awb" in text


def test_the_running_version_is_shown_when_known():
    blocks = build_blocks("v1.2.3-abc")
    assert "v1.2.3-abc" in blocks[-1]["elements"][0]["text"]
    assert len(build_blocks(None)) == 2  # and omitted when it is not


# --- the first start -----------------------------------------------------


def test_the_first_start_posts_and_pins(store):
    client = FakeClient()
    ts = ensure_pinned(client, store, CHANNEL, "v1")

    assert len(client.posted) == 1
    assert client.pinned == [ts]
    assert store.get_meta(f"explainer_ts:{CHANNEL}") == ts


# --- every start after that ---------------------------------------------


def test_a_restart_rewrites_the_same_message(store):
    """The whole point: one pin, kept current, however often the bot restarts."""
    first = FakeClient()
    ts = ensure_pinned(first, store, CHANNEL, "v1")

    second = FakeClient()
    again = ensure_pinned(second, store, CHANNEL, "v2")

    assert again == ts
    assert second.posted == []  # nothing new in the channel
    assert second.pinned == []  # and nothing pinned a second time
    assert second.updated[0]["ts"] == ts
    assert "v2" in second.updated[0]["blocks"][-1]["elements"][0]["text"]


def test_a_hundred_restarts_leave_one_message(store):
    for i in range(100):
        client = FakeClient()
        ensure_pinned(client, store, CHANNEL, f"v{i}")
    assert client.posted == []


def test_a_deleted_explainer_is_replaced(store):
    """Somebody unpinned and deleted it. Post a new one and pin that."""
    ensure_pinned(FakeClient(), store, CHANNEL, "v1")

    client = FakeClient(update="message_not_found")
    client.next_ts = "1700000999.000200"
    ts = ensure_pinned(client, store, CHANNEL, "v1")

    assert ts == "1700000999.000200"
    assert client.pinned == [ts]
    assert store.get_meta(f"explainer_ts:{CHANNEL}") == ts


def test_a_transient_update_failure_keeps_the_message(store):
    """A rate limit is not a deleted message -- do not post a duplicate."""
    ts = ensure_pinned(FakeClient(), store, CHANNEL, "v1")

    client = FakeClient(update="ratelimited")
    assert ensure_pinned(client, store, CHANNEL, "v1") == ts
    assert client.posted == []


# --- things that must not stop the bot starting -------------------------


def test_a_channel_we_are_not_in_is_survivable(store):
    client = FakeClient(post="not_in_channel")
    assert ensure_pinned(client, store, CHANNEL, "v1") is None
    assert store.get_meta(f"explainer_ts:{CHANNEL}") is None


def test_a_missing_pin_scope_still_leaves_the_message_up(store):
    """Half the value is the text. Losing the pin is not losing the message."""
    client = FakeClient(pin="missing_scope")
    ts = ensure_pinned(client, store, CHANNEL, "v1")

    assert ts is not None
    assert client.posted
    assert store.get_meta(f"explainer_ts:{CHANNEL}") == ts


def test_an_already_pinned_message_is_not_an_error(store):
    client = FakeClient(pin="already_pinned")
    assert ensure_pinned(client, store, CHANNEL, "v1") is not None


# --- two channels are independent ---------------------------------------


def test_each_channel_remembers_its_own(store):
    a = FakeClient()
    a.next_ts = "1.1"
    ensure_pinned(a, store, "C0AAA", "v1")
    b = FakeClient()
    b.next_ts = "2.2"
    ensure_pinned(b, store, "C0BBB", "v1")

    assert store.get_meta("explainer_ts:C0AAA") == "1.1"
    assert store.get_meta("explainer_ts:C0BBB") == "2.2"
