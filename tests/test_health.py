"""Health tracking and the dead-man's switch."""

from __future__ import annotations

from lej_cc.health import Health, Heartbeat


def test_counters_move():
    health = Health()
    assert health.polls_ok == 0

    health.poll_succeeded(37.5)
    health.poll_failed("936-02927971: ApiUnavailable")

    assert health.polls_ok == 1
    assert health.polls_failed == 1
    assert health.last_api_seconds == 37.5
    assert health.last_poll_ok_at is not None
    assert "ApiUnavailable" in health.last_poll_error


def test_uptime_starts_at_zero():
    assert Health().uptime.total_seconds() < 1


def test_heartbeat_is_off_without_a_url():
    beat = Heartbeat("")
    assert beat.enabled is False
    beat.ping()  # must not raise or reach the network


def test_heartbeat_respects_its_interval(monkeypatch):
    """Pinging on every 30s scheduler tick would be wasteful; the interval is
    what makes it safe to call from the loop."""
    calls = []

    class FakeHttpx:
        @staticmethod
        def get(url, timeout=None):
            calls.append(url)

    import sys

    monkeypatch.setitem(sys.modules, "httpx", FakeHttpx)

    beat = Heartbeat("https://hc.example/abc", interval_seconds=3600)
    beat.ping()
    beat.ping()
    beat.ping()
    assert calls == ["https://hc.example/abc"]

    beat.ping(force=True)  # startup announces immediately
    assert len(calls) == 2


def test_a_failed_ping_never_propagates(monkeypatch):
    """The heartbeat exists to report trouble, not to cause it."""

    class Broken:
        @staticmethod
        def get(url, timeout=None):
            raise OSError("no route to host")

    import sys

    monkeypatch.setitem(sys.modules, "httpx", Broken)
    Heartbeat("https://hc.example/abc").ping()  # must not raise


def test_the_very_first_ping_is_never_skipped(monkeypatch):
    """time.monotonic() counts from boot. A zero sentinel made the first ping
    look recent on a machine up for less than the interval -- a freshly
    booted server, which is when the ping matters most."""
    calls = []

    class FakeHttpx:
        @staticmethod
        def get(url, timeout=None):
            calls.append(url)

    import sys
    import time as time_module

    monkeypatch.setitem(sys.modules, "httpx", FakeHttpx)
    # Pretend the machine booted 30 seconds ago.
    monkeypatch.setattr(time_module, "monotonic", lambda: 30.0)

    Heartbeat("https://hc.example/abc", interval_seconds=300).ping()
    assert calls == ["https://hc.example/abc"]
