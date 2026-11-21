"""Per-team limits: a request rate and a token rate, both as token buckets refilled continuously.

The request bucket is charged before forwarding. The token bucket is charged after the response
with the tokens actually used, so it can go negative; a team in debt is refused until it refills.
That keeps one oversized request from being rejected up front while still bounding the average.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class Bucket:
    capacity: float
    per_second: float
    level: float = field(init=False)
    stamp: float = field(init=False)
    clock: Callable[[], float] = time.monotonic

    def __post_init__(self) -> None:
        self.level = self.capacity
        self.stamp = self.clock()

    def _refill(self) -> None:
        now = self.clock()
        self.level = min(self.capacity, self.level + (now - self.stamp) * self.per_second)
        self.stamp = now

    def take(self, amount: float = 1.0) -> bool:
        self._refill()
        if self.level >= amount:
            self.level -= amount
            return True
        return False

    def charge(self, amount: float) -> None:
        self._refill()
        self.level -= amount

    def in_credit(self) -> bool:
        self._refill()
        return self.level > 0

    def seconds_until(self, amount: float) -> int:
        self._refill()
        missing = amount - self.level
        return 0 if missing <= 0 else max(1, math.ceil(missing / self.per_second))


class Limiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._requests: dict[str, Bucket] = {}
        self._tokens: dict[str, Bucket] = {}

    def _buckets(self, team: str, rpm: int, tpm: int) -> tuple[Bucket, Bucket]:
        if team not in self._requests:
            self._requests[team] = Bucket(rpm, rpm / 60, clock=self._clock)
            self._tokens[team] = Bucket(tpm, tpm / 60, clock=self._clock)
        return self._requests[team], self._tokens[team]

    def admit(self, team: str, rpm: int, tpm: int) -> tuple[bool, str, int]:
        """(allowed, reason, retry_after_seconds)."""
        req, tok = self._buckets(team, rpm, tpm)
        if not tok.in_credit():
            return False, "tokens", tok.seconds_until(1)
        if not req.take():
            return False, "requests", req.seconds_until(1)
        return True, "", 0

    def record_tokens(self, team: str, rpm: int, tpm: int, tokens: int) -> None:
        if tokens > 0:
            self._buckets(team, rpm, tpm)[1].charge(tokens)
