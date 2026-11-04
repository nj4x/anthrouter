"""Thread continuation re-classification (ADR-0033): the ordered continuation check."""

import hashlib
import json
import logging
import urllib.error

import pytest

from anthrouter import model_router
from anthrouter.handlers import THREAD_MODEL_MISMATCH_MESSAGE, _handoff_key
from anthrouter.model_config import resolve_model
from anthrouter.model_router import ModelRoutingDecision
from tests.conftest import (
    DEEP_PROMPT,
    SESSION,
    _FakeUpstream,
    db_rows,
    post,
    session_state,
    set_session_clock,
)

HAIKU = resolve_model('haiku')
OPUS = resolve_model('opus')

ROUTED = {'auto_model_routing': True, 'auto_model_routing_mode': 'rules'}

TOOL_RESULT_DELTA = [{'role': 'user', 'content': [
    {'type': 'tool_result', 'tool_use_id': 'toolu_1', 'content': 'ok'}]}]


@pytest.fixture
def classifier(monkeypatch):
    """Counts classifier calls and records ``prior_response_summary``; ``fail`` forces a failure."""
    real = model_router._dispatch_classifier_mode
    stub = {'calls': 0, 'priors': [], 'fail': False}

    def _dispatch(*args, prior_response_summary=None, **kwargs):
        stub['calls'] += 1
        stub['priors'].append(prior_response_summary)
        if stub['fail']:
            return None, 'classifier_failed', 0, 0, None, None, None, None, None
        return real(*args, prior_response_summary=prior_response_summary, **kwargs)

    monkeypatch.setattr(model_router, '_dispatch_classifier_mode', _dispatch)
    return stub


def _create(text='hi', stream=False):
    return {
        'model': 'sonnet', 'stream': stream, 'metadata': {'user_id': SESSION},
        'thread': {'type': 'create'},
        'messages': [{'role': 'user', 'content': text}],
    }


def _continue(previous, content=None, stream=False, model='sonnet'):
    return {
        'model': model, 'stream': stream, 'metadata': {'user_id': SESSION},
        'thread': {'type': 'continue', 'previous_message_id': previous},
        'messages': ([{'role': 'user', 'content': content}] if content is not None
                     else TOOL_RESULT_DELTA),
    }


def _haiku_thread(server, classifier, message_id='msg_A'):
    _FakeUpstream.message_ids = [message_id]
    post(server, '/v1/messages', _create())
    assert _FakeUpstream.received[-1]['payload']['model'] == HAIKU
    classifier['calls'] = 0
    classifier['priors'].clear()
    return len(_FakeUpstream.received)


def _post_400(server, payload):
    with pytest.raises(urllib.error.HTTPError) as caught:
        post(server, '/v1/messages', payload, raw=bool(payload.get('stream')))
    assert caught.value.code == 400
    return json.loads(caught.value.read().decode())


def _key(text):
    return SESSION, hashlib.sha256(text.encode()).hexdigest()


def test_tool_result_only_continuation_stays_pinned_without_classifier(proxy, classifier):
    server = proxy(**ROUTED)
    _haiku_thread(server, classifier)
    post(server, '/v1/messages', _continue('msg_A', model='opus'))
    assert _FakeUpstream.received[-1]['payload']['model'] == HAIKU
    assert classifier['calls'] == 0
    assert db_rows(server)[0]['reason_code'] == 'thread_pinned'


@pytest.mark.parametrize('stream', [False, True])
def test_long_context_continuation_stays_pinned_with_its_beta(proxy, classifier, stream):
    server = proxy(**ROUTED, auto_model_routing_long='opus[1m]',
                   auto_model_routing_long_context_threshold=1000)
    _FakeUpstream.message_ids = ['msg_L']
    post(server, '/v1/messages', _create(text='word ' * 5000, stream=stream), raw=stream)
    long_model = _FakeUpstream.received[-1]['payload']['model']
    classifier['calls'] = 0
    session_state(server).store_handoff(_key('hi'), HAIKU, ModelRoutingDecision(
        requested_model='sonnet', routed_model=HAIKU, classification='trivial',
        applied=True, reason_code='classifier_rules_trivial'))

    post(server, '/v1/messages', _continue('msg_L', 'hi', stream=stream), raw=stream)
    sent = _FakeUpstream.received[-1]
    assert sent['payload']['model'] == long_model
    assert 'context-1m' in sent['headers'].get('anthropic-beta', '')
    assert classifier['calls'] == 0
    assert db_rows(server)[0]['reason_code'] == 'thread_pinned'


