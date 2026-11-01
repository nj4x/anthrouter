"""Caveman-mode system-prompt rewrite: edit-op validation, cache, fallback."""

import hashlib
import json
import threading
import time
from types import SimpleNamespace

from anthrouter import caveman_rewrite as cr
from anthrouter import handlers

PROMPT = (
    "You are Claude Code.\n\n"
    "# Doing tasks\n"
    "Write complete sentences in commit messages.\n\n"
    "# Tone and style\n"
    "Write in complete, grammatical sentences with articles.\n"
    "Use markdown links for files.\n\n"
    "# Tool usage\n"
    "Prefer dedicated tools.\n"
)
SYSTEM = [
    {'type': 'text', 'text': "You are Claude Code, Anthropic's official CLI for Claude."},
    {'type': 'text', 'text': PROMPT, 'cache_control': {'type': 'ephemeral'}},
]
GOOD_OPS = [{'find': 'Write in complete, grammatical sentences with articles.\n', 'replace': ''}]


def _payload(level='ultra', system=SYSTEM):
    return {
        'model': 'claude-opus-5-5',
        'system': json.loads(json.dumps(system)),
        'messages': [{'role': 'user', 'content': [
            {'type': 'text', 'text': f'<system-reminder>CAVEMAN MODE ACTIVE — level: {level}</system-reminder>'},
            {'type': 'text', 'text': 'hi'},
        ]}],
    }


def _model(reply, calls=None):
    def call(system, user):
        if calls is not None:
            calls.append(user)
        return json.dumps(reply) if not isinstance(reply, str) else reply
    return call


# --- detection ---------------------------------------------------------------

def test_detect_mode_from_first_user_message():
    assert cr.detect_mode(_payload('full')) == 'full'
    assert cr.detect_mode({'messages': [{'role': 'user', 'content': 'plain'}]}) is None
    later = {'messages': [{'role': 'user', 'content': 'x'}, {'role': 'assistant', 'content': 'y'},
             {'role': 'user', 'content': 'CAVEMAN MODE ACTIVE — level: ultra'}]}
    assert cr.detect_mode(later) is None


def test_detect_mode_from_inline_system_message_in_opening_turn():
    # Claude Code sends the SessionStart header as role:system after the first user message.
    payload = {'messages': [
        {'role': 'user', 'content': 'hello'},
        {'role': 'system', 'content': 'CAVEMAN MODE ACTIVE — level: ultra (rules: ultracave)'},
    ]}
    assert cr.detect_mode(payload) == 'ultra'


# --- edit-op validation ------------------------------------------------------

def test_edit_inside_tone_section_applies():
    out, n = cr.apply_edits(PROMPT, GOOD_OPS)
    assert n == 1
    assert out == PROMPT.replace(GOOD_OPS[0]['find'], '')


def test_edit_outside_comm_section_rejected():
    ops = [{'find': 'Write complete sentences in commit messages.', 'replace': ''}]
    assert cr.apply_edits(PROMPT, ops) is None


def test_edit_spanning_section_boundary_rejected():
    ops = [{'find': 'Use markdown links for files.\n\n# Tool usage', 'replace': ''}]
    assert cr.apply_edits(PROMPT, ops) is None


def test_edit_touching_heading_rejected():
    ops = [{'find': '# Tone and style', 'replace': '# Tone'}]
    assert cr.apply_edits(PROMPT, ops) is None


def test_non_unique_and_missing_find_rejected():
    assert cr.apply_edits(PROMPT, [{'find': 'Write', 'replace': ''}]) is None
    assert cr.apply_edits(PROMPT, [{'find': 'absent text', 'replace': ''}]) is None
    assert cr.apply_edits(PROMPT, 'not a list') is None


def test_out_of_section_op_keeps_original_and_does_not_cache(tmp_path):
    rw = cr.CavemanRewriter(cr.RewriteCache(tmp_path))
    bad = GOOD_OPS + [{'find': 'Prefer dedicated tools.', 'replace': ''}]
    result = rw.rewrite(SYSTEM, 'ultra', _model(bad))
    assert result.outcome == 'rejected'
    assert result.system == SYSTEM
    assert list(tmp_path.iterdir()) == []


