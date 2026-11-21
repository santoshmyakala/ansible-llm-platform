"""Prometheus metrics, in a registry owned by each app instance (keeps tests isolated)."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 15, 30, 60, 120)


class Metrics:
    def __init__(self) -> None:
        r = self.registry = CollectorRegistry()
        self.requests = Counter("gateway_requests", "Requests by team, model and HTTP status.",
                                ["team", "model", "status"], registry=r)
        self.tokens = Counter("gateway_tokens", "Tokens processed, by team, model and kind (prompt|completion).",
                              ["team", "model", "kind"], registry=r)
        self.rate_limited = Counter("gateway_rate_limited", "Requests refused by a team limit.",
                                    ["team", "reason"], registry=r)
        self.upstream_failures = Counter("gateway_upstream_failures", "Failed calls to a backend.",
                                         ["backend"], registry=r)
        self.failovers = Counter("gateway_failovers", "Requests retried on another backend.", ["model"], registry=r)
        self.backend_up = Gauge("gateway_backend_up", "1 if the backend passes health checks.", ["backend"], registry=r)
        self.inflight = Gauge("gateway_backend_inflight", "Requests in flight per backend.", ["backend"], registry=r)
        self.ttft = Histogram("gateway_time_to_first_token_seconds", "Time to the first streamed token.",
                              ["model"], buckets=LATENCY_BUCKETS, registry=r)
        self.duration = Histogram("gateway_request_duration_seconds", "Whole request duration.",
                                  ["model", "stream"], buckets=LATENCY_BUCKETS, registry=r)
