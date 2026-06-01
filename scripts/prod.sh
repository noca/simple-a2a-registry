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
#
# Environment:
#   ADMIN_PASSWORD   — admin account password (default: auto-generated, logged)
#   A2A_REGISTRY_*   — config overrides, e.g. A2A_REGISTRY_SERVER__PORT=9000
#
# NOTE: Ensure MySQL is running and the DSN in config.yaml is correct before
#       starting. Configure TLS via --tls-cert / --tls-key if needed.
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")/.." || exit 1

# Use the standard persistent data dir where config.yaml lives
DATA_DIR="${HOME}/.simple-a2a-registry"

mkdir -p "$DATA_DIR/workspaces"

exec python -m simple_a2a_registry \
  --host 0.0.0.0 \
  --port 8321 \
  --data-dir "$DATA_DIR" \
  --log-level INFO \
  --log-format json \
  --auth-enabled \
  --board-path "$DATA_DIR/board.db" \
  --workspaces-root "$DATA_DIR/workspaces" \
  --dispatcher-enabled \
  --dispatcher-interval 5 \
  "$@"