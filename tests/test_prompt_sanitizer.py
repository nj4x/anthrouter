"""System-prompt sanitizer: allowlist stripping and volatility detection (ADR-0029)."""

import hashlib
import json

from anthrouter.mapper.common import (
    VOLATILE_SYSTEM_BLOCK_PREFIXES,
    strip_volatile_system_blocks,
    system_content_str,
)
from anthrouter.prompt_volatility import (
    PromptVolatilityTracker,
    last_cache_control_index,
)
from anthrouter.sanitizer import sanitize_system_prompt

BILLING = (
    'x-anthropic-billing-header: cc_version=2.1.245.fe7; cc_entrypoint=cli; '
    'cch=ba498; cc_prompt_id=77cb8807-0000-4000-8000-000000000001;'
)
CC_PREFIX = "You are Claude Code, Anthropic's official CLI for Claude."


def _block(text, **extra):
    return {'type': 'text', 'text': text, **extra}


def _sha(system):
    return hashlib.sha256(
        json.dumps(system, sort_keys=True, ensure_ascii=False).encode('utf-8')
    ).hexdigest()


# ---------------------------------------------------------------------------
# strip_volatile_system_blocks
# ---------------------------------------------------------------------------

def test_allowlist_hit_is_stripped():
    system = [_block(BILLING), _block(CC_PREFIX)]
    result, stripped = strip_volatile_system_blocks(system)
    assert stripped is True
    assert result == [_block(CC_PREFIX)]


def test_volatile_but_not_allowlisted_passes_through():
    """A block that looks volatile but is not allowlisted is never dropped."""
    system = [
        _block('Current time is 2026-08-25T14:03:11Z and rising'),
        _block(CC_PREFIX),
    ]
    result, stripped = strip_volatile_system_blocks(system)
    assert stripped is False
    assert result is system


def test_stable_block_untouched():
    system = [_block(CC_PREFIX), _block('Be concise.')]
    result, stripped = strip_volatile_system_blocks(system)
    assert stripped is False
    assert result is system


def test_string_form_system_untouched():
    system = f'{BILLING}\n{CC_PREFIX}'
    result, stripped = strip_volatile_system_blocks(system)
    assert stripped is False
    assert result is system


def test_empty_and_missing_system_are_safe():
    for value in (None, [], '', {}):
        result, stripped = strip_volatile_system_blocks(value)
        assert stripped is False
        assert result is value


def test_non_dict_and_non_text_blocks_ignored():
    system = ['bare string', {'type': 'text'}, {'text': 123}, _block(BILLING)]
    result, stripped = strip_volatile_system_blocks(system)
    assert stripped is True
    assert result == ['bare string', {'type': 'text'}, {'text': 123}]


def test_leading_whitespace_still_matches():
    system = [_block(f'   {BILLING}'), _block(CC_PREFIX)]
    _, stripped = strip_volatile_system_blocks(system)
    assert stripped is True


def test_all_blocks_volatile_yields_empty_list():
    system = [_block(BILLING)]
    result, stripped = strip_volatile_system_blocks(system)
    assert stripped is True
    assert result == []


def test_cache_control_is_preserved_on_surviving_blocks():
    system = [
        _block(BILLING),
        _block(CC_PREFIX, cache_control={'type': 'ephemeral'}),
    ]
    result, _ = strip_volatile_system_blocks(system)
    assert result[0]['cache_control'] == {'type': 'ephemeral'}


def test_allowlist_is_a_tuple_of_prefixes():
    assert isinstance(VOLATILE_SYSTEM_BLOCK_PREFIXES, tuple)
    assert 'x-anthropic-billing-header:' in VOLATILE_SYSTEM_BLOCK_PREFIXES


# ---------------------------------------------------------------------------
# system_content_str
# ---------------------------------------------------------------------------

def test_system_content_str_shapes():
    assert system_content_str('plain') == 'plain'
    system = [_block(CC_PREFIX)]
    assert system_content_str(system) == json.dumps(
        system, sort_keys=True, ensure_ascii=False
    )
    assert system_content_str({'unexpected': 'shape'}) is None
    assert system_content_str(None) is None


# ---------------------------------------------------------------------------
# last_cache_control_index
# ---------------------------------------------------------------------------

def test_last_cache_control_index():
    assert last_cache_control_index([]) == -1
    assert last_cache_control_index('a string') == -1
    assert last_cache_control_index([_block('a'), _block('b')]) == -1
    system = [
        _block('a', cache_control={'type': 'ephemeral'}),
        _block('b'),
        _block('c', cache_control={'type': 'ephemeral'}),
        _block('d'),
    ]
    assert last_cache_control_index(system) == 2


# ---------------------------------------------------------------------------
# PromptVolatilityTracker
# ---------------------------------------------------------------------------

