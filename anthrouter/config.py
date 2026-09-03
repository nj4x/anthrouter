import argparse
import dataclasses
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_UPSTREAM_BASE_URL = 'https://api.anthropic.com'

# EDITABLE_FIELDS: registry of config fields that can be edited via admin API
# True = file-editable (restart required for changes to apply)
# False = live-editable (applies immediately)
# Excluded fields (not in this registry): admin_token, auto_model_routing_classification,
#   auto_model_routing_task_tiers, model_aliases, anthrouter_home, enable_ui, request_history_size
EDITABLE_FIELDS: dict[str, bool] = {
    # File-editable (restart required)
    'host': True,
    'port': True,
    'log_file': True,
    'db_path': True,
    'upstream_base_url': True,
    'auto_model_routing_classifier_model': True,
    # Live-editable (applies immediately)
    'log_level': False,
    'auto_model_routing': False,
    'auto_model_routing_long_context_threshold': False,
    'auto_model_routing_affirmation_inherit': False,
    'auto_model_routing_long': False,
    'auto_model_routing_confidence_bump': False,
    'auto_model_routing_min_confidence': False,
    'auto_model_routing_mode': False,
    'auto_model_routing_prior_response_summary_limit': False,
    'auto_model_routing_system_prompt_weight': False,
    'auto_model_routing_user_prompt_weight': False,
    'auto_model_routing_trivial_threshold': False,
    'auto_model_routing_standard_threshold': False,
    'auto_model_routing_system_prompt_cache_size': False,
    'auto_model_routing_system_prompt_preview_limit': False,
    'lock_requested_model': False,
    'sanitize_system_prompt': False,
    'sse_keepalive_interval': False,
    'db_retention_days': False,
}


# FieldMetadata: per-field metadata for the admin UI config editor
# Each entry has: description, type, enum (optional), min (optional), max (optional), group
# The insertion order IS the display order in the UI
FIELD_METADATA: dict[str, dict] = {
    # === Logging ===
    'log_level': {
        'description': 'Minimum severity level for log messages.',
        'type': 'str',
        'enum': ['DEBUG', 'INFO', 'WARNING', 'ERROR'],
        'group': 'Logging',
    },
    'log_file': {
        'description': 'Path to the log file where messages are written.',
        'type': 'str',
        'group': 'Logging',
    },
    # === Upstream ===
    'upstream_base_url': {
        'description': 'Base URL of the Anthropic API to forward requests to.',
        'type': 'str',
        'group': 'Upstream',
    },
    # === Model Routing ===
    'auto_model_routing': {
        'description': 'Automatically route requests to a configured target model based on complexity.',
        'type': 'bool',
        'group': 'Model Routing',
    },
    'auto_model_routing_mode': {
        'description': 'Classification mode for auto routing: "classifier" uses an LLM, "rules" uses deterministic keyword rules, "tag" routes by task name.',
        'type': 'str',
        'enum': ['classifier', 'rules', 'tag'],
        'group': 'Model Routing',
    },
    'auto_model_routing_classifier_model': {
        'description': 'Model alias used for the internal complexity-classifier call when auto routing is enabled.',
        'type': 'str',
        'group': 'Model Routing',
    },
    'auto_model_routing_long': {
        'description': 'Model forced by the long-context size floor; "off" disables the floor.',
        'type': 'str',
        'group': 'Model Routing',
    },
    'auto_model_routing_long_context_threshold': {
        'description': 'Estimated input token threshold at or above which auto routing deterministically forces the long model; 0 disables the floor.',
        'type': 'int',
        'min': 0,
        'group': 'Model Routing',
    },
    'auto_model_routing_trivial_threshold': {
        'description': 'Weighted score threshold below which the blended tier is "trivial"; must be strictly less than auto_model_routing_standard_threshold.',
        'type': 'float',
        'group': 'Model Routing',
    },
    'auto_model_routing_standard_threshold': {
        'description': 'Weighted score threshold at or above which the blended tier is "deep"; values between auto_model_routing_trivial_threshold and this are "standard".',
        'type': 'float',
        'group': 'Model Routing',
    },
    'auto_model_routing_system_prompt_weight': {
        'description': 'Weight applied to the system-prompt tier score in the weighted blend; must sum to 1.0 with auto_model_routing_user_prompt_weight and both must be > 0.',
        'type': 'float',
        'min': 0.0,
        'max': 1.0,
        'group': 'Model Routing',
    },
    'auto_model_routing_user_prompt_weight': {
        'description': 'Weight applied to the user-prompt tier score in the weighted blend; must sum to 1.0 with auto_model_routing_system_prompt_weight and both must be > 0.',
        'type': 'float',
        'min': 0.0,
        'max': 1.0,
        'group': 'Model Routing',
    },
    'auto_model_routing_system_prompt_cache_size': {
        'description': 'Maximum number of system-prompt SHA256 to tier-score entries in the in-memory LRU cache.',
        'type': 'int',
        'min': 1,
        'group': 'Model Routing',
    },
    'auto_model_routing_system_prompt_preview_limit': {
        'description': 'Maximum characters of the system prompt sent to the system-prompt classifier.',
        'type': 'int',
        'min': 1,
        'group': 'Model Routing',
    },
    'auto_model_routing_confidence_bump': {
        'description': 'When enabled, the classifier uses a structured JSON output format that includes a confidence score; turns classified below auto_model_routing_min_confidence are bumped to the next tier.',
        'type': 'bool',
        'group': 'Model Routing',
    },
    'auto_model_routing_min_confidence': {
        'description': 'Minimum confidence score (0.0 to 1.0) required before the classifier tier label is used as-is; turns below this threshold are bumped up.',
        'type': 'float',
        'min': 0.0,
        'max': 1.0,
        'group': 'Model Routing',
    },
    'auto_model_routing_affirmation_inherit': {
        'description': 'When auto routing is on, treat a bare confirmation turn as a continuation and inherit the conversation established tier instead of classifying it as trivial.',
        'type': 'bool',
        'group': 'Model Routing',
    },
    'auto_model_routing_prior_response_summary_limit': {
        'description': 'Maximum characters of the prior assistant response sent to the classifier during affirmation enrichment.',
        'type': 'int',
        'min': 50,
        'max': 32000,
        'group': 'Model Routing',
    },
    'lock_requested_model': {
        'description': 'Override the incoming request model with a fixed baseline before auto routing fires; "off" disables the lock and passes the client model through unchanged.',
        'type': 'str',
        'group': 'Model Routing',
    },
    # === Prompt Sanitization ===
    'sanitize_system_prompt': {
        'description': 'Handle cache-hostile volatile blocks in the inbound system prompt: "strip" removes allowlisted telemetry blocks, "warn" only detects and warns, "off" disables both.',
        'type': 'str',
        'enum': ['off', 'warn', 'strip'],
        'group': 'Prompt Sanitization',
    },
    # === Database ===
    'db_path': {
        'description': 'Path to the SQLite database file for request and routing records.',
        'type': 'str',
        'group': 'Database',
    },
    'db_retention_days': {
        'description': 'Delete request rows older than this many days; 0 keeps rows forever.',
        'type': 'int',
        'min': 0,
        'group': 'Database',
    },
    # === Server ===
    'host': {
        'description': 'Network address the server binds to.',
        'type': 'str',
        'group': 'Server',
    },
    'port': {
        'description': 'TCP port the server listens on.',
        'type': 'int',
        'group': 'Server',
    },
    'sse_keepalive_interval': {
        'description': 'Seconds between SSE keepalive comment lines sent to the client while waiting for the upstream first byte; 0 disables keepalive.',
        'type': 'float',
        'min': 0.0,
        'group': 'Server',
    },
}

