import dataclasses
import http.client
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from anthrouter import admin
from anthrouter.config import EDITABLE_FIELDS, FIELD_METADATA, Config
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
    status, _ = admin.handle_get('/admin/requests', {}, None, cfg)
    assert status == 503


def test_handle_get_unknown_path_is_404(db, cfg):
    status, _ = admin.handle_get('/admin/backends', {}, db, cfg)
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


def post_status(server, path, payload, headers=None):
    request = urllib.request.Request(
        server.base_url + path,
        data=json.dumps(payload).encode(),
        headers={'Content-Type': 'application/json', **(headers or {})},
        method='POST',
    )
    try:
        with urllib.request.urlopen(request) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def test_post_admin_config_404_when_ui_disabled(proxy):
    server = proxy(enable_ui=False, admin_token='sekret')
    status, _body = post_status(server, '/admin/config', {}, {'X-Admin-Token': 'sekret'})
    assert status == 404


def test_post_admin_config_403_without_admin_token_configured(proxy):
    server = proxy(enable_ui=True)
    status, _body = post_status(server, '/admin/config', {}, {'X-Admin-Token': 'anything'})
    assert status == 403


def test_post_admin_config_403_with_wrong_token(proxy):
    server = proxy(enable_ui=True, admin_token='sekret')
    status, _body = post_status(server, '/admin/config', {}, {'X-Admin-Token': 'nope'})
    assert status == 403


def test_post_admin_config_full_round_trip(proxy, tmp_path):
    server = proxy(enable_ui=True, admin_token='sekret', anthrouter_home=str(tmp_path))

    current = get(server, '/admin/config')
    body = {name: field['value'] for name, field in current['fields'].items()}
    body['db_retention_days'] = '7'
    body['host'] = '10.0.0.42'

    status, resp = post_status(server, '/admin/config', body, {'X-Admin-Token': 'sekret'})
    assert status == 200
    assert resp == {'status': 'ok'}

    # Live-editable field applied immediately, visible to a later request on
    # the same running server (proves the self.__class__.config swap worked).
    updated = get(server, '/admin/config')
    assert updated['fields']['db_retention_days']['value'] == '7'

    # File-editable field written to config.env but not yet applied in memory.
    assert updated['fields']['host']['value'] == '10.0.0.42'
    content = (tmp_path / 'config.env').read_text()
    assert 'ANTHROUTER_HOST="10.0.0.42"' in content

    status_body = get(server, '/admin/status')
    assert status_body['db_retention_days'] == 7


def test_post_admin_config_400_surfaces_field_error(proxy, tmp_path):
    server = proxy(enable_ui=True, admin_token='sekret', anthrouter_home=str(tmp_path))
    current = get(server, '/admin/config')
    body = {name: field['value'] for name, field in current['fields'].items()}
    body['port'] = 'not-a-port'

    status, resp = post_status(server, '/admin/config', body, {'X-Admin-Token': 'sekret'})
    assert status == 400
    assert 'port' in resp['error']['message']


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


# ---------------------------------------------------------------------------
# GET /admin/config - FIELD_METADATA tests (ADR-0007)
# ---------------------------------------------------------------------------

def test_get_config_includes_field_order(cfg):
    """GET /admin/config includes field_order array."""
    _status, body = admin.handle_get('/admin/config', {}, None, cfg)
    assert 'field_order' in body
    assert isinstance(body['field_order'], list)
    assert len(body['field_order']) == len(EDITABLE_FIELDS)


def test_get_config_field_order_matches_metadata_keys(cfg):
    """field_order matches FIELD_METADATA insertion order."""
    _status, body = admin.handle_get('/admin/config', {}, None, cfg)
    assert body['field_order'] == list(FIELD_METADATA.keys())


def test_get_config_includes_field_metadata(cfg):
    """Each field includes metadata: description, type, group."""
    _status, body = admin.handle_get('/admin/config', {}, None, cfg)
    for field_name, field_data in body['fields'].items():
        assert 'description' in field_data, f'{field_name} missing description'
        assert 'type' in field_data, f'{field_name} missing type'
        assert 'group' in field_data, f'{field_name} missing group'


def test_get_config_enum_fields_have_enum_array(cfg):
    """Enum fields include enum array."""
    _status, body = admin.handle_get('/admin/config', {}, None, cfg)
    # log_level is an enum field
    assert 'enum' in body['fields']['log_level']
    assert body['fields']['log_level']['enum'] == ['DEBUG', 'INFO', 'WARNING', 'ERROR']
    # auto_model_routing_mode is an enum field
    assert 'enum' in body['fields']['auto_model_routing_mode']
    assert body['fields']['auto_model_routing_mode']['enum'] == ['classifier', 'rules']
    # host is not an enum field
    assert 'enum' not in body['fields']['host']


