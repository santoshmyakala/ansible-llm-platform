# Operations

Day-2 tasks. Every change is an inventory edit followed by `site.yml`; nothing is changed by hand on
the hosts. Run `verify.yml` afterwards to prove the platform still works.

```bash
ansible-playbook -i inventories/<site> playbooks/site.yml --ask-vault-pass
ansible-playbook -i inventories/<site> playbooks/verify.yml --ask-vault-pass
```

## Add a model

1. Add it to `platform_models` in `group_vars/all/main.yml`:
   - **llama.cpp (GGUF):** `hf_repo`, `hf_file`, `revision`. Leave `sha256` empty for the first download. The role then
     verifies the file against the SHA-256 that Hugging Face publishes. Copy the hash from
     `/opt/llm-platform/models/<model>/SOURCE.json` into `sha256` to pin it.
   - **vLLM (NVIDIA):** `hf_model` and `max_model_len`.
2. Add an instance to a host's `platform_inference_instances`, with a free `port`.
3. If only some teams may use it, add the model name to those teams' `models` in the vault.
4. Run `site.yml`.

The firewall rule for the new port, the gateway backend and the Prometheus target are all generated
from the inventory, so nothing else changes.

## Add an inference node

Add the host with its `platform_address` to the `inference` group, give it `platform_gpu_vendor` and
`platform_inference_instances`, and run `site.yml`. The new node only accepts the gateway and
monitoring hosts, the gateway starts routing to it once its health check passes, and Prometheus
scrapes it.

## Rolling update (new model version, llama.cpp or vLLM upgrade)

Change the image in `platform_images` or the model's `revision`, then run `site.yml`. The inference
play runs **one node at a time** (`serial: 1`):
- While a node's model server restarts, the gateway sees connection errors and **fails over** to
  the other nodes serving the model.
- Two failed health checks remove the node from rotation until it is healthy again.

A model served by a single instance has a short outage during its restart. Run two instances of any
model that needs to stay up.

## Rotate a team's key

1. Generate a new key: `openssl rand -hex 32`.
2. Put it in the team's entry in the vault and run `site.yml --limit gateway`. The gateway validates the new
   configuration and restarts in about a second. The old key stops working immediately.
3. Hand the new key to the team.

Keys are never stored on the gateway host. Its configuration holds only their SHA-256.

## Change a team's limits

Edit `requests_per_minute`, `tokens_per_minute` or `models` for the team in the vault, then run
`site.yml --limit gateway`. Watch `gateway_rate_limited_total` and the *Teams* row of the dashboard.

## Rotate the backend key

Change `vault_backend_api_key` and run `site.yml`. The inference nodes restart one by one with the
new key, then the gateway and Prometheus pick it up. Expect failed requests for the minute between
the first node restarting and the gateway being updated. For zero impact, do it in a quiet window.

## Respond to alerts

| Alert | First steps |
|---|---|
| `NoHealthyBackends` | Every request fails. Check `docker ps` and `docker logs llm-<instance>` on the inference nodes: a model may be failing to load (out of memory, missing file). The gateway's `/readyz` returns 503 until one backend is healthy. |
| `ModelServerDown` | The other backends carry the load. Check the node's container logs and `nvidia-smi` or `free -g`, then re-run `site.yml --limit <node>`. |
| `TimeToFirstTokenSlow` / `RequestsQueueing` | Demand exceeds capacity. Add an instance or node, raise `parallel`, or lower the busiest team's `requests_per_minute`. |
| `GatewayErrorRateHigh` | Split `gateway_requests_total` by `status`: 502/503 means a backend problem, 504 means generations are exceeding `llm_gateway_read_timeout`. |
| `TeamThrottled` | A team keeps hitting its limit. Either their limit is too low for a legitimate workload or a client is retrying in a loop. |
| `DiskAlmostFull` | Models are large. Remove unused models from `/opt/llm-platform/models` after removing them from the inventory. |

## Where things are on a host

| Path | Contents |
|---|---|
| `/etc/llm-platform/firewall.nft` | The node's firewall (`systemctl reload llm-platform-firewall` to apply) |
| `/opt/llm-platform/models/<model>/` | GGUF files plus `SOURCE.json` (repository, commit, SHA-256) |
| `/opt/llm-platform/gateway/config.yml` | Gateway configuration (hashed team keys) |
| `/opt/llm-platform/monitoring/` | Prometheus, Alertmanager and Grafana configuration and data |
| `docker logs llm-gateway` | One JSON line per request: team, model, backend, status, tokens, latency. Never prompt text. |
