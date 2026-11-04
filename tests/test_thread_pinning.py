"""Thread pinning (ADR-0027): a continuation is served by the model that served its previous message."""

import json
import logging
import urllib.error

import pytest

from anthrouter.model_config import resolve_model
from anthrouter.session_state import MAX_ENTRIES, SessionState
from tests.conftest import (
    SESSION,
    _FakeUpstream,
    db_rows,
    post,
    session_state,
)

HAIKU = resolve_model('haiku')
OPUS = resolve_model('opus')
SONNET = resolve_model('sonnet')

ROUTED = {'auto_model_routing': True, 'auto_model_routing_mode': 'rules'}

TOOL_RESULT_DELTA = [{'role': 'user', 'content': [
    {'type': 'tool_result', 'tool_use_id': 'toolu_1', 'content': 'ok'}]}]


def _create(model='sonnet', stream=False, text='hi'):
    return {
        'model': model, 'stream': stream, 'metadata': {'user_id': SESSION},
        'thread': {'type': 'create'},
        'messages': [{'role': 'user', 'content': text}],
    }


def _continue(previous, model='sonnet', stream=False, messages=None):
    return {
        'model': model, 'stream': stream, 'metadata': {'user_id': SESSION},
        'thread': {'type': 'continue', 'previous_message_id': previous},
        'messages': messages or TOOL_RESULT_DELTA,
    }


def _sent():
    return _FakeUpstream.received[-1]['payload']['model']


def _reason(server):
    return db_rows(server)[0]['reason_code']


@pytest.mark.parametrize('thread, expected', [
    ({'type': 'continue', 'previous_message_id': 'msg_1'}, True),
    ({'type': 'continue', 'previous_message_id': ''}, True),
    ({'previous_message_id': 'msg_1'}, True),
    ({'type': 'future', 'previous_message_id': 'msg_1'}, True),
    ({'type': 'create', 'previous_message_id': 'msg_1'}, False),
    ({'type': 'create'}, False),
    ({'type': 'continue'}, False),
    ({'type': 'continue', 'previous_message_id': 7}, False),
    ({'type': 'continue', 'previous_message_id': None}, False),
    ('continue', False),
    (None, False),
])
def test_continuation_predicate(proxy, thread, expected):
    server = proxy(**ROUTED)
    post(server, '/v1/messages', {
        'model': 'sonnet', 'metadata': {'user_id': SESSION}, 'thread': thread,
        'messages': [{'role': 'user', 'content': 'hi'}]})
    assert (_reason(server) == 'thread_pin_unknown') is expected


def test_payload_without_thread_is_not_a_continuation(proxy):
    server = proxy(**ROUTED)
    post(server, '/v1/messages', {
        'model': 'sonnet', 'metadata': {'user_id': SESSION},
        'messages': [{'role': 'user', 'content': 'hi'}]})
    assert not _reason(server).startswith('thread_pin')


def test_thread_map_evicts_oldest_first_and_is_keyed_by_id():
    # Direct on SessionState: filling the map through the proxy would take
    # 10000 round trips.
    assert MAX_ENTRIES == 10000
    state = SessionState()
    for i in range(MAX_ENTRIES + 5):
        state.record_thread(f'msg_{i}', 'haiku')
    assert state.thread_pin('msg_0') is None
    assert state.thread_pin(f'msg_{MAX_ENTRIES + 4}') == ('haiku', False, None)
    assert state.thread_pin('absent') is None


def test_continuation_goes_to_the_creating_turns_model_without_routing(proxy):
    server = proxy(**ROUTED)
    _FakeUpstream.message_ids = ['msg_A']
    post(server, '/v1/messages', _create())
    assert _sent() == HAIKU

    post(server, '/v1/messages', _continue('msg_A', model='opus'))
    assert _sent() == HAIKU
    assert len(_FakeUpstream.received) == 2
    row = db_rows(server)[0]
    assert row['reason_code'] == 'thread_pinned'
    assert row['routed_model'] == 'haiku'


def test_pin_outranks_a_lock_set_for_the_tool_result_only_turn(proxy):
    server = proxy(**ROUTED, lock_requested_model='sonnet')
    _FakeUpstream.message_ids = ['msg_A']
    post(server, '/v1/messages', _create(model='opus'))
    assert _sent() == HAIKU

    post(server, '/v1/messages', _continue('msg_A', model='opus'))
    assert _sent() == HAIKU
    assert _reason(server) == 'thread_pinned'


