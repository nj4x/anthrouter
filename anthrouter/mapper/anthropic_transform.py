"""Outbound Anthropic Messages request shaping.

Alias resolution, beta merging, and body construction.  anthrouter forwards the
client's own request essentially as it arrived — it injects no system blocks, no
required betas, and no cache breakpoints, because the client already speaks
native Anthropic and holds its own credential.

What it does do is model-aware sanitization, and only because it *changes the
model*: a payload valid for the tier the client asked for can be rejected with
HTTP 400 by the tier the router picked.  Each gate below fails open — a model
family absent from a list keeps its field and surfaces the 400.  The one
exception is ``fallbacks``, which is stripped unconditionally: no tier is
modeled as accepting a fallback list chosen for a different model (ADR-0013).
"""

import json
import logging

from ..model_config import MODEL_OUTPUT_LIMITS, resolve_model

logger = logging.getLogger(__name__)

ANTHROPIC_VERSION = '2023-06-01'
MESSAGES_PATH = '/v1/messages?beta=true'
COUNT_TOKENS_PATH = '/v1/messages/count_tokens?beta=true'


def _supports_effort(model_id: str) -> bool:
    """Haiku rejects top-level ``output_config.effort`` with HTTP 400; other tiers accept it.

    Fable accepts every top-level effort level.  Its per-message effort is gated
    separately by ``_supports_per_message_effort``.
    """
    return 'haiku' not in model_id.lower()


# Beta token enabling per-message ``output_config`` (effort inside ``messages[]``),
# e.g. 'mid-conversation-output-config-2026-07-01'.  Prefix-matched so the gate
# survives date-stamp revisions.
_PER_MESSAGE_EFFORT_BETA_PREFIX = 'mid-conversation-output-config'


def _supports_per_message_effort(model_id: str, betas: list[str]) -> bool:
    """Fable rejects per-message ``output_config.effort`` unless the client sent the beta.

    The 400 reads ``output_config.effort requires a model that supports per-turn
    effort; this model does not``.  With the beta present Fable accepts it, so
    the field is kept.
    """
    if 'fable' not in model_id.lower():
        return True
    return any(beta.startswith(_PER_MESSAGE_EFFORT_BETA_PREFIX) for beta in betas)


def _supports_adaptive_thinking(model_id: str) -> bool:
    """Haiku rejects ``thinking.type='adaptive'`` but accepts manual ``'enabled'``."""
    return 'haiku' not in model_id.lower()


def _supports_disabled_thinking(model_id: str) -> bool:
    """Fable rejects ``thinking.type='disabled'``; an absent field defaults to adaptive."""
    return 'fable' not in model_id.lower()


def _requires_between_tools(model_id: str) -> bool:
    """Sonnet 5.5 rejects ``thinking.type='disabled'`` and takes ``'between_tools'`` instead.

    Sonnet 5 and earlier still accept ``'disabled'``.  Matched as a substring so
    dated IDs are covered.
    """
    return 'sonnet-5-5' in model_id.lower()


# Efforts at which Sonnet 5.5 rejects 'between_tools' as well; there the field
# is dropped so the model falls back to its adaptive default.
_BETWEEN_TOOLS_REJECTING_EFFORTS = frozenset({'xhigh', 'max'})


_SAMPLING_CONTROL_KEYS = frozenset({'temperature', 'top_p', 'top_k'})

# Families using fixed sampling, which reject any non-default
# temperature/top_p/top_k.  Matched as substrings of the resolved ID so dated
# variants are covered.  Keep specific: bare 'sonnet' would wrongly strip from
# Sonnet 4.5, which still accepts them.
_FIXED_SAMPLING_FAMILIES = ('opus-4-7', 'opus-4-8', 'opus-5', 'sonnet-4-6', 'fable')


def _supports_sampling_controls(model_id: str) -> bool:
    model = model_id.lower()
    return not any(family in model for family in _FIXED_SAMPLING_FAMILIES)


# Families that answer HTTP 400 to role:'system' entries in messages[], matched
# as substrings of the resolved ID.  Members get their inline system turns
# rewritten into user turns by ``_rewrite_inline_system`` (ADR-0010).  Fails
# open: a family absent here keeps its inline system turns, cache_control
# included; the transport's one-shot forced-rewrite retry recovers a rejection
# and warns so the family can be added.
_SYSTEM_ROLE_REJECTING_FAMILIES = (
    'sonnet-4-6', 'claude-4-5-sonnet', 'claude-sonnet-4-6', 'claude-haiku-4-5-20251001', 'haiku',
)


