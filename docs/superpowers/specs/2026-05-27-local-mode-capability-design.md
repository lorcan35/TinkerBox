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
pick the **capability knee** (latency recorded but not gating).

**Selection constraint (from the 2026-05-27 deep model survey):** because
Component 2 uses the native OpenAI `tools=[...]` API, the candidate must emit
tool calls that **round-trip cleanly through llama-server's OpenAI-compatible
parser**. This is a hard gate equal to GGUF availability. Function-calling
*specialists* with custom formats (Hammer 2.1, xLAM-2) fail it badly on this
runtime class (independent eval: Hammer 0/8 on actual calls, xLAM ~15%) even
though their weights are strong — they would require their bespoke parsers (the
existing 5-dialect text parser path), not the native API. So the native-path
bake-off favors **native-OpenAI-schema generalists**:

- **NVIDIA Nemotron-3-Nano-4B @ Q4_K_M** — *new top challenger.* Mar 2026,
  official NVIDIA GGUF, Mamba-2/Transformer hybrid (linear attention → fast on
  the CPU-bound Dragon), tool-use as a primary RL target, native OpenAI tools +
  `finish_reason:"tool_calls"`, reasoning toggleable off for latency. Scored
  95% on an independent OpenAI-compatible tool-calling eval. `<think>` tokens
  (12/13) must be stripped.
- **Qwen3.5-4B @ Q4_K_M** — the parked 18/20 accuracy leader, BFCL-v4 0.503,
  now viable since latency is not the gate. Use a GGUF post-dating the universal
  chat-template tool-calling fix.
- **Qwen3-4B-Instruct-2507 @ Q4_K_M** (thinking off) — accuracy anchor,
  lighter quant than the Q8 we benched.
- **IBM Granite 4.0 Micro 3B / Nano-1B (plain transformer)** — cleanest native
  OpenAI-schema tool-calling, BFCLv3 + IFEval-proven; the safe well-behaved
  baseline and the tiny option.
- **MiniCPM5-1B** — *speed wildcard.* Brand new (~late May 2026), official clean
  GGUF, claims 1B-class agentic-tool SOTA. Emits XML tool calls → needs a
  `--jinja` template mapping to OpenAI JSON; validate that round-trip before
  trusting it on the native path.
- **LFM2.5-VL-1.6B** — incumbent baseline (already measured: prose 7/10 →
  native 9/10).
- *(optional)* **LFM2.5-1.2B-Instruct** / **ministral-3:3b** — additional
  latency/baseline data points.

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

## Bake-off results (2026-05-27) — Component 3 run on live hardware

All candidates pulled as Q4_K_M GGUF and run through the native OpenAI
`tools=[...]` path on Dragon (llama-server, `--jinja`, reasoning disabled,
`temperature=0`) over the same 10-scenario hard-gauntlet subset. Production
llama-server was stopped during the run so each candidate had the full CPU.

| Model | Params | Score | Avg latency/turn |
|-------|--------|-------|------------------|
| **IBM Granite 4.0 Nano-1B** | **1B** | **10/10** | **13.0 s** |
| Qwen3.5-4B | 4B | 10/10 | 118.3 s |
| IBM Granite 4.0 Micro-3B | 3B | 9/10 | 31.4 s |
| NVIDIA Nemotron-3-Nano-4B | 4B | 9/10 | 117.1 s |
| LFM2.5-VL-1.6B (incumbent) | 1.6B | 9/10 | ~28 s* |
| MiniCPM5-1B | 1B | 4/10 | 15.0 s |

\* LFM-VL measured earlier through the deployed `generate_with_tools` path.

### Findings

- **Winner: IBM Granite 4.0 Nano-1B — perfect 10/10 at 13 s/turn.** A 1 B / ~1 GB
  model matched the 4 B Qwen's perfect score (both red-herrings, OOS, chit-chat,
  and the complex-arg `gmail_send` with apostrophe handling) at **~9× lower
  latency than Qwen and ~2× faster than the current LFM-VL incumbent — while
  also beating LFM-VL on accuracy (10 vs 9).** This breaks the project's
  founding assumption that sub-4B capability requires high latency; the
  native-tools path plus a tool-tuned 1 B model gets both.
