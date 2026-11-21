"""HTTP API: OpenAI-compatible /v1/chat/completions and /v1/models, plus health endpoints."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from prometheus_client import make_asgi_app

from .auth import authenticate
from .backends import Pool, State
from .config import Settings, Team
from .metrics import Metrics
from .ratelimit import Limiter

log = logging.getLogger("llm_gateway")
access = logging.getLogger("llm_gateway.access")
RETRYABLE_STATUS = {500, 502, 503, 504}


def error(status: int, message: str, kind: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": kind, "code": status}}, status, headers=headers)


def usage_tokens(payload: dict) -> tuple[int, int]:
    u = payload.get("usage") or {}
    return int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)


class Gateway:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None,
                 limiter: Limiter | None = None, health_checks: bool = True) -> None:
        self.settings = settings
        self.metrics = Metrics()
        self.limiter = limiter or Limiter()
        timeout = httpx.Timeout(settings.read_timeout, connect=settings.connect_timeout)
        self.client = httpx.AsyncClient(transport=transport, timeout=timeout,
                                        limits=httpx.Limits(max_connections=256, max_keepalive_connections=64))
        self.pool = Pool(settings, self.client, self.metrics)
        self.health_checks = health_checks

    # ------------------------------------------------------------------ bookkeeping
    def _account(self, team: Team, model: str, status: int, prompt: int, completion: int,
                  started: float, stream: bool, backend: str, ttft: float | None) -> None:
        m = self.metrics
        m.requests.labels(team.name, model, str(status)).inc()
        if prompt:
            m.tokens.labels(team.name, model, "prompt").inc(prompt)
        if completion:
            m.tokens.labels(team.name, model, "completion").inc(completion)
        m.duration.labels(model, str(stream).lower()).observe(time.monotonic() - started)
        self.limiter.record_tokens(team.name, team.requests_per_minute, team.tokens_per_minute, prompt + completion)
        access.info(json.dumps({  # never the prompt or the completion
            "team": team.name, "model": model, "backend": backend, "status": status, "stream": stream,
            "prompt_tokens": prompt, "completion_tokens": completion,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "ttft_ms": None if ttft is None else round(ttft * 1000),
        }))

    # ------------------------------------------------------------------ handlers
    async def chat_completions(self, request: Request) -> Response:
        started = time.monotonic()
        team = authenticate(request.headers.get("authorization"), self.settings.teams)
        if team is None:
            return error(401, "Missing or invalid API key.", "authentication_error", {"WWW-Authenticate": "Bearer"})
        raw = await request.body()
        if len(raw) > self.settings.max_body_bytes:
            return error(413, f"Request body exceeds {self.settings.max_body_bytes} bytes.", "invalid_request_error")
        try:
            body = json.loads(raw)
            model = body["model"]
            if not isinstance(model, str) or not isinstance(body.get("messages"), list):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            return error(400, "Body must be JSON with a string 'model' and a 'messages' list.", "invalid_request_error")
        if not self.pool.serves(model):
            return error(404, f"Model {model!r} is not served here.", "model_not_found")
        if not team.may_use(model):
            return error(403, f"This key may not use model {model!r}.", "permission_error")
        allowed, reason, retry = self.limiter.admit(team.name, team.requests_per_minute, team.tokens_per_minute)
        if not allowed:
            self.metrics.rate_limited.labels(team.name, reason).inc()
            self.metrics.requests.labels(team.name, model, "429").inc()
            return error(429, f"Rate limit exceeded ({reason} per minute).", "rate_limit_error",
                         {"Retry-After": str(retry)})
        stream = bool(body.get("stream"))
        if stream:
            body.setdefault("stream_options", {"include_usage": True})

        candidates = self.pool.candidates(model)
        if not candidates:
            self.metrics.requests.labels(team.name, model, "503").inc()
            return error(503, f"No healthy backend for {model!r}.", "service_unavailable", {"Retry-After": "5"})
        for attempt, state in enumerate(candidates):
            if attempt:
                self.metrics.failovers.labels(model).inc()
            response = await self._forward(state, body, team, model, stream, started)
            if response is not None:
                return response
        self.metrics.requests.labels(team.name, model, "502").inc()
        return error(502, "Every backend for this model failed.", "upstream_error")

    async def _forward(self, state: State, body: dict, team: Team, model: str, stream: bool,
                       started: float) -> Response | None:
        """The response, or None to try the next backend (connection error or retryable 5xx)."""
        url = f"{state.backend.url}/v1/chat/completions"
        self.pool.acquire(state)
        released = False
        try:
            req = self.client.build_request("POST", url, json=body, headers=self.pool.auth_headers(state))
            try:
                upstream = await self.client.send(req, stream=True)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError):
                self.pool.mark_failure(state)
                return None
            except httpx.TimeoutException:  # sent but no reply in time: do not run the generation twice
                self.pool.mark_failure(state)
                self._account(team, model, 504, 0, 0, started, stream, state.backend.name, None)
                return error(504, "The backend did not respond in time.", "timeout_error")
            if upstream.status_code in RETRYABLE_STATUS:
                await upstream.aclose()
                self.pool.mark_failure(state)
                return None
            self.pool.mark_success(state)
            if not stream or upstream.status_code != 200:
                payload_raw = await upstream.aread()
                await upstream.aclose()
                try:
                    payload = json.loads(payload_raw)
                except ValueError:
                    payload = {"error": {"message": "Backend returned a non-JSON body.", "type": "upstream_error"}}
                prompt, completion = usage_tokens(payload) if upstream.status_code == 200 else (0, 0)
                self._account(team, model, upstream.status_code, prompt, completion, started, stream,
                              state.backend.name, None)
                return JSONResponse(payload, upstream.status_code)
            released = True  # the stream generator owns the release from here
            return StreamingResponse(self._relay(upstream, state, team, model, started),
                                     media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        finally:
            if not released:
                self.pool.release(state)

    async def _relay(self, upstream: httpx.Response, state: State, team: Team, model: str,
                     started: float) -> AsyncIterator[bytes]:
        prompt = completion = 0
        first: float | None = None
        status = 200
        try:
            async for line in upstream.aiter_lines():
                if line.startswith("data:"):
                    data = line[5:].strip()
                    if data and data != "[DONE]":
                        with contextlib.suppress(ValueError):
                            chunk = json.loads(data)
                            if first is None and any((c.get("delta") or {}).get("content")
                                                     for c in chunk.get("choices") or []):
                                first = time.monotonic() - started
                                self.metrics.ttft.labels(model).observe(first)
                            if chunk.get("usage"):
                                prompt, completion = usage_tokens(chunk)
                yield (line + "\n").encode()
        except httpx.HTTPError:  # backend died mid-stream; the client sees a truncated stream
            status = 502
            self.pool.mark_failure(state)
        finally:
            await upstream.aclose()
            self.pool.release(state)
            self._account(team, model, status, prompt, completion, started, True, state.backend.name, first)

    async def list_models(self, request: Request) -> Response:
        team = authenticate(request.headers.get("authorization"), self.settings.teams)
        if team is None:
            return error(401, "Missing or invalid API key.", "authentication_error", {"WWW-Authenticate": "Bearer"})
        data = [{"id": m, "object": "model", "owned_by": "self-hosted"} for m in self.settings.models if team.may_use(m)]
        return JSONResponse({"object": "list", "data": data})


def create_app(settings: Settings, **kwargs):
    """(API app, metrics app, gateway). The metrics app is served on a separate port."""
    gw = Gateway(settings, **kwargs)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        task = None
        if gw.health_checks:
            await gw.pool.probe_all()  # know backend health before taking traffic
            task = asyncio.create_task(gw.pool.run_health_checks())
        yield
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await gw.client.aclose()

    api = FastAPI(title="llm-gateway", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    api.add_api_route("/v1/chat/completions", gw.chat_completions, methods=["POST"])
    api.add_api_route("/v1/models", gw.list_models, methods=["GET"])
    api.add_api_route("/healthz", lambda: {"status": "ok"}, methods=["GET"])

    async def readyz() -> Response:
        return JSONResponse({"ready": True}) if gw.pool.ready() else JSONResponse({"ready": False}, 503)

    api.add_api_route("/readyz", readyz, methods=["GET"])
    metrics_app = make_asgi_app(registry=gw.metrics.registry)  # answers /metrics directly, no redirect
    return api, metrics_app, gw