def test_get_config_numeric_fields_have_min_max(cfg):
    """Numeric fields include min/max where applicable."""
    _status, body = admin.handle_get('/admin/config', {}, None, cfg)
    # auto_model_routing_min_confidence has min/max
    assert 'min' in body['fields']['auto_model_routing_min_confidence']
    assert 'max' in body['fields']['auto_model_routing_min_confidence']
    assert body['fields']['auto_model_routing_min_confidence']['min'] == 0.0
    assert body['fields']['auto_model_routing_min_confidence']['max'] == 1.0
    # db_retention_days has min
    assert 'min' in body['fields']['db_retention_days']
    assert body['fields']['db_retention_days']['min'] == 0
    # host has no min/max
    assert 'min' not in body['fields']['host']
    assert 'max' not in body['fields']['host']


def test_get_config_groups_are_correct(cfg):
    """Fields are grouped by subsystem."""
    _status, body = admin.handle_get('/admin/config', {}, None, cfg)
    assert body['fields']['log_level']['group'] == 'Logging'
    assert body['fields']['upstream_base_url']['group'] == 'Upstream'
    assert body['fields']['auto_model_routing']['group'] == 'Model Routing'
    assert body['fields']['sanitize_system_prompt']['group'] == 'Prompt Sanitization'
    assert body['fields']['db_path']['group'] == 'Database'
    assert body['fields']['host']['group'] == 'Server'


# ---------------------------------------------------------------------------
# POST /admin/config tests
# ---------------------------------------------------------------------------

@pytest.fixture
def admin_cfg(tmp_path):
    return Config(
        upstream_base_url='https://api.anthropic.com',
        enable_ui=True,
        anthrouter_home=str(tmp_path),
        admin_token='sekret',
    )


def _full_body(cfg, overrides=None):
    """Build a valid full round-trip POST body from a Config instance."""
    body = {}
    for name in EDITABLE_FIELDS:
        value = getattr(cfg, name)
        if value is None:
            body[name] = ''
        elif isinstance(value, bool):
            body[name] = 'true' if value else 'false'
        elif isinstance(value, dict):
            body[name] = ','.join(f'{k}:{v}' for k, v in value.items())
        else:
            body[name] = str(value)
    if overrides:
        body.update(overrides)
    return body


def _setter(holder):
    def _set(cfg):
        holder['config'] = cfg
    return _set


def test_post_config_403_when_admin_token_unset(cfg):
    """admin_token unset (default) -> unconditional 403, regardless of header."""
    holder = {'config': cfg}
    status, _resp = admin.handle_post_config('anything', _full_body(cfg), cfg, _setter(holder))
    assert status == 403
    assert holder['config'] is cfg


def test_post_config_403_when_token_wrong(admin_cfg):
    holder = {'config': admin_cfg}
    status, _resp = admin.handle_post_config(
        'wrong-token', _full_body(admin_cfg), admin_cfg, _setter(holder))
    assert status == 403
    assert holder['config'] is admin_cfg


def test_post_config_403_when_header_missing(admin_cfg):
    holder = {'config': admin_cfg}
    status, _resp = admin.handle_post_config(
        None, _full_body(admin_cfg), admin_cfg, _setter(holder))
    assert status == 403


def test_post_config_400_unknown_key(admin_cfg):
    holder = {'config': admin_cfg}
    body = _full_body(admin_cfg, {'totally_unknown_field': 'x'})
    status, resp = admin.handle_post_config('sekret', body, admin_cfg, _setter(holder))
    assert status == 400
    assert 'totally_unknown_field' in resp['error']['message']
    assert holder['config'] is admin_cfg


def test_post_config_400_rejects_admin_token_key(admin_cfg):
    """admin_token is excluded from EDITABLE_FIELDS; submitting it is an unknown-key 400."""
    holder = {'config': admin_cfg}
    body = _full_body(admin_cfg, {'admin_token': 'new-token'})
    status, resp = admin.handle_post_config('sekret', body, admin_cfg, _setter(holder))
    assert status == 400
    assert 'admin_token' in resp['error']['message']


def test_post_config_400_missing_key(admin_cfg):
    holder = {'config': admin_cfg}
    body = _full_body(admin_cfg)
    del body['host']
    status, resp = admin.handle_post_config('sekret', body, admin_cfg, _setter(holder))
    assert status == 400
    assert 'host' in resp['error']['message']


