import json
import urllib.error
import urllib.request

import pytest

from anthrouter.db import RequestDB
from anthrouter.handlers import (
    HAPPY_BIRTHDAY_REPLY,
    HAPPY_NEW_YEAR_PREFIX,
    ProxyRequestHandler,
    _extract_response_text,
    _extract_sse_stats,
    _extract_sse_text,
    _has_happy_new_year_system_prompt,
    _requested_tier,
    _rewrite_message_start_model,
    context_key,
    parse_local_command,
    session_key,
)
from anthrouter.mapper import CLASSIFIER_SENTINEL_KEY
from anthrouter.model_config import resolve_model
from anthrouter.model_router import _TRAILING_SCAN_LIMIT, ModelRoutingDecision
from anthrouter.request_text import (
    ACTIVITY_LABEL_FALLBACKS,
    ACTIVITY_LABEL_PREFIX,
    is_activity_label_prompt,
)
from anthrouter.session_state import MAX_ENTRIES, SessionState
from tests.conftest import DEEP_PROMPT, SESSION, SSE_BODY, _FakeUpstream, db_rows, post

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


@pytest.mark.parametrize('status, row_status', [(429, 'rate_limited'), (500, 'error')])
def test_failed_request_row_stores_the_rate_limit_window(proxy, status, row_status):
    _FakeUpstream.status = status
    _FakeUpstream.extra_headers = {
        'anthropic-ratelimit-requests-remaining': '42',
        'anthropic-ratelimit-tokens-remaining': '13000',
        'anthropic-ratelimit-tokens-reset': '2026-08-31T12:00:00Z',
    }
    server = proxy()
    with pytest.raises(urllib.error.HTTPError):
        post(server, '/v1/messages', {
            'model': 'sonnet', 'metadata': {'user_id': SESSION},
            'messages': [{'role': 'user', 'content': 'hello there'}]})
    row = db_rows(server)[0]
    assert row['status'] == row_status
    assert row['ratelimit_requests_remaining'] == 42
    assert row['ratelimit_tokens_remaining'] == 13000
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
        state.set_routed_tier((f'k{i}', 'sonnet'), 'haiku')
    assert state.routed_tier(('k0', 'sonnet')) is None
    assert state.routed_tier((f'k{MAX_ENTRIES + 4}', 'sonnet')) == 'haiku'


def test_routed_tier_slots_are_per_requested_tier():
    state = SessionState()
    state.set_routed_tier(('k', 'haiku'), 'haiku')
    assert state.routed_tier(('k', 'haiku')) == 'haiku'
    assert state.routed_tier(('k', 'sonnet')) is None


# ---------------------------------------------------------------------------
# Tier-cache key scoped by requested tier (ADR-0012)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('model, expected', [
    ('haiku', 'haiku'),
    ('claude-sonnet-5-5', 'sonnet'),
    ('Claude-Opus-5-5', 'opus'),
    ('claude-fable-5-1', 'fable'),
    ('gpt-4-turbo', '_gpt-4-turbo'),
])
def test_requested_tier_maps_tier_models_and_isolates_others(model, expected):
    assert _requested_tier(model) == expected


def _continuation(model):
    """Same conversation as ``hi``, but no user turn within the trailing scan.

    A short trailing run of assistant messages does not qualify:
    build_routing_summary walks back over it to the last user turn (ADR-0011)
    and the turn is re-classified. Beyond ``_TRAILING_SCAN_LIMIT`` the router
    finds no user text, while the first-user-message anchor is unchanged.
    """
    return {
        'model': model, 'metadata': {'user_id': SESSION},
        'messages': [{'role': 'user', 'content': 'hi'}]
        + [{'role': 'assistant', 'content': 'hello'}] * _TRAILING_SCAN_LIMIT,
    }


def test_cached_tier_is_not_replayed_into_a_different_requested_tier(proxy):
    server = proxy(auto_model_routing=True, auto_model_routing_mode='rules')
    post(server, '/v1/messages', {
        'model': 'sonnet', 'metadata': {'user_id': SESSION},
        'messages': [{'role': 'user', 'content': 'hi'}]})
    assert _FakeUpstream.received[-1]['payload']['model'] == resolve_model('haiku')

    post(server, '/v1/messages', _continuation('sonnet'))
    assert _FakeUpstream.received[-1]['payload']['model'] == resolve_model('haiku')
    assert db_rows(server)[0]['reason_code'] == 'session_cached_tier'

    post(server, '/v1/messages', _continuation('opus'))
    assert _FakeUpstream.received[-1]['payload']['model'] == resolve_model('opus')
    assert db_rows(server)[0]['reason_code'] == 'missing_final_user_text'


