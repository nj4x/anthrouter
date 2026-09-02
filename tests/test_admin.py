import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from anthrouter import admin
from anthrouter.config import EDITABLE_FIELDS, Config
from anthrouter.db import RequestDB
from tests.conftest import SESSION, post


def get(server, path):
    with urllib.request.urlopen(server.base_url + path) as resp:
        return json.loads(resp.read().decode())


def get_raw(server, path):
    with urllib.request.urlopen(server.base_url + path) as resp:
        return resp.status, resp.headers, resp.read()


def get_status(server, path):
    try:
        with urllib.request.urlopen(server.base_url + path) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


class _Decision:
    """Minimal stand-in for ModelRoutingDecision as db.record_request reads it."""

    def __init__(self, **kwargs):
        defaults = {
            'requested_model': 'claude-sonnet-4-5-20250929',
            'routed_model': 'claude-haiku-4-5-20251001',
            'classification': 'trivial',
            'reason_code': 'classified',
            'estimated_input_tokens': 120,
            'applied': True,
            'classifier_model': 'claude-haiku-4-5-20251001',
            'classifier_summary_json': '{"task":"trivial"}',
            'classifier_raw_response': 'trivial',
            'classifier_format': 'label',
        }
        self.__dict__.update({**defaults, **kwargs})


@pytest.fixture
def db(tmp_path):
    store = RequestDB(str(tmp_path / 'admin.db'))
    yield store
    store.close()


def seed(store, **overrides):
    return store.record_request(
        session_id=overrides.pop('session_id', 'sess-1'),
        routing_decision=_Decision(**overrides.pop('decision', {})),
        stats_dict=overrides.pop('stats_dict', {
            'input_tokens': 100, 'output_tokens': 20, 'cache_read_tokens': 900,
        }),
        duration_ms=overrides.pop('duration_ms', 42),
        status=overrides.pop('status', 'success'),
        **overrides,
    )


# ---------------------------------------------------------------------------
# DB read surface
# ---------------------------------------------------------------------------

def test_routing_decisions_and_summary(db):
    seed(db)
    seed(db, decision={'classification': 'deep', 'applied': False,
                       'reason_code': 'model_not_eligible'})

    decisions = db.get_routing_decisions()
    assert [d['classification'] for d in decisions] == ['deep', 'trivial']

    summary = db.get_routing_summary()
    assert summary['total'] == 2
    assert summary['applied'] == 1
    assert summary['trivial'] == 1 and summary['deep'] == 1


def test_routing_summary_on_empty_db(db):
    summary = db.get_routing_summary()
    assert summary['total'] == 0
    assert summary['net_savings_usd'] is None


def test_sanitizer_event_feed_joins_the_request(db):
    request_id = seed(db, sanitizer_events=[
        {'block_type': 'cc_prompt_id', 'is_allowlisted': False,
         'payload_preview': 'cc_prompt_id: 0d1f...'},
        {'block_type': 'env_block', 'is_allowlisted': True},
    ])

    events = db.get_recent_sanitizer_events()
    assert len(events) == 2
    assert {e['block_type'] for e in events} == {'cc_prompt_id', 'env_block'}
    assert all(e['request_id'] == request_id for e in events)
    assert all(e['session_id'] == 'sess-1' for e in events)

    summary = db.get_sanitizer_summary()
    assert summary['total_events'] == 2
    assert summary['requests_with_events'] == 1
    assert summary['allowlisted'] == 1


def test_sanitizer_summary_counts_changed_requests(db):
    seed(db, system_prompt_sha256='aaa', system_prompt_sanitized_sha256='bbb')
    seed(db, system_prompt_sha256='ccc', system_prompt_sanitized_sha256='ccc')
    seed(db, system_prompt_sha256='ddd')

    # Equal hashes mean the sanitizer ran and matched nothing; NULL means it
    # never ran.  Neither is a change.
    assert db.get_sanitizer_summary()['requests_changed'] == 1