def _post_cutover_system(i):
    """Block 0 unique per request — the measured post-cutover shape (ratio 1.0)."""
    return [
        _block(f'x-anthropic-billing-header: cc_prompt_id=uuid-{i};'),
        _block(CC_PREFIX, cache_control={'type': 'ephemeral'}),
    ]


def _pre_cutover_system(i):
    """Block 0 stable across the session — the pre-cutover shape (ratio ~0)."""
    return [
        _block('x-anthropic-billing-header: cc_version=2.1.231.bd5;'),
        _block(CC_PREFIX, cache_control={'type': 'ephemeral'}),
    ]


def test_post_cutover_session_flags():
    tracker = PromptVolatilityTracker()
    flagged = []
    for i in range(20):
        flagged = tracker.observe('sess-a', _post_cutover_system(i))
    assert [f['index'] for f in flagged] == [0]
    assert flagged[0]['ratio'] == 1.0
    assert flagged[0]['requests'] == 20


def test_pre_cutover_session_does_not_flag():
    tracker = PromptVolatilityTracker()
    flagged = []
    for i in range(50):
        flagged = tracker.observe('sess-b', _pre_cutover_system(i))
    assert flagged == []


def test_below_minimum_sample_floor_never_flags():
    tracker = PromptVolatilityTracker(min_samples=8)
    for i in range(7):
        assert tracker.observe('sess-c', _post_cutover_system(i)) == []
    # The 8th request crosses the floor and the ratio is already 1.0.
    assert tracker.observe('sess-c', _post_cutover_system(7))


def test_volatile_block_after_last_breakpoint_does_not_flag():
    """Severity gate: variation outside the cached prefix costs nothing."""
    tracker = PromptVolatilityTracker()
    flagged = []
    for i in range(20):
        system = [
            _block(CC_PREFIX, cache_control={'type': 'ephemeral'}),
            _block(f'volatile tail {i}'),
        ]
        flagged = tracker.observe('sess-d', system)
    assert flagged == []


def test_system_without_any_breakpoint_does_not_flag():
    tracker = PromptVolatilityTracker()
    flagged = []
    for i in range(20):
        flagged = tracker.observe('sess-e', [_block(f'no breakpoint {i}')])
    assert flagged == []


def test_sessions_are_isolated():
    """Variation across sessions is harmless and must not flag either session."""
    tracker = PromptVolatilityTracker()
    flagged_a = flagged_b = []
    for i in range(20):
        flagged_a = tracker.observe('sess-f', _pre_cutover_system(i))
        flagged_b = tracker.observe('sess-g', [
            _block('x-anthropic-billing-header: cc_version=2.1.999.zzz;'),
            _block(CC_PREFIX, cache_control={'type': 'ephemeral'}),
        ])
    assert flagged_a == []
    assert flagged_b == []


def test_session_eviction_is_bounded():
    tracker = PromptVolatilityTracker(max_sessions=4)
    for i in range(10):
        tracker.observe(f'sess-{i}', _post_cutover_system(i))
    assert tracker.session_report('sess-0') is None
    assert tracker.session_report('sess-9') is not None


def test_session_report_and_forget():
    tracker = PromptVolatilityTracker()
    for i in range(10):
        tracker.observe('sess-h', _post_cutover_system(i))
    report = tracker.session_report('sess-h')
    assert report['requests'] == 10
    assert report['blocks'][0]['ratio'] == 1.0
    assert report['blocks'][1]['ratio'] == 0.1
    tracker.forget('sess-h')
    assert tracker.session_report('sess-h') is None


def test_observe_ignores_empty_input():
    tracker = PromptVolatilityTracker()
    assert tracker.observe('', _post_cutover_system(0)) == []
    assert tracker.observe('sess-i', None) == []
    assert tracker.observe('sess-i', []) == []
    assert tracker.observe('sess-i', 'a string') == []


def test_saturated_block_reports_capped_distinct_and_ratio():
    """Once a block hits _MAX_DISTINCT_PER_BLOCK, tracking stops growing but
    both observe() and session_report() must keep reporting a sane value
    instead of silently going wrong once the set is replaced by the sentinel.
    """
    from anthrouter.prompt_volatility import _MAX_DISTINCT_PER_BLOCK

    tracker = PromptVolatilityTracker(min_samples=1)
    flagged = []
    n = _MAX_DISTINCT_PER_BLOCK + 20
    for i in range(n):
        flagged = tracker.observe('sess-sat', [
            _block(f'x-anthropic-billing-header: cc_prompt_id={i};'),
            _block(CC_PREFIX, cache_control={'type': 'ephemeral'}),
        ])

    assert flagged[0]['index'] == 0
    assert flagged[0]['distinct'] == _MAX_DISTINCT_PER_BLOCK
    assert flagged[0]['requests'] == n
    assert flagged[0]['ratio'] == round(_MAX_DISTINCT_PER_BLOCK / n, 4)

    report = tracker.session_report('sess-sat')
    assert report['blocks'][0]['distinct'] == _MAX_DISTINCT_PER_BLOCK

    # A block that stopped varying after saturation still reports the capped
    # count, not a frozen-at-cap ratio that silently understates severity.
    more_flagged = tracker.observe('sess-sat', [
        _block('x-anthropic-billing-header: cc_prompt_id=repeat;'),
        _block(CC_PREFIX, cache_control={'type': 'ephemeral'}),
    ])
    assert more_flagged[0]['distinct'] == _MAX_DISTINCT_PER_BLOCK


