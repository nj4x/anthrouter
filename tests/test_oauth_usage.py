"""Tests for the OAuth usage poller's cooldown arithmetic and log noise.

The seam is the arithmetic itself: every test here drives ``OAuthUsageCache``
bookkeeping methods directly, with no HTTP round trip and no background thread.
"""

import logging

import pytest

from anthrouter.oauth_usage import (
    MAX_BACKOFF_SECONDS,
    MIN_COOLDOWN_SECONDS,
    OAuthUsage,
    OAuthUsageCache,
)


def _usage() -> OAuthUsage:
    """A minimal successful usage payload."""
    return OAuthUsage(
        burn_pct=10.0,
        used_usd=1.0,
        total_usd=10.0,
        month_elapsed_pct=50.0,
        monthly_blocked=False,
        eligible=True,
        cooldown_remaining_seconds=0,
        usage_age_seconds=0,
        usage_stale=False,
    )


def _fail(cache: OAuthUsageCache, times: int = 1) -> None:
    """Drive ``times`` failed polls through the failure bookkeeping."""
    for _ in range(times):
        cache._record_failure(RuntimeError('OAuth usage endpoint returned HTTP 429'))


class TestCooldownSeconds:
    """The bounded min(max(...)) cooldown on the failure path."""

    def test_retry_after_zero_yields_at_least_min_cooldown(self):
        """Retry-After: 0 cannot collapse the wait — the floor still applies."""
        cache = OAuthUsageCache(poll_interval_seconds=1)
        cache._note_error_response(429, {'retry-after': '0'})
        _fail(cache)

        assert cache._retry_after_seconds == 0.0
        assert cache._cooldown_seconds() == MIN_COOLDOWN_SECONDS

    def test_retry_after_below_exponential_does_not_lower_the_wait(self):
        """A small Retry-After enters through max(), so the exponential value wins."""
        cache = OAuthUsageCache(poll_interval_seconds=60)
        cache._note_error_response(429, {'retry-after': '10'})
        _fail(cache)

        # multiplier 2 x 60s = 120s, well above both the 10s header and the 5s floor.
        assert cache._cooldown_seconds() == 120.0

    def test_retry_after_above_cap_is_clamped(self):
        """An hour-long Retry-After is capped at MAX_BACKOFF_SECONDS."""
        cache = OAuthUsageCache(poll_interval_seconds=1)
        cache._note_error_response(429, {'retry-after': '3600'})
        _fail(cache)

        assert cache._cooldown_seconds() == MAX_BACKOFF_SECONDS

    def test_missing_header_leaves_exponential_with_floor(self):
        """No Retry-After drops out of the max(), leaving the floored exponential value."""
        cache = OAuthUsageCache(poll_interval_seconds=1)
        cache._note_error_response(429, {})
        _fail(cache)

        assert cache._retry_after_seconds is None
        assert cache._cooldown_seconds() == MIN_COOLDOWN_SECONDS

    def test_non_429_failure_ignores_any_retry_after(self):
        """A Retry-After on a non-429 response is not applied."""
        cache = OAuthUsageCache(poll_interval_seconds=1)
        cache._note_error_response(500, {'retry-after': '120'})
        _fail(cache)

        assert cache._retry_after_seconds is None
        assert cache._cooldown_seconds() == MIN_COOLDOWN_SECONDS

    def test_unparseable_header_behaves_as_missing_and_warns(self, caplog):
        """An unreadable Retry-After is treated as absent and logs a warning."""
        cache = OAuthUsageCache(poll_interval_seconds=1)
        with caplog.at_level(logging.WARNING, logger='anthrouter.oauth_usage'):
            cache._note_error_response(429, {'retry-after': 'Wed, 21 Oct 2026 07:28:00 GMT'})
        _fail(cache)

        assert cache._retry_after_seconds is None
        assert cache._cooldown_seconds() == MIN_COOLDOWN_SECONDS
        assert any('invalid Retry-After header' in r.message for r in caplog.records)

    def test_consecutive_failures_reach_the_cap(self):
        """Repeated failures walk the exponential value out to the 300-second cap."""
        cache = OAuthUsageCache(poll_interval_seconds=60)
        waits = []
        for _ in range(6):
            cache._note_error_response(429, {})
            _fail(cache)
            waits.append(cache._cooldown_seconds())

        assert waits[0] == 120.0
        assert waits[-1] == MAX_BACKOFF_SECONDS
        assert waits == sorted(waits)

    def test_retry_after_zero_never_pins_the_loop_at_zero(self):
        """Regression: Retry-After: 0 repeated forever still backs off, never busy-loops."""
        cache = OAuthUsageCache(poll_interval_seconds=1)
        for _ in range(10):
            cache._note_error_response(429, {'retry-after': '0'})
            _fail(cache)
            assert cache._cooldown_seconds() >= MIN_COOLDOWN_SECONDS

    def test_healthy_poll_sleeps_the_configured_interval_below_the_floor(self):
        """A sub-5s --oauth-usage-poll-interval is honoured, not raised to the floor."""
        cache = OAuthUsageCache(poll_interval_seconds=2)

        assert cache._cooldown_seconds() == 2.0

    def test_success_returns_the_healthy_interval_after_a_failure(self):
        """A successful poll drops the wait back to the configured interval."""
        cache = OAuthUsageCache(poll_interval_seconds=2)
        cache._token_hash = 'abc'
        cache._note_error_response(429, {'retry-after': '200'})
        _fail(cache)
        assert cache._cooldown_seconds() == 200.0

        cache._record_success(_usage(), 'abc')

        assert cache._cooldown_seconds() == 2.0

    def test_healthy_poll_at_the_default_interval(self):
        """The default cadence is unchanged by the floor."""
        cache = OAuthUsageCache()

        assert cache._cooldown_seconds() == 60.0


