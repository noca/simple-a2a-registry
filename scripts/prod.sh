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
#   • Graceful shutdown via SIGTERM handler
#
# Usage:
#   ./scripts/prod.sh                        # default: enforce mode
#   ./scripts/prod.sh --mode warn            # warn mode
#   ./scripts/prod.sh --mode audit           # audit-only mode
#   SIMPLE_A2A_REGISTRY_MODE=warn ./scripts/prod.sh
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")/.." || exit 1

# ---------------------------------------------------------------------------
# Configuration — env vars with sensible defaults
# ---------------------------------------------------------------------------

DATA_DIR="${SIMPLE_A2A_REGISTRY_DATA_DIR:-${HOME}/.simple-a2a-registry}"
HOST="${SIMPLE_A2A_REGISTRY_HOST:-0.0.0.0}"
PORT="${SIMPLE_A2A_REGISTRY_PORT:-8321}"
LOG_LEVEL="${SIMPLE_A2A_REGISTRY_LOG_LEVEL:-INFO}"
LOG_FORMAT="${SIMPLE_A2A_REGISTRY_LOG_FORMAT:-json}"
LOG_FILE="${SIMPLE_A2A_REGISTRY_LOG_FILE:-}"
AUTH_ENABLED="${SIMPLE_A2A_REGISTRY_AUTH_ENABLED:-true}"
BOOTSTRAP_SECRET="${SIMPLE_A2A_REGISTRY_BOOTSTRAP_SECRET:-admin123}"
MODE="${SIMPLE_A2A_REGISTRY_MODE:-enforce}"
DISPATCHER_ENABLED="${SIMPLE_A2A_REGISTRY_DISPATCHER_ENABLED:-true}"
DISPATCHER_INTERVAL="${SIMPLE_A2A_REGISTRY_DISPATCHER_INTERVAL:-5}"

# ---------------------------------------------------------------------------
# Argument parsing — override env via --mode flag
# ---------------------------------------------------------------------------

MODE_FLAG=""
AUTH_FLAG=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --mode)
            if [[ -z "${2:-}" ]]; then
                echo "ERROR: --mode requires one of: enforce, warn, audit" >&2
                exit 1
            fi
            MODE="$2"
            shift 2
            ;;
        --mode=*)
            MODE="${1#*=}"
            shift
            ;;
        --auth-enabled)
            AUTH_ENABLED="true"
            shift
            ;;
        --auth-disabled)
            AUTH_ENABLED="false"
            shift
            ;;
        -h|--help)
            echo "Usage: $0 [--mode enforce|warn|audit] [--auth-enabled|--auth-disabled]"
            echo ""
            echo "Environment variables (all SIMPLE_A2A_REGISTRY_*):"
            echo "  SIMPLE_A2A_REGISTRY_MODE          enforce|warn|audit (default: enforce)"
            echo "  SIMPLE_A2A_REGISTRY_DATA_DIR      data directory (default: ~/.simple-a2a-registry)"
            echo "  SIMPLE_A2A_REGISTRY_HOST          bind address (default: 0.0.0.0)"
            echo "  SIMPLE_A2A_REGISTRY_PORT          bind port (default: 8321)"
            echo "  SIMPLE_A2A_REGISTRY_LOG_LEVEL     DEBUG|INFO|WARNING|ERROR (default: INFO)"
            echo "  SIMPLE_A2A_REGISTRY_LOG_FORMAT    json|text (default: json)"
            echo "  SIMPLE_A2A_REGISTRY_LOG_FILE      log file path (default: stdout)"
            echo "  SIMPLE_A2A_REGISTRY_AUTH_ENABLED  true|false (default: true)"
            echo "  SIMPLE_A2A_REGISTRY_BOOTSTRAP_SECRET  admin bootstrap secret"
            echo "  SIMPLE_A2A_REGISTRY_DISPATCHER_ENABLED  true|false (default: true)"
            echo "  SIMPLE_A2A_REGISTRY_DISPATCHER_INTERVAL  poll interval in seconds (default: 5)"
            exit 0
            ;;
        *)
            echo "WARNING: unknown argument '$1' (forwarded to server)" >&2
            MODE_FLAG="$MODE_FLAG $1"
            shift
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Mode validation
# ---------------------------------------------------------------------------

case "$MODE" in
    enforce|warn|audit)
        echo "[prod.sh] Mode: $MODE"
        ;;
    *)
        echo "ERROR: invalid mode '$MODE'. Use: enforce, warn, or audit." >&2
        exit 1
        ;;
esac

# ---------------------------------------------------------------------------
# Ensure data directory
# ---------------------------------------------------------------------------

mkdir -p "$DATA_DIR/workspaces"

# ---------------------------------------------------------------------------
# Build auth flag
# ---------------------------------------------------------------------------

if [[ "$AUTH_ENABLED" == "true" ]]; then
    AUTH_FLAG="--auth-enabled"
else
    AUTH_FLAG="--auth-disabled"
fi

# ---------------------------------------------------------------------------
# Log file flag
# ---------------------------------------------------------------------------

LOG_FILE_FLAG=""
if [[ -n "$LOG_FILE" ]]; then
    LOG_FILE_FLAG="--log-file $LOG_FILE"
fi

# ---------------------------------------------------------------------------
# Launch with graceful shutdown
# ---------------------------------------------------------------------------

echo "[prod.sh] Starting Simple A2A Registry (mode=$MODE, auth=$AUTH_ENABLED, port=$PORT)"
echo "[prod.sh] Data dir: $DATA_DIR"

# Trap SIGTERM/SIGINT for graceful shutdown
cleanup() {
    echo ""
    echo "[prod.sh] Received shutdown signal — stopping gracefully..."
    echo "[prod.sh] Waiting for in-flight tasks to complete (up to 30s)..."
    # The server's internal SIGTERM handler + stop_grace_period handles this
    # in docker-compose; on bare-metal we send SIGTERM to the server process.
    if [[ -n "${SERVER_PID:-}" ]]; then
        kill -TERM "$SERVER_PID" 2>/dev/null || true
        wait "$SERVER_PID" 2>/dev/null || true
    fi
    echo "[prod.sh] Shutdown complete."
    exit 0
}
trap cleanup SIGTERM SIGINT

# Launch server in background so we can trap signals
python -m simple_a2a_registry server \
    --host "$HOST" \
    --port "$PORT" \
    --data-dir "$DATA_DIR" \
    --log-level "$LOG_LEVEL" \
    --log-format "$LOG_FORMAT" \
    $LOG_FILE_FLAG \
    $AUTH_FLAG \
    --workspaces-root "$DATA_DIR/workspaces" \
    --dispatcher-enabled \
    --dispatcher-interval "$DISPATCHER_INTERVAL" \
    --bootstrap-secret "$BOOTSTRAP_SECRET" \
    $MODE_FLAG &
SERVER_PID=$!

echo "[prod.sh] Server PID: $SERVER_PID"

# Wait for server process
wait "$SERVER_PID"