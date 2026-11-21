#!/usr/bin/env python3
"""Load-test the gateway with streamed chat completions and report what users experience.

For each concurrency level it sends N requests and measures, per request, the time to first token
(TTFT) and the decode rate (completion tokens / time after the first token), plus aggregate
throughput (all completion tokens / wall time). Numbers are only meaningful for the hardware they
were measured on; the CI run reports a GitHub-hosted runner's CPU.

    python3 scripts/benchmark.py --url http://10.20.0.31:8000 --key "$KEY" --model qwen2.5-1.5b-instruct
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time

import httpx

PROMPT = ("Explain, in about 150 words, why a platform team puts an API gateway in front of "
          "self-hosted language models.")


async def one(client: httpx.AsyncClient, url: str, key: str, model: str, max_tokens: int) -> dict:
    body = {"model": model, "stream": True, "max_tokens": max_tokens, "temperature": 0.7,
            "messages": [{"role": "user", "content": PROMPT}]}
    start = time.perf_counter()
    first = None
    completion = 0
    async with client.stream("POST", f"{url}/v1/chat/completions", json=body,
                             headers={"Authorization": f"Bearer {key}"}) as r:
        if r.status_code != 200:
            await r.aread()
            return {"ok": False, "status": r.status_code}
        async for line in r.aiter_lines():
            if not line.startswith("data:") or line.strip() == "data: [DONE]":
                continue
            chunk = json.loads(line[5:])
            if first is None and any((c.get("delta") or {}).get("content") for c in chunk.get("choices") or []):
                first = time.perf_counter()
            if chunk.get("usage"):
                completion = int(chunk["usage"].get("completion_tokens") or 0)
    end = time.perf_counter()
    decode = completion / (end - first) if first and end > first and completion > 1 else None
    return {"ok": first is not None, "status": 200, "ttft": (first - start) if first else None,
            "duration": end - start, "completion_tokens": completion, "decode_tps": decode}


def pct(values: list[float], q: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, round(q * (len(s) - 1)))]


async def level(url: str, key: str, model: str, concurrency: int, requests: int, max_tokens: int) -> dict:
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=5)) as client:
        async def guarded():
            async with sem:
                return await one(client, url, key, model, max_tokens)
        wall = time.perf_counter()
        results = await asyncio.gather(*(guarded() for _ in range(requests)))
        wall = time.perf_counter() - wall
    ok = [r for r in results if r["ok"]]
    ttft = [r["ttft"] for r in ok]
    decode = [r["decode_tps"] for r in ok if r["decode_tps"]]
    tokens = sum(r["completion_tokens"] for r in ok)
    return {"concurrency": concurrency, "requests": requests, "succeeded": len(ok),
            "ttft_p50": statistics.median(ttft) if ttft else None, "ttft_p95": pct(ttft, 0.95) if ttft else None,
            "decode_tps_per_request": statistics.median(decode) if decode else None,
            "aggregate_tps": tokens / wall if wall else None, "completion_tokens": tokens, "wall_seconds": wall}


def markdown(rows: list[dict], model: str, note: str) -> str:
    f = lambda v, d=2: "–" if v is None else f"{v:.{d}f}"
    out = [f"### Benchmark: `{model}` through the gateway", "", note, "",
           "| Concurrency | Requests OK | TTFT p50 (s) | TTFT p95 (s) | Decode tok/s per request | Aggregate tok/s |",
           "|---:|---:|---:|---:|---:|---:|"]
    for r in rows:
        out.append(f"| {r['concurrency']} | {r['succeeded']}/{r['requests']} | {f(r['ttft_p50'])} | {f(r['ttft_p95'])} | "
                   f"{f(r['decode_tps_per_request'], 1)} | {f(r['aggregate_tps'], 1)} |")
    return "\n".join(out) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--concurrency", default="1,4")
    ap.add_argument("--requests", type=int, default=8)
    ap.add_argument("--max-tokens", type=int, default=128)
    ap.add_argument("--note", default="")
    ap.add_argument("--json", help="also write raw results here")
    args = ap.parse_args()
    rows = [asyncio.run(level(args.url, args.key, args.model, int(c), args.requests, args.max_tokens))
            for c in args.concurrency.split(",")]
    print(markdown(rows, args.model, args.note))
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(rows, fh, indent=2)
    sys.exit(0 if all(r["succeeded"] == r["requests"] for r in rows) else 1)


if __name__ == "__main__":
    main()
