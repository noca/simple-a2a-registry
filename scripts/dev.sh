#!/usr/bin/env bash
# =============================================================================
# dev.sh — Development A2A Registry
#
# Launches a local registry for development with:
#   • SQLite (no external deps)
#   • Auth disabled (no tokens needed)
#   • DEBUG logging
#   • Dedicated data dir under data/dev/
#   • WebUI served at /
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")/.." || exit 1

DATA_DIR="data/dev"
mkdir -p "$DATA_DIR/workspaces"

exec python -m simple_a2a_registry \
  --host 0.0.0.0 \
  --port 8321 \
  --data-dir "$DATA_DIR" \
  --log-level DEBUG \
  --log-format text \
  --no-auth-enabled \
  --board-path "$DATA_DIR/board.db" \
  --workspaces-root "$DATA_DIR/workspaces" \
  --dispatcher-enabled \
  --dispatcher-interval 5 \
  "$@"