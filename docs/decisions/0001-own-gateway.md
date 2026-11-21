# ADR 0001: A small, in-house gateway

## Status
Accepted

## Context
Teams need one OpenAI-compatible endpoint with their own keys, and the platform needs per-team limits,
routing across model servers, failover and metrics. Off-the-shelf LLM proxies can do this and much
more: many providers, spend databases, admin UIs.

## Decision
Write a small gateway (`gateway/`, about 500 lines of Python) that does only what this platform needs:
- keys stored as SHA-256 digests;
- per-team request and token rates;
- least-in-flight routing with failover;
- streaming pass-through;
- Prometheus metrics on a separate port;
- an access log without prompt content.

## Consequences
- The whole security-relevant surface is reviewable in an afternoon and has 26 tests.
- It needs no database: configuration is a file rendered by Ansible and validated with `--check` before use.
- Features such as per-key budgets over months, cloud providers or a key-management UI are not provided.
  If they become requirements, an established proxy can replace the gateway. Clients are unaffected
  because both speak the OpenAI API.