def _rejects_system_role_in_messages(model_id: str) -> bool:
    """Return True iff the resolved model rejects role:'system' in messages[].

    Sonnet 5.5 and Opus 5.5 accept inline system turns natively and receive
    them unchanged.
    """
    model = model_id.lower()
    return any(family in model for family in _SYSTEM_ROLE_REJECTING_FAMILIES)


# Long-context beta tokens, e.g. 'context-1m-2025-08-07'.  Prefix-matched so the
# gate survives date-stamp revisions.
_LONG_CONTEXT_BETA_PREFIX = 'context-1m'

# Server-side fallback beta tokens, stripped alongside the ``fallbacks`` body
# field in ``build_body`` (see ADR-0013).  Prefix-matched like the long-context
# beta.
_FALLBACK_BETA_PREFIX = 'server-side-fallback'


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
        if beta.startswith(_FALLBACK_BETA_PREFIX):
            logger.debug('Dropped fallback beta %s: fallbacks are never sent', beta)
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


def _strip_per_message_effort(body: dict) -> None:
    """Drop ``output_config.effort`` from each entry of ``messages`` in the shallow copy.

    Rebuilds the affected entries rather than editing them, so the caller's
    payload is never mutated.  Entries that are not dicts, or whose
    ``output_config`` is not a dict, cross untouched.
    """
    messages = body.get('messages')
    if not isinstance(messages, list):
        return
    rebuilt: list = []
    dropped = 0
    for entry in messages:
        oc = entry.get('output_config') if isinstance(entry, dict) else None
        if not (isinstance(oc, dict) and 'effort' in oc):
            rebuilt.append(entry)
            continue
        oc = {k: v for k, v in oc.items() if k != 'effort'}
        entry = {k: v for k, v in entry.items() if k != 'output_config'}
        if oc:
            entry['output_config'] = oc
        rebuilt.append(entry)
        dropped += 1
    if not dropped:
        return
    body['messages'] = rebuilt
    logger.debug('Dropped per-message output_config.effort from %d message(s) for model %s',
                 dropped, body.get('model'))


