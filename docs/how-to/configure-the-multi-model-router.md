---
audience: operator
type: how-to
prerequisites: [Swap the LLM backend](swap-the-llm-backend.md), [Multi-Model Router Cookbook](../router-cookbook.md)
last-verified: 2026-05-29
est-time: 20 min
---
# Configure the multi-model router

Use this when you want the Dragon to pick a different LLM per turn instead of
running every turn on one fixed backend — a cheap cloud model for plain text, a
vision-capable model when a photo arrives, a video-capable model for a clip,
and a premium model on standby for the hard turns. You opt in by setting
`llm.backend: "router"` and listing the models in `llm.fleet`. Assumes you can
SSH to the Dragon and have already decided which [backend](../../GLOSSARY.md#core--llm-serving--models)
keys you want in the fleet (see [Swap the LLM backend](swap-the-llm-backend.md)).

The router is the [`CapabilityAwareRouter`](../../GLOSSARY.md#multi-model-router)
in `dragon_voice/llm/router.py`. It implements `LLMBackend` so the
ConversationEngine treats it like any single model, but internally it holds the
whole fleet and chooses one per turn from two inputs:

- **the modalities the message needs** — text, vision, video, or audio-in,
  inferred from the message content by `infer_required_caps()`.
- **the [tier](../../GLOSSARY.md#core--llm-serving--models) allowed by the
  active voice mode** — Local mode picks only `local` models, Cloud mode picks
  `cloud` and `lan` models.

Among the models that satisfy both, the lowest `priority` integer wins.

The router is fully opt-in. With any single backend and `fleet: []` the router
code never runs and behavior is identical to a fixed backend — so there is no
risk in editing the fleet on a Dragon that is currently on `lmstudio` or
`ollama`.

## How the pick works

The choice is a one-line filter-then-minimize. Pseudocode from
`dragon_voice/llm/router.py`:

```python
def choose(required_caps, voice_mode):
    tier_filter = TIER_FOR_MODE[voice_mode]
    if tier_filter is None:
        return None  # tinkerclaw mode (voice_mode 3) — router not used
    candidates = [m for m in fleet
                  if required_caps <= m.capabilities   # model covers every needed modality
                  and m.tier in tier_filter]            # model is in an allowed tier
    return min(candidates, key=lambda m: m.priority) if candidates else None
```

`TIER_FOR_MODE` maps each voice mode to the tiers the router may pick from:

| `voice_mode` | Mode | Tiers the router considers |
|--------------|------|----------------------------|
| 0 | Local | `{"local"}` |
| 1 | Hybrid | `{"local"}` (LLM stays local; only STT/TTS go cloud) |
| 2 | Full Cloud | `{"cloud", "lan"}` |
| 3 | TinkerClaw | `None` — router bypassed, the gateway picks |

The required capabilities are inferred from the message content, never from the
system prompt. `image_url` content adds `VISION`, `video_url` adds `VIDEO`,
`input_audio` adds `AUDIO_IN`. `TOOL_CALLING` is deliberately **not** a routing
gate — gating on it would stop a vision turn from picking a vision model that
lacks tool training (MiniCPM-V). Tool detection happens at runtime when the
model emits a tool marker.

## Tiers, capabilities, and priority

A fleet entry is a `ModelSpec`. Three fields drive routing:

- **`tier`** — one of `local`, `cloud`, `lan`. `local` runs on the Dragon
  (Ollama or llama-server); `cloud` is OpenRouter; `lan` is an
  OpenAI-compatible server elsewhere on the network (for example LM Studio on a
  workstation). The active voice mode decides which tiers are eligible.
- **`caps`** — the modalities the model can serve. A model is a candidate only
  if its caps are a superset of what the turn needs. Valid values: `text`,
  `vision`, `video`, `audio_in`, `audio_out`, `tool_calling`.
- **`priority`** — a plain integer; **lowest wins**. Leave gaps (0, 5, 10, 15,
  …) so you can slot a new model in without renumbering the rest.

Two more fields tune resource use rather than routing:

- **`keep_alive_s`** — passed straight to Ollama as its `keep_alive`. Set it
  high (600+ s) for the always-resident text model so it never has to reload,
  and low (60–120 s) for a heavy multimodal model so it evicts from RAM
  promptly between bursts.
- **`model_id`** — the provider-specific model string (Ollama tag, OpenRouter
  slug, or the loaded GGUF for an LM Studio server).

How each backend declares its own capabilities:

| Backend | Capability source |
|---------|-------------------|
| `ollama` | substring scan of the model id (vision on `llava`, `minicpm-v`, `minicpm-o`, `moondream`, `qwen2-vl`, `pixtral`, `internvl`; audio on `minicpm-o`; tools on known FC families) |
| `openrouter` | static registry `_OPENROUTER_CAPS` in `openrouter_llm.py` (35 models tracked, last synced 2026-04-27); unknown models default to `{TEXT, TOOL_CALLING}` |
| `lmstudio` | same name-substring heuristic as Ollama |
| `npu_genie` | text-only (QAIRT has no vision) |
| `tinkerclaw` | text + tool-calling, plus vision when the gateway model is `anthropic/`, `openai/gpt-4o`, `google/gemini`, or `minimax/` |

The `caps` you write in the fleet entry are what the router uses — keep them
honest with what the model actually supports, or you will route a vision turn
to a model that returns a 400.

## Steps

### 1. SSH to the Dragon

The LAN rotates between the `192.168.1.x` and `192.168.70.x` subnets, so verify
the IP first.

```bash
ping radxa-dragon-q6a
ssh radxa@192.168.70.242   # or whatever the current LAN IP resolves to
```

### 2. Set `backend: "router"` and add a fleet

Edit the `llm` block in `/home/radxa/dragon_voice/config.yaml`. This is the
canonical example fleet — cheap defaults for both text and multimodal, premium
models on standby. Model ids are verified live against OpenRouter (2026-04-27);
the trailing comments are dollars-per-million input/output tokens.

```yaml
llm:
  backend: "router"
  fleet:
    # ── Local tier ──
    - {id: ministral,    backend: ollama,     model_id: "ministral-3:3b",
       caps: [text, tool_calling],          tier: local, priority: 0,  keep_alive_s: 600}
    - {id: minicpm_v4,   backend: ollama,
       model_id: "hf.co/openbmb/MiniCPM-V-4-gguf:Q4_K_M",
       caps: [text, vision, video],         tier: local, priority: 10, keep_alive_s: 120}
    # ── Cloud tier ──
    - {id: ds_v4_flash,  backend: openrouter, model_id: "deepseek/deepseek-v4-flash",
       caps: [text, tool_calling],          tier: cloud, priority: 0}      # $0.14/$0.28
    - {id: qwen36_flash, backend: openrouter, model_id: "qwen/qwen3.6-flash",
       caps: [text, vision, tool_calling],  tier: cloud, priority: 5}      # $0.25/$1.50
    - {id: gemini_flash, backend: openrouter, model_id: "google/gemini-3-flash-preview",
       caps: [text, vision, video, audio_in, tool_calling],
                                            tier: cloud, priority: 8}      # $0.50/$3
    - {id: sonnet_46,    backend: openrouter, model_id: "anthropic/claude-sonnet-4.6",
       caps: [text, vision, tool_calling],  tier: cloud, priority: 12}     # $3/$15
    - {id: gemini_pro,   backend: openrouter, model_id: "google/gemini-3.1-pro-preview",
       caps: [text, vision, video, audio_in, tool_calling],
                                            tier: cloud, priority: 18}     # $2/$12
    - {id: opus_47,      backend: openrouter, model_id: "anthropic/claude-opus-4.7",
       caps: [text, vision, tool_calling],  tier: cloud, priority: 25}     # $5/$25
    - {id: gpt_55,       backend: openrouter, model_id: "openai/gpt-5.5",
       caps: [text, vision, tool_calling],  tier: cloud, priority: 30}     # $5/$30
```

This fleet routes like so:

- **Local mode, text turn** → `ministral` (priority 0, always resident).
- **Local mode, vision turn** → `minicpm_v4` (the only local-tier model with
  `vision`; loads on the first photo, evicts after 2 min idle).
- **Cloud mode, text turn** → `ds_v4_flash` (cheapest text + tools).
- **Cloud mode, vision turn** → `qwen36_flash` (cheapest multimodal, 1M context).
- **Cloud mode, video turn** → `gemini_flash` (the lowest-priority cloud entry
  carrying `video`).

The OpenRouter API key is **not** in `config.yaml`. It lives in
`/home/radxa/.env` (loaded by the systemd `EnvironmentFile=`) and survives
`scp` deploys. Keep `llm.openrouter_api_key` empty in the file.

For more fleet patterns — sub-$0.50/M "cheapskate", frontier-on-every-turn, and
a `lan`-tier DGX/workstation acceleration fleet — see the
[Multi-Model Router Cookbook](../router-cookbook.md).

### 3. Restart the voice service

The router reads the fleet at process start and picks it up on the next
`session_start`.

```bash
# clear stale bytecode first if you also scp'd new code
find /home/radxa/dragon_voice -name '__pycache__' -exec rm -rf {} +
sudo systemctl restart tinkerclaw-voice
```

## Verify it worked

When the router is active, both `session_start.config` and the `config_update`
ACK carry a `fleet_summary` — a per-modality map of which model would serve each
modality. Tab5 uses it to light up its vision/video/audio capability chips. If
`fleet_summary` is present, the router is live.

```json
{
  "type": "session_start",
  "config": {
    "fleet_summary": {
      "text":     "ministral-3:3b",
      "vision":   "hf.co/openbmb/MiniCPM-V-4-gguf:Q4_K_M",
      "video":    "hf.co/openbmb/MiniCPM-V-4-gguf:Q4_K_M",
      "audio_in": null,
      "audio_out": null,
      "tool_calling": "ministral-3:3b"
    }
  }
}
```

The fast confirmation is to read back the live config (secrets redacted) and
check the backend flipped:

```bash
curl -s http://192.168.70.242:3502/api/config | python3 -m json.tool
# → "llm": { "backend": "router", "fleet": [ ... ], ... }
```

Then connect a client (or open the dashboard Chat tab on port 3500) and confirm
the per-modality model names in `fleet_summary` match the fleet you wrote. Watch
which model actually fires:

```bash
journalctl -u tinkerclaw-voice -f
# send a text turn → expect the local/cloud text model
# send a photo (vision turn) → expect the vision model
```

## Troubleshooting

- **`fleet_summary` is missing from `session_start.config`** → the router is not
  active. Confirm `backend: "router"` is actually set in `config.yaml` and that
  the service restarted cleanly.

- **`router: no fleet model satisfies …`** → no model in the active tier covers
  the modality combination the turn needs (for example a vision turn in Local
  mode with no `vision` model in the `local` tier). Either add a model that
  covers it, or switch voice mode so a tier that does is eligible.

- **The router is active but the wrong model fires** → check
  `infer_required_caps(messages)`. `TOOL_CALLING` is intentionally not inferred,
  so it never gates the pick — it is a bonus, not a requirement. The pick is
  driven only by `VISION`/`VIDEO`/`AUDIO_IN` present in the message content plus
  the tier filter. See `tests/test_router_routing.py`.

- **An OpenRouter model returns a 400** → OpenRouter may have brokered the
  request to a route that does not support the modality your `caps` claimed.
  Conservatively declare that entry text-only in the fleet and revisit once the
  route situation clarifies.

- **A new OpenRouter model is rejected or has no price** → add it to
  `_OPENROUTER_CAPS` in `dragon_voice/llm/openrouter_llm.py` with the right
  `frozenset` of modalities, add the matching row to `_PRICING_MILS_PER_M` (the
  registry-completeness test catches drift), then add the fleet entry and
  restart.

- **A heavy multimodal model hogs RAM between turns** → lower its `keep_alive_s`
  (60–120 s) so Ollama evicts it promptly; keep the always-on text model high
  (600+ s) so it never reloads. The Dragon's effective fleet ceiling is ~8 GB
  after the OS and services.

- **Cloud turns auto-revert to Local unexpectedly** → cost cap. Spend is
  enforced device-side via the Tab5 NVS `cap_mils` value (default $1/day); the
  router asks `price_for_model()` after each turn and sums into the daily
  counter, and auto-downgrade flips `voice_mode` to 0 when the cap is exceeded.
  Raise `cap_mils` before running a frontier-only fleet.

- **You want to turn the router off** → set `backend` back to a single backend
  (`lmstudio`, `ollama`, …) and `local_backend` to match. An empty `fleet: []`
  is fine in that mode — the router code never runs. See
  [Swap the LLM backend](swap-the-llm-backend.md).

## Related

- [Swap the LLM backend](swap-the-llm-backend.md) — choosing a single backend, or
  pointing Local mode at the router from the backend side.
- [Multi-Model Router Cookbook](../router-cookbook.md) — copy-paste fleets and
  tuning notes.
- [`docs/protocol.md`](../protocol.md) — the `session_start` / `config_update`
  wire format that carries `fleet_summary`.
- [`docs/ARCHITECTURE.md`](../ARCHITECTURE.md) — where the router sits in the
  voice pipeline and how it relates to the four voice modes.
