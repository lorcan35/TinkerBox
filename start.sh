#!/bin/bash
# Start TinkerBox — Chromium + Dragon Server
#
# Usage: ./start.sh [--no-chromium]

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CDP_PORT="${CDP_PORT:-9222}"
DRAGON_PORT="${DRAGON_PORT:-3501}"

echo "======================================"
echo "  TinkerBox — Dragon Server Stack"
echo "======================================"

# Start Chromium (unless --no-chromium)
if [[ "$1" != "--no-chromium" ]]; then
    echo "[1/2] Starting Chromium..."
    bash "$SCRIPT_DIR/launch-chromium.sh" &

    # Wait for CDP to be ready
    echo "  Waiting for CDP on port $CDP_PORT..."
    for i in $(seq 1 30); do
        if curl -s "http://127.0.0.1:$CDP_PORT/json" > /dev/null 2>&1; then
            echo "  CDP ready!"
            break
        fi
        sleep 1
        if [ "$i" -eq 30 ]; then
            echo "  WARNING: CDP not responding after 30s. Dragon server will retry."
        fi
    done
else
    echo "[1/2] Skipping Chromium (--no-chromium)"
fi

# Start Dragon server
echo "[2/2] Starting Dragon server on port $DRAGON_PORT..."
exec python3 "$SCRIPT_DIR/dragon_server.py"
