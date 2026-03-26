#!/bin/bash
# Install TinkerBox as systemd services for auto-start on boot
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
USER="${SUDO_USER:-$(whoami)}"

echo "Installing TinkerBox systemd services..."
echo "  Directory: $SCRIPT_DIR"
echo "  User: $USER"

# Chromium service
cat > /etc/systemd/system/tinkerbox-chromium.service << EOF
[Unit]
Description=TinkerBox Chromium (CDP)
After=network-online.target graphical.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
Environment=DISPLAY=:0
Environment=CDP_PORT=18800
WorkingDirectory=$SCRIPT_DIR
ExecStart=/bin/bash $SCRIPT_DIR/launch-chromium.sh
Restart=on-failure
RestartSec=5

[Install]
WantedBy=graphical.target
EOF

# Dragon server service
cat > /etc/systemd/system/tinkerbox-dragon.service << EOF
[Unit]
Description=TinkerBox Dragon Server
After=network-online.target tinkerbox-chromium.service
Wants=network-online.target
Requires=tinkerbox-chromium.service

[Service]
Type=simple
User=$USER
WorkingDirectory=$SCRIPT_DIR
ExecStartPre=/bin/sleep 5
ExecStart=/usr/bin/python3 $SCRIPT_DIR/dragon_server.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable tinkerbox-chromium tinkerbox-dragon

echo ""
echo "Services installed! Commands:"
echo "  sudo systemctl start tinkerbox-chromium tinkerbox-dragon"
echo "  sudo systemctl status tinkerbox-dragon"
echo "  journalctl -u tinkerbox-dragon -f"
