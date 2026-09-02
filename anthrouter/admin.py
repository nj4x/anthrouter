"""Read-only admin REST API for the anthrouter observability UI.

One pure entry point called by the HTTP layer:

    status, body = admin.handle_get(path, query_params, db, config)

There is no ``handle_post``: with a single backend there is nothing to switch
or select, so every runtime control the anthproxy admin API carries is absent
by design.  Responses are JSON-serialisable dicts; the caller serialises them.
"""

from __future__ import annotations

import logging
from pathlib import Path

from anthrouter.config import EDITABLE_FIELDS

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


def handle_get(path: str, query_params: dict, db, config, oauth_cache=None) -> tuple[int, dict]:
    """Route one admin GET; never raises for a bad path or parameter."""
    route = path.rstrip('/')
    if route == '/admin/config':
        return _get_config(config)

    if db is None:
        return _err(503, 'api_error', 'Request recording is disabled')

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
    if route == '/admin/oauth-usage':
        return _get_oauth_usage(query_params, oauth_cache)
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


def _read_config_env(anthrouter_home: str) -> dict[str, str]:
    """Read config.env and parse ``KEY=value`` / ``KEY="value"`` pairs.

    install.sh currently writes unquoted values (``ANTHROUTER_HOST=127.0.0.1``);
    ticket 03's POST handler writes the double-quoted form. Both are accepted so
    a GET works against a file either writer produced. Non-matching lines are
    skipped (fall back to in-memory Config). Returns empty dict if file absent.
    """
    config_env_path = Path(anthrouter_home) / 'config.env'
    result = {}
    if not config_env_path.exists():
        return result

    try:
        with open(config_env_path, 'r') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' not in line:
                    continue
                key, value = line.split('=', 1)
                key = key.strip()
                value = value.strip()
                if value.startswith('"') and value.endswith('"') and len(value) >= 2:
                    value = value[1:-1]
                result[key] = value
    except OSError as exc:
        # Falls back to in-memory Config values below; not a request-fatal error.
        logger.warning('failed to read %s: %s', config_env_path, exc)

    return result


def _get_config(config) -> tuple[int, dict]:
    """Return current config with restart-required metadata."""
    file_values = _read_config_env(config.anthrouter_home)

    fields = {}
    for field_name, restart_required in EDITABLE_FIELDS.items():
        if restart_required:
            env_key = 'ANTHROUTER_' + field_name.upper()
            if env_key in file_values:
                value = file_values[env_key]
            else:
                value = str(getattr(config, field_name, ''))
        else:
            value = str(getattr(config, field_name, ''))

        fields[field_name] = {
            'restart_required': restart_required,
            'value': value,
        }

    return 200, {
        'admin_token_configured': bool(getattr(config, 'admin_token', None)),
        'fields': fields,
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


def _get_oauth_usage(query_params: dict, oauth_cache) -> tuple[int, dict]:
    """Issue 3 Fix: Get cached OAuth usage with proper race condition handling.

    Uses public getter oauth_cache.get_usage() instead of accessing private _usage.
    Returns HTTP 202 Accepted with retry hint when cache is empty (first load or
    no-token queries), allowing the UI to poll until background fetch completes.
    """
    if oauth_cache is None:
        return _err(503, 'api_error', 'OAuth cache unavailable')

    # Try explicit token param first (for testing); fall back to last cached
    token = query_params.get('token', [''])[0] if isinstance(query_params.get('token'), list) else query_params.get('token', '')
    if token:
        # Explicit token requested — trigger a fetch and return what we have
        oauth_cache.get(token)
        usage = oauth_cache.get_usage()
    else:
        # Return the last cached usage (from any recent bearer auth request)
        usage = oauth_cache.get_usage()

    if usage is None:
        # Issue 3 Fix: Return 202 Accepted instead of 200 with None
        # This signals to the UI that data is being fetched and it should poll again
        return 202, {
            'oauth_token': None,
            'message': 'OAuth usage data is being fetched. Please retry in a few seconds.',
            'retry_after_seconds': 5,
        }

    return 200, {
        'oauth_token': {
            'burn_pct': usage.burn_pct,
            'used_usd': usage.used_usd,
            'total_usd': usage.total_usd,
            'month_elapsed_pct': usage.month_elapsed_pct,
            'monthly_blocked': usage.monthly_blocked,
            'eligible': usage.eligible,
            'cooldown_remaining_seconds': usage.cooldown_remaining_seconds,
            'usage_age_seconds': usage.usage_age_seconds,
            'usage_stale': usage.usage_stale,
        }
    }


def _get_prompt(content_hash: str, db) -> tuple[int, dict]:
    prompt = db.get_prompt(content_hash)
    if prompt is None:
        return _err(404, 'not_found_error', f'No stored prompt {content_hash}')
    return 200, prompt