def test_other_text_survives_verbatim(tmp_path):
    rw = cr.CavemanRewriter(cr.RewriteCache(tmp_path))
    result = rw.rewrite(SYSTEM, 'ultra', _model(GOOD_OPS))
    assert result.outcome == 'rewritten'
    assert result.system[0] == SYSTEM[0]
    assert result.system[1]['cache_control'] == {'type': 'ephemeral'}
    assert result.system[1]['text'] == PROMPT.replace(GOOD_OPS[0]['find'], '')
    assert SYSTEM[1]['text'] == PROMPT  # input not mutated


# --- cache -------------------------------------------------------------------

def test_cache_miss_then_hit(tmp_path):
    rw = cr.CavemanRewriter(cr.RewriteCache(tmp_path))
    calls = []
    first = rw.rewrite(SYSTEM, 'ultra', _model(GOOD_OPS, calls))
    second = rw.rewrite(SYSTEM, 'ultra', _model(GOOD_OPS, calls))
    assert (first.outcome, second.outcome) == ('rewritten', 'cache_hit')
    assert second.system == first.system
    assert len(calls) == 1
    # A different mode is a different key.
    assert rw.rewrite(SYSTEM, 'lite', _model([], calls)).outcome == 'unchanged'
    assert len(calls) == 2


def test_rewritten_prompt_maps_to_itself(tmp_path):
    rw = cr.CavemanRewriter(cr.RewriteCache(tmp_path))
    first = rw.rewrite(SYSTEM, 'ultra', _model(GOOD_OPS))
    again = rw.rewrite(first.system, 'ultra', _model(GOOD_OPS))
    assert again.outcome == 'cache_hit' and again.system == first.system


def test_no_comm_section_skips_model(tmp_path):
    rw = cr.CavemanRewriter(cr.RewriteCache(tmp_path))
    calls = []
    result = rw.rewrite('# Tools\nUse them.', 'ultra', _model(GOOD_OPS, calls))
    assert result.outcome == 'no_guidance' and calls == []


def test_byte_stable_across_restart(tmp_path):
    first = cr.CavemanRewriter(cr.RewriteCache(tmp_path)).rewrite(SYSTEM, 'ultra', _model(GOOD_OPS))

    def boom(system, user):
        raise AssertionError('model must not be called after restart')

    restarted = cr.CavemanRewriter(cr.RewriteCache(tmp_path))
    second = restarted.rewrite(SYSTEM, 'ultra', boom)
    assert second.outcome == 'cache_hit'
    assert json.dumps(second.system, sort_keys=True) == json.dumps(first.system, sort_keys=True)


# --- failure fallback --------------------------------------------------------

def test_timeout_falls_back_uncached(tmp_path):
    release = threading.Event()

    def slow(system, user):
        release.wait(5)
        return json.dumps(GOOD_OPS)

    rw = cr.CavemanRewriter(cr.RewriteCache(tmp_path), timeout=0.05)
    result = rw.rewrite(SYSTEM, 'ultra', slow)
    release.set()
    assert result.outcome == 'timeout' and result.system == SYSTEM
    assert list(tmp_path.iterdir()) == []


def test_model_error_falls_back_uncached(tmp_path):
    def fail(system, user):
        raise RuntimeError('upstream 500')

    rw = cr.CavemanRewriter(cr.RewriteCache(tmp_path))
    result = rw.rewrite(SYSTEM, 'ultra', fail)
    assert result.outcome == 'error' and result.system == SYSTEM
    assert list(tmp_path.iterdir()) == []


def test_garbage_reply_rejected(tmp_path):
    rw = cr.CavemanRewriter(cr.RewriteCache(tmp_path))
    assert rw.rewrite(SYSTEM, 'ultra', _model('sure, here you go')).outcome == 'rejected'


# --- handler integration -----------------------------------------------------

class _Backend:
    def __init__(self, reply=None, exc=None):
        self.reply, self.exc, self.payloads = reply, exc, []

    def send_classifier_message(self, payload, credentials, config):
        self.payloads.append(payload)
        if self.exc:
            raise self.exc
        return {'content': [{'type': 'text', 'text': json.dumps(self.reply)}]}


