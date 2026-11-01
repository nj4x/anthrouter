"""Caveman-mode system-prompt rewrite (edit ops confined to communication sections).

caveman-kit injects a terse-output ruleset (``CAVEMAN MODE ACTIVE — level: X``)
into the opening turn, but the client carries communication guidance that
contradicts it — in the ``system`` field, or (Claude Code) in the inline
``role:'system'`` messages of the opening turn, where the output style lives.
Both are rewrite targets, each cached under its own key.  The caveman ruleset
itself, which rides in the same message, is never a target.  For a caveman
request this module asks a model for *edit ops* against that guidance — never a whole new prompt — applies them
locally, and accepts the result only when every edit falls inside a
communication/tone section.  The rewrite ``P -> P'`` is cached in memory and on
disk, keyed by ``sha256(P + mode)``, so ``P'`` stays byte-stable across
restarts and the provider prompt cache keeps hitting.

Every failure (model error, timeout, invalid ops, no free worker) sends ``P``
unchanged and writes nothing to the cache; invalid ops are remembered in memory
for ``REJECT_TTL_SECS`` so a bad prompt does not re-call the model every turn.
Nothing here raises into the request path.
"""
from __future__ import annotations

import concurrent.futures
import copy
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

CAVEMAN_HEADER_RE = re.compile(r'CAVEMAN MODE ACTIVE — level: (\w+)')

# Headings whose section counts as communication/tone guidance.
_COMM_HEADING_RE = re.compile(
    r'tone|style|communicat|concise|brevity|verbos|output|format|writing|voice|prose',
    re.IGNORECASE,
)
_HEADING_RE = re.compile(r'^(#{1,6})[ \t]+(.+?)[ \t]*$', re.MULTILINE)
# The caveman ruleset ("# ultracave", "# caveman", …) shares a message with the
# output style it is meant to override; editing it would defeat the feature.
_RULESET_HEADING_RE = re.compile(r'caveman|ultracave|wenyan', re.IGNORECASE)

DEFAULT_TIMEOUT_SECS = 30.0
REJECT_TTL_SECS = 600.0
_MAX_WORKERS = 4
_MAX_REJECTS = 4096
_MAX_TOKENS = 4096

_EDITOR_SYSTEM = (
    'You edit an AI assistant system prompt so it no longer contradicts a terse '
    '"caveman" output mode, whose rules drop articles, copulas and connectives '
    'and favour sentence fragments. Return ONLY a JSON array of edit ops, no '
    'prose and no code fence. Each op is {"find": "<exact substring>", '
    '"replace": "<new text>"}; an empty "replace" deletes the span. "find" must '
    'be copied verbatim from the prompt and occur exactly once. Edit only text '
    'inside the communication / tone / style / output sections listed, and only '
    'where it demands full sentences, grammatical prose, articles, connective '
    'words, or a register that contradicts terse fragments. Keep guidance on '
    'accuracy, safety, formatting of code, file references and tool use. Two '
    'more cases count as contradicting. A precedence clause that makes these '
    'sections win over other guidance ("these rules win") must be narrowed so '
    'the caveman ruleset wins on register, for example "the caveman ruleset '
    'wins on register; these rules win otherwise". A completeness rule ("answer '
    'completely", "give full detail") keeps its content demand but must say the '
    'answer stays in fragments, for example "answer completely, in fragments"; '
    'never delete it. Return [] when nothing contradicts.'
)
# Part of every cache key: a changed editor prompt re-runs for cached prompts.
EDITOR_VERSION = hashlib.sha256(_EDITOR_SYSTEM.encode('utf-8')).hexdigest()[:12]
CACHE_MAX_AGE_SECS = 30 * 86400


@dataclass(frozen=True)
class RewriteResult:
    system: object          # the system value to dispatch (P or P')
    outcome: str            # cache_hit | rewritten | unchanged | no_guidance | rejected | rejected_cached | busy | error | timeout
    edits: int = 0