def test_post_config_400_names_every_unknown_key(admin_cfg):
    holder = {'config': admin_cfg}
    body = _full_body(admin_cfg, {'bogus_one': 'x', 'bogus_two': 'y'})
    status, resp = admin.handle_post_config('sekret', body, admin_cfg, _setter(holder))
    assert status == 400
    assert 'bogus_one' in resp['error']['message']
    assert 'bogus_two' in resp['error']['message']
    assert len(resp['error']['errors']) == 2


def test_post_config_400_names_every_missing_key(admin_cfg):
    holder = {'config': admin_cfg}
    body = _full_body(admin_cfg)
    del body['host']
    del body['port']
    status, resp = admin.handle_post_config('sekret', body, admin_cfg, _setter(holder))
    assert status == 400
    assert 'host' in resp['error']['message']
    assert 'port' in resp['error']['message']
    assert len(resp['error']['errors']) == 2


@pytest.mark.parametrize('bad_char', ['"', '\\', '$', '`', '\n'])
def test_post_config_400_quoting_unsafe_value(admin_cfg, bad_char):
    holder = {'config': admin_cfg}
    body = _full_body(admin_cfg, {'lock_requested_model': f'evil{bad_char}value'})
    status, resp = admin.handle_post_config('sekret', body, admin_cfg, _setter(holder))
    assert status == 400
    assert 'lock_requested_model' in resp['error']['message']
    assert holder['config'] is admin_cfg


def test_post_config_int_field_skips_quoting_check(admin_cfg):
    """int/bool/float fields are exempt from the quoting pre-check."""
    holder = {'config': admin_cfg}
    body = _full_body(admin_cfg, {'port': '9090'})
    status, _resp = admin.handle_post_config('sekret', body, admin_cfg, _setter(holder))
    assert status == 200


def test_post_config_400_int_coercion_failure(admin_cfg):
    holder = {'config': admin_cfg}
    body = _full_body(admin_cfg, {'port': 'not-a-number'})
    status, resp = admin.handle_post_config('sekret', body, admin_cfg, _setter(holder))
    assert status == 400
    assert 'port' in resp['error']['message']


def test_post_config_400_float_coercion_failure(admin_cfg):
    holder = {'config': admin_cfg}
    body = _full_body(admin_cfg, {'sse_keepalive_interval': 'nope'})
    status, resp = admin.handle_post_config('sekret', body, admin_cfg, _setter(holder))
    assert status == 400
    assert 'sse_keepalive_interval' in resp['error']['message']


def test_post_config_400_bool_coercion_failure(admin_cfg):
    holder = {'config': admin_cfg}
    body = _full_body(admin_cfg, {'auto_model_routing': 'maybe'})
    status, resp = admin.handle_post_config('sekret', body, admin_cfg, _setter(holder))
    assert status == 400
    assert 'auto_model_routing' in resp['error']['message']


@pytest.mark.parametrize('raw,expected', [
    ('true', True), ('TRUE', True), ('1', True), ('yes', True),
    ('false', False), ('FALSE', False), ('0', False), ('no', False),
])
def test_post_config_bool_coercion_accepts_common_forms(admin_cfg, raw, expected):
    holder = {'config': admin_cfg}
    body = _full_body(admin_cfg, {'auto_model_routing': raw})
    status, _resp = admin.handle_post_config('sekret', body, admin_cfg, _setter(holder))
    assert status == 200
    assert holder['config'].auto_model_routing is expected


def test_post_config_optional_empty_string_maps_to_none(admin_cfg):
    holder = {'config': admin_cfg}
    body = _full_body(admin_cfg, {'db_path': ''})
    status, _resp = admin.handle_post_config('sekret', body, admin_cfg, _setter(holder))
    assert status == 200
    assert holder['config'].db_path is None


def test_post_config_400_validation_error_blocks_write_and_reload(admin_cfg):
    """Cross-field validation failure -> 400, no file write, config_setter not called."""
    holder = {'config': admin_cfg}
    body = _full_body(admin_cfg, {
        'auto_model_routing_system_prompt_weight': '0.5',
        'auto_model_routing_user_prompt_weight': '0.6',
    })
    status, _resp = admin.handle_post_config('sekret', body, admin_cfg, _setter(holder))
    assert status == 400
    assert holder['config'] is admin_cfg
    assert not (Path(admin_cfg.anthrouter_home) / 'config.env').exists()


def test_post_config_200_applies_live_editable_field_immediately(admin_cfg):
    holder = {'config': admin_cfg}
    body = _full_body(admin_cfg, {'db_retention_days': '99'})
    status, _resp = admin.handle_post_config('sekret', body, admin_cfg, _setter(holder))
    assert status == 200
    assert holder['config'].db_retention_days == 99