@pytest.mark.parametrize('overrides, expected', [
    ({}, OPUS),
    ({'lock_requested_model': 'sonnet'}, SONNET),
])
def test_unknown_id_falls_to_the_baseline(proxy, overrides, expected):
    server = proxy(**ROUTED, **overrides)
    post(server, '/v1/messages', _continue('msg_never_seen', model='opus'))
    assert _sent() == expected
    assert _reason(server) == 'thread_pin_unknown'


def test_pinned_turn_records_its_own_response_id(proxy):
    server = proxy(**ROUTED)
    _FakeUpstream.message_ids = ['msg_A', 'msg_B']
    post(server, '/v1/messages', _create())
    post(server, '/v1/messages', _continue('msg_A'))
    post(server, '/v1/messages', _continue('msg_B', model='opus'))
    assert _sent() == HAIKU
    assert _reason(server) == 'thread_pinned'


def test_unknown_turn_records_its_own_response_id(proxy):
    server = proxy(**ROUTED)
    _FakeUpstream.message_ids = ['msg_B']
    post(server, '/v1/messages', _continue('msg_gone', model='opus'))
    post(server, '/v1/messages', _continue('msg_B', model='sonnet'))
    assert _sent() == OPUS
    assert _reason(server) == 'thread_pinned'


def test_stream_with_an_error_event_records_nothing(proxy):
    server = proxy(**ROUTED)
    _FakeUpstream.message_ids = ['msg_A']
    _FakeUpstream.stream_error = True
    body = post(server, '/v1/messages', _create(stream=True), raw=True)
    assert 'event: error' in body

    _FakeUpstream.stream_error = False
    post(server, '/v1/messages', _continue('msg_A', model='opus'))
    assert _sent() == OPUS
    assert _reason(server) == 'thread_pin_unknown'


def test_stream_text_mentioning_an_error_event_still_records(proxy):
    server = proxy(**ROUTED)
    _FakeUpstream.message_ids = ['msg_T']
    _FakeUpstream.stream_text = 'SSE frames look like\nevent: error\ndata: {...}'
    body = post(server, '/v1/messages', _create(stream=True), raw=True)
    assert 'event: error' in body

    post(server, '/v1/messages', _continue('msg_T', model='opus'))
    assert _sent() == HAIKU
    assert _reason(server) == 'thread_pinned'


def test_stream_records_the_message_start_id_after_message_stop(proxy):
    server = proxy(**ROUTED)
    _FakeUpstream.message_ids = ['msg_S']
    post(server, '/v1/messages', _create(stream=True), raw=True)
    post(server, '/v1/messages', _continue('msg_S', model='opus'))
    assert _sent() == HAIKU
    assert _reason(server) == 'thread_pinned'


def test_non_streaming_200_records_the_top_level_id(proxy):
    server = proxy(**ROUTED)
    _FakeUpstream.message_ids = ['msg_J']
    post(server, '/v1/messages', _create())
    post(server, '/v1/messages', _continue('msg_J', model='opus'))
    assert _sent() == HAIKU
    assert _reason(server) == 'thread_pinned'


def test_upstream_error_records_nothing(proxy):
    server = proxy(**ROUTED)
    _FakeUpstream.message_ids = ['msg_E']
    _FakeUpstream.status = 400
    with pytest.raises(urllib.error.HTTPError):
        post(server, '/v1/messages', _create())
    _FakeUpstream.status = 200
    post(server, '/v1/messages', _continue('msg_E', model='opus'))
    assert _reason(server) == 'thread_pin_unknown'


def test_local_command_reply_records_nothing(proxy):
    server = proxy(**ROUTED)
    local = post(server, '/v1/messages', {
        'model': 'sonnet', 'thread': {'type': 'create'},
        'messages': [{'role': 'user', 'content': 'proxy-help'}]})
    assert _FakeUpstream.received == []

    post(server, '/v1/messages', _continue(local['id'], model='opus'))
    assert _sent() == OPUS
    assert _reason(server) == 'thread_pin_unknown'


