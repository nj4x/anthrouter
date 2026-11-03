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


_RATELIMIT_HEADERS = (
    ('requests_remaining', 'anthropic-ratelimit-requests-remaining'),
    ('tokens_remaining', 'anthropic-ratelimit-tokens-remaining'),
    ('input_tokens_remaining', 'anthropic-ratelimit-input-tokens-remaining'),
    ('output_tokens_remaining', 'anthropic-ratelimit-output-tokens-remaining'),
)

_RATELIMIT_RESET_HEADERS = (
    'anthropic-ratelimit-tokens-reset',
    'anthropic-ratelimit-requests-reset',
    'anthropic-ratelimit-input-tokens-reset',
)


def parse_ratelimit_headers(resp) -> dict:
    """Extract the ``anthropic-ratelimit-*`` window from an upstream response.

    These headers are the only passive quota signal available to a proxy that
    forwards someone else's credential — an opaque OAuth token cannot be
    introspected and the usage API needs an admin key anthrouter never holds.
    Missing or unparseable values are omitted rather than zeroed.
    """
    if resp is None:
        return {}
    out: dict = {}
    for key, header in _RATELIMIT_HEADERS:
        raw = resp.getheader(header)
        if raw is None:
            continue
        try:
            out[key] = int(raw)
        except (TypeError, ValueError):
            continue
    for header in _RATELIMIT_RESET_HEADERS:
        raw = resp.getheader(header)
        if raw:
            out['reset_at'] = raw
            break
    return out


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


_DEFAULT_4XX_TYPES = {
    401: 'authentication_error',
    403: 'permission_error',
    404: 'not_found_error',
    413: 'request_too_large',
    429: 'rate_limit_error',
}

# Claude Desktop replays the conversation with ``thread: {"type": "create"}``
# after a Thread 400 but stops after this 404 (ADR-0031).  Upstream sends no
# ``error.code`` for it, so the message text is the only identifier.
_EXPIRED_THREAD_PREFIX = 'No thread state was found'


def handle_error_response(status: int, body_bytes: bytes, resp) -> None:
    """Map an upstream HTTP error status to an ``AnthropicRequestError`` (ADR-0028).

    Always raises — never returns.  Every upstream 4xx keeps its own status and
    its upstream ``error.type`` (falling back to a per-status default when
    upstream sends none), with upstream's message unprefixed, so a
    non-retryable error is never shown to the client as a retryable 502.  The
    one exception is the expired-Thread 404, raised as ``400
    invalid_request_error`` with the upstream status kept on the exception
    (ADR-0031).  5xx
    and 529 map to ``502 api_error`` with an ``Upstream error (HTTP n): ``
    message prefix.  ``resp`` is read for its headers only.  Every error raised
    *here* carries the upstream retry guidance as ``retry_after`` — float
    seconds, unrounded and uncapped, or ``None`` when absent or unparseable.
    Callers that raise elsewhere (the transport's loop-exit fallback) are not
    covered by that guarantee.  The ``request-id`` is logged for every status;
    ``retry-after`` and ``ratelimit`` only for 429 and 5xx.
    """
    retry_after = parse_retry_after(resp)
    is_json = True
    error_code = ''
    try:
        detail = json.loads(body_bytes)
        error = detail.get('error') or {}
        if not isinstance(error, dict):
            error = {}
        message = error.get('message', '') or detail.get('message', '') or str(detail)
        error_type = error.get('type', '')
        error_code = error.get('code', '') or ''
    except (json.JSONDecodeError, TypeError, AttributeError):
        message = body_bytes.decode('utf-8', errors='replace')[:500]
        error_type = ''
        is_json = False

    req_id = resp.getheader('request-id') if resp is not None else None
    timing = ''
    if status == 429 or status >= 500:
        timing = '  retry-after=%s  ratelimit=%s' % (
            retry_after, parse_ratelimit_headers(resp) or None)
    if is_json:
        type_part = f' type={error_type}' if error_type else ''
        code_part = f' code={error_code}' if error_code else ''
        logger.warning(
            'Upstream error HTTP %d: %s%s%s  request-id=%s%s',
            status, message[:300], type_part, code_part, req_id, timing)
    else:
        logger.warning(
            'Upstream error HTTP %d (plain): %s  request-id=%s%s',
            status, message[:300], req_id, timing)

    if 400 <= status < 500:
        error_type = error_type or _DEFAULT_4XX_TYPES.get(status, 'invalid_request_error')
        if status == 404 and is_json and message.startswith(_EXPIRED_THREAD_PREFIX):
            logger.info('Upstream Thread not found (usually expired): sent to client '
                        'as 400 so it replays')
            raise AnthropicRequestError(
                message,
                error_type='invalid_request_error',
                status_code=400,
                retry_after=retry_after,
                upstream_status=status,
                upstream_error_type=error_type)
        raise AnthropicRequestError(
            message,
            error_type=error_type,
            status_code=status,
            retry_after=retry_after)
    raise AnthropicRequestError(
        f'Upstream error (HTTP {status}): {message}',
        error_type='api_error',
        status_code=502,
        retry_after=retry_after,
    )