def test_post_config_200_file_editable_field_unchanged_in_memory(admin_cfg):
    """File-editable fields are written to config.env but kept at their old in-memory value."""
    holder = {'config': admin_cfg}
    body = _full_body(admin_cfg, {'host': '10.0.0.5'})
    status, _resp = admin.handle_post_config('sekret', body, admin_cfg, _setter(holder))
    assert status == 200
    assert holder['config'].host == admin_cfg.host  # unchanged in memory
    content = (Path(admin_cfg.anthrouter_home) / 'config.env').read_text()
    assert 'ANTHROUTER_HOST="10.0.0.5"' in content


def test_post_config_writes_all_submitted_keys_double_quoted(admin_cfg):
    holder = {'config': admin_cfg}
    body = _full_body(admin_cfg, {'port': '9999'})
    status, _resp = admin.handle_post_config('sekret', body, admin_cfg, _setter(holder))
    assert status == 200
    content = (Path(admin_cfg.anthrouter_home) / 'config.env').read_text()
    assert 'ANTHROUTER_PORT="9999"' in content


def test_post_config_merge_preserves_untouched_lines(admin_cfg):
    config_env = Path(admin_cfg.anthrouter_home) / 'config.env'
    config_env.write_text(
        '# install.sh header\n'
        'ANTHROUTER_HOST="127.0.0.1"\n'
        'ANTHROUTER_SOME_UNKNOWN_KEY="keep-me"\n'
    )
    holder = {'config': admin_cfg}
    body = _full_body(admin_cfg, {'host': '10.0.0.9'})
    status, _resp = admin.handle_post_config('sekret', body, admin_cfg, _setter(holder))
    assert status == 200
    content = config_env.read_text()
    assert '# install.sh header' in content
    assert 'ANTHROUTER_SOME_UNKNOWN_KEY="keep-me"' in content
    assert 'ANTHROUTER_HOST="10.0.0.9"' in content
    assert content.count('ANTHROUTER_HOST=') == 1


def test_post_config_merge_appends_keys_absent_from_file(admin_cfg):
    config_env = Path(admin_cfg.anthrouter_home) / 'config.env'
    config_env.write_text('ANTHROUTER_HOST="127.0.0.1"\n')
    holder = {'config': admin_cfg}
    body = _full_body(admin_cfg, {'port': '9191'})
    status, _resp = admin.handle_post_config('sekret', body, admin_cfg, _setter(holder))
    assert status == 200
    content = config_env.read_text()
    assert 'ANTHROUTER_PORT="9191"' in content
    assert 'ANTHROUTER_HOST="127.0.0.1"' in content


def test_post_config_creates_file_when_absent(admin_cfg):
    config_env = Path(admin_cfg.anthrouter_home) / 'config.env'
    assert not config_env.exists()
    holder = {'config': admin_cfg}
    status, _resp = admin.handle_post_config('sekret', _full_body(admin_cfg), admin_cfg, _setter(holder))
    assert status == 200
    assert config_env.exists()


def test_post_config_500_on_write_failure_leaves_config_untouched(admin_cfg):
    """anthrouter_home pointing at a non-existent directory -> write fails -> 5xx, no reload."""
    bad_cfg = dataclasses.replace(
        admin_cfg, anthrouter_home=str(Path(admin_cfg.anthrouter_home) / 'does' / 'not' / 'exist'))
    holder = {'config': bad_cfg}
    status, _resp = admin.handle_post_config(
        'sekret', _full_body(bad_cfg), bad_cfg, _setter(holder))
    assert status >= 500
    assert holder['config'] is bad_cfg


def test_post_config_concurrent_posts_are_serialized(admin_cfg):
    """Two concurrent POSTs never interleave their file writes or in-memory swap."""
    results = []
    barrier = threading.Barrier(2)

    def worker(retention_value):
        barrier.wait()
        holder = {'config': admin_cfg}
        body = _full_body(admin_cfg, {'db_retention_days': retention_value})
        status, _resp = admin.handle_post_config('sekret', body, admin_cfg, _setter(holder))
        results.append((status, holder['config']))

    threads = [threading.Thread(target=worker, args=(r,)) for r in ('11', '22')]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(status == 200 for status, _ in results)
    content = (Path(admin_cfg.anthrouter_home) / 'config.env').read_text()
    # Whichever write went last, the file has exactly one well-formed line per key.
    assert content.count('ANTHROUTER_DB_RETENTION_DAYS=') == 1
    written = content.split('ANTHROUTER_DB_RETENTION_DAYS="')[1].split('"')[0]
    assert written in ('11', '22')
    # db_retention_days is live-editable: each thread's own reload reflects its
    # own submission (last writer wins at the intent level, per ADR-0005).
    final_retentions = {cfg.db_retention_days for _, cfg in results}
    assert final_retentions == {11, 22}
