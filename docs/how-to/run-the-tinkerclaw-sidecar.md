---
audience: operator
type: how-to
prerequisites: [Architecture overview](../ARCHITECTURE.md), [Swap the LLM backend](swap-the-llm-backend.md)
last-verified: 2026-05-29
est-time: 20 min
---
# Run the TinkerClaw sidecar (vmode 3)

Use this when you want a voice turn handled by the agentic [TinkerClaw](../../GLOSSARY.md) gateway instead of by the Dragon's own conversation engine — for long-running autonomous work like code review or browser automation. Assumes you can SSH to the Dragon and have already read the [backend options](../../README.md#backend-options).

In voice mode 3, the Dragon stops being the brain and becomes an audio pipe. STT still runs locally to turn speech into a transcript, that transcript is forwarded to the TinkerClaw gateway on `localhost:18789`, and the gateway's reply is spoken back through local TTS. The `ConversationEngine`, `ToolRegistry`, and `MemoryService` are all bypassed — TinkerClaw runs its own LLM choice, tool execution, and browser automation. The LLM-backend adapter that forwards the call is [`dragon_voice/llm/tinkerclaw_llm.py`](../../dragon_voice/llm/tinkerclaw_llm.py).

## Steps

### 1. Confirm the gateway is running and listening on 18789

The gateway runs as its own systemd unit, `tinkerclaw-gateway.service`, bound to localhost only. SSH to the Dragon first — the LAN rotates between the `192.168.1.x` and `192.168.70.x` subnets, so verify the IP:

```bash
ping radxa-dragon-q6a
ssh radxa@192.168.70.242   # or whatever the current LAN IP resolves to
```

Then check the unit and the port:

```bash
sudo systemctl status tinkerclaw-gateway
ss -ltnp | grep 18789
# → LISTEN 0 ... 127.0.0.1:18789 ...
```

If the unit is dead, start it and watch the logs:

```bash
sudo systemctl start tinkerclaw-gateway
journalctl -u tinkerclaw-gateway -f
```

### 2. Match the gateway auth token in `config.yaml`

The voice server authenticates to the gateway with a shared token. The `tinkerclaw_token` value in [`dragon_voice/config.yaml`](../../dragon_voice/config.yaml) must match the gateway auth token in `~/.tinkerclaw/tinkerclaw.json`. This is one of the items that an `scp -r dragon_voice/` deploy can clobber, so re-check it after every code push.

```bash
# The gateway's configured token
grep -i token /home/radxa/.tinkerclaw/tinkerclaw.json

# The voice server's copy — these two must agree
grep -i tinkerclaw_token /home/radxa/dragon_voice/config.yaml
```

If they differ, edit `config.yaml` so its `tinkerclaw_token` matches `~/.tinkerclaw/tinkerclaw.json`, then restart the voice service:

```bash
sudo systemctl restart tinkerclaw-voice
```

### 3. Enter voice mode 3 from the Tab5

Mode selection is device-driven, not operator-driven. The Tab5 sends a `config_update` frame to switch the whole pipeline to TinkerClaw:

```json
{"type": "config_update", "voice_mode": 3}
```

On receiving `voice_mode: 3`, the Dragon keeps STT (Moonshine, or OpenRouter if the device is in a cloud STT/TTS configuration) and TTS (Piper, or OpenRouter) local, but routes the LLM call to the gateway. The multi-model router is bypassed entirely in this mode — `TIER_FOR_MODE[3]` is `None`, so the gateway picks the model, not the Dragon.

To exercise the path without a Tab5 in hand, drive the WebSocket directly. Send a `register` frame first, then `config_update`, then a `text` frame — text input in mode 3 bypasses the `ConversationEngine` and routes straight through `tinkerclaw_llm.py`:

```bash
ssh radxa@192.168.70.242
```

```python
# On the Dragon: a minimal mode-3 probe over /ws/voice
import asyncio, json, aiohttp

async def main():
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect("ws://localhost:3502/ws/voice") as ws:
            await ws.send_json({"type": "register", "device_id": "probe",
                                "hardware_id": "00:00:00:00:00:01",
                                "firmware_ver": "0.0.0", "platform": "probe"})
            await ws.receive_json()                                  # session_start
            await ws.send_json({"type": "config_update", "voice_mode": 3})
            await ws.send_json({"type": "text", "content": "review the open PRs"})
            async for msg in ws:
                print(msg.data)

asyncio.run(main())
```

## Verify it worked

Watch the voice-server log while you send a mode-3 turn:

```bash
journalctl -u tinkerclaw-voice -f
```

A successful turn shows the LLM call going to the gateway (not to `lmstudio`/`ollama`/`openrouter`) and streams `llm` tokens back. Over the WebSocket you should see the standard turn shape — `llm` tokens, then `llm_done`, then `tts_start` / binary audio / `tts_end` — with no `tool_call` events originating from the Dragon's own `ToolRegistry` (the gateway runs its tools internally). Rich-media rendering (code blocks, tables) still works in mode 3: media detection runs in the TinkerClaw code path too, emitting `text_update` before any `media` frame.

You can also confirm session continuity. The Dragon passes its `session_id` as the `user` field on every TinkerClaw request, so the gateway maintains per-session context across turns. A second turn in the same session reaches the gateway with the same `user` value.

## Troubleshooting

- **Turn fails immediately, Tab5 drops back to Local mode** → the gateway is down. When `tinkerclaw_llm.py` gets connection-refused on `localhost:18789`, the Dragon sends an `error` frame to the Tab5 — the same auto-revert-to-Local pattern as a cloud STT/TTS failure. Start the unit (`sudo systemctl start tinkerclaw-gateway`) and re-send `config_update` with `voice_mode: 3`.
- **Gateway is up but every turn is rejected with an auth error** → `tinkerclaw_token` in `config.yaml` no longer matches `~/.tinkerclaw/tinkerclaw.json`. This is the classic post-deploy regression because `scp -r dragon_voice/` overwrites `config.yaml`. Re-sync the token (step 2) and restart `tinkerclaw-voice`.
- **Mode 3 turns hang for a long time before responding** → expected. The TinkerClaw pipeline timeout is 180 s (3 min), set per voice mode in `pipeline.py`, because agentic tool-execution chains leave long gaps with no token output. The Tab5's own response timeout is 35 s, but keepalive `ping` frames during `PROCESSING` keep the connection alive across the gap.
- **`config_update` to mode 3 seems ignored after a reconnect** → on reconnect the pipeline re-initializes with Local defaults (voice_mode 0) regardless of the prior session's mode. The client must re-send `config_update` to restore mode 3.
- **Channel messages from Telegram/WhatsApp do not arrive** → that is a different path. Inbound third-party messages flow through `GatewayConnector` (`dragon_voice/channels/gateway.py`) over the same `localhost:18789` gateway, not through `tinkerclaw_llm.py`. See [`docs/protocol.md`](../protocol.md) §20 for the `channel_message` / `channel_reply` / `channel_reply_ack` frames.

## Related

- [Swap the LLM backend](swap-the-llm-backend.md) — flip `llm.backend` to `tinkerclaw` persistently or at runtime, and the rest of the backend keys.
- [Architecture overview](../ARCHITECTURE.md) — where the gateway sits among the three pieces and the four voice modes.
- [WebSocket protocol](../protocol.md) — the full `config_update` and channel-frame contracts.
