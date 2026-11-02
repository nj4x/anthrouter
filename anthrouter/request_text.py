"""Shared helper for extracting user-visible text from Anthropic message payloads.

Both ``anthproxy.handlers`` (local-command matching) and ``anthproxy.model_router``
(complexity classification) need to strip ``<system-reminder>`` and ``<transcript>``
blocks injected by the Claude Code CLI before inspecting the final user message.
This module owns the regexes and the stripping logic so both callers stay in sync.
It also owns ``strip_attribution_reminders``, which rewrites outbound user messages
(ADR-0023).
"""

import re

# Claude Code CLI injects these wrapper blocks into the final user message.
# Keep ``system-reminder`` in ``_WRAPPER_TAGS`` so handlers can still recognize
# the exact unclosed opener in its final-line guard, but strip it with a more
# tolerant regex than the command-wrapper audit tags.
_WRAPPER_TAGS = (
    'system-reminder',
    'local-command-caveat',
    'command-name',
    'command-message',
    'command-args',
    'local-command-stdout',
    'local-command-stderr',
)
_LOCAL_COMMAND_WRAPPER_TAGS = tuple(
    tag for tag in _WRAPPER_TAGS if tag != 'system-reminder'
)
# Claude Code may vary the wrapper shape slightly (attributes, whitespace, case)
# and can occasionally leave the closing tag off.  Treat any ``system-reminder``
# block as injected context and strip it so classifier and local-command parsing
# only see the real user text.
_SYSTEM_REMINDER_RE = re.compile(
    r'<system-reminder\b[^>]*>.*?</system-reminder\s*>|<system-reminder\b[^>]*>.*',
    re.DOTALL | re.IGNORECASE,
)
# The local-command wrapper tags are different: they are audit/context blocks
# around slash commands, and malformed/unclosed variants should remain visible
# so the command matcher rejects them as embedded prose.
_LOCAL_COMMAND_WRAPPER_RE = re.compile(
    r'<(' + '|'.join(re.escape(tag) for tag in _LOCAL_COMMAND_WRAPPER_TAGS) + r')>.*?</\1>',
    re.DOTALL,
)

# <transcript>…</transcript> wraps prior conversation that Claude Code embeds in
# the final user message (e.g. when a skill or sub-agent runs).  It is history,
# not the current request: strip it before classification and command-matching so
# the accumulating transcript does not bias the classifier toward higher tiers.
# Unlike _REMINDER_RE, this regex tolerates optional tag attributes and an
# unclosed block (strips to end of string when </transcript> is absent).
_TRANSCRIPT_RE = re.compile(
    r'<transcript\b[^>]*>.*?</transcript>|<transcript\b[^>]*>.*',
    re.DOTALL,
)

# Role-marker pattern used inside transcript blocks.  Captures the role name
# (User/Human/Assistant) and the text that follows it up to the next marker.
# Written permissively so a format mismatch degrades to tail-slice extraction.
_TRANSCRIPT_TURN_RE = re.compile(
    r'(?:^|\n)\s*(User|Human|Assistant)\s*:\s*(.*?)(?=\n\s*(?:User|Human|Assistant)\s*:|\Z)',
    re.DOTALL | re.IGNORECASE,
)

# Classifier fallback only: when the final user message is transcript-only,
# bound the recovered tail so a large prior turn cannot bias routing upward.
_TRANSCRIPT_FALLBACK_LIMIT = 1_000

