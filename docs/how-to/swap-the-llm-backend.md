---
audience: operator
type: how-to
prerequisites: [Backend Options in the README](../../README.md#backend-options), [Multi-Model Router Cookbook](../router-cookbook.md)
last-verified: 2026-05-29
est-time: 15 min
---
# Swap the LLM backend

Use this when you need to change which language model the Dragon runs a turn
on — flipping the Local path between `lmstudio` (llama-server) and `ollama`,
pointing Local mode at the multi-model `router`, sending Cloud turns to a
different OpenRouter model, or handing turns to the `npu_genie`, `tinkerclaw`,
or `dual` backends. Assumes you can SSH to the Dragon and have already read the
[backend options table](../../README.md#backend-options) so you know which key
names which engine.

The LLM backend is one field: `llm.backend` in
[`dragon_voice/config.yaml`](../../dragon_voice/config.yaml). You change it in
one of two places:

- **At rest, in `config.yaml`** — edit the file on the Dragon and restart
  `tinkerclaw-voice`. This is the persistent, survives-reboot path.
- **At runtime, over the config API** — `POST /api/config` hot-swaps the
  backend on every active pipeline without dropping the WebSocket. The change
  lasts until the next restart, which reloads `config.yaml`.

There is also a third, Tab5-driven path: a `config_update` WebSocket frame
flips the whole voice mode (Local / Hybrid / Cloud / TinkerClaw) for one
connection. That belongs to the device, not the operator — see
[Three-Tier Voice Mode](#how-voice-mode-relates-to-the-backend) below.

## The valid backend keys

| Key | Engine | Where it runs |
|-----|--------|---------------|
| `lmstudio` | llama-server (OpenAI-compatible) on `localhost:1234` | Local — **primary** Local path |
| `ollama` | Ollama on `localhost:11434` | Local — automatic fallback for `lmstudio` |
| `npu_genie` | Llama 3.2 1B on the QCS6490 Hexagon DSP (~8 tok/s) | Local (NPU) |
| `openrouter` | Any cloud model via OpenRouter | Cloud |
| `tinkerclaw` | TinkerClaw agent gateway on `localhost:18789` | Local sidecar (voice mode 3) |
| `dual` | Fast tool-picker + warm responder pair | Local |
| `router` | `CapabilityAwareRouter` picks per-turn from a fleet | Mixed (local / cloud / lan) |

`lmstudio` and `ollama` are the two Local-mode options that matter day to day.
The Dragon ships with `backend: "lmstudio"` and `local_backend: "lmstudio"` —
llama-server is the Local default because it is roughly 10× faster on a direct
tool-call probe than Ollama on the same Q6A hardware and serves the
[Granite](../../GLOSSARY.md) and MiniCPM-V GGUFs that Ollama 500'd on.

## Steps

### Option A — swap persistently in `config.yaml`

1. SSH to the Dragon (verify the current IP first — the LAN rotates between the
   `192.168.1.x` and `192.168.70.x` subnets):

   ```bash
   ping radxa-dragon-q6a
   ssh radxa@192.168.70.242   # or whatever the current LAN IP resolves to
   ```

2. Edit the `llm` block in `/home/radxa/dragon_voice/config.yaml`. To switch the
   Local path from llama-server to plain Ollama, set both `backend` and
   `local_backend`:

   ```yaml
   llm:
     backend: "ollama"          # was "lmstudio"
     local_backend: "ollama"    # was "lmstudio" — used for cloud→local fallback
     ollama_url: "http://localhost:11434"
     ollama_model: "qwen3.5:4b"
   ```

   To go the other way — Ollama back to the llama-server Local default with
   native tool-calling on:

   ```yaml
   llm:
     backend: "lmstudio"
     local_backend: "lmstudio"
     lmstudio_url: "http://localhost:1234/v1"
     lmstudio_model: "default"  # llama-server reports its loaded GGUF
     native_tools: true
   ```

3. Restart the voice service so the new config loads:

   ```bash
   sudo systemctl restart tinkerclaw-voice
   ```

> **Always set `local_backend` too, not just `backend`.** `local_backend` is
> the backend the pipeline reverts to when a Cloud turn fails or when a device
> reconnects (reconnect resets to Local defaults). If you leave it stale, a
> cloud-failure fallback or a reconnect will quietly land on the wrong engine.

### Option B — hot-swap at runtime over the config API

`POST /api/config` re-initializes every active pipeline in place. The
WebSocket stays up; the next turn uses the new backend.

```bash
curl -X POST http://192.168.70.242:3502/api/config \
  -H "Content-Type: application/json" \
  -d '{"llm": {"backend": "ollama", "ollama_model": "qwen3.5:4b"}}'
# → {"status": "ok", "message": "Config updated, 1 pipelines reloaded", ...}
```

The response reports how many pipelines reloaded. This change is **not**
written to `config.yaml` — the next `tinkerclaw-voice` restart reloads the file
and reverts it. Use Option A for anything you want to persist.

### Local path: pick the GGUF llama-server serves

The `lmstudio` backend does not pick the model — it points at whatever GGUF
llama-server already has loaded on `localhost:1234`, which is why
`lmstudio_model: "default"` is fine. The model is chosen by the
`--model` argument in the `tinkerclaw-llama-server.service` systemd unit. To
swap the served model, edit the unit's `--model` path and restart it:

```bash
sudo systemctl restart tinkerclaw-llama-server   # picks up the new --model
sudo systemctl restart tinkerclaw-voice          # re-probe + re-evaluate fallback
```

> **Restarting llama-server mid-session is not picked up automatically.**
> `create_llm()` does a one-shot TCP probe of `lmstudio_url` at process start.
> If llama-server was down at boot the session falls back to Ollama and stays
> there. After you fix or restart llama-server, restart `tinkerclaw-voice` too
> so the probe re-runs.

The winning Local model is IBM Granite 4.0 Nano-1B (10/10 → 19/20 on the tool
gauntlet at ~13 s/turn) served at
`/home/radxa/llama.cpp/models/`. Ministral-3:3b remains the one-line rollback at
`/home/radxa/llama.cpp/models/ministral/model-q4_k_m.gguf`.

### Turn on native tool-calling

`native_tools` controls *how* the Local model is asked to call tools. With it
on, the Dragon passes the OpenAI `tools=[...]` array plus `tool_choice="auto"`
to llama-server (which must run with `--jinja`) and consumes the structured
`tool_calls` response. With it off, the Dragon prose-lists the tools in the
system prompt and parses `<tool>NAME</tool><args>{}</args>` markers from the
model's free text (the five-dialect parser).

```yaml
llm:
  backend: "lmstudio"
  native_tools: true   # structured tools=[...] path; falls back to prose if unsupported
```

Native tool-calling is shipped on (`native_tools: true`) and is essential for
Granite — it scores 19/20 native versus 4/10 on the prose path. It only applies
to the `lmstudio` Local path; other backends ignore it. See
[native tool-calling in the glossary](../../GLOSSARY.md#core--llm-serving--models)
for the concept.

### Point Local mode at the multi-model router

To let Dragon pick a model per turn from a fleet instead of one fixed backend,
set `backend: "router"` and populate `llm.fleet`. The router is opt-in — an
empty `fleet: []` with any single backend behaves exactly as today.

```yaml
llm:
  backend: "router"
  fleet:
    - {id: ministral,  backend: ollama, model_id: "ministral-3:3b",
       caps: [text, tool_calling],         tier: local, priority: 0,  keep_alive_s: 600}
    - {id: ds_v4_flash, backend: openrouter, model_id: "deepseek/deepseek-v4-flash",
       caps: [text, tool_calling],         tier: cloud, priority: 0}
```

Restart `tinkerclaw-voice`; the router picks up the fleet on the next
`session_start`. Copy-paste fleets for common patterns (cheapskate, top-tier,
DGX-accelerated) live in the
[Multi-Model Router Cookbook](../router-cookbook.md).

### Hand turns to the TinkerClaw gateway

`backend: "tinkerclaw"` (voice mode 3) makes Dragon an audio pipe — STT and TTS
still run locally, but the LLM call, tools, and memory all go to the TinkerClaw
gateway on `localhost:18789`. The gateway token must come from the environment,
never `config.yaml`:

```yaml
llm:
  backend: "tinkerclaw"
  tinkerclaw_url: "http://localhost:18789"
  tinkerclaw_token: ""          # injected via TINKERCLAW_TOKEN env (in /home/radxa/.env)
  tinkerclaw_model: "minimax/MiniMax-M2.5"
```

`TinkerClawBackend` raises at construction if the token resolves to blank, so a
misconfigured gateway fails loudly rather than silently no-opping.

## How voice mode relates to the backend

The Tab5 swaps the *voice mode tier* (Local / Hybrid / Cloud / TinkerClaw) per
connection with a `config_update` frame; the operator config above sets the
*default backend* the server boots with. They interact:

| Voice mode | `voice_mode` | LLM source |
|------------|--------------|------------|
| Local | 0 | The configured Local backend (`lmstudio` → `ollama` fallback, or `router` local tier) |
| Hybrid | 1 | Same Local LLM (only STT/TTS go cloud) |
| Full Cloud | 2 | OpenRouter, user-selected model (or `router` cloud/lan tier) |
| TinkerClaw | 3 | The TinkerClaw gateway (router bypassed entirely) |

Two consequences for an operator:

- **Reconnect resets to Local defaults.** When a device reconnects, the
  pipeline re-initializes at `voice_mode 0` regardless of the previous mode.
  The client must re-send `config_update` to restore Cloud. Your `config.yaml`
  Local backend is what it lands on.
- **Per-connection config is deep-copied.** Each WebSocket gets a
  `copy.deepcopy()` of the global config, so one Tab5 switching to Cloud does
  not corrupt another device's pipeline.

The full mode contract lives in
[`docs/protocol.md`](../protocol.md); the four-repo picture is in
[`docs/explanation/how-the-stack-fits-together.md`](../explanation/how-the-stack-fits-together.md).

## Verify it worked

Read back the live config (secrets are redacted in the response):

```bash
curl -s http://192.168.70.242:3502/api/config | python3 -m json.tool
# → "llm": { "backend": "lmstudio", "local_backend": "lmstudio", "native_tools": true, ... }
```

Confirm the backend is actually reachable and serving:

```bash
# llama-server (lmstudio path)
curl -s http://localhost:1234/v1/models
# → {"data": [{"id": "...granite...", ...}]}

# Ollama (ollama path)
curl -s http://localhost:11434/api/tags
# → {"models": [{"name": "qwen3.5:4b", ...}]}
```

Then run one stateless completion through the new backend and watch the logs:

```bash
curl -s -X POST http://192.168.70.242:3502/api/v1/completions \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Say hi in three words."}'

journalctl -u tinkerclaw-voice -f   # confirm the turn ran on the expected backend
```

If you swapped to `router`, the new model lands in `fleet_summary` inside the
`session_start.config` frame — connect a client (or the dashboard Chat tab) and
confirm the per-modality model names match your fleet.

## Troubleshooting

- **`POST /api/config` returns "0 pipelines reloaded"** → no WebSocket is
  currently connected, so there is no active pipeline to swap. The new config
  still applies to the next connection. To make it persistent, edit
  `config.yaml` and restart.

- **Swapped to `lmstudio` but turns still run on Ollama** → llama-server was not
  reachable on `127.0.0.1:1234` at process start, so `create_llm()` fell back to
  Ollama for the whole session. The fallback is one-shot at instance creation,
  not per-request. Fix llama-server, then `sudo systemctl restart
  tinkerclaw-voice` to re-probe.

- **Granite (or any thinking model) emits an empty reply** → the token cap is
  too low; thinking-mode models spend budget inside `<think>...</think>`. Local
  mode uses `MAX_TOKENS_LOCAL = 1024`. Non-thinking models stop naturally well
  under the cap, so this is a no-op cost for them.

- **Local turns crawl to the 300 s timeout** → concurrent Local turns thrash the
  8-core CPU. Local mode's pipeline timeout is 300 s by design (5 min) for slow
  tool-calling chains; the CPU backend must be serialized (one inference at a
  time) so two turns don't compete. Cloud mode's timeout is 60 s; TinkerClaw is
  180 s.

- **Tools don't fire after switching to native** → llama-server must be started
  with `--jinja` for the `tools=[...]` path to work, and `native_tools` only
  affects the `lmstudio` backend. If you are on `ollama`, tool-calling uses the
  prose five-dialect parser instead — that is expected.

- **Ollama is very slow (~0.24 tok/s)** → expected on the QCS6490 ARM64 CPU.
  Ollama runs CPU-only. Use `lmstudio` (llama-server) for the Local path, or
  `npu_genie` (~8 tok/s on the Hexagon DSP — see
  [`docs/npu-setup.md`](../npu-setup.md)), or switch to Cloud.

- **`tinkerclaw` backend raises at startup** → the gateway token is blank.
  `tinkerclaw_token` must stay empty in `config.yaml`; inject the real value via
  the `TINKERCLAW_TOKEN` env var in `/home/radxa/.env` (loaded by the systemd
  `EnvironmentFile=`).

- **`router` is set but the wrong model fires** → `TOOL_CALLING` is intentionally
  not inferred as a routing gate; only `VISION`/`VIDEO`/`AUDIO_IN` from the
  message content drive the pick. If no fleet model covers the required modality
  in the active tier you get `router: no fleet model satisfies …` — expand the
  fleet or change voice mode. See the
  [router cookbook troubleshooting section](../router-cookbook.md#troubleshooting).
