"""HTTP entrypoint: ``/v1/messages``, ``/v1/messages/count_tokens``, ``/health``.

One request walks: local-command interception → model-tier routing →
system-prompt sanitization → upstream dispatch → single-pass SSE wrapper →
DB record → Anthropic-shaped response.

There is no handler-level retry.  ``AnthropicTransport`` already retries 429s
with upstream timing guidance and 5xx in-connection, and with one backend there
is nowhere else for a failed request to go — so the routing decision and the
prompt-hash pair are derived exactly once per request, and the original/sanitized
hash pair never has to be stashed and restored across attempts.
"""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import re
import threading
import time
import urllib.parse
import uuid
from dataclasses import replace
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from . import admin
from .mapper import AnthropicRequestError, anthropic_error_payload, sse_event
from .model_router import (
    RoutingTarget,
    _cap_cached_tier,
    _extract_user_text,
    calibrated_ratio,
    route_model,
)
from .request_text import _WRAPPER_TAGS, last_transcript_user_turn, strip_reminders
from .sanitizer import sanitize_system_prompt
from .transport import extract_client_credentials

logger = logging.getLogger(__name__)

# Client-side socket teardown: the client cancelled an in-flight request.
# Benign, so it is logged quietly rather than as a proxy failure.
_CLIENT_DISCONNECT = (BrokenPipeError, ConnectionResetError)

UNTRACKED_SESSION_ID = '(untracked)'

_SET_MODEL_ROUTING_PREFIX = 'proxy-set-model-routing:'
_SESSION_SUFFIX = ':session'

# Unlike _WRAPPER_TAGS (whose blocks are removed), <session>…</session> keeps its
# inner content: it is a recognized way to wrap a proxy-* command.  Anchored at
# the start so prose before the tag prevents unwrapping, while trailing
# boilerplate after </session> is ignored.
_SESSION_WRAP_RE = re.compile(r'\s*<session>(.*?)</session>', re.DOTALL)

_SSE_DATA_RE = re.compile(r'^data: (.+)$', re.MULTILINE)
_RESPONSE_TEXT_LIMIT = 1024 * 1024

_UUID_RE = re.compile(
    r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
    r'[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
)

_ANCHOR_LIMIT = 4_000
_ROUTING_HEADER = '## anthrouter model routing\n\n'


# ---------------------------------------------------------------------------
# Request inspection
# ---------------------------------------------------------------------------

def _final_user_text(payload: dict) -> str | None:
    """Return the sole final-user-message text, or None if not a clean command.

    Non-text blocks (tool_result, image, …) disqualify the message so ordinary
    conversation is never intercepted, and after reminder-stripping exactly one
    non-empty segment must remain: two or more indicate prose, not a command.
    """
    messages = payload.get('messages')
    if not isinstance(messages, list) or not messages:
        return None
    message = messages[-1]
    if not isinstance(message, dict) or message.get('role') != 'user':
        return None

    content = message.get('content')
    if isinstance(content, str):
        return strip_reminders(content) or None

    if isinstance(content, list):
        segments = []
        for block in content:
            if not isinstance(block, dict) or block.get('type') != 'text':
                return None
            text = block.get('text')
            if not isinstance(text, str):
                return None
            stripped = strip_reminders(text)
            if stripped:
                segments.append(stripped)
        return segments[0] if len(segments) == 1 else None

    return None


def parse_local_command(payload: dict) -> tuple[str, object | None] | None:
    """Parse an exact local command from the final user message, or return None.

    Matching runs after wrapper stripping and before credential parsing or any
    dispatch, so a command never reaches the upstream.
    """
    text = _final_user_text(payload)
    if text is None:
        return None
    m = _SESSION_WRAP_RE.match(text)
    if m:
        text = m.group(1).strip()
    if '\n' in text:
        before, last = text.rsplit('\n', 1)
        cmd = text if any(f'<{t}>' in before for t in _WRAPPER_TAGS) else last
    else:
        cmd = text

    if cmd == 'proxy-help':
        return ('help', None)
    if cmd == 'proxy-status':
        return ('status', None)
    if cmd.startswith(_SET_MODEL_ROUTING_PREFIX):
        rest = cmd[len(_SET_MODEL_ROUTING_PREFIX):]
        if rest.endswith(_SESSION_SUFFIX):
            name = 'session-set-model-routing'
            value = rest[:-len(_SESSION_SUFFIX)]
        else:
            name = 'set-model-routing'
            value = rest
        if value == 'on':
            arg = True
        elif value == 'off':
            arg = False
        elif value == 'auto' and name == 'session-set-model-routing':
            arg = None  # clear the session override, follow global
        else:
            arg = 'invalid'
        return (name, arg)
    return None


def session_key(payload: dict) -> str | None:
    """Return the full ``metadata.user_id`` blob, or None when absent.

    The whole blob is the key — Claude Code sends a JSON object here, and only
    its nested ``session_id`` is safe to use as a human-facing label.
    """
    metadata = payload.get('metadata')
    if not isinstance(metadata, dict):
        return None
    user_id = metadata.get('user_id')
    if not isinstance(user_id, str) or not user_id:
        return None
    return user_id


