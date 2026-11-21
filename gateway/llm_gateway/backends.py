"""The pool of inference backends: health state, in-flight counts and selection."""

from __future__ import annotations

import asyncio
import itertools
import logging
from dataclasses import dataclass

import httpx

from .config import Backend, Settings
from .metrics import Metrics

log = logging.getLogger("llm_gateway.backends")


@dataclass
class State:
    backend: Backend
    healthy: bool = False
    failures: int = 0
    inflight: int = 0


class Pool:
    def __init__(self, settings: Settings, client: httpx.AsyncClient, metrics: Metrics) -> None:
        self.settings = settings
        self.client = client
        self.metrics = metrics
        self.states = {b.name: State(b) for b in settings.backends}
        self._order = {name: i for i, name in enumerate(self.states)}
        self._tiebreak = itertools.count()

    def serves(self, model: str) -> bool:
        return model in self.settings.models

    def ready(self) -> bool:
        return any(s.healthy for s in self.states.values())

    def candidates(self, model: str) -> list[State]:
        """Healthy backends serving the model, least in-flight first (ties rotate)."""
        tick, n = next(self._tiebreak), len(self._order)
        pool = [s for s in self.states.values() if s.healthy and model in s.backend.models]
        pool.sort(key=lambda s: (s.inflight, (self._order[s.backend.name] - tick) % n))
        return pool

    def acquire(self, s: State) -> None:
        s.inflight += 1
        self.metrics.inflight.labels(s.backend.name).set(s.inflight)

    def release(self, s: State) -> None:
        s.inflight = max(0, s.inflight - 1)
        self.metrics.inflight.labels(s.backend.name).set(s.inflight)

    def _set(self, s: State, healthy: bool) -> None:
        if s.healthy != healthy:
            log.warning("backend %s is now %s", s.backend.name, "healthy" if healthy else "UNHEALTHY")
        s.healthy = healthy
        self.metrics.backend_up.labels(s.backend.name).set(1 if healthy else 0)

    def mark_failure(self, s: State) -> None:
        s.failures += 1
        self.metrics.upstream_failures.labels(s.backend.name).inc()
        if s.failures >= self.settings.unhealthy_after:
            self._set(s, False)

    def mark_success(self, s: State) -> None:
        s.failures = 0
        self._set(s, True)

    def auth_headers(self, s: State) -> dict[str, str]:
        return {"Authorization": f"Bearer {s.backend.api_key}"} if s.backend.api_key else {}

    async def probe(self, s: State) -> None:
        try:
            r = await self.client.get(f"{s.backend.url}/health", headers=self.auth_headers(s),
                                      timeout=self.settings.connect_timeout + 1)
            ok = r.status_code == 200
        except httpx.HTTPError:
            ok = False
        if ok:
            self.mark_success(s)
        else:
            s.failures += 1
            if s.failures >= self.settings.unhealthy_after or not s.healthy:
                self._set(s, False)

    async def probe_all(self) -> None:
        await asyncio.gather(*(self.probe(s) for s in self.states.values()))

    async def run_health_checks(self) -> None:
        while True:
            await self.probe_all()
            await asyncio.sleep(self.settings.health_interval)
