# Capability-first Local Mode — Design

**Date:** 2026-05-27
**Repo:** TinkerBox (Dragon-side; Tab5 is a thin client and is not touched)
**Status:** Design — pending implementation plan

## Problem

Local mode (`voice_mode=0`, Dragon-hosted LLM via llama-server) is the
privacy-and-free tier — the product's reason to exist over Cloud. Today it is
both slow and unreliable:

- **Latency:** ~67 s/turn on the current default (LFM2.5-VL-1.6B Q8_0). Not
  conversational.
- **Capability:** the same model scores **7/20 on the hard tool-use gauntlet**
  (2026-05-17, documented in TinkerBox `CLAUDE.md` → "LFM2.5-VL-1.6B HARD
  gauntlet"). The documented failure modes are red-herring tool firing, verb
  confusion, few-shot contamination, and arg-extraction brittleness.

The accuracy leader we already benched — Qwen3.5-4B — scores **18/20
(effectively 20/20)** but at **~96 s/turn**, and was parked as "too slow for
real-time voice."

### User intent (captured during brainstorming, 2026-05-27)

The user wants the **best balance point**, explicitly weighted so that
**capability is the gate and latency is secondary** ("I'd tolerate slowness if
it were actually reliable at tools/instructions"). The capability bar is **all
four** of:

1. Right tool + right args, first try (single-shot).
2. Multi-step agentic chains (call → read result → next call → finish).
3. Instruction-following / not rambling / not hallucinating tool calls.
4. Knowing when **not** to call a tool (no spurious fire on chit-chat / Q&A).

### Research finding (2026-05-26)

A deep survey of GitHub / HuggingFace / r/LocalLLaMA for sub-4B models released
in the prior ~2 weeks (mid-May 2026) found **nothing that beats the
incumbents**. The May drops (Qwen3.7 Max, DeepSeek-V4, Gemini 3.5 Flash) are
large or API-only. The strongest sub-4B candidates remain April releases
(LFM2.5-1.2B-Instruct, Gemma 4 E2B, Granite 4.1 3B) plus the standing
Qwen3-4B-Instruct-2507 and Qwen3.5-4B. **Conclusion: the lever is not "grab the
hot new model" — it is fixing how we use the models we already have.**

## Constraints

- **Dragon-only Local-first.** The LLM runs on the Dragon Q6A (ARM64, 12 GB
  RAM, ~12 TOPS NPU not in the llama.cpp path). Never introduce a
  workstation-LAN inference dependency as a Local path. (User directive →
  memory `feedback_dragon_only_local.md`.)
- **Sub-4B parameters**, served as **GGUF via llama-server** (the
  LM-Studio-compatible OpenAI API on `localhost:1234`, backend `lmstudio`).
- **No Tab5 firmware changes.** Tab5 sends `{"type":"text",...}` / mic PCM over
  the voice WS and renders the reply; all model/tool logic is Dragon-side.

## Current-state findings (code-grounded, 2026-05-27)

1. **Tools are prose-listed, not native.** `LMStudioBackend.generate_stream*`
   (`dragon_voice/llm/lmstudio_llm.py`) sends only
   `{model, messages, stream, max_tokens, temperature}`. Tool definitions are
   injected as a prose `[TOOLS]` block in the system prompt
   (`dragon_voice/tools/formatter.py::_format_compact` / `_format_full`), and
   tool calls are recovered by a **5-dialect text parser**
   (`dragon_voice/tools/parser.py`). There is **no** `tools=[...]`, no
   `tool_choice`, no schema/grammar constraint. This is the single biggest
   undone capability lever and it is already listed in `CLAUDE.md` as a
   "mitigation to try before giving up" — never built.

2. **Config drift.** Committed `dragon_voice/config.yaml` still declares
   `backend: "ollama"` + `ollama_model: "ministral-3:3b"`, but the documented
   production decision (CLAUDE.md → "Local-first Inference on Dragon") is
   `backend: "lmstudio"` (llama-server) + LFM2.5-VL-1.6B. The committed default
   does not match reality. (NOTE: verify the live Dragon `config.yaml` before
   assuming either — the on-device file may differ from the repo.)

3. **Dual-model is a dead end here.** The xLAM-picker + ministral-responder
   pipeline (`dragon_voice/llm/dual.py`, `docs/PLAN-dual-model-pipeline.md`)
   was post-mortemed: combined resident set ~8.7/11 GB, Ollama LRU eviction
   ping-pong → 240–300 s timeouts after 2–3 turns. Reusable only on ≥16 GB
   hardware. Not in scope.

4. **The gauntlets are not committed.** The easy-10 / hard-20 scenario sets
   exist only as hand-run tables pasted into `CLAUDE.md` / `CHANGELOG.md`.
   There is no repeatable, scored harness in the tree, so there is no
   regression net against capability drift.

## Design

Three components, sequenced so every lever's contribution is **measured, not
guessed**.

### Component 1 — Committed capability gauntlet (the foundation)

A repeatable, scored harness in TinkerBox `tests/` (local-only; it needs a live
llama-server, so it is **not** added to the CI named-set).

- **Scenario corpus:** the seven axes already in use — happy-path,
  disambiguation, complex-args, red-herring, out-of-scope, multi-step,
  chitchat. Each scenario is a dataclass: `utterance`, `expected` (one of:
  `{"tool": name, "args_contains": {...}}` or `{"no_tool": true}`), and an
  `axis` tag. Seed it from the documented hard-20 set so today's results are
  directly comparable.
- **Execution path:** runs through **Dragon's real `ConversationEngine` tool
  path** (system prompt build → tool formatting → LLM call → tool-call
  extraction), against the configured local backend — i.e. it measures the
  actual product behavior, not a direct llama-server probe. A flag allows a
  direct-probe mode for fast iteration during development.
- **Scoring:** per-axis pass/fail + overall, with a `args_contains` partial
  match for arg extraction. Logs **per-turn latency** alongside each result.
- **Output:** a markdown report (mirroring the existing CLAUDE.md tables) +
  a machine-readable JSON, written under a gitignored runs dir.
- **Why E2E, not direct probe:** the failure modes we care about (few-shot
  contamination, prose-parser mismatch) are introduced by Dragon's own prompt
  assembly. A direct llama-server probe would hide exactly the bugs we are
  hunting.

### Component 2 — Tool-path hardening (the structural fix)

Add llama-server's **native OpenAI `tools=[...]` API** to the local path.
llama-server with `--jinja` already emits OpenAI-format `tool_calls` in the
response, so the wire support exists; we are not currently using it.

- **`LMStudioBackend`:** add a generation path that includes `tools=[...]`
  (built from each registered tool's `parameters_schema`) and
  `tool_choice="auto"`, and surfaces native `tool_calls` from the streamed
  response (the `delta.tool_calls` shape) instead of relying on prose markers.
- **`ConversationEngine`:** when native `tool_calls` are present, consume them
  directly (name + JSON args already structured). **Keep the existing 5-dialect
  text parser as a fallback** for backends/models that do not support native
  tool-calling, so nothing regresses for the prose path.
- **Sampling + prompt hygiene for tool turns:** `temperature=0` (was 0.1);
  drop the few-shot exemplars that caused contamination ("buy bread" leaking
  into the Venmo scenario); rely on `tool_choice="auto"` for the chit-chat /
  out-of-scope case instead of the synthetic `none()` escape-hatch tool.
- **Feature flag:** a config key (e.g. `llm.native_tools: true`) gates the new
  path so the prose path remains the default until the bake-off proves the
  native path is better. Default off on merge; flipped on by the bake-off
  result.

This single change targets every documented hard-gauntlet failure class at
once: arg-extraction brittleness (model fills a JSON schema rather than a prose
template), malformed/positional args (schema-constrained), red-herring
over-firing and chit-chat (native `tool_choice="auto"`), and few-shot
contamination (exemplars removed).

### Component 3 — Model bake-off behind the harness

With the harness (1) and the hardened path (2) in place, run the candidates and
pick the **capability knee** (latency recorded but not gating):

- **LFM2.5-VL-1.6B** — incumbent; does the native tools API lift it past 7/20?
- **Qwen3-4B-Instruct-2507 @ Q4_K_M** (thinking off) — research-recommended
  accuracy anchor, lighter quant than the Q8 we benched.
- **Qwen3.5-4B @ Q4_K_M** — the parked 18/20 leader, now viable since latency
  is not the gate.
- **ministral-3:3b** — old default, baseline.
- *(optional)* **LFM2.5-1.2B-Instruct** — fastest candidate, for the latency
  data point.

Make the winner the **committed `config.yaml` default**, reconciling the repo
with the documented production reality (finding #2).

### Sequence

1. Build the harness (Component 1).
2. Run it once to capture **today's baseline** (current default, prose path).
3. Land tool-path hardening (Component 2); re-run to **prove the lift** on the
   same model.
4. Run the bake-off (Component 3); commit the winning default.

## Explicitly out of scope

- **Dual-model pipeline** — known RAM dead-end on Q6A (finding #3).
- **NPU offload** — the ~12 TOPS NPU is not in the llama.cpp path; wait for
  upstream (Dragon-only constraint). Not this project.
- **Chasing brand-new models** — research confirmed nothing in the last ~2
  weeks beats the incumbents (research finding above).
- **Tab5 firmware changes** — none required.
- **Speculative decoding (MTP)** — interesting (merged into llama.cpp
  2026-05-16) and latency-relevant, but latency is secondary here; track as a
  follow-up, not part of this capability-first project.

## Validation (2026-05-27) — Component 2 proven on live hardware

Before writing the production code, an A/B probe ran against the live
llama-server (LFM2.5-VL-1.6B Q8, the current Local default, `--jinja` already
enabled) over a 10-scenario hard-gauntlet subset spanning all four capability
axes:

| Path | Score | Avg latency |
|------|-------|-------------|
| Prose-listed tools (current) | **7/10** | 13.7 s |
| Native `tools=[...]` API (proposed) | **9/10** | 27.7 s |

The native path fixed exactly the documented prose-path failures: happy-path
tool selection ("what's on my plate today?" → `calendar_today` not
`tasks_list`) and complex-arg extraction ("email mom…" → clean
`gmail_send(to, subject, body)` instead of positional placeholders). The one
native miss is a 1.6B red-herring over-fire ("these tasks are killing me" →
`tasks_list`), unmoved by a tool-discipline system-prompt line — a model
ceiling that the Component 3 bake-off (Qwen3.5-4B scored 3/3 on red-herrings)
addresses.

The implementation then landed behind `llm.native_tools` (default off) and was
re-validated **through the real deployed code path** (`LMStudioBackend
.generate_with_tools` + `ToolRegistry.openai_tools` + `_process_text_stream_
native`): **9/10**, matching the raw probe. Latency ~2× (acceptable —
capability is the gate). Committed: `feat(llm): native tool-calling path`.

## Risks & open questions

- **llama-server native tool-calling fidelity per model.** `--jinja` tool
  parsing depends on each GGUF carrying a correct chat template with a tool
  section. Some community quants ship broken templates (the Gemma-4-E2B
  mradermacher quant already burned us). The harness will catch this per-model;
  the prose-path fallback is the safety net.
- **E2E latency of the harness.** 20 scenarios × up to ~3 min E2E per turn on a
  4B model ≈ an hour per model. Acceptable for an occasional bake-off; the
  direct-probe mode is the fast inner loop during development.
- **Multi-step axis is the weakest at sub-4B.** Even Qwen3.5-4B was only
  spot-checked on multi-step ("picks first reasonable step"). The harness should
  score multi-step honestly; if no sub-4B model clears it, that is a finding to
  report, not a failure to hide.
- **Config-drift verification.** Confirm the live Dragon `config.yaml` before
  changing the committed default, so we reconcile to the true production state
  rather than overwriting a hand-tuned on-device file.

## Success criteria

- A committed, repeatable gauntlet that scores all four capability axes + logs
  latency, runnable against a live Dragon.
- A measured before/after showing the native-tools path's contribution on a
  fixed model.
- A data-driven Local-mode default committed to `config.yaml`, matching the
  documented production reality, that beats today's 7/20 on the hard gauntlet
  while keeping latency within the "tolerable, capability-first" envelope.