def test_locked_baseline_scopes_the_tier_slot_by_the_lock(proxy):
    server = proxy(auto_model_routing=True, auto_model_routing_mode='rules',
                   lock_requested_model='sonnet')
    post(server, '/v1/messages', {
        'model': 'opus', 'metadata': {'user_id': SESSION},
        'messages': [{'role': 'user', 'content': 'hi'}]})
    assert _FakeUpstream.received[-1]['payload']['model'] == resolve_model('haiku')

    # The lock is the routing baseline, so a client-side model change does not
    # move the request to a different cache slot.
    post(server, '/v1/messages', _continuation('opus'))
    assert _FakeUpstream.received[-1]['payload']['model'] == resolve_model('haiku')
    assert db_rows(server)[0]['reason_code'] == 'session_cached_tier'


# ---------------------------------------------------------------------------
# Happy New Year interception
# ---------------------------------------------------------------------------

class TestHasHappyNewYearSystemPrompt:
    def test_string_exact_prefix(self):
        assert _has_happy_new_year_system_prompt({'system': HAPPY_NEW_YEAR_PREFIX}) is True

    def test_string_with_suffix(self):
        assert _has_happy_new_year_system_prompt({'system': f'{HAPPY_NEW_YEAR_PREFIX}, friend!'}) is True

    def test_string_wrong_case(self):
        assert _has_happy_new_year_system_prompt({'system': 'happy new year'}) is False

    def test_string_mismatch(self):
        assert _has_happy_new_year_system_prompt({'system': 'Hello world'}) is False

    def test_string_empty(self):
        assert _has_happy_new_year_system_prompt({'system': ''}) is False

    def test_list_first_block_matches(self):
        assert _has_happy_new_year_system_prompt(
            {'system': [{'type': 'text', 'text': f'{HAPPY_NEW_YEAR_PREFIX} everyone'}]}) is True

    def test_list_second_block_matches(self):
        assert _has_happy_new_year_system_prompt(
            {'system': [
                {'type': 'text', 'text': 'Preamble'},
                {'type': 'text', 'text': f'{HAPPY_NEW_YEAR_PREFIX}!'},
            ]}) is True

    def test_list_no_block_matches(self):
        assert _has_happy_new_year_system_prompt(
            {'system': [{'type': 'text', 'text': 'Just a normal prompt'}]}) is False

    def test_list_non_dict_items_skipped(self):
        assert _has_happy_new_year_system_prompt(
            {'system': [HAPPY_NEW_YEAR_PREFIX, {'type': 'text', 'text': 'other'}]}) is False

    def test_list_block_missing_text_key(self):
        assert _has_happy_new_year_system_prompt({'system': [{'type': 'text'}]}) is False

    def test_list_any_type_matches(self):
        assert _has_happy_new_year_system_prompt(
            {'system': [{'type': 'cache_control', 'text': f'{HAPPY_NEW_YEAR_PREFIX}'}]}) is True

    def test_no_system_key(self):
        assert _has_happy_new_year_system_prompt({}) is False

    def test_system_none(self):
        assert _has_happy_new_year_system_prompt({'system': None}) is False

    def test_system_integer(self):
        assert _has_happy_new_year_system_prompt({'system': 42}) is False

    def test_system_empty_list(self):
        assert _has_happy_new_year_system_prompt({'system': []}) is False