def test_latest_ratelimit_prefers_the_newest_row_with_a_window(db):
    seed(db, ratelimit={'requests_remaining': 10, 'reset_at': '2026-08-31T10:00:00Z'})
    seed(db, ratelimit={'requests_remaining': 4, 'reset_at': '2026-08-31T11:00:00Z'})
    seed(db)  # no headers at all — must not shadow the row above

    latest = db.get_latest_ratelimit()
    assert latest['ratelimit_requests_remaining'] == 4


def test_latest_ratelimit_is_none_without_any_window(db):
    seed(db)
    assert db.get_latest_ratelimit() is None


def test_stats_totals(db):
    seed(db)
    seed(db, status='error', stats_dict={}, error='502 — upstream_failure')

    stats = db.get_stats()
    assert stats['requests'] == 2
    assert stats['errors'] == 1
    assert stats['sessions'] == 1
    # Absent usage stays NULL, so it is not summed as zero-cost traffic.
    assert stats['input_tokens'] == 100


# ---------------------------------------------------------------------------
# handle_get routing
# ---------------------------------------------------------------------------

@pytest.fixture
def cfg():
    return Config(upstream_base_url='https://api.anthropic.com', enable_ui=True)


def test_handle_get_without_a_db_is_unavailable(cfg):
    status, body = admin.handle_get('/admin/requests', {}, None, cfg)
    assert status == 503


def test_handle_get_unknown_path_is_404(db, cfg):
    status, body = admin.handle_get('/admin/backends', {}, db, cfg)
    assert status == 404


def test_handle_get_status_reports_config_and_stats(db, cfg):
    seed(db)
    status, body = admin.handle_get('/admin', {}, db, cfg)
    assert status == 200
    assert body['upstream_base_url'] == 'https://api.anthropic.com'
    assert body['sanitize_system_prompt'] == 'strip'
    assert body['stats']['requests'] == 1


def test_request_detail_carries_its_sanitizer_events(db, cfg):
    request_id = seed(db, sanitizer_events=[
        {'block_type': 'cc_prompt_id', 'is_allowlisted': False},
    ])
    status, body = admin.handle_get(f'/admin/requests/{request_id}', {}, db, cfg)
    assert status == 200
    assert body['request']['id'] == request_id
    assert body['sanitizer_events'][0]['block_type'] == 'cc_prompt_id'


def test_request_detail_rejects_a_non_numeric_id(db, cfg):
    status, _ = admin.handle_get('/admin/requests/abc', {}, db, cfg)
    assert status == 400


def test_request_detail_404s_for_a_missing_row(db, cfg):
    status, _ = admin.handle_get('/admin/requests/999', {}, db, cfg)
    assert status == 404


def test_limit_is_clamped_and_garbage_falls_back_to_the_default(db, cfg):
    for _ in range(3):
        seed(db)
    _, clamped = admin.handle_get('/admin/requests', {'limit': '9999'}, db, cfg)
    assert clamped['limit'] == admin.MAX_LIMIT

    _, garbage = admin.handle_get('/admin/requests', {'limit': 'lots'}, db, cfg)
    assert garbage['limit'] == 50

    _, negative = admin.handle_get('/admin/requests', {'offset': '-5'}, db, cfg)
    assert negative['offset'] == 0


def test_requests_search_filters(db, cfg):
    seed(db, user_prompt_text='migrate the schema')
    seed(db, user_prompt_text='rename a variable')

    _, body = admin.handle_get('/admin/requests', {'q': 'migrate'}, db, cfg)
    assert len(body['requests']) == 1
    assert body['q'] == 'migrate'


def test_prompt_lookup_by_hash(db, cfg):
    seed(db, system_prompt_sha256='deadbeef',
         prompt_store_entries={'deadbeef': ('system', 'You are Claude.')})

    status, body = admin.handle_get('/admin/prompts/deadbeef', {}, db, cfg)
    assert status == 200
    assert body['content'] == 'You are Claude.'
    assert body['ref_count'] == 1

    missing, _ = admin.handle_get('/admin/prompts/nope', {}, db, cfg)
    assert missing == 404


# ---------------------------------------------------------------------------
# HTTP wiring
# ---------------------------------------------------------------------------

def test_admin_endpoints_are_404_when_ui_is_disabled(proxy):
    server = proxy(enable_ui=False)
    status, body = get_status(server, '/admin/requests')
    assert status == 404
    assert body['error']['message'] == 'Admin UI not enabled'


