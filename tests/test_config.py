from pathlib import Path

import pytest

from anthrouter.config import (
    DEFAULT_UPSTREAM_BASE_URL,
    EDITABLE_FIELDS,
    FIELD_METADATA,
    Config,
    parse_args,
    validate_config,
)


def test_defaults():
    cfg = parse_args([])
    assert cfg.host == '127.0.0.1'
    assert cfg.port == 8083
    assert cfg.upstream_base_url == DEFAULT_UPSTREAM_BASE_URL
    assert cfg.auto_model_routing is False
    assert cfg.sanitize_system_prompt == 'strip'
    assert cfg.lock_requested_model == 'off'
    assert cfg.db_path is None
    assert cfg.db_retention_days == 30
    assert cfg.anthrouter_home == str(Path.home() / '.anthrouter')
    assert cfg.auto_model_routing_classification == {
        'trivial': 'haiku', 'standard': 'sonnet', 'deep': 'opus',
    }


def test_admin_token_defaults_to_none():
    assert parse_args([]).admin_token is None


def test_admin_token_flag():
    assert parse_args(['--admin-token', 'sekret']).admin_token == 'sekret'


def test_admin_token_env(monkeypatch):
    monkeypatch.setenv('ANTHROUTER_ADMIN_TOKEN', 'from-env')
    assert parse_args([]).admin_token == 'from-env'


def test_env_overrides(monkeypatch):
    monkeypatch.setenv('ANTHROUTER_PORT', '9001')
    monkeypatch.setenv('ANTHROUTER_AUTO_MODEL_ROUTING', '1')
    monkeypatch.setenv('ANTHROUTER_SANITIZE_SYSTEM_PROMPT', 'warn')
    cfg = parse_args([])
    assert cfg.port == 9001
    assert cfg.auto_model_routing is True
    assert cfg.sanitize_system_prompt == 'warn'


def test_db_retention_days_zero_is_allowed():
    assert parse_args(['--db-retention-days', '0']).db_retention_days == 0


def test_negative_db_retention_days_rejected():
    with pytest.raises(SystemExit):
        parse_args(['--db-retention-days', '-1'])


def test_upstream_base_url_trailing_slash_stripped():
    cfg = parse_args(['--upstream-base-url', 'https://example.test/v1/'])
    assert cfg.upstream_base_url == 'https://example.test/v1'


def test_empty_upstream_base_url_rejected():
    with pytest.raises(SystemExit):
        parse_args(['--upstream-base-url', '  '])


def test_classification_overlay_keeps_unspecified_defaults():
    cfg = parse_args(['--auto-model-routing-classification', 'standard:opus'])
    assert cfg.auto_model_routing_classification == {
        'trivial': 'haiku', 'standard': 'opus', 'deep': 'opus',
    }


def test_classification_unknown_label_rejected():
    with pytest.raises(SystemExit):
        parse_args(['--auto-model-routing-classification', 'huge:opus'])




def test_weights_must_sum_to_one():
    with pytest.raises(SystemExit):
        parse_args(['--auto-model-routing-system-prompt-weight', '0.5',
                    '--auto-model-routing-user-prompt-weight', '0.9'])


def test_thresholds_must_be_ordered():
    with pytest.raises(SystemExit):
        parse_args(['--auto-model-routing-trivial-threshold', '80',
                    '--auto-model-routing-standard-threshold', '75'])


def test_min_confidence_clamped():
    assert parse_args(['--auto-model-routing-min-confidence', '2.5']
                      ).auto_model_routing_min_confidence == 1.0
    assert parse_args(['--auto-model-routing-min-confidence', '-1']
                      ).auto_model_routing_min_confidence == 0.0


def test_enable_ui_defaults_db_path():
    cfg = parse_args(['--enable-ui', '--anthrouter-home', '/tmp/ar-home'])
    assert cfg.db_path == '/tmp/ar-home/anthrouter.db'


def test_explicit_db_path_survives_enable_ui():
    cfg = parse_args(['--enable-ui', '--db-path', '/tmp/other.db'])
    assert cfg.db_path == '/tmp/other.db'


# =============================================================================
# Tests for EDITABLE_FIELDS registry
# =============================================================================

def test_editable_fields_exists():
    """EDITABLE_FIELDS registry exists and is a dict."""
    assert isinstance(EDITABLE_FIELDS, dict)
    assert len(EDITABLE_FIELDS) > 0


