"""Trimmed SQLite layer: schema, recording, FTS search, ref-counting, retention."""

import dataclasses

import pytest

from anthrouter.db import RequestDB, compute_cache_savings, compute_cost


@dataclasses.dataclass
class FakeDecision:
    requested_model: str = 'sonnet'
    routed_model: str = 'haiku'
    classification: str | None = 'trivial'
    applied: bool = True
    reason_code: str = 'classifier_trivial'
    estimated_input_tokens: int = 1200
    classifier_model: str | None = 'haiku'
    classifier_summary_json: str | None = '{"user":"fix a typo"}'
    classifier_raw_response: str | None = 'trivial'
    classifier_format: str | None = 'standard'
    system_prompt_score: float | None = None
    user_prompt_score: float | None = None
    routing_weighted_score: float | None = None


STATS = {
    'input_tokens': 1000,
    'output_tokens': 200,
    'cache_creation_tokens': 0,
    'cache_read_tokens': 5000,
}


@pytest.fixture
def db(tmp_path):
    store = RequestDB(str(tmp_path / 'anthrouter.db'))
    yield store
    store.close()


def _record(store, **kwargs):
    params = {
        'session_id': 'sess-1',
        'routing_decision': FakeDecision(),
        'stats_dict': dict(STATS),
        'duration_ms': 900,
        'status': 'success',
    }
    params.update(kwargs)
    return store.record_request(**params)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def test_schema_version_is_three(db):
    assert db._conn.execute('PRAGMA user_version').fetchone()[0] == 3


def test_only_the_three_tables_exist(db):
    rows = db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "AND name NOT LIKE 'requests_fts%'"
    ).fetchall()
    assert {r[0] for r in rows} == {'requests', 'prompt_store', 'sanitizer_events'}


def test_reopening_an_existing_db_is_a_no_op(tmp_path):
    path = str(tmp_path / 'x.db')
    first = RequestDB(path)
    _record(first)
    first.close()
    second = RequestDB(path)
    assert len(second.get_requests()) == 1
    second.close()


def test_status_check_constraint_rejects_unknown_status(db):
    with pytest.raises(Exception):
        _record(db, status='weird')


# ---------------------------------------------------------------------------
# record_request
# ---------------------------------------------------------------------------

def test_record_returns_rowid_and_stores_decision(db):
    request_id = _record(db)
    row = db.get_request(request_id)
    assert row['requested_model'] == 'sonnet'
    assert row['routed_model'] == 'haiku'
    assert row['classification'] == 'trivial'
    assert row['reason_code'] == 'classifier_trivial'
    assert row['model_tier'] == 'haiku'
    assert row['applied'] == 1
    assert row['classifier_summary_json'] == '{"user":"fix a typo"}'


def test_missing_decision_records_nothing(db):
    assert _record(db, routing_decision=None) == -1
    assert db.get_requests() == []


def test_empty_stats_record_absent_usage_not_zero(db):
    row = db.get_request(_record(db, stats_dict={}))
    assert row['input_tokens'] is None
    assert row['output_tokens'] is None
    assert row['cost_estimate'] is None


def test_net_savings_is_null_when_routing_was_not_applied(db):
    decision = FakeDecision(applied=False, routed_model='sonnet')
    row = db.get_request(
        _record(db, routing_decision=decision, net_savings_usd=0.5,
                classifier_overhead_usd=0.01)
    )
    assert row['applied'] == 0
    assert row['net_savings_usd'] is None


def test_classifier_overhead_is_kept_when_routing_was_not_applied(db):
    """A classifier that ran and confirmed the requested model still cost money."""
    decision = FakeDecision(applied=False, routed_model='sonnet')
    row = db.get_request(
        _record(db, routing_decision=decision, net_savings_usd=0.5,
                classifier_overhead_usd=0.01)
    )
    assert row['applied'] == 0
    assert row['classifier_overhead_usd'] == 0.01


def test_economics_are_kept_when_routing_was_applied(db):
    row = db.get_request(
        _record(db, net_savings_usd=0.5, classifier_overhead_usd=0.01)
    )
    assert row['net_savings_usd'] == 0.5
    assert row['classifier_overhead_usd'] == 0.01


def test_ratelimit_headers_are_stored_as_received(db):
    row = db.get_request(_record(db, ratelimit={
        'requests_remaining': 12,
        'tokens_remaining': 340000,
        'input_tokens_remaining': 200000,
        'output_tokens_remaining': 140000,
        'reset_at': '2026-08-31T12:00:00Z',
    }))
    assert row['ratelimit_requests_remaining'] == 12
    assert row['ratelimit_reset_at'] == '2026-08-31T12:00:00Z'


def test_absent_ratelimit_headers_are_null(db):
    row = db.get_request(_record(db))
    assert row['ratelimit_tokens_remaining'] is None


def test_both_prompt_hashes_are_kept_separately(db):
    row = db.get_request(_record(
        db, system_prompt_sha256='aaa', system_prompt_sanitized_sha256='bbb'))
    assert row['system_prompt_sha256'] == 'aaa'
    assert row['system_prompt_sanitized_sha256'] == 'bbb'


