---
audience: operator
type: tutorial
prerequisites: A Dragon Q6A (or any Linux box) with Python 3.12+, network access, and an OpenRouter API key for cloud backends
last-verified: 2026-05-29
est-time: 20 min
---
# Get your Dragon running

By the end you will have the Dragon voice server live on port **3502**, answering
a health check over the network. This is the brain of the stack — once it
responds, a [Tab5](../../GLOSSARY.md) can connect to it and hold a voice
conversation. Every step below has a visible result so you know it worked before
you move on.

This is a learning-by-doing walkthrough. You run the commands *on the Dragon
itself* (SSH in first, or sit at its keyboard). If you only want to push code
changes from a workstation to an already-running Dragon, that is the
[deploy how-to](../../CLAUDE.md#deploy) instead — this page is about the first
cold start.

## Before you start

- **A Dragon Q6A** (Radxa, Qualcomm QCS6490, ARM64, Ubuntu) — or any Linux box
  with **Python 3.12+**. The Dragon is recommended because the NPU path only
  exists there, but the server runs anywhere Python does.
- **Network access** from the machine you will test from (a workstation on the
  same LAN, or the Dragon's own shell).
- **An OpenRouter API key** (`sk-or-v1-...`) if you want Hybrid or Cloud voice
  modes. Pure Local mode (Moonshine STT + a local LLM + Piper TTS) needs no key,
  but most first runs use Cloud for a quick, high-quality reply.
- **SQLite 3.35+** (ships with Python 3.12+) and **git**.

If your Dragon is across the room, SSH in first:

```bash
ssh radxa@192.168.70.242   # the LAN IP rotates — see note below
```

> The Dragon's IP flips between the `192.168.1.x` and `192.168.70.x` LANs over
> time. If `192.168.70.242` does not answer, find it with
> `ping radxa-dragon-q6a` or `nmap -p 22,3502 --open 192.168.70.0/24`. The
> user is `radxa`, not `rock`.

## Steps

1. **Clone the repo.** SSH'd into the Dragon (or on your Linux box), clone
   TinkerBox and enter it.

   ```bash
   git clone https://github.com/lorcan35/TinkerBox.git
   cd TinkerBox
   ```

   _You should see:_ a `TinkerBox/` directory containing `dragon_voice/`,
   `dashboard.py`, `schema.sql`, and `setup.sh`. Confirm with `ls`.

2. **Run setup.** This installs the base Python packages and checks for Chromium.

   ```bash
   ./setup.sh
   ```

   _You should see:_ pip installing dependencies, then a line confirming setup
   finished. On the Dragon (Debian/Radxa OS), pip needs `--break-system-packages`
   because of PEP 668; `setup.sh` handles that for you.

3. **Install the voice-pipeline dependencies.** These are the packages the
   STT → LLM → TTS pipeline imports at startup.

   ```bash
   pip3 install --break-system-packages -r dragon_voice/requirements.txt
   ```

   _You should see:_ pip resolving and installing the voice pipeline
   requirements with no `ERROR` lines. If you plan to run **Local** mode, also
   install the backends you want — Moonshine STT and Piper TTS are the local
   defaults:

   ```bash
   pip3 install --break-system-packages sherpa-onnx    # Moonshine STT
   pip3 install --break-system-packages piper-tts       # Piper TTS
   ```

4. **Set your OpenRouter API key.** Export it into the shell that will launch the
   server. The server reads `OPENROUTER_API_KEY` when
   `llm.openrouter_api_key` is empty in the config.

   ```bash
   export OPENROUTER_API_KEY="sk-or-v1-..."
   ```

   _You should see:_ nothing printed — that is correct. Verify it stuck with
   `echo "${OPENROUTER_API_KEY:0:10}..."`, which prints the first 10 characters.

   > For a permanent install, the key lives in `/home/radxa/.env` and is loaded
   > by the systemd unit's `EnvironmentFile=`. That file survives code deploys —
   > never put a real key in `config.yaml` in the repo.

5. **Start the voice server.** Launch the `dragon_voice` package as a module.

   ```bash
   python3 -m dragon_voice
   ```

   _You should see:_ startup logs ending with the server binding to
   `0.0.0.0:3502`. The startup sequence brings up the database, sessions, memory
   and tools, then the conversation engine and REST routes. Leave this terminal
   running — closing it stops the server.

   > If you get `OSError: [Errno 98] Address already in use`, port 3502 is taken.
   > Find the owner with `lsof -i :3502` and `kill <PID>`, or start on another
   > port with `python3 -m dragon_voice --port 3503`.

6. **Confirm health on 3502.** From a second terminal (on the Dragon, or from a
   workstation on the same LAN), hit the health endpoint.

   ```bash
   curl -s http://localhost:3502/health | python3 -m json.tool
   # → {"status": "ok", ...}
   ```

   From a workstation, swap `localhost` for the Dragon's IP:

   ```bash
   curl -s http://192.168.70.242:3502/health | python3 -m json.tool
   # → {"status": "ok", ...}
   ```

   _You should see:_ a JSON object with `"status": "ok"`. That is the Dragon
   telling you the voice server is alive and reachable. You can also open
   `http://192.168.70.242:3502/` in a browser for the HTML status page with
   backend info and uptime.

7. **Check the configured backends.** Confirm which STT/LLM/TTS backends are
   active. Secrets are redacted in this response.

   ```bash
   curl -s http://localhost:3502/api/config | python3 -m json.tool
   ```

   _You should see:_ the resolved config showing `stt.backend` (e.g.
   `moonshine`), `llm.backend` (e.g. `openrouter` or `ollama`), and `tts.backend`
   (e.g. `piper`). This is the live configuration the next voice turn will use.

## What you built

You have a running Dragon voice server: `python3 -m dragon_voice` is bound to
port 3502, `GET /health` returns `{"status": "ok"}`, and `GET /api/config` shows
your active backends. The Dragon is now ready to accept a WebSocket connection
from a Tab5 (or any client) at `ws://<dragon-ip>:3502/ws/voice`.

That health line is your proof of success:

```bash
$ curl -s http://192.168.70.242:3502/health | python3 -m json.tool
{
    "status": "ok"
}
```

This was a foreground run — it dies when you close the terminal. To make the
Dragon start on boot and restart on failure, install it as the
`tinkerclaw-voice` systemd unit (and its siblings for the dashboard, mDNS, and
ngrok). The full service map — Dashboard on 3500, CDP on 3501, Voice on 3502 —
and the systemd install commands live in the runbook.

## Next

- [Run it as a service and deploy code changes](../../CLAUDE.md#deploy) — install
  the `tinkerclaw-voice` systemd unit, then `scp` + `systemctl restart` for every
  later update.
- [Switch voice modes (Local / Hybrid / Cloud / TinkerClaw)](../../CLAUDE.md#three-tier-voice-mode)
  — how Dragon hot-swaps STT/LLM/TTS backends from a single `config_update`.
- [The WebSocket protocol](../protocol.md) — the contract a Tab5 (or your own
  client) uses to talk to the server you just started.
- [How the stack fits together](../ARCHITECTURE.md) — where the Dragon sits
  relative to the Tab5 and the optional TinkerClaw gateway.
- [Set up the NPU LLM path](../npu-setup.md) — get Llama 3.2 1B running on the
  Qualcomm Hexagon DSP at ~8 tok/s for faster Local mode.
