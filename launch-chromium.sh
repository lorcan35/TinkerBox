#!/bin/bash
# Launch Chromium with CDP remote debugging for TinkerBox
#
# Tab5 display: 720x1280 portrait
# CDP port: 9222 (dragon_server.py connects here)

CDP_PORT="${CDP_PORT:-9222}"
WINDOW_SIZE="720,1280"
START_URL="${START_URL:-https://www.google.com}"

echo "[TinkerBox] Starting Chromium (CDP port $CDP_PORT, viewport $WINDOW_SIZE)"

# Kill existing Chromium instances
pkill -f "chromium.*remote-debugging-port" 2>/dev/null
sleep 1

exec chromium \
    --remote-debugging-port="$CDP_PORT" \
    --remote-allow-origins="*" \
    --window-size="$WINDOW_SIZE" \
    --user-agent="Mozilla/5.0 (Linux; Android 14; Tab5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36" \
    --disable-gpu \
    --no-sandbox \
    --disable-dev-shm-usage \
    --disable-extensions \
    --disable-background-networking \
    --disable-sync \
    --no-first-run \
    --no-default-browser-check \
    --disable-translate \
    --disable-features=TranslateUI \
    --autoplay-policy=no-user-gesture-required \
    "$START_URL" \
    2>/dev/null &

echo "[TinkerBox] Chromium PID: $!"
echo "[TinkerBox] CDP endpoint: http://127.0.0.1:$CDP_PORT"
