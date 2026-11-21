"""A small, auditable gateway in front of self-hosted LLM inference servers.

It authenticates teams by API key (stored only as SHA-256 digests), enforces per-team request and
token rates, routes each request to a healthy backend that serves the requested model (least
in-flight first, failing over on connection errors and 5xx), and exports Prometheus metrics.
Prompts and completions are never logged.
"""

__version__ = "1.0.0"
