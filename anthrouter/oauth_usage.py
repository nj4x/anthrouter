"""Cached OAuth usage fetcher for Anthropic enterprise tokens."""

import hashlib
import http.client
import json
import threading
import time
from dataclasses import dataclass
from typing import Optional

ANTHROPIC_HOST = 'api.anthropic.com'
USAGE_PATH = '/api/oauth/usage'
USAGE_TIMEOUT_SECONDS = 5
CACHE_TTL_SECONDS = 60


@dataclass
class OAuthUsage:
    """OAuth enterprise token usage snapshot."""
    burn_pct: Optional[float]
    used_usd: Optional[float]
    total_usd: Optional[float]
    month_elapsed_pct: Optional[float]
    monthly_blocked: bool
    eligible: bool
    cooldown_remaining_seconds: int
    usage_age_seconds: Optional[int]
    usage_stale: bool


class OAuthUsageCache:
    """In-memory cache of OAuth token usage (last seen token only)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._token_hash: Optional[str] = None
        self._usage: Optional[OAuthUsage] = None
        self._cached_at: float = 0
        self._fetch_at: float = 0

    def get(self, access_token: str) -> Optional[OAuthUsage]:
        """Fetch cached usage or refresh if stale. Never blocks on network."""
        token_hash = hashlib.sha256(access_token.encode()).hexdigest()
        now = time.time()

        with self._lock:
            # Token unchanged and cache fresh — return cached
            if token_hash == self._token_hash and now - self._cached_at < CACHE_TTL_SECONDS:
                return self._usage

            # Token changed or cache stale — schedule fetch (non-blocking)
            if token_hash != self._token_hash or now > self._fetch_at:
                self._token_hash = token_hash
                self._fetch_at = now + CACHE_TTL_SECONDS
                # Spawn background fetch; return stale cache in the meantime
                threading.Thread(
                    target=self._fetch_and_cache,
                    args=(access_token, token_hash, now),
                    daemon=True,
                ).start()

            return self._usage

    def _fetch_and_cache(self, access_token: str, token_hash: str, started_at: float) -> None:
        """Fetch from Anthropic and cache result."""
        try:
            usage = self._fetch_usage(access_token)
            age_secs = time.time() - started_at
            usage.usage_age_seconds = int(age_secs) if age_secs >= 0 else None
            usage.usage_stale = False

            with self._lock:
                if token_hash == self._token_hash:
                    self._usage = usage
                    self._cached_at = time.time()
        except Exception:
            # Fetch failed; mark existing cache as stale but don't replace it
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
        quota = data.get('quota', {})
        monthly = quota.get('monthly', {})

        return OAuthUsage(
            burn_pct=monthly.get('burn_pct'),
            used_usd=monthly.get('used_usd'),
            total_usd=monthly.get('total_usd'),
            month_elapsed_pct=monthly.get('month_elapsed_pct'),
            monthly_blocked=monthly.get('blocked', False),
            eligible=data.get('eligible', False),
            cooldown_remaining_seconds=data.get('cooldown_remaining_seconds', 0),
            usage_age_seconds=None,
            usage_stale=False,
        )