class _SplitBackend:
    """Replies with the edit ops that fit the sections it was shown."""

    def send_classifier_message(self, payload, credentials, config):
        user = payload['messages'][0]['content']
        ops = STYLE_OPS if 'Output Style' in user else GOOD_OPS
        return {'content': [{'type': 'text', 'text': json.dumps(ops)}]}


def _snapshot(tmp_path, backend, name='anthropic'):
    config = SimpleNamespace(
        caveman_rewrite='on', caveman_rewrite_model='claude-sonnet-5-5',
        caveman_rewrite_timeout=5.0, caveman_rewrite_dir=str(tmp_path),
    )
    return SimpleNamespace(config=config, backend=backend, name=name)


def _handler():
    return SimpleNamespace(_log_tag=lambda: '[t]', _security_monitor=False, _sanitized_capture=None)


def test_handler_rewrites_and_logs(tmp_path, caplog):
    backend = _Backend(GOOD_OPS)
    payload, h, capture = _payload(), _handler(), {'system_prompt_sha256': 'orig'}
    with caplog.at_level('INFO'):
        handlers._rewrite_caveman_system(h, payload, _snapshot(tmp_path, backend), {}, capture)
    assert payload['system'][1]['text'] == PROMPT.replace(GOOD_OPS[0]['find'], '')
    assert backend.payloads[0]['model'] == 'claude-sonnet-5-5'
    assert backend.payloads[0]['_anthproxy_internal_classifier'] is True
    assert h._caveman_state == 'rewritten'
    assert capture['system_prompt_sha256'] == 'orig'
    assert 'outcome=rewritten' in caplog.text


def test_handler_error_sends_original(tmp_path, caplog):
    payload, h = _payload(), _handler()
    snap = _snapshot(tmp_path, _Backend(exc=RuntimeError('boom')))
    handlers._rewrite_caveman_system(h, payload, snap, {}, {})
    assert payload['system'] == SYSTEM
    assert h._caveman_state == 'original'
    assert 'outcome=error' in caplog.text


def test_handler_skips_non_caveman(tmp_path):
    backend = _Backend(GOOD_OPS)
    plain = _payload()
    plain['messages'][0]['content'] = 'hello'
    handlers._rewrite_caveman_system(_handler(), plain, _snapshot(tmp_path, backend), {}, {})
    assert backend.payloads == [] and plain['system'] == SYSTEM


def test_handler_skips_when_off(tmp_path):
    backend = _Backend(GOOD_OPS)
    snap = _snapshot(tmp_path, backend)
    snap.config.caveman_rewrite = 'off'
    payload = _payload()
    handlers._rewrite_caveman_system(_handler(), payload, snap, {}, {})
    assert backend.payloads == [] and payload['system'] == SYSTEM


def test_handler_sends_client_credentials_to_editor(tmp_path):
    seen = []

    class _CredBackend(_Backend):
        def send_classifier_message(self, payload, credentials, config):
            seen.append(credentials)
            return super().send_classifier_message(payload, credentials, config)

    creds = {'x-api-key': 'client-key'}
    handlers._rewrite_caveman_system(
        _handler(), _payload(), _snapshot(tmp_path, _CredBackend(GOOD_OPS)), creds, {})
    assert seen == [creds]


STYLE_MSG = (
    "CAVEMAN MODE ACTIVE — level: ultra\n\n"
    "# Output Style: Concise\n"
    "Answer simple questions in 1-3 sentences of plain prose.\n"
)
STYLE_OPS = [{'find': 'Answer simple questions in 1-3 sentences of plain prose.\n', 'replace': ''}]


def _claude_code_payload():
    # Claude Code: the output style and caveman header ride in an inline
    # role:system message of the opening turn; the system field has neither.
    return {
        'model': 'claude-opus-5-5',
        'system': [{'type': 'text', 'text': '# Harness\nUse tools.\n'}],
        'messages': [
            {'role': 'user', 'content': 'hi'},
            {'role': 'system', 'content': [{'type': 'text', 'text': STYLE_MSG}]},
        ],
    }