# Short-affirmation detection (model-tier routing).  A final user turn that is a
# bare confirmation ("yes", "go ahead", "proceed") carries no complexity signal:
# it merely greenlights work the prior turns already established.  The classifier,
# seeing only that text, would label it "trivial" → haiku, and that decision would
# poison the session tier cache.  ``is_short_affirmation`` lets the router treat
# such a turn as a continuation (inherit the cached tier / floor to standard)
# instead.  Conservative by design: a curated phrase set, not a length heuristic,
# so short-but-substantive instructions ("fix the bug", "delete auth.py") are
# never matched.
_AFFIRMATION_MAX_CHARS = 40
_AFFIRMATION_PHRASES = frozenset({
    'yes', 'yep', 'yeah', 'yup', 'ok', 'okay', 'sure',
    'proceed', 'go', 'go ahead', 'go for it', 'please go ahead',
    'do it', 'please do', 'continue', 'please continue',
    'confirmed', 'sounds good', 'lgtm', 'ship it', 'make it so',
    'run it', 'please start',
})
# Trailing punctuation stripped before membership test.
_AFFIRMATION_TRAILING_PUNCT = '.!?,;: '
# Closed token set for compound affirmations ("yes please", "ok go ahead"):
# every whitespace token must come from this set, so short-but-substantive
# instructions ("do it anyway", "yes park here") still reach the classifier.
_AFFIRMATION_TOKENS = frozenset({
    'yes', 'yep', 'yeah', 'ok', 'okay', 'sure', 'please', 'go', 'ahead',
    'do', 'it', 'continue', 'proceed', 'start',
})
_AFFIRMATION_MAX_TOKENS = 4
# At least one token must carry assent on its own, so filler-only text
# ("it", "do", "ahead", "please") never counts as an approval.
_AFFIRMATION_ANCHOR_TOKENS = frozenset({
    'yes', 'yep', 'yeah', 'ok', 'okay', 'sure', 'go', 'continue', 'proceed',
})


def is_short_affirmation(text: str) -> bool:
    """Return True iff *text* is a bare confirmation/continuation phrase.

    Deliberately conservative: rejects anything longer than
    ``_AFFIRMATION_MAX_CHARS`` (so "yes, but also rename the class" never
    matches), then normalizes (lowercase, collapse interior whitespace, strip
    trailing punctuation, drop a single leading "please ") and tests membership
    against the curated ``_AFFIRMATION_PHRASES`` set.  Compound affirmations
    such as "yes please" or "ok go ahead" also match when the normalized text
    is at most ``_AFFIRMATION_MAX_TOKENS`` whitespace tokens (each stripped of
    edge punctuation, so "yes, please" counts) and every token
    belongs to the closed ``_AFFIRMATION_TOKENS`` set, with at least one
    token from ``_AFFIRMATION_ANCHOR_TOKENS``.

    The bare imperative "start" is intentionally excluded — alone it is most
    often a real command ("start the server/migration"); "please start" is kept
    because the softener makes the imperative reading unlikely, and the
    token-set rule likewise rejects a normalized text of exactly "start".
    """
    if not isinstance(text, str):
        return False
    if len(text) > _AFFIRMATION_MAX_CHARS:
        return False
    normalized = ' '.join(text.lower().split()).strip(_AFFIRMATION_TRAILING_PUNCT)
    if normalized.startswith('please '):
        candidate = normalized[len('please '):].strip()
        if candidate in _AFFIRMATION_PHRASES:
            return True
    if normalized in _AFFIRMATION_PHRASES:
        return True
    tokens = [t.strip(_AFFIRMATION_TRAILING_PUNCT) for t in normalized.split()]
    tokens = [t for t in tokens if t]
    if not tokens or len(tokens) > _AFFIRMATION_MAX_TOKENS:
        return False
    return (
        all(token in _AFFIRMATION_TOKENS for token in tokens)
        and any(token in _AFFIRMATION_ANCHOR_TOKENS for token in tokens)
    )


# Title-generation prompts are appended by the Claude Code IDE to ask for a
# short session title.  The payload wraps prior session text in <session>…</session>
# and ends with a fixed instruction beginning with this prefix.  The task is
# always trivial (one short label), regardless of the session content inside the
# wrapper, so the classifier should not be invoked.
_TITLE_GEN_PREFIX = 'write the title in the predominant language'


