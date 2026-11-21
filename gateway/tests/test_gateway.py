import json
import logging

import httpx

from .conftest import auth, chat, completion, metric, sse


async def test_rejects_missing_and_wrong_keys(gateway):
    client, _, _ = gateway
    r = await client.post("/v1/chat/completions", json=chat())
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    r = await client.post("/v1/chat/completions", json=chat(), headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401
    assert (await client.get("/v1/models")).status_code == 401


async def test_proxies_a_completion_and_counts_tokens(gateway, upstreams):
    client, _, metrics = gateway
    r = await client.post("/v1/chat/completions", json=chat(), headers=auth("research"))
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"].startswith("from node-")
    m = (await metrics.get("/metrics")).text
    assert metric(m, "gateway_requests_total", team="research", model="qwen-small", status="200") == 1
    assert metric(m, "gateway_tokens_total", team="research", kind="prompt") == 7
    assert metric(m, "gateway_tokens_total", team="research", kind="completion") == 3


async def test_backend_key_is_sent_only_to_its_backend(gateway, upstreams):
    client, _, _ = gateway
    for _ in range(4):
        await client.post("/v1/chat/completions", json=chat(), headers=auth("research"))
    hosts = {host: headers.get("authorization") for host, _, headers, _ in upstreams.calls}
    assert hosts["node-a"] == "Bearer backend-secret"
    assert hosts["node-b"] is None  # node-b has no key configured; the client's key is never forwarded


async def test_requests_are_spread_across_backends(gateway, upstreams):
    client, _, _ = gateway
    for _ in range(6):
        await client.post("/v1/chat/completions", json=chat(), headers=auth("research"))
    served = [host for host, *_ in upstreams.calls]
    assert served.count("node-a") == 3 and served.count("node-b") == 3


async def test_model_permissions_and_unknown_models(gateway):
    client, _, _ = gateway
    r = await client.get("/v1/models", headers=auth("ops"))
    assert [m["id"] for m in r.json()["data"]] == ["qwen-small"]
    r = await client.post("/v1/chat/completions", json=chat("coder"), headers=auth("ops"))
    assert r.status_code == 403
    r = await client.post("/v1/chat/completions", json=chat("not-a-model"), headers=auth("research"))
    assert r.status_code == 404 and r.json()["error"]["type"] == "model_not_found"


async def test_rejects_malformed_and_oversized_bodies(gateway):
    client, _, _ = gateway
    r = await client.post("/v1/chat/completions", content=b"{not json", headers=auth("research"))
    assert r.status_code == 400
    r = await client.post("/v1/chat/completions", json={"model": "qwen-small"}, headers=auth("research"))
    assert r.status_code == 400
    big = chat(text="x" * 2_000_000)
    r = await client.post("/v1/chat/completions", json=big, headers=auth("research"))
    assert r.status_code == 413


async def test_fails_over_when_a_backend_errors_and_marks_it_down(gateway, upstreams):
    client, gw, metrics = gateway
    upstreams.behaviour["node-a"] = lambda body, req: httpx.Response(503, json={"error": "overloaded"})
    for _ in range(4):
        r = await client.post("/v1/chat/completions", json=chat(), headers=auth("research"))
        assert r.status_code == 200 and "node-b" in r.json()["choices"][0]["message"]["content"]
    assert gw.pool.states["node-a"].healthy is False  # unhealthy_after = 2
    m = (await metrics.get("/metrics")).text
    assert metric(m, "gateway_failovers_total", model="qwen-small") >= 1
    assert metric(m, "gateway_backend_up", backend="node-a") == 0


async def test_connection_errors_fail_over_too(gateway, upstreams):
    client, _, _ = gateway

    def refuse(body, req):
        raise httpx.ConnectError("refused", request=req)

    upstreams.behaviour["node-b"] = refuse
    for _ in range(3):
        r = await client.post("/v1/chat/completions", json=chat(), headers=auth("research"))
        assert r.status_code == 200


async def test_a_read_timeout_is_not_retried_elsewhere(gateway, upstreams):
    client, gw, _ = gateway
    gw.pool.states["node-b"].healthy = False  # make node-a the only candidate first

    def slow(body, req):
        raise httpx.ReadTimeout("slow", request=req)

    upstreams.behaviour["node-a"] = slow
    gw.pool.states["node-b"].healthy = True
    gw.pool.states["node-a"].inflight = -1  # sort node-a first
    r = await client.post("/v1/chat/completions", json=chat(), headers=auth("research"))
    assert r.status_code == 504
    assert [h for h, *_ in upstreams.calls] == ["node-a"]  # the generation was not run twice


async def test_all_backends_failing_returns_502_and_none_healthy_returns_503(gateway, upstreams):
    client, gw, _ = gateway
    for host in ("node-a", "node-b"):
        upstreams.behaviour[host] = lambda body, req: httpx.Response(500)
    r = await client.post("/v1/chat/completions", json=chat(), headers=auth("research"))
    assert r.status_code == 502
    r = await client.post("/v1/chat/completions", json=chat(), headers=auth("research"))
    r = await client.post("/v1/chat/completions", json=chat(), headers=auth("research"))
    assert r.status_code == 503 and r.headers["retry-after"] == "5"
    assert (await client.get("/readyz")).status_code == 503


async def test_backends_recover_through_health_checks(gateway, upstreams):
    client, gw, _ = gateway
    upstreams.health = {"node-a": 0, "node-b": 503}
    await gw.pool.probe_all()
    await gw.pool.probe_all()
    assert (await client.get("/readyz")).status_code == 503
    upstreams.health = {"node-a": 200, "node-b": 200}
    await gw.pool.probe_all()
    assert (await client.get("/readyz")).status_code == 200


async def test_request_rate_limit_with_retry_after(gateway, clock):
    client, _, metrics = gateway
    for _ in range(2):
        assert (await client.post("/v1/chat/completions", json=chat(), headers=auth("tight"))).status_code == 200
    # each response used 10 tokens (7 + 3): 20 of 50 used, but the request bucket (2/min) is empty
    r = await client.post("/v1/chat/completions", json=chat(), headers=auth("tight"))
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
    assert "requests" in r.json()["error"]["message"]
    clock.t += 30  # 2/min refills one request every 30 s
    assert (await client.post("/v1/chat/completions", json=chat(), headers=auth("tight"))).status_code == 200
    assert metric((await metrics.get("/metrics")).text, "gateway_rate_limited_total", team="tight",
                  reason="requests") == 1


async def test_token_rate_limit_charges_actual_usage(gateway, upstreams, clock):
    client, _, _ = gateway
    upstreams.behaviour["node-a"] = upstreams.behaviour["node-b"] = (
        lambda body, req: httpx.Response(200, json=completion("qwen-small", prompt=40, completion_tokens=30)))
    assert (await client.post("/v1/chat/completions", json=chat(), headers=auth("tight"))).status_code == 200
    clock.t += 30  # request bucket refilled; token bucket: 50 - 70 + 25 = 5 tokens left, still in credit
    assert (await client.post("/v1/chat/completions", json=chat(), headers=auth("tight"))).status_code == 200
    clock.t += 30  # tokens: 5 - 70 + 25 = -40 → in debt
    r = await client.post("/v1/chat/completions", json=chat(), headers=auth("tight"))
    assert r.status_code == 429 and "tokens" in r.json()["error"]["message"]
    clock.t += 60  # +50 tokens → back in credit
    assert (await client.post("/v1/chat/completions", json=chat(), headers=auth("tight"))).status_code == 200


async def test_streaming_relays_events_and_measures_ttft_and_tokens(gateway, upstreams):
    client, _, metrics = gateway
    upstreams.behaviour["node-a"] = upstreams.behaviour["node-b"] = (
        lambda body, req: httpx.Response(200, content=sse("qwen-small", ["Two", " plus", " two", " is", " four"]),
                                         headers={"content-type": "text/event-stream"}))
    r = await client.post("/v1/chat/completions", json=chat(stream=True), headers=auth("research"))
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = [json.loads(line[5:]) for line in r.text.splitlines()
              if line.startswith("data:") and line.strip() != "data: [DONE]"]
    text = "".join(c["choices"][0]["delta"]["content"] for c in events if c["choices"])
    assert text == "Two plus two is four" and r.text.rstrip().endswith("data: [DONE]")
    assert upstreams.calls[0][3]["stream_options"] == {"include_usage": True}  # asked for usage
    m = (await metrics.get("/metrics")).text
    assert metric(m, "gateway_time_to_first_token_seconds_count", model="qwen-small") == 1
    assert metric(m, "gateway_tokens_total", team="research", kind="completion") == 5


async def test_a_backend_dying_mid_stream_is_recorded_and_released(gateway, upstreams):
    client, gw, metrics = gateway

    async def broken():
        yield b'data: {"choices":[{"index":0,"delta":{"content":"Hel"}}]}\n\n'
        raise httpx.ReadError("connection reset")

    upstreams.behaviour["node-a"] = upstreams.behaviour["node-b"] = (
        lambda body, req: httpx.Response(200, content=broken(), headers={"content-type": "text/event-stream"}))
    r = await client.post("/v1/chat/completions", json=chat(stream=True), headers=auth("research"))
    assert "Hel" in r.text
    assert all(s.inflight == 0 for s in gw.pool.states.values())
    m = (await metrics.get("/metrics")).text
    assert metric(m, "gateway_requests_total", team="research", status="502") == 1


async def test_access_log_never_contains_prompts_or_completions(gateway, caplog):
    client, _, _ = gateway
    secret = "my secret prompt about project falcon"
    with caplog.at_level(logging.INFO, logger="llm_gateway.access"):
        await client.post("/v1/chat/completions", json=chat(text=secret), headers=auth("research"))
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert '"team": "research"' in logged
    assert "falcon" not in logged and "from node-" not in logged


async def test_metrics_are_not_served_on_the_api_port(gateway):
    client, _, _ = gateway
    assert (await client.get("/metrics")).status_code == 404
