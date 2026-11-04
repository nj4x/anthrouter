"""SessionState stores the HTTP seam cannot see (ADR-0033): handoffs, last Thread model, Thread text."""

import hashlib

import pytest

from anthrouter.model_router import (
    _TRUNCATION_MARKER,
    ModelRoutingDecision,
    _truncate_prior_response,
)
from anthrouter.session_state import HANDOFF_TTL_SECONDS, MAX_ENTRIES, SessionState

SESSION = 'session-a'


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _key(session=SESSION, text='refactor the parser'):
    return (session, hashlib.sha256(text.encode()).hexdigest())


def _decision(model='claude-opus'):
    return ModelRoutingDecision(
        requested_model='claude-sonnet', routed_model=model, classification='deep',
        applied=True, reason_code='thread_pinned',
    )


@pytest.fixture
def clock():
    return _Clock()


@pytest.fixture
def state(clock):
    return SessionState(clock=clock)


def test_handoff_ttl_is_sixty_seconds():
    assert HANDOFF_TTL_SECONDS == 60


def test_handoff_is_live_until_sixty_seconds_then_absent(state, clock):
    decision = _decision()
    state.store_handoff(_key(), 'claude-opus', decision)
    clock.now += HANDOFF_TTL_SECONDS - 0.001
    held = state.peek_handoff(_key())
    assert held is not None
    assert (held.model, held.decision) == ('claude-opus', decision)

    clock.now += 0.001
    assert state.peek_handoff(_key()) is None
    assert state.take_handoff(_key()) is None


def test_expired_handoff_is_absent_even_if_the_clock_goes_back(state, clock):
    state.store_handoff(_key(), 'claude-opus', _decision())
    clock.now += HANDOFF_TTL_SECONDS
    assert state.take_handoff(_key()) is None
    clock.now -= HANDOFF_TTL_SECONDS
    assert state.peek_handoff(_key()) is None


def test_peek_keeps_and_take_deletes(state):
    state.store_handoff(_key(), 'claude-opus', _decision())
    assert state.peek_handoff(_key()).model == 'claude-opus'
    assert state.peek_handoff(_key()).model == 'claude-opus'
    assert state.take_handoff(_key()).model == 'claude-opus'
    assert state.take_handoff(_key()) is None
    assert state.peek_handoff(_key()) is None


def test_restore_overwrites_with_a_fresh_expiry(state, clock):
    state.store_handoff(_key(), 'claude-opus', _decision('claude-opus'))
    clock.now += 50
    state.store_handoff(_key(), 'claude-haiku', _decision('claude-haiku'))
    clock.now += 50
    held = state.peek_handoff(_key())
    assert held.model == 'claude-haiku'
    assert held.decision.routed_model == 'claude-haiku'


def test_handoff_key_separates_sessions_and_texts(state):
    state.store_handoff(_key(), 'claude-opus', _decision())
    assert state.peek_handoff(_key(session='session-b')) is None
    assert state.peek_handoff(_key(text='other prompt')) is None


def test_no_session_key_stores_peeks_and_takes_nothing(state):
    state.store_handoff(_key(session=None), 'claude-opus', _decision())
    assert state.peek_handoff(_key(session=None)) is None
    assert state.take_handoff(_key(session=None)) is None
    assert state._handoffs == {}


def test_handoff_store_evicts_oldest_first(state):
    for i in range(MAX_ENTRIES + 3):
        state.store_handoff(_key(text=f'prompt {i}'), 'claude-opus', _decision())
    assert len(state._handoffs) == MAX_ENTRIES
    assert state.peek_handoff(_key(text='prompt 0')) is None
    assert state.peek_handoff(_key(text='prompt 2')) is None
    assert state.peek_handoff(_key(text='prompt 3')) is not None
    assert state.peek_handoff(_key(text=f'prompt {MAX_ENTRIES + 2}')) is not None


def test_record_thread_updates_the_sessions_last_thread_model(state):
    assert state.last_thread_model(SESSION) is None
    state.record_thread('msg_1', 'claude-haiku', session_key=SESSION)
    assert state.last_thread_model(SESSION) == 'claude-haiku'
    state.record_thread('msg_2', 'claude-opus', session_key=SESSION)
    assert state.last_thread_model(SESSION) == 'claude-opus'
    state.record_thread('msg_3', 'claude-sonnet', session_key='session-b')
    assert state.last_thread_model(SESSION) == 'claude-opus'


def test_record_thread_without_a_session_key_leaves_last_thread_model_alone(state):
    state.record_thread('msg_1', 'claude-haiku', session_key=SESSION)
    state.record_thread('msg_2', 'claude-opus')
    assert state.last_thread_model(SESSION) == 'claude-haiku'
    assert state.last_thread_model(None) is None
    assert state.thread_pin('msg_2').model == 'claude-opus'


def test_last_thread_model_store_evicts_oldest_first(state):
    for i in range(MAX_ENTRIES + 2):
        state.record_thread(f'msg_{i}', 'claude-haiku', session_key=f'session-{i}')
    assert len(state._last_thread_model) == MAX_ENTRIES
    assert state.last_thread_model('session-0') is None
    assert state.last_thread_model('session-1') is None
    assert state.last_thread_model('session-2') == 'claude-haiku'


def test_thread_pin_returns_model_long_context_and_response_text(state):
    state.record_thread('msg_1', 'claude-opus', True, response_text='short reply')
    pin = state.thread_pin('msg_1')
    assert pin == ('claude-opus', True, 'short reply')
    assert (pin.model, pin.long_context, pin.response_text) == (
        'claude-opus', True, 'short reply')


def test_stored_response_text_is_truncated_by_the_prior_response_rule(state):
    text = 'h' * 400 + 'm' * 400 + 't' * 400
    state.record_thread('msg_1', 'claude-opus', response_text=text, response_text_limit=200)
    stored = state.thread_pin('msg_1').response_text
    assert stored == _truncate_prior_response(text, 200)
    assert len(stored) == 200
    assert stored.startswith('h') and stored.endswith('t')


def test_stored_response_text_defaults_to_the_config_default_limit(state):
    text = 'h' * 750 + 't' * 750
    state.record_thread('msg_1', 'claude-opus', response_text=text)
    stored = state.thread_pin('msg_1').response_text
    assert len(stored) == 1000
    assert stored.startswith('h') and stored.endswith('t')
    assert _TRUNCATION_MARKER in stored


@pytest.mark.parametrize('text', [None, ''])
def test_missing_response_text_is_stored_as_none(state, text):
    state.record_thread('msg_1', 'claude-opus', response_text=text)
    assert state.thread_pin('msg_1').response_text is None