def _session_short_id(sess_key: str) -> str:
    """An 8-char display token for logs: session_id, else any UUID, else a hash."""
    if sess_key and sess_key[0] == '{':
        try:
            data = json.loads(sess_key)
            if isinstance(data, dict):
                sid = data.get('session_id')
                if isinstance(sid, str) and sid:
                    m = _UUID_RE.search(sid)
                    return m.group()[:8] if m else sid[:8]
        except (json.JSONDecodeError, TypeError):
            pass
    m = _UUID_RE.search(sess_key)
    if m:
        return m.group()[:8]
    return hashlib.sha256(sess_key.encode()).hexdigest()[:8]


def _system_hash(payload: dict) -> str:
    system = payload.get('system')
    parts: list[str] = []
    if isinstance(system, str):
        if system:
            parts.append(system)
    elif isinstance(system, list):
        for blk in system:
            if isinstance(blk, dict) and isinstance(blk.get('text'), str):
                parts.append(blk['text'])
            elif isinstance(blk, str):
                parts.append(blk)
    text = '\n'.join(parts)
    return hashlib.sha256(text.encode()).hexdigest()[:8] if text else '--------'


def _conversation_anchor(payload: dict) -> str:
    """Hash the conversation's first user message as a per-conversation anchor.

    Claude Code reuses one ``metadata.user_id`` for a Task sub-agent and the
    parent that spawned it, so the session alone cannot separate them; the first
    user turn is distinct per agent and stable across that agent's own turns.
    """
    messages = payload.get('messages')
    if not isinstance(messages, list) or not messages:
        return '--------'
    for msg in messages:
        if not isinstance(msg, dict) or msg.get('role') != 'user':
            continue
        extracted = _extract_user_text(msg.get('content'))
        if extracted is None:
            continue
        stripped, raw, _non_text, _has_images = extracted
        text = stripped or last_transcript_user_turn(raw)
        if text:
            return hashlib.sha256(text[:_ANCHOR_LIMIT].encode()).hexdigest()[:8]
        break
    return '--------'


def context_key(sess_key: str | None, payload: dict) -> str | None:
    """Routing key: session key plus first-user-message hash, or None."""
    if not sess_key:
        return None
    return f'{sess_key}\x00{_conversation_anchor(payload)}'


# ---------------------------------------------------------------------------
# Response shaping
# ---------------------------------------------------------------------------

def _local_message(markdown: str, model: str) -> dict:
    return {
        'id': f'msg_{uuid.uuid4().hex}',
        'type': 'message',
        'role': 'assistant',
        'content': [{'type': 'text', 'text': markdown}],
        'model': model or 'anthrouter',
        'stop_reason': 'end_turn',
        'stop_sequence': None,
        'usage': {'input_tokens': 0, 'output_tokens': 0},
    }


def _local_message_sse(markdown: str, model: str):
    message_id = f'msg_{uuid.uuid4().hex}'
    model = model or 'anthrouter'
    return iter([
        sse_event('message_start', {
            'type': 'message_start',
            'message': {
                'id': message_id,
                'type': 'message',
                'role': 'assistant',
                'content': [],
                'model': model,
                'stop_reason': None,
                'stop_sequence': None,
                'usage': {'input_tokens': 0, 'output_tokens': 0},
            },
        }),
        sse_event('content_block_start', {
            'type': 'content_block_start',
            'index': 0,
            'content_block': {'type': 'text', 'text': ''},
        }),
        sse_event('content_block_delta', {
            'type': 'content_block_delta',
            'index': 0,
            'delta': {'type': 'text_delta', 'text': markdown},
        }),
        sse_event('content_block_stop', {'type': 'content_block_stop', 'index': 0}),
        sse_event('message_delta', {
            'type': 'message_delta',
            'delta': {'stop_reason': 'end_turn', 'stop_sequence': None},
            'usage': {'output_tokens': 0},
        }),
        sse_event('message_stop', {'type': 'message_stop'}),
    ])


_USAGE_FIELD_MAP = (
    ('input_tokens', 'input_tokens'),
    ('output_tokens', 'output_tokens'),
    ('cache_creation_input_tokens', 'cache_creation_tokens'),
    ('cache_read_input_tokens', 'cache_read_tokens'),
)


def _extract_sse_stats(chunk: str, stats: dict) -> None:
    """Track cumulative token counts across SSE events by running max.

    Anthropic usage counts are cumulative and the final ``message_delta``
    re-states the whole snapshot, sometimes with a larger input total than
    ``message_start``.  Summing them double-counts the cached prefix and inflates
    a measured context to roughly twice its real size.
    """
    for m in _SSE_DATA_RE.finditer(chunk):
        try:
            event = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        etype = event.get('type', '')
        if etype == 'message_start':
            usage = event.get('message', {}).get('usage', {}) or {}
        elif etype == 'message_delta':
            usage = event.get('usage', {}) or {}
        else:
            continue
        for wire_key, stat_key in _USAGE_FIELD_MAP:
            val = usage.get(wire_key)
            if val is None:
                continue
            stats[stat_key] = max(stats[stat_key], int(val or 0))