def detect_mode(payload) -> str | None:
    """Caveman level named in the opening turn, or None.

    The opening turn is every message before the first assistant message.
    Claude Code delivers the SessionStart hook header as an inline
    ``role:'system'`` message after the first user message, so both roles count.
    """
    messages = payload.get('messages') if isinstance(payload, dict) else None
    if not isinstance(messages, list):
        return None
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get('role')
        if role == 'assistant':
            break
        if role in ('user', 'system'):
            match = CAVEMAN_HEADER_RE.search(_message_text(msg.get('content')))
            if match:
                return match.group(1)
    return None


def opening_system_blocks(payload) -> tuple[list[dict], list[tuple[int, int | None]]]:
    """Text of the opening turn's ``role:'system'`` messages as a block list.

    Returns ``(blocks, where)``: ``blocks`` is a ``system``-shaped list of text
    blocks the rewriter accepts, ``where[k]`` is ``(message index, content
    index)`` of ``blocks[k]`` (content index None for a string content).
    """
    blocks: list[dict] = []
    where: list[tuple[int, int | None]] = []
    messages = payload.get('messages') if isinstance(payload, dict) else None
    if not isinstance(messages, list):
        return blocks, where
    for mi, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        if msg.get('role') == 'assistant':
            break
        if msg.get('role') != 'system':
            continue
        content = msg.get('content')
        if isinstance(content, str):
            blocks.append({'type': 'text', 'text': content})
            where.append((mi, None))
        elif isinstance(content, list):
            for ci, b in enumerate(content):
                if isinstance(b, dict) and b.get('type') == 'text' and isinstance(b.get('text'), str):
                    blocks.append({'type': 'text', 'text': b['text']})
                    where.append((mi, ci))
    return blocks, where


def write_opening_system_blocks(
    payload: dict, where: list[tuple[int, int | None]], blocks: list[dict],
) -> None:
    """Write rewritten ``blocks`` back to the messages ``where`` names."""
    if len(where) != len(blocks):
        raise ValueError(f'{len(blocks)} blocks for {len(where)} positions')
    messages = payload['messages']
    for (mi, ci), block in zip(where, blocks):
        if ci is None:
            messages[mi]['content'] = block['text']
        else:
            messages[mi]['content'][ci]['text'] = block['text']


def _message_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(
            b['text'] for b in content
            if isinstance(b, dict) and b.get('type') == 'text' and isinstance(b.get('text'), str)
        )
    return ''


def comm_sections(text: str) -> list[tuple[int, int]]:
    """``[start, end)`` spans of communication sections (heading through body).

    A section runs from its heading to the next heading of the same or a higher
    level, so sub-headings inside a tone section stay inside it.  A section that
    starts inside a caveman-ruleset section (see ``ruleset_sections``) is dropped.
    """
    headings = _headings(text)
    ruleset = ruleset_sections(text)
    spans = []
    for i, (start, _, title) in enumerate(headings):
        if not _COMM_HEADING_RE.search(title):
            continue
        if any(rs <= start < rend for rs, rend in ruleset):
            continue
        spans.append((start, _section_end(text, headings, i)))
    return spans


def _headings(text: str) -> list[tuple[int, int, str]]:
    """``(offset, level, title)`` of every markdown heading in ``text``."""
    return [(m.start(), len(m.group(1)), m.group(2)) for m in _HEADING_RE.finditer(text)]


def _section_end(text: str, headings: list[tuple[int, int, str]], i: int) -> int:
    level = headings[i][1]
    for nstart, nlevel, _ in headings[i + 1:]:
        if nlevel <= level:
            return nstart
    return len(text)


def ruleset_sections(text: str) -> list[tuple[int, int]]:
    """``[start, end)`` spans of caveman-ruleset sections.

    A ruleset section is a heading matching ``_RULESET_HEADING_RE`` through the
    next heading of the same or a higher level.
    """
    headings = _headings(text)
    return [(headings[i][0], _section_end(text, headings, i))
            for i, h in enumerate(headings) if _RULESET_HEADING_RE.search(h[2])]


