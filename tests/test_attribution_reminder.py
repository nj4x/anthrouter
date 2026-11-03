"""Attribution-reminder strip (ADR-0023, ported from anthproxy ADR-0054)."""

import copy
from types import SimpleNamespace

import pytest

from anthrouter.config import Config, parse_args
from anthrouter.handlers import (
    HAPPY_NEW_YEAR_PREFIX,
    _capture_dispatched_user_text,
    _strip_attribution_reminder,
)
from anthrouter.request_text import strip_attribution_reminders
from tests.conftest import SESSION, _FakeUpstream, db_rows, post

REMINDER = (
    '<system-reminder>\n'
    'Attribution for git commits and pull requests you create from here on '
    "(this replaces Claude Code's own earlier attribution guidance):\n"
    '- End git commit messages with:\n'
    'Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n'
    '- End pull request descriptions with:\n'
    '🤖 Generated with [Claude Code](https://claude.com/claude-code)\n'
    '</system-reminder>'
)
CLAUDE_MD = (
    '<system-reminder>\nContents of CLAUDE.md:\n'
    'Never add Co-Authored-By or Generated with lines.\n</system-reminder>'
)


def _user(content):
    return {'role': 'user', 'content': content}


# ---------------------------------------------------------------------------
# strip_attribution_reminders
# ---------------------------------------------------------------------------

def test_string_content_keeps_surrounding_text_byte_for_byte():
    msgs = [_user('before\n' + REMINDER + '\nafter')]
    out, n = strip_attribution_reminders(msgs)
    assert n == 1
    assert out[0]['content'] == 'before\n\nafter'


def test_list_block_holding_only_reminder_is_dropped():
    msgs = [_user([{'type': 'text', 'text': 'hello'}, {'type': 'text', 'text': REMINDER}])]
    out, n = strip_attribution_reminders(msgs)
    assert n == 1
    assert out[0]['content'] == [{'type': 'text', 'text': 'hello'}]


def test_reminder_inside_shared_block_is_cut_out():
    text = CLAUDE_MD + '\n' + REMINDER + '\nquestion'
    out, n = strip_attribution_reminders([_user([{'type': 'text', 'text': text}])])
    assert n == 1
    assert out[0]['content'][0]['text'] == CLAUDE_MD + '\n\nquestion'


def test_other_reminders_mentioning_keywords_are_kept():
    msgs = [_user(CLAUDE_MD + '\nhi')]
    out, n = strip_attribution_reminders(msgs)
    assert n == 0
    assert out is msgs


def test_unclosed_reminder_does_not_match_or_run_into_next_close():
    unclosed = REMINDER.replace('</system-reminder>', '')
    text = unclosed + '\n' + CLAUDE_MD + '\nq'
    msgs = [_user(text)]
    out, n = strip_attribution_reminders(msgs)
    assert n == 0
    assert out is msgs


def test_every_user_message_is_stripped_not_assistant_or_system():
    msgs = [
        _user('a ' + REMINDER),
        {'role': 'assistant', 'content': 'x ' + REMINDER},
        {'role': 'system', 'content': 'y ' + REMINDER},
        _user([{'type': 'text', 'text': 'b'}, {'type': 'text', 'text': REMINDER}]),
    ]
    out, n = strip_attribution_reminders(msgs)
    assert n == 2
    assert out[0]['content'] == 'a '
    assert out[1] is msgs[1]
    assert out[2] is msgs[2]
    assert out[3]['content'] == [{'type': 'text', 'text': 'b'}]


def test_tool_result_content_is_not_scanned():
    block = {'type': 'tool_result', 'tool_use_id': 't1', 'content': [{'type': 'text', 'text': REMINDER}]}
    msgs = [_user([block, {'type': 'text', 'text': 'q'}])]
    out, n = strip_attribution_reminders(msgs)
    assert n == 0
    assert out is msgs


@pytest.mark.parametrize('content', [
    REMINDER, [{'type': 'text', 'text': REMINDER}],
], ids=['string', 'list'])
def test_message_left_empty_is_kept_unchanged(content):
    msgs = [_user(content)]
    out, n = strip_attribution_reminders(msgs)
    assert n == 0
    assert out is msgs


def test_prefix_is_case_sensitive():
    other = REMINDER.replace('Attribution for git', 'attribution for git')
    msgs = [_user('q ' + other)]
    out, n = strip_attribution_reminders(msgs)
    assert n == 0
    assert out is msgs


