"""Cached OAuth usage fetcher for Anthropic enterprise tokens."""

import datetime as dt
import hashlib
import http.client
import json
import logging
import threading
import time
from dataclasses import dataclass

from . import pace

logger = logging.getLogger(__name__)

ANTHROPIC_HOST = 'api.anthropic.com'
USAGE_PATH = '/api/oauth/usage'
USAGE_TIMEOUT_SECONDS = 5
CACHE_TTL_SECONDS = 60
FETCH_INTERVAL_SECONDS = 30  # Throttle: single background thread sleeps between fetches


@dataclass
class OAuthUsage:
    """OAuth enterprise token usage snapshot.

    ``month_elapsed_pct`` is deprecated: it duplicates ``calendar_elapsed_pct`` and is
    retained only so existing clients keep working. New consumers read
    ``workday_elapsed_pct`` / ``calendar_elapsed_pct`` and pick a mode (ADR-0008).
    """
    burn_pct: float | None
    used_usd: float | None
    total_usd: float | None
    month_elapsed_pct: float | None
    monthly_blocked: bool
    eligible: bool
    cooldown_remaining_seconds: int
    usage_age_seconds: int | None
    usage_stale: bool
    workday_elapsed_pct: float | None = None
    calendar_elapsed_pct: float | None = None
    workday_timezone: str | None = None
    period_start: str | None = None
    period_end: str | None = None
    period_workday_count: int | None = None


class OAuthUsageCache:
    """In-memory cache of OAuth token usage (last seen token only).

    Issue 1 Fix: Uses a single timer-based background thread that sleeps
    FETCH_INTERVAL_SECONDS between fetches, preventing per-request thread
    spawning that causes quota exhaustion.
    """

    def __init__(self, timezone_name: str | None = None):
        self._lock = threading.Lock()
        self._token_hash: str | None = None
        self._usage: OAuthUsage | None = None
        self._cached_at: float = 0
        self._fetch_scheduled_at: float = 0
        self._fetch_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._current_token: str | None = None
        self._tz = pace.resolve_timezone(timezone_name)

    def get(self, access_token: str) -> OAuthUsage | None:
        """Fetch cached usage or refresh if stale. Never blocks on network."""
        token_hash = hashlib.sha256(access_token.encode()).hexdigest()
        now = time.time()

        with self._lock:
            # Token unchanged and cache fresh — return cached
            if token_hash == self._token_hash and now - self._cached_at < CACHE_TTL_SECONDS:
                return self._usage

            # Token changed or cache stale — ensure single background thread is running
            if token_hash != self._token_hash or now > self._fetch_scheduled_at:
                self._token_hash = token_hash
                self._current_token = access_token
                self._fetch_scheduled_at = now + FETCH_INTERVAL_SECONDS
                logger.debug('OAuth usage fetch scheduled for token %s...', token_hash[:8])

                # Start single background thread if not already running
                if self._fetch_thread is None or not self._fetch_thread.is_alive():
                    self._stop_event.clear()
                    self._fetch_thread = threading.Thread(
                        target=self._background_fetch_loop,
                        daemon=True,
                    )
                    self._fetch_thread.start()

            return self._usage

    def get_usage(self) -> OAuthUsage | None:
        """Issue 3 Fix: Public getter for cached OAuth usage.

        Returns the last cached usage data (may be None on first load or if no
        token has been seen). Use this instead of accessing _usage directly.
        """
        with self._lock:
            return self._usage

    def _background_fetch_loop(self) -> None:
        """Single background thread that fetches OAuth usage at intervals."""
        while not self._stop_event.wait(timeout=FETCH_INTERVAL_SECONDS):
            token = None
            token_hash = None
            with self._lock:
                token = self._current_token
                token_hash = self._token_hash

            if token is None:
                continue

            try:
                usage = self._fetch_usage(token)
                usage.usage_age_seconds = 0
                usage.usage_stale = False

                with self._lock:
                    if token_hash == self._token_hash:
                        self._usage = usage
                        self._cached_at = time.time()
                logger.debug('OAuth usage fetched: burn_pct=%s eligible=%s', usage.burn_pct, usage.eligible)
            except Exception as exc:
                logger.warning('OAuth usage fetch failed: %s', exc)
                # Mark existing cache as stale on failure
                with self._lock:
                    if self._usage:
                        self._usage.usage_stale = True

    def _fetch_usage(self, access_token: str) -> OAuthUsage:
        """Fetch OAuth usage from Anthropic API."""
        conn = http.client.HTTPSConnection(ANTHROPIC_HOST, timeout=USAGE_TIMEOUT_SECONDS)
        try:
            conn.request(
                'GET',
                USAGE_PATH,
                headers={
                    'Authorization': f'Bearer {access_token}',
                    'anthropic-version': '2023-06-01',
                    'anthropic-beta': 'oauth-2025-04-20',
                    'Accept': 'application/json',
                },
            )
            resp = conn.getresponse()
            body = resp.read()
            status = resp.status
        finally:
            conn.close()

        if status != 200:
            raise RuntimeError(f'OAuth usage endpoint returned HTTP {status}')

        data = json.loads(body)
        # Real shape: {"extra_usage": {"monthly_limit", "used_credits",
        # "utilization", "is_enabled", "spend_limit_reached", "decimal_places"}}
        # — confirmed against anthproxy's oauth_registry.py::_usage_burn and
        # server.py::oauth_token_status, which parse this exact response.
        extra = data.get('extra_usage') or {}

        try:
            dp = int(extra.get('decimal_places') or 2)
            scale = 10 ** dp
        except (TypeError, ValueError):
            scale = 100

        try:
            used_usd = float(extra['used_credits']) / scale
        except (KeyError, TypeError, ValueError):
            used_usd = None
        try:
            total_usd = float(extra['monthly_limit']) / scale
        except (KeyError, TypeError, ValueError):
            total_usd = None

        try:
            monthly_limit = float(extra['monthly_limit'])
            used_credits = float(extra['used_credits'])
            utilization = float(extra['utilization'])
            valid = True
        except (KeyError, TypeError, ValueError):
            monthly_limit = used_credits = utilization = None
            valid = False

        is_enabled = extra.get('is_enabled') is True
        cap_reached = extra.get('spend_limit_reached') is True or (
            utilization is not None and utilization >= 100.0
        )
        valid = valid and is_enabled and monthly_limit is not None and monthly_limit > 0 \
            and used_credits is not None and used_credits >= 0 and utilization is not None and utilization >= 0
        burn_pct = utilization if valid else None

        # One instant for every derived field, so the two baselines and the period
        # bounds can never straddle a clock tick and disagree.
        now = dt.datetime.now(dt.timezone.utc)
        period_start, period_end = pace.period_bounds(now)
        calendar_pct = pace.calendar_elapsed_pct(now)
        workday_pct, period_wd_count = pace.workday_elapsed_pct(now, self._tz)

        return OAuthUsage(
            burn_pct=burn_pct,
            used_usd=used_usd,
            total_usd=total_usd,
            month_elapsed_pct=calendar_pct,
            monthly_blocked=cap_reached,
            eligible=valid and not cap_reached,
            cooldown_remaining_seconds=0,
            usage_age_seconds=None,
            usage_stale=False,
            workday_elapsed_pct=workday_pct,
            calendar_elapsed_pct=calendar_pct,
            workday_timezone=self._tz.key,
            period_start=period_start.isoformat(),
            period_end=period_end.isoformat(),
            period_workday_count=period_wd_count,
        )