def test_tool_result_only_continuation_ignores_a_live_handoff(proxy, classifier):
    server = proxy(**ROUTED)
    _haiku_thread(server, classifier)
    session_state(server).store_handoff(_key('ok'), OPUS, ModelRoutingDecision(
        requested_model='sonnet', routed_model='opus', classification='deep',
        applied=True, reason_code='classifier_rules_deep'))
    post(server, '/v1/messages', _continue('msg_A'))
    assert _FakeUpstream.received[-1]['payload']['model'] == HAIKU
    assert db_rows(server)[0]['reason_code'] == 'thread_pinned'


def test_user_prompt_to_the_threads_model_goes_upstream_pinned(proxy, classifier):
    server = proxy(**ROUTED)
    sent = _haiku_thread(server, classifier)
    post(server, '/v1/messages', _continue('msg_A', 'hi', model='opus'))
    assert len(_FakeUpstream.received) == sent + 1
    assert _FakeUpstream.received[-1]['payload']['model'] == HAIKU
    assert classifier['calls'] == 1
    row = db_rows(server)[0]
    assert row['reason_code'] == 'thread_pinned'
    assert row['classification'] == 'trivial'
    assert row['status'] == 'success'


@pytest.mark.parametrize('stream', [False, True])
def test_user_prompt_to_another_model_gets_the_local_400(proxy, classifier, caplog, stream):
    server = proxy(**ROUTED)
    sent = _haiku_thread(server, classifier)
    with caplog.at_level(logging.INFO):
        body = _post_400(server, _continue('msg_A', DEEP_PROMPT, stream=stream))
    assert body['error']['type'] == 'invalid_request_error'
    assert body['error']['message'] == THREAD_MODEL_MISMATCH_MESSAGE
    assert len(_FakeUpstream.received) == sent
    row = db_rows(server)[0]
    assert row['status'] == 'reclassified'
    assert row['reason_code'] == 'thread_reclassified'
    assert row['routed_model'] == 'opus'
    assert row['classification'] == 'deep'
    assert row['thread_model'] == 'haiku'
    assert row['error'] is None
    assert row['cost_estimate'] == 0
    assert any(
        r.getMessage().endswith(
            'Thread re-classified: haiku -> opus (tier=deep); sent model-mismatch 400 '
            'so the client opens a new Thread')
        for r in caplog.records)
    assert session_state(server).peek_handoff(_key(DEEP_PROMPT)).model == 'opus'


def test_resend_while_the_handoff_is_live_gets_the_same_400(proxy, classifier):
    server = proxy(**ROUTED)
    sent = _haiku_thread(server, classifier)
    _post_400(server, _continue('msg_A', DEEP_PROMPT, stream=True))
    assert classifier['calls'] == 1
    body = _post_400(server, _continue('msg_A', DEEP_PROMPT))
    assert body['error']['message'] == THREAD_MODEL_MISMATCH_MESSAGE
    assert classifier['calls'] == 1
    assert len(_FakeUpstream.received) == sent
    rows = db_rows(server)
    assert [r['status'] for r in rows[:2]] == ['reclassified', 'reclassified']
    assert rows[0]['reason_code'] == 'thread_reclassified'
    assert rows[0]['routed_model'] == 'opus'
    assert session_state(server).peek_handoff(_key(DEEP_PROMPT)) is not None


def test_routing_failure_pins_to_the_threads_model(proxy, classifier):
    server = proxy(**ROUTED)
    session_state(server).record_thread('msg_A', 'haiku', session_key=SESSION)
    classifier['fail'] = True
    post(server, '/v1/messages', _continue('msg_A', DEEP_PROMPT, model='opus'))
    assert classifier['calls'] == 1
    assert _FakeUpstream.received[-1]['payload']['model'] == HAIKU
    row = db_rows(server)[0]
    assert row['reason_code'] == 'thread_pinned'
    assert row['classification'] is None
    assert row['status'] == 'success'