# Fail-closed invariant: FIELD_METADATA keys must exactly match EDITABLE_FIELDS keys
if set(FIELD_METADATA.keys()) != set(EDITABLE_FIELDS.keys()):
    missing_in_metadata = set(EDITABLE_FIELDS.keys()) - set(FIELD_METADATA.keys())
    extra_in_metadata = set(FIELD_METADATA.keys()) - set(EDITABLE_FIELDS.keys())
    raise RuntimeError(
        f'FIELD_METADATA keys diverge from EDITABLE_FIELDS keys. '
        f'Missing in metadata: {sorted(missing_in_metadata)}. '
        f'Extra in metadata: {sorted(extra_in_metadata)}.'
    )

_DEFAULT_CLASSIFICATION: dict[str, str] = {
    'trivial': 'haiku',
    'standard': 'sonnet',
    'deep': 'opus',
}

_VALID_CLASSIFICATION_LABELS: frozenset[str] = frozenset(_DEFAULT_CLASSIFICATION)


def _env_bool(name: str, default: bool) -> bool:
    """Read a boolean from an env var.  '1'/'true'/'yes' → True; '0'/'false'/'no' → False."""
    val = os.environ.get(name, '').lower()
    if val in ('1', 'true', 'yes'):
        return True
    if val in ('0', 'false', 'no'):
        return False
    return default


def _resolve_home(home_override: str) -> str:
    """Resolve a home directory: explicit override > default ~/.anthrouter."""
    if home_override and home_override.strip():
        return home_override.strip()
    return str(Path.home() / '.anthrouter')