def test_editable_fields_file_editable_fields():
    """Fields that require restart (file-editable)."""
    file_editable = {k for k, v in EDITABLE_FIELDS.items() if v is True}
    expected = {
        'host', 'port', 'log_file', 'db_path',
        'upstream_base_url', 'auto_model_routing_classifier_model',
        'model_aliases', 'oauth_usage_timezone',
    }
    assert file_editable == expected


def test_editable_fields_live_editable_fields():
    """Fields that are live-editable (apply immediately)."""
    live_editable = {k for k, v in EDITABLE_FIELDS.items() if v is False}
    expected = {
        'log_level', 'auto_model_routing',
        'auto_model_routing_long_context_threshold',
        'auto_model_routing_affirmation_inherit',
        'auto_model_routing_long',
        'auto_model_routing_confidence_bump',
        'auto_model_routing_min_confidence',
        'auto_model_routing_mode',
        'auto_model_routing_prior_response_summary_limit',
        'auto_model_routing_system_prompt_weight',
        'auto_model_routing_user_prompt_weight',
        'auto_model_routing_trivial_threshold',
        'auto_model_routing_standard_threshold',
        'auto_model_routing_system_prompt_cache_size',
        'auto_model_routing_system_prompt_preview_limit',
        'lock_requested_model',
        'sanitize_system_prompt',
        'sse_keepalive_interval',
        'db_retention_days',
        'auto_model_routing_classification',
    }
    assert live_editable == expected


def test_editable_fields_excludes_admin_token():
    """admin_token is not in EDITABLE_FIELDS (not editable via API)."""
    assert 'admin_token' not in EDITABLE_FIELDS


def test_editable_fields_includes_classification():
    """auto_model_routing_classification is in EDITABLE_FIELDS (live-editable)."""
    assert 'auto_model_routing_classification' in EDITABLE_FIELDS
    assert EDITABLE_FIELDS['auto_model_routing_classification'] is False  # live-editable, no restart required


def test_editable_fields_includes_model_aliases():
    """model_aliases is in EDITABLE_FIELDS (file-editable, requires restart)."""
    assert 'model_aliases' in EDITABLE_FIELDS
    assert EDITABLE_FIELDS['model_aliases'] is True  # file-editable, requires restart


def test_editable_fields_excludes_anthrouter_home():
    """anthrouter_home is not in EDITABLE_FIELDS."""
    assert 'anthrouter_home' not in EDITABLE_FIELDS


def test_editable_fields_excludes_enable_ui():
    """enable_ui is not in EDITABLE_FIELDS."""
    assert 'enable_ui' not in EDITABLE_FIELDS


def test_editable_fields_excludes_request_history_size():
    """request_history_size is not in EDITABLE_FIELDS."""
    assert 'request_history_size' not in EDITABLE_FIELDS


# =============================================================================
# Tests for FIELD_METADATA registry
# =============================================================================

def test_field_metadata_exists():
    """FIELD_METADATA registry exists and is a dict."""
    assert isinstance(FIELD_METADATA, dict)
    assert len(FIELD_METADATA) > 0


def test_field_metadata_keys_match_editable_fields():
    """FIELD_METADATA keys exactly match EDITABLE_FIELDS keys."""
    assert set(FIELD_METADATA.keys()) == set(EDITABLE_FIELDS.keys())


def test_field_metadata_has_required_keys():
    """Each FIELD_METADATA entry has required keys."""
    for field_name, meta in FIELD_METADATA.items():
        assert 'description' in meta, f'{field_name} missing description'
        assert 'type' in meta, f'{field_name} missing type'
        assert 'group' in meta, f'{field_name} missing group'
        assert meta['type'] in ('str', 'int', 'float', 'bool'), f'{field_name} has invalid type'


def test_field_metadata_enum_fields():
    """Enum fields have correct enum values."""
    assert FIELD_METADATA['log_level']['enum'] == ['DEBUG', 'INFO', 'WARNING', 'ERROR']
    assert FIELD_METADATA['auto_model_routing_mode']['enum'] == ['classifier', 'rules']
    assert FIELD_METADATA['sanitize_system_prompt']['enum'] == ['off', 'warn', 'strip']


def test_field_metadata_has_min_max():
    """Fields with bounds have min/max defined."""
    # auto_model_routing_min_confidence [0, 1]
    assert FIELD_METADATA['auto_model_routing_min_confidence']['min'] == 0.0
    assert FIELD_METADATA['auto_model_routing_min_confidence']['max'] == 1.0
    # db_retention_days min=0
    assert FIELD_METADATA['db_retention_days']['min'] == 0
    # sse_keepalive_interval min=0
    assert FIELD_METADATA['sse_keepalive_interval']['min'] == 0.0