class TestHappyNewYearShortCircuit:
    def test_non_streaming_does_not_reach_upstream(self, proxy):
        server = proxy(security_monitor_local_reply=True)
        post(server, '/v1/messages', {
            'model': 'claude-opus-4-5',
            'system': HAPPY_NEW_YEAR_PREFIX,
            'messages': [{'role': 'user', 'content': 'hi'}]})
        assert _FakeUpstream.received == []

    def test_non_streaming_reply_body(self, proxy):
        server = proxy(security_monitor_local_reply=True)
        result = post(server, '/v1/messages', {
            'model': 'claude-opus-4-5',
            'system': HAPPY_NEW_YEAR_PREFIX,
            'messages': [{'role': 'user', 'content': 'hi'}]})
        assert result['content'][0]['text'] == HAPPY_BIRTHDAY_REPLY
        assert result['model'] == 'claude-opus-4-5'

    def test_streaming_does_not_reach_upstream(self, proxy):
        server = proxy(security_monitor_local_reply=True)
        post(server, '/v1/messages', {
            'model': 'claude-opus-4-5',
            'system': HAPPY_NEW_YEAR_PREFIX,
            'stream': True,
            'messages': [{'role': 'user', 'content': 'hi'}]}, raw=True)
        assert _FakeUpstream.received == []

    def test_streaming_reply_body(self, proxy):
        server = proxy(security_monitor_local_reply=True)
        body = post(server, '/v1/messages', {
            'model': 'claude-opus-4-5',
            'system': HAPPY_NEW_YEAR_PREFIX,
            'stream': True,
            'messages': [{'role': 'user', 'content': 'hi'}]}, raw=True)
        assert f'"text": "{HAPPY_BIRTHDAY_REPLY}"' in body
        assert 'event: message_stop' in body

    def test_list_system_trigger(self, proxy):
        server = proxy(security_monitor_local_reply=True)
        result = post(server, '/v1/messages', {
            'model': 'claude-opus-4-5',
            'system': [{'type': 'text', 'text': f'{HAPPY_NEW_YEAR_PREFIX}, everyone!'}],
            'messages': [{'role': 'user', 'content': 'hi'}]})
        assert result['content'][0]['text'] == HAPPY_BIRTHDAY_REPLY

    def test_no_db_recording(self, proxy):
        server = proxy(security_monitor_local_reply=True)
        post(server, '/v1/messages', {
            'model': 'claude-opus-4-5',
            'system': HAPPY_NEW_YEAR_PREFIX,
            'messages': [{'role': 'user', 'content': 'hi'}]})
        assert db_rows(server) == []

    def test_no_credential_check(self, proxy):
        server = proxy(security_monitor_local_reply=True)
        result = post(server, '/v1/messages', {
            'model': 'claude-opus-4-5',
            'system': HAPPY_NEW_YEAR_PREFIX,
            'messages': [{'role': 'user', 'content': 'hi'}]})
        assert result['content'][0]['text'] == HAPPY_BIRTHDAY_REPLY


# ---------------------------------------------------------------------------
# Security-monitor passthrough (ADR-0017)
# ---------------------------------------------------------------------------

def _monitor_payload(model='claude-sonnet-5'):
    return {'model': model, 'metadata': {'user_id': SESSION},
            'system': [{'type': 'text', 'text': 'x-anthropic-billing-header: cc_version=1'},
                       {'type': 'text', 'text': f'{HAPPY_NEW_YEAR_PREFIX} Review it.'}],
            'messages': [{'role': 'user', 'content': 'hi'}]}


class TestSecurityMonitorPassthrough:
    def test_lock_and_classifier_are_skipped(self, proxy):
        server = proxy(auto_model_routing=True, lock_requested_model='sonnet')
        post(server, '/v1/messages', _monitor_payload())
        assert len(_FakeUpstream.received) == 1
        assert _FakeUpstream.received[0]['payload']['model'] == 'claude-sonnet-5'
        row = db_rows(server)[0]
        assert row['reason_code'] == 'security_monitor_passthrough'
        assert row['routed_model'] == 'claude-sonnet-5'

    def test_monitor_request_forwards_system_unsanitized(self, proxy):
        server = proxy()
        payload = _monitor_payload()
        post(server, '/v1/messages', payload)
        assert _FakeUpstream.received[0]['payload']['system'] == payload['system']

    def test_monitor_request_makes_no_volatility_observation(self, proxy, monkeypatch):
        import anthrouter.sanitizer as sanitizer
        seen = []
        monkeypatch.setattr(sanitizer.TRACKER, 'observe',
                            lambda sid, system: seen.append(sid) or [])
        server = proxy()
        post(server, '/v1/messages', _monitor_payload())
        assert seen == []

    def test_streaming_reply_passes_through_unrewritten(self, proxy):
        server = proxy(lock_requested_model='sonnet')
        body = post(server, '/v1/messages', {**_monitor_payload(), 'stream': True}, raw=True)
        assert _FakeUpstream.received[0]['payload']['model'] == 'claude-sonnet-5'
        assert body == SSE_BODY

    def test_monitor_turn_leaves_the_session_tier_cache_unset(self, proxy):
        server = proxy(auto_model_routing=True, auto_model_routing_mode='rules')
        post(server, '/v1/messages', _monitor_payload(model='sonnet'))
        post(server, '/v1/messages', _continuation('sonnet'))
        rows = db_rows(server)
        assert rows[1]['reason_code'] == 'security_monitor_passthrough'
        assert rows[0]['reason_code'] == 'missing_final_user_text'

    def test_ordinary_request_still_takes_the_lock(self, proxy):
        server = proxy(auto_model_routing=False, lock_requested_model='haiku')
        post(server, '/v1/messages', {
            'model': 'claude-opus-4-5', 'metadata': {'user_id': SESSION},
            'messages': [{'role': 'user', 'content': 'hi'}]})
        assert _FakeUpstream.received[-1]['payload']['model'] == resolve_model('haiku')

    def test_local_reply_flag_off_by_default(self, proxy):
        server = proxy()
        post(server, '/v1/messages', _monitor_payload())
        assert len(_FakeUpstream.received) == 1


