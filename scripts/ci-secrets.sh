#!/usr/bin/env bash
# Writes throw-away secrets for the CI inventory (git-ignored). Real deployments use ansible-vault.
set -euo pipefail
out=inventories/ci/group_vars/all/secrets.yml
r() { openssl rand -hex 32; }
cat > "$out" <<YAML
---
vault_backend_api_key: "$(r)"
vault_grafana_admin_password: "$(r)"
vault_webui_secret_key: "$(r)"
vault_webui_admin_email: "ci-admin@example.com"
vault_webui_admin_password: "$(r)"
vault_hf_token: ""
vault_gateway_teams:
  - {name: ci-research, key: "$(r)", requests_per_minute: 600, tokens_per_minute: 1000000, models: ["*"]}
  - {name: ci-limited, key: "$(r)", requests_per_minute: 3, tokens_per_minute: 1000000, models: ["*"]}
  - {name: chat-ui, key: "$(r)", requests_per_minute: 300, tokens_per_minute: 1000000, models: ["*"]}
YAML
chmod 600 "$out"
echo "wrote $out"
