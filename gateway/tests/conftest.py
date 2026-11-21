"""Fake inference backends behind httpx.MockTransport, and a client for the gateway's ASGI app."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from llm_gateway.app import create_app
from llm_gateway.auth import digest
from llm_gateway.config import parse
from llm_gateway.ratelimit import Limiter

KEYS = {"research": "rk-research-0123456789", "ops": "rk-ops-0123456789", "tight": "rk-tight-0123456789"}


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def config(**overrides) -> dict:
    raw = {
        "backends": [
            {"name": "node-a", "url": "http://node-a:8080", "models": ["qwen-small"], "api_key": "backend-secret"},
            {"name": "node-b", "url": "http://node-b:8080", "models": ["qwen-small", "coder"]},
        ],
        "teams": [
            {"name": "research", "key_sha256": digest(KEYS["research"]), "requests_per_minute": 600,
             "tokens_per_minute": 1_000_000},
            {"name": "ops", "key_sha256": digest(KEYS["ops"]), "requests_per_minute": 600,
             "tokens_per_minute": 1_000_000, "models": ["qwen-small"]},
            {"name": "tight", "key_sha256": digest(KEYS["tight"]), "requests_per_minute": 2,
             "tokens_per_minute": 50},
        ],
        "options": {"unhealthy_after": 2},
    }
    raw.update(overrides)
    return raw


def completion(model: str, text: str = "hello", prompt: int = 7, completion_tokens: int = 3) -> dict:
    return {"id": "c1", "object": "chat.completion", "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion_tokens,
                      "total_tokens": prompt + completion_tokens}}


def sse(model: str, words: list[str], prompt: int = 5) -> bytes:
    lines = []
    for w in words:
        lines.append("data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": w}}], "model": model}))
        lines.append("")
    lines.append("data: " + json.dumps({"choices": [], "usage": {"prompt_tokens": prompt,
                                                                 "completion_tokens": len(words)}}))
    lines.append("")
    lines.append("data: [DONE]")
    lines.append("")
    return "\n".join(lines).encode()


class Upstreams:
    """Programmable fake backends. behaviour[host] decides how /v1/chat/completions answers."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict, dict]] = []  # (host, path, headers, body)
        self.health = {"node-a": 200, "node-b": 200}
        self.behaviour: dict[str, Callable[[dict], httpx.Response]] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if request.url.path == "/health":
            status = self.health.get(host, 503)
            if status == 0:
                raise httpx.ConnectError("refused", request=request)
            return httpx.Response(status, json={"status": "ok" if status == 200 else "loading"})
        body = json.loads(request.content or b"{}")
        self.calls.append((host, request.url.path, dict(request.headers), body))
        behave = self.behaviour.get(host)
        if behave is None:
            return httpx.Response(200, json=completion(body.get("model", "?"), text=f"from {host}"))
        return behave(body, request)


@pytest.fixture
def upstreams() -> Upstreams:
    return Upstreams()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def gateway(upstreams, clock):
    api, metrics_app, gw = create_app(parse(config()), transport=httpx.MockTransport(upstreams.handler),
                                      limiter=Limiter(clock=clock), health_checks=False)
    await gw.pool.probe_all()
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://gw")
    metrics = httpx.AsyncClient(transport=httpx.ASGITransport(app=metrics_app), base_url="http://gw")
    yield client, gw, metrics
    await client.aclose()
    await metrics.aclose()
    await gw.client.aclose()


def auth(team: str) -> dict:
    return {"Authorization": f"Bearer {KEYS[team]}"}


def chat(model: str = "qwen-small", stream: bool = False, text: str = "What is 2+2?") -> dict:
    return {"model": model, "stream": stream, "messages": [{"role": "user", "content": text}]}


def metric(text: str, name: str, **labels) -> float:
    """Value of a sample in Prometheus text format (0 if absent)."""
    for line in text.splitlines():
        if line.startswith(name + "{") or line.startswith(name + " "):
            if all(f'{k}="{v}"' in line for k, v in labels.items()):
                return float(line.rsplit(" ", 1)[1])
    return 0.0