def _extract_sse_text(chunk: str, text_parts: list) -> None:
    """Accumulate assistant text deltas into *text_parts*, capped and never raising."""
    current_len = sum(len(p) for p in text_parts)
    if current_len >= _RESPONSE_TEXT_LIMIT:
        return
    for m in _SSE_DATA_RE.finditer(chunk):
        try:
            event = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if event.get('type') != 'content_block_delta':
            continue
        delta = event.get('delta') or {}
        if delta.get('type') == 'text_delta':
            t = delta.get('text', '')
            if isinstance(t, str) and t:
                current_len += len(t)
                if current_len > _RESPONSE_TEXT_LIMIT:
                    text_parts.append(t[:max(0, _RESPONSE_TEXT_LIMIT - (current_len - len(t)))])
                    return
                text_parts.append(t)


def _extract_response_text(result: dict) -> str | None:
    parts: list[str] = []
    current_len = 0
    for block in (result.get('content') or []):
        if isinstance(block, dict) and block.get('type') == 'text':
            t = block.get('text', '')
            if isinstance(t, str) and t:
                remaining = _RESPONSE_TEXT_LIMIT - current_len
                if remaining <= 0:
                    break
                if len(t) > remaining:
                    parts.append(t[:remaining])
                    break
                parts.append(t)
                current_len += len(t)
    return ''.join(parts) or None


def _rewrite_message_start_model(chunk: str, requested_model: str) -> str:
    """Restore the client's requested model in any ``message_start`` in *chunk*.

    Chunks without a rewrite pass through byte-for-byte so the upstream's own SSE
    framing survives.
    """
    if 'message_start' not in chunk:
        return chunk
    changed = False

    def _sub(m: 're.Match') -> str:
        nonlocal changed
        try:
            event = json.loads(m.group(1))
        except json.JSONDecodeError:
            return m.group(0)
        message = event.get('message')
        if event.get('type') != 'message_start' or not isinstance(message, dict):
            return m.group(0)
        if message.get('model') == requested_model:
            return m.group(0)
        message['model'] = requested_model
        changed = True
        return 'data: ' + json.dumps(event)

    result = _SSE_DATA_RE.sub(_sub, chunk)
    return result if changed else chunk


def _extract_user_prompt_text(payload: dict) -> str | None:
    """Raw text of the last user message for the request log.

    A tool_result-only turn yields bracketed descriptions rather than nothing, so
    the log shows which tools fired instead of an empty row.
    """
    messages = payload.get('messages')
    if not isinstance(messages, list) or not messages:
        return None
    for msg in reversed(messages):
        if not isinstance(msg, dict) or msg.get('role') != 'user':
            continue
        content = msg.get('content')
        if isinstance(content, str):
            return content or None
        if not isinstance(content, list):
            return None
        text_parts: list[str] = []
        tool_result_parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get('type')
            if btype == 'text':
                t = block.get('text', '')
                if isinstance(t, str) and t:
                    text_parts.append(t)
            elif btype == 'tool_result':
                tid = block.get('tool_use_id') or '?'
                result_content = block.get('content')
                if isinstance(result_content, str):
                    preview = result_content[:300]
                elif isinstance(result_content, list):
                    preview = ' '.join(
                        b.get('text', '') for b in result_content
                        if isinstance(b, dict) and b.get('type') == 'text'
                        and isinstance(b.get('text'), str)
                    )[:300]
                else:
                    preview = ''
                error_tag = ' [error]' if block.get('is_error') else ''
                tool_result_parts.append(f'[tool_result {tid}{error_tag}: {preview!r}]')
        if text_parts:
            return ''.join(text_parts) or None
        if tool_result_parts:
            return '\n'.join(tool_result_parts) or None
        return None
    return None


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

def _parse_query(query_string: str) -> dict:
    """Flatten a query string to one value per key; last occurrence wins."""
    return {
        key: values[-1]
        for key, values in urllib.parse.parse_qs(query_string, keep_blank_values=True).items()
    }