def test_admin_requests_reflects_a_dispatched_request(proxy):
    server = proxy(enable_ui=True, auto_model_routing=True)
    post(server, '/v1/messages', {
        'model': 'claude-sonnet-4-5-20250929',
        'messages': [{'role': 'user', 'content': 'fix a typo'}],
        'metadata': {'user_id': SESSION},
    })

    body = get(server, '/admin/requests')
    assert len(body['requests']) == 1
    assert body['requests'][0]['status'] == 'success'

    routing = get(server, '/admin/routing')
    assert routing['summary']['total'] == 1

    ratelimit = get(server, '/admin/ratelimit')['ratelimit']
    assert ratelimit['ratelimit_requests_remaining'] == 42


def test_admin_query_string_is_parsed(proxy):
    server = proxy(enable_ui=True)
    for _ in range(3):
        post(server, '/v1/messages', {
            'model': 'claude-sonnet-4-5-20250929',
            'messages': [{'role': 'user', 'content': 'hello'}],
            'metadata': {'user_id': SESSION},
        })

    body = get(server, '/admin/requests?limit=2&offset=1')
    assert len(body['requests']) == 2
    assert body['offset'] == 1


def test_health_is_served_without_the_ui(proxy):
    server = proxy(enable_ui=False)
    assert get(server, '/health') == {'status': 'ok'}


def test_unknown_get_path_is_404(proxy):
    server = proxy(enable_ui=True)
    status, _ = get_status(server, '/nope')
    assert status == 404


def test_ui_path_traversal_serves_the_bundle_not_the_source(proxy):
    server = proxy(enable_ui=True)
    # urllib would normalise the '..' away client-side, so send it raw.
    host, port = server.server_address[0], server.server_address[1]
    conn = http.client.HTTPConnection(host, port)
    conn.request('GET', '/ui/../config.py')
    content = conn.getresponse().read()
    conn.close()
    assert b'upstream_base_url' not in content


def test_ui_serves_the_built_bundle(proxy):
    server = proxy(enable_ui=True)
    status, headers, content = get_raw(server, '/ui/')
    assert status == 200
    assert headers['Content-Type'] == 'text/html'
    assert headers['Cache-Control'] == 'no-cache'
    assert b'<div id="root">' in content


# ---------------------------------------------------------------------------
# GET /admin/config tests
# ---------------------------------------------------------------------------

def test_get_config_returns_all_editable_fields(cfg):
    """GET /admin/config returns all fields from EDITABLE_FIELDS."""
    status, body = admin.handle_get('/admin/config', {}, None, cfg)
    assert status == 200
    assert 'fields' in body
    assert 'admin_token_configured' in body
    assert set(body['fields'].keys()) == set(EDITABLE_FIELDS.keys())


def test_get_config_field_structure(cfg):
    """Each field has restart_required and value."""
    _status, body = admin.handle_get('/admin/config', {}, None, cfg)
    for field_data in body['fields'].values():
        assert 'restart_required' in field_data
        assert 'value' in field_data
        assert isinstance(field_data['restart_required'], bool)
        assert isinstance(field_data['value'], str)


def test_get_config_restart_required_polarity(cfg):
    """restart_required polarity matches EDITABLE_FIELDS."""
    _status, body = admin.handle_get('/admin/config', {}, None, cfg)
    for field_name, field_data in body['fields'].items():
        assert field_data['restart_required'] == EDITABLE_FIELDS[field_name]


def test_get_config_admin_token_always_false_without_token(cfg):
    """admin_token_configured is false when Config.admin_token not set."""
    _status, body = admin.handle_get('/admin/config', {}, None, cfg)
    assert body['admin_token_configured'] is False


def test_get_config_admin_token_true_when_configured():
    """admin_token_configured is true when Config has admin_token."""
    cfg = Config(upstream_base_url='https://api.anthropic.com', enable_ui=True)
    cfg.admin_token = 'test-secret-token'
    _status, body = admin.handle_get('/admin/config', {}, None, cfg)
    assert body['admin_token_configured'] is True


