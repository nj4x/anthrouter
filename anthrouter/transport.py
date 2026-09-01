"""Upstream Anthropic transport with client-credential passthrough.

anthrouter holds no credential of its own.  Whichever of ``x-api-key`` /
``Authorization`` the client sent is forwarded byte-for-byte, and a request
carrying neither is rejected — never defaulted, never substituted.  That posture
is what removes the whole OAuth subsystem: there is no token to refresh, so a
401 is the client's to resolve and propagates untouched.
"""

import http.client
import json
import logging
import ssl
import time

from .http_util import (
    MAX_RETRIES,
    RETRY_BASE_DELAY,
    RETRY_MAX_DELAY,
    UpstreamTarget,
    handle_error_response,
    parse_ratelimit_headers,
    read_sse_lines,
    retry_delay,
    should_retry,
)
from .mapper.anthropic_transform import (
    ANTHROPIC_VERSION,
    COUNT_TOKENS_PATH,
    MESSAGES_PATH,
    build_body,
    merge_betas,
)
from .mapper.common import (
    AnthropicRequestError,
    estimate_input_tokens,
    strip_all_thinking_blocks,
)
from .model_config import resolve_model

logger = logging.getLogger(__name__)

# Client auth headers, in the order they are looked for.  Anthropic accepts
# either; a client that sends both has its x-api-key win, matching the API.
AUTH_HEADERS = ('x-api-key', 'authorization')

# Non-credential request headers forwarded verbatim when the client sent them.
# The client speaks native Anthropic, so its own framing is more accurate than
# anything anthrouter could invent.  anthropic-beta is excluded: it is rebuilt
# from the payload by merge_betas, because routing can invalidate a beta.
PASSTHROUGH_HEADERS = (
    'anthropic-version',
    'anthropic-dangerous-direct-browser-access',
    'x-app',
    'user-agent',
)


def extract_client_credentials(headers) -> dict:
    """Pull the client's auth and framing headers out of an inbound request.

    ``headers`` is the request's ``email.message.Message`` (case-insensitive) or
    a plain dict with lowercase keys.

    Raises:
        AnthropicRequestError: 401, when neither auth header is present.
    """
    auth = None
    for name in AUTH_HEADERS:
        value = headers.get(name)
        if value:
            auth = (name, value)
            break
    if auth is None:
        raise AnthropicRequestError(
            'Missing credentials: send your own Anthropic x-api-key or Authorization '
            'header. anthrouter forwards the client credential and supplies none.',
            error_type='authentication_error',
            status_code=401,
        )

    forward = {}
    for name in PASSTHROUGH_HEADERS:
        value = headers.get(name)
        if value:
            forward[name] = value

    return {'auth': auth, 'forward': forward}


def _request_headers(credentials: dict, betas: str, stream: bool) -> dict:
    auth_name, auth_value = credentials['auth']
    headers = {
        'anthropic-version': ANTHROPIC_VERSION,
        'Content-Type': 'application/json',
        'Accept': 'text/event-stream' if stream else 'application/json',
    }
    headers.update(credentials.get('forward') or {})
    headers[auth_name] = auth_value
    if betas:
        headers['anthropic-beta'] = betas
    return headers