class ProxyRequestHandler(BaseHTTPRequestHandler):
    # Injected by the server factory.
    config = None
    transport = None
    sessions = None
    request_db = None   # RequestDB, or None when DB recording is disabled

    def do_POST(self):
        self._reset_request_state()
        path = self.path.split('?')[0].rstrip('/')
        if path == '/v1/messages':
            self._handle_messages()
        elif path == '/v1/messages/count_tokens':
            self._handle_count_tokens()
        else:
            self._send_json(404, anthropic_error_payload('not_found_error', 'Not found'))

    def do_GET(self):
        self._reset_request_state()
        raw_path, _, query_string = self.path.partition('?')
        path = raw_path.rstrip('/')
        if path == '/health':
            self._send_json(200, {'status': 'ok'})
            return
        if not (path.startswith('/admin') or path.startswith('/ui')):
            self._send_json(404, anthropic_error_payload('not_found_error', 'Not found'))
            return
        if not self.config.enable_ui:
            self._send_json(404, anthropic_error_payload('not_found_error',
                                                         'Admin UI not enabled'))
            return
        if path.startswith('/ui'):
            self._serve_ui_file(raw_path)
            return
        try:
            status, body = admin.handle_get(
                path or '/admin', _parse_query(query_string), self.request_db, self.config,
            )
            self._send_json(status, body)
        except Exception:
            logger.exception('%s Admin GET failed', self._log_tag())
            self._send_json(500, anthropic_error_payload('api_error', 'Admin handler failed'))

    def _serve_ui_file(self, raw_path: str):
        """Serve the built SPA from ``anthrouter/ui/dist``, index.html as fallback."""
        ui_dist = (Path(__file__).parent / 'ui' / 'dist').resolve()
        index_path = ui_dist / 'index.html'
        relative = raw_path[len('/ui'):].lstrip('/') or 'index.html'
        file_path = (ui_dist / relative).resolve()

        # A resolved path outside dist means the request walked out with '..'.
        if not file_path.is_relative_to(ui_dist) or not file_path.is_file():
            file_path = index_path
        if not file_path.is_file():
            self._send_json(404, anthropic_error_payload(
                'not_found_error', 'UI bundle not built'))
            return

        mime_type = mimetypes.guess_type(str(file_path))[0] or 'application/octet-stream'
        content = file_path.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', mime_type)
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'no-cache' if file_path == index_path
                         else 'public, max-age=31536000')
        self.end_headers()
        self.wfile.write(content)

    def _reset_request_state(self) -> None:
        # Reset per request: HTTP keep-alive reuses one handler instance across
        # requests, so leftover state would leak from one request into the next.
        self._req_start = time.monotonic()
        self._session_prefix = None
        self._session_hash = None
        self._ctx_key = None
        self._route_est = 0
        self._routing = None
        self._prompt_capture = {}
        self._sanitizer_events = []
        self._ratelimit = {}

    # -- messages -------------------------------------------------------------

    def _handle_messages(self):
        payload = None
        dispatched = False
        try:
            self._validate_content_type()
            payload = self._parse_json(self._read_body())

            command = parse_local_command(payload)
            if command is not None:
                self._handle_local_command(command, payload)
                return

            anthropic_beta = self.headers.get('anthropic-beta', '')
            if anthropic_beta:
                payload['_anthropic_beta'] = [
                    item.strip() for item in anthropic_beta.split(',') if item.strip()
                ]

            sess_key = session_key(payload)
            self._session_prefix = _session_short_id(sess_key) if sess_key else None
            self._session_hash = _system_hash(payload)

            credentials = extract_client_credentials(self.headers)
            self._prepare(payload, sess_key)
            dispatched = True
            self._dispatch(payload, credentials, sess_key)

        except AnthropicRequestError as exc:
            if dispatched:
                self._record_failed_request(
                    payload, f'{exc.status_code} — {exc.error_type}',
                    status='rate_limited' if exc.status_code == 429 else 'error',
                )
            self._send_error(exc)
        except _CLIENT_DISCONNECT:
            logger.info('%s client disconnected before response', self._log_tag())
        except Exception as exc:
            logger.exception('%s Proxy failure: %s', self._log_tag(), exc)
            if dispatched:
                self._record_failed_request(payload, '502 — upstream_failure')
            self._send_json(502, anthropic_error_payload('api_error', 'Upstream request failed'))

    def _prepare(self, payload: dict, sess_key: str | None) -> None:
        """Route, sanitize, and capture prompt hashes — once, before dispatch."""
        config = self.config
        routing_on = sess_key is not None and self.sessions.model_routing_enabled(sess_key)
        # A per-session routing override must reach route_model, which reads the
        # flag off the config it is handed rather than consulting session state.
        if bool(config.auto_model_routing) != routing_on and sess_key is not None:
            config = replace(config, auto_model_routing=routing_on)

        ctx_key = context_key(sess_key, payload) if routing_on else None
        cached_tier = self.sessions.routed_tier(ctx_key) if ctx_key else None
        floor_active = (
            ctx_key is not None
            and config.auto_model_routing_long_context_threshold > 0
        )
        session_floor, session_ratio = (
            self.sessions.context(ctx_key) if floor_active else (0, 1.0)
        )

        lock = config.lock_requested_model
        baseline_model = lock if lock != 'off' else None
        if baseline_model:
            logger.info('%s Model lock: forcing routing baseline from %s to %s',
                        self._log_tag(), payload.get('model'), baseline_model)

        target = RoutingTarget(config=config, backend=self.transport)
        routing = route_model(
            payload, target, {}, cached_tier, session_floor, session_ratio,
            log_tag=self._log_tag(), ctx_key=ctx_key, baseline_model=baseline_model,
        )

        routing = self._apply_tier_cache(routing, payload, ctx_key, config)

        self._routing = routing
        self._ctx_key = ctx_key if floor_active else None
        self._route_est = routing.estimated_input_tokens

        if routing.applied or routing.reason_code not in ('disabled', 'model_not_eligible'):
            logger.info(
                '%s Model routing: requested=%s routed=%s classification=%s '
                'applied=%s reason=%s ctx_anchor=%s predicted=%d sess_ctx=%d '
                'est_ratio=%.2f mode=%s',
                self._log_tag(), routing.requested_model, routing.routed_model,
                routing.classification, routing.applied, routing.reason_code,
                ctx_key.split('\x00', 1)[-1] if ctx_key else '--------',
                routing.predicted_input_tokens, routing.session_context_tokens,
                routing.session_estimate_ratio, routing.classifier_mode,
            )

        self._prompt_capture = self._extract_prompt_capture(payload)
        sanitized = sanitize_system_prompt(
            payload, getattr(config, 'sanitize_system_prompt', 'off'), sess_key,
            log_tag=self._log_tag(),
        )
        if sanitized.sanitized_sha256 is not None:
            self._prompt_capture['system_prompt_sanitized_sha256'] = sanitized.sanitized_sha256
            entries = self._prompt_capture.get('prompt_store_entries')
            if isinstance(entries, dict) and sanitized.sanitized_content is not None:
                entries[sanitized.sanitized_sha256] = (
                    'system_sanitized', sanitized.sanitized_content,
                )
        self._sanitizer_events = [
            {
                'block_type': block['block_type'],
                'is_allowlisted': True,
                'payload_preview': block['preview'],
            }
            for block in sanitized.stripped_blocks
        ] + [
            # Flagged but not stripped: the allowlist did not cover it, and
            # silently dropping content the operator cannot see would be
            # undebuggable client-side.
            {
                'block_type': f"index:{block['index']}",
                'is_allowlisted': False,
                'payload_preview': (
                    f"volatile: {block['distinct']} distinct values over "
                    f"{block['requests']} requests (ratio {block['ratio']})"
                ),
            }
            for block in sanitized.flagged
        ]

    def _apply_tier_cache(self, routing, payload: dict, ctx_key: str | None, config):
        """Persist a fresh classification, or replay the cached tier for a text-less turn.

        The cached tier is a last-resort fallback: it is read only when this turn
        yielded no user text at all, and it is capped so a replay can never route
        above what the client asked for.
        """
        if ctx_key is None:
            return routing
        if routing.classification is not None:
            tier = (
                routing.cache_tier or routing.routed_model
                if routing.reason_code == 'affirmation_classified'
                else routing.routed_model
            )
            self.sessions.set_routed_tier(ctx_key, tier)
            return routing
        if routing.reason_code != 'missing_final_user_text':
            return routing
        cached = self.sessions.routed_tier(ctx_key)
        if cached is None:
            return routing
        capped = _cap_cached_tier(
            cached, routing.requested_model,
            label_map=config.auto_model_routing_classification,
        )
        payload['model'] = capped
        return replace(
            routing,
            routed_model=capped,
            applied=(capped != routing.requested_model),
            reason_code=('session_cached_tier_capped' if capped != cached
                         else 'session_cached_tier'),
        )

    def _dispatch(self, payload: dict, credentials: dict, sess_key: str | None) -> None:
        streaming = bool(payload.get('stream'))
        logger.info('%s Routing request: operation=messages stream=%s model=%s',
                    self._log_tag(), streaming, payload.get('model', ''))
        start_time = time.monotonic()
        model = payload.get('model', '')
        sid = sess_key or UNTRACKED_SESSION_ID

        if streaming:
            sse_gen = self.transport.send_message_stream(
                payload, credentials, self.config, ratelimit_out=self._ratelimit,
            )
            wrapped = self._usage_sse_wrapper(sse_gen, start_time, model, sid)
            self._send_sse(self._rewrite_response_model_sse(wrapped))
            return

        result = self.transport.send_message(
            payload, credentials, self.config, ratelimit_out=self._ratelimit,
        )
        self.sessions.record_ratelimit(self._ratelimit)
        usage = result.get('usage', {}) or {}
        stats = {
            'input_tokens': usage.get('input_tokens', 0),
            'output_tokens': usage.get('output_tokens', 0),
            'cache_creation_tokens': usage.get('cache_creation_input_tokens', 0),
            'cache_read_tokens': usage.get('cache_read_input_tokens', 0),
        }
        self._record_session_context(stats)
        self._record_db(
            sid, stats, int((time.monotonic() - start_time) * 1000), 'success',
            response_text=_extract_response_text(result),
        )

        routing = self._routing
        if routing is not None and routing.applied and 'model' in result:
            result['model'] = routing.requested_model
        self._send_json(200, result)

    def _rewrite_response_model_sse(self, sse_gen):
        """Echo the client's requested model in ``message_start``; no-op if unrouted."""
        routing = self._routing
        if routing is None or not routing.applied:
            return sse_gen
        requested = routing.requested_model

        def _wrapped():
            for chunk in sse_gen:
                yield _rewrite_message_start_model(chunk, requested)
        return _wrapped()

    def _usage_sse_wrapper(self, sse_gen, start_time: float, model: str, sid: str):
        """Yield SSE chunks, parsing usage, text, and errors in a single pass."""
        stats = {
            'input_tokens': 0,
            'output_tokens': 0,
            'cache_creation_tokens': 0,
            'cache_read_tokens': 0,
        }
        capture_text = self.request_db is not None
        text_parts: list[str] = []
        errored = False
        # An upstream failure raised out of the stream is delivered by _send_sse
        # as an out-of-band error frame, so it never appears as an "event: error"
        # line here.  Without capturing it, a stream that carried no response at
        # all would be recorded as a success that cost nothing.
        upstream_error: tuple[int | None, str] | None = None
        # Tail of the previous chunk, so an "event: error" header split across a
        # chunk boundary is still detected.
        line_tail = ''
        try:
            for chunk in sse_gen:
                _extract_sse_stats(chunk, stats)
                if capture_text:
                    _extract_sse_text(chunk, text_parts)
                if 'event: error' in line_tail + chunk:
                    errored = True
                line_tail = chunk[-20:]
                yield chunk
        except AnthropicRequestError as exc:
            upstream_error = (exc.status_code, exc.error_type)
            raise
        except Exception:
            upstream_error = (502, 'upstream_failure')
            raise
        finally:
            close = getattr(sse_gen, 'close', None)
            if close is not None:
                try:
                    close()
                except Exception:
                    logger.debug('%s sse_gen close failed', self._log_tag(), exc_info=True)
            duration_ms = int((time.monotonic() - start_time) * 1000)
            self.sessions.record_ratelimit(self._ratelimit)
            if upstream_error is not None:
                errored = True
            self._record_session_context(stats)
            if upstream_error is not None:
                db_stats: dict = {}
                db_error: str | None = f'{upstream_error[0]} — {upstream_error[1]}'
            else:
                db_stats = stats
                db_error = 'sse_error' if errored else None
            self._record_db(
                sid, db_stats, duration_ms, 'error' if errored else 'success',
                error=db_error, response_text=''.join(text_parts) or None,
            )

    # -- recording ------------------------------------------------------------

    def _extract_prompt_capture(self, payload: dict) -> dict:
        """Hash the prompt as the client sent it.  Never raises.

        ``system_prompt_sha256`` is taken before the sanitizer runs, so the column
        always holds the client's original prompt and a later change in client
        behaviour stays observable.
        """
        try:
            system = payload.get('system')
            system_content: str | None = None
            system_sha: str | None = None
            if system:
                if isinstance(system, str):
                    system_content = system
                elif isinstance(system, list):
                    system_content = json.dumps(system, sort_keys=True, ensure_ascii=False)
                if system_content is not None:
                    system_sha = hashlib.sha256(system_content.encode('utf-8')).hexdigest()

            tools = payload.get('tools')
            tools_content: str | None = None
            tools_sha: str | None = None
            if tools:
                tools_content = json.dumps(tools, sort_keys=True, ensure_ascii=False)
                tools_sha = hashlib.sha256(tools_content.encode('utf-8')).hexdigest()

            entries: dict[str, tuple[str, str]] = {}
            if system_sha is not None and system_content is not None:
                entries[system_sha] = ('system', system_content)
            if tools_sha is not None and tools_content is not None:
                entries[tools_sha] = ('tools', tools_content)

            return {
                'user_prompt_text': _extract_user_prompt_text(payload),
                'system_prompt_sha256': system_sha,
                'tools_sha256': tools_sha,
                'prompt_store_entries': entries,
            }
        except Exception:
            logger.warning('%s prompt capture extraction failed', self._log_tag(),
                           exc_info=True)
            return {}

    def _record_db(self, sid: str, stats: dict, duration_ms: int, status: str,
                   error: str | None = None, response_text: str | None = None) -> None:
        if self.request_db is None or self._routing is None:
            return
        try:
            self.request_db.record_request(
                session_id=sid,
                routing_decision=self._routing,
                stats_dict=stats,
                duration_ms=duration_ms,
                status=status,
                error=error,
                response_text=response_text,
                sanitizer_events=self._sanitizer_events,
                ratelimit=self._ratelimit,
                **self._prompt_capture,
            )
        except Exception:
            logger.debug('%s db record failed', self._log_tag(), exc_info=True)

    def _record_failed_request(self, payload, error: str, status: str = 'error') -> None:
        """Record a dispatch that failed before any usage was learned.

        The empty ``stats_dict`` records usage as absent rather than zero, so a
        request whose cost was never learned is not summed as free.
        """
        sid = (session_key(payload) if isinstance(payload, dict) else None) \
            or UNTRACKED_SESSION_ID
        duration_ms = int((time.monotonic() - self._req_start) * 1000)
        self._record_db(sid, {}, duration_ms, status, error=error)

    def _record_session_context(self, stats: dict) -> None:
        """Store the measured context size and refreshed calibration ratio.

        A zero measurement (e.g. an early client disconnect) is skipped so it
        cannot wrongly reset a real floor.
        """
        ctx_key = self._ctx_key
        if not ctx_key:
            return
        measured_input = (
            int(stats.get('input_tokens') or 0)
            + int(stats.get('cache_creation_tokens') or 0)
            + int(stats.get('cache_read_tokens') or 0)
        )
        floor = measured_input + int(stats.get('output_tokens') or 0)
        if floor <= 0:
            return
        _prior_floor, prior_ratio = self.sessions.context(ctx_key)
        ratio = calibrated_ratio(measured_input, self._route_est, prior_ratio)
        self.sessions.record_context(ctx_key, floor, ratio)

    # -- local commands -------------------------------------------------------

    def _handle_local_command(self, command, payload: dict):
        name, arg = command
        sess_key = session_key(payload)
        self._session_prefix = _session_short_id(sess_key) if sess_key else None
        self._session_hash = _system_hash(payload)
        logger.info('%s Routing request: operation=local_command command=%s',
                    self._log_tag(), name)

        if name == 'help':
            markdown = self._help_markdown()
        elif name == 'status':
            markdown = self._status_markdown(sess_key)
        elif name == 'set-model-routing':
            markdown = self._set_model_routing_markdown(arg)
        else:
            markdown = self._session_set_model_routing_markdown(arg, sess_key)

        model = payload.get('model', '')
        if payload.get('stream'):
            self._send_sse(_local_message_sse(markdown, model))
        else:
            self._send_json(200, _local_message(markdown, model))

    def _help_markdown(self) -> str:
        return (
            '## anthrouter\n\n'
            'Single-backend Anthropic proxy: model-tier routing plus '
            'system-prompt sanitization. Your own credential is forwarded '
            'untouched.\n\n'
            '**Commands**\n\n'
            '- `proxy-help` — this message\n'
            '- `proxy-status` — routing mode and the last observed rate-limit window\n'
            '- `proxy-set-model-routing:on|off` — toggle auto model routing globally\n'
            '- `proxy-set-model-routing:on|off|auto:session` — override for this '
            'session; `auto` clears the override and follows the global setting\n'
        )

    def _status_markdown(self, sess_key: str | None) -> str:
        config = self.config
        global_on = self.sessions.global_model_routing
        lines = [
            '## anthrouter status\n',
            f'**Auto model routing (global):** {"on" if global_on else "off"}',
        ]
        if global_on:
            mode = config.auto_model_routing_mode or 'classifier'
            lines.append(f'**Routing mode:** `{mode}`')
            if mode == 'classifier':
                lines.append(
                    f'**Classifier model:** `{config.auto_model_routing_classifier_model}`'
                )
            targets = ', '.join(
                f'{label} → `{model}`'
                for label, model in config.auto_model_routing_classification.items()
            )
            lines.append(f'**Tier targets:** {targets}')
        if sess_key is not None:
            override = self.sessions.session_model_routing(sess_key)
            if override is not None:
                lines.append(
                    f'**Auto model routing (this session):** {"on" if override else "off"}'
                )

        window = self.sessions.last_ratelimit()
        lines.append('')
        if window:
            lines.append('**Rate-limit window (last upstream response)**\n')
            labels = (
                ('requests_remaining', 'Requests remaining'),
                ('tokens_remaining', 'Tokens remaining'),
                ('input_tokens_remaining', 'Input tokens remaining'),
                ('output_tokens_remaining', 'Output tokens remaining'),
                ('reset_at', 'Resets at'),
            )
            for key, label in labels:
                if key in window:
                    lines.append(f'- {label}: `{window[key]}`')
        else:
            lines.append(
                '**Rate-limit window:** no upstream response observed yet.'
            )
        return '\n'.join(lines)

    def _set_model_routing_markdown(self, arg) -> str:
        if arg == 'invalid':
            return (
                f'{_ROUTING_HEADER}**Error:** unrecognised value. '
                'Use `proxy-set-model-routing:on` or `proxy-set-model-routing:off`.'
            )
        self.sessions.set_model_routing(arg)
        return f'{_ROUTING_HEADER}**Auto model routing:** {"on" if arg else "off"} (global)'

    def _session_set_model_routing_markdown(self, arg, sess_key: str | None) -> str:
        if arg == 'invalid':
            return (
                f'{_ROUTING_HEADER}**Error:** unrecognised value. '
                'Use `proxy-set-model-routing:on:session`, '
                '`proxy-set-model-routing:off:session`, or '
                '`proxy-set-model-routing:auto:session` to clear.'
            )
        if sess_key is None:
            return (
                f'{_ROUTING_HEADER}**Error:** session key not available — '
                '`metadata.user_id` is required for session-scoped commands.'
            )
        self.sessions.set_session_model_routing(sess_key, arg)
        if arg is None:
            return (
                f'{_ROUTING_HEADER}'
                '**Auto model routing (this session):** following global setting'
            )
        return (
            f'{_ROUTING_HEADER}'
            f'**Auto model routing (this session):** {"on" if arg else "off"}\n\n'
            '_Use `proxy-set-model-routing:auto:session` to follow the global setting._'
        )

    # -- count_tokens ---------------------------------------------------------

    def _handle_count_tokens(self):
        try:
            self._validate_content_type()
            payload = self._parse_json(self._read_body())
            sess_key = session_key(payload)
            self._session_prefix = _session_short_id(sess_key) if sess_key else None
            self._session_hash = _system_hash(payload)
            credentials = extract_client_credentials(self.headers)
            logger.info('%s Routing request: operation=count_tokens', self._log_tag())
            self._send_json(200, self.transport.count_tokens(
                payload, credentials, self.config))
        except AnthropicRequestError as exc:
            self._send_error(exc)
        except _CLIENT_DISCONNECT:
            logger.info('%s client disconnected before response', self._log_tag())
        except Exception as exc:
            logger.exception('%s Count tokens failure: %s', self._log_tag(), exc)
            self._send_json(502, anthropic_error_payload('api_error', 'Upstream request failed'))

    # -- transport plumbing ---------------------------------------------------

    def _log_tag(self) -> str:
        sess = getattr(self, '_session_prefix', None) or '--------'
        shash = getattr(self, '_session_hash', None) or '--------'
        start = getattr(self, '_req_start', None)
        if start is not None:
            return f'[{sess} {shash} +{time.monotonic() - start:.2f}s]'
        return f'[{sess} {shash}]'

    def _send_error(self, exc: AnthropicRequestError) -> None:
        self._send_json(exc.status_code, anthropic_error_payload(exc.error_type, exc.message))

    def _validate_content_type(self):
        if 'application/json' not in self.headers.get('Content-Type', ''):
            raise AnthropicRequestError(
                'content-type must be application/json',
                error_type='invalid_request_error',
                status_code=400,
            )

    def _read_body(self) -> bytes:
        return self.rfile.read(int(self.headers.get('Content-Length', 0)))

    def _parse_json(self, body: bytes) -> dict:
        try:
            return json.loads(body or b'{}')
        except (json.JSONDecodeError, ValueError):
            raise AnthropicRequestError('Malformed JSON body')

    def _send_json(self, status: int, data: dict):
        body = json.dumps(data).encode('utf-8')
        try:
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            if status == 429:
                self.send_header('Retry-After', '1')
            self.end_headers()
            self.wfile.write(body)
        except _CLIENT_DISCONNECT:
            logger.info('%s client disconnected before response', self._log_tag())

    def _prime_sse_with_keepalive(self, generator, req_start):
        """Prime the first chunk, sending keepalive comments while waiting.

        Headers are already committed, so a slow upstream would otherwise leave
        the socket idle for the whole TTFB.  Returns ``(first_chunk, error)``;
        an ``AnthropicRequestError`` raised while priming is returned rather than
        propagated, because it must be delivered in-band as SSE.

        The background thread only reads from *generator*; the main thread only
        writes keepalives.  After ``primed.set()`` the thread has exited and the
        main thread owns both for the rest of the stream.
        """
        interval = getattr(self.config, 'sse_keepalive_interval', 0)
        if not isinstance(interval, (int, float)) or interval <= 0:
            try:
                return next(generator), None
            except StopIteration:
                return None, None
            except AnthropicRequestError as exc:
                return None, exc

        first_chunk = [None]
        priming_error = [None]
        primed = threading.Event()

        def _prime():
            try:
                first_chunk[0] = next(generator)
            except StopIteration:
                pass
            except AnthropicRequestError as exc:
                priming_error[0] = exc
            finally:
                primed.set()

        threading.Thread(target=_prime, daemon=True, name='sse-prime').start()

        keepalive_count = 0
        while not primed.wait(timeout=interval):
            try:
                self.wfile.write(b': keepalive\n\n')
                self.wfile.flush()
                keepalive_count += 1
            except _CLIENT_DISCONNECT:
                logger.info('%s client disconnected during SSE keepalive '
                            '(keepalives_sent=%d)', self._log_tag(), keepalive_count)
                primed.wait()  # never abandon the priming thread
                break
            except Exception as exc:
                logger.warning('%s SSE keepalive write error: %s', self._log_tag(), exc)
                primed.wait()
                break

        if keepalive_count:
            elapsed = time.monotonic() - req_start if req_start is not None else 0.0
            logger.info('%s SSE keepalive complete (keepalives_sent=%d ttfb=%.2fs)',
                        self._log_tag(), keepalive_count, elapsed)

        return first_chunk[0], priming_error[0]

    def _send_sse_error(self, exc: AnthropicRequestError) -> None:
        frame = sse_event('error', anthropic_error_payload(exc.error_type, exc.message))
        try:
            self.wfile.write(frame.encode('utf-8'))
            self.wfile.flush()
        except _CLIENT_DISCONNECT:
            pass
        except Exception:
            logger.debug('%s error sending SSE error event', self._log_tag(), exc_info=True)

    def _send_sse(self, generator):
        """Stream an SSE response, committing HTTP 200 before priming upstream.

        Because the status line is already sent, a pre-stream upstream failure is
        delivered as an in-band SSE error event rather than an HTTP status.
        """
        req_start = getattr(self, '_req_start', None)
        chunks_sent = 0
        try:
            # The header commit is inside the try: on a cancelled request
            # end_headers is where the broken pipe first surfaces.
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('X-Accel-Buffering', 'no')
            self.end_headers()

            first_chunk, priming_error = self._prime_sse_with_keepalive(
                generator, req_start)
            if priming_error is not None:
                logger.warning('%s SSE priming error (post-header): %s',
                               self._log_tag(), priming_error)
                self._send_sse_error(priming_error)
                return

            if first_chunk is not None:
                self._write_chunk(first_chunk)
                chunks_sent += 1
            for chunk in generator:
                self._write_chunk(chunk)
                chunks_sent += 1
        except _CLIENT_DISCONNECT:
            logger.info('%s client disconnected mid-stream (chunks_sent=%d)',
                        self._log_tag(), chunks_sent)
        except AnthropicRequestError as exc:
            logger.warning('%s SSE stream error after headers committed '
                           '(chunks_sent=%d): %s', self._log_tag(), chunks_sent, exc)
            self._send_sse_error(exc)
        except Exception as exc:
            # Headers and data are already sent, so no new HTTP status is
            # possible: close the stream and let the client see a clean EOF.
            logger.error('%s SSE stream failure after headers committed '
                         '(chunks_sent=%d): %s', self._log_tag(), chunks_sent, exc)
        finally:
            close = getattr(generator, 'close', None)
            if close is not None:
                try:
                    close()
                except Exception:
                    logger.debug('%s error closing SSE generator',
                                 self._log_tag(), exc_info=True)

    def _write_chunk(self, chunk: str) -> None:
        self.wfile.write(chunk.encode('utf-8'))
        self.wfile.flush()

    def log_message(self, format, *args):
        logger.info('%s ' + format, self._log_tag(), *args)
