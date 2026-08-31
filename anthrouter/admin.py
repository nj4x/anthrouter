"""Read-only admin REST API for the anthrouter observability UI.

One pure entry point called by the HTTP layer:

    status, body = admin.handle_get(path, query_params, db, config)

There is no ``handle_post``: with a single backend there is nothing to switch
or select, so every runtime control the anthproxy admin API carries is absent
by design.  Responses are JSON-serialisable dicts; the caller serialises them.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

MAX_LIMIT = 500
_DEFAULT_LIMIT = 50


def _err(code: int, error_code: str, message: str) -> tuple[int, dict]:
    return code, {'type': 'error', 'error': {'type': error_code, 'message': message}}


def _int_param(params: dict, key: str, default: int, max_val: int | None = None) -> int:
    try:
        value = int(params.get(key, default))
    except (TypeError, ValueError):
        return default
    if value < 0:
        return default
    return min(value, max_val) if max_val is not None else value


def _page(params: dict) -> tuple[int, int]:
    return (
        _int_param(params, 'limit', _DEFAULT_LIMIT, MAX_LIMIT),
        _int_param(params, 'offset', 0),
    )


def handle_get(path: str, query_params: dict, db, config) -> tuple[int, dict]:
    """Route one admin GET; never raises for a bad path or parameter."""
    if db is None:
        return _err(503, 'api_error', 'Request recording is disabled')

    route = path.rstrip('/')
    if route in ('/admin', '/admin/status'):
        return _get_status(db, config)
    if route == '/admin/requests':
        return _get_requests(query_params, db)
    if route.startswith('/admin/requests/'):
        raw = route[len('/admin/requests/'):]
        if not raw.isdigit():
            return _err(400, 'invalid_request_error', 'Request id must be an integer')
        return _get_request_detail(int(raw), db)
    if route == '/admin/routing':
        return _get_routing(query_params, db)
    if route == '/admin/sanitizer-events':
        return _get_sanitizer_events(query_params, db)
    if route == '/admin/ratelimit':
        return _get_ratelimit(db)
    if route.startswith('/admin/prompts/'):
        return _get_prompt(route[len('/admin/prompts/'):], db)
    return _err(404, 'not_found_error', f'Unknown admin endpoint: {path}')


def _get_status(db, config) -> tuple[int, dict]:
    return 200, {
        'upstream_base_url': config.upstream_base_url,
        'auto_model_routing': bool(config.auto_model_routing),
        'sanitize_system_prompt': config.sanitize_system_prompt,
        'lock_requested_model': config.lock_requested_model,
        'db_retention_days': config.db_retention_days,
        'stats': db.get_stats(),
        'ratelimit': db.get_latest_ratelimit(),
    }


def _get_requests(query_params: dict, db) -> tuple[int, dict]:
    limit, offset = _page(query_params)
    q = query_params.get('q') or None
    return 200, {
        'requests': db.get_requests(limit=limit, offset=offset, q=q),
        'limit': limit,
        'offset': offset,
        'q': q,
    }


def _get_request_detail(request_id: int, db) -> tuple[int, dict]:
    request = db.get_request(request_id)
    if request is None:
        return _err(404, 'not_found_error', f'No request with id {request_id}')
    return 200, {
        'request': request,
        'sanitizer_events': db.get_sanitizer_events(request_id),
    }


def _get_routing(query_params: dict, db) -> tuple[int, dict]:
    limit, offset = _page(query_params)
    return 200, {
        'decisions': db.get_routing_decisions(limit=limit, offset=offset),
        'summary': db.get_routing_summary(),
        'limit': limit,
        'offset': offset,
    }


def _get_sanitizer_events(query_params: dict, db) -> tuple[int, dict]:
    limit, offset = _page(query_params)
    return 200, {
        'events': db.get_recent_sanitizer_events(limit=limit, offset=offset),
        'summary': db.get_sanitizer_summary(),
        'limit': limit,
        'offset': offset,
    }


def _get_ratelimit(db) -> tuple[int, dict]:
    return 200, {'ratelimit': db.get_latest_ratelimit()}


def _get_prompt(content_hash: str, db) -> tuple[int, dict]:
    prompt = db.get_prompt(content_hash)
    if prompt is None:
        return _err(404, 'not_found_error', f'No stored prompt {content_hash}')
    return 200, prompt