# ---------------------------------------------------------------------------
# get_request: prompt_store content JOIN
# ---------------------------------------------------------------------------

def test_get_request_joins_system_tools_and_sanitized_content(db):
    entries = {
        'aaa': ('system', 'original system prompt'),
        'bbb': ('system_sanitized', 'sanitized system prompt'),
        'ccc': ('tools', '[{"name": "bash"}]'),
    }
    row = db.get_request(_record(
        db,
        system_prompt_sha256='aaa',
        system_prompt_sanitized_sha256='bbb',
        tools_sha256='ccc',
        prompt_store_entries=entries,
    ))
    assert row['system_prompt_content'] == 'original system prompt'
    assert row['system_prompt_sanitized_content'] == 'sanitized system prompt'
    assert row['tools_content'] == '[{"name": "bash"}]'


def test_get_request_with_no_tools_still_returns_row_with_null_tools_content(db):
    row = db.get_request(_record(
        db,
        system_prompt_sha256='aaa',
        prompt_store_entries={'aaa': ('system', 'a system prompt')},
    ))
    assert row is not None
    assert row['tools_content'] is None
    assert row['system_prompt_content'] == 'a system prompt'


def test_get_request_with_no_sanitized_hash_has_null_sanitized_content(db):
    row = db.get_request(_record(
        db,
        system_prompt_sha256='aaa',
        prompt_store_entries={'aaa': ('system', 'a system prompt')},
    ))
    assert row['system_prompt_sanitized_sha256'] is None
    assert row['system_prompt_sanitized_content'] is None


def test_get_request_non_null_hash_with_no_prompt_store_row_returns_null_content(db):
    row = db.get_request(_record(
        db,
        system_prompt_sha256='missing-hash',
        tools_sha256='also-missing',
    ))
    assert row is not None
    assert row['system_prompt_content'] is None
    assert row['tools_content'] is None


def test_get_request_column_names_unchanged_for_existing_callers(db):
    row = db.get_request(_record(db))
    assert row['requested_model'] == 'sonnet'
    assert row['routed_model'] == 'haiku'
    assert row['id'] is not None


# ---------------------------------------------------------------------------
# prompt_store and ref-counting
# ---------------------------------------------------------------------------

def test_prompt_store_dedups_and_counts_references(db):
    entries = {'aaa': ('system', 'you are a proxy')}
    _record(db, system_prompt_sha256='aaa', prompt_store_entries=entries)
    _record(db, system_prompt_sha256='aaa', prompt_store_entries=entries)
    stored = db.get_prompt('aaa')
    assert stored['content'] == 'you are a proxy'
    assert stored['char_count'] == len('you are a proxy')
    assert stored['ref_count'] == 2


def test_one_row_referenced_by_two_columns_counts_once(db):
    _record(
        db,
        system_prompt_sha256='aaa',
        system_prompt_sanitized_sha256='aaa',
        prompt_store_entries={'aaa': ('system', 'unchanged by the sanitizer')},
    )
    assert db.get_prompt('aaa')['ref_count'] == 1


def test_distinct_hashes_each_count_once(db):
    _record(
        db,
        system_prompt_sha256='aaa',
        system_prompt_sanitized_sha256='bbb',
        tools_sha256='ccc',
        prompt_store_entries={
            'aaa': ('system', 'original'),
            'bbb': ('system_sanitized', 'stripped'),
            'ccc': ('tools', '[]'),
        },
    )
    assert db.get_prompt('aaa')['ref_count'] == 1
    assert db.get_prompt('bbb')['ref_count'] == 1
    assert db.get_prompt('ccc')['ref_count'] == 1


def test_deleting_a_request_decrements_the_reference(db):
    entries = {'aaa': ('system', 'you are a proxy')}
    first = _record(db, system_prompt_sha256='aaa', prompt_store_entries=entries)
    _record(db, system_prompt_sha256='aaa', prompt_store_entries=entries)
    with db._conn:
        db._conn.execute('DELETE FROM requests WHERE id = ?', (first,))
    assert db.get_prompt('aaa')['ref_count'] == 1


def test_unknown_hash_reads_as_missing(db):
    assert db.get_prompt('nope') is None


# ---------------------------------------------------------------------------
# sanitizer_events
# ---------------------------------------------------------------------------

def test_sanitizer_events_are_recorded_per_request(db):
    request_id = _record(db, sanitizer_events=[
        {'block_type': 'x-anthropic-billing-header', 'is_allowlisted': True,
         'payload_preview': 'cc_prompt_id=77cb'},
        {'block_type': 'block-index-3', 'is_allowlisted': False,
         'payload_preview': 'timestamp: ...'},
    ])
    events = db.get_sanitizer_events(request_id)
    assert [e['block_type'] for e in events] == [
        'x-anthropic-billing-header', 'block-index-3']
    assert [e['is_allowlisted'] for e in events] == [1, 0]


