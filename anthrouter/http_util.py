"""HTTP scaffolding for the upstream Anthropic connection.

Connection construction, SSE line reading, retry policy, and error-envelope
shaping.  Ported from anthproxy's ``_shared/http_util.py``; the upstream target
is a parsed base URL rather than a fixed host, because ``--upstream-base-url``
may name a local endpoint or an instance behind a reverse proxy.
"""

import email.utils
import http.client
import json
import logging
import ssl
import time
from urllib.parse import urlsplit

from .mapper.common import AnthropicRequestError

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
RETRY_BASE_DELAY = 1.0   # seconds, exponential base
RETRY_MAX_DELAY = 30.0   # seconds

DEFAULT_TIMEOUT = 300


class UpstreamTarget:
    """A validated ``--upstream-base-url``, split into connection parameters.

    Rejects a malformed target rather than defaulting it: the client's own
    credential rides on every outbound request, so a silently retargeted request
    would carry it to a host the operator never named.  Plain HTTP is preserved
    so a local endpoint stays reachable.
    """

    def __init__(self, base_url: str):
        parts = urlsplit(base_url)
        if parts.scheme not in ('http', 'https'):
            raise ValueError(
                f'--upstream-base-url must use http or https, got {parts.scheme!r}'
            )
        if not parts.hostname:
            raise ValueError('--upstream-base-url must include a host')
        try:
            port = parts.port
        except ValueError as exc:
            raise ValueError(f'--upstream-base-url has an invalid port: {exc}') from exc

        self.scheme = parts.scheme
        self.host = parts.hostname
        self.port = port
        self.path_prefix = parts.path.rstrip('/')

    @property
    def netloc(self) -> str:
        return f'{self.host}:{self.port}' if self.port else self.host

    def path(self, path: str) -> str:
        return f'{self.path_prefix}{path}'

    def connect(self, timeout: int = DEFAULT_TIMEOUT):
        if self.scheme == 'http':
            return http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        return http.client.HTTPSConnection(
            self.host, self.port, timeout=timeout, context=ssl.create_default_context()
        )


def read_sse_lines(response):
    """Yield decoded newline-delimited lines from an ``http.client`` response.

    A trailing fragment without a newline is yielded once the response is
    exhausted, for servers that omit the final newline.
    """
    buf = b''
    while True:
        chunk = response.read(4096)
        if not chunk:
            if buf:
                yield buf.decode('utf-8', errors='replace')
            break
        buf += chunk
        while b'\n' in buf:
            line, buf = buf.split(b'\n', 1)
            yield line.decode('utf-8', errors='replace')


def parse_retry_after(resp) -> float | None:
    """Return upstream retry guidance in seconds, if present and parseable."""
    if resp is None:
        return None

    ms_val = resp.getheader('retry-after-ms', '')
    if ms_val:
        try:
            return max(0.0, float(ms_val) / 1000.0)
        except (ValueError, TypeError):
            pass

    ra = resp.getheader('Retry-After', '')
    if ra:
        try:
            return max(0.0, float(ra))
        except (ValueError, TypeError):
            pass
        try:
            dt = email.utils.parsedate_to_datetime(ra)
            delta = dt.timestamp() - time.time()
            if delta > 0:
                return delta
        except Exception:
            pass

    return None


def retry_delay(resp, attempt: int) -> float:
    """Seconds to sleep before the next retry: upstream guidance, else backoff."""
    delay = parse_retry_after(resp)
    if delay is not None:
        return delay
    return min(RETRY_BASE_DELAY * (2 ** attempt), RETRY_MAX_DELAY)


def should_retry(status: int, resp) -> bool:
    """Whether an upstream error status warrants a transparent retry.

    5xx is always retried.  A 429 is retried only when the upstream gave
    explicit timing guidance; without it a short backoff won't clear the limit,
    so the error surfaces to the client immediately.
    """
    if status not in RETRYABLE_STATUSES:
        return False
    if status == 429:
        return parse_retry_after(resp) is not None
    return True


def handle_error_response(status: int, body_bytes: bytes) -> None:
    """Map an upstream HTTP error status to an ``AnthropicRequestError``.

    Always raises — never returns.  The upstream ``error.type`` is forwarded for
    400s so native Anthropic error types survive the hop.
    """
    try:
        detail = json.loads(body_bytes)
        error = detail.get('error') or {}
        if not isinstance(error, dict):
            error = {}
        message = error.get('message', '') or detail.get('message', '') or str(detail)
        error_type = error.get('type', '')
    except (json.JSONDecodeError, TypeError):
        message = body_bytes.decode('utf-8', errors='replace')[:500]
        error_type = ''

    logger.warning('Upstream error HTTP %d: %s', status, message[:300])

    if status == 400:
        raise AnthropicRequestError(
            message, error_type=error_type or 'invalid_request_error', status_code=400)
    if status == 401:
        raise AnthropicRequestError(message, error_type='authentication_error', status_code=401)
    if status == 403:
        raise AnthropicRequestError(message, error_type='permission_error', status_code=403)
    if status == 429:
        raise AnthropicRequestError(message, error_type='rate_limit_error', status_code=429)
    raise AnthropicRequestError(
        f'Upstream error (HTTP {status}): {message}',
        error_type='api_error',
        status_code=502,
    )
