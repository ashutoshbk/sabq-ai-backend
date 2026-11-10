"""A tiny in-memory cache that holds one value for a limited time.

It resets when the service restarts. That is fine for a POC.
"""

from __future__ import annotations

import time
from typing import Callable, Generic, TypeVar

T = TypeVar("T")


class SingleValueTTLCache(Generic[T]):
    def __init__(self, ttl_seconds: float, clock: Callable[[], float] = time.monotonic):
        self._ttl = ttl_seconds
        self._clock = clock
        self._value: T | None = None
        self._stored_at: float = 0.0

    def get(self) -> T | None:
        if self._value is None or self._ttl <= 0:
            return None
        if self._clock() - self._stored_at >= self._ttl:
            self._value = None
            return None
        return self._value

    def set(self, value: T) -> None:
        self._value = value
        self._stored_at = self._clock()

    def clear(self) -> None:
        self._value = None
