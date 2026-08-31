"""Within-session detection of cache-hostile volatile system blocks.

Prompt caching matches on a prefix, so a system block whose value changes from
turn to turn invalidates the cached prefix on every request.  What separates a
harmful block from a harmless one is *where* the variation happens: a value that
differs across sessions costs nothing, because each session has its own cache.
Only variation **within** one session breaks turn-to-turn reuse.

The metric is therefore a per-block, per-session uniqueness ratio::

    distinct(block_hash) / requests_in_session

Measured separation is unambiguous — post-cutover sessions carrying a
per-request ``cc_prompt_id`` scored 1.00, pre-cutover sessions scored 0.001.
Three orders of magnitude, so the threshold's exact value is not delicate.

State is ephemeral and in-memory, keyed by session, evicted on restart.  It is
advisory only: nothing here gates a dispatch, so losing it on restart costs a
warning, not correctness.  Keeping it out of the database keeps a DB read off
the hot request path.
"""

import hashlib
import json
import threading
from collections import OrderedDict

# Below this many requests a session has not yet shown whether a block varies,
# and a ratio over one or two samples is noise — two requests with two distinct
# values score 1.0 and mean nothing.
DEFAULT_MIN_SAMPLES = 8

# Anywhere in 0.3-0.7 separates the measured cohorts cleanly.
DEFAULT_RATIO_THRESHOLD = 0.5

# Sessions tracked at once.  Oldest-touched is evicted first.
_MAX_SESSIONS = 512

# Distinct values recorded per block before it is considered proven volatile.
# A block with this many distinct values inside a single session is not a
# borderline call, so tracking stops rather than growing without bound.
_MAX_DISTINCT_PER_BLOCK = 256

_SATURATED = object()


def _block_hash(block) -> str:
    try:
        raw = json.dumps(block, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        raw = repr(block)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]


def last_cache_control_index(system) -> int:
    """Index of the last system block carrying a ``cache_control`` breakpoint.

    Returns -1 when the system array declares no breakpoint of its own.  A
    breakpoint marks the end of a cacheable prefix, so the block carrying it is
    itself inside that prefix — hence blocks at indexes up to and including this
    one are cache-relevant.
    """
    if not isinstance(system, list):
        return -1
    last = -1
    for i, block in enumerate(system):
        if isinstance(block, dict) and block.get('cache_control'):
            last = i
    return last


class PromptVolatilityTracker:
    """Tracks per-session, per-position system-block variation.

    Thread-safe: the server is threaded and a single session's turns can land on
    different threads.  The lock guards only dict mutation, never I/O.
    """

    def __init__(
        self,
        min_samples: int = DEFAULT_MIN_SAMPLES,
        ratio_threshold: float = DEFAULT_RATIO_THRESHOLD,
        max_sessions: int = _MAX_SESSIONS,
    ) -> None:
        self._min_samples = min_samples
        self._ratio_threshold = ratio_threshold
        self._max_sessions = max_sessions
        self._lock = threading.Lock()
        # session_id -> {'requests': int, 'blocks': {index: set|_SATURATED}}
        self._sessions: OrderedDict[str, dict] = OrderedDict()

    def observe(self, session_id: str, system) -> list[dict]:
        """Record one request's system blocks; return blocks judged volatile.

        Each returned dict is ``{'index', 'ratio', 'distinct', 'requests'}``.

        Only blocks inside the cached prefix are returned.  A volatile block
        *after* the last breakpoint costs nothing, and reporting it would train
        the operator to ignore the warning.  A system array declaring no
        breakpoint of its own reports nothing for the same reason.
        """
        if not session_id or not isinstance(system, list) or not system:
            return []

        cutoff = last_cache_control_index(system)
        hashes = [(i, _block_hash(b)) for i, b in enumerate(system)]

        with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                state = {'requests': 0, 'blocks': {}}
                self._sessions[session_id] = state
                while len(self._sessions) > self._max_sessions:
                    self._sessions.popitem(last=False)
            self._sessions.move_to_end(session_id)

            state['requests'] += 1
            requests = state['requests']
            blocks = state['blocks']

            for index, digest in hashes:
                seen = blocks.get(index)
                if seen is _SATURATED:
                    continue
                if seen is None:
                    seen = set()
                    blocks[index] = seen
                seen.add(digest)
                if len(seen) >= _MAX_DISTINCT_PER_BLOCK:
                    blocks[index] = _SATURATED

            if requests < self._min_samples:
                return []

            flagged = []
            for index, _ in hashes:
                if index > cutoff:
                    continue
                seen = blocks.get(index)
                if seen is _SATURATED:
                    distinct = _MAX_DISTINCT_PER_BLOCK
                elif seen is None:
                    continue
                else:
                    distinct = len(seen)
                ratio = distinct / requests
                if ratio > self._ratio_threshold:
                    flagged.append({
                        'index': index,
                        'ratio': round(ratio, 4),
                        'distinct': distinct,
                        'requests': requests,
                    })
            return flagged

    def session_report(self, session_id: str) -> dict | None:
        """Current per-block ratios for one session, or None if untracked."""
        with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                return None
            requests = state['requests']
            blocks = {}
            for index, seen in state['blocks'].items():
                distinct = (
                    _MAX_DISTINCT_PER_BLOCK if seen is _SATURATED else len(seen)
                )
                blocks[index] = {
                    'distinct': distinct,
                    'ratio': round(distinct / requests, 4) if requests else 0.0,
                }
            return {'requests': requests, 'blocks': blocks}

    def forget(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)


# Process-wide instance shared by the request path and the admin surface.
# Ephemeral by design — see the module docstring.
TRACKER = PromptVolatilityTracker()