class TestCooldownLogging:
    """Defect H: one 429 line per cooldown window, not one per attempt."""

    def test_429_logs_once_per_cooldown_window(self, caplog):
        """Five failed attempts inside one window produce one 429 line."""
        cache = OAuthUsageCache(poll_interval_seconds=1)
        with caplog.at_level(logging.INFO, logger='anthrouter.oauth_usage'):
            for _ in range(5):
                cache._note_error_response(429, {'retry-after': '0'})
                _fail(cache)

        lines = [r for r in caplog.records if 'OAuth usage 429' in r.message]
        assert len(lines) == 1

    def test_successful_poll_closes_the_window_so_the_next_429_logs_again(self, caplog):
        """A success resets the suppression; the next failure logs the 429 line."""
        cache = OAuthUsageCache(poll_interval_seconds=1)
        cache._token_hash = 'abc'
        with caplog.at_level(logging.INFO, logger='anthrouter.oauth_usage'):
            cache._note_error_response(429, {})
            _fail(cache)
            cache._note_error_response(429, {})
            _fail(cache)

            cache._record_success(_usage(), 'abc')

            cache._note_error_response(429, {})
            _fail(cache)

        lines = [r for r in caplog.records if 'OAuth usage 429' in r.message]
        assert len(lines) == 2

    def test_429_logs_once_when_window_opened_by_non_429_failure(self, caplog):
        """A 429 inside a window that a timeout/500 opened still logs the 429 line once."""
        cache = OAuthUsageCache(poll_interval_seconds=1)
        with caplog.at_level(logging.INFO, logger='anthrouter.oauth_usage'):
            # Open the window with a non-429 failure.
            _fail(cache)
            # Now a 429 arrives inside the already-open window.
            cache._note_error_response(429, {'retry-after': '10'})
            _fail(cache)
            # A second 429 must not repeat the line.
            cache._note_error_response(429, {'retry-after': '10'})
            _fail(cache)

        lines = [r for r in caplog.records if 'OAuth usage 429' in r.message]
        assert len(lines) == 1

    def test_failure_line_is_warning_once_then_debug(self, caplog):
        """The per-attempt failure line drops to debug while a cooldown is in force."""
        cache = OAuthUsageCache(poll_interval_seconds=1)
        with caplog.at_level(logging.DEBUG, logger='anthrouter.oauth_usage'):
            _fail(cache, times=4)

        failures = [r for r in caplog.records if 'OAuth usage fetch failed' in r.message]
        assert [r.levelno for r in failures] == [
            logging.WARNING,
            logging.DEBUG,
            logging.DEBUG,
            logging.DEBUG,
        ]


class TestRecordSuccess:
    """A success for a different token must not reset another token's backoff."""

    def test_stale_token_hash_does_not_reset_backoff(self):
        cache = OAuthUsageCache(poll_interval_seconds=1)
        cache._token_hash = 'current'
        _fail(cache)
        before = cache._cooldown_seconds()

        cache._record_success(_usage(), 'stale')

        assert cache._usage is None
        assert cache._cooldown_seconds() == before


@pytest.mark.parametrize('interval', [1, 2, 5, 60])
def test_failure_wait_is_never_below_the_floor(interval):
    """Whatever the configured cadence, a failed poll waits at least 5 seconds."""
    cache = OAuthUsageCache(poll_interval_seconds=interval)
    cache._note_error_response(429, {'retry-after': '0'})
    _fail(cache)

    assert cache._cooldown_seconds() >= MIN_COOLDOWN_SECONDS