def _parse_classification_str(
    raw: str | None, p: argparse.ArgumentParser
) -> dict[str, str]:
    """Parse a comma-separated ``label:model`` string, overlay on defaults.

    Merges parsed pairs into ``_DEFAULT_CLASSIFICATION`` so unspecified labels
    keep their default targets.  Calls ``p.error()`` on any malformed input.
    """
    if not raw or not raw.strip():
        return dict(_DEFAULT_CLASSIFICATION)
    result = dict(_DEFAULT_CLASSIFICATION)
    for pair in raw.split(','):
        pair = pair.strip()
        if not pair:
            continue
        parts = pair.split(':', 1)
        if len(parts) != 2:
            p.error(
                f'--auto-model-routing-classification: malformed pair {pair!r}; '
                'expected label:model format'
            )
        label, model = parts[0].strip(), parts[1].strip()
        if label not in _VALID_CLASSIFICATION_LABELS:
            p.error(
                f'--auto-model-routing-classification: unknown label {label!r}; '
                'valid labels are: trivial, standard, deep'
            )
        if not model:
            p.error(
                f'--auto-model-routing-classification: model for label {label!r} '
                'must be a non-empty string'
            )
        result[label] = model
    return result


def _parse_model_aliases_str(
    raw: str | None, p: argparse.ArgumentParser
) -> dict[str, str]:
    """Parse a comma-separated ``alias:model`` string into an alias table.

    Returns a pure override table (no merging with defaults).  Calls ``p.error()``
    on any malformed input.
    """
    if not raw or not raw.strip():
        return {}
    result: dict[str, str] = {}
    for pair in raw.split(','):
        pair = pair.strip()
        if not pair:
            continue
        parts = pair.split(':', 1)
        if len(parts) != 2:
            p.error(
                f'--model-aliases: malformed pair {pair!r}; '
                'expected alias:model format'
            )
        alias, model = parts[0].strip(), parts[1].strip()
        if not alias:
            p.error(
                f'--model-aliases: alias in pair {pair!r} '
                'must be a non-empty string'
            )
        if not model:
            p.error(
                f'--model-aliases: model for alias {alias!r} '
                'must be a non-empty string'
            )
        result[alias] = model
    return result


@dataclasses.dataclass
class Config:
    host: str = '127.0.0.1'
    port: int = 8083
    upstream_base_url: str = DEFAULT_UPSTREAM_BASE_URL
    log_level: str = 'INFO'
    log_file: str = '/tmp/anthrouter.log'
    anthrouter_home: str = ''
    request_history_size: int = 5
    auto_model_routing: bool = False
    auto_model_routing_classifier_model: str = 'haiku'
    auto_model_routing_long_context_threshold: int = 150_000
    auto_model_routing_affirmation_inherit: bool = True
    auto_model_routing_classification: dict[str, str] = dataclasses.field(
        default_factory=lambda: dict(_DEFAULT_CLASSIFICATION)
    )
    auto_model_routing_long: str = 'off'
    auto_model_routing_confidence_bump: bool = False
    auto_model_routing_min_confidence: float = 0.0
    auto_model_routing_mode: str = 'classifier'
    auto_model_routing_task_tiers: dict[str, str] | None = None
    auto_model_routing_prior_response_summary_limit: int = 1000
    auto_model_routing_system_prompt_weight: float = 0.20
    auto_model_routing_user_prompt_weight: float = 0.80
    auto_model_routing_trivial_threshold: float = 30.0
    auto_model_routing_standard_threshold: float = 60.0
    auto_model_routing_system_prompt_cache_size: int = 256
    auto_model_routing_system_prompt_preview_limit: int = 500
    lock_requested_model: str = 'off'   # Model baseline lock for routing; 'off' disables
    sanitize_system_prompt: str = 'strip'  # off | warn | strip
    sse_keepalive_interval: float = 10.0
    db_path: str | None = None   # Path to SQLite DB file; None disables DB recording
    db_retention_days: int = 30  # Prune request rows older than this; 0 keeps forever
    enable_ui: bool = False     # Whether /admin/* and /ui/* endpoints are active
    model_aliases: dict[str, str] = dataclasses.field(default_factory=dict)  # User-supplied alias overrides
    admin_token: str | None = None  # Gates POST /admin/config; unset disables config writes


