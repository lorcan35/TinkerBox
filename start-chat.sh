#!/bin/bash
# Start voice server + dashboard for Tinker web chat
# Access: http://192.168.1.89:3500/chat

cd "$(dirname "$0")"

export TINKERCLAW_DB_PATH=/home/rebelforce/tinkerclaw/tinkerclaw.db
mkdir -p /home/rebelforce/tinkerclaw

echo "[1/2] Starting voice server (port 3502)..."
python3 -m dragon_voice --log-level WARNING > /tmp/voice_server.log 2>&1 &
VPID=$!

echo "      Waiting for voice server..."
for i in $(seq 1 15); do
    curl -s http://127.0.0.1:3502/health >/dev/null 2>&1 && break
    sleep 1
done
echo "      Voice server ready (PID $VPID)"

echo "[2/2] Starting dashboard (port 3500)..."
python3 dashboard.py > /tmp/dashboard.log 2>&1 &
DPID=$!
sleep 2

echo ""
echo "  ✓ Chat ready at: http://192.168.1.89:3500/chat"
echo "  ✓ Dashboard at:  http://192.168.1.89:3500"
echo ""
echo "  Voice server PID: $VPID"
echo "  Dashboard PID:    $DPID"
echo "  Logs: /tmp/voice_server.log  /tmp/dashboard.log"