# ---------------------------------------------------------------------------
# Activity-label local reply (ADR-0015)
# ---------------------------------------------------------------------------

_LABEL_PROMPT = f'{ACTIVITY_LABEL_PREFIX}, present tense.'


def _label_payload(content=_LABEL_PROMPT, **extra):
    return {'model': 'claude-sonnet-5-5', 'metadata': {'user_id': SESSION},
            'messages': [{'role': 'user', 'content': content}], **extra}


@pytest.mark.parametrize('text, expected', [
    (_LABEL_PROMPT, True),
    (f'  \n{_LABEL_PROMPT}', True),
    (f'<system-reminder>ctx</system-reminder>\n{_LABEL_PROMPT}', True),
    (f'Please. {_LABEL_PROMPT}', False),
    (_LABEL_PROMPT.lower(), False),
    ('', False),
    (None, False),
])
def test_activity_label_prompt_is_a_prefix_match_after_reminder_stripping(text, expected):
    assert is_activity_label_prompt(text) is expected


class TestActivityLabelLocalReply:
    def test_non_streaming_reply_is_a_fallback_verb_and_skips_upstream(self, proxy):
        server = proxy(auto_model_routing=True, auto_model_routing_mode='rules')
        result = post(server, '/v1/messages', _label_payload())
        assert result['content'][0]['text'] in ACTIVITY_LABEL_FALLBACKS
        assert result['model'] == 'claude-sonnet-5-5'
        assert _FakeUpstream.received == []

    def test_streaming_reply_is_a_fallback_verb(self, proxy):
        server = proxy()
        body = post(server, '/v1/messages', _label_payload(stream=True), raw=True)
        assert any(f'"text": "{verb}"' in body for verb in ACTIVITY_LABEL_FALLBACKS)
        assert 'event: message_stop' in body
        assert _FakeUpstream.received == []

    def test_nothing_is_recorded(self, proxy):
        server = proxy()
        post(server, '/v1/messages', _label_payload())
        assert db_rows(server) == []

    def test_text_blocks_after_a_reminder_block_are_detected(self, proxy):
        server = proxy()
        post(server, '/v1/messages', _label_payload(content=[
            {'type': 'text', 'text': '<system-reminder>ctx</system-reminder>'},
            {'type': 'text', 'text': _LABEL_PROMPT},
        ]))
        assert _FakeUpstream.received == []

    def test_trailing_system_entry_does_not_hide_the_final_user_turn(self, proxy):
        server = proxy()
        payload = _label_payload()
        payload['messages'].append({'role': 'system', 'content': 'tool loop ended'})
        post(server, '/v1/messages', payload)
        assert _FakeUpstream.received == []

    def test_only_the_final_user_turn_is_checked(self, proxy):
        server = proxy()
        post(server, '/v1/messages', {
            'model': 'claude-sonnet-5-5',
            'messages': [
                {'role': 'user', 'content': _LABEL_PROMPT},
                {'role': 'assistant', 'content': 'Cooking'},
                {'role': 'user', 'content': 'now fix the bug'},
            ]})
        assert len(_FakeUpstream.received) == 1

    def test_ordinary_prompt_is_dispatched(self, proxy):
        server = proxy()
        post(server, '/v1/messages', _label_payload(content='Describe the bug.'))
        assert len(_FakeUpstream.received) == 1

    def test_flag_off_dispatches_upstream(self, proxy):
        server = proxy(activity_label_local_reply=False)
        post(server, '/v1/messages', _label_payload())
        assert len(_FakeUpstream.received) == 1

    def test_classifier_sentinel_payload_is_dispatched(self, proxy):
        server = proxy()
        post(server, '/v1/messages', _label_payload(**{CLASSIFIER_SENTINEL_KEY: True}))
        assert len(_FakeUpstream.received) == 1


# ---------------------------------------------------------------------------
# Net savings and classifier overhead (ADR-0002)
# ---------------------------------------------------------------------------

SAVINGS_STATS = {'input_tokens': 1000, 'output_tokens': 200,
                  'cache_creation_tokens': 0, 'cache_read_tokens': 0}


def _decision(**kwargs):
    base = dict(
        requested_model='sonnet',
        routed_model='haiku',
        classification='trivial',
        applied=True,
        reason_code='classifier_trivial',
    )
    base.update(kwargs)
    return ModelRoutingDecision(**base)


def test_savings_computed_when_classifier_ran_and_routing_applied():
    decision = _decision(classifier_model='haiku', classifier_input_tokens=500,
                          classifier_output_tokens=50)
    net, overhead = ProxyRequestHandler._compute_savings(decision, SAVINGS_STATS)
    assert overhead == pytest.approx(0.00075)
    assert net == pytest.approx(0.00325)