def apply_edits(text: str, ops) -> tuple[str, int] | None:
    """Apply validated edit ops to ``text``; None when any op is invalid.

    Invalid: not a list of ``{find, replace}`` string pairs, an empty or
    non-unique ``find``, overlapping spans, or a span not wholly inside one
    communication section.  Edits never touch a heading line, so the section
    structure that validated them survives.
    """
    if not isinstance(ops, list):
        return None
    sections = comm_sections(text)
    ruleset = ruleset_sections(text)
    heading_spans = [(m.start(), m.end()) for m in _HEADING_RE.finditer(text)]
    spans = []
    for op in ops:
        if not isinstance(op, dict):
            return None
        find, repl = op.get('find'), op.get('replace')
        if not isinstance(find, str) or not find or not isinstance(repl, str):
            return None
        start = text.find(find)
        if start < 0 or text.find(find, start + 1) >= 0:
            return None
        end = start + len(find)
        if not any(s <= start and end <= e for s, e in sections):
            return None
        # A communication section can enclose a ruleset section; the ruleset
        # stays untouched even then.
        if any(start < rend and rs < end for rs, rend in ruleset):
            return None
        if any(start < he and hs < end for hs, he in heading_spans):
            return None
        spans.append((start, end, repl))
    spans.sort()
    for (_, prev_end, _), (start, _, _) in zip(spans, spans[1:]):
        if start < prev_end:
            return None
    out, pos = [], 0
    for start, end, repl in spans:
        out.append(text[pos:start])
        out.append(repl)
        pos = end
    out.append(text[pos:])
    return ''.join(out), len(spans)


def _parse_ops(reply: str):
    reply = reply.strip()
    if reply.startswith('```'):
        reply = re.sub(r'^```\w*\n?|\n?```$', '', reply).strip()
    try:
        return json.loads(reply)
    except (ValueError, TypeError):
        return None


def _system_key(system, mode: str) -> str:
    raw = system if isinstance(system, str) else json.dumps(system, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256((raw + '\0' + mode + '\0' + EDITOR_VERSION).encode('utf-8')).hexdigest()


def _text_slots(system):
    """``(index, text)`` for each text block (``index`` None for a str system)."""
    if isinstance(system, str):
        return [(None, system)]
    if isinstance(system, list):
        return [
            (i, b['text']) for i, b in enumerate(system)
            if isinstance(b, dict) and b.get('type') == 'text' and isinstance(b.get('text'), str)
        ]
    return []


class RewriteCache:
    """``sha256(P + mode + editor version) -> P'`` in memory, persisted one JSON file per key.

    A file's mtime is its last use: a disk load touches it.  Construction
    deletes files unused for ``max_age`` seconds, which is how keys orphaned by
    an ``EDITOR_VERSION`` change leave the directory.
    """

    def __init__(self, directory: str | os.PathLike | None, *,
                 max_age: float = CACHE_MAX_AGE_SECS) -> None:
        self._dir = Path(directory) if directory else None
        self._mem: dict[str, object] = {}
        self._lock = threading.Lock()
        if self._dir is not None:
            self._prune(max_age)

    def _prune(self, max_age: float) -> None:
        cutoff = time.time() - max_age
        try:
            paths = list(self._dir.glob('*.json'))
        except OSError:
            return
        for path in paths:
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                pass

    def get(self, key: str):
        with self._lock:
            if key in self._mem:
                return True, self._mem[key]
        if self._dir is None:
            return False, None
        path = self._dir / f'{key}.json'
        try:
            value = json.loads(path.read_text(encoding='utf-8'))['system']
        except (OSError, ValueError, KeyError, TypeError):
            return False, None
        try:
            os.utime(path)
        except OSError:
            pass
        with self._lock:
            self._mem[key] = value
        return True, value

    def put(self, key: str, system) -> None:
        with self._lock:
            self._mem[key] = system
        if self._dir is None:
            return
        try:
            self._dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self._dir, prefix='.tmp-')
            with os.fdopen(fd, 'w', encoding='utf-8') as fh:
                json.dump({'system': system}, fh, ensure_ascii=False)
            os.replace(tmp, self._dir / f'{key}.json')
        except OSError:
            logger.warning('Caveman rewrite cache write failed for %s', key[:12], exc_info=True)


