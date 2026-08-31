import json
from pathlib import Path

import pytest

from anthrouter.config import DEFAULT_UPSTREAM_BASE_URL, parse_args


def test_defaults():
    cfg = parse_args([])
    assert cfg.host == '127.0.0.1'
    assert cfg.port == 8083
    assert cfg.upstream_base_url == DEFAULT_UPSTREAM_BASE_URL
    assert cfg.auto_model_routing is False
    assert cfg.sanitize_system_prompt == 'strip'
    assert cfg.lock_requested_model == 'off'
    assert cfg.db_path is None
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
