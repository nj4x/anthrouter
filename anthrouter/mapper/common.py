"""Shared helpers for Anthropic payload shaping."""


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