class CavemanRewriter:
    """Rewrites caveman-request system prompts; one per server."""

    def __init__(self, cache: RewriteCache, *, timeout: float = DEFAULT_TIMEOUT_SECS,
                 reject_ttl: float = REJECT_TTL_SECS, clock=time.monotonic) -> None:
        self.cache = cache
        self.timeout = timeout
        self.reject_ttl = reject_ttl
        self._clock = clock
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=_MAX_WORKERS, thread_name_prefix='caveman-rw',
        )
        # A timed-out call keeps its worker until the backend returns.  Calls
        # beyond the pool size are refused ('busy') rather than queued, so a
        # hung upstream never makes later requests wait out a queue.
        self._slots = threading.BoundedSemaphore(_MAX_WORKERS)
        self._lock = threading.Lock()
        self._key_locks: dict[str, threading.Lock] = {}
        # Memory-only: a reject is a model-output property, so a restart (or a
        # new --caveman-rewrite-model, which needs one) retries it.
        self._rejects: dict[str, float] = {}

    def rewrite(self, system, mode: str, call_model) -> RewriteResult:
        """Return the system to dispatch.  ``call_model(system, user) -> str``."""
        key = _system_key(system, mode)
        hit, cached = self.cache.get(key)
        if hit:
            return RewriteResult(cached, 'cache_hit')
        if self._rejected_recently(key):
            return RewriteResult(system, 'rejected_cached')

        # Single flight per key: concurrent first sightings of one prompt make
        # one model call, and every caller dispatches the same P'.
        with self._lock:
            key_lock = self._key_locks.setdefault(key, threading.Lock())
        if not key_lock.acquire(timeout=self.timeout):
            return RewriteResult(system, 'busy')
        try:
            hit, cached = self.cache.get(key)
            if hit:
                return RewriteResult(cached, 'cache_hit')
            if self._rejected_recently(key):
                return RewriteResult(system, 'rejected_cached')
            result = self._compute(key, system, mode, call_model)
            if result.outcome == 'rejected':
                with self._lock:
                    self._rejects[key] = self._clock() + self.reject_ttl
                    if len(self._rejects) > _MAX_REJECTS:
                        self._rejects.pop(next(iter(self._rejects)))
            return result
        finally:
            key_lock.release()
            with self._lock:
                if self._key_locks.get(key) is key_lock and not key_lock.locked():
                    del self._key_locks[key]

    def _rejected_recently(self, key: str) -> bool:
        with self._lock:
            expiry = self._rejects.get(key)
            if expiry is None:
                return False
            if self._clock() < expiry:
                return True
            del self._rejects[key]
            return False

    def _compute(self, key, system, mode, call_model) -> RewriteResult:
        slots = [(i, t, comm_sections(t)) for i, t in _text_slots(system)]
        slots = [s for s in slots if s[2]]
        if not slots:
            self.cache.put(key, system)
            return RewriteResult(system, 'no_guidance')

        excerpt = '\n\n'.join(
            f'<section block="{i if i is not None else 0}">\n{t[s:e]}\n</section>'
            for i, t, secs in slots for s, e in secs
        )
        user = (
            f'Caveman mode level: {mode}.\n\nCommunication sections of the system '
            f'prompt (edit only text inside these):\n\n{excerpt}\n\nReturn the JSON '
            'array of edit ops. Each "find" must be unique within its whole block.'
        )
        if not self._slots.acquire(blocking=False):
            return RewriteResult(system, 'busy')
        try:
            future = self._pool.submit(call_model, _EDITOR_SYSTEM, user)
        except Exception:
            self._slots.release()
            raise
        future.add_done_callback(lambda _f: self._slots.release())
        try:
            reply = future.result(timeout=self.timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            return RewriteResult(system, 'timeout')
        except Exception:
            logger.warning('Caveman rewrite model call failed', exc_info=True)
            return RewriteResult(system, 'error')

        ops = _parse_ops(reply or '')
        if not isinstance(ops, list):
            return RewriteResult(system, 'rejected')
        new_system = copy.deepcopy(system)
        total = 0
        # An op is valid in at most one block: it must be unique in the block
        # it lands in, and must not appear in any other.
        per_block: dict[object, list] = {i: [] for i, _, _ in slots}
        for op in ops:
            find = op.get('find') if isinstance(op, dict) else None
            if not isinstance(find, str) or not find:
                return RewriteResult(system, 'rejected')
            owners = [i for i, t, _ in slots if find in t]
            others = [i for i, t in _text_slots(system) if find in t]
            if len(owners) != 1 or len(others) != 1:
                return RewriteResult(system, 'rejected')
            per_block[owners[0]].append(op)
        for i, text, _ in slots:
            if not per_block[i]:
                continue
            applied = apply_edits(text, per_block[i])
            if applied is None:
                return RewriteResult(system, 'rejected')
            new_text, n = applied
            total += n
            if i is None:
                new_system = new_text
            else:
                new_system[i]['text'] = new_text
        outcome = 'rewritten' if total else 'unchanged'
        self.cache.put(key, new_system)
        # A retry re-enters with P' already in the payload; map it to itself so
        # it is a cache hit rather than a second model call.
        if total:
            self.cache.put(_system_key(new_system, mode), new_system)
        return RewriteResult(new_system, outcome, total)


class CacheRatioTracker:
    """Running prompt-cache read ratio per (session, rewrite state)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._totals: dict[tuple[str, str], list[int]] = {}

    def record(self, sess: str, state: str, input_tokens, cache_creation, cache_read) -> tuple[float, float | None]:
        """Add one response; return (this request's ratio, other state's running ratio)."""
        total = int(input_tokens or 0) + int(cache_creation or 0) + int(cache_read or 0)
        ratio = (int(cache_read or 0) / total) if total else 0.0
        other = 'original' if state == 'rewritten' else 'rewritten'
        with self._lock:
            agg = self._totals.setdefault((sess, state), [0, 0])
            agg[0] += int(cache_read or 0)
            agg[1] += total
            if len(self._totals) > 4096:
                self._totals.pop(next(iter(self._totals)))
            o = self._totals.get((sess, other))
        return ratio, ((o[0] / o[1]) if o and o[1] else None)


RATIO_TRACKER = CacheRatioTracker()


_PROSE_WORDS = frozenset({'a', 'an', 'the', 'is', 'are', 'was', 'were', 'be'})
_CODE_RE = re.compile(r'```.*?(?:```|\Z)|`[^`\n]*`', re.DOTALL)
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z']*")


def prose_rate(text: str) -> tuple[float, int] | None:
    """Articles and copulas per 100 words of ``text``, code excluded; None when wordless."""
    words = _WORD_RE.findall(_CODE_RE.sub(' ', text or ''))
    if not words:
        return None
    hits = sum(1 for w in words if w.lower() in _PROSE_WORDS)
    return 100.0 * hits / len(words), len(words)


class ProseRateTracker:
    """Running article/copula rate per (session, rewrite state); advisory only."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._totals: dict[tuple[str, str], list[float]] = {}

    def record(self, sess: str, state: str, rate: float) -> tuple[float, float | None]:
        """Add one response's rate; return (this state's mean, other state's mean)."""
        other = 'original' if state == 'rewritten' else 'rewritten'
        with self._lock:
            agg = self._totals.setdefault((sess, state), [0.0, 0])
            agg[0] += rate
            agg[1] += 1
            mean = agg[0] / agg[1]
            if len(self._totals) > 4096:
                self._totals.pop(next(iter(self._totals)))
            o = self._totals.get((sess, other))
        return mean, ((o[0] / o[1]) if o and o[1] else None)


PROSE_TRACKER = ProseRateTracker()