def test_request_without_a_thread_records_nothing(proxy):
    server = proxy(**ROUTED)
    _FakeUpstream.message_ids = ['msg_N']
    plain = _create()
    del plain['thread']
    post(server, '/v1/messages', plain)

    post(server, '/v1/messages', _continue('msg_N', model='opus'))
    assert _sent() == OPUS
    assert _reason(server) == 'thread_pin_unknown'


def test_non_dict_thread_records_nothing(proxy):
    server = proxy(**ROUTED)
    _FakeUpstream.message_ids = ['msg_N']
    odd = _create()
    odd['thread'] = 'create'
    post(server, '/v1/messages', odd)

    post(server, '/v1/messages', _continue('msg_N', model='opus'))
    assert _reason(server) == 'thread_pin_unknown'


def test_pin_applies_with_auto_routing_disabled(proxy):
    server = proxy()
    _FakeUpstream.message_ids = ['msg_A']
    post(server, '/v1/messages', _create(model='sonnet'))
    assert _sent() == SONNET

    post(server, '/v1/messages', _continue('msg_A', model='opus'))
    assert _sent() == SONNET
    assert _reason(server) == 'thread_pinned'


def test_pinned_own_model_is_not_applied(proxy):
    server = proxy()
    _FakeUpstream.message_ids = ['msg_A']
    post(server, '/v1/messages', _create(model='sonnet'))
    post(server, '/v1/messages', _continue('msg_A', model='sonnet'))
    assert db_rows(server)[0]['applied'] == 0


@pytest.mark.parametrize('stream', [False, True])
def test_pinned_turn_echoes_the_requested_model(proxy, stream):
    server = proxy(**ROUTED)
    _FakeUpstream.message_ids = ['msg_A']
    post(server, '/v1/messages', _create())

    out = post(server, '/v1/messages', _continue('msg_A', model='opus', stream=stream),
               raw=stream)
    assert _sent() == HAIKU
    if stream:
        start = json.loads(out.split('data: ', 1)[1].split('\n', 1)[0])
        assert start['message']['model'] == 'opus'
    else:
        assert out['model'] == 'opus'


