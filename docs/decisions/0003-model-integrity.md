# ADR 0003: Verify every model file

## Status
Accepted

## Context
Model files are multi-gigabyte binaries fetched from the internet and loaded by a C++ parser. A
truncated or substituted file must never be served.

## Decision
Every GGUF download is checked with `get_url`'s SHA-256 verification. The expected hash is:
1. the `sha256` pinned in the inventory when set; otherwise
2. the SHA-256 that Hugging Face publishes for that exact file (its `X-Linked-ETag`), read before the
   download.

The download fails if neither is available. A `SOURCE.json` next to each model records the
repository, the resolved commit and the hash.

## Consequences
- Production inventories should pin `sha256` (copied from `SOURCE.json`), so a file that changes
  upstream is refused rather than silently replaced.
- vLLM downloads its own weights through the Hugging Face client, which verifies files itself; those
  models are pinned by repository and can be pinned further with `--revision` in `extra_args`.