def test_classifier_overhead_recorded_when_routing_confirms_the_requested_model(tmp_path):
    decision = _decision(routed_model='sonnet', applied=False,
                          classifier_model='haiku', classifier_input_tokens=500,
                          classifier_output_tokens=50)
    net, overhead = ProxyRequestHandler._compute_savings(decision, SAVINGS_STATS)
    assert overhead == pytest.approx(0.00075)

    store = RequestDB(str(tmp_path / 'a.db'))
    row = store.get_request(store.record_request(
        session_id='s', routing_decision=decision, stats_dict=SAVINGS_STATS,
        duration_ms=1, status='success',
        net_savings_usd=net, classifier_overhead_usd=overhead))
    assert row['net_savings_usd'] is None
    assert row['classifier_overhead_usd'] == pytest.approx(0.00075)
    store.close()


def test_no_classifier_call_records_zero_overhead():
    decision = _decision(reason_code='rules_standard')
    net, overhead = ProxyRequestHandler._compute_savings(decision, SAVINGS_STATS)
    assert overhead == 0.0


def test_unrecognized_routed_model_nulls_net_savings():
    decision = _decision(routed_model='gpt-4-turbo', classifier_model='haiku',
                          classifier_input_tokens=500, classifier_output_tokens=50)
    net, overhead = ProxyRequestHandler._compute_savings(decision, SAVINGS_STATS)
    assert net is None
    assert overhead == pytest.approx(0.00075)


def test_empty_stats_nulls_net_savings_but_keeps_overhead():
    decision = _decision(classifier_model='haiku', classifier_input_tokens=500,
                          classifier_output_tokens=50)
    net, overhead = ProxyRequestHandler._compute_savings(decision, {})
    assert net is None
    assert overhead == pytest.approx(0.00075)


def test_unrecognized_classifier_model_nulls_both_fields():
    decision = _decision(classifier_model='gpt-4-turbo', classifier_input_tokens=500,
                          classifier_output_tokens=50)
    net, overhead = ProxyRequestHandler._compute_savings(decision, SAVINGS_STATS)
    assert overhead is None
    assert net is None


def test_routing_to_a_pricier_tier_yields_a_negative_net_savings():
    decision = _decision(requested_model='haiku', routed_model='opus',
                          classifier_model='haiku', classifier_input_tokens=500,
                          classifier_output_tokens=50)
    net, overhead = ProxyRequestHandler._compute_savings(decision, SAVINGS_STATS)
    assert net is not None
    assert net < 0


# ---------------------------------------------------------------------------
# Classifier failure evidence and overhead booking
# ---------------------------------------------------------------------------

_THINKING_ONLY_BODY = {
    'id': 'msg_up', 'type': 'message', 'role': 'assistant',
    'content': [{'type': 'thinking', 'thinking': 'deliberating'}],
    'stop_reason': 'max_tokens',
    'usage': {'input_tokens': 500, 'output_tokens': 50},
}


def _post_routed_prompt(server):
    post(server, '/v1/messages', {
        'model': 'sonnet', 'metadata': {'user_id': SESSION},
        'messages': [{'role': 'user', 'content': 'fix the failing auth test'}]})
    return db_rows(server)[0]


def _classifier_calls_raise(monkeypatch, exc):
    from anthrouter.transport import AnthropicTransport

    original = AnthropicTransport.send_message

    def send_message(self, payload, credentials, config=None, ratelimit_out=None,
                     attempt_out=None):
        if payload.get('_anthproxy_internal_classifier'):
            raise exc
        return original(self, payload, credentials, config, ratelimit_out, attempt_out)

    monkeypatch.setattr(AnthropicTransport, 'send_message', send_message)
    monkeypatch.setattr('anthrouter.model_router.time.sleep', lambda _s: None)


def test_invalid_classifier_row_books_overhead_and_stores_evidence(proxy):
    _FakeUpstream.json_body = _THINKING_ONLY_BODY
    server = proxy(auto_model_routing=True, auto_model_routing_mode='classifier')

    row = _post_routed_prompt(server)

    assert row['reason_code'] == 'classifier_invalid'
    assert row['classifier_overhead_usd'] > 0
    assert row['classifier_raw_response'] == '<non-text>'
    assert row['classifier_stop_reason'] == 'max_tokens'


