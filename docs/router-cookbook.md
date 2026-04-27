# Multi-Model Router Cookbook

Copy-paste fleets for common routing patterns. Drop into `dragon_voice/config.yaml` under `llm:` and set `backend: "router"`.

All `model_id` strings verified live against OpenRouter on 2026-04-27. Pricing shown is in $/M tokens (input/output).

---

## 1. Default Recommended Fleet

The all-purpose fleet shipped as the canonical example in `config.yaml`. Cheap defaults for both text and multimodal, premium options on standby for hard turns.

```yaml
llm:
  backend: "router"
  fleet:
    # Local tier
    - {id: ministral,   backend: ollama, model_id: "ministral-3:3b",
       caps: [text, tool_calling],          tier: local, priority: 0,  keep_alive_s: 600}
    - {id: minicpm_v4,  backend: ollama,
       model_id: "hf.co/openbmb/MiniCPM-V-4-gguf:Q4_K_M",
       caps: [text, vision, video],         tier: local, priority: 10, keep_alive_s: 120}
    # Cloud tier
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

**Behavior:**
- Local text → `ministral` (always resident, 10-min keepalive)
- Local vision → `minicpm_v4` (loads on first photo, evicts after 2-min idle)
- Cloud text → `ds_v4_flash` (cheapest)
- Cloud vision → `qwen36_flash`
- Cloud video → `gemini_flash` (only ≤ priority 18 cloud entry with VIDEO)

---

## 2. "Cheapskate" — Sub-$0.50/M everything

Maximum cost optimization. Sacrifices quality for spend.

```yaml
llm:
  backend: "router"
  fleet:
    - {id: ds_v4_flash,  backend: openrouter, model_id: "deepseek/deepseek-v4-flash",
       caps: [text, tool_calling],          tier: cloud, priority: 0}
    - {id: qwen35_flash, backend: openrouter, model_id: "qwen/qwen3.5-flash-02-23",
       caps: [text, vision, tool_calling],  tier: cloud, priority: 5}      # $0.07/$0.26 (!)
    - {id: gemma_4_26b,  backend: openrouter, model_id: "google/gemma-4-26b-a4b-it",
       caps: [text, vision, tool_calling],  tier: cloud, priority: 8}      # $0.06/$0.33
    - {id: glm_4_7,      backend: openrouter, model_id: "z-ai/glm-4.7-flash",
       caps: [text, tool_calling],          tier: cloud, priority: 3}      # $0.06/$0.40
```

Best for: high-volume background tasks, agentic chains where one bad reply isn't catastrophic.

---

## 3. "Top Tier Only" — Frontier on every turn

For when latency + cost don't matter and you want the best answer every time.

```yaml
llm:
  backend: "router"
  fleet:
    - {id: opus_47,    backend: openrouter, model_id: "anthropic/claude-opus-4.7",
       caps: [text, vision, tool_calling],  tier: cloud, priority: 0}
    - {id: gpt_55,     backend: openrouter, model_id: "openai/gpt-5.5",
       caps: [text, vision, tool_calling],  tier: cloud, priority: 5}
    - {id: gemini_pro, backend: openrouter, model_id: "google/gemini-3.1-pro-preview",
       caps: [text, vision, video, audio_in, tool_calling],
                                            tier: cloud, priority: 10}
```

Default ($5–30/M output) is fine — but watch the daily cap. Bump Tab5 NVS `cap_mils` to 2000000 ($20/day) before flipping this on or auto-downgrade will kick in fast.

---

## 4. "Local with Cloud Vision Fallback"

Hybrid pattern that lets Local mode borrow a cloud vision model when the local one isn't loaded or fails. Currently NOT supported by the router (tier filter is hard) — file as a follow-up issue if you want it. Workaround: stay in Local mode for text, manually flip to Cloud when you tap the camera.

---

## 5. "DGX Workstation Acceleration"

Run heavy multimodal models on a workstation/DGX via LM Studio's OpenAI-compatible API; Dragon stays text-fast.

```yaml
llm:
  backend: "router"
  fleet:
    # Local tier (Dragon Q6A)
    - {id: ministral, backend: ollama, model_id: "ministral-3:3b",
       caps: [text, tool_calling],     tier: local, priority: 0}
    # LAN tier (DGX/workstation running LM Studio on port 1234)
    - {id: dgx_minicpm_o, backend: lmstudio,
       model_id: "openbmb/minicpm-o-4.5",
       caps: [text, vision, video, audio_in, audio_out, tool_calling],
       tier: lan, priority: 5,
       lmstudio_url: "http://workstation.local:1234/v1"}
    # Cloud tier (fallback)
    - {id: ds_v4_flash, backend: openrouter, model_id: "deepseek/deepseek-v4-flash",
       caps: [text, tool_calling],     tier: cloud, priority: 0}
```

Vision turns in Cloud mode route to `dgx_minicpm_o` (priority 5 < cloud entries) — fast inference on DGX hardware, zero per-token cost, no internet round-trip.

---

## Tuning notes

- **`keep_alive_s`** is passed to ollama as the `keep_alive` parameter. Set high (600+ s) for the always-resident text model and low (60-120 s) for the heavy multimodal model so RAM isn't wasted between bursts.
- **`priority`** is just an integer — lower wins. Use gaps (0, 5, 10, 15, ...) so you can insert new models without renumbering.
- **`tier`** must be one of `local`, `cloud`, `lan`. Custom tier strings are a follow-up if needed.
- **Removing the router** is just `backend: "ollama"` (or whatever single backend). Empty `fleet: []` is fine in that mode — router code never runs.
- **Cost cap is enforced device-side** via Tab5 NVS `cap_mils`. Default $1/day. The router doesn't itself track spend; it asks `price_for_model()` after each turn and sums into the daily counter. Auto-downgrade fires when exceeded → `voice_mode=0` (Local).

## Adding a new OpenRouter model

1. Add to `_OPENROUTER_CAPS` in `dragon_voice/llm/openrouter_llm.py` with the right `frozenset({Modality.TEXT, ...})`.
2. Add the same entry to `_PRICING_MILS_PER_M` (the registry-completeness test catches drift).
3. Add a fleet entry in your `config.yaml` under `llm.fleet`.
4. Restart `tinkerclaw-voice`. Router picks up the new entry on next session_start.

## Troubleshooting

- **Router is configured but the wrong model fires:** check `infer_required_caps(messages)` (see `tests/test_router_routing.py`) — TOOL_CALLING is intentionally NOT inferred from system-prompt content; it's a bonus, not a gate.
- **`router: no fleet model satisfies …`:** the modality combo isn't covered in the active tier. Either expand the fleet or switch tier (voice_mode).
- **Model ID gets a 400 from OpenRouter:** OR may have brokered to a route that doesn't support the modality your registry claimed (see LEARNINGS #93 about haiku-3.5 via Bedrock). Conservatively declare text-only; flip back when the route situation clarifies.
- **Fleet summary in `session_start.config` is missing:** the router isn't active. Confirm `backend: "router"` in `config.yaml`.
