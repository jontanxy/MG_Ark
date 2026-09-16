"""Small in-memory rate limiters (per process). Good enough for a single-instance bot."""
from __future__ import annotations

import time
from collections import deque


class SlidingWindow:
    """Counts events per key inside a rolling window."""

    def __init__(self, limit: int, window_seconds: float) -> None:
        self.limit = limit
        self.window = window_seconds
        self._events: dict[object, deque[float]] = {}

    def _prune(self, key: object, now: float) -> deque[float]:
        q = self._events.setdefault(key, deque())
        while q and now - q[0] > self.window:
            q.popleft()
        if not q:
            self._events.pop(key, None)
            q = deque()
            self._events[key] = q
        return q

    def count(self, key: object) -> int:
        return len(self._prune(key, time.monotonic()))

    def record(self, key: object) -> int:
        """Record one event; return the count inside the window (including this one)."""
        q = self._prune(key, time.monotonic())
        q.append(time.monotonic())
        return len(q)

    def allow(self, key: object) -> bool:
        """Record one event if under the limit; False when the key is over its budget."""
        q = self._prune(key, time.monotonic())
        if len(q) >= self.limit:
            return False
        q.append(time.monotonic())
        return True


class Breaker:
    """Opens (pauses an action) for ``pause_seconds`` once ``limit`` events happen inside ``window_seconds``."""

    def __init__(self, limit: int, window_seconds: float, pause_seconds: float) -> None:
        self.window = SlidingWindow(limit, window_seconds)
        self.pause = pause_seconds
        self.open_until = 0.0
        self.alerted = False

    def is_open(self) -> bool:
        return time.monotonic() < self.open_until

    def record(self) -> bool:
        """Record one event; return True if this event opened the breaker."""
        if self.window.record("global") >= self.window.limit and not self.is_open():
            self.open_until = time.monotonic() + self.pause
            self.alerted = False
            return True
        return False

    def remaining_seconds(self) -> int:
        return max(0, int(self.open_until - time.monotonic()) + 1)
