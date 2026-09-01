"""Outbound Anthropic Messages request shaping.

Alias resolution, beta merging, and body construction.  anthrouter forwards the
client's own request essentially as it arrived — it injects no system blocks, no
required betas, and no cache breakpoints, because the client already speaks
native Anthropic and holds its own credential.

What it does do is model-aware sanitization, and only because it *changes the
model*: a payload valid for the tier the client asked for can be rejected with
HTTP 400 by the tier the router picked.  Each gate below fails open — a model
family absent from a list keeps its field and surfaces the 400.
"""

import json
import logging

from ..model_config import resolve_model

logger = logging.getLogger(__name__)

ANTHROPIC_VERSION = '2023-06-01'
MESSAGES_PATH = '/v1/messages?beta=true'
COUNT_TOKENS_PATH = '/v1/messages/count_tokens?beta=true'


def _supports_effort(model_id: str) -> bool:
    """Haiku rejects ``output_config.effort`` with HTTP 400; other tiers accept it."""
    return 'haiku' not in model_id.lower()


def _supports_adaptive_thinking(model_id: str) -> bool:
    """Haiku rejects ``thinking.type='adaptive'`` but accepts manual ``'enabled'``."""
    return 'haiku' not in model_id.lower()


def _supports_disabled_thinking(model_id: str) -> bool:
    """Fable rejects ``thinking.type='disabled'``; an absent field defaults to adaptive."""
    return 'fable' not in model_id.lower()


_SAMPLING_CONTROL_KEYS = frozenset({'temperature', 'top_p', 'top_k'})

# Families using fixed sampling, which reject any non-default
# temperature/top_p/top_k.  Matched as substrings of the resolved ID so dated
# variants are covered.  Keep specific: bare 'sonnet' would wrongly strip from
# Sonnet 4.5, which still accepts them.
_FIXED_SAMPLING_FAMILIES = ('opus-4-7', 'opus-4-8', 'opus-5', 'sonnet-4-6', 'fable')


def _supports_sampling_controls(model_id: str) -> bool:
    model = model_id.lower()
    return not any(family in model for family in _FIXED_SAMPLING_FAMILIES)


# Long-context beta tokens, e.g. 'context-1m-2025-08-07'.  Prefix-matched so the
# gate survives date-stamp revisions.
_LONG_CONTEXT_BETA_PREFIX = 'context-1m'


def _supports_long_context(model_id: str) -> bool:
    """Only Opus may carry the 1m context beta.

    Haiku has a 200k window and rejects it with HTTP 400; Sonnet answers HTTP 429
    ("Usage credits are required for long context requests").  Keyed on the
    resolved model so a routing decision toward a lower tier never forwards an
    unusable beta.
    """
    return 'opus' in model_id.lower()


# Betas requiring thinking to be enabled or adaptive.
_THINKING_REQUIRED_BETAS = frozenset({'clear_thinking_20251015'})

# Context-management edit types with the same requirement, prefix-matched.
_THINKING_REQUIRED_EDIT_PREFIX = 'clear_thinking'


def _thinking_active(payload: dict, resolved_model: str) -> bool:
    """Whether thinking will be active outbound, mirroring the strips in ``build_body``."""
    thinking = payload.get('thinking')
    if not isinstance(thinking, dict):
        return False
    t_type = thinking.get('type')
    if t_type == 'enabled':
        return True
    if t_type == 'adaptive':
        return _supports_adaptive_thinking(resolved_model)
    return False


def merge_betas(payload: dict, aliases: dict[str, str] | None = None) -> str:
    """Build the outbound ``anthropic-beta`` value from the client's own betas.

    Returns an empty string when the client asked for none, in which case the
    header is omitted entirely rather than sent blank.
    """
    raw_model = payload.get('model') or ''
    resolved_model = resolve_model(raw_model, aliases=aliases) if raw_model else ''
    active = _thinking_active(payload, resolved_model)
    long_context_ok = _supports_long_context(resolved_model)
    betas: list[str] = []
    seen: set[str] = set()
    for beta in payload.get('_anthropic_beta') or []:
        if not active and beta in _THINKING_REQUIRED_BETAS:
            logger.debug('Dropped thinking beta %s: thinking not active for model %s',
                         beta, resolved_model)
            continue
        if not long_context_ok and beta.startswith(_LONG_CONTEXT_BETA_PREFIX):
            logger.debug('Dropped long-context beta %s: 1m context not supported for model %s',
                         beta, resolved_model)
            continue
        if beta not in seen:
            betas.append(beta)
            seen.add(beta)
    return ','.join(betas)


def _strip_thinking_edits(body: dict) -> None:
    """Drop ``clear_thinking*`` edits from ``context_management`` in the shallow copy.

    Rebuilds the nested structures rather than editing them, so the caller's
    payload is never mutated.
    """
    cm = body.get('context_management')
    if not isinstance(cm, dict):
        return
    edits = cm.get('edits')
    if not isinstance(edits, list):
        return
    kept = [
        e for e in edits
        if not (isinstance(e, dict)
                and isinstance(e.get('type'), str)
                and e['type'].startswith(_THINKING_REQUIRED_EDIT_PREFIX))
    ]
    if len(kept) == len(edits):
        return
    siblings = {k: v for k, v in cm.items() if k != 'edits'}
    if kept:
        body['context_management'] = {**siblings, 'edits': kept}
    elif siblings:
        body['context_management'] = siblings
    else:
        body.pop('context_management', None)
    logger.debug('Dropped clear_thinking context-management edit(s) for model %s',
                 body.get('model'))


_INTERNAL_KEYS = frozenset({'_anthropic_beta', '_anthproxy_internal_classifier'})


def build_body(payload: dict, aliases: dict[str, str] | None = None) -> bytes:
    """Serialize the outbound request body.

    Internal keys are removed, the model is resolved, and fields the resolved
    model would reject are dropped.  Everything else crosses as the client sent
    it.
    """
    body = {k: v for k, v in payload.items() if k not in _INTERNAL_KEYS}
    body['model'] = resolve_model(payload.get('model', ''), aliases=aliases)

    if not _supports_effort(body['model']):
        oc = body.get('output_config')
        if isinstance(oc, dict) and 'effort' in oc:
            oc = {k: v for k, v in oc.items() if k != 'effort'}
            if oc:
                body['output_config'] = oc
            else:
                body.pop('output_config', None)
            logger.debug('Dropped unsupported output_config.effort for model %s',
                         body['model'])

    if not _supports_adaptive_thinking(body['model']):
        thinking = body.get('thinking')
        if isinstance(thinking, dict) and thinking.get('type') == 'adaptive':
            body.pop('thinking', None)
            logger.debug('Dropped unsupported adaptive thinking for model %s', body['model'])

    if not _supports_disabled_thinking(body['model']):
        thinking = body.get('thinking')
        if isinstance(thinking, dict) and thinking.get('type') == 'disabled':
            body.pop('thinking', None)
            logger.debug('Dropped unsupported disabled thinking for model %s', body['model'])

    # Pairs with the clear_thinking beta strip in merge_betas — both keyed on
    # _thinking_active — so the body strategy can never outlive the thinking it
    # depends on.
    if not _thinking_active(payload, body['model']):
        _strip_thinking_edits(body)

    if not _supports_sampling_controls(body['model']):
        dropped = [k for k in _SAMPLING_CONTROL_KEYS if k in body]
        for k in dropped:
            body.pop(k, None)
        if dropped:
            logger.debug('Dropped unsupported sampling controls for model %s: %s',
                         body['model'], ','.join(sorted(dropped)))

    return json.dumps(body).encode('utf-8')
