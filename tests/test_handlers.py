import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from anthrouter.config import Config
from anthrouter.handlers import (
    _extract_response_text,
    _extract_sse_stats,
    _extract_sse_text,
    _rewrite_message_start_model,
    context_key,
    parse_local_command,
    session_key,
)
from anthrouter.model_config import resolve_model
from anthrouter.server import create_server
from anthrouter.session_state import MAX_ENTRIES, SessionState

SESSION = json.dumps({'session_id': '11111111-2222-3333-4444-555555555555'})

# Classifies 'deep' under rules mode, so a sonnet request is actually rewritten.
DEEP_PROMPT = 'architect a distributed system'

SSE_BODY = (
    'event: message_start\n'
    'data: {"type":"message_start","message":{"id":"msg_1","type":"message",'
    '"role":"assistant","content":[],"model":"claude-haiku-4-5-20251001",'
    '"usage":{"input_tokens":100,"cache_read_input_tokens":900}}}\n\n'
    'event: content_block_delta\n'
    'data: {"type":"content_block_delta","index":0,'
    '"delta":{"type":"text_delta","text":"hello"}}\n\n'
    'event: message_delta\n'
    'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
    '"usage":{"input_tokens":100,"output_tokens":7,"cache_read_input_tokens":900}}\n\n'
    'event: message_stop\n'
    'data: {"type":"message_stop"}\n\n'
)


class _FakeUpstream(BaseHTTPRequestHandler):
    """Records what it received and replies with whatever the test configured."""

    received: list = []
    status = 200
    json_body: dict = {}

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        payload = json.loads(body or b'{}')
        type(self).received.append({
            'path': self.path,
            'payload': payload,
            'headers': {k.lower(): v for k, v in self.headers.items()},
        })

        if type(self).status != 200:
            data = json.dumps({'type': 'error', 'error': {
                'type': 'api_error', 'message': 'upstream exploded'}}).encode()
            self.send_response(type(self).status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if payload.get('stream'):
            data = SSE_BODY.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(data)))
            self._ratelimit_headers()
            self.end_headers()
            self.wfile.write(data)
            return

        body_out = dict(type(self).json_body)
        body_out.setdefault('model', payload.get('model'))
        data = json.dumps(body_out).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self._ratelimit_headers()
        self.end_headers()
        self.wfile.write(data)

    def _ratelimit_headers(self):
        self.send_header('anthropic-ratelimit-requests-remaining', '42')
        self.send_header('anthropic-ratelimit-tokens-remaining', '13000')
        self.send_header('anthropic-ratelimit-tokens-reset', '2026-08-31T12:00:00Z')

    def log_message(self, *args):
        pass