def test_unknown_pin_goes_to_the_baseline_without_classifier(proxy, classifier):
    server = proxy(**ROUTED, lock_requested_model='sonnet')
    post(server, '/v1/messages', _continue('msg_never', DEEP_PROMPT, model='haiku'))
    assert _FakeUpstream.received[-1]['payload']['model'] == resolve_model('sonnet')
    assert classifier['calls'] == 0
    assert db_rows(server)[0]['reason_code'] == 'thread_pin_unknown'


def test_prior_response_text_reaches_prior_response_summary(proxy, classifier):
    server = proxy(**ROUTED)
    _FakeUpstream.json_body['content'] = [{'type': 'text', 'text': 'Shall I refactor it?'}]
    _haiku_thread(server, classifier)
    post(server, '/v1/messages', _continue('msg_A', 'hi'))
    assert classifier['priors'] == ['Shall I refactor it?']


def test_classified_continuation_does_not_write_the_tier_cache(proxy, classifier):
    server = proxy(**ROUTED)
    _haiku_thread(server, classifier)
    state = session_state(server)
    before = dict(state._routed_tier)
    assert before
    post(server, '/v1/messages', _continue('msg_A', 'hi'))
    assert classifier['calls'] == 1
    assert dict(state._routed_tier) == before


def test_classified_continuation_does_not_read_the_tier_cache(proxy, classifier):
    # A cached 'opus' tier would be inherited by the affirmation 'yes'
    # (affirmation_inherited) and cause a 400; unread, the turn stays pinned.
    server = proxy(**ROUTED)
    _haiku_thread(server, classifier)
    state = session_state(server)
    assert state._routed_tier
    for key in list(state._routed_tier):
        state.set_routed_tier(key, 'opus')
    post(server, '/v1/messages', _continue('msg_A', 'yes'))
    assert _FakeUpstream.received[-1]['payload']['model'] == HAIKU
    row = db_rows(server)[0]
    assert row['status'] == 'success'
    assert row['reason_code'] == 'thread_pinned'


REMINDER = (
    '<system-reminder>\n'
    'Attribution for git commits and pull requests you create from here on '
    "(this replaces Claude Code's own earlier attribution guidance):\n"
    '- End git commit messages with:\n'
    'Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n'
    '</system-reminder>'
)


def test_handoff_key_is_the_as_received_text_joined_with_newlines(proxy, classifier):
    reminder = (
        '<system-reminder>\n'
        'Attribution for git commits and pull requests you create from here on '
        "(this replaces Claude Code's own earlier attribution guidance):\n"
        '- End git commit messages with:\n'
        'Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n'
        '</system-reminder>'
    )
    server = proxy(**ROUTED, strip_attribution_reminder='on', sanitize_system_prompt='on')
    _haiku_thread(server, classifier)
    content = [{'type': 'text', 'text': DEEP_PROMPT},
               {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png',
                                            'data': 'AA=='}},
               {'type': 'text', 'text': reminder}]
    _post_400(server, _continue('msg_A', content))
    assert session_state(server).peek_handoff(_key(f'{DEEP_PROMPT}\n{reminder}')) is not None


def test_handoff_key_is_taken_before_the_attribution_strip(proxy, classifier, monkeypatch):
    # Pinned path: the strip runs and changes the dispatched user text, but the
    # key the handoff check used is the hash of the text as received.  The
    # caveman rewrite edits only the system prompt, so it cannot change the key.
    server = proxy(**ROUTED, strip_attribution_reminder='on')
    _haiku_thread(server, classifier)
    state = session_state(server)
    seen = []
    real_peek = state.peek_handoff
    monkeypatch.setattr(state, 'peek_handoff', lambda key: seen.append(key) or real_peek(key))
    content = [{'type': 'text', 'text': 'hi'}, {'type': 'text', 'text': REMINDER}]
    post(server, '/v1/messages', _continue('msg_A', content))
    dispatched = _FakeUpstream.received[-1]['payload']['messages'][-1]['content']
    assert 'Attribution for' not in json.dumps(dispatched)
    assert seen == [_key(f'hi\n{REMINDER}')]


