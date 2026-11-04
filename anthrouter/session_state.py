"""Per-session routing state: overrides, tier cache, and context observations.

anthproxy kept this on ``BackendRegistry``; with one backend there is no
registry, so the state that routing actually reads lives here on its own.  Every
map is bounded and evicted oldest-first, and nothing survives a restart.

Five key shapes are stored side by side and must not be confused: the routing
override is keyed by the bare session key (``metadata.user_id``); context
observations are keyed by the *context key* — session key plus a
first-user-message hash — so a Claude Code Task sub-agent does not inherit and
clobber its parent's slot; and the tier cache is keyed by ``(context key,
requested tier)`` so a tier cached on a haiku-requested turn is never replayed
into a sonnet-requested turn of the same conversation (ADR-0012).  The Thread
map is keyed by upstream message id alone: ids are globally unique, and a
continuation's context key never matches the key of the turn that created its
Thread (ADR-0027).  The last Thread model store is keyed by the bare session key,
like the routing override.  The handoff store is keyed by ``(session key, SHA-256
hex of the final user text)``, so a replay of the same prompt in the same session
finds the model a re-classified continuation chose (ADR-0033).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import TYPE_CHECKING, NamedTuple

from .model_router import _truncate_prior_response

if TYPE_CHECKING:
    from .model_router import ModelRoutingDecision

logger = logging.getLogger(__name__)

MAX_ENTRIES = 10000

HANDOFF_TTL_SECONDS = 60.0
"""Fixed, not a config flag (ADR-0033 Decision 5)."""

DEFAULT_RESPONSE_TEXT_LIMIT = 1000
"""Default of ``auto_model_routing_prior_response_summary_limit``."""


class ThreadPin(NamedTuple):
    """What the Thread map holds for one upstream message id."""

    model: str
    long_context: bool
    response_text: str | None


class Handoff(NamedTuple):
    """A re-classified continuation's routing, held for the client's replay (ADR-0033)."""

    model: str
    decision: ModelRoutingDecision
    expires_at: float


class SessionState:
    """Bounded, thread-safe session bookkeeping for the routing path."""

    def __init__(self, auto_model_routing: bool = False,
                 clock: Callable[[], float] = time.monotonic) -> None:
        """``clock`` times handoff expiry; tests swap it through the ``clock`` attribute."""
        self.clock = clock
        self._lock = threading.Lock()
        self._global_routing = bool(auto_model_routing)
        self._routing_overrides: OrderedDict[str, bool] = OrderedDict()
        self._routed_tier: OrderedDict[tuple[str, str], str] = OrderedDict()
        self._context_obs: OrderedDict[str, tuple[int, float]] = OrderedDict()
        self._thread_models: OrderedDict[str, ThreadPin] = OrderedDict()
        self._last_thread_model: OrderedDict[str, str] = OrderedDict()
        self._handoffs: OrderedDict[tuple[str, str], Handoff] = OrderedDict()
        self._last_ratelimit: dict = {}

    def _put(self, store: OrderedDict, key, value) -> None:
        if key not in store and len(store) >= MAX_ENTRIES:
            store.popitem(last=False)
        store[key] = value
        store.move_to_end(key)

    # -- model-routing toggle -------------------------------------------------

    def set_model_routing(self, enabled: bool) -> None:
        with self._lock:
            self._global_routing = bool(enabled)
        logger.info('Auto model routing %s (global)', 'enabled' if enabled else 'disabled')

    def set_session_model_routing(self, session_key: str, enabled: bool | None) -> None:
        """Pin routing on/off for one session, or clear the pin with ``None``."""
        with self._lock:
            if enabled is None:
                self._routing_overrides.pop(session_key, None)
            else:
                self._put(self._routing_overrides, session_key, bool(enabled))

    def session_model_routing(self, session_key: str) -> bool | None:
        with self._lock:
            return self._routing_overrides.get(session_key)

    def model_routing_enabled(self, session_key: str | None) -> bool:
        with self._lock:
            if session_key is not None:
                override = self._routing_overrides.get(session_key)
                if override is not None:
                    return override
            return self._global_routing

    @property
    def global_model_routing(self) -> bool:
        with self._lock:
            return self._global_routing

    # -- routed-tier cache ----------------------------------------------------

    def set_routed_tier(self, key: tuple[str, str], tier: str) -> None:
        """``key`` is ``(ctx_key, requested_tier)``; see ``handlers._requested_tier``."""
        with self._lock:
            self._put(self._routed_tier, key, tier)

    def routed_tier(self, key: tuple[str, str]) -> str | None:
        with self._lock:
            return self._routed_tier.get(key)

    # -- Thread map -----------------------------------------------------------

    def record_thread(self, message_id: str, model: str, long_context: bool = False,
                      session_key: str | None = None, response_text: str | None = None,
                      response_text_limit: int = DEFAULT_RESPONSE_TEXT_LIMIT) -> None:
        """Remember the model that served ``message_id`` so its continuation can be pinned.

        ``long_context`` records whether the dispatched request carried a
        ``context-1m*`` beta, so a pinned continuation keeps the Thread's 1M
        window even when only the size floor added it (ADR-0027).
        ``response_text`` is stored truncated to ``response_text_limit`` by the
        same head/tail rule as ``prior_response_summary``, so a continuation's
        classifier sees the text it would have seen in a full history.  With a
        ``session_key``, ``model`` also becomes that session's last Thread model
        (ADR-0033).
        """
        text = (_truncate_prior_response(response_text, response_text_limit)
                if response_text else None)
        with self._lock:
            self._put(self._thread_models, message_id,
                      ThreadPin(model, bool(long_context), text))
            if session_key is not None:
                self._put(self._last_thread_model, session_key, model)

    def thread_pin(self, message_id: str) -> ThreadPin | None:
        """The ``ThreadPin`` recorded for ``message_id``, or ``None``."""
        with self._lock:
            return self._thread_models.get(message_id)

    def last_thread_model(self, session_key: str | None) -> str | None:
        """Model of the last Thread map entry recorded in this session, or ``None``."""
        if session_key is None:
            return None
        with self._lock:
            return self._last_thread_model.get(session_key)

    # -- handoff store (ADR-0033) ---------------------------------------------

    def store_handoff(self, key: tuple[str | None, str], model: str,
                      decision: ModelRoutingDecision) -> None:
        """Hold ``model`` for ``HANDOFF_TTL_SECONDS``; a re-store overwrites with a fresh expiry.

        ``key`` is ``(session key, SHA-256 hex of the final user text)``.  A
        request with no session key stores nothing.
        """
        if key[0] is None:
            return
        with self._lock:
            self._put(self._handoffs, key,
                      Handoff(model, decision, self.clock() + HANDOFF_TTL_SECONDS))

    def peek_handoff(self, key: tuple[str | None, str]) -> Handoff | None:
        """The live handoff for ``key``, kept in the store (a resend of the continuation)."""
        return self._live_handoff(key, take=False)

    def take_handoff(self, key: tuple[str | None, str]) -> Handoff | None:
        """The live handoff for ``key``, removed from the store (a replay)."""
        return self._live_handoff(key, take=True)

    def _live_handoff(self, key: tuple[str | None, str], take: bool) -> Handoff | None:
        """An entry is expired once ``now >= expires_at``; it is then dropped and absent."""
        if key[0] is None:
            return None
        with self._lock:
            entry = self._handoffs.get(key)
            if entry is None:
                return None
            if self.clock() >= entry.expires_at:
                del self._handoffs[key]
                return None
            if take:
                del self._handoffs[key]
            return entry

    # -- context observations -------------------------------------------------

    def record_context(self, ctx_key: str, measured_floor: int, est_ratio: float) -> None:
        """Replace, not accumulate: each response already restates the full context."""
        with self._lock:
            self._put(self._context_obs, ctx_key, (measured_floor, est_ratio))

    def context(self, ctx_key: str) -> tuple[int, float]:
        with self._lock:
            return self._context_obs.get(ctx_key, (0, 1.0))

    # -- rate-limit window ----------------------------------------------------

    def record_ratelimit(self, window: dict) -> None:
        if not window:
            return
        with self._lock:
            self._last_ratelimit = dict(window)

    def last_ratelimit(self) -> dict:
        with self._lock:
            return dict(self._last_ratelimit)