def is_title_generation(text: str) -> bool:
    """Return True iff *text* is a session-title generation prompt."""
    if not isinstance(text, str):
        return False
    # Strip trailing whitespace so a trailing \n doesn't yield an empty last line.
    lowered = text.lower().rstrip()
    last_line = lowered.rsplit('\n', 1)[-1].lstrip()
    return last_line.startswith(_TITLE_GEN_PREFIX)


# Claude Code asks for a 3-5 word activity label for each live sub-agent.  The
# final user turn starts with this fixed prefix (ADR-0015).  Detection is a
# plain case-sensitive prefix match after reminder stripping.
ACTIVITY_LABEL_PREFIX = 'Describe your most recent action in 3-5 words'

# The local reply to an activity-label prompt, picked per ask so the label
# moves instead of sitting on one word.  anthrouter keeps no conversation
# summary to answer from, so every label is one of these (ADR-0015).
ACTIVITY_LABEL_FALLBACKS = (
    'Discombobulating', 'Recombobulating', 'Cooking', 'Pondering', 'Tinkering',
    'Noodling', 'Puzzling', 'Brewing', 'Wrangling', 'Untangling',
)


def is_activity_label_prompt(text: str) -> bool:
    """Return True iff *text* is a Claude Code activity-label prompt.

    *text* is the final user turn's raw text.  Reminder blocks are stripped
    first, then the prefix must match at the start.
    """
    if not isinstance(text, str):
        return False
    return strip_reminders(text).startswith(ACTIVITY_LABEL_PREFIX)


def strip_reminders(text: str) -> str:
    """Remove Claude Code wrapper blocks and trim outer whitespace.

    Covers ``<system-reminder>``, ``<transcript>`` (prior conversation history
    embedded by skills/sub-agents), and the local-command wrappers Claude Code
    injects around slash commands (see ``_WRAPPER_TAGS``).

    Transcript and system-reminder blocks are stripped even when the opening tag
    carries attributes or the closing tag is missing.  The remaining local-
    command wrapper blocks are removed only when properly closed; malformed
    variants stay visible so the local-command matcher still rejects them as
    embedded prose.
    """
    text = _TRANSCRIPT_RE.sub('', text)
    text = _SYSTEM_REMINDER_RE.sub('', text)
    return _LOCAL_COMMAND_WRAPPER_RE.sub('', text).strip()


def last_transcript_user_turn(text: str) -> str:
    """Best-effort extraction of the most recent user turn from a transcript block.

    Returns the **trailing** ``_TRANSCRIPT_FALLBACK_LIMIT`` chars of the last
    ``User:`` / ``Human:`` turn found inside any ``<transcript>`` block in
    *text*.  Returns ``''`` when no transcript is present or no recoverable
    user turn can be parsed.

    Used only as a classifier fallback when ``strip_reminders()`` leaves an
    empty string (i.e. the final user message consists entirely of transcript
    with no additional instruction).  Bounding to the tail keeps large prior
    turns from biasing the classifier toward higher tiers.  Degrades
    gracefully: if role markers cannot be parsed, returns the trailing
    ``_TRANSCRIPT_FALLBACK_LIMIT`` characters of the transcript inner content
    rather than nothing.
    """
    # Find the last (or only) transcript block's inner content.
    inner = ''
    for m in _TRANSCRIPT_RE.finditer(text):
        raw = m.group(0)
        # Strip the opening tag to get inner content.
        inner_start = raw.index('>') + 1
        # Strip the closing tag if present.
        if raw.endswith('</transcript>'):
            inner = raw[inner_start:-len('</transcript>')]
        else:
            inner = raw[inner_start:]

    if not inner:
        return ''

    # Walk role-marker splits; keep the last user/human turn's text.
    last_user_text = ''
    for m in _TRANSCRIPT_TURN_RE.finditer(inner):
        role = m.group(1).lower()
        if role in ('user', 'human'):
            last_user_text = m.group(2).strip()

    if last_user_text:
        return last_user_text[-_TRANSCRIPT_FALLBACK_LIMIT:]

    # Fallback: no parseable markers — return the tail slice so we still have
    # something recent to classify on.
    tail = inner.strip()
    return tail[-_TRANSCRIPT_FALLBACK_LIMIT:]