def test_rate_limited_classifier_row_with_priced_model_books_zero_overhead(proxy, monkeypatch):
    from anthrouter.mapper.common import AnthropicRequestError

    _classifier_calls_raise(monkeypatch, AnthropicRequestError(
        'slow down', error_type='rate_limit_error', status_code=429))
    server = proxy(auto_model_routing=True, auto_model_routing_mode='classifier')

    row = _post_routed_prompt(server)

    assert row['reason_code'] == 'classifier_rate_limited'
    assert not row['applied']
    assert row['classifier_model'] == 'haiku'
    assert row['classifier_overhead_usd'] == 0.0
    assert row['classifier_raw_response'] == 'HTTP 429 AnthropicRequestError: slow down'


def test_failed_classifier_row_with_unpriced_model_leaves_overhead_null(proxy, monkeypatch):
    _classifier_calls_raise(monkeypatch, RuntimeError('boom'))
    server = proxy(auto_model_routing=True, auto_model_routing_mode='classifier',
                   auto_model_routing_classifier_model='unpriced-model')

    row = _post_routed_prompt(server)

    assert row['reason_code'] == 'classifier_failed'
    assert row['classifier_model'] == 'unpriced-model'
    assert row['classifier_overhead_usd'] is None


# ---------------------------------------------------------------------------
# Fail-closed paths record the model they dispatch (ADR-0016)
# ---------------------------------------------------------------------------

def _fail_closed_case(proxy, monkeypatch, case, **overrides):
    """Build a proxy that fails closed with ``case``; return it and the client model."""
    if case == 'classifier_failed':
        _classifier_calls_raise(monkeypatch, RuntimeError('boom'))
        return proxy(auto_model_routing=True, auto_model_routing_mode='classifier',
                     **overrides), 'haiku'
    if case == 'classifier_rate_limited':
        from anthrouter.mapper.common import AnthropicRequestError

        _classifier_calls_raise(monkeypatch, AnthropicRequestError(
            'slow down', error_type='rate_limit_error', status_code=429))
        return proxy(auto_model_routing=True, auto_model_routing_mode='classifier',
                     **overrides), 'haiku'
    if case == 'disabled':
        return proxy(auto_model_routing=False, **overrides), 'haiku'
    return proxy(auto_model_routing=True, auto_model_routing_mode='rules',
                 **overrides), ''


_FAIL_CLOSED_CASES = ['classifier_failed', 'classifier_rate_limited', 'disabled',
                      'model_not_eligible']


@pytest.mark.parametrize('case', _FAIL_CLOSED_CASES)
def test_locked_fail_closed_records_the_model_it_dispatches(proxy, monkeypatch, case):
    server, client_model = _fail_closed_case(
        proxy, monkeypatch, case, lock_requested_model='sonnet')

    body = post(server, '/v1/messages', {
        'model': client_model, 'metadata': {'user_id': SESSION},
        'messages': [{'role': 'user', 'content': 'fix the failing auth test'}]})
    row = db_rows(server)[0]

    assert row['reason_code'] == case
    assert _FakeUpstream.received[-1]['payload']['model'] == resolve_model('sonnet')
    assert row['routed_model'] == 'sonnet'
    assert row['requested_model'] == client_model
    assert row['applied']
    assert body['model'] == client_model


# 'model_not_eligible' is absent: without a lock its ineligible model (empty or
# non-string) never reaches the upstream — the mapper rejects it with a 400.
@pytest.mark.parametrize('case', ['classifier_failed', 'classifier_rate_limited', 'disabled'])
def test_unlocked_fail_closed_keeps_the_client_model(proxy, monkeypatch, case):
    server, client_model = _fail_closed_case(proxy, monkeypatch, case)

    body = post(server, '/v1/messages', {
        'model': client_model, 'metadata': {'user_id': SESSION},
        'messages': [{'role': 'user', 'content': 'fix the failing auth test'}]})
    row = db_rows(server)[0]

    assert row['reason_code'] == case
    assert _FakeUpstream.received[-1]['payload']['model'] == resolve_model(client_model)
    assert row['routed_model'] == client_model
    assert row['requested_model'] == client_model
    assert not row['applied']
    # Nothing was rewritten, so the response echoes the upstream's resolved model,
    # which is identical to the client's model.
    assert body['model'] == resolve_model(client_model)


# ---------------------------------------------------------------------------
# Ticket #34 — Truthful Retry-After on outbound 429
# ---------------------------------------------------------------------------

def _post_expect_error(server, status_code):
    """POST to /v1/messages and return the HTTPError (non-2xx)."""
    import urllib.error
    request = urllib.request.Request(
        server.base_url + '/v1/messages',
        data=json.dumps({'model': 'sonnet',
                         'messages': [{'role': 'user', 'content': 'hi'}]}).encode(),
        headers={'Content-Type': 'application/json', 'x-api-key': 'k'},
        method='POST')
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(request)
    assert exc_info.value.code == status_code
    return exc_info.value