def test_routing_request_line_names_the_thread_type_and_no_id(proxy, caplog):
    server = proxy(**ROUTED)
    _FakeUpstream.message_ids = ['msg_secret']
    with caplog.at_level(logging.INFO):
        post(server, '/v1/messages', _create())
        post(server, '/v1/messages', _continue('msg_secret'))
    lines = [r.getMessage() for r in caplog.records if 'Routing request:' in r.getMessage()]
    assert lines[0].endswith('thread=create')
    assert lines[1].endswith('thread=continue')
    assert not any('msg_secret' in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize('thread', [{}, {'type': 7}])
def test_thread_without_a_string_type_logs_none(proxy, caplog, thread):
    server = proxy()
    with caplog.at_level(logging.INFO):
        post(server, '/v1/messages', {
            'model': 'sonnet', 'thread': thread, 'messages': [{'role': 'user', 'content': 'hi'}]})
    assert any(r.getMessage().endswith('thread=none') for r in caplog.records)


def test_unknown_thread_type_is_logged_as_other_not_raw(proxy, caplog):
    server = proxy()
    forged = 'continue\nINFO forged line' + 'x' * 5000
    with caplog.at_level(logging.INFO):
        post(server, '/v1/messages', {
            'model': 'sonnet', 'thread': {'type': forged},
            'messages': [{'role': 'user', 'content': 'hi'}]})
    lines = [r.getMessage() for r in caplog.records if 'Routing request:' in r.getMessage()]
    assert lines and lines[0].endswith('thread=other')
    assert not any('forged line' in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize('stream', [False, True])
def test_pinned_continuation_keeps_the_size_floors_long_context_beta(proxy, stream):
    server = proxy(**ROUTED, auto_model_routing_long='opus[1m]',
                   auto_model_routing_long_context_threshold=1000)
    _FakeUpstream.message_ids = ['msg_L']
    post(server, '/v1/messages', _create(stream=stream, text='word ' * 5000), raw=stream)
    assert 'context-1m' in _FakeUpstream.received[-1]['headers'].get('anthropic-beta', '')

    post(server, '/v1/messages', _continue('msg_L', stream=stream), raw=stream)
    assert _reason(server) == 'thread_pinned'
    assert 'context-1m' in _FakeUpstream.received[-1]['headers'].get('anthropic-beta', '')


@pytest.mark.parametrize('stream', [False, True])
def test_pinned_continuation_of_a_short_thread_adds_no_long_context_beta(proxy, stream):
    server = proxy(**ROUTED, auto_model_routing_long='opus[1m]',
                   auto_model_routing_long_context_threshold=1000)
    _FakeUpstream.message_ids = ['msg_S']
    post(server, '/v1/messages', _create(stream=stream), raw=stream)
    post(server, '/v1/messages', _continue('msg_S', stream=stream), raw=stream)
    assert _reason(server) == 'thread_pinned'
    assert 'context-1m' not in _FakeUpstream.received[-1]['headers'].get('anthropic-beta', '')


def test_request_without_a_thread_logs_no_thread_tag(proxy, caplog):
    server = proxy()
    with caplog.at_level(logging.INFO):
        post(server, '/v1/messages', {
            'model': 'sonnet', 'messages': [{'role': 'user', 'content': 'hi'}]})
    lines = [r.getMessage() for r in caplog.records if 'Routing request:' in r.getMessage()]
    assert lines and all('thread=' not in line for line in lines)


def test_thread_and_beta_header_are_forwarded_unchanged(proxy):
    server = proxy(**ROUTED)
    post(server, '/v1/messages', _continue('msg_x'),
         headers={'anthropic-beta': 'message-threads-2026-08-12'})
    received = _FakeUpstream.received[-1]
    assert received['payload']['thread'] == {
        'type': 'continue', 'previous_message_id': 'msg_x'}
    assert 'message-threads-2026-08-12' in received['headers']['anthropic-beta']


def test_unknown_unlocked_continuation_without_a_model_fails_locally(proxy):
    server = proxy(**ROUTED)
    payload = _continue('msg_never_seen')
    del payload['model']
    with pytest.raises(urllib.error.HTTPError) as caught:
        post(server, '/v1/messages', payload)
    assert caught.value.code == 400
    assert _FakeUpstream.received == []


LONG_REPLY = 'head ' * 40 + 'middle ' * 40 + 'tail ' * 40


@pytest.mark.parametrize('stream', [False, True])
def test_thread_turn_records_truncated_text_and_last_thread_model(proxy, stream):
    server = proxy(**ROUTED, auto_model_routing_prior_response_summary_limit=100)
    _FakeUpstream.message_ids = ['msg_R']
    _FakeUpstream.stream_text = LONG_REPLY
    _FakeUpstream.json_body['content'] = [{'type': 'text', 'text': LONG_REPLY}]
    post(server, '/v1/messages', _create(stream=stream), raw=stream)

    state = session_state(server)
    pin = state.thread_pin('msg_R')
    assert pin.model == 'haiku'
    text = pin.response_text
    assert len(text) <= 100
    assert text.startswith('head ') and text.rstrip().endswith('tail')
    assert '[truncated]' in text and 'middle' not in text
    assert state.last_thread_model(SESSION) == 'haiku'


@pytest.mark.parametrize('stream', [False, True])
def test_thread_turn_records_text_without_a_db(proxy, stream):
    server = proxy(**ROUTED, db_path=None)
    _FakeUpstream.message_ids = ['msg_R']
    post(server, '/v1/messages', _create(stream=stream), raw=stream)
    expected = 'hello' if stream else 'hi there'
    assert session_state(server).thread_pin('msg_R').response_text == expected


def test_pinned_continuation_updates_the_last_thread_model(proxy):
    server = proxy(**ROUTED)
    _FakeUpstream.message_ids = ['msg_A', 'msg_B']
    post(server, '/v1/messages', _create())
    post(server, '/v1/messages', _continue('msg_A', model='opus'))
    state = session_state(server)
    assert state.thread_pin('msg_B').model == 'haiku'
    assert state.last_thread_model(SESSION) == 'haiku'


def test_thread_turn_without_a_session_key_records_no_last_thread_model(proxy):
    server = proxy(**ROUTED)
    _FakeUpstream.message_ids = ['msg_N']
    payload = _create()
    del payload['metadata']
    post(server, '/v1/messages', payload)
    state = session_state(server)
    assert state.thread_pin('msg_N').model == 'haiku'
    assert state.last_thread_model(None) is None
