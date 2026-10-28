"""Bundle of long-lived collaborators shared by the HTTP handlers."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

from .container import Container
from .data.models import Shop
from .jobs import Writer


class RateLimiter:
    """Per-chat sliding window; best effort and per-instance, which is fine for abuse control."""

    def __init__(self, per_minute: int, clock: Any = time.monotonic) -> None:
        self._limit = per_minute
        self._clock = clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = self._clock()
        q = self._hits[key]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= self._limit:
            return False
        q.append(now)
        return True


class Gateway:  # structural type for what the Telegram handler needs
    async def reply(self, *, shop: Shop, chat_id: str, text: str, audio: tuple[bytes, str] | None = None) -> str:
        raise NotImplementedError


@dataclass
class Hub:
    c: Container
    gateway: Any
    writer: Writer
    limiter: RateLimiter = field(default_factory=lambda: RateLimiter(20))
