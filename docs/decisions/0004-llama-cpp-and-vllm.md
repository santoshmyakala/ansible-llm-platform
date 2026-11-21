# ADR 0004: llama.cpp and vLLM, chosen per node

## Status
Accepted

## Context
The platform has to run on whatever hardware a site has: NVIDIA servers, AMD or Intel GPUs, and
CPU-only machines.

## Decision
- **vLLM** on NVIDIA GPUs, for throughput: continuous batching, paged KV cache and tensor parallelism.
- **llama.cpp** everywhere else, using the build for the hardware: CPU, CUDA, or Vulkan for AMD and
  Intel GPUs. It runs quantised GGUF models in modest memory.
- The engine is a property of the model in the catalog. The GPU vendor is a property of the host. The
  inference role picks the image and device access from both.
- Prometheus recording rules map both engines' metrics onto common names (`platform:*`), so
  dashboards and alerts don't care which engine served a request.

## Consequences
- The same model name can be served by different engines on different nodes, and the gateway treats
  them as one pool.
- Only the CPU path runs in CI (GitHub runners have no GPU). The NVIDIA and Vulkan paths are linted and
  rendered, and their image tags and flags were checked against the pinned upstream sources, but they
  are not executed in CI.