def test_retry_after_omitted_when_upstream_sent_none(proxy, monkeypatch):
    """429 with no upstream Retry-After header omits Retry-After in the response."""
    monkeypatch.setattr('anthrouter.transport.time.sleep', lambda _s: None)
    _FakeUpstream.status = 429
    server = proxy()
    err = _post_expect_error(server, 429)
    assert err.headers.get('Retry-After') is None


def test_retry_after_echoed_as_integer_seconds(proxy, monkeypatch):
    """upstream Retry-After: 5 is echoed as '5'."""
    monkeypatch.setattr('anthrouter.transport.time.sleep', lambda _s: None)
    _FakeUpstream.status = 429
    _FakeUpstream.extra_headers = {'Retry-After': '5'}
    server = proxy()
    err = _post_expect_error(server, 429)
    assert err.headers.get('Retry-After') == '5'


def test_retry_after_zero_echoed_not_omitted(proxy, monkeypatch):
    """upstream Retry-After: 0 is echoed as '0', not omitted."""
    monkeypatch.setattr('anthrouter.transport.time.sleep', lambda _s: None)
    _FakeUpstream.status = 429
    _FakeUpstream.extra_headers = {'Retry-After': '0'}
    server = proxy()
    err = _post_expect_error(server, 429)
    assert err.headers.get('Retry-After') == '0'


def test_retry_after_ms_rounded_up(proxy, monkeypatch):
    """upstream retry-after-ms: 1500 (stored as 1.5s) is echoed as '2' (ceiling)."""
    monkeypatch.setattr('anthrouter.transport.time.sleep', lambda _s: None)
    _FakeUpstream.status = 429
    _FakeUpstream.extra_headers = {'retry-after-ms': '1500'}
    server = proxy()
    err = _post_expect_error(server, 429)
    assert err.headers.get('Retry-After') == '2'


def test_5xx_produces_502_without_retry_after(proxy, monkeypatch):
    """A 5xx from the upstream produces a 502 with no Retry-After header."""
    monkeypatch.setattr('anthrouter.transport.time.sleep', lambda _s: None)
    _FakeUpstream.status = 500
    server = proxy()
    err = _post_expect_error(server, 502)
    assert err.headers.get('Retry-After') is None


@pytest.mark.parametrize('upstream_status,client_status',
                         [(503, 502), (400, 400), (401, 401), (403, 403)])
def test_retry_after_not_echoed_on_non_429(proxy, monkeypatch, upstream_status, client_status):
    """Upstream Retry-After on a non-429 error must not reach the client."""
    monkeypatch.setattr('anthrouter.transport.time.sleep', lambda _s: None)
    _FakeUpstream.status = upstream_status
    _FakeUpstream.extra_headers = {'Retry-After': '30'}
    server = proxy()
    err = _post_expect_error(server, client_status)
    assert err.headers.get('Retry-After') is None


def test_retry_after_non_finite_omitted(proxy, monkeypatch):
    """retry-after-ms: 1e999 parses to inf; header is omitted, response still sent."""
    monkeypatch.setattr('anthrouter.transport.time.sleep', lambda _s: None)
    _FakeUpstream.status = 429
    _FakeUpstream.extra_headers = {'retry-after-ms': '1e999'}
    server = proxy()
    err = _post_expect_error(server, 429)
    assert err.headers.get('Retry-After') is None


# ---------------------------------------------------------------------------
# Ticket #36 — Record the attempt number (transport retries only)
# ---------------------------------------------------------------------------

def _fail_first_n_posts(monkeypatch, n):
    """Make the fake upstream answer HTTP 500 to its first ``n`` POSTs, then 200."""
    original = _FakeUpstream.do_POST
    calls = {'n': 0}

    def do_POST(self):
        calls['n'] += 1
        type(self).status = 500 if calls['n'] <= n else 200
        original(self)

    monkeypatch.setattr(_FakeUpstream, 'do_POST', do_POST)


def _post_one_prompt(server):
    request = urllib.request.Request(
        server.base_url + '/v1/messages',
        data=json.dumps({'model': 'sonnet', 'metadata': {'user_id': SESSION},
                         'messages': [{'role': 'user', 'content': 'hi'}]}).encode(),
        headers={'Content-Type': 'application/json', 'x-api-key': 'k'},
        method='POST')
    try:
        urllib.request.urlopen(request).read()
    except urllib.error.HTTPError:
        pass
    return db_rows(server)[0]


def test_transport_retried_request_records_attempt_above_one(proxy, monkeypatch):
    monkeypatch.setattr('anthrouter.transport.time.sleep', lambda _s: None)
    _fail_first_n_posts(monkeypatch, 2)
    server = proxy()

    row = _post_one_prompt(server)

    assert row['status'] == 'success'
    assert row['attempt'] == 3