def test_duplicate_block_types_collapse_within_one_request(db):
    request_id = _record(db, sanitizer_events=[
        {'block_type': 'x-anthropic-billing-header', 'is_allowlisted': True},
        {'block_type': 'x-anthropic-billing-header', 'is_allowlisted': True},
    ])
    assert len(db.get_sanitizer_events(request_id)) == 1


def test_events_cascade_when_their_request_is_deleted(db):
    request_id = _record(db, sanitizer_events=[
        {'block_type': 'x-anthropic-billing-header', 'is_allowlisted': True},
    ])
    with db._conn:
        db._conn.execute('DELETE FROM requests WHERE id = ?', (request_id,))
    assert db.get_sanitizer_events(request_id) == []


# ---------------------------------------------------------------------------
# Reads and FTS
# ---------------------------------------------------------------------------

def test_requests_are_returned_newest_first(db):
    first = _record(db, user_prompt_text='first')
    second = _record(db, user_prompt_text='second')
    assert [r['id'] for r in db.get_requests()] == [second, first]


def test_search_matches_a_substring_inside_a_word(db):
    match = _record(db, user_prompt_text='refactor the classifier payload')
    _record(db, user_prompt_text='something else entirely')
    assert [r['id'] for r in db.get_requests(q='ssifi')] == [match]


def test_search_covers_response_text(db):
    match = _record(db, response_text='the migration applies cleanly')
    _record(db, response_text='unrelated')
    assert [r['id'] for r in db.get_requests(q='applies')] == [match]


def test_search_is_case_insensitive(db):
    match = _record(db, user_prompt_text='Refactor The Classifier')
    assert [r['id'] for r in db.get_requests(q='classifier')] == [match]


def test_short_query_falls_back_to_like(db):
    match = _record(db, user_prompt_text='ab initio')
    _record(db, user_prompt_text='zz')
    assert [r['id'] for r in db.get_requests(q='ab')] == [match]


def test_search_treats_fts_syntax_as_literal_text(db):
    match = _record(db, user_prompt_text='cost AND savings')
    assert [r['id'] for r in db.get_requests(q='AND savings')] == [match]


def test_deleted_rows_leave_the_search_index(db):
    request_id = _record(db, user_prompt_text='refactor the classifier')
    with db._conn:
        db._conn.execute('DELETE FROM requests WHERE id = ?', (request_id,))
    assert db.get_requests(q='classifier') == []


def test_get_request_returns_none_for_unknown_id(db):
    assert db.get_request(999) is None


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------

def _age_request(db, request_id, days):
    with db._conn:
        db._conn.execute(
            "UPDATE requests SET request_ts = "
            "strftime('%Y-%m-%dT%H:%M:%fZ','now', ?) WHERE id = ?",
            (f'-{days} days', request_id),
        )


def test_retention_deletes_rows_past_the_window(db):
    old = _record(db, user_prompt_text='old')
    fresh = _record(db, user_prompt_text='fresh')
    _age_request(db, old, 45)
    assert db.run_retention() == 1
    assert [r['id'] for r in db.get_requests()] == [fresh]


def test_retention_drops_unreferenced_prompts_only(db):
    old = _record(db, system_prompt_sha256='aaa',
                  prompt_store_entries={'aaa': ('system', 'old prompt')})
    _record(db, system_prompt_sha256='bbb',
            prompt_store_entries={'bbb': ('system', 'live prompt')})
    _age_request(db, old, 45)
    db.run_retention()
    assert db.get_prompt('aaa') is None
    assert db.get_prompt('bbb') is not None


def test_retention_deletes_in_chunks(db):
    for i in range(2100):
        _age_request(db, _record(db, user_prompt_text=f'r{i}'), 45)
    assert db.run_retention() == 2100
    assert db.get_requests() == []


def test_zero_retention_days_keeps_everything(tmp_path):
    store = RequestDB(str(tmp_path / 'keep.db'), retention_days=0)
    old = _record(store, user_prompt_text='old')
    _age_request(store, old, 4000)
    store._maybe_run_retention()
    assert len(store.get_requests()) == 1
    store.close()


def test_retention_runs_at_most_once_per_day(db):
    old = _record(db, user_prompt_text='old')  # first insert consumes the pass
    _age_request(db, old, 45)
    second = _record(db, user_prompt_text='second')
    assert len(db.get_requests()) == 2

    db._last_retention_at = 0.0
    db._maybe_run_retention()
    assert [r['id'] for r in db.get_requests()] == [second]


# ---------------------------------------------------------------------------
# Cost helpers
# ---------------------------------------------------------------------------

def test_cost_uses_the_tier_price():
    cost = compute_cost('claude-haiku-4-5-20251001', {
        'input_tokens': 1_000_000, 'output_tokens': 0,
        'cache_read_tokens': 0, 'cache_creation_tokens': 0,
    })
    assert cost == pytest.approx(1.0)


def test_cost_is_none_for_an_unpriced_model():
    assert compute_cost('some-other-model', STATS) is None


def test_cache_savings_is_the_avoided_input_price():
    assert compute_cache_savings('haiku', 1_000_000) == pytest.approx(0.9)


def test_cache_savings_is_none_without_cache_reads():
    assert compute_cache_savings('haiku', 0) is None