def validate_config(cfg: Config) -> list[str]:
    """Validate a Config instance and return a list of error messages.
    
    Returns an empty list if the config is valid.
    
    Validates:
    - Enum values: log_level, auto_model_routing_mode, sanitize_system_prompt
    - Numeric bounds: min_confidence in [0, 1], sse_keepalive_interval >= 0,
      db_retention_days >= 0, long_context_threshold >= 0, prior_limit in [50, 32000],
      system_prompt_cache_size >= 1, system_prompt_preview_limit >= 1,
      weight bounds [0, 1]
    - Cross-field invariants: weight pairs summing to 1.0, threshold ordering
    - Non-empty: upstream_base_url, auto_model_routing_classifier_model
    
    Does NOT enforce config.env quoting rules (serialization concern).
    """
    errors: list[str] = []
    
    # Enum enforcement (sourced from FIELD_METADATA)
    log_level_meta = FIELD_METADATA.get('log_level', {})
    if log_level_meta.get('enum') and cfg.log_level not in log_level_meta['enum']:
        errors.append(f"log_level must be one of {log_level_meta['enum']}, got {cfg.log_level!r}")
    
    mode_meta = FIELD_METADATA.get('auto_model_routing_mode', {})
    if mode_meta.get('enum') and cfg.auto_model_routing_mode not in mode_meta['enum']:
        errors.append(f"auto_model_routing_mode must be one of {mode_meta['enum']}, got {cfg.auto_model_routing_mode!r}")
    
    sanitize_meta = FIELD_METADATA.get('sanitize_system_prompt', {})
    if sanitize_meta.get('enum') and cfg.sanitize_system_prompt not in sanitize_meta['enum']:
        errors.append(f"sanitize_system_prompt must be one of {sanitize_meta['enum']}, got {cfg.sanitize_system_prompt!r}")
    
    # Numeric bounds: auto_model_routing_min_confidence [0, 1]
    min_conf_meta = FIELD_METADATA.get('auto_model_routing_min_confidence', {})
    min_conf_min = min_conf_meta.get('min', 0.0)
    min_conf_max = min_conf_meta.get('max', 1.0)
    if cfg.auto_model_routing_min_confidence < min_conf_min or cfg.auto_model_routing_min_confidence > min_conf_max:
        errors.append(f'auto_model_routing_min_confidence must be in [{min_conf_min}, {min_conf_max}], got {cfg.auto_model_routing_min_confidence}')
    
    # Single-field bounds checks
    if not cfg.auto_model_routing_classifier_model.strip():
        errors.append('auto_model_routing_classifier_model must be a non-empty string')
    
    sse_meta = FIELD_METADATA.get('sse_keepalive_interval', {})
    sse_min = sse_meta.get('min', 0.0)
    if cfg.sse_keepalive_interval < sse_min:
        errors.append(f'sse_keepalive_interval must be >= {sse_min}, got {cfg.sse_keepalive_interval}')
    
    db_ret_meta = FIELD_METADATA.get('db_retention_days', {})
    db_ret_min = db_ret_meta.get('min', 0)
    if cfg.db_retention_days < db_ret_min:
        errors.append(f'db_retention_days must be >= {db_ret_min}, got {cfg.db_retention_days}')
    
    ctx_meta = FIELD_METADATA.get('auto_model_routing_long_context_threshold', {})
    ctx_min = ctx_meta.get('min', 0)
    if cfg.auto_model_routing_long_context_threshold < ctx_min:
        errors.append(f'auto_model_routing_long_context_threshold must be >= {ctx_min}, got {cfg.auto_model_routing_long_context_threshold}')
    
    prior_meta = FIELD_METADATA.get('auto_model_routing_prior_response_summary_limit', {})
    prior_min = prior_meta.get('min', 50)
    prior_max = prior_meta.get('max', 32000)
    prior_limit = cfg.auto_model_routing_prior_response_summary_limit
    if prior_limit < prior_min or prior_limit > prior_max:
        errors.append(f'auto_model_routing_prior_response_summary_limit must be in [{prior_min}, {prior_max}], got {prior_limit}')
    
    cache_meta = FIELD_METADATA.get('auto_model_routing_system_prompt_cache_size', {})
    cache_min = cache_meta.get('min', 1)
    if cfg.auto_model_routing_system_prompt_cache_size < cache_min:
        errors.append(f'auto_model_routing_system_prompt_cache_size must be >= {cache_min}, got {cfg.auto_model_routing_system_prompt_cache_size}')
    
    preview_meta = FIELD_METADATA.get('auto_model_routing_system_prompt_preview_limit', {})
    preview_min = preview_meta.get('min', 1)
    if cfg.auto_model_routing_system_prompt_preview_limit < preview_min:
        errors.append(f'auto_model_routing_system_prompt_preview_limit must be >= {preview_min}, got {cfg.auto_model_routing_system_prompt_preview_limit}')
    
    if not cfg.upstream_base_url:
        errors.append('upstream_base_url must be a non-empty URL')
    
    # Weight bounds [0, 1] from metadata
    sys_w_meta = FIELD_METADATA.get('auto_model_routing_system_prompt_weight', {})
    usr_w_meta = FIELD_METADATA.get('auto_model_routing_user_prompt_weight', {})
    sys_w_min = sys_w_meta.get('min', 0.0)
    sys_w_max = sys_w_meta.get('max', 1.0)
    usr_w_min = usr_w_meta.get('min', 0.0)
    usr_w_max = usr_w_meta.get('max', 1.0)
    
    sys_w = cfg.auto_model_routing_system_prompt_weight
    usr_w = cfg.auto_model_routing_user_prompt_weight
    
    if sys_w < sys_w_min or sys_w > sys_w_max:
        errors.append(f'auto_model_routing_system_prompt_weight must be in [{sys_w_min}, {sys_w_max}], got {sys_w}')
    if usr_w < usr_w_min or usr_w > usr_w_max:
        errors.append(f'auto_model_routing_user_prompt_weight must be in [{usr_w_min}, {usr_w_max}], got {usr_w}')
    
    # Cross-field invariants: weight sum
    if abs(sys_w + usr_w - 1.0) >= 1e-9:
        errors.append(f'auto_model_routing_system_prompt_weight + auto_model_routing_user_prompt_weight must equal 1.0, got {sys_w} + {usr_w} = {sys_w + usr_w}')
    
    # Cross-field invariants: weight > 0 (separate from bounds check)
    if sys_w <= 0:
        errors.append(f'auto_model_routing_system_prompt_weight must be > 0, got {sys_w}')
    
    if usr_w <= 0:
        errors.append(f'auto_model_routing_user_prompt_weight must be > 0, got {usr_w}')
    
    # Cross-field invariants: threshold ordering
    trivial_t = cfg.auto_model_routing_trivial_threshold
    standard_t = cfg.auto_model_routing_standard_threshold
    if trivial_t >= standard_t:
        errors.append(f'auto_model_routing_trivial_threshold must be < auto_model_routing_standard_threshold, got {trivial_t} >= {standard_t}')
    
    return errors