def test_opening_system_blocks_stop_at_first_assistant():
    payload = {'messages': [
        {'role': 'user', 'content': 'u'},
        {'role': 'system', 'content': 'first'},
        {'role': 'system', 'content': [{'type': 'image'}, {'type': 'text', 'text': 'second'}]},
        {'role': 'assistant', 'content': 'a'},
        {'role': 'system', 'content': 'later'},
    ]}
    blocks, where = cr.opening_system_blocks(payload)
    assert [b['text'] for b in blocks] == ['first', 'second']
    assert where == [(1, None), (2, 1)]
    cr.write_opening_system_blocks(payload, where, [{'text': 'F'}, {'text': 'S'}])
    assert payload['messages'][1]['content'] == 'F'
    assert payload['messages'][2]['content'] == [{'type': 'image'}, {'type': 'text', 'text': 'S'}]
    assert payload['messages'][4]['content'] == 'later'


def test_handler_rewrites_output_style_in_inline_system_message(tmp_path, caplog):
    backend = _Backend(STYLE_OPS)
    payload, h, capture = _claude_code_payload(), _handler(), {}
    with caplog.at_level('INFO'):
        handlers._rewrite_caveman_system(h, payload, _snapshot(tmp_path, backend), {}, capture)
    assert payload['messages'][1]['content'][0]['text'] == STYLE_MSG.replace(STYLE_OPS[0]['find'], '')
    assert payload['system'] == _claude_code_payload()['system']
    assert h._caveman_state == 'rewritten'
    assert len(backend.payloads) == 1  # system field has no comm section: no call
    assert 'target=messages outcome=rewritten' in caplog.text
    assert 'target=system outcome=no_guidance' in caplog.text
    assert 'system_prompt_caveman_sha256' not in capture

    # Next turn re-sends the original message: cache hit, same bytes, no call.
    again = _claude_code_payload()
    handlers._rewrite_caveman_system(_handler(), again, _snapshot(tmp_path, backend), {}, {})
    assert again['messages'][1] == payload['messages'][1]
    assert len(backend.payloads) == 1


def test_ruleset_sections_are_never_edit_targets():
    text = ("# ultracave\n\n## Rules\nFragments.\n\n## Output format\nPayload only.\n\n"
            "# Output Style: Concise\nProse is fine.\n")
    titles = [text[s:e].splitlines()[0] for s, e in cr.comm_sections(text)]
    assert titles == ['# Output Style: Concise']
    assert cr.apply_edits(text, [{'find': 'Payload only.', 'replace': 'x'}]) is None


def test_handler_rewrites_string_content_system_message_without_system_field(tmp_path):
    backend = _Backend(STYLE_OPS)
    payload = {'model': 'claude-opus-5-5', 'messages': [
        {'role': 'user', 'content': 'hi'},
        {'role': 'system', 'content': STYLE_MSG},
    ]}
    h = _handler()
    handlers._rewrite_caveman_system(h, payload, _snapshot(tmp_path, backend), {}, {})
    assert payload['messages'][1]['content'] == STYLE_MSG.replace(STYLE_OPS[0]['find'], '')
    assert 'system' not in payload and h._caveman_state == 'rewritten'


def test_handler_rewrites_both_targets_and_records_system_hash(tmp_path):
    payload = _payload()
    payload['messages'].append({'role': 'system', 'content': STYLE_MSG})
    h, capture = _handler(), {'system_prompt_sha256': 'orig'}
    handlers._rewrite_caveman_system(h, payload, _snapshot(tmp_path, _SplitBackend()), {}, capture)
    rewritten_prompt = PROMPT.replace(GOOD_OPS[0]['find'], '')
    assert payload['system'][1]['text'] == rewritten_prompt
    assert payload['messages'][-1]['content'] == STYLE_MSG.replace(STYLE_OPS[0]['find'], '')
    assert h._caveman_state == 'rewritten'
    expected = hashlib.sha256(json.dumps(payload['system'], sort_keys=True, ensure_ascii=False)
                              .encode('utf-8')).hexdigest()
    assert capture['system_prompt_caveman_sha256'] == expected
    assert capture['system_prompt_sha256'] == 'orig'


def test_nested_ruleset_section_inside_comm_section_is_never_edited():
    text = "# Output Style: Concise\nBe prose.\n## caveman\nDrop articles.\n"
    assert cr.apply_edits(text, [{'find': 'Drop articles.', 'replace': ''}]) is None
    assert cr.apply_edits(text, [{'find': 'Be prose.', 'replace': 'Be terse.'}])[0].startswith(
        "# Output Style: Concise\nBe terse.")


