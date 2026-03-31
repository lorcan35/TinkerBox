# TinkerClaw Telegram bot

This adds a **separate** Telegram bot service for Dragon deployments.
It does not replace `tinkerclaw.service`, `tinkerclaw-dashboard.service`, or `tinkerclaw-voice.service`.

## Design
- Polling bot using the Telegram Bot API directly via `aiohttp`
- OpenRouter-backed replies
- Per-chat JSON memory stored under `/home/radxa/tinkerclaw/telegram/`
- systemd unit: `tinkerclaw-telegram.service`
- secrets file on device: `/home/radxa/tinkerclaw/telegram.env`

## Required environment
```bash
TELEGRAM_BOT_TOKEN=...
OPENROUTER_API_KEY=...
TINKERCLAW_OPENROUTER_MODEL=google/gemma-3-4b-it
```

## Deploy on Dragon
```bash
mkdir -p /home/radxa/tinkerclaw/telegram
install -m 600 telegram.env /home/radxa/tinkerclaw/telegram.env
sudo install -m 644 systemd/tinkerclaw-telegram.service /etc/systemd/system/tinkerclaw-telegram.service
sudo systemctl daemon-reload
sudo systemctl enable --now tinkerclaw-telegram.service
```

## Basic verification
```bash
systemctl status tinkerclaw-telegram --no-pager
journalctl -u tinkerclaw-telegram -n 50 --no-pager
```

## Commands
- `/start`
- `/help`
- `/model`
- `/clear`
- `/status`