def parse_args(argv=None) -> Config:
    p = argparse.ArgumentParser(
        prog='anthrouter',
        description='Single-backend Anthropic proxy with model-tier routing '
                    'and system-prompt sanitization',
    )
    
    # Helper to build help text from FIELD_METADATA
    def _help_from_meta(field_name: str, extra: str = '') -> str:
        """Build argparse help from FIELD_METADATA description plus optional extra."""
        meta = FIELD_METADATA.get(field_name, {})
        desc = meta.get('description', '')
        parts = [desc] if desc else []
        if extra:
            parts.append(extra)
        return ' '.join(parts)
    
    p.add_argument('--host', default=os.environ.get('ANTHROUTER_HOST', '127.0.0.1'),
                   help=f'{_help_from_meta("host")} (default: 127.0.0.1, env: ANTHROUTER_HOST)')
    p.add_argument('--port', type=int,
                   default=int(os.environ.get('ANTHROUTER_PORT', '8083')),
                   help=f'{_help_from_meta("port")} (default: 8083, env: ANTHROUTER_PORT)')
    p.add_argument('--upstream-base-url', dest='upstream_base_url',
                   default=os.environ.get('ANTHROUTER_UPSTREAM_BASE_URL',
                                          DEFAULT_UPSTREAM_BASE_URL),
                   help=f'{_help_from_meta("upstream_base_url")} (default: {DEFAULT_UPSTREAM_BASE_URL}, env: ANTHROUTER_UPSTREAM_BASE_URL)')
    p.add_argument('--log-level',
                   default=os.environ.get('ANTHROUTER_LOG_LEVEL', 'INFO'),
                   choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
                   help=f'{_help_from_meta("log_level")} Choices: DEBUG, INFO, WARNING, ERROR. (default: INFO, env: ANTHROUTER_LOG_LEVEL)')
    p.add_argument('--log-file',
                   default=os.environ.get('ANTHROUTER_LOG_FILE', '/tmp/anthrouter.log'),
                   help=f'{_help_from_meta("log_file")} (default: /tmp/anthrouter.log, env: ANTHROUTER_LOG_FILE)')
    p.add_argument('--anthrouter-home', dest='anthrouter_home',
                   default=os.environ.get('ANTHROUTER_HOME', ''),
                   help='Root directory for anthrouter config and state'
                        ' (default: ~/.anthrouter, env: ANTHROUTER_HOME)')
    p.add_argument('--request-history-size', type=int,
                   default=int(os.environ.get('ANTHROUTER_REQUEST_HISTORY_SIZE', '5')),
                   help='Number of recent requests to keep in ring buffer'
                        ' (default: 5, env: ANTHROUTER_REQUEST_HISTORY_SIZE)')
    p.add_argument('--auto-model-routing', dest='auto_model_routing',
                   action=argparse.BooleanOptionalAction,
                   default=_env_bool('ANTHROUTER_AUTO_MODEL_ROUTING', False),
                   help='Automatically route requests whose model is any non-empty string to a'
                        ' configured target. Routing failures preserve the original requested model.'
                        ' In classifier mode, the classifier call uses the model set by'
                        ' --auto-model-routing-classifier-model (default: off,'
                        ' env: ANTHROUTER_AUTO_MODEL_ROUTING=1)')
    p.add_argument('--auto-model-routing-classifier-model',
                   dest='auto_model_routing_classifier_model',
                   default=os.environ.get(
                       'ANTHROUTER_AUTO_MODEL_ROUTING_CLASSIFIER_MODEL', 'haiku'),
                   help='Model alias to use for the internal complexity-classifier call when'
                        ' --auto-model-routing is enabled (default: haiku,'
                        ' env: ANTHROUTER_AUTO_MODEL_ROUTING_CLASSIFIER_MODEL)')
    p.add_argument('--auto-model-routing-long-context-threshold',
                   dest='auto_model_routing_long_context_threshold', type=int,
                   default=int(os.environ.get(
                       'ANTHROUTER_AUTO_MODEL_ROUTING_LONG_CONTEXT_THRESHOLD', '150000')),
                   help='Estimated-input-token threshold at/above which --auto-model-routing'
                        ' deterministically forces the target configured by'
                        ' --auto-model-routing-long and injects the context-1m beta, bypassing the'
                        ' classifier. 0 disables the floor; setting --auto-model-routing-long=off'
                        ' also disables it. Only effective when --auto-model-routing is on'
                        ' (default: 150000,'
                        ' env: ANTHROUTER_AUTO_MODEL_ROUTING_LONG_CONTEXT_THRESHOLD)')
    p.add_argument('--auto-model-routing-affirmation-inherit',
                   dest='auto_model_routing_affirmation_inherit',
                   action=argparse.BooleanOptionalAction,
                   default=_env_bool(
                       'ANTHROUTER_AUTO_MODEL_ROUTING_AFFIRMATION_INHERIT', True),
                   help='When --auto-model-routing is on, treat a bare confirmation turn'
                        ' ("yes", "go ahead", "proceed") as a continuation: inherit the'
                        " conversation's established tier instead of classifying it as"
                        ' trivial and poisoning the session tier cache'
                        ' (default: on, env: ANTHROUTER_AUTO_MODEL_ROUTING_AFFIRMATION_INHERIT)')
    p.add_argument(
        '--auto-model-routing-classification',
        dest='auto_model_routing_classification',
        default=os.environ.get('ANTHROUTER_AUTO_MODEL_ROUTING_CLASSIFICATION', ''),
        help='Comma-separated label:model pairs overriding tier targets for '
             '--auto-model-routing. Valid labels: trivial, standard, deep. '
             'Unspecified labels keep their defaults (trivial→haiku, '
             'standard→sonnet, deep→opus). Example: standard:opus,deep:fable '
             '(env: ANTHROUTER_AUTO_MODEL_ROUTING_CLASSIFICATION)',
    )
    p.add_argument(
        '--auto-model-routing-long',
        dest='auto_model_routing_long',
        default=os.environ.get('ANTHROUTER_AUTO_MODEL_ROUTING_LONG', 'off'),
        help='Model forced by the long-context size floor under --auto-model-routing '
             '(default: off). Pass "opus[1m]" or another model to enable the floor. '
             '(env: ANTHROUTER_AUTO_MODEL_ROUTING_LONG)',
    )
    p.add_argument('--auto-model-routing-confidence-bump',
                   dest='auto_model_routing_confidence_bump',
                   action=argparse.BooleanOptionalAction,
                   default=_env_bool(
                       'ANTHROUTER_AUTO_MODEL_ROUTING_CONFIDENCE_BUMP', False),
                   help='When enabled, the classifier uses a structured JSON output format '
                        'that includes a confidence score; turns classified below '
                        '--auto-model-routing-min-confidence are bumped to the next tier '
                        '(trivial→standard, standard→deep). Only effective when '
                        '--auto-model-routing is on. '
                        '(default: off, env: ANTHROUTER_AUTO_MODEL_ROUTING_CONFIDENCE_BUMP)')
    p.add_argument('--auto-model-routing-min-confidence',
                   dest='auto_model_routing_min_confidence', type=float,
                   default=float(os.environ.get(
                       'ANTHROUTER_AUTO_MODEL_ROUTING_MIN_CONFIDENCE', '0.0')),
                   help='Minimum confidence score (0.0–1.0) required before the '
                        "classifier's tier label is used as-is; turns below this threshold "
                        'are bumped up (trivial→standard, standard→deep). Only effective when '
                        '--auto-model-routing-confidence-bump is on. '
                        '(default: 0.0, env: ANTHROUTER_AUTO_MODEL_ROUTING_MIN_CONFIDENCE)')
    p.add_argument(
        '--auto-model-routing-mode',
        dest='auto_model_routing_mode',
        default=os.environ.get('ANTHROUTER_AUTO_MODEL_ROUTING_MODE', 'classifier'),
        choices=['classifier', 'rules', 'tag'],
        help='Classification mode for --auto-model-routing: "classifier" (default) calls a '
             'lightweight LLM classifier upstream; "rules" uses deterministic keyword rules '
             'with no LLM call; "tag" routes via a supplied task name against '
             '--auto-model-routing-task-tiers. '
             '(env: ANTHROUTER_AUTO_MODEL_ROUTING_MODE)',
    )
    p.add_argument(
        '--auto-model-routing-task-tiers',
        dest='auto_model_routing_task_tiers',
        default=os.environ.get('ANTHROUTER_AUTO_MODEL_ROUTING_TASK_TIERS', ''),
        help='JSON object mapping task names to model tier aliases for '
             '--auto-model-routing-mode=tag. Example: \'{"extraction":"haiku","analysis":"sonnet"}\'. '
             'Unknown task names fail-closed to the requested model. '
             '(env: ANTHROUTER_AUTO_MODEL_ROUTING_TASK_TIERS)',
    )
    p.add_argument(
        '--auto-model-routing-prior-response-summary-limit',
        dest='auto_model_routing_prior_response_summary_limit', type=int,
        default=int(os.environ.get(
            'ANTHROUTER_AUTO_MODEL_ROUTING_PRIOR_RESPONSE_SUMMARY_LIMIT', '1000')),
        help='Maximum characters of the prior assistant response sent to the classifier '
             'during affirmation enrichment (30/70 head/tail split). '
             'Valid range: [50, 32000]. '
             '(default: 1000, env: ANTHROUTER_AUTO_MODEL_ROUTING_PRIOR_RESPONSE_SUMMARY_LIMIT)',
    )
    p.add_argument(
        '--auto-model-routing-system-prompt-weight',
        dest='auto_model_routing_system_prompt_weight', type=float,
        default=float(os.environ.get(
            'ANTHROUTER_AUTO_MODEL_ROUTING_SYSTEM_PROMPT_WEIGHT', '0.20')),
        help='Weight applied to the system-prompt tier score in the weighted blend '
             '(must sum to 1.0 with --auto-model-routing-user-prompt-weight; both > 0). '
             '(default: 0.20, env: ANTHROUTER_AUTO_MODEL_ROUTING_SYSTEM_PROMPT_WEIGHT)',
    )
    p.add_argument(
        '--auto-model-routing-user-prompt-weight',
        dest='auto_model_routing_user_prompt_weight', type=float,
        default=float(os.environ.get(
            'ANTHROUTER_AUTO_MODEL_ROUTING_USER_PROMPT_WEIGHT', '0.80')),
        help='Weight applied to the user-prompt tier score in the weighted blend '
             '(must sum to 1.0 with --auto-model-routing-system-prompt-weight; both > 0). '
             '(default: 0.80, env: ANTHROUTER_AUTO_MODEL_ROUTING_USER_PROMPT_WEIGHT)',
    )
    p.add_argument(
        '--auto-model-routing-trivial-threshold',
        dest='auto_model_routing_trivial_threshold', type=float,
        default=float(os.environ.get(
            'ANTHROUTER_AUTO_MODEL_ROUTING_TRIVIAL_THRESHOLD', '30')),
        help='Weighted-score threshold below which the blended tier is "trivial". '
             'Must be strictly less than --auto-model-routing-standard-threshold. '
             '(default: 30, env: ANTHROUTER_AUTO_MODEL_ROUTING_TRIVIAL_THRESHOLD)',
    )
    p.add_argument(
        '--auto-model-routing-standard-threshold',
        dest='auto_model_routing_standard_threshold', type=float,
        default=float(os.environ.get(
            'ANTHROUTER_AUTO_MODEL_ROUTING_STANDARD_THRESHOLD', '60')),
        help='Weighted-score threshold at/above which the blended tier is "deep"; '
             'between trivial_threshold and this value is "standard". '
             'Must be strictly greater than --auto-model-routing-trivial-threshold. '
             '(default: 60, env: ANTHROUTER_AUTO_MODEL_ROUTING_STANDARD_THRESHOLD)',
    )
    p.add_argument(
        '--auto-model-routing-system-prompt-cache-size',
        dest='auto_model_routing_system_prompt_cache_size', type=int,
        default=int(os.environ.get(
            'ANTHROUTER_AUTO_MODEL_ROUTING_SYSTEM_PROMPT_CACHE_SIZE', '256')),
        help='Maximum number of system-prompt SHA256 → tier-score entries in the '
             'in-memory LRU cache (evicts oldest on overflow). Must be >= 1. '
             '(default: 256, env: ANTHROUTER_AUTO_MODEL_ROUTING_SYSTEM_PROMPT_CACHE_SIZE)',
    )
    p.add_argument(
        '--auto-model-routing-system-prompt-preview-limit',
        dest='auto_model_routing_system_prompt_preview_limit', type=int,
        default=int(os.environ.get(
            'ANTHROUTER_AUTO_MODEL_ROUTING_SYSTEM_PROMPT_PREVIEW_LIMIT', '500')),
        help='Maximum characters of the system prompt sent to the system-prompt '
             'classifier (head-capped). Must be >= 1. '
             '(default: 500, env: ANTHROUTER_AUTO_MODEL_ROUTING_SYSTEM_PROMPT_PREVIEW_LIMIT)',
    )
    p.add_argument('--lock-requested-model', dest='lock_requested_model',
                   default=os.environ.get('ANTHROUTER_LOCK_REQUESTED_MODEL', 'off'),
                   help='Override the incoming request model with a fixed baseline before'
                        ' auto-routing fires. The classifier still runs and routes relative'
                        ' to this baseline (trivial→haiku, deep→opus). "off" disables the'
                        " lock and passes the client's model through unchanged"
                        ' (default: off, env: ANTHROUTER_LOCK_REQUESTED_MODEL)')
    p.add_argument('--sanitize-system-prompt', dest='sanitize_system_prompt',
                   default=os.environ.get('ANTHROUTER_SANITIZE_SYSTEM_PROMPT', 'strip'),
                   choices=['off', 'warn', 'strip'],
                   help='Handle cache-hostile volatile blocks in the inbound system prompt.'
                        ' "strip" removes allowlisted telemetry blocks (currently'
                        ' x-anthropic-billing-header, which carries a per-request cc_prompt_id'
                        ' that invalidates the cached prefix every turn) and warns about'
                        ' unrecognised volatile blocks; "warn" only detects and warns;'
                        ' "off" disables both (default: strip,'
                        ' env: ANTHROUTER_SANITIZE_SYSTEM_PROMPT)')
    p.add_argument('--sse-keepalive-interval', dest='sse_keepalive_interval', type=float,
                   default=float(os.environ.get('ANTHROUTER_SSE_KEEPALIVE_INTERVAL', '10.0')),
                   help='Seconds between SSE keepalive comment lines (": keepalive\\n\\n") sent to'
                        ' the client while waiting for the upstream first byte on streaming'
                        ' requests; 0 disables keepalive (default: 10.0,'
                        ' env: ANTHROUTER_SSE_KEEPALIVE_INTERVAL)')
    p.add_argument('--db-path', dest='db_path',
                   default=os.environ.get('ANTHROUTER_DB_PATH', None),
                   help='Path to SQLite DB for request and routing records'
                        ' (default: ~/.anthrouter/anthrouter.db when --enable-ui is set,'
                        ' env: ANTHROUTER_DB_PATH)')
    p.add_argument('--db-retention-days', dest='db_retention_days', type=int,
                   default=int(os.environ.get('ANTHROUTER_DB_RETENTION_DAYS', '30')),
                   help='Delete request rows older than this many days, pruned'
                        ' opportunistically at insert (at most once per 24h);'
                        ' 0 keeps rows forever (default: 30,'
                        ' env: ANTHROUTER_DB_RETENTION_DAYS)')
    p.add_argument('--enable-ui', dest='enable_ui',
                   action='store_true', default=False,
                   help='Enable the read-only observability API and web UI at /admin/* and /ui/*')
    p.add_argument(
        '--model-aliases',
        dest='model_aliases',
        default=os.environ.get('ANTHROUTER_MODEL_ALIASES', ''),
        help='Comma-separated alias:model pairs overriding or extending the built-in model alias '
             'table. Example: opus:claude-opus-5,mymodel:claude-sonnet-4-6 '
             '(env: ANTHROUTER_MODEL_ALIASES)',
    )
    p.add_argument(
        '--admin-token',
        dest='admin_token',
        default=os.environ.get('ANTHROUTER_ADMIN_TOKEN', None),
        help='Token required in the X-Admin-Token header on POST /admin/config. Unset (default) '
             'disables config writes entirely; that endpoint returns 403 regardless of token. '
             '(env: ANTHROUTER_ADMIN_TOKEN)',
    )

    args = p.parse_args(argv)

    args.auto_model_routing_classification = _parse_classification_str(
        args.auto_model_routing_classification, p
    )
    args.model_aliases = _parse_model_aliases_str(args.model_aliases, p)
    if not (args.auto_model_routing_long or '').strip():
        p.error('--auto-model-routing-long must be a non-empty string or "off"')
    args.auto_model_routing_min_confidence = max(
        0.0, min(1.0, args.auto_model_routing_min_confidence)
    )

    raw_task_tiers = (args.auto_model_routing_task_tiers or '').strip()
    if raw_task_tiers:
        try:
            tiers_parsed = json.loads(raw_task_tiers)
        except json.JSONDecodeError as exc:
            p.error(f'--auto-model-routing-task-tiers: invalid JSON: {exc}')
        if not isinstance(tiers_parsed, dict):
            p.error('--auto-model-routing-task-tiers must be a JSON object mapping '
                    'task names to tier aliases')
        args.auto_model_routing_task_tiers = {
            str(k): str(v) for k, v in tiers_parsed.items()
        }
    else:
        args.auto_model_routing_task_tiers = None

    args.lock_requested_model = (args.lock_requested_model or '').strip() or 'off'
    args.upstream_base_url = (args.upstream_base_url or '').strip().rstrip('/')
    if not args.upstream_base_url:
        p.error('--upstream-base-url must be a non-empty URL')

    cfg = Config(**{f.name: getattr(args, f.name) for f in dataclasses.fields(Config)})

    # Run extracted validation and report errors
    validation_errors = validate_config(cfg)
    if validation_errors:
        # Join all errors and report as a single error message
        p.error('\n'.join(validation_errors))

    cfg.anthrouter_home = _resolve_home(cfg.anthrouter_home)
    if cfg.enable_ui and cfg.db_path is None:
        cfg.db_path = str(Path(cfg.anthrouter_home) / 'anthrouter.db')

    return cfg
