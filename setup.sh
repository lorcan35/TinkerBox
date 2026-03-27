#!/bin/bash
# TinkerBox setup — install all dependencies on Dragon Q6A
set -e

echo "======================================"
echo "  TinkerBox Setup"
echo "======================================"

# Python dependencies
echo "[1/3] Installing Python packages..."
pip3 install --break-system-packages aiohttp Pillow zeroconf 2>/dev/null || pip3 install aiohttp Pillow zeroconf

# Verify Chromium
echo "[2/3] Checking Chromium..."
if command -v chromium &> /dev/null; then
    echo "  Chromium found: $(chromium --version 2>/dev/null | head -1)"
elif command -v chromium-browser &> /dev/null; then
    echo "  Chromium found: $(chromium-browser --version 2>/dev/null | head -1)"
elif snap list chromium &> /dev/null 2>&1; then
    echo "  Chromium (snap) found"
else
    echo "  WARNING: Chromium not found. Install with:"
    echo "    sudo apt install chromium-browser"
    echo "    OR: sudo snap install chromium"
fi

# Make scripts executable
echo "[3/3] Setting permissions..."
chmod +x launch-chromium.sh start.sh install-services.sh 2>/dev/null || true

echo ""
echo "Setup complete! Run ./start.sh to launch TinkerBox."
