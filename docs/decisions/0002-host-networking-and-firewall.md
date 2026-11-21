# ADR 0002: Host networking, and a firewall table of our own

## Status
Accepted

## Context
Docker publishes container ports with DNAT rules in its own tables. That traffic never reaches the
host's INPUT chain, so a host firewall would not protect published ports. Separately, the usual
`nftables.conf` begins with `flush ruleset`, which on a Docker host also deletes Docker's rules and
breaks container networking until the daemon restarts.

## Decision
- Every platform container uses **host networking**: no published ports, so the INPUT chain governs
  every service. `verify.yml` asserts that no platform container publishes a port.
- The firewall lives in its own table, `inet llm_platform`. It is declared, deleted and recreated in one
  atomic load, and is loaded by a dedicated systemd unit whose stop removes only that table.
- Rules are generated from the inventory, per host and per service:
  - SSH and Grafana: admin networks only;
  - gateway API and chat UI: client networks only;
  - model servers: the gateway and monitoring hosts only;
  - metrics endpoints: the monitoring host only.

## Consequences
- One place decides reachability, and it follows the inventory automatically.
- Services bind to the node's address, and each port is unique per host (enforced in preflight).
- vLLM's own documentation says its API key does not protect every endpoint. The firewall, not the key,
  is the control that keeps model servers private.