def test_field_metadata_groups():
    """Fields are grouped correctly."""
    groups = {meta['group'] for meta in FIELD_METADATA.values()}
    expected_groups = {'Logging', 'Upstream', 'Model Routing', 'Prompt Sanitization', 'Database', 'Server', 'OAuth Usage'}
    assert groups == expected_groups


def test_field_metadata_order():
    """FIELD_METADATA order matches expected display order."""
    keys = list(FIELD_METADATA.keys())
    # First fields should be Logging group
    assert FIELD_METADATA[keys[0]]['group'] == 'Logging'
    # Last fields should be OAuth Usage group (newest group added)
    assert FIELD_METADATA[keys[-1]]['group'] == 'OAuth Usage'


# =============================================================================
# Tests for validate_config() - Enum enforcement
# =============================================================================

def test_validate_config_invalid_log_level():
    """Invalid log_level is rejected."""
    cfg = _make_valid_config(log_level='INVALID')
    errors = validate_config(cfg)
    assert len(errors) >= 1
    assert 'log_level must be one of' in errors[0]
    assert 'DEBUG' in errors[0]


def test_validate_config_valid_log_level():
    """Valid log_level passes."""
    for level in ['DEBUG', 'INFO', 'WARNING', 'ERROR']:
        cfg = _make_valid_config(log_level=level)
        errors = validate_config(cfg)
        # log_level check passes (other errors may exist)
        assert not any('log_level' in e for e in errors)


def test_validate_config_invalid_routing_mode():
    """Invalid auto_model_routing_mode is rejected."""
    cfg = _make_valid_config(auto_model_routing_mode='invalid_mode')
    errors = validate_config(cfg)
    assert len(errors) >= 1
    assert 'auto_model_routing_mode must be one of' in errors[0]
    assert 'classifier' in errors[0]


def test_validate_config_valid_routing_mode():
    """Valid auto_model_routing_mode passes."""
    for mode in ['classifier', 'rules']:
        cfg = _make_valid_config(auto_model_routing_mode=mode)
        errors = validate_config(cfg)
        assert not any('auto_model_routing_mode' in e for e in errors)


def test_validate_config_invalid_sanitize_system_prompt():
    """Invalid sanitize_system_prompt is rejected."""
    cfg = _make_valid_config(sanitize_system_prompt='invalid')
    errors = validate_config(cfg)
    assert len(errors) >= 1
    assert 'sanitize_system_prompt must be one of' in errors[0]
    assert 'off' in errors[0]


def test_validate_config_valid_sanitize_system_prompt():
    """Valid sanitize_system_prompt passes."""
    for mode in ['off', 'warn', 'strip']:
        cfg = _make_valid_config(sanitize_system_prompt=mode)
        errors = validate_config(cfg)
        assert not any('sanitize_system_prompt' in e for e in errors)


# =============================================================================
# Tests for validate_config() - min_confidence range [0, 1]
# =============================================================================

def test_validate_config_min_confidence_below_range():
    """auto_model_routing_min_confidence < 0 is rejected."""
    cfg = _make_valid_config(auto_model_routing_min_confidence=-0.5)
    errors = validate_config(cfg)
    assert len(errors) >= 1
    assert 'auto_model_routing_min_confidence must be in [0.0, 1.0]' in errors[0]


def test_validate_config_min_confidence_above_range():
    """auto_model_routing_min_confidence > 1 is rejected."""
    cfg = _make_valid_config(auto_model_routing_min_confidence=1.5)
    errors = validate_config(cfg)
    assert len(errors) >= 1
    assert 'auto_model_routing_min_confidence must be in [0.0, 1.0]' in errors[0]


def test_validate_config_min_confidence_boundary_low():
    """auto_model_routing_min_confidence = 0 is valid."""
    cfg = _make_valid_config(auto_model_routing_min_confidence=0.0)
    errors = validate_config(cfg)
    assert not any('auto_model_routing_min_confidence' in e for e in errors)


def test_validate_config_min_confidence_boundary_high():
    """auto_model_routing_min_confidence = 1 is valid."""
    cfg = _make_valid_config(auto_model_routing_min_confidence=1.0)
    errors = validate_config(cfg)
    assert not any('auto_model_routing_min_confidence' in e for e in errors)


