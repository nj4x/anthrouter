"""Shared helpers for Anthropic payload shaping."""

import json


def anthropic_error_payload(type_, message):
    return {
        'type': 'error',
        'error': {
            'type': type_,
            'message': message,
        }
    }


def sse_event(event_type, payload):
    return 'event: %s\ndata: %s\n\n' % (event_type, json.dumps(payload))


class AnthropicRequestError(Exception):
    """A client-facing failure, shaped as an Anthropic error envelope.

    ``connection_error`` marks a transport-level failure (refused connection,
    timeout, TLS error) as distinct from an HTTP response the upstream actually
    sent.
    """

    def __init__(self, message, error_type='invalid_request_error', status_code=400,
                 retry_after=None, connection_error=False):
        super().__init__(message)
        self.message = message
        self.error_type = error_type
        self.status_code = status_code
        self.retry_after = retry_after
        self.connection_error = connection_error


_ALL_THINKING_TYPES = frozenset({'thinking', 'redacted_thinking'})


def strip_all_thinking_blocks(messages):
    """Remove ALL ``thinking`` and ``redacted_thinking`` blocks from history.

    Used as a recovery fallback when Anthropic rejects a thinking block with
    HTTP 400 after a model-tier switch (opus signatures/data are invalid for
    sonnet/haiku and vice versa).  Both ``thinking`` (rejected as "Invalid
    `signature`") and ``redacted_thinking`` (rejected as "Invalid `data`") are
    model-specific and must be stripped together.  Conversation alternation is
    maintained by inserting an empty text block when all content blocks in a
    message were thinking blocks.

    Returns the original list unchanged when nothing was stripped.
    """
    if not messages:
        return messages
    result = []
    changed = False
    for msg in messages:
        if not isinstance(msg, dict) or msg.get('role') != 'assistant':
            result.append(msg)
            continue
        content = msg.get('content')
        if not isinstance(content, list):
            result.append(msg)
            continue
        filtered = [
            b for b in content
            if not (isinstance(b, dict) and b.get('type') in _ALL_THINKING_TYPES)
        ]
        if len(filtered) == len(content):
            result.append(msg)
            continue
        changed = True
        result.append({**msg, 'content': filtered or [{'type': 'text', 'text': ''}]})
    return result if changed else messages


def _count_chars(obj):
    """Recursively sum all string lengths in a nested structure."""
    if isinstance(obj, str):
        return len(obj)
    if isinstance(obj, list):
        return sum(_count_chars(item) for item in obj)
    if isinstance(obj, dict):
        return sum(_count_chars(v) for v in obj.values())
    return 0


# Tool-use scaffolding overhead, in tokens.  When a request declares tools the
# provider injects a sizeable tool-use system preamble plus per-tool framing that
# the ~4-chars/token text heuristic alone misses badly (measured ~5x undercount
# without these).  The constants were calibrated against the Anthropic
# /v1/messages/count_tokens endpoint: a one-time base of ~500 tokens when any
# tools are present, plus ~60 tokens of framing per tool, on top of each tool's
# serialized text.  With these, tool-bearing requests track the official count to
# within ~5% (vs. 4-5x off before).
_TOOL_USE_BASE_OVERHEAD = 500
_TOOL_FRAMING_OVERHEAD = 60


def estimate_input_tokens(payload):
    """Rough token estimate from the Anthropic request payload.

    Uses a ~4-chars/token heuristic over the meaningful content — messages,
    system prompt, and the full serialized tool definitions — plus a calibrated
    tool-use scaffolding overhead (see ``_TOOL_USE_BASE_OVERHEAD`` /
    ``_TOOL_FRAMING_OVERHEAD``) when tools are present.

    Accuracy: tracks the official count_tokens endpoint to within ~10% for
    natural-language prose and tool-bearing requests.  It systematically
    *undercounts* dense code (~1.5-1.8x: code tokenizes to ~2.2 chars/token, not
    4) and non-text blocks such as images count ~0 — both are intrinsic to a
    char-based heuristic and not recoverable without a real tokenizer.  Callers
    that gate on a window threshold should leave headroom for this skew.
    """
    total_chars = 0

    for msg in payload.get('messages') or []:
        total_chars += _count_chars(msg.get('content') or '')

    system = payload.get('system')
    if system:
        total_chars += _count_chars(system)

    tools = payload.get('tools') or []
    for tool in tools:
        # Count the whole tool structure (name, description, input_schema, and
        # any other fields) rather than three hand-picked keys.
        total_chars += _count_chars(tool)

    tokens = total_chars // 4
    if tools:
        tokens += _TOOL_USE_BASE_OVERHEAD + _TOOL_FRAMING_OVERHEAD * len(tools)

    return max(1, tokens)


def system_content_str(system):
    """Canonical serialization of a ``system`` field, or None if unserializable.

    The single definition every hash of a system prompt goes through, so the
    original and sanitized digests are always comparable.
    """
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        return json.dumps(system, sort_keys=True, ensure_ascii=False)
    return None


# Prefixes of system blocks that are known-safe to drop wholesale.  A block is
# removed only when its text starts with one of these; anything else passes
# through untouched however volatile it looks, because dropping unrecognised
# content is undebuggable from the client side.  Additions here are deliberate
# code changes, not configuration (ADR-0029).
#
# ``x-anthropic-billing-header:`` is CLI telemetry (cc_version, cc_entrypoint,
# cch, cc_prompt_id).  The model never reads it, and since 2026-08-21 it carries
# a per-request ``cc_prompt_id`` UUID that invalidates the cached prefix on
# every turn.
#
# Matching is by prefix, not exact block equality: a client system prompt that
# happens to start its own text with this literal string would also be
# dropped.  Accepted — the string is CLI-internal telemetry syntax, vanishingly
# unlikely to occur as the start of genuine user content.
VOLATILE_SYSTEM_BLOCK_PREFIXES = (
    'x-anthropic-billing-header:',
)


def strip_volatile_system_blocks(system):
    """Drop allowlisted cache-hostile telemetry blocks from a ``system`` field.

    Prompt caching matches on a prefix, so a block carrying a per-request unique
    value at position 0 invalidates the whole cached prefix every turn.  Cache
    writes bill at 1.25x input and reads at 0.1x, making each displaced token
    12.5x more expensive.

    Returns ``(system, stripped)``.  The original object is returned unchanged
    when nothing matched, when ``system`` is a bare string, or when it is empty.
    Dropping every block is allowed: an all-telemetry system field legitimately
    sanitizes to nothing, and an empty list is treated the same as an absent
    field downstream.
    """
    if not system or not isinstance(system, list):
        return system, False
    kept = [b for b in system if not _is_volatile_system_block(b)]
    if len(kept) == len(system):
        return system, False
    return kept, True


def _is_volatile_system_block(block):
    if not isinstance(block, dict):
        return False
    text = block.get('text')
    if not isinstance(text, str):
        return False
    stripped = text.lstrip()
    return any(
        stripped.startswith(prefix) for prefix in VOLATILE_SYSTEM_BLOCK_PREFIXES
    )
