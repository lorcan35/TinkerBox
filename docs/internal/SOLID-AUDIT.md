# TinkerBox SOLID Audit — refreshed 2026-05-02

## TL;DR

The server has **regressed on SRP** since the #65 decomposition: `server.py` has grown from 1,803 LOC (post-split) to 2,719 LOC as WS-voice handlers (text/config/media/disconnect) accreted. The multi-model router (#183–#188, April 2026) is a clean abstraction win, but it **masks a deeper LSP debt** on the 6 existing backends — they declare capabilities via heuristics + the router now depends on declarations being correct, so adding a backend requires auditing all 6 capability detectors. Tool parsing and execution are still tangled (S17 unresolved). Biggest single fix: extract `WsDispatcher` + per-command handler classes from `server.py` before the handlers regrow.

---

## Status of prior findings (from 2026-04-26 audit)

| Old finding | Status | Notes |
|-------------|--------|-------|
| **S1** VoiceServer god-object post-#65 | REGRESSED | Server.py grew from 1,803 to 2,719 LOC; the #74/#76/#77 WS-voice handler family + #92 agent_log instrumentation + #75 tool-tracking + multimodal media detection all accreted to the same dispatch loop. TurnGate is now at 5/9 SurfaceManager methods + used by scheduler + pipeline + server (S7 extraction still pending). |
| **S2** VoicePipeline owns 5+ responsibilities | STILL-OPEN | 1,721 LOC still. Cancel surface touches 7 distinct state kinds. Pool-identity helpers live here due to no `BackendPool` class. Pools now exist (W15-C01) but scattered across pipeline + server + router (no unified class). |
| **S3** STT/TTS/LLM backends feature-detect via `hasattr` | PARTIALLY-RESOLVED | Router (#183–#188) now declares `capabilities: frozenset[Modality]` per backend. BUT: 9 live `hasattr` calls still in pipeline.py (set_session_key, get_last_usage, trim_history, kill_active_procs, clear_history, total_calls). Backends do NOT enforce a uniform contract — optional features are optional. Router depends on capability declarations being correct, but declarations themselves are heuristic-based (ollama substring scan, openrouter static dict, etc.). Adding a 7th backend requires auditing all 6 capability detectors + the router rules. |
| **S4** Server reaches into ConvEngine._llm private | STILL-OPEN | 6 sites in server.py still directly access `self._conversation._llm` (lines 1384, 1386, 1651, 1652, 2373, 2374). No public `swap_llm()` method — HTTP config path (#65 handlers/config_api.py) still drifts from WS path. |
| **S5** `swap_backends` policy duplicated 4x | PARTIALLY-RESOLVED | Pipeline.swap_backends is the source of truth now (W15-C01). Server.py config_update uses it (line 2322). handlers/config_api.py still has its own swap logic (lines 80–99) that predates the W15-C01 unified path — will drift. |
| **S6** Tool registration open in intent, closed in practice | STILL-OPEN | startup.py has 5 edit sites for a new tool. Priority list hardcoded at tools/registry.py:366 (added in #79). No topology sort; self-registration not implemented. |
| **S7** SchedulerManager ↔ SurfaceManager coupling via TurnGate | STILL-OPEN | TurnGate (mark_turn_start/mark_turn_end + deferred_emits queue) is 50/213 LOC in SurfaceManager, has 4+ callers (scheduler + pipeline + server text path + server cancel handler). Still embedded in SurfaceManager; still not extracted. |
| **S8** _format_compact/_format_full violate OCP | STILL-OPEN | 439 LOC in tools/registry.py, hardcoded priority list + XML/FC format. Pre-router this was "nice to fix"; post-#183 router, the formatter needs to be fed the `ModelSpec.capabilities` so router can tell the LLM what format variants it understands. Currently one-way: tools/registry broadcasts one format regardless of consumer. |
| **S9** LLMConfig god-config (mode-shaped fields per backend) | STILL-OPEN | Single LLMConfig carries ollama_*/openrouter_*/lmstudio_*/tinkerclaw_*/dual_* fields. Router added `fleet: list[ModelSpec]` but didn't deprecate the single-backend fields — both paths coexist, confusing the config shape. |
| **S10** LLMBackend.generate_stream_with_messages default pretends-to-format | PARTIALLY-RESOLVED | Ollama now overrides with full OpenAI->Ollama multimodal translation (#186). But lmstudio still uses the base default-impl which silently degrades `/chat/completions` to single-prompt formatting. |
| **S11** API *Routes classes uniform but duplicate constructor injection | STILL-OPEN | 11 `*Routes` classes, all `__init__(db, session_mgr, message_store, ...)`. No instance state. Plain functions would be cleaner; aligns with "no speculative abstractions" rule. |
| **S12** STT/TTS backends import `inference_executor` from pipeline.py — DIP inversion | STILL-OPEN | 4 sites still do `from dragon_voice.pipeline import inference_executor`. This is a shared thread pool; should live in a top-level `dragon_voice/runtime/executors.py`. Blocks STT/TTS import path from pulling in db + llm registry + memory (15s test startup cost). |
| **S13** Hallucination-cleaning regexes scattered across 5 modules | STILL-OPEN | `_HALLUCINATION_STOPS`, `_TOOL_MARKUP_RE`, `looks_like_useful_text` (twice — server.py:1706 + dual.py:55). dual.py has explicit TODO referencing #79 which has landed but the extraction hasn't. These helpers are now also used by tools/response_wrap.py (#79). |
| **S14** ConversationEngine ctor takes 5 concrete deps, half-DIP | STILL-OPEN | No protocols. Duplication: `is_local = backend in ("ollama", ...)` at lines 267, 285. Router-era would benefit from `LLMBackend.kind: Literal["local","cloud","gateway"]`. |
| **S15** _handle_text_body + _process_utterance duplicate empty-reply guard | PARTIALLY-RESOLVED | #79 (tools/response_wrap.py) extracted `synthesize_wrap()` + `looks_like_useful_text()` as common helpers. But the guard is still re-implemented in server.py:1720–1740 (TC text path), server.py WS-voice path (for dictation), and likely in pipeline.py vision path. Not unified. |
| **S16** Receipt emission coded thrice with subtle drift | STILL-OPEN | Voice path, text path, TC bypass path still hand-build receipts. Field-set drift continues to cause dashboard billing mismatches (per-turn view). No ReceiptEmitter class. |
| **S17** tools/registry.py mixes registry + parser + formatter | STILL-OPEN | 439 LOC. Parser now has error-surfacing (γ2-M1, tools/registry.py:155). But extraction hasn't happened. Tools/response_wrap.py is new (from #79) but doesn't consolidate the helpers. |
| **S18** lifecycle/startup.run_startup mutates ~20 attrs on server | STILL-OPEN | Free function reaches into 20+ attrs by name. Startup-define-time mutations are still silent; rename `_session_mgr` and startup still runs. No `ServerComponents` dataclass. |
| **S19** Database is monolithic (40+ methods, no domain split) | STILL-OPEN | 844 LOC. Every test touching DB imports all of db.py (~15 s including aiosqlite + sqlite-vec). No domain repos (DeviceRepo, SessionRepo, MessageRepo, etc.). |
| **S20** Mode-aware logic ("voice_mode 0/1/2/3") scattered across 6+ sites | STILL-OPEN | Each mode is a tuple of (stt_backend, tts_backend, llm_backend_resolver, system_prompt, max_tokens, timeout_s, fallback_to). Mode logic hardcoded in server.py:2147–2189 (config_update handler) + pipeline.py + router.py. No `VoiceMode` dataclass + `ModeRegistry`. Adding voice_mode 4 = 6 lockstep edits. |

---

## New findings, ranked by severity

### F1 [P0] — Router enables backend capability mismatch (LSP violation chain)
**Principle:** LSP, OCP
**Where:** dragon_voice/llm/router.py:choose() + base.py:capabilities + all 6 backend implementations (ollama_llm.py, openrouter_llm.py, lmstudio_llm.py, npu_genie.py, tinkerclaw_llm.py, dual.py)
**Smell:** Router (#183–#188) declares `ModelSpec.capabilities: frozenset[Modality]` and routes based on subset checks (router.py:choose() at ~line 200). But capability declarations are heuristic-based (ollama: model_id substring scan for `minicpm-v`, `llava`, `moondream`, etc.; openrouter: static `_OPENROUTER_CAPS` registry). When a new backend is added (e.g., a 7th LLM backend), the router rules in TIER_FOR_MODE don't change, BUT all 6 existing backends' capability declarations must be audited. Adding lmstudio, the only way to know its vision+video capability is to scan source code (no registration). If capabilities drift (e.g., a new Ollama model released that's vision-capable but not in the substring list), the router silently gates it. Openrouter caps are manually synced (last sync 2026-04-27 per CLAUDE.md); out-of-date by definition.
**Consequence:** Silent capability gaps. A router-fed multimodal turn might pick a text-only model when a vision-capable one is available. A new backend might be added with incorrect capability declarations, failing on real-world usage. The router's correctness depends on 6 independent heuristics staying synchronized.
**Suggested fix:** (1-PR bundle, 1 day) Create `dragon_voice/llm/capability_registry.py` that centralizes all capability detectors. Each backend registers a `declare_capabilities(model_id, backend_name) -> frozenset[Modality]` function instead of embedding the logic inline. Router calls this at spec-parsing time, not model-load time. Ollamacaps function does the substring scan; openrouter function looks up the static dict; lmstudio function returns the same set for all models (text-only for now). When adding a new backend, CapabilityRegistry must be updated explicitly — a central audit point.

### F2 [P0] — WS-voice handler family regrew; SRP violated
**Principle:** SRP
**Where:** dragon_voice/server.py:1602–2500+ (main dispatch loop + _handle_text + _handle_text_body + _handle_config_update + _handle_user_media + _handle_disconnect + _handle_cancel + media detection + tool tracking + receipt emission)
**Smell:** Post-#65 decomposition, the 2,400-LOC WS handler family was supposed to be extracted but instead accreted. _handle_text_body (lines 1642–1967, 326 LOC) + _handle_config_update (lines 2074–2361, 288 LOC) handle: WS keepalive during inference (TinkerClaw path + voice path + vision path), empty-reply guard synthesis, tool-call tracking per-turn (for wrap fallback), media detection + rendering, receipt emission, LLM swap race conditions (TC bypass uses ConvEngine._llm not pipeline._llm), TTS audio resampling, vision multimodal content, and pipeline drain-on-cancel. The dispatch loop (lines 870–1000) threads through all 5 handler invocations with shared conn_state dict — changes to one path require updating all 5.
**Consequence:** (1) Testing handlers in isolation requires mocking the entire VoiceServer. (2) Adding a new WS command type requires editing the dispatch loop + understanding 5 existing handlers' state-threading patterns. (3) Tool-tracking, empty-reply, receipt emission are re-implemented differently in text path vs voice path vs TC bypass path — bugs in one don't get caught in another.
**Suggested fix:** (3-PR bundle, 2 days) Extract `WsDispatcher` class holding the command routing table. Per-command handler classes (`TextHandler(db, conv_engine, message_store, tool_registry, media_pipeline)`, `ConfigHandler`, `MediaHandler`, etc.). Dispatch becomes `await handler.handle(ws, conn_state, cmd)`. Immediate side effect: handlers become testable in isolation. Longer term: tool-tracking + receipt + media detection become centralized (see S15/S16).

### F3 [P1] — Server directly accesses ConversationEngine._llm; encapsulation broken
**Principle:** Encapsulation, DIP
**Where:** dragon_voice/server.py:1384, 1386, 1651, 1652, 2324–2326, 2373–2405 (6 sites directly access `self._conversation._llm` or `pipeline._llm`)
**Smell:** TinkerClaw text bypass (lines 1651–1740) directly calls `llm.generate_stream_with_messages()` by fetching `self._conversation._llm`. Config swap (lines 2373–2405) directly assigns `self._conversation._llm = new_llm`. Fleet summary display (lines 1384–1386) checks `isinstance(self._conversation._llm, CapabilityAwareRouter)`. These bypass ConversationEngine's public interface — no method to swap LLM or query the current backend. HTTP config swap path (handlers/config_api.py:80–99) has its own logic that doesn't call ConversationEngine. Result: WS config_update updates both paths; HTTP config_update updates only pipeline, leaving ConversationEngine on the old backend.
**Consequence:** WS text turns use a different LLM than REST /api/sessions/{id}/chat turns after a mode swap. Session context stored with one backend's tokens is answered by another backend in a later turn.
**Suggested fix:** (1 PR, <1 day) Add `ConversationEngine.swap_llm(new_config, *, pool) -> None` method. Both WS path and HTTP path call it. Remove all direct `_llm` assignments from server.py.

### F4 [P1] — Tool parsing and execution are tangled; error handling incomplete
**Principle:** SRP, OCP
**Where:** dragon_voice/tools/registry.py:134–380 (parsing + execution + optional error surfacing)
**Smell:** `parse_tool_calls()` (line 134) silently swallows JSON errors for backward compat. `parse_tool_calls_with_errors()` (line 155) surfaces them. Callers must choose which to use. Execution path (registry.py:86) calls `_agent_log_call()` (new in #92 Wave 12) but errors during `tool_args_invalid` are caught and logged locally, not fed to agent_log. Parser supports 3 dialects (legacy, standard, bracketed-name) inline with tolerance heuristics — a parser fix (e.g., improving brace-walk logic) forces re-running formatter tests. `tools/response_wrap.py` (from #79) synthesizes natural-language wraps for empty-reply cases but is only called from server.py text path, not from voice path or TC bypass. The three places that handle empty replies (server.py:1700–1740 TC path, server.py WS-voice path dictation, pipeline.py vision path) each have different wrap logic.
**Consequence:** Tool parse errors are either silent (backward-compat path) or surfaced (error path) depending on which method the caller invokes. Adding a new tool XML dialect requires editing parser + formatter + tolerance heuristics in one file. Empty-reply handling diverges across three paths.
**Suggested fix:** (2-PR bundle, 2 days) (1) Split tools/registry.py into registry.py (dict only) + parser.py (all 3 dialects, error surfacing, brace-walk) + formatter.py (compact + full + strategy for router-aware format). (2) Extract all empty-reply guards into a unified `ReplyFinalizer` class in `dragon_voice/text_utils.py` alongside hallucination-cleaning helpers (S13).

### F5 [P1] — Capabilities declared but not enforced; backends can silently lack features
**Principle:** ISP, LSP
**Where:** dragon_voice/llm/base.py:25–95 (LLMBackend ABC) + all 6 backends
**Smell:** `LLMBackend` ABC declares `generate_stream()` and `async def generate_stream_with_messages()` (default impl). 9 live `hasattr` calls in pipeline.py + server.py check for optional features: `set_session_key`, `get_last_usage`, `trim_history`, `kill_active_procs`, `clear_history`, `total_calls`. New backends can omit these without type-checker catching it. `OpenRouterBackend.get_last_usage()` is called from pipeline.py:1198 unconditionally (after the hasattr check). TinkerClaw usage is checked but Dual backend doesn't declare it. Concrete bug: dual + tinkerclaw users get receipts with `model="llm"` instead of the real backend name because `get_last_usage` is missing (cited in prior audit as S3).
**Consequence:** Backend authors don't know which optional features must be implemented. Callers must sprinkle `hasattr` checks everywhere. Adding a new feature (e.g., `get_context_size()`) requires auditing all backends + all call sites.
**Suggested fix:** (1 PR, 1 day) Create capability Protocol mixins: `SupportsUsage`, `SupportsHistoryTrim`, `SupportsSessionKey`, `SupportsKill`. Make each optional-feature-dependent method check `isinstance(backend, SupportsUsage)` instead of `hasattr(backend, 'get_last_usage')`. Backends that don't implement a mixin get a type-checker error at ctor time. Callsites become unconditional (the backend was already checked at construction).

### F6 [P1] — pipeline.swap_backends duplication; HTTP config path still drifts
**Principle:** DRY, OCP
**Where:** dragon_voice/pipeline.py:1557 (source of truth) vs dragon_voice/handlers/config_api.py:80–99 (pre-#65 HTTP path)
**Smell:** WS config_update handler (server.py:2322) calls `await pipeline.swap_backends(conn_config)` (W15-C01 unified). HTTP config path (handlers/config_api.py:80–99) still has its own swap logic checking `config.llm.backend != old_config.llm.backend` + manually calling `create_stt`, `create_tts`, `create_llm`. If a swap edge case is fixed in pipeline.swap_backends (e.g., a new `_pooled_stt` guard), the HTTP path doesn't benefit. When the API was refactored (#65), the HTTP path was supposed to be aligned; it still uses the old pattern.
**Consequence:** WS and HTTP config_update follow different code paths. A regression in one doesn't appear in the other. New backends or swap scenarios are fixed in one path, leaving the other broken.
**Suggested fix:** (0.5 PR, <1 day) Replace the HTTP config path (handlers/config_api.py:80–99) with a call to `pipeline.swap_backends()` if the pipeline exists, same as WS does. No duplication. Any future swap fix benefits both paths.

### F7 [P1] — Tool capability declarations heuristic; out-of-date static dicts
**Principle:** OCP, maintainability
**Where:** dragon_voice/llm/openrouter_llm.py:_OPENROUTER_CAPS (static dict of 35 models as of 2026-04-27) + all 6 backends' substring/static declarations
**Smell:** Openrouter backend has a manually-maintained `_OPENROUTER_CAPS` dict with 35 models as of April 27, 2026. If OpenRouter ships a new model (e.g., "openai/gpt-5.5" mentioned in CLAUDE.md), the capabilities aren't declared. Router would treat it as {TEXT, TOOL_CALLING} (default). A similar static `_PRICING_MILS_PER_M` registry exists (also 35 models, matched 1:1). Both are pre-computed at load time; out-of-sync with OpenRouter's live API. Ollama capability detection is model_id substring scan — if a new Llama-vision model is released with a name that doesn't match the hardcoded list, the router won't know it supports vision.
**Consequence:** New models ship with wrong capability declarations. Router makes incorrect routing decisions. Users don't know why a model they added isn't being picked for a capability it supports.
**Suggested fix:** (0.5 PR, <1 day) Ollama: query `/api/tags` at startup to auto-detect vision models (check internal `details.models` for capability tags if exposed). OpenRouter: make the static registry a cache that's validated against OpenRouter's live `/models` endpoint on startup. Warn if upstream-present models are missing from the cache. During the wave, assume the manual cache is ground truth; in a follow-up (Wave 16?), wire the live-fetch path.

---

## Summary table
| ID | Sev | Principle | Where | Title |
|----|----|-----------|-------|-------|
| F1 | P0 | LSP, OCP | llm/router.py + base.py + all 6 backends | Router enables backend capability mismatch via heuristic-based declarations |
| F2 | P0 | SRP | server.py:1602–2500 | WS-voice handler family regrew post-#65; needs extraction to WsDispatcher |
| F3 | P1 | Encapsulation, DIP | server.py:1384, 1651, 2324, 2373 | Direct access to ConversationEngine._llm; no public swap method |
| F4 | P1 | SRP, OCP | tools/registry.py:134–380 | Parsing + execution + formatting tangled; error handling inconsistent |
| F5 | P1 | ISP, LSP | llm/base.py + all backends | Optional features not enforced; 9 hasattr() calls scattered across call sites |
| F6 | P1 | DRY, OCP | pipeline.py:1557 vs handlers/config_api.py:80 | HTTP config swap still drifts from unified WS path |
| F7 | P1 | OCP, maintainability | openrouter_llm.py + ollama_llm.py + all backends | Capability declarations heuristic + out-of-date static dicts |
| S1 | P0 | SRP | server.py | (REGRESSED) god-object post-#65, grew from 1.8K to 2.7K LOC |
| S2 | P1 | SRP | pipeline.py:140 | (STILL-OPEN) 5+ responsibilities; cancel touches 7 state kinds |
| S7 | P1 | SRP, ISP | surfaces/manager.py:92–162 | (STILL-OPEN) TurnGate coupled to SurfaceManager, 4+ callers |
| S13 | P2 | SRP, DRY | server.py + dual.py + tools/response_wrap.py | (STILL-OPEN) Hallucination/empty-reply regexes scattered across 5 sites |
| S16 | P2 | SRP, DRY | server.py (3 receipt paths) | (STILL-OPEN) Receipt emission hand-built 3x with field drift |
| S18 | P1 | SRP | lifecycle/startup.py | (STILL-OPEN) run_startup mutates 20+ server attrs; no ServerComponents dataclass |
| S19 | P2 | SRP, ISP | db.py:31 | (STILL-OPEN) Monolithic Database; no domain repos; every test imports all 844 LOC |

---

## Out-of-scope but worth noting

1. **Router vs Dual redundancy (emerging pattern):** `dual.py` (226 LOC, predates router) is now subsumed by router's capability-aware selection. `dual` is still accepted as a backend choice but two backends doing the same job create maintenance debt. Recommendation: in a follow-up wave, deprecate `dual` with a 2-release warning; users can migrate to `router` + 2-model fleet.

2. **Mode logic scattered:** Voice_mode selection (0=local, 1=hybrid, 2=cloud, 3=tinkerclaw) is hardcoded in server.py:2147–2189 + pipeline.py mode-aware timeouts + router.py TIER_FOR_MODE. No `@dataclass VoiceMode` + registry. Each mode is a tuple of (stt_backend, tts_backend, llm_tier, system_prompt, max_tokens, timeout_s). Adding voice_mode 4 requires edits in 3+ files. (S20 from prior audit, still open.)

3. **LLMConfig god-config shape:** Single config carries fields for all backends (ollama_*, openrouter_*, lmstudio_*, tinkerclaw_*, dual_*, router_*). Per-backend dataclass extraction (S9 from prior audit) + router's `fleet` field coexist, confusing the shape. The config schema now has two separate paths: single-backend (legacy) and router fleet (new). Both are valid; users can upgrade gradually, but the grammar is ambiguous.

4. **Formatter needs router context:** tools/registry.py `_format_compact()` + `_format_full()` (lines 383–440) hardcode priority list + example tool calls. Post-#183 router, the LLM's capabilities determine what format dialects it can consume. A minimal-context model might only understand legacy XML; a modern FC-trained model might prefer standard dialect. Formatter should query the LLM's declared capabilities (via ModelSpec) and emit the best-matching format. Currently one-way broadcast.

5. **Test import cost:** Database import pulls in aiosqlite + sqlite-vec + schema migration logic. STT/TTS imports pull in inference_executor from pipeline, which transitively imports llm registry + memory service. Every unit test touching persistence or backends pays ~15 s startup. (S12/S19 from prior audit, still open — blocked on executor move + database domain split.)

6. **API Routes as classes:** 11 `*Routes` classes (sessions.py, messages.py, devices.py, etc.) are constructor-injected bundles of functions. No instance state — should be plain functions + a setup_all_routes helper. (S11 from prior audit, still open.)

7. **Speech output sample-rate tangles:** TTS backends emit different rates (Piper 22050 Hz, OpenRouter 24 kHz). Server.py:1971 unconditionally reads `pipeline._tts.sample_rate` to decide resampling. The resampler lives in server.py (tight coupling); new TTS backends must know to set this attr. Post-#65, this should live in a TTS base interface. (Minor, but a concrete example of LSP drift.)

---

## Recommendations for the next wave

**Immediate (P0, blocks shipping):** F1 (capability registry centralization) + F2 (WS dispatcher extraction).

**Short-term (P1, blocks roadmap features):** F3 (ConversationEngine.swap_llm), F4 (tool parsing split), F5 (capability protocols), F6 (HTTP config dedup), F7 (capability declaration automation).

**Long-term (P2, technical debt):** S1 (full server split, paired with F2), S2 (pipeline split into AudioIngestor/TurnRunner/BackendOrchestrator), S7 (TurnGate extraction), S13/S16 (text_utils consolidation).

**Deprecation path:** dual.py + old `backend: "dual"` config → router-fleet alternative documented; 2-release warning before removal.