def test_validate_config_min_confidence_mid_range():
    """auto_model_routing_min_confidence = 0.5 is valid."""
    cfg = _make_valid_config(auto_model_routing_min_confidence=0.5)
    errors = validate_config(cfg)
    assert not any('auto_model_routing_min_confidence' in e for e in errors)


# =============================================================================
# Tests for validate_config() - Error message uses field names not CLI flags
# =============================================================================

def test_validate_config_error_messages_use_field_names():
    """Validation errors reference Python field names, not CLI flags."""
    cfg = _make_valid_config(
        auto_model_routing_system_prompt_weight=0.5,
        auto_model_routing_user_prompt_weight=0.6,
    )
    errors = validate_config(cfg)
    # Error should use field name, not --flag-name
    assert any('auto_model_routing_system_prompt_weight' in e for e in errors)
    assert any('auto_model_routing_user_prompt_weight' in e for e in errors)
    # Should NOT use CLI flag spelling
    assert not any('--auto-model-routing-system-prompt-weight' in e for e in errors)


# =============================================================================
# Tests for validate_config() - Single-field bounds checks
# =============================================================================

def _make_valid_config(**overrides) -> Config:
    """Create a valid Config with optional overrides."""
    return Config(**overrides)


def test_validate_config_valid_config():
    """A valid config returns no errors."""
    cfg = _make_valid_config()
    errors = validate_config(cfg)
    assert errors == []


def test_validate_config_empty_classifier_model():
    """Empty auto_model_routing_classifier_model is rejected."""
    cfg = _make_valid_config(auto_model_routing_classifier_model='')
    errors = validate_config(cfg)
    assert len(errors) == 1
    assert 'auto_model_routing_classifier_model must be a non-empty string' in errors[0]


def test_validate_config_whitespace_classifier_model():
    """Whitespace-only auto_model_routing_classifier_model is rejected."""
    cfg = _make_valid_config(auto_model_routing_classifier_model='   ')
    errors = validate_config(cfg)
    assert len(errors) == 1
    assert 'auto_model_routing_classifier_model must be a non-empty string' in errors[0]


def test_validate_config_negative_sse_keepalive():
    """Negative sse_keepalive_interval is rejected."""
    cfg = _make_valid_config(sse_keepalive_interval=-0.1)
    errors = validate_config(cfg)
    assert len(errors) == 1
    assert 'sse_keepalive_interval must be >=' in errors[0]


def test_validate_config_zero_sse_keepalive():
    """Zero sse_keepalive_interval is valid (disables keepalive)."""
    cfg = _make_valid_config(sse_keepalive_interval=0.0)
    errors = validate_config(cfg)
    assert errors == []


def test_validate_config_negative_db_retention():
    """Negative db_retention_days is rejected."""
    cfg = _make_valid_config(db_retention_days=-1)
    errors = validate_config(cfg)
    assert len(errors) == 1
    assert 'db_retention_days must be >=' in errors[0]


def test_validate_config_zero_db_retention():
    """Zero db_retention_days is valid (keeps forever)."""
    cfg = _make_valid_config(db_retention_days=0)
    errors = validate_config(cfg)
    assert errors == []


def test_validate_config_negative_long_context_threshold():
    """Negative auto_model_routing_long_context_threshold is rejected."""
    cfg = _make_valid_config(auto_model_routing_long_context_threshold=-1)
    errors = validate_config(cfg)
    assert len(errors) == 1
    assert 'auto_model_routing_long_context_threshold must be >=' in errors[0]


def test_validate_config_zero_long_context_threshold():
    """Zero auto_model_routing_long_context_threshold is valid."""
    cfg = _make_valid_config(auto_model_routing_long_context_threshold=0)
    errors = validate_config(cfg)
    assert errors == []


def test_validate_config_prior_limit_too_low():
    """prior_response_summary_limit < 50 is rejected."""
    cfg = _make_valid_config(auto_model_routing_prior_response_summary_limit=49)
    errors = validate_config(cfg)
    assert len(errors) == 1
    assert 'auto_model_routing_prior_response_summary_limit must be in [50, 32000]' in errors[0]


def test_validate_config_prior_limit_boundary_low():
    """prior_response_summary_limit = 50 is valid (boundary)."""
    cfg = _make_valid_config(auto_model_routing_prior_response_summary_limit=50)
    errors = validate_config(cfg)
    assert errors == []


