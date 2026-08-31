"""Model-alias table and resolution for the Anthropic upstream.

The router emits tier aliases (``haiku`` / ``sonnet`` / ``opus``) and clients may
send either an alias or a full model ID.  Everything reaching the wire goes
through :func:`resolve_model`; unknown names pass through verbatim so a model
shipped after this table was written still works.
"""

from .mapper.common import AnthropicRequestError

MODEL_ALIASES: dict[str, str] = {
    'fable': 'claude-fable-5',
    'opus': 'claude-opus-5',
    'sonnet': 'claude-sonnet-4-6',
    'haiku': 'claude-haiku-4-5-20251001',
}

# Tier -> (input, output, cache_read, cache_write) USD per million tokens.
MODEL_PRICING: dict[str, tuple[float, float, float, float]] = {
    'fable': (10.0, 30.0, 1.00, 12.50),
    'opus': (5.0, 25.0, 0.50, 6.25),
    'sonnet': (3.0, 15.0, 0.30, 3.75),
    'haiku': (1.0, 5.0, 0.10, 1.25),
}

# Context-window variant suffixes stripped before alias lookup: the 1m window is
# requested via the ``context-1m`` beta, not a distinct upstream model ID.
CONTEXT_SUFFIXES: tuple[str, ...] = (':1m', '[1m]')


def resolve_model(model: str) -> str:
    """Resolve an alias or full model ID to the upstream Anthropic model ID."""
    if not model:
        raise AnthropicRequestError('model is required', status_code=400)
    if model in MODEL_ALIASES:
        return MODEL_ALIASES[model]
    for suffix in CONTEXT_SUFFIXES:
        if model.endswith(suffix):
            base = model[:-len(suffix)]
            if base in MODEL_ALIASES:
                return MODEL_ALIASES[base]
    return model