def test_tag_case_attributes_and_closing_whitespace_match():
    text = REMINDER.replace('<system-reminder>', '<System-Reminder data-x="1">').replace(
        '</system-reminder>', '</SYSTEM-REMINDER >')
    out, n = strip_attribution_reminders([_user('q ' + text)])
    assert n == 1
    assert out[0]['content'] == 'q '


def test_two_reminders_in_one_block():
    out, n = strip_attribution_reminders([_user('q ' + REMINDER + ' ' + REMINDER)])
    assert n == 2
    assert out[0]['content'] == 'q  '


def test_cache_control_moves_onto_non_text_previous_block():
    cc = {'type': 'ephemeral'}
    image = {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'x'}}
    msgs = [_user([image, {'type': 'text', 'text': REMINDER, 'cache_control': cc}])]
    out, n = strip_attribution_reminders(msgs)
    assert n == 1
    assert out[0]['content'] == [{**image, 'cache_control': cc}]


def test_dropped_block_cache_control_moves_to_previous_block():
    cc = {'type': 'ephemeral'}
    msgs = [_user([
        {'type': 'text', 'text': 'q'},
        {'type': 'text', 'text': REMINDER, 'cache_control': cc},
    ])]
    out, n = strip_attribution_reminders(msgs)
    assert n == 1
    assert out[0]['content'] == [{'type': 'text', 'text': 'q', 'cache_control': cc}]


def test_dropped_block_cache_control_without_free_previous_block_keeps_block():
    cc = {'type': 'ephemeral'}
    content = [
        {'type': 'text', 'text': 'q', 'cache_control': cc},
        {'type': 'text', 'text': REMINDER, 'cache_control': cc},
    ]
    msgs = [_user(content)]
    out, n = strip_attribution_reminders(msgs)
    assert n == 0
    assert out is msgs


def test_input_is_not_mutated():
    msgs = [_user([{'type': 'text', 'text': 'q ' + REMINDER}])]
    before = copy.deepcopy(msgs)
    strip_attribution_reminders(msgs)
    assert msgs == before


def test_non_list_messages_pass_through():
    assert strip_attribution_reminders(None) == (None, 0)


def test_opener_requires_tag_name_boundary():
    text = REMINDER.replace('<system-reminder>', '<system-reminder-x>', 1)
    msgs = [_user('q ' + text)]
    out, n = strip_attribution_reminders(msgs)
    assert n == 0
    assert out is msgs


def test_list_with_only_blank_sibling_is_kept_unchanged():
    msgs = [_user([{'type': 'text', 'text': '  '}, {'type': 'text', 'text': REMINDER}])]
    out, n = strip_attribution_reminders(msgs)
    assert n == 0
    assert out is msgs


def test_long_unterminated_opener_scan_is_bounded():
    msgs = [_user('<system-reminder ' * 20000)]
    out, n = strip_attribution_reminders(msgs)
    assert n == 0
    assert out is msgs


# ---------------------------------------------------------------------------
# Handler seam: _strip_attribution_reminder
# ---------------------------------------------------------------------------

def _handler():
    return SimpleNamespace(_log_tag=lambda: '[t]')


def _payload(system=None):
    payload = {'messages': [_user('q ' + REMINDER)]}
    if system is not None:
        payload['system'] = system
    return payload


def test_handler_strips_when_on(caplog):
    payload, capture = _payload(), {}
    with caplog.at_level('INFO', logger='anthrouter.handlers'):
        _strip_attribution_reminder(_handler(), payload, SimpleNamespace(strip_attribution_reminder='on'), capture)
    assert payload['messages'][0]['content'] == 'q '
    assert 'Stripped 1 attribution reminder(s) from user messages' in caplog.text
    assert capture == {'attribution_reminders_stripped': 1}


def test_handler_counts_strips_across_every_user_turn():
    payload = {'messages': [
        _user('a ' + REMINDER), {'role': 'assistant', 'content': 'x'}, _user('b ' + REMINDER),
        {'role': 'assistant', 'content': 'y'}, _user('c'),
    ]}
    capture = {}
    _strip_attribution_reminder(_handler(), payload, SimpleNamespace(strip_attribution_reminder='on'), capture)
    assert capture == {'attribution_reminders_stripped': 2}


def test_handler_records_no_count_when_nothing_stripped():
    capture = {}
    _strip_attribution_reminder(
        _handler(), {'messages': [_user('q')]}, SimpleNamespace(strip_attribution_reminder='on'), capture)
    assert capture == {}


