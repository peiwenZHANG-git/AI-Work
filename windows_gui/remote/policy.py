"""Rate limiting for remote commands (Phase 3B-1, in-process, bounded).

All structures are in-memory and reset on restart, which is the safe
direction: limits start accumulating from zero again.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class RateLimit:
    name: str
    max_events: int
    window_seconds: float


LIMITS: dict[str, RateLimit] = {
    'pairing_claim_source': RateLimit('pairing_claim_source', 5, 600.0),
    'pairing_claim_global': RateLimit('pairing_claim_global', 20, 3600.0),
    'auth_failure_device': RateLimit('auth_failure_device', 10, 300.0),
    'auth_failure_global': RateLimit('auth_failure_global', 100, 3600.0),
    'health_read_device': RateLimit('health_read_device', 30, 60.0),
    'health_read_global': RateLimit('health_read_global', 300, 60.0),
    'task_status_device': RateLimit('task_status_device', 30, 60.0),
    'task_status_global': RateLimit('task_status_global', 300, 60.0),
    'task_cancel_device': RateLimit('task_cancel_device', 30, 60.0),
    'session_device': RateLimit('session_device', 30, 60.0),
    'mutating_stage_device': RateLimit('mutating_stage_device', 10, 3600.0),
    'mutating_stage_global': RateLimit('mutating_stage_global', 50, 3600.0),
}


class RateLimiter:
    """Sliding-window limiter with per-key and global keys, bounded memory."""

    def __init__(
        self,
        *,
        capacity: int = 512,
        now_factory: Callable[[], float] = time.monotonic,
    ) -> None:
        self._capacity = int(capacity)
        self._now = now_factory
        self._lock = threading.Lock()
        self._windows: OrderedDict[str, deque[float]] = OrderedDict()

    def allow(self, limit: RateLimit, key: str, *, now: float | None = None) -> bool:
        """Record one event if allowed; return False when rate limited."""
        current = self._now() if now is None else float(now)
        with self._lock:
            window = self._windows.get(key)
            if window is None:
                if len(self._windows) >= self._capacity:
                    self._windows.popitem(last=False)
                window = deque()
                self._windows[key] = window
            while window and window[0] <= current - limit.window_seconds:
                window.popleft()
            if len(window) >= limit.max_events:
                return False
            window.append(current)
            return True

    def reset(self) -> None:
        with self._lock:
            self._windows.clear()