@pytest.fixture
def upstream():
    _FakeUpstream.received = []
    _FakeUpstream.status = 200
    _FakeUpstream.json_body = {
        'id': 'msg_up', 'type': 'message', 'role': 'assistant',
        'content': [{'type': 'text', 'text': 'hi there'}],
        'usage': {'input_tokens': 100, 'output_tokens': 7,
                  'cache_read_input_tokens': 900},
    }
    server = ThreadingHTTPServer(('127.0.0.1', 0), _FakeUpstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def proxy(upstream, tmp_path):
    def _build(**overrides):
        cfg = Config(**{
            'host': '127.0.0.1',
            'port': 0,
            'upstream_base_url': f'http://127.0.0.1:{upstream.server_address[1]}',
            'db_path': str(tmp_path / 'anthrouter.db'),
            'sse_keepalive_interval': 0,
            **overrides,
        })
        server = create_server(cfg)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        server.base_url = f'http://127.0.0.1:{server.server_address[1]}'
        started.append(server)
        return server

    started: list = []
    yield _build
    for server in started:
        server.shutdown()
        server.server_close()


def post(server, path, payload, headers=None, raw=False):
    request = urllib.request.Request(
        server.base_url + path,
        data=json.dumps(payload).encode(),
        headers={'Content-Type': 'application/json', 'x-api-key': 'sk-test',
                 **(headers or {})},
        method='POST',
    )
    with urllib.request.urlopen(request) as resp:
        body = resp.read().decode()
    return body if raw else json.loads(body)


def db_rows(server):
    return server.RequestHandlerClass.request_db.get_requests(limit=10)


# ---------------------------------------------------------------------------
# Local commands
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('text, expected', [
    ('proxy-help', ('help', None)),
    ('proxy-status', ('status', None)),
    ('proxy-set-model-routing:on', ('set-model-routing', True)),
    ('proxy-set-model-routing:off', ('set-model-routing', False)),
    ('proxy-set-model-routing:on:session', ('session-set-model-routing', True)),
    ('proxy-set-model-routing:auto:session', ('session-set-model-routing', None)),
    ('proxy-set-model-routing:sideways', ('set-model-routing', 'invalid')),
    ('what does proxy-help do?', None),
    ('proxy-set-backend:oauth', None),
])
def test_local_command_parsing(text, expected):
    payload = {'messages': [{'role': 'user', 'content': text}]}
    assert parse_local_command(payload) == expected


def test_command_survives_a_system_reminder_wrapper():
    payload = {'messages': [{'role': 'user', 'content': [
        {'type': 'text', 'text': '<system-reminder>be nice</system-reminder>'},
        {'type': 'text', 'text': 'proxy-help'},
    ]}]}
    assert parse_local_command(payload) == ('help', None)


def test_tool_result_block_disqualifies_a_command():
    payload = {'messages': [{'role': 'user', 'content': [
        {'type': 'tool_result', 'tool_use_id': 't1', 'content': 'proxy-help'},
    ]}]}
    assert parse_local_command(payload) is None


def test_help_command_never_reaches_upstream(proxy, upstream):
    server = proxy()
    result = post(server, '/v1/messages', {
        'model': 'sonnet', 'messages': [{'role': 'user', 'content': 'proxy-help'}]})
    assert 'anthrouter' in result['content'][0]['text']
    assert _FakeUpstream.received == []


def test_status_reports_the_last_rate_limit_window(proxy):
    server = proxy()
    post(server, '/v1/messages', {
        'model': 'sonnet', 'messages': [{'role': 'user', 'content': 'hello'}]})
    result = post(server, '/v1/messages', {
        'model': 'sonnet', 'messages': [{'role': 'user', 'content': 'proxy-status'}]})
    text = result['content'][0]['text']
    assert 'Requests remaining: `42`' in text
    assert 'Tokens remaining: `13000`' in text
    assert 'Resets at: `2026-08-31T12:00:00Z`' in text


def test_session_routing_override_beats_the_global_flag(proxy):
    server = proxy(auto_model_routing=True, auto_model_routing_mode='rules')
    post(server, '/v1/messages', {
        'model': 'sonnet', 'metadata': {'user_id': SESSION},
        'messages': [{'role': 'user',
                      'content': 'proxy-set-model-routing:off:session'}]})
    post(server, '/v1/messages', {
        'model': 'sonnet', 'metadata': {'user_id': SESSION},
        'messages': [{'role': 'user', 'content': DEEP_PROMPT}]})
    assert _FakeUpstream.received[-1]['payload']['model'] == resolve_model('sonnet')

    post(server, '/v1/messages', {
        'model': 'sonnet', 'metadata': {'user_id': SESSION},
        'messages': [{'role': 'user',
                      'content': 'proxy-set-model-routing:auto:session'}]})
    post(server, '/v1/messages', {
        'model': 'sonnet', 'metadata': {'user_id': SESSION},
        'messages': [{'role': 'user', 'content': DEEP_PROMPT}]})
    assert _FakeUpstream.received[-1]['payload']['model'] == resolve_model('opus')


def test_local_command_streams_when_the_client_asked_for_a_stream(proxy):
    server = proxy()
    body = post(server, '/v1/messages', {
        'model': 'sonnet', 'stream': True,
        'messages': [{'role': 'user', 'content': 'proxy-help'}]}, raw=True)
    assert body.startswith('event: message_start')
    assert 'event: message_stop' in body


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def test_missing_credentials_are_rejected_before_any_upstream_call(proxy):
    server = proxy()
    request = urllib.request.Request(
        server.base_url + '/v1/messages',
        data=json.dumps({'model': 'sonnet',
                         'messages': [{'role': 'user', 'content': 'hi'}]}).encode(),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(request)
    assert excinfo.value.code == 401
    assert _FakeUpstream.received == []


def test_client_credential_is_forwarded_untouched(proxy):
    server = proxy()
    post(server, '/v1/messages',
         {'model': 'sonnet', 'messages': [{'role': 'user', 'content': 'hi'}]},
         headers={'x-api-key': 'sk-ant-mine'})
    assert _FakeUpstream.received[0]['headers']['x-api-key'] == 'sk-ant-mine'


def test_non_streaming_response_echoes_the_requested_model(proxy):
    server = proxy(auto_model_routing=True, auto_model_routing_mode='rules')
    result = post(server, '/v1/messages', {
        'model': 'sonnet', 'metadata': {'user_id': SESSION},
        'messages': [{'role': 'user', 'content': DEEP_PROMPT}]})
    assert _FakeUpstream.received[0]['payload']['model'] == resolve_model('opus')
    assert result['model'] == 'sonnet'


def test_streaming_relays_chunks_and_restores_the_requested_model(proxy):
    server = proxy(auto_model_routing=True, auto_model_routing_mode='rules')
    body = post(server, '/v1/messages', {
        'model': 'sonnet', 'stream': True, 'metadata': {'user_id': SESSION},
        'messages': [{'role': 'user', 'content': DEEP_PROMPT}]}, raw=True)
    assert 'event: message_stop' in body
    start = json.loads(body.split('data: ', 1)[1].split('\n', 1)[0])
    assert start['message']['model'] == 'sonnet'


def test_unrouted_stream_passes_through_byte_for_byte(proxy):
    server = proxy()
    body = post(server, '/v1/messages', {
        'model': 'claude-haiku-4-5-20251001', 'stream': True,
        'messages': [{'role': 'user', 'content': 'hi'}]}, raw=True)
    assert body == SSE_BODY


def test_stream_primes_through_the_keepalive_path(proxy):
    server = proxy(sse_keepalive_interval=0.01)
    body = post(server, '/v1/messages', {
        'model': 'sonnet', 'stream': True,
        'messages': [{'role': 'user', 'content': 'hi'}]}, raw=True)
    assert body.endswith('event: message_stop\ndata: {"type":"message_stop"}\n\n')


def test_count_tokens_is_proxied_upstream(proxy):
    _FakeUpstream.json_body = {'input_tokens': 1234}
    server = proxy()
    result = post(server, '/v1/messages/count_tokens', {
        'model': 'sonnet', 'messages': [{'role': 'user', 'content': 'hi'}]})
    assert result['input_tokens'] == 1234
    assert _FakeUpstream.received[0]['path'].startswith('/v1/messages/count_tokens')


def test_malformed_json_is_a_client_error(proxy):
    server = proxy()
    request = urllib.request.Request(
        server.base_url + '/v1/messages', data=b'{not json',
        headers={'Content-Type': 'application/json', 'x-api-key': 'k'},
        method='POST')
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(request)
    assert excinfo.value.code == 400


def test_non_json_content_type_is_rejected(proxy):
    server = proxy()
    request = urllib.request.Request(
        server.base_url + '/v1/messages', data=b'{}',
        headers={'Content-Type': 'text/plain', 'x-api-key': 'k'}, method='POST')
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(request)
    assert excinfo.value.code == 400


def test_health_endpoint_answers_without_credentials(proxy):
    server = proxy()
    with urllib.request.urlopen(server.base_url + '/health') as resp:
        assert json.loads(resp.read())['status'] == 'ok'


def test_unknown_path_is_a_not_found_envelope(proxy):
    server = proxy()
    request = urllib.request.Request(
        server.base_url + '/v1/complete', data=b'{}',
        headers={'Content-Type': 'application/json'}, method='POST')
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(request)
    assert excinfo.value.code == 404
    assert json.loads(excinfo.value.read())['error']['type'] == 'not_found_error'


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

def test_successful_request_records_usage_and_the_rate_limit_window(proxy):
    server = proxy()
    post(server, '/v1/messages', {
        'model': 'sonnet', 'metadata': {'user_id': SESSION},
        'messages': [{'role': 'user', 'content': 'hello there'}]})
    row = db_rows(server)[0]
    assert row['status'] == 'success'
    assert (row['input_tokens'], row['output_tokens']) == (100, 7)
    assert row['cache_read_tokens'] == 900
    assert row['user_prompt_text'] == 'hello there'
    assert row['response_text'] == 'hi there'
    assert row['ratelimit_requests_remaining'] == 42
    assert row['ratelimit_reset_at'] == '2026-08-31T12:00:00Z'


def test_streaming_request_records_usage_by_max_not_by_sum(proxy):
    server = proxy()
    post(server, '/v1/messages', {
        'model': 'sonnet', 'stream': True, 'metadata': {'user_id': SESSION},
        'messages': [{'role': 'user', 'content': 'hello'}]}, raw=True)
    row = db_rows(server)[0]
    assert (row['input_tokens'], row['cache_read_tokens']) == (100, 900)
    assert row['output_tokens'] == 7
    assert row['response_text'] == 'hello'


def test_upstream_failure_records_absent_usage_not_zero(proxy):
    _FakeUpstream.status = 500
    server = proxy()
    request = urllib.request.Request(
        server.base_url + '/v1/messages',
        data=json.dumps({'model': 'sonnet',
                         'messages': [{'role': 'user', 'content': 'hi'}]}).encode(),
        headers={'Content-Type': 'application/json', 'x-api-key': 'k'},
        method='POST')
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(request)
    row = db_rows(server)[0]
    assert row['status'] == 'error'
    assert row['input_tokens'] is None
    assert row['cost_estimate'] is None


def test_pre_dispatch_failure_records_nothing(proxy):
    server = proxy()
    request = urllib.request.Request(
        server.base_url + '/v1/messages',
        data=json.dumps({'model': 'sonnet',
                         'messages': [{'role': 'user', 'content': 'hi'}]}).encode(),
        headers={'Content-Type': 'application/json'}, method='POST')
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(request)
    assert db_rows(server) == []


def test_original_system_hash_survives_sanitization(proxy):
    server = proxy(sanitize_system_prompt='strip')
    system = [
        {'type': 'text', 'text': 'x-anthropic-billing-header: cc_prompt_id=abc'},
        {'type': 'text', 'text': 'You are Claude Code.'},
    ]
    post(server, '/v1/messages', {
        'model': 'sonnet', 'system': system, 'metadata': {'user_id': SESSION},
        'messages': [{'role': 'user', 'content': 'hi'}]})

    assert _FakeUpstream.received[0]['payload']['system'] == [system[1]]
    row = db_rows(server)[0]
    assert row['system_prompt_sha256'] != row['system_prompt_sanitized_sha256']
    original = server.RequestHandlerClass.request_db.get_prompt(
        row['system_prompt_sha256'])
    assert 'cc_prompt_id' in original['content']

    events = server.RequestHandlerClass.request_db.get_sanitizer_events(row['id'])
    assert [(e['block_type'], e['is_allowlisted']) for e in events] == [
        ('x-anthropic-billing-header:', 1)]


def test_sanitizer_off_leaves_the_sanitized_hash_null(proxy):
    server = proxy(sanitize_system_prompt='off')
    post(server, '/v1/messages', {
        'model': 'sonnet', 'system': 'You are Claude Code.',
        'messages': [{'role': 'user', 'content': 'hi'}]})
    row = db_rows(server)[0]
    assert row['system_prompt_sha256'] is not None
    assert row['system_prompt_sanitized_sha256'] is None


# ---------------------------------------------------------------------------
# Session identity
# ---------------------------------------------------------------------------

def test_session_key_is_the_whole_user_id_blob():
    assert session_key({'metadata': {'user_id': SESSION}}) == SESSION
    assert session_key({'metadata': {'user_id': ''}}) is None
    assert session_key({}) is None


def test_context_key_separates_conversations_sharing_a_session():
    parent = {'messages': [{'role': 'user', 'content': 'build the parser'}]}
    child = {'messages': [{'role': 'user', 'content': 'search for the config'}]}
    assert context_key(SESSION, parent) != context_key(SESSION, child)


def test_context_key_is_stable_across_a_conversations_own_turns():
    first = {'messages': [{'role': 'user', 'content': 'build the parser'}]}
    later = {'messages': [
        {'role': 'user', 'content': 'build the parser'},
        {'role': 'assistant', 'content': 'done'},
        {'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': 't1', 'content': 'ok'}]},
    ]}
    assert context_key(SESSION, first) == context_key(SESSION, later)


