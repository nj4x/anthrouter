"""Per-session routing state: overrides, tier cache, and context observations.

anthproxy kept this on ``BackendRegistry``; with one backend there is no
registry, so the state that routing actually reads lives here on its own.  Every
map is bounded and evicted oldest-first, and nothing survives a restart.

Two key shapes are stored side by side and must not be confused: the routing
override is keyed by the bare session key (``metadata.user_id``), while the tier
cache and context observations are keyed by the *context key* — session key plus
a first-user-message hash — so a Claude Code Task sub-agent does not inherit and
clobber its parent's slot.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict

logger = logging.getLogger(__name__)

MAX_ENTRIES = 1000


class SessionState:
    """Bounded, thread-safe session bookkeeping for the routing path."""

    def __init__(self, auto_model_routing: bool = False) -> None:
        self._lock = threading.Lock()
        self._global_routing = bool(auto_model_routing)
        self._routing_overrides: OrderedDict[str, bool] = OrderedDict()
        self._routed_tier: OrderedDict[str, str] = OrderedDict()
        self._context_obs: OrderedDict[str, tuple[int, float]] = OrderedDict()
        self._last_ratelimit: dict = {}

    def _put(self, store: OrderedDict, key: str, value) -> None:
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

    def set_routed_tier(self, ctx_key: str, tier: str) -> None:
        with self._lock:
            self._put(self._routed_tier, ctx_key, tier)

    def routed_tier(self, ctx_key: str) -> str | None:
        with self._lock:
            return self._routed_tier.get(ctx_key)

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
