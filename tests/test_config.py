import json
from pathlib import Path

import pytest

from anthrouter.config import (
    DEFAULT_UPSTREAM_BASE_URL,
    EDITABLE_FIELDS,
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


def test_task_tiers_parsed():
    cfg = parse_args(['--auto-model-routing-task-tiers',
                      json.dumps({'extraction': 'haiku'})])
    assert cfg.auto_model_routing_task_tiers == {'extraction': 'haiku'}


def test_task_tiers_invalid_json_rejected():
    with pytest.raises(SystemExit):
        parse_args(['--auto-model-routing-task-tiers', '{not json'])


def test_task_tiers_non_object_rejected():
    with pytest.raises(SystemExit):
        parse_args(['--auto-model-routing-task-tiers', '[1, 2]'])


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
    }
    assert live_editable == expected


def test_editable_fields_excludes_admin_token():
    """admin_token is not in EDITABLE_FIELDS (not editable via API)."""
    assert 'admin_token' not in EDITABLE_FIELDS


def test_editable_fields_excludes_classification():
    """auto_model_routing_classification is not in EDITABLE_FIELDS."""
    assert 'auto_model_routing_classification' not in EDITABLE_FIELDS


def test_editable_fields_excludes_task_tiers():
    """auto_model_routing_task_tiers is not in EDITABLE_FIELDS."""
    assert 'auto_model_routing_task_tiers' not in EDITABLE_FIELDS


def test_editable_fields_excludes_model_aliases():
    """model_aliases is not in EDITABLE_FIELDS."""
    assert 'model_aliases' not in EDITABLE_FIELDS


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
    assert '--auto-model-routing-classifier-model must be a non-empty string' in errors[0]


def test_validate_config_whitespace_classifier_model():
    """Whitespace-only auto_model_routing_classifier_model is rejected."""
    cfg = _make_valid_config(auto_model_routing_classifier_model='   ')
    errors = validate_config(cfg)
    assert len(errors) == 1
    assert '--auto-model-routing-classifier-model must be a non-empty string' in errors[0]


def test_validate_config_negative_sse_keepalive():
    """Negative sse_keepalive_interval is rejected."""
    cfg = _make_valid_config(sse_keepalive_interval=-0.1)
    errors = validate_config(cfg)
    assert len(errors) == 1
    assert '--sse-keepalive-interval must be >= 0' in errors[0]


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
    assert '--db-retention-days must be >= 0' in errors[0]


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
    assert '--auto-model-routing-long-context-threshold must be >= 0' in errors[0]


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
    assert '--auto-model-routing-prior-response-summary-limit must be in [50, 32000]' in errors[0]


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
    assert '--auto-model-routing-prior-response-summary-limit must be in [50, 32000]' in errors[0]


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
    assert '--auto-model-routing-system-prompt-cache-size must be >= 1' in errors[0]


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
    assert '--auto-model-routing-system-prompt-preview-limit must be >= 1' in errors[0]


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
    assert '--upstream-base-url must be a non-empty URL' in errors[0]


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
    assert '--auto-model-routing-system-prompt-weight + --auto-model-routing-user-prompt-weight must equal 1.0' in errors[0]


def test_validate_config_weights_sum_greater_than_one():
    """Weights summing to > 1.0 is rejected."""
    cfg = _make_valid_config(
        auto_model_routing_system_prompt_weight=0.5,
        auto_model_routing_user_prompt_weight=0.6,
    )
    errors = validate_config(cfg)
    assert len(errors) == 1
    assert '--auto-model-routing-system-prompt-weight + --auto-model-routing-user-prompt-weight must equal 1.0' in errors[0]


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
    assert any('--auto-model-routing-system-prompt-weight must be > 0' in e for e in errors)


def test_validate_config_user_prompt_weight_zero():
    """user_prompt_weight = 0 is rejected."""
    cfg = _make_valid_config(
        auto_model_routing_system_prompt_weight=1.0,
        auto_model_routing_user_prompt_weight=0.0,
    )
    errors = validate_config(cfg)
    assert len(errors) >= 1
    assert any('--auto-model-routing-user-prompt-weight must be > 0' in e for e in errors)


def test_validate_config_thresholds_equal():
    """trivial_threshold == standard_threshold is rejected."""
    cfg = _make_valid_config(
        auto_model_routing_trivial_threshold=50.0,
        auto_model_routing_standard_threshold=50.0,
    )
    errors = validate_config(cfg)
    assert len(errors) == 1
    assert '--auto-model-routing-trivial-threshold must be < --auto-model-routing-standard-threshold' in errors[0]


def test_validate_config_thresholds_reversed():
    """trivial_threshold > standard_threshold is rejected."""
    cfg = _make_valid_config(
        auto_model_routing_trivial_threshold=70.0,
        auto_model_routing_standard_threshold=30.0,
    )
    errors = validate_config(cfg)
    assert len(errors) == 1
    assert '--auto-model-routing-trivial-threshold must be < --auto-model-routing-standard-threshold' in errors[0]


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
    assert '--auto-model-routing-classifier-model must be a non-empty string' in error_text
    assert '--sse-keepalive-interval must be >= 0' in error_text
    assert '--db-retention-days must be >= 0' in error_text