def _stringify_content(content) -> str:
    """Flatten an Anthropic message ``content`` to plain text.

    A string is returned as-is; a list of blocks is reduced to its ``type=='text'``
    block texts joined by newlines.  Used to rewrite inline ``role:'system'``
    turns into user-turn reminder blocks.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            blk['text'] for blk in content
            if isinstance(blk, dict) and blk.get('type') == 'text'
            and isinstance(blk.get('text'), str)
        ]
        return '\n'.join(parts)
    return ''


def _system_message_cache_control(message: dict):
    """Return the last ``cache_control`` on an inline system message, or None.

    Checked at message level first, then on each text block, so a block-level
    breakpoint (the shape Claude Code 5.x sends) wins.
    """
    found = message.get('cache_control')
    content = message.get('content')
    if isinstance(content, list):
        for blk in content:
            if isinstance(blk, dict) and blk.get('type') == 'text' and 'cache_control' in blk:
                found = blk['cache_control']
    return found


def _has_tool_use(message: dict) -> bool:
    content = message.get('content')
    return isinstance(content, list) and any(
        isinstance(blk, dict) and blk.get('type') == 'tool_use' for blk in content
    )


def _rewrite_inline_system(messages: list, model: str) -> list:
    """Rewrite inline role:'system' messages as user-turn reminder blocks (ADR-0010).

    Each system message becomes one ``<system-reminder>``-wrapped text block
    carrying the message's ``cache_control``.  The block is appended to the user
    message directly before it; after an assistant ``tool_use`` turn it is
    dropped, because a user turn there would separate the tool call from its
    result; anywhere else it becomes a standalone user message.  The
    message-level ``output_config`` is never copied.  Returns the input list
    itself when it holds no system message, otherwise a new list; the caller's
    messages are never mutated.
    """
    if not any(isinstance(m, dict) and m.get('role') == 'system' for m in messages):
        return messages
    out: list = []
    for msg in messages:
        if not (isinstance(msg, dict) and msg.get('role') == 'system'):
            out.append(msg)
            continue
        if 'output_config' in msg:
            logger.debug(
                'Dropped message-level output_config from inline system message for model %s',
                model,
            )
        text = _stringify_content(msg.get('content'))
        if not text:
            continue
        block = {'type': 'text', 'text': f'<system-reminder>\n{text}\n</system-reminder>'}
        cache_control = _system_message_cache_control(msg)
        if cache_control is not None:
            block['cache_control'] = cache_control

        prev = out[-1] if out else None
        if isinstance(prev, dict) and prev.get('role') == 'user':
            content = prev.get('content')
            if isinstance(content, str):
                content = [{'type': 'text', 'text': content}] if content else []
            elif not isinstance(content, list):
                content = []
            out[-1] = {**prev, 'content': [*content, block]}
        elif isinstance(prev, dict) and prev.get('role') == 'assistant' and _has_tool_use(prev):
            logger.warning(
                'Dropped inline system message after an assistant tool_use turn for model %s '
                '(cache_control=%s); a user turn there would split the tool call from its result',
                model, cache_control is not None,
            )
        else:
            out.append({'role': 'user', 'content': [block]})
    return out


_INTERNAL_KEYS = frozenset({'_anthropic_beta', '_anthproxy_internal_classifier'})


def build_body(payload: dict, aliases: dict[str, str] | None = None, *,
               force_rewrite: bool = False) -> bytes:
    """Serialize the outbound request body.

    Internal keys are removed, the model is resolved, and fields the resolved
    model would reject are dropped.  Everything else crosses as the client sent
    it.

    Args:
        force_rewrite: rewrite inline role:'system' messages regardless of
            ``_rejects_system_role_in_messages``; set by the transport when a
            model outside ``_SYSTEM_ROLE_REJECTING_FAMILIES`` rejected them.
    """
    body = {k: v for k, v in payload.items() if k not in _INTERNAL_KEYS}
    body['model'] = resolve_model(payload.get('model', ''), aliases=aliases)

    # Unconditional, not model-gated: Anthropic validates each fallback target
    # against the primary model, and routing may have changed that model after
    # the client chose its targets.  Pairs with the beta strip in merge_betas.
    if body.pop('fallbacks', None) is not None:
        logger.debug('Dropped fallbacks for model %s', body['model'])

    ceiling = MODEL_OUTPUT_LIMITS.get(body['model'])
    max_tokens = body.get('max_tokens')
    if ceiling is not None and isinstance(max_tokens, int) and max_tokens > ceiling:
        body['max_tokens'] = ceiling
        logger.debug('Clamped max_tokens %d to %d for model %s',
                     max_tokens, ceiling, body['model'])

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

    if not _supports_per_message_effort(body['model'], payload.get('_anthropic_beta') or []):
        _strip_per_message_effort(body)

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

    # Runs after the effort strip so the effort read here is the one the request
    # will carry.  Disjoint from the haiku/fable gates above — Anthropic model IDs
    # never embed both 'sonnet-5-5' and ('haiku'|'fable') — so no real payload hits both.
    if _requires_between_tools(body['model']):
        thinking = body.get('thinking')
        if isinstance(thinking, dict) and thinking.get('type') == 'disabled':
            oc = body.get('output_config')
            effort = oc.get('effort') if isinstance(oc, dict) else None
            if effort in _BETWEEN_TOOLS_REJECTING_EFFORTS:
                body.pop('thinking', None)
                logger.debug('Dropped disabled thinking for model %s (effort=%s)',
                             body['model'], effort)
            else:
                body['thinking'] = {'type': 'between_tools'}
                logger.debug('Rewrote disabled thinking to between_tools for model %s (effort=%s)',
                             body['model'], effort)

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

    # Last because it is the only gate that touches messages[]; the gates above
    # touch only top-level fields, so the two never interact.  Never folded into
    # top-level system: that grows system[] one block per turn and defeats the
    # prompt cache.  The rewrite keeps each message's position and breakpoint,
    # so the cached prefix stays append-only (ADR-0010).
    messages = body.get('messages')
    if (force_rewrite or _rejects_system_role_in_messages(body['model'])) \
            and isinstance(messages, list):
        body['messages'] = _rewrite_inline_system(messages, body['model'])

    return json.dumps(body).encode('utf-8')
