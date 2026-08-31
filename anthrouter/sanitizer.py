"""System-prompt sanitizer: allowlist stripping and volatility detection (ADR-0029).

The request path calls :func:`sanitize_system_prompt` once per attempt, after it
has hashed the client's system prompt and before the payload goes upstream.  The
original hash is the caller's; this module returns the post-strip hash so the
recording layer can hold both.  The split is load-bearing: the original column
always holds the prompt **as the client sent it**, so a later change in client
behaviour stays observable.  A NULL sanitized hash means the sanitizer did not
run; equal-to-original means it ran and matched nothing.

A retry re-derives the payload hash from a `system` this function has already
stripped, so the caller must stash the first attempt's pair and restore it
rather than recompute.
"""

import dataclasses
import hashlib
import logging

from .mapper.common import strip_volatile_system_blocks, system_content_str
from .prompt_volatility import TRACKER

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class SanitizeResult:
    """What one sanitizer pass learned.  A default instance means it did not run."""

    ran: bool = False
    dropped: int = 0
    sanitized_sha256: str | None = None
    sanitized_content: str | None = None
    flagged: list[dict] = dataclasses.field(default_factory=list)


def sanitize_system_prompt(payload, mode, session_id, tracker=None, log_tag='') -> SanitizeResult:
    """Detect and optionally strip cache-hostile volatile system blocks.

    In ``strip`` mode the allowlisted blocks are removed from ``payload`` in
    place and the post-strip hash is returned — including when nothing matched,
    so "ran and found nothing" stays distinct from "did not run".  ``warn`` only
    observes and logs; ``off`` is inert.

    Never raises: a sanitizer fault must not fail an otherwise valid request.
    """
    result = SanitizeResult()
    try:
        if mode not in ('warn', 'strip'):
            return result

        system = payload.get('system')
        if not system:
            return result

        result.ran = True
        result.flagged = (tracker or TRACKER).observe(session_id or '', system)

        if mode == 'strip':
            sanitized, stripped = strip_volatile_system_blocks(system)
            if stripped:
                payload['system'] = sanitized
                result.dropped = len(system) - len(sanitized)
                logger.info(
                    '%s Sanitized system prompt: dropped %d volatile telemetry '
                    'block(s) to restore prompt-cache prefix reuse',
                    log_tag, result.dropped,
                )
            content = system_content_str(payload.get('system'))
            if content is not None:
                result.sanitized_content = content
                result.sanitized_sha256 = hashlib.sha256(
                    content.encode('utf-8')
                ).hexdigest()

        # Reported for every block the allowlist did not cover, in both modes.
        # An unrecognised volatile block is never dropped — silently deleting
        # content the operator cannot see would be undebuggable client-side.
        for block in result.flagged:
            logger.warning(
                '%s Volatile system block at index %d varies within this session '
                '(%d distinct values over %d requests, ratio %.2f) and sits inside '
                'the cached prefix. Not stripped: no allowlist match.',
                log_tag, block['index'], block['distinct'],
                block['requests'], block['ratio'],
            )
    except Exception:
        logger.warning('%s System-prompt sanitization failed', log_tag, exc_info=True)
    return result
