"""In-process sliding-window rate limiter for the auth endpoints (IMPLEMENTATION_PLAN C2).

Single-process by design (one app container). Keys are client IPs; behind Traefik, uvicorn must trust
the proxy (FORWARDED_ALLOW_IPS) so request.client.host is the real client, not Traefik.
"""

import time
from collections import defaultdict, deque

WINDOW_S = 60.0
MAX_KEYS = 50_000


class SlidingWindowLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def hit(self, key: str, limit: int, window: float = WINDOW_S, now: float | None = None) -> float | None:
        """Record an attempt. Returns None if allowed, else the seconds until the next attempt is allowed."""
        now = time.monotonic() if now is None else now
        hits = self._hits[key]
        while hits and hits[0] <= now - window:
            hits.popleft()
        if len(hits) >= limit:
            return max(0.0, window - (now - hits[0]))
        hits.append(now)
        if len(self._hits) > MAX_KEYS:
            self._prune(now, window)
        return None

    def _prune(self, now: float, window: float) -> None:
        for key in [k for k, v in self._hits.items() if not v or v[-1] <= now - window]:
            del self._hits[key]

    def reset(self) -> None:
        self._hits.clear()


limiter = SlidingWindowLimiter()
