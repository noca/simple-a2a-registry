#!/usr/bin/env bash
# =============================================================================
# prod.sh — Production A2A Registry
#
# Launches the registry in production mode with:
#   • MySQL database (configured via ~/.simple-a2a-registry/config.yaml)
#   • Auth enabled (OAuth 2.1 / Bearer JWT)
#   • INFO logging
#   • Standard data dir (~/.simple-a2a-registry/)
#   • WebUI served at / with auth integration
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")/.." || exit 1

DATA_DIR="${HOME}/.simple-a2a-registry"
mkdir -p "$DATA_DIR/workspaces"

exec env ADMIN_PASSWORD=admin123 python -m simple_a2a_registry server \
  --host 0.0.0.0 \
  --port 8321 \
  --data-dir "$DATA_DIR" \
  --log-level INFO \
  --log-format json \
  --auth-enabled \
  --workspaces-root "$DATA_DIR/workspaces" \
  --dispatcher-enabled \
  --dispatcher-interval 5 \
  --bootstrap-secret admin123 \
  "$@"