def test_context_key_needs_a_session():
    assert context_key(None, {'messages': []}) is None


# ---------------------------------------------------------------------------
# SSE parsing helpers
# ---------------------------------------------------------------------------

def test_cumulative_usage_restatements_are_maxed_not_summed():
    stats = dict.fromkeys(
        ('input_tokens', 'output_tokens', 'cache_creation_tokens',
         'cache_read_tokens'), 0)
    _extract_sse_stats(SSE_BODY, stats)
    assert stats['input_tokens'] == 100
    assert stats['cache_read_tokens'] == 900
    assert stats['output_tokens'] == 7


def test_sse_text_capture_skips_non_text_deltas():
    parts: list[str] = []
    _extract_sse_text(
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"thinking_delta","thinking":"hmm"}}\n\n' + SSE_BODY, parts)
    assert ''.join(parts) == 'hello'


def test_response_text_extraction_ignores_tool_use_blocks():
    result = {'content': [
        {'type': 'text', 'text': 'part one '},
        {'type': 'tool_use', 'name': 'Read', 'input': {}},
        {'type': 'text', 'text': 'part two'},
    ]}
    assert _extract_response_text(result) == 'part one part two'


def test_chunk_without_message_start_is_returned_unchanged():
    chunk = 'event: ping\ndata: {"type":"ping"}\n\n'
    assert _rewrite_message_start_model(chunk, 'sonnet') is chunk


def test_malformed_data_line_survives_a_model_rewrite():
    chunk = 'event: message_start\ndata: {not json\n\n'
    assert _rewrite_message_start_model(chunk, 'sonnet') == chunk


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

def test_session_override_falls_back_to_global_once_cleared():
    state = SessionState(auto_model_routing=True)
    assert state.model_routing_enabled('s1') is True
    state.set_session_model_routing('s1', False)
    assert state.model_routing_enabled('s1') is False
    assert state.model_routing_enabled('s2') is True
    state.set_session_model_routing('s1', None)
    assert state.model_routing_enabled('s1') is True


def test_context_observations_replace_rather_than_accumulate():
    state = SessionState()
    state.record_context('k', 120_000, 1.2)
    state.record_context('k', 40_000, 1.1)
    assert state.context('k') == (40_000, 1.1)
    assert state.context('unseen') == (0, 1.0)


def test_state_maps_evict_oldest_first():
    state = SessionState()
    for i in range(MAX_ENTRIES + 5):
        state.set_routed_tier(f'k{i}', 'haiku')
    assert state.routed_tier('k0') is None
    assert state.routed_tier(f'k{MAX_ENTRIES + 4}') == 'haiku'