def test_exhausted_transport_request_records_the_exhausted_count(proxy, monkeypatch):
    from anthrouter.http_util import MAX_RETRIES

    monkeypatch.setattr('anthrouter.transport.time.sleep', lambda _s: None)
    _FakeUpstream.status = 500
    server = proxy()

    row = _post_one_prompt(server)

    assert row['status'] == 'error'
    assert row['attempt'] == MAX_RETRIES + 1
    assert len(_FakeUpstream.received) == MAX_RETRIES + 1


def test_request_that_never_reaches_the_retry_loop_records_one(proxy, monkeypatch):
    from anthrouter.mapper.common import AnthropicRequestError
    from anthrouter.transport import AnthropicTransport

    def refuse(self, payload, credentials, config=None, ratelimit_out=None,
               attempt_out=None):
        raise AnthropicRequestError('refused before dispatch', status_code=400)

    monkeypatch.setattr(AnthropicTransport, 'send_message', refuse)
    server = proxy()

    row = _post_one_prompt(server)

    assert _FakeUpstream.received == []
    assert row['status'] == 'error'
    assert row['attempt'] == 1


def test_classifier_retry_with_first_try_upstream_success_records_one(proxy, monkeypatch):
    from anthrouter.mapper.common import AnthropicRequestError
    from anthrouter.transport import AnthropicTransport

    original = AnthropicTransport.send_message
    classifier_calls = []

    def send_message(self, payload, credentials, config=None, ratelimit_out=None,
                     attempt_out=None):
        if payload.get('_anthproxy_internal_classifier'):
            classifier_calls.append(1)
            raise AnthropicRequestError('overloaded', error_type='api_error',
                                        status_code=503)
        return original(self, payload, credentials, config, ratelimit_out, attempt_out)

    monkeypatch.setattr(AnthropicTransport, 'send_message', send_message)
    monkeypatch.setattr('anthrouter.model_router.time.sleep', lambda _s: None)
    server = proxy(auto_model_routing=True, auto_model_routing_mode='classifier')

    row = _post_routed_prompt(server)

    assert len(classifier_calls) > 1
    assert row['status'] == 'success'
    assert row['attempt'] == 1


# ---------------------------------------------------------------------------
# Absolute-form request targets (a client that sends http:// traffic through HTTPS_PROXY)
# ---------------------------------------------------------------------------

def _absolute_form(server, method, target, payload=None, headers=None):
    import http.client
    conn = http.client.HTTPConnection('127.0.0.1', server.server_address[1], timeout=10)
    body = json.dumps(payload).encode() if payload is not None else None
    conn.request(method, target, body=body,
                 headers={'Content-Type': 'application/json', 'x-api-key': 'sk-test',
                          **(headers or {})})
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp, data


@pytest.mark.parametrize('host', ['127.0.0.1', 'localhost'])
def test_absolute_form_target_naming_this_proxy_is_served_like_a_path(proxy, upstream, host):
    server = proxy()
    port = server.server_address[1]
    resp, data = _absolute_form(
        server, 'POST', f'http://{host}:{port}/v1/messages?beta=true',
        {'model': 'sonnet', 'messages': [{'role': 'user', 'content': 'hi'}]})
    assert resp.status == 200, data
    assert len(_FakeUpstream.received) == 1
    resp, data = _absolute_form(server, 'GET', f'http://{host}:{port}/health')
    assert resp.status == 200 and json.loads(data)['status'] == 'ok'


@pytest.mark.parametrize('method, target', [
    ('POST', 'http://127.0.0.1:8787/w/claude/v1/messages?beta=true'),
    ('GET', 'http://example.com/health'),
    ('DELETE', 'http://127.0.0.1:1/v1/messages'),
])
def test_absolute_form_target_for_another_host_is_a_tagged_502_never_a_404(
        proxy, upstream, method, target):
    server = proxy()
    resp, data = _absolute_form(server, method, target,
                                {} if method == 'POST' else None)
    assert resp.status == 502
    assert resp.getheader('X-Anthrouter-Error') == 'forward-proxy-unsupported'
    error = json.loads(data)['error']
    assert error['type'] == 'api_error' and 'NO_PROXY' in error['message']
    assert _FakeUpstream.received == []


def test_refused_absolute_form_post_with_a_large_body_still_gets_its_502(proxy, upstream):
    server = proxy()
    resp, data = _absolute_form(
        server, 'POST', 'http://127.0.0.1:8787/w/claude/v1/messages',
        {'model': 'sonnet', 'pad': 'x' * (4 * 1024 * 1024)})
    assert resp.status == 502
    assert resp.getheader('X-Anthrouter-Error') == 'forward-proxy-unsupported'