def test_write_opening_system_blocks_rejects_length_mismatch_before_writing():
    payload = {'messages': [{'role': 'system', 'content': 'a'}, {'role': 'system', 'content': 'b'}]}
    try:
        cr.write_opening_system_blocks(payload, [(0, None), (1, None)], [{'text': 'A'}])
    except ValueError:
        pass
    else:
        raise AssertionError('expected ValueError')
    assert [m['content'] for m in payload['messages']] == ['a', 'b']


def test_handler_messages_target_error_keeps_messages_original(tmp_path, caplog):
    payload, h = _claude_code_payload(), _handler()
    snap = _snapshot(tmp_path, _Backend(exc=RuntimeError('boom')))
    with caplog.at_level('INFO'):
        handlers._rewrite_caveman_system(h, payload, snap, {}, {})
    assert payload == _claude_code_payload()
    assert h._caveman_state == 'original'
    assert 'target=messages outcome=error' in caplog.text


def test_handler_skips_security_monitor_with_inline_system_message(tmp_path):
    backend = _Backend(STYLE_OPS)
    payload, h = _claude_code_payload(), _handler()
    payload['system'] = handlers.HAPPY_NEW_YEAR_PREFIX + ' Evaluate the action.'
    expected = json.loads(json.dumps(payload))
    handlers._rewrite_caveman_system(h, payload, _snapshot(tmp_path, backend), {}, {})
    assert backend.payloads == [] and payload == expected


def test_ratio_tracker_reports_other_state():
    t = cr.CacheRatioTracker()
    assert t.record('s', 'original', 100, 0, 0) == (0.0, None)
    ratio, other = t.record('s', 'rewritten', 10, 0, 90)
    assert ratio == 0.9 and other == 0.0


# --- review fixes: reject memory, busy, single flight, own hash column ----------

BAD_OPS = [{'find': 'Prefer dedicated tools.', 'replace': ''}]


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_reject_remembered_in_memory_until_ttl(tmp_path):
    clock, calls = _Clock(), []
    rw = cr.CavemanRewriter(cr.RewriteCache(tmp_path), reject_ttl=60, clock=clock)
    assert rw.rewrite(SYSTEM, 'ultra', _model(BAD_OPS, calls)).outcome == 'rejected'
    again = rw.rewrite(SYSTEM, 'ultra', _model(BAD_OPS, calls))
    assert again.outcome == 'rejected_cached' and again.system == SYSTEM
    assert len(calls) == 1
    assert list(tmp_path.iterdir()) == []
    clock.now = 61
    assert rw.rewrite(SYSTEM, 'ultra', _model(GOOD_OPS, calls)).outcome == 'rewritten'
    assert len(calls) == 2


def test_reject_memory_not_persisted_across_restart(tmp_path):
    cr.CavemanRewriter(cr.RewriteCache(tmp_path)).rewrite(SYSTEM, 'ultra', _model(BAD_OPS))
    result = cr.CavemanRewriter(cr.RewriteCache(tmp_path)).rewrite(SYSTEM, 'ultra', _model(GOOD_OPS))
    assert result.outcome == 'rewritten'


def test_missing_replace_key_rejected():
    find = GOOD_OPS[0]['find']
    assert cr.apply_edits(PROMPT, [{'find': find}]) is None


def test_busy_when_all_workers_hung(tmp_path):
    release = threading.Event()
    started = threading.Semaphore(0)

    def hung(system, user):
        started.release()
        release.wait(5)
        return '[]'

    rw = cr.CavemanRewriter(cr.RewriteCache(tmp_path), timeout=0.05)
    try:
        for n in range(cr._MAX_WORKERS):
            system = [{'type': 'text', 'text': PROMPT + f'\n# Tone {n}\nx\n'}]
            assert rw.rewrite(system, 'ultra', hung).outcome == 'timeout'
        calls = []
        result = rw.rewrite(SYSTEM, 'ultra', _model(GOOD_OPS, calls))
        assert result.outcome == 'busy' and result.system == SYSTEM and calls == []
    finally:
        release.set()
    # Slots come back once the hung calls return.
    deadline = time.monotonic() + 5
    while (result := rw.rewrite(SYSTEM, 'ultra', _model(GOOD_OPS))).outcome == 'busy':
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert result.outcome == 'rewritten'


