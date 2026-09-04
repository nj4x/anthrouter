"""Admin REST API for the anthrouter observability UI.

Two entry points called by the HTTP layer:

    status, body = admin.handle_get(path, query_params, db, config)
    status, body = admin.handle_post_config(admin_token, body, config, config_setter)

Every GET is read-only by design.  ``POST /admin/config`` is the sole,
deliberate exception (see ADR-0005): with a single backend there is nothing
else to switch or select, so no other runtime control is exposed.  Responses
are JSON-serialisable dicts; the caller serialises them.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import tempfile
import threading
import types
import typing
from pathlib import Path

from anthrouter.config import EDITABLE_FIELDS, FIELD_METADATA, Config, validate_config, _parse_classification_str, _parse_model_aliases_str

logger = logging.getLogger(__name__)

MAX_LIMIT = 500
_DEFAULT_LIMIT = 50

# Guards the read-merge-write of config.env and the in-memory config swap as a
# single critical section, so two concurrent POSTs can't interleave their file
# writes (ADR-0005 concurrency section).
_config_write_lock = threading.Lock()

# The config.env writer quotes every value KEY="value"; these characters would
# break that quoting or trigger shell expansion/substitution at source time.
_UNSAFE_QUOTE_CHARS = ('"', '\\', '$', '`', '\n')


def _err(code: int, error_code: str, message: str) -> tuple[int, dict]:
    return code, {'type': 'error', 'error': {'type': error_code, 'message': message}}


def _err_list(code: int, error_code: str, messages: list[str]) -> tuple[int, dict]:
    return code, {
        'type': 'error',
        'error': {'type': error_code, 'message': '; '.join(messages), 'errors': messages},
    }


def _analyze_field_type(field_type) -> tuple[str, bool]:
    """Return (kind, optional) for a ``dataclasses.fields(Config)`` type.

    kind is one of 'bool', 'int', 'float', 'str'.  optional is True for an
    ``X | None`` annotation (only ``db_path`` today).
    """
    optional = False
    origin = typing.get_origin(field_type)
    if origin is typing.Union or origin is types.UnionType:
        args = [a for a in typing.get_args(field_type) if a is not type(None)]
        if len(args) == 1:
            field_type = args[0]
            optional = True
    if field_type is bool:
        return 'bool', optional
    if field_type is int:
        return 'int', optional
    if field_type is float:
        return 'float', optional
    return 'str', optional


_FIELD_KIND: dict[str, tuple[str, bool]] = {
    f.name: _analyze_field_type(f.type) for f in dataclasses.fields(Config)
}


def _quoting_error(name: str, raw_value: str) -> str | None:
    for ch in _UNSAFE_QUOTE_CHARS:
        if ch in raw_value:
            return f'field {name!r} contains disallowed character {ch!r}'
    return None


def _coerce_field(name: str, raw_value: str, kind: str) -> tuple[object, str | None]:
    if kind == 'bool':
        low = raw_value.strip().lower()
        if low in ('true', '1', 'yes'):
            return True, None
        if low in ('false', '0', 'no'):
            return False, None
        return None, f'field {name!r} must be a boolean (true/false/1/0/yes/no), got {raw_value!r}'
    if kind == 'int':
        try:
            return int(raw_value), None
        except ValueError:
            return None, f'field {name!r} must be an integer, got {raw_value!r}'
    if kind == 'float':
        try:
            return float(raw_value), None
        except ValueError:
            return None, f'field {name!r} must be a number, got {raw_value!r}'
    return raw_value, None


def _write_config_env(config_env_path: Path, submitted: dict[str, str]) -> None:
    """Merge-write ``submitted`` into config.env, atomically.

    Replaces each submitted key's line in place (or appends it if absent);
    every other line — including install.sh's header comments — is left
    untouched.  Written via a sibling temp file + ``os.replace()``, which is
    atomic on POSIX: a concurrent GET sees either the whole old file or the
    whole new one, never a partial write.
    """
    env_map = {f'ANTHROUTER_{name.upper()}': value for name, value in submitted.items()}
    remaining = dict(env_map)

    lines: list[str] = []
    if config_env_path.exists():
        with open(config_env_path, 'r') as f:
            for raw_line in f:
                line = raw_line.rstrip('\n')
                stripped = line.strip()
                key = None
                if '=' in stripped and not stripped.startswith('#'):
                    candidate_key = stripped.split('=', 1)[0].strip()
                    if candidate_key in remaining:
                        key = candidate_key
                if key is not None:
                    lines.append(f'{key}="{remaining.pop(key)}"')
                else:
                    lines.append(line)

    for key, value in remaining.items():
        lines.append(f'{key}="{value}"')

    content = '\n'.join(lines)
    if content:
        content += '\n'

    fd, tmp_name = tempfile.mkstemp(
        dir=str(config_env_path.parent), prefix=f'.{config_env_path.name}.')
    try:
        with os.fdopen(fd, 'w') as tmp_f:
            tmp_f.write(content)
        os.replace(tmp_name, config_env_path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def handle_post_config(
    admin_token: str | None, body, config: Config, config_setter,
) -> tuple[int, dict]:
    """Validate, persist, and (for live-editable fields) apply a config edit.

    ``config_setter`` is called with the reloaded ``Config`` instance while
    ``_config_write_lock`` is still held, so the file write and the in-memory
    swap are serialized as one unit against a second concurrent POST.
    """
    if not config.admin_token or admin_token != config.admin_token:
        return _err(403, 'permission_error', 'Invalid or missing admin token')

    if not isinstance(body, dict):
        return _err(400, 'invalid_request_error', 'Request body must be a JSON object')

    unknown = sorted(k for k in body if k not in EDITABLE_FIELDS)
    if unknown:
        return _err_list(400, 'invalid_request_error',
                         [f'Unknown config field: {k!r}' for k in unknown])

    missing = sorted(k for k in EDITABLE_FIELDS if k not in body)
    if missing:
        return _err_list(400, 'invalid_request_error',
                         [f'Missing required config field: {k!r}' for k in missing])

    field_errors: list[str] = []
    coerced: dict[str, object] = {}
    for name, raw_value in body.items():
        if not isinstance(raw_value, str):
            field_errors.append(f'field {name!r} must be a string')
            continue
        kind, optional = _FIELD_KIND[name]
        if kind == 'str':
            err = _quoting_error(name, raw_value)
            if err:
                field_errors.append(err)
                continue
            coerced[name] = None if (optional and raw_value == '') else raw_value
        else:
            value, err = _coerce_field(name, raw_value, kind)
            if err:
                field_errors.append(err)
            else:
                coerced[name] = value

    if field_errors:
        return _err_list(400, 'invalid_request_error', field_errors)

    # Special-case: parse dict fields from string format before validation/replace
    if 'auto_model_routing_classification' in coerced and isinstance(coerced['auto_model_routing_classification'], str):
        from argparse import ArgumentParser
        _dummy_parser = ArgumentParser()
        coerced['auto_model_routing_classification'] = _parse_classification_str(coerced['auto_model_routing_classification'], _dummy_parser)
    if 'model_aliases' in coerced and isinstance(coerced['model_aliases'], str):
        from argparse import ArgumentParser
        _dummy_parser = ArgumentParser()
        coerced['model_aliases'] = _parse_model_aliases_str(coerced['model_aliases'], _dummy_parser)

    with _config_write_lock:
        full_candidate = dataclasses.replace(config, **coerced)
        validation_errors = validate_config(full_candidate)
        if validation_errors:
            return _err_list(400, 'invalid_request_error', validation_errors)

        config_env_path = Path(config.anthrouter_home) / 'config.env'
        try:
            _write_config_env(config_env_path, body)
        except OSError as exc:
            logger.error('failed to write %s: %s', config_env_path, exc)
            return _err(500, 'api_error', 'Failed to persist configuration')

        swap_overrides = {k: v for k, v in coerced.items() if not EDITABLE_FIELDS[k]}
        swap_candidate = dataclasses.replace(config, **swap_overrides)
        config_setter(swap_candidate)

    return 200, {'status': 'ok'}


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
    """Return current config with restart-required metadata and field hints."""
    file_values = _read_config_env(config.anthrouter_home)

    fields = {}
    field_order = list(FIELD_METADATA.keys())
    
    for field_name in field_order:
        meta = FIELD_METADATA[field_name]
        restart_required = EDITABLE_FIELDS.get(field_name, False)
        
        if restart_required:
            env_key = 'ANTHROUTER_' + field_name.upper()
            if env_key in file_values:
                value = file_values[env_key]
            else:
                attr_value = getattr(config, field_name, '')
                # Special-case: format dict fields as comma-separated string
                if field_name == 'auto_model_routing_classification' and isinstance(attr_value, dict):
                    value = ','.join(f'{k}:{v}' for k, v in attr_value.items())
                elif field_name == 'model_aliases' and isinstance(attr_value, dict):
                    value = ','.join(f'{k}:{v}' for k, v in attr_value.items())
                else:
                    value = str(attr_value)
        else:
            attr_value = getattr(config, field_name, '')
            # Special-case: format dict fields as comma-separated string
            if field_name == 'auto_model_routing_classification' and isinstance(attr_value, dict):
                value = ','.join(f'{k}:{v}' for k, v in attr_value.items())
            elif field_name == 'model_aliases' and isinstance(attr_value, dict):
                value = ','.join(f'{k}:{v}' for k, v in attr_value.items())
            else:
                value = str(attr_value)

        field_data: dict = {
            'restart_required': restart_required,
            'value': value,
            'description': meta.get('description', ''),
            'type': meta.get('type', 'str'),
            'group': meta.get('group', ''),
        }
        
        if 'enum' in meta and meta['enum'] is not None:
            field_data['enum'] = meta['enum']
        if 'min' in meta and meta['min'] is not None:
            field_data['min'] = meta['min']
        if 'max' in meta and meta['max'] is not None:
            field_data['max'] = meta['max']
        
        fields[field_name] = field_data

    return 200, {
        'admin_token_configured': bool(getattr(config, 'admin_token', None)),
        'fields': fields,
        'field_order': field_order,
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
            'workday_elapsed_pct': usage.workday_elapsed_pct,
            'calendar_elapsed_pct': usage.calendar_elapsed_pct,
            'workday_timezone': usage.workday_timezone,
            'period_start': usage.period_start,
            'period_end': usage.period_end,
            'period_workday_count': usage.period_workday_count,
        }
    }


def _get_prompt(content_hash: str, db) -> tuple[int, dict]:
    prompt = db.get_prompt(content_hash)
    if prompt is None:
        return _err(404, 'not_found_error', f'No stored prompt {content_hash}')
    return 200, prompt