# ---------------------------------------------------------------------------
# Integration: hashing behaviour the recording path depends on
# ---------------------------------------------------------------------------

def test_synthetic_session_collapses_to_one_sanitized_hash():
    """N requests differing only in block 0 -> N original hashes, 1 sanitized."""
    originals, sanitized = set(), set()
    for i in range(25):
        system = _post_cutover_system(i)
        originals.add(_sha(system))
        stripped_system, was_stripped = strip_volatile_system_blocks(system)
        assert was_stripped is True
        sanitized.add(_sha(stripped_system))

    assert len(originals) == 25
    assert len(sanitized) == 1


# ---------------------------------------------------------------------------
# Request-path seam: sanitize_system_prompt
# ---------------------------------------------------------------------------

def _run(mode, system=None, session='seam', tracker=None):
    system = system if system is not None else [_block(BILLING), _block(CC_PREFIX)]
    payload = {'system': system}
    original = _sha(system)
    result = sanitize_system_prompt(
        payload, mode, session, tracker=tracker or PromptVolatilityTracker(),
    )
    return payload, result, original


def test_strip_mode_removes_block_and_records_both_hashes():
    payload, result, original = _run('strip', session='seam-1')
    assert payload['system'] == [_block(CC_PREFIX)]
    assert result.ran is True
    assert result.dropped == 1
    assert result.sanitized_sha256 == _sha(payload['system'])
    assert result.sanitized_sha256 != original
    assert result.sanitized_content == json.dumps(
        payload['system'], sort_keys=True, ensure_ascii=False
    )


def test_warn_mode_does_not_mutate_payload():
    system = [_block(BILLING), _block(CC_PREFIX)]
    payload, result, _ = _run('warn', system=system, session='seam-2')
    assert payload['system'] == system
    assert result.ran is True
    assert result.sanitized_sha256 is None
    assert result.dropped == 0


def test_off_mode_is_inert():
    system = [_block(BILLING), _block(CC_PREFIX)]
    payload, result, _ = _run('off', system=system, session='seam-3')
    assert payload['system'] == system
    assert result.ran is False
    assert result.sanitized_sha256 is None


def test_unknown_mode_is_inert():
    system = [_block(BILLING), _block(CC_PREFIX)]
    payload, result, _ = _run('nonsense', system=system, session='seam-4')
    assert payload['system'] == system
    assert result.ran is False


def test_strip_mode_with_nothing_to_strip_still_records_sanitized_hash():
    """'Sanitizer ran, found nothing' must stay distinct from 'did not run'."""
    system = [_block(CC_PREFIX)]
    _, result, original = _run('strip', system=system, session='seam-5')
    assert result.sanitized_sha256 == original
    assert result.dropped == 0


def test_string_form_system_is_hashed_but_not_stripped():
    payload = {'system': f'{BILLING}\n{CC_PREFIX}'}
    original = payload['system']
    result = sanitize_system_prompt(payload, 'strip', 'seam-6')
    assert payload['system'] == original
    assert result.sanitized_sha256 == hashlib.sha256(
        original.encode('utf-8')
    ).hexdigest()


def test_missing_system_is_safe():
    payload = {'model': 'x'}
    result = sanitize_system_prompt(payload, 'strip', 'seam-7')
    assert result.ran is False
    assert payload == {'model': 'x'}


def test_sanitizer_never_raises_on_malformed_payload():
    result = sanitize_system_prompt(object(), 'strip', 'seam-8')
    assert result.ran is False


def test_flagged_blocks_are_reported_in_both_modes():
    tracker = PromptVolatilityTracker(min_samples=1)
    for mode in ('warn', 'strip'):
        result = sanitize_system_prompt(
            {'system': [
                _block(f'unrecognised volatile {mode}'),
                _block(CC_PREFIX, cache_control={'type': 'ephemeral'}),
            ]},
            mode, f'flag-{mode}', tracker=tracker,
        )
        assert 0 in [f['index'] for f in result.flagged]


def test_unrecognised_volatile_block_is_flagged_not_stripped():
    tracker = PromptVolatilityTracker(min_samples=1)
    system = [
        _block('Current time is 2026-08-25T14:03:11Z', cache_control={'type': 'ephemeral'}),
    ]
    payload = {'system': system}
    result = sanitize_system_prompt(payload, 'strip', 'flag-keep', tracker=tracker)
    assert payload['system'] == system
    assert result.dropped == 0
    assert [f['index'] for f in result.flagged] == [0]