@pytest.mark.parametrize('mode,system', [
    ('off', None),
    ('on', HAPPY_NEW_YEAR_PREFIX),
], ids=['off', 'security-monitor'])
def test_handler_skips(mode, system):
    payload, capture = _payload(system), {}
    _strip_attribution_reminder(_handler(), payload, SimpleNamespace(strip_attribution_reminder=mode), capture)
    assert payload == _payload(system)
    assert capture == {}


def test_handler_never_raises(monkeypatch, caplog):
    from anthrouter import handlers

    def boom(messages):
        raise RuntimeError('boom')
    monkeypatch.setattr(handlers, 'strip_attribution_reminders', boom)
    payload, capture = _payload(), {}
    with caplog.at_level('WARNING', logger='anthrouter.handlers'):
        _strip_attribution_reminder(_handler(), payload, SimpleNamespace(strip_attribution_reminder='on'), capture)
    assert payload == _payload()
    assert capture == {}
    assert 'Attribution-reminder strip failed' in caplog.text


def test_capture_records_changed_dispatched_text():
    capture = {'user_prompt_text': 'q ' + REMINDER}
    _capture_dispatched_user_text(_handler(), {'messages': [_user('q ')]}, capture)
    assert capture['user_prompt_dispatched_text'] == 'q '


def test_capture_skips_unchanged_text():
    capture = {'user_prompt_text': 'q'}
    _capture_dispatched_user_text(_handler(), {'messages': [_user('q')]}, capture)
    assert 'user_prompt_dispatched_text' not in capture


def test_capture_skips_when_original_capture_failed():
    capture = {}
    _capture_dispatched_user_text(_handler(), {'messages': [_user('q')]}, capture)
    assert capture == {}


def test_capture_never_raises(monkeypatch, caplog):
    from anthrouter import handlers

    def boom(payload):
        raise RuntimeError('boom')
    monkeypatch.setattr(handlers, '_extract_user_prompt_text', boom)
    capture = {'user_prompt_text': 'q'}
    with caplog.at_level('WARNING', logger='anthrouter.handlers'):
        _capture_dispatched_user_text(_handler(), {'messages': [_user('q')]}, capture)
    assert capture == {'user_prompt_text': 'q'}
    assert 'Dispatched user-text capture failed' in caplog.text


def test_config_flag_default():
    assert Config().strip_attribution_reminder == 'on'


def test_config_cli_and_env(monkeypatch):
    monkeypatch.delenv('ANTHROUTER_STRIP_ATTRIBUTION_REMINDER', raising=False)
    assert parse_args([]).strip_attribution_reminder == 'on'
    assert parse_args(['--strip-attribution-reminder', 'off']).strip_attribution_reminder == 'off'
    monkeypatch.setenv('ANTHROUTER_STRIP_ATTRIBUTION_REMINDER', 'off')
    assert parse_args([]).strip_attribution_reminder == 'off'


def test_config_rejects_unknown_value():
    with pytest.raises(SystemExit):
        parse_args(['--strip-attribution-reminder', 'maybe'])


# ---------------------------------------------------------------------------
# End to end through the proxy
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('mode,stripped', [('on', True), ('off', False)])
def test_dispatched_messages_are_stripped(proxy, upstream, mode, stripped):
    server = proxy(strip_attribution_reminder=mode)
    post(server, '/v1/messages', {
        'model': 'sonnet', 'metadata': {'user_id': SESSION},
        'messages': [_user('q ' + REMINDER)]})
    sent = _FakeUpstream.received[-1]['payload']['messages'][0]['content']
    assert (sent == 'q ') is stripped


def test_security_monitor_request_is_forwarded_unstripped(proxy, upstream):
    server = proxy(strip_attribution_reminder='on')
    post(server, '/v1/messages', {
        'model': 'sonnet', 'system': HAPPY_NEW_YEAR_PREFIX,
        'messages': [_user('q ' + REMINDER)]})
    assert _FakeUpstream.received[-1]['payload']['messages'][0]['content'] == 'q ' + REMINDER


@pytest.mark.parametrize('mode,dispatched', [('on', 'q '), ('off', None)])
def test_recorded_row_holds_dispatched_user_text(proxy, upstream, mode, dispatched):
    server = proxy(strip_attribution_reminder=mode)
    post(server, '/v1/messages', {
        'model': 'sonnet', 'metadata': {'user_id': SESSION},
        'messages': [_user('q ' + REMINDER)]})
    row = db_rows(server)[0]
    assert row['user_prompt_text'] == 'q ' + REMINDER
    assert row['user_prompt_dispatched_text'] == dispatched