class AnthropicTransport:
    """Sends Anthropic Messages requests upstream over stdlib HTTP."""

    def __init__(self, base_url: str):
        self.target = UpstreamTarget(base_url)

    def _send_with_retries(self, payload: dict, credentials: dict, stream: bool,
                           path: str = MESSAGES_PATH, method: str = 'POST',
                           ratelimit_out: dict | None = None, config=None):
        aliases = config.model_aliases if config else None
        body_bytes = build_body(payload, aliases=aliases)
        betas = merge_betas(payload, aliases=aliases)
        headers = _request_headers(credentials, betas, stream)
        url_path = self.target.path(path)
        thinking_stripped = False

        for attempt in range(MAX_RETRIES + 1):
            conn = self.target.connect()
            try:
                conn.request(method, url_path, body=body_bytes, headers=headers)
                resp = conn.getresponse()
            except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
                conn.close()
                if attempt < MAX_RETRIES:
                    delay = min(RETRY_BASE_DELAY * (2 ** attempt), RETRY_MAX_DELAY)
                    logger.warning(
                        'Upstream request failed (network, attempt %d/%d): %s — '
                        'retrying in %.1fs', attempt + 1, MAX_RETRIES, exc, delay,
                    )
                    time.sleep(delay)
                    continue
                raise AnthropicRequestError(
                    f'Upstream connection error after {MAX_RETRIES} retries: {exc}',
                    error_type='api_error',
                    status_code=502,
                    connection_error=True,
                ) from exc

            if resp.status == 200:
                if ratelimit_out is not None:
                    ratelimit_out.update(parse_ratelimit_headers(resp))
                return conn, resp

            if should_retry(resp.status, resp) and attempt < MAX_RETRIES:
                delay = retry_delay(resp, attempt)
                resp.read()
                conn.close()
                logger.warning('Upstream HTTP %d (attempt %d/%d) — retrying in %.1fs',
                               resp.status, attempt + 1, MAX_RETRIES, delay)
                time.sleep(delay)
                continue

            resp_body = resp.read()
            conn.close()

            # Thinking-block signatures are model-specific: an opus-generated
            # block is invalid for sonnet/haiku and vice versa.  Because the
            # router can switch tiers mid-session, the resent history may carry
            # blocks from the previous tier.  Recover once by stripping them all.
            if (not thinking_stripped and resp.status == 400
                    and b'thinking' in resp_body
                    and (b'signature' in resp_body or b'redacted_thinking' in resp_body)):
                messages = payload.get('messages')
                if isinstance(messages, list):
                    stripped_msgs = strip_all_thinking_blocks(messages)
                    if stripped_msgs is not messages:
                        body_bytes = build_body({**payload, 'messages': stripped_msgs})
                        thinking_stripped = True
                        logger.warning(
                            'Retrying after thinking-block 400 with all thinking/'
                            'redacted_thinking blocks stripped from history')
                        continue

            handle_error_response(resp.status, resp_body)

        raise AnthropicRequestError('Upstream request failed', error_type='api_error',
                                    status_code=502)

    def send_message(self, payload: dict, credentials: dict, config=None,
                     ratelimit_out: dict | None = None) -> dict:
        stream = bool(payload.get('stream'))
        conn, resp = self._send_with_retries(payload, credentials, stream=stream,
                                             ratelimit_out=ratelimit_out, config=config)
        try:
            body = resp.read()
        finally:
            conn.close()

        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise AnthropicRequestError(
                f'Upstream returned non-JSON response: {body[:200]!r}',
                error_type='api_error',
                status_code=502,
            ) from exc

    def send_message_stream(self, payload: dict, credentials: dict, config=None,
                            ratelimit_out: dict | None = None):
        conn, resp = self._send_with_retries(payload, credentials, stream=True,
                                             ratelimit_out=ratelimit_out, config=config)
        try:
            pending = ''
            for line in read_sse_lines(resp):
                pending += line + '\n'
                if line.strip() == '':
                    if pending.strip():
                        yield pending
                    pending = ''
            if pending.strip():
                yield pending + '\n'
        finally:
            conn.close()

    def count_tokens(self, payload: dict, credentials: dict, config=None) -> dict:
        aliases = config.model_aliases if config else None
        try:
            body_bytes = build_body(payload, aliases=aliases)
            betas = merge_betas(payload, aliases=aliases)
            headers = _request_headers(credentials, betas, stream=False)
            conn = self.target.connect()
            try:
                conn.request('POST', self.target.path(COUNT_TOKENS_PATH),
                             body=body_bytes, headers=headers)
                resp = conn.getresponse()
                resp_body = resp.read()
                status = resp.status
            finally:
                conn.close()

            if status == 200:
                return json.loads(resp_body)

            logger.warning('count_tokens upstream returned HTTP %d — falling back to estimate',
                           status)
        except Exception as exc:
            logger.warning('count_tokens upstream call failed: %s — using estimate', exc)

        return {
            'input_tokens': estimate_input_tokens(payload),
            'model': resolve_model(payload.get('model', ''), aliases=config.model_aliases if config else None),
        }