- **The 4 B models are ~2 min/turn on the Dragon CPU** regardless of
  architecture — Nemotron's Mamba-2 hybrid did not help (llama.cpp's ARM Mamba
  kernels aren't optimized, and reasoning isn't fully suppressible via the
  template kwargs). 4 B is impractical for voice even under a capability-first
  priority.
- **MiniCPM5-1B scored 4/10 — emitted zero native `tool_calls`.** Its XML tool
  format does not round-trip through llama-server's OpenAI-compatible parser,
  exactly as the survey warned. It would need a custom parser, not the native
  path; not worth it given Granite Nano wins outright.
- The native-tools selection constraint held: every model that emits native
  OpenAI tool calls (Granite, Qwen, Nemotron, LFM) worked on the path; the one
  that uses a custom format (MiniCPM5) failed.

### Recommendation

Make **IBM Granite 4.0 Nano-1B (Q4_K_M) the new Local default** with
`llm.native_tools: true`. It is the capability *and* latency winner, ~1 GB
resident (leaving ample headroom on the 12 GB Dragon), and uses native
OpenAI-schema tool calling that the deployed Component 2 path already handles.
Keep LFM2.5-VL-1.6B available as the vision-capable fallback (Granite is
text-only). Next step: swap the `tinkerclaw-llama-server` unit's `--model` to
the Granite Nano GGUF, set `llm.native_tools: true` in the live config, restart,
and confirm a real WS voice turn end-to-end.

## Further model survey (2026-05-27, round 2) — challengers + vision question

A second deep survey (≤2B native-tool challengers + sub-4B vision+tools VLMs):

**Tool challengers to Granite Nano-1B (only native-OpenAI/Hermes round-trip models qualify):**
- **Qwen3.5-2B** (NEW ~Mar 2026) — Hermes `<tool_call>` JSON, thinking off by
  default, **201 languages**, 256K ctx. The "equal tools + far better
  multilingual" play. GGUF `unsloth/Qwen3.5-2B-GGUF` Q4_K_M (1.28 GB). Caveat:
  use a post-template-fix GGUF (a universal Qwen3.5 tool-template bug is fixed).
- **Qwen3-1.7B** — Hermes JSON, BFCL-v3 56.6 (non-thinking), the "faster than
  13 s" play. Pin non-thinking + temp 0. Official GGUF.