def test_validate_config_prior_limit_too_high():
    """prior_response_summary_limit > 32000 is rejected."""
    cfg = _make_valid_config(auto_model_routing_prior_response_summary_limit=32001)
    errors = validate_config(cfg)
    assert len(errors) == 1
    assert 'auto_model_routing_prior_response_summary_limit must be in [50, 32000]' in errors[0]


def test_validate_config_prior_limit_boundary_high():
    """prior_response_summary_limit = 32000 is valid (boundary)."""
    cfg = _make_valid_config(auto_model_routing_prior_response_summary_limit=32000)
    errors = validate_config(cfg)
    assert errors == []


def test_validate_config_cache_size_zero():
    """system_prompt_cache_size < 1 is rejected."""
    cfg = _make_valid_config(auto_model_routing_system_prompt_cache_size=0)
    errors = validate_config(cfg)
    assert len(errors) == 1
    assert 'auto_model_routing_system_prompt_cache_size must be >=' in errors[0]


def test_validate_config_cache_size_one():
    """system_prompt_cache_size = 1 is valid (boundary)."""
    cfg = _make_valid_config(auto_model_routing_system_prompt_cache_size=1)
    errors = validate_config(cfg)
    assert errors == []


def test_validate_config_preview_limit_zero():
    """system_prompt_preview_limit < 1 is rejected."""
    cfg = _make_valid_config(auto_model_routing_system_prompt_preview_limit=0)
    errors = validate_config(cfg)
    assert len(errors) == 1
    assert 'auto_model_routing_system_prompt_preview_limit must be >=' in errors[0]


def test_validate_config_preview_limit_one():
    """system_prompt_preview_limit = 1 is valid (boundary)."""
    cfg = _make_valid_config(auto_model_routing_system_prompt_preview_limit=1)
    errors = validate_config(cfg)
    assert errors == []


def test_validate_config_empty_upstream_base_url():
    """Empty upstream_base_url is rejected."""
    cfg = _make_valid_config(upstream_base_url='')
    errors = validate_config(cfg)
    assert len(errors) == 1
    assert 'upstream_base_url must be a non-empty URL' in errors[0]


# =============================================================================
# Tests for validate_config() - Cross-field invariants
# =============================================================================

def test_validate_config_weights_sum_less_than_one():
    """Weights summing to < 1.0 is rejected."""
    cfg = _make_valid_config(
        auto_model_routing_system_prompt_weight=0.3,
        auto_model_routing_user_prompt_weight=0.6,
    )
    errors = validate_config(cfg)
    assert len(errors) == 1
    assert 'auto_model_routing_system_prompt_weight + auto_model_routing_user_prompt_weight must equal 1.0' in errors[0]


def test_validate_config_weights_sum_greater_than_one():
    """Weights summing to > 1.0 is rejected."""
    cfg = _make_valid_config(
        auto_model_routing_system_prompt_weight=0.5,
        auto_model_routing_user_prompt_weight=0.6,
    )
    errors = validate_config(cfg)
    assert len(errors) == 1
    assert 'auto_model_routing_system_prompt_weight + auto_model_routing_user_prompt_weight must equal 1.0' in errors[0]


def test_validate_config_weights_sum_exactly_one():
    """Weights summing to exactly 1.0 is valid."""
    cfg = _make_valid_config(
        auto_model_routing_system_prompt_weight=0.2,
        auto_model_routing_user_prompt_weight=0.8,
    )
    errors = validate_config(cfg)
    assert errors == []


def test_validate_config_system_prompt_weight_zero():
    """system_prompt_weight = 0 is rejected."""
    cfg = _make_valid_config(
        auto_model_routing_system_prompt_weight=0.0,
        auto_model_routing_user_prompt_weight=1.0,
    )
    errors = validate_config(cfg)
    assert len(errors) >= 1
    assert any('auto_model_routing_system_prompt_weight must be > 0' in e for e in errors)


def test_validate_config_user_prompt_weight_zero():
    """user_prompt_weight = 0 is rejected."""
    cfg = _make_valid_config(
        auto_model_routing_system_prompt_weight=1.0,
        auto_model_routing_user_prompt_weight=0.0,
    )
    errors = validate_config(cfg)
    assert len(errors) >= 1
    assert any('auto_model_routing_user_prompt_weight must be > 0' in e for e in errors)