def test_get_config_never_leaks_admin_token():
    """Admin token value is never included in response."""
    cfg = Config(upstream_base_url='https://api.anthropic.com', enable_ui=True)
    cfg.admin_token = 'super-secret-token'
    _status, body = admin.handle_get('/admin/config', {}, None, cfg)
    response_str = json.dumps(body)
    assert 'super-secret-token' not in response_str
    assert 'admin_token' not in body['fields']


def test_get_config_file_editable_fields_from_memory_by_default(cfg):
    """File-editable fields read from in-memory Config when file absent."""
    _status, body = admin.handle_get('/admin/config', {}, None, cfg)
    # host is file-editable
    assert body['fields']['host']['value'] == '127.0.0.1'
    assert body['fields']['host']['restart_required'] is True


def test_get_config_file_editable_fields_from_config_env(cfg, tmp_path):
    """File-editable fields read from config.env file when present (quoted form)."""
    anthrouter_home = str(tmp_path)
    cfg = Config(
        upstream_base_url='https://api.anthropic.com',
        enable_ui=True,
        anthrouter_home=anthrouter_home,
        host='127.0.0.1',
        port=8083,
    )
    config_env = Path(anthrouter_home) / 'config.env'
    config_env.write_text('ANTHROUTER_HOST="192.168.1.1"\n')

    _status, body = admin.handle_get('/admin/config', {}, None, cfg)
    assert body['fields']['host']['value'] == '192.168.1.1'
    assert body['fields']['host']['restart_required'] is True


def test_get_config_reads_unquoted_install_sh_format(cfg, tmp_path):
    """install.sh currently writes unquoted KEY=value lines; GET must parse those too."""
    anthrouter_home = str(tmp_path)
    cfg = Config(
        upstream_base_url='https://api.anthropic.com',
        enable_ui=True,
        anthrouter_home=anthrouter_home,
        host='127.0.0.1',
        port=8083,
    )
    config_env = Path(anthrouter_home) / 'config.env'
    config_env.write_text('ANTHROUTER_HOST=192.168.1.1\nANTHROUTER_PORT=9090\n')

    _status, body = admin.handle_get('/admin/config', {}, None, cfg)
    assert body['fields']['host']['value'] == '192.168.1.1'
    assert body['fields']['port']['value'] == '9090'


def test_get_config_fallback_to_memory_when_file_key_absent(cfg, tmp_path):
    """File-editable fields fall back to in-memory Config when key absent from file."""
    anthrouter_home = str(tmp_path)
    cfg = Config(
        upstream_base_url='https://api.anthropic.com',
        enable_ui=True,
        anthrouter_home=anthrouter_home,
        host='192.168.1.1',
        port=9999,
    )
    config_env = Path(anthrouter_home) / 'config.env'
    config_env.write_text('ANTHROUTER_PORT="8888"\n')  # HOST key absent

    _status, body = admin.handle_get('/admin/config', {}, None, cfg)
    assert body['fields']['host']['value'] == '192.168.1.1'  # fallback
    assert body['fields']['port']['value'] == '8888'  # from file


def test_get_config_file_parse_handles_comments_and_empty_lines(cfg, tmp_path):
    """config.env parser skips comments and empty lines."""
    anthrouter_home = str(tmp_path)
    cfg = Config(
        upstream_base_url='https://api.anthropic.com',
        enable_ui=True,
        anthrouter_home=anthrouter_home,
        host='127.0.0.1',
    )
    config_env = Path(anthrouter_home) / 'config.env'
    config_env.write_text('# Comment\n\nANTHROUTER_HOST="example.com"\n')

    _status, body = admin.handle_get('/admin/config', {}, None, cfg)
    assert body['fields']['host']['value'] == 'example.com'


def test_get_config_live_editable_fields_from_memory(cfg):
    """Live-editable fields always read from in-memory Config."""
    cfg = Config(
        upstream_base_url='https://api.anthropic.com',
        enable_ui=True,
        log_level='DEBUG',
    )
    _status, body = admin.handle_get('/admin/config', {}, None, cfg)
    assert body['fields']['log_level']['value'] == 'DEBUG'
    assert body['fields']['log_level']['restart_required'] is False
