# TinkerBox

Dragon-side server software for the **TinkerClaw** AI device.

TinkerBox runs on the Dragon Q6A (Radxa aarch64 SBC) and provides:
- **CDP Browser Streaming** — screencast of Chromium to Tab5 via MJPEG
- **Touch Forwarding** — Tab5 touch events become mouse clicks in the browser
- **Health/Handshake API** — Tab5 auto-discovers and connects to Dragon
- **AI Services** — Ollama (local LLM), Moonshine (STT), Piper (TTS)

## Architecture

```
Tab5 (ESP32-P4)                  Dragon Q6A
┌─────────────┐                  ┌──────────────────┐
│ LVGL UI     │ ──WiFi──────── │ dragon_server.py │
│             │  GET /stream    │   ├─ MJPEG stream │
│ MJPEG view  │ ◄────────────── │   ├─ Touch WS    │
│ Touch input │ ───────────────►│   ├─ Health API   │
│             │  WS /ws/touch   │   └─ CDP bridge   │
│ Dragon Link │  GET /health    │                    │
│             │  GET /api/hs    │ Chromium (CDP)     │
└─────────────┘                  │ Ollama (LLM)      │
                                 │ Moonshine (STT)   │
                                 │ Piper (TTS)       │
                                 └──────────────────┘
```

## Hardware

| Component | Details |
|-----------|---------|
| SBC | Radxa Dragon Q6A (Qualcomm, aarch64) |
| OS | Linux 6.18.2 (Debian-based) |
| RAM | 8GB |
| Storage | 64GB eMMC |
| Network | Gigabit Ethernet + WiFi |
| IP | 192.168.1.89 (static) |

## Quick Start

```bash
# 1. Clone to Dragon
ssh radxa@192.168.1.89
git clone https://github.com/lorcan35/TinkerBox.git
cd TinkerBox

# 2. Install dependencies
./setup.sh

# 3. Start everything
./start.sh

# Or install as systemd services for auto-start on boot
sudo ./install-services.sh
```

## Components

### dragon_server.py — CDP Streaming Server (Port 3501)

Connects to Chromium via Chrome DevTools Protocol and bridges to Tab5:

| Endpoint | Protocol | Description |
|----------|----------|-------------|
| `GET /` | HTTP | Status page |
| `GET /health` | HTTP | Health check for Tab5 discovery |
| `GET /api/handshake` | HTTP | Tab5 identifies itself, Dragon returns stream params |
| `GET /stream` | HTTP | MJPEG multipart stream (screencast frames) |
| `WS /ws/touch` | WebSocket | Touch events from Tab5 → CDP mouse events |

### launch-chromium.sh — Browser Launcher

Starts Chromium with:
- Remote debugging on port 9222 (CDP)
- Mobile viewport (720x1280) matching Tab5 display
- GPU acceleration disabled (software rendering for stability)
- No first-run dialogs

### start.sh — One-Command Launch

Starts Chromium + dragon_server.py in the correct order with health checking.

## Configuration

Edit `dragon_server.py` top-level constants:

```python
HOST = "0.0.0.0"           # Listen on all interfaces
PORT = 3501                 # Dragon server port
CDP_HOST = "127.0.0.1"     # Chromium CDP host
CDP_PORT = 9222             # Chromium CDP port
SCREENCAST_QUALITY = 60     # JPEG quality (0-100)
SCREENCAST_MAX_W = 720      # Match Tab5 display width
SCREENCAST_MAX_H = 1280     # Match Tab5 display height
SCREENCAST_FPS = 15         # Target FPS
```

## Systemd Services

After running `install-services.sh`:

```bash
# Check status
systemctl status tinkerbox-chromium
systemctl status tinkerbox-dragon

# View logs
journalctl -u tinkerbox-dragon -f

# Restart
sudo systemctl restart tinkerbox-dragon
```

## Requirements

- Python 3.10+
- aiohttp (`pip3 install aiohttp`)
- Chromium or Chrome with remote debugging support
- Network access to Tab5 on same LAN

## Companion Project

- **[TinkerTab](https://github.com/lorcan35/TinkerTab)** — ESP32-P4 firmware for the M5Stack Tab5

## License

MIT