def test_validate_config_thresholds_equal():
    """trivial_threshold == standard_threshold is rejected."""
    cfg = _make_valid_config(
        auto_model_routing_trivial_threshold=50.0,
        auto_model_routing_standard_threshold=50.0,
    )
    errors = validate_config(cfg)
    assert len(errors) == 1
    assert 'auto_model_routing_trivial_threshold must be < auto_model_routing_standard_threshold' in errors[0]


def test_validate_config_thresholds_reversed():
    """trivial_threshold > standard_threshold is rejected."""
    cfg = _make_valid_config(
        auto_model_routing_trivial_threshold=70.0,
        auto_model_routing_standard_threshold=30.0,
    )
    errors = validate_config(cfg)
    assert len(errors) == 1
    assert 'auto_model_routing_trivial_threshold must be < auto_model_routing_standard_threshold' in errors[0]


def test_validate_config_thresholds_valid():
    """trivial_threshold < standard_threshold is valid."""
    cfg = _make_valid_config(
        auto_model_routing_trivial_threshold=30.0,
        auto_model_routing_standard_threshold=60.0,
    )
    errors = validate_config(cfg)
    assert errors == []


# =============================================================================
# Tests for validate_config() - Multiple errors
# =============================================================================

def test_validate_config_multiple_errors():
    """Multiple validation errors are all reported."""
    cfg = _make_valid_config(
        auto_model_routing_classifier_model='',
        sse_keepalive_interval=-1,
        db_retention_days=-5,
    )
    errors = validate_config(cfg)
    assert len(errors) >= 3
    error_text = '\n'.join(errors)
    assert 'auto_model_routing_classifier_model must be a non-empty string' in error_text
    assert 'sse_keepalive_interval must be >=' in error_text
    assert 'db_retention_days must be >=' in error_text


# =============================================================================
# Tests for --oauth-usage-timezone (ADR-0008)
# =============================================================================

def test_oauth_usage_timezone_defaults_to_none():
    """--oauth-usage-timezone defaults to None (auto-detect with Pacific fallback)."""
    cfg = parse_args([])
    # After resolution, should be a concrete IANA name (either detected or fallback)
    assert cfg.oauth_usage_timezone is not None


def test_oauth_usage_timezone_explicit_valid():
    """Explicit valid timezone is honoured."""
    cfg = parse_args(['--oauth-usage-timezone', 'Europe/Berlin'])
    assert cfg.oauth_usage_timezone == 'Europe/Berlin'


def test_oauth_usage_timezone_explicit_utc():
    """Explicit 'UTC' is honoured (no fallback)."""
    cfg = parse_args(['--oauth-usage-timezone', 'UTC'])
    assert cfg.oauth_usage_timezone == 'UTC'


def test_oauth_usage_timezone_invalid_rejected():
    """Invalid timezone name causes SystemExit."""
    with pytest.raises(SystemExit):
        parse_args(['--oauth-usage-timezone', 'Not/AZone'])


def test_oauth_usage_timezone_env_var(monkeypatch):
    """ANTHROUTER_OAUTH_USAGE_TIMEZONE env var is honoured."""
    monkeypatch.setenv('ANTHROUTER_OAUTH_USAGE_TIMEZONE', 'America/New_York')
    cfg = parse_args([])
    assert cfg.oauth_usage_timezone == 'America/New_York'


def test_oauth_usage_timezone_flag_overrides_env(monkeypatch):
    """CLI flag overrides ANTHROUTER_OAUTH_USAGE_TIMEZONE env var."""
    monkeypatch.setenv('ANTHROUTER_OAUTH_USAGE_TIMEZONE', 'America/New_York')
    cfg = parse_args(['--oauth-usage-timezone', 'Asia/Tokyo'])
    assert cfg.oauth_usage_timezone == 'Asia/Tokyo'


def test_oauth_usage_timezone_in_editable_fields():
    """oauth_usage_timezone is in EDITABLE_FIELDS (file-editable, restart required)."""
    assert 'oauth_usage_timezone' in EDITABLE_FIELDS
    assert EDITABLE_FIELDS['oauth_usage_timezone'] is True  # restart required


def test_oauth_usage_timezone_in_field_metadata():
    """oauth_usage_timezone has FIELD_METADATA entry."""
    assert 'oauth_usage_timezone' in FIELD_METADATA
    meta = FIELD_METADATA['oauth_usage_timezone']
    assert meta['type'] == 'str'
    assert 'description' in meta
    assert 'group' in meta
    assert meta['group'] == 'OAuth Usage'