- **Qwen3.5-0.8B** — sub-1B speed gamble; accuracy may crack at arg extraction.
- *Not ready (custom/non-OpenAI tool format → won't round-trip):* LFM2-1.2B-Tool
  / LFM2.5-1.2B (Pythonic `<|tool_call_start|>`), SmolLM2-1.7B (plain-text tags,
  BFCL ~27), EXAONE-4.0-1.2B (works only via `--chat-template-file`), Kanana,
  Falcon3-1B (unverified), StableLM2, Zamba2, Danube3, R1-Distill-1.5B.

**Vision + tools sub-4B (can one model replace text-tool + vision-fallback?):**
**Conclusion — keep them separate.** No sub-4B VLM is good enough at tool-calling
to replace a dedicated text tool model. LFM2.5-VL tools are weak (450M variant
BFCL-v4 ~21, text-only). Most VLMs can't even run vision on mainline llama.cpp
(MiniCPM-V, Granite-vision, Ovis2, DeepSeek-VL2, PaliGemma2, Phi-3.5-vision all
unsupported/crash). The only sub-4B VLM with both halves live on stock llama.cpp
is **Qwen3-VL-2B**, but its 2B tool-calling won't beat Granite Nano and VL chat
templates are historically fragile for the OpenAI round-trip. **Plan: Granite
Nano-1B stays the tool/voice brain; keep a VLM as the camera fallback (trial
Qwen3-VL-2B vs LFM2.5-VL-1.6B only for vision quality, route tools to Granite).**

## Round-3 bake-off (2026-05-27) — all challengers + ruled-out, BOTH modes

Tested every candidate in **native** (OpenAI `tools=[...]`) AND **prose**
(tools in system prompt, parsed by Dragon's deployed 5-dialect parser — the
production fallback path). Best mode per model bolded. (A 4/10 floor = the model
emitted no correct tool call; only the 4 no-tool scenarios pass.)

| Model | Params | Native | Prose |
|-------|--------|--------|-------|
| **IBM Granite 4.0 Nano-1B** | 1B | **10/10 @ 14s** | 4/10 @ 10s |
| Qwen3-1.7B | 1.7B | 8/10 @ 13s | **9/10 @ 7.6s** |
| Qwen3.5-2B | 2B | 8/10 @ 41s | 8/10 @ 24s |
| LFM2-1.2B-Tool | 1.2B | **8/10 @ 29s** | 6/10 @ 19s |
| LFM2.5-1.2B-Instruct | 1.2B | **8/10 @ 30s** | 4/10 @ 15s |
| Qwen3.5-0.8B | 0.8B | 7/10 @ 22s | 5/10 @ 11s |
| SmolLM2-1.7B | 1.7B | 4/10 @ 10s | **7/10 @ 7.6s** |
| EXAONE-4.0-1.2B | 1.2B | 4/10 @ 24s | 4/10 @ 26s |
| DeepSeek-R1-Distill-Qwen-1.5B | 1.5B | 4/10 @ 87s | 4/10 @ 81s |
| Kanana-nano-2.1B | 2.1B | 4/10 @ 24s | 4/10 @ 15s |
| Falcon3-1B-Instruct | 1B | 2/10 @ 20s | 4/10 @ 6.5s |
| StableLM-2-1.6B | 1.6B | 4/10 @ 12s | 4/10 @ 14s |
| H2O-Danube3-1.8B | 1.8B | — (GGUF gated, not obtainable) | — |

### Findings

- **Granite 4.0 Nano-1B is confirmed the winner — 10/10 @ 14s, reproduced.**
  Nothing beat it across two rounds and both modes.
- **Granite's accuracy lives in the native path** (10/10 native vs 4/10 prose).
  Confirms Component 2 (`native_tools`) is *essential* for the winner, not
  optional.
- **Qwen models are the inverse — better via prose.** Qwen3-1.7B: 9/10 prose vs
  8/10 native; SmolLM2: 7/10 prose vs 4/10 native. The prose-path test earned
  its keep here.
- **Qwen3-1.7B (9/10 @ 7.6s prose) is the one real alternative** — one point
  below Granite but ~2× faster. The speed pick if 14s feels long.
- **The ruled-out set stayed ruled out.** LFM2-Tool/LFM2.5 reach 8/10 native but
  slower than Granite; the rest sit at the 4/10 floor (no reliable tool
  emission). Reasoning models (R1-Distill, EXAONE) are both weak and slow.

## Live deployment + real-tools test (2026-05-27)

**Shipped:** `tinkerclaw-llama-server` unit swapped to Granite 4.0 Nano-1B
(text-only, `--mmproj` dropped; LFM-VL unit backed up at `.lfmvl.bak`).
`llm.native_tools: true` added to live `config.yaml`; voice server restarted.
Granite serves on :1234, native tool probe confirmed (`weather → {location:Paris}`).
**Local mode vision is temporarily unavailable** (Granite is text-only) — wiring
a dual-server / router fallback for camera turns is the follow-up.

**Real-tools test** — varied phrasings against Granite with the *actual* 27-tool
Dragon registry (calendar/email/tasks/weather/web/datetime/memory) + native API:

Correct + clean args: "any new emails"→`gmail_unread`, "search inbox for
invoices"→`gmail_search(subject:invoice)`, "email mom happy birthday…"→
`gmail_send(to,subject,body)` (clean), "to-do list"→`tasks_list`, "add buy
groceries"→`tasks_add(title)`, "what time"→`datetime`, "weather in London"→
`weather(London)`, "18% tip on 64"→`calculator(64*0.18)`, "thanks!"→no tool. (~12/19)

**Misroutes exposed by the 27-tool granularity (not seen on the 9-tool bench):**
- Defaults to the READ/LIST variant over the ACTION variant: "schedule a
  dentist…"→`calendar_today` (want `calendar_create`); "cancel my 2pm"→
  `calendar_today` (want `calendar_cancel`); "mark laundry done"→`tasks_list`
  (want `tasks_complete`).
- "did Sarah email me…"→`gmail_unread` (want `gmail_search`).
- "remember I prefer window seats"→`recall` (want `remember`).
- "tell me a joke"→`web_search` (should answer directly — chitchat over-fire).
- First 3 turns timed out = cold-start (first inference ~117 s after load); warm
  turns 9-18 s.

**Conclusion:** Granite native-tools is solid for the common single-shot cases
but the large, granular real registry + the terse production system prompt drop
it below the controlled-bench 10/10. **Tuning needed before this is
production-grade:** (1) local-mode system prompt that nudges tool use + the
read-vs-action and remember-vs-recall distinctions; (2) sharper action-tool
descriptions; (3) consider curating/grouping the tool set for the 1 B model.

## Quant + hardware-speed research (2026-05-27)

Dragon CPU verified: **8-core aarch64, `asimddp` (dotprod), NO `i8mm`, NO `sve`**
(QCS6490, Hexagon v66/v68-class NPU).

**Optimal quant for this core:**
- Ship plain **Q4_0** → llama.cpp **runtime-repacks** to `q4_0_4x4` (dotprod) at
  load (PR #9921; the static `Q4_0_4_4` type was removed in b4282). Gives
  ~1.2–1.5× tok/s over Q4_K_M on this class of core. Verify `DOTPROD=1` +
  `AARCH64_REPACK=1` + `q4_0_4x4` repack lines in the startup log.
- **Q4_K_M** = best quality-per-bit (~5× less perplexity damage than Q4_0).
- **Q8_0** = near-lossless but ~2× the memory bandwidth → ~half the tok/s.
- **i-quants (IQ4_NL)** net neutral-to-slower on a slow dotprod core.

**Q8 quant test (live, optimized recipe):** Granite-1B Q8 = **17/20**,
Qwen3-1.7B Q8 = **15/20** — i.e. **Q8 gave NO tool-accuracy gain** over Q4
(Granite Q4_K_M was 19/20; the gap is run-variance + the turn-1 cold-start
artifact) while running ~2× slower. **Conclusion: Q4_K_M is the right production
quant; Q4_0 is the speed option (~1.3×) if a quick A/B shows the 1B keeps its
tool accuracy. Q8 is not worth the latency.**

**Making 3-4B usable — verdict: not on this CPU.**
- **Speculative decoding**: ~no gain on a compute-bound CPU (llama.cpp CPU
  spec-decode is still a proposal, #21453; voice = open-ended chat = ~50% draft
  acceptance). MTP is GPU-only numbers + Qwen3.6-only.
- **Hexagon NPU**: QCS6490 is below the v73 floor that LLM-on-Genie/HTP
  requires — no LLM-on-NPU path in 2026.
- Best case for a 4B (Q4_0 + tuned threads) ≈ 70–90 s/turn, still above the
  ~30-40 s usability bar. **Sub-2B is the only viable real-time path.**

**Recommended llama-server flags for the live 1-2B tool model:**
`-t 6 -fa on -ctk q8_0 -ctv q8_0 --mlock -c 4096 --cache-reuse 256`
(pin with `taskset -c 0-5`; benchmark -t 6 vs 7 vs 8 — bandwidth-bound, 6 often
wins). KV-quant must be symmetric (ctk==ctv) or it falls back to the slow path.

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