def test_single_flight_one_model_call_same_bytes(tmp_path):
    gate, calls = threading.Event(), []

    def slow(system, user):
        calls.append(user)
        gate.wait(5)
        return json.dumps(GOOD_OPS)

    rw = cr.CavemanRewriter(cr.RewriteCache(tmp_path))
    results = []
    threads = [threading.Thread(target=lambda: results.append(rw.rewrite(SYSTEM, 'ultra', slow)))
               for _ in range(2)]
    for t in threads:
        t.start()
    while not calls:
        time.sleep(0.01)
    gate.set()
    for t in threads:
        t.join(5)
    assert len(calls) == 1
    assert sorted(r.outcome for r in results) == ['cache_hit', 'rewritten']
    assert results[0].system == results[1].system


def test_handler_records_own_hash_column(tmp_path):
    payload, h = _payload(), _handler()
    capture = {'system_prompt_sha256': 'orig', 'prompt_store_entries': {}}
    handlers._rewrite_caveman_system(h, payload, _snapshot(tmp_path, _Backend(GOOD_OPS)), {}, capture)
    assert capture.get('system_prompt_caveman_sha256')
    assert 'system_prompt_sanitized_sha256' not in capture


# --- editor version, file pruning, prose rate (Decisions 10-12) ---------------

def test_editor_prompt_covers_precedence_and_completeness():
    assert 'these rules win' in cr._EDITOR_SYSTEM
    assert 'in fragments' in cr._EDITOR_SYSTEM
    assert 'never delete it' in cr._EDITOR_SYSTEM


def test_editor_version_change_misses_cache(tmp_path, monkeypatch):
    cr.CavemanRewriter(cr.RewriteCache(tmp_path)).rewrite(SYSTEM, 'ultra', _model(GOOD_OPS))
    monkeypatch.setattr(cr, 'EDITOR_VERSION', 'changed00000')
    calls = []
    result = cr.CavemanRewriter(cr.RewriteCache(tmp_path)).rewrite(SYSTEM, 'ultra', _model(GOOD_OPS, calls))
    assert result.outcome == 'rewritten' and len(calls) == 1


def test_stale_cache_files_pruned_and_load_touches(tmp_path):
    import os
    cr.CavemanRewriter(cr.RewriteCache(tmp_path)).rewrite(SYSTEM, 'ultra', _model(GOOD_OPS))
    stale = tmp_path / ('0' * 64 + '.json')
    stale.write_text('{"system": "x"}', encoding='utf-8')
    old = time.time() - cr.CACHE_MAX_AGE_SECS - 60
    os.utime(stale, (old, old))
    live = tmp_path / (cr._system_key(SYSTEM, 'ultra') + '.json')
    os.utime(live, (old + 120, old + 120))
    cache = cr.RewriteCache(tmp_path)
    assert not stale.exists() and live.exists()
    hit, _ = cache.get(cr._system_key(SYSTEM, 'ultra'))
    assert hit and live.stat().st_mtime > time.time() - 60


def test_prose_rate_excludes_code():
    rate, words = cr.prose_rate('The fix is here. `the a an is` ```\nthe the the\n```')
    assert words == 4 and rate == 50.0
    assert cr.prose_rate('```only code```') is None


def test_prose_tracker_reports_other_state():
    t = cr.ProseRateTracker()
    assert t.record('s', 'original', 10.0) == (10.0, None)
    assert t.record('s', 'rewritten', 2.0) == (2.0, 10.0)
    assert t.record('s', 'rewritten', 4.0) == (3.0, 10.0)


def test_handler_prose_log_only_for_caveman(caplog):
    import logging
    h = SimpleNamespace(_caveman_state=None, _session_prefix='p', _log_tag=lambda: '[t]')
    with caplog.at_level(logging.INFO, logger='anthrouter.handlers'):
        handlers.ProxyRequestHandler._record_caveman_prose(h, 'The cat is here.')
        assert 'prose rate' not in caplog.text
        h._caveman_state = 'rewritten'
        handlers.ProxyRequestHandler._record_caveman_prose(h, 'The cat is here.')
    assert 'Caveman prose rate: rewritten=50.0/100w' in caplog.text