# Opening text of the reminder Claude Code appends to user messages to make the
# agent end commits with ``Co-Authored-By`` and PR bodies with ``Generated with``.
# Matched by this exact opening only: the user's own CLAUDE.md and memory also
# arrive as ``<system-reminder>`` blocks and may mention those strings.
# Additions are deliberate code changes, not configuration (ADR-0023).
ATTRIBUTION_REMINDER_PREFIX = 'Attribution for git commits and pull requests'

# A closed span whose body starts with the prefix.  The body may not contain
# another opener, so an unclosed attribution reminder never runs on into the
# next reminder's closing tag: it does not match at all (fail open).
# Only the tags are case-insensitive; the prefix is matched exactly.  The
# opener's attribute scan is bounded so a run of unterminated openers stays linear.
_ATTRIBUTION_REMINDER_RE = re.compile(
    r'(?i:<system-reminder(?=[\s>])[^>]{0,512}>)\s*' + re.escape(ATTRIBUTION_REMINDER_PREFIX)
    + r'(?:(?!(?i:<system-reminder(?=[\s>]))).)*?(?i:</system-reminder\s*>)',
    re.DOTALL,
)


def _is_blank_text_block(block):
    return (isinstance(block, dict) and block.get('type') == 'text'
            and isinstance(block.get('text'), str) and not block['text'].strip())


def _strip_attribution_content(content):
    """Return ``(content, removed)`` for one user message's content."""
    if isinstance(content, str):
        new, n = _ATTRIBUTION_REMINDER_RE.subn('', content)
        if not n or not new.strip():
            # Empty user content is rejected upstream; keep the message whole.
            return content, 0
        return new, n
    if not isinstance(content, list):
        return content, 0
    removed = 0
    blocks = []
    for block in content:
        text = block.get('text') if isinstance(block, dict) and block.get('type') == 'text' else None
        if not isinstance(text, str):
            blocks.append(block)
            continue
        new, n = _ATTRIBUTION_REMINDER_RE.subn('', text)
        if not n:
            blocks.append(block)
            continue
        if new.strip():
            blocks.append({**block, 'text': new})
            removed += n
            continue
        # The block held only the reminder: drop it, moving any cache
        # breakpoint to the preceding block so the cached prefix keeps it.
        cache_control = block.get('cache_control')
        if cache_control is not None:
            if not blocks or not isinstance(blocks[-1], dict) or 'cache_control' in blocks[-1]:
                blocks.append(block)
                continue
            blocks[-1] = {**blocks[-1], 'cache_control': cache_control}
        removed += n
    if not removed or not any(not _is_blank_text_block(b) for b in blocks):
        # Nothing removed, or removal would leave only blank text (rejected
        # upstream): keep the message whole.
        return content, 0
    return blocks, removed


def strip_attribution_reminders(messages):
    """Remove the attribution ``<system-reminder>`` from every user message.

    Returns ``(messages, removed)``.  The original list is returned unchanged
    when nothing matched; otherwise a new list with new message dicts for the
    changed messages.  Only top-level text of ``role:'user'`` messages is
    scanned; ``tool_result`` content and ``role:'system'`` messages are not.
    A message the removal would leave with only blank text is kept unchanged,
    and a dropped block's ``cache_control`` moves to the block before it
    (ADR-0023).
    """
    if not isinstance(messages, list):
        return messages, 0
    removed = 0
    result = []
    for msg in messages:
        if isinstance(msg, dict) and msg.get('role') == 'user':
            content, n = _strip_attribution_content(msg.get('content'))
            if n:
                removed += n
                msg = {**msg, 'content': content}
        result.append(msg)
    return (result, removed) if removed else (messages, 0)
