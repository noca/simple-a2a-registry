#!/bin/bash
# ── A2A Coder Agent Process Supervisor ──────────────────────────────
# Auto-restarts the agent if it crashes or exits unexpectedly.
# Run: ./run_a2a_agent.sh
# Stop: touch /tmp/a2a-agent.stop && ./run_a2a_agent.sh  (or Ctrl+C)
#
# For systemd integration, see companion service file.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
STOP_FILE="/tmp/a2a-agent.stop"
MAX_RESTARTS=10
RESTART_WINDOW=300  # seconds
RESTART_DELAY=2

cleanup() {
    rm -f "$STOP_FILE"
}
trap cleanup EXIT

restart_count=0
first_failure=0

while true; do
    if [ -f "$STOP_FILE" ]; then
        echo "[supervisor] STOP_FILE detected, exiting."
        exit 0
    fi

    echo "[supervisor] Starting A2A Coder Agent..."
    cd "$SCRIPT_DIR"

    # Run the agent; on non-zero exit, restart
    python a2a_coder_agent.py
    EXIT_CODE=$?

    if [ -f "$STOP_FILE" ]; then
        echo "[supervisor] STOP_FILE detected, exiting."
        exit 0
    fi

    echo "[supervisor] Agent exited with code $EXIT_CODE, restarting in ${RESTART_DELAY}s..."

    # Rate-limit restarts: if too many in a short window, give up
    now=$(date +%s)
    if [ $restart_count -eq 0 ]; then
        first_failure=$now
    fi
    restart_count=$((restart_count + 1))

    if [ $restart_count -ge $MAX_RESTARTS ] && [ $((now - first_failure)) -lt $RESTART_WINDOW ]; then
        echo "[supervisor] ERROR: $MAX_RESTARTS restarts in ${RESTART_WINDOW}s — giving up."
        exit 1
    fi

    # Reset counter if we've been stable for a window
    if [ $((now - first_failure)) -ge $RESTART_WINDOW ]; then
        restart_count=0
    fi

    sleep "$RESTART_DELAY"
done