@pytest.mark.parametrize('payload, expected', [
    ({'messages': [{'role': 'user', 'content': 'abc'}]}, 'abc'),
    ({'messages': [{'role': 'user', 'content': [
        {'type': 'text', 'text': 'a'}, {'type': 'tool_result', 'tool_use_id': 't'},
        {'type': 'text', 'text': 'b'}]}]}, 'a\nb'),
    ({'messages': TOOL_RESULT_DELTA}, None),
    ({'messages': []}, None),
])
def test_handoff_key_helper(payload, expected):
    key = _handoff_key(SESSION, payload)
    if expected is None:
        assert key is None
    else:
        assert key == _key(expected)
    assert _handoff_key(None, {'messages': [{'role': 'user', 'content': 'abc'}]}) is None


# -- Thread replays (ADR-0033 Decisions 5 and 6) -------------------------------

PRIOR = 'Shall I refactor it?'


def _replay(final, stream=False, model='sonnet', opening='hello'):
    return {
        'model': model, 'stream': stream, 'metadata': {'user_id': SESSION},
        'thread': {'type': 'create'},
        'messages': [
            {'role': 'user', 'content': opening},
            {'role': 'assistant', 'content': [{'type': 'text', 'text': PRIOR}]},
            {'role': 'user', 'content': final},
        ],
    }


def _store(server, text, model='sonnet'):
    session_state(server).store_handoff(_key(text), model, ModelRoutingDecision(
        requested_model='sonnet', routed_model=model, classification='standard',
        applied=False, reason_code='classifier_rules_standard'))


def test_replay_takes_the_handoff_and_skips_the_classifier(proxy, classifier, caplog):
    server = proxy(**ROUTED)
    _store(server, 'hi')
    with caplog.at_level(logging.INFO):
        post(server, '/v1/messages', _replay('hi'))
    assert _FakeUpstream.received[-1]['payload']['model'] == resolve_model('sonnet')
    assert classifier['calls'] == 0
    assert db_rows(server)[0]['reason_code'] == 'thread_reclassified_replay'
    assert session_state(server).peek_handoff(_key('hi')) is None
    assert not session_state(server)._routed_tier
    assert any(r.getMessage().endswith('Thread re-classification handoff: model=sonnet')
               for r in caplog.records)


def test_replay_with_other_final_text_does_not_take_the_handoff(proxy, classifier):
    server = proxy(**ROUTED)
    _store(server, DEEP_PROMPT)
    post(server, '/v1/messages', _replay('hi'))
    assert _FakeUpstream.received[-1]['payload']['model'] == HAIKU
    assert classifier['calls'] == 1
    assert session_state(server).peek_handoff(_key(DEEP_PROMPT)) is not None


def test_handoff_expires_after_60_seconds(proxy, classifier):
    server = proxy(**ROUTED)
    now = [1000.0]
    set_session_clock(server, lambda: now[0])
    _store(server, 'hi')
    now[0] += 61
    post(server, '/v1/messages', _replay('hi'))
    assert _FakeUpstream.received[-1]['payload']['model'] == HAIKU
    assert classifier['calls'] == 1
    assert db_rows(server)[0]['reason_code'] != 'thread_reclassified_replay'


def test_size_floor_wins_over_a_live_handoff(proxy, classifier):
    server = proxy(**ROUTED, auto_model_routing_long='opus[1m]',
                   auto_model_routing_long_context_threshold=1000)
    _store(server, 'hi', model='haiku')
    _FakeUpstream.message_ids = ['msg_R']
    post(server, '/v1/messages', _replay('hi', opening='word ' * 5000))
    sent = _FakeUpstream.received[-1]
    assert sent['payload']['model'] == resolve_model('opus[1m]')
    assert 'context-1m' in sent['headers'].get('anthropic-beta', '')
    assert classifier['calls'] == 0
    assert db_rows(server)[0]['reason_code'] == 'thread_reclassified_replay'
    state = session_state(server)
    assert state.peek_handoff(_key('hi')) is None
    assert state.thread_pin('msg_R').long_context is True


def test_create_without_prior_assistant_keeps_the_tier_cache_and_the_handoff(proxy, classifier):
    server = proxy(**ROUTED)
    _store(server, 'hi')
    post(server, '/v1/messages', _create('hi'))
    assert _FakeUpstream.received[-1]['payload']['model'] == HAIKU
    state = session_state(server)
    assert state.peek_handoff(_key('hi')) is not None
    assert state._routed_tier


def test_replay_retry_after_429_is_classified_without_handoff(proxy, classifier):
    server = proxy(**ROUTED)
    _store(server, 'hi', model='opus')
    _FakeUpstream.status = 429
    _FakeUpstream.error_type = 'rate_limit_error'
    with pytest.raises(urllib.error.HTTPError):
        post(server, '/v1/messages', _replay('hi'))
    assert _FakeUpstream.received[-1]['payload']['model'] == OPUS
    assert session_state(server).peek_handoff(_key('hi')) is None
    _FakeUpstream.status = 200
    post(server, '/v1/messages', _replay('hi'))
    assert _FakeUpstream.received[-1]['payload']['model'] == HAIKU
    assert classifier['calls'] == 1
    assert db_rows(server)[0]['reason_code'] != 'thread_reclassified_replay'
    assert not session_state(server)._routed_tier


def test_replay_without_handoff_is_classified_with_prior_and_no_tier_cache(proxy, classifier):
    # A cached 'opus' tier for the opening turn would be inherited by the
    # affirmation 'yes' (affirmation_inherited); unread, the replay is classified.
    server = proxy(**ROUTED)
    post(server, '/v1/messages', _create('hello'))
    state = session_state(server)
    assert state._routed_tier
    for key in list(state._routed_tier):
        state.set_routed_tier(key, 'opus')
    before = dict(state._routed_tier)
    classifier['calls'] = 0
    classifier['priors'].clear()
    post(server, '/v1/messages', _replay('yes'))
    reason = db_rows(server)[0]['reason_code']
    assert reason.startswith('affirmation_') and reason != 'affirmation_inherited'
    assert _FakeUpstream.received[-1]['payload']['model'] != OPUS
    post(server, '/v1/messages', _replay('hi'))
    assert classifier['calls'] == 1
    assert classifier['priors'] == [PRIOR]
    assert dict(state._routed_tier) == before


def test_replay_routing_failure_fails_closed_to_the_baseline(proxy, classifier):
    server = proxy(**ROUTED, lock_requested_model='sonnet')
    session_state(server).record_thread('msg_A', 'haiku', session_key=SESSION)
    classifier['fail'] = True
    post(server, '/v1/messages', _replay(DEEP_PROMPT, model='opus'))
    assert classifier['calls'] == 1
    assert _FakeUpstream.received[-1]['payload']['model'] == resolve_model('sonnet')
    assert db_rows(server)[0]['classification'] is None


def test_tool_result_replay_goes_to_the_last_thread_model(proxy, classifier):
    server = proxy(**ROUTED)
    session_state(server).record_thread('msg_A', 'haiku', session_key=SESSION)
    post(server, '/v1/messages', _replay(TOOL_RESULT_DELTA[0]['content'], model='opus'))
    assert _FakeUpstream.received[-1]['payload']['model'] == HAIKU
    assert classifier['calls'] == 0
    assert db_rows(server)[0]['reason_code'] == 'thread_replay_pinned'


def test_tool_result_replay_with_no_last_thread_model_goes_to_the_baseline(proxy, classifier):
    server = proxy(**ROUTED, lock_requested_model='sonnet')
    post(server, '/v1/messages', _replay(TOOL_RESULT_DELTA[0]['content'], model='opus'))
    assert _FakeUpstream.received[-1]['payload']['model'] == resolve_model('sonnet')
    assert classifier['calls'] == 0
    assert db_rows(server)[0]['reason_code'] == 'thread_replay_pinned'


def test_continuation_400_then_replay_lands_on_the_new_model(proxy, classifier):
    server = proxy(**ROUTED)
    sent = _haiku_thread(server, classifier)
    _post_400(server, _continue('msg_A', DEEP_PROMPT))
    assert len(_FakeUpstream.received) == sent
    post(server, '/v1/messages', _replay(DEEP_PROMPT, opening='hi'))
    assert _FakeUpstream.received[-1]['payload']['model'] == OPUS
    assert classifier['calls'] == 1
    row = db_rows(server)[0]
    assert row['reason_code'] == 'thread_reclassified_replay'
    assert row['status'] == 'success'
    assert session_state(server).peek_handoff(_key(DEEP_PROMPT)) is None
