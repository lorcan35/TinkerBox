# TinkerBox — Dragon Server Stack

## Overview
TinkerBox is the Dragon-side (ARM64) server stack for the TinkerClaw AI device. It runs on a Radxa Zero 3W ("Dragon") and provides:
- CDP browser streaming to Tab5 (port 3501)
- Voice pipeline: STT → LLM → TTS (port 3502)
- Web dashboard for device management (port 3500)
- mDNS service discovery

Companion repo: [TinkerTab](https://github.com/lorcan35/TinkerTab) (ESP32-P4 Tab5 firmware)

## MANDATORY: Check LEARNINGS.md First
Before writing any fix, CHECK LEARNINGS.md first. Your bug might already be documented. Every bug found, every fix, every gotcha MUST be added to LEARNINGS.md with Date/Symptom/Root Cause/Fix/Prevention.

## Workflow
1. **Issue first** — Create a GitHub issue before starting work (`gh issue create`)
2. **Branch** — Create a feature/fix branch from main
3. **Commit with issue ref** — Every commit must reference an issue (`refs #N` or `closes #N`)
4. **Push and merge** — Push to origin, merge to main

## Dragon Access
- **Host:** 192.168.1.89 (static IP on LAN)
- **User:** radxa
- **Password:** radxa
- **SSH:** `sshpass -p 'radxa' ssh radxa@192.168.1.89`
- **OS:** Debian (Radxa Zero 3W, ARM64)

## Service Map
| Service | Port | SystemD Unit | Description |
|---------|------|-------------|-------------|
| Dashboard | 3500 | tinkerclaw-dashboard | Web UI for device management |
| Dragon CDP | 3501 | tinkerclaw | CDP browser streaming + touch relay |
| Voice | 3502 | tinkerclaw-voice | STT/LLM/TTS voice pipeline |
| mDNS | — | tinkerclaw-mdns | Advertises _tinkerclaw._tcp |
| Chromium | 9222 | (launched by tinkerclaw) | CDP target browser |
| Ollama | 11434 | ollama | Local LLM inference (CPU, slow) |
| NPU Genie | — | (via voice pipeline) | Llama 3.2 1B on QCS6490 HTP (~8 tok/s) |

## Deploy
```bash
# Sync code to Dragon
sshpass -p 'radxa' scp -r dragon_voice/ radxa@192.168.1.89:/home/radxa/
sshpass -p 'radxa' scp dashboard.py radxa@192.168.1.89:/home/radxa/

# Restart services
sshpass -p 'radxa' ssh radxa@192.168.1.89 "echo 'radxa' | sudo -S systemctl restart tinkerclaw-voice"
```

## Key Technical Notes
- **NPU inference (preferred):** Llama 3.2 1B on Genie/HTP achieves ~8 tok/s. Use `npu_genie` backend. See `docs/npu-setup.md`.
- **ARM64 CPU fallback:** Ollama gemma3:4b is ~0.24 tok/s — 30x slower than NPU. Use only when NPU unavailable.
- **Python packages:** Use `pip install --break-system-packages` on Dragon (PEP 668)
- **User is radxa, NOT rock:** All service files, paths, and caches must use /home/radxa/
- **PYTHONPATH:** dragon_voice runs as `python3 -m dragon_voice` with PYTHONPATH=/home/radxa
- **Audio rates:** Piper TTS outputs 22050Hz, resampled to 16kHz before sending to Tab5. Tab5 upsamples 16k→48k.
- **moonshine-voice API:** v0.0.51+ changed download API. Check cache before downloading.

## File Structure
```
dragon_server.py      — CDP streaming + touch WebSocket
dashboard.py          — Web dashboard (aggregates 3501+3502)
dragon_voice/         — Voice pipeline package
  server.py           — aiohttp WebSocket voice server
  pipeline.py         — STT→LLM→TTS orchestration
  config.py           — Config dataclasses
  config.yaml         — Default configuration
  stt/                — STT backends (moonshine, whisper, vosk)
  tts/                — TTS backends (piper, kokoro, edge)
  llm/                — LLM backends (ollama, openrouter, lmstudio, npu_genie)
LEARNINGS.md          — Institutional knowledge (MANDATORY)
```
