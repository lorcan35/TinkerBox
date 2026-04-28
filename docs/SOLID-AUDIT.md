# SOLID audit — TinkerBox + TinkerTab

**Status:** open · 42 findings · 3 Critical · 9 High · 19 Medium · 11 Low
**Tracking:** [TinkerBox#169](https://github.com/lorcan35/TinkerBox/issues/169)
**Method:** parallel research agents read both codebases (TinkerBox: ~38 Python modules, ~17 KLOC; TinkerTab: ~50 C modules in `main/`, ~25 KLOC) scoring against SRP / OCP / LSP / ISP / DIP with file:line evidence.  Date: 2026-04-26.

This is a **map**, not a plan.  Following CLAUDE.md "one concern per PR" and "extract before decompose," each finding becomes either a single PR or a small bundle.  Sequencing is in [§Sequencing](#sequencing) at the bottom.

---

## How to read this doc

- IDs `S1…S25` are TinkerBox (server, Python).
- IDs `T1…T17` are TinkerTab (firmware, C).
- Severity is about *future-pain delta* — Critical means "this is going to bite hard the next time we touch it"; Low means "would be nice; not bleeding."
- Effort: S = single PR (≤ 1 day), M = bundle of 2-3 (~3-5 days), L = sprint.
- Risk: low/medium/high — risk of regression, weighted by recovery cost (firmware `high` is harder to recover from than server `high`).

---

## Critical findings

### S1: VoiceServer is still a god-object after #65 split  (TinkerBox · SRP · L · medium)
**File:** [`dragon_voice/server.py:62-2536`](../dragon_voice/server.py#L62) — 54 instance attrs, ~25 methods.
**What:** The #65 decomposition extracted middleware/handlers/lifecycle bodies, but the *class* still owns: WS upgrade auth, ngrok keepalive, dashboard reverse-proxy, full WS dispatch (register/text/media/config/cancel), P13 device-eviction, surface registration, scheduler offline-queue replay, per-turn tool-call tracking, hallucination wrapping, TTS resampling, vision multimodal, TC bypass, OpenRouter receipts, hot-swap of `ConversationEngine._llm` private attr, plus 8 thin adapters that just forward to the extracted modules.
**Concrete pain:**
- `_handle_text` + `_handle_text_body` is **454 LOC** ([1513-1967](../dragon_voice/server.py#L1513)).
- `_handle_config_update` is **382 LOC** ([1969-2351](../dragon_voice/server.py#L1969)) and the same logic at [`handlers/config_api.py:34-118`](../dragon_voice/handlers/config_api.py#L34) has **already drifted** (HTTP path doesn't touch `_conversation._llm`, WS path does).
- Two distinct keepalive tasks (`_ws_keepalive` at 580-639 vs `_ws_keepalive_during_inference` at 371-461).
**Refactor:** Extract `WsDispatcher` + per-command handler classes (`TextHandler`, `MediaHandler`, `ConfigHandler`, `RegisterHandler`).  `VoiceServer` becomes ~300 LOC of "wire collaborators + run app."

### S2: VoicePipeline owns 5 rotating responsibilities  (TinkerBox · SRP · L · medium)
**File:** [`dragon_voice/pipeline.py:140-1672`](../dragon_voice/pipeline.py#L140) — 44 instance attrs vs the 7 the docstring implies.
**What:** Audio buffering+VAD, STT/LLM/TTS orchestration, dictation+post-process, backend pool membership+sig computation, cancellation surface, fallback STT/TTS pre-warm+cache, hot-swap, media-pipeline routing.
**Concrete pain:**
- `cancel()` ([494-535](../dragon_voice/pipeline.py#L494)) touches 7 distinct kinds of state; each new "kind of in-flight work" forces parity changes here AND in `swap_backends` AND in `shutdown` AND in `_handle_disconnect`.  The recent B5/B6 audit work explicitly called the cancellation surface "tangled."
- Pool-identity helpers `_stt_sig`/`_tts_sig`/`_llm_sig` ([101-137](../dragon_voice/pipeline.py#L101)) live here only because there's no `BackendPool` class.
**Refactor:** Three siblings — `AudioIngestor`, `TurnRunner`, `BackendOrchestrator`.  `VoicePipeline` becomes a 100-LOC composition root.  Bridge: extract pool ownership to a server-side `BackendPool` class first.

### T1: voice.c is 8 modules masquerading as one  (TinkerTab · SRP · L · high)
**File:** [`main/voice.c`](https://github.com/lorcan35/TinkerTab/blob/main/main/voice.c) — 3,224 LOC, 50+ static globals.
**What:** Owns transport+reconnect+backoff+auth, mic capture task, TTS playback ring + drain task, JSON dispatch for ~30 message types, dictation FSM + auto-stop VAD, three-tier mode plumbing + budget enforcement, widget_store ingestion (5 widget types), chat-store ingestion (media/card/audio/system), link-health probe + zombie-WiFi escalation + controlled reboot, ngrok fallback.
**Concrete pain:**
- `handle_text_message` is one **826-LOC** function dispatching 28 message types (voice.c:700-1526).
- `voice_lan_probe_task` (voice.c:2316) is a 100+ line zombie-WiFi watchdog that calls `esp_restart()` — has nothing to do with voice but lives here because it shares static globals.
- Budget enforcement + auto-downgrade live inside the `receipt` handler (voice.c:1054-1093) — pure policy embedded in JSON parser.
**Refactor:** Carve along ownership lines: `voice_ws.c` (transport), `voice_dispatch.c` (JSON router), `voice_audio.c` (playback), `voice_mic.c` (capture+dictation), `voice_health.c` (probe+escalation), `voice_budget.c` (cap downgrade), `voice.c` residual façade.  6 atomic PRs.  **Risk high** — voice is the device's product; regressions = mic doesn't work.

---

## High findings

### S3: STT/TTS/LLM backends violate LSP — feature-detect with `hasattr`  (TinkerBox · LSP+ISP · M · low)
Backends differ by side-effect.  Callers reach via `hasattr` ≥ 14 sites in pipeline+server.  Adding a new backend silently misses receipts/trim/session-key.
**Fix:** Capability `Protocol` mixins (`SupportsUsage`, `SupportsHistoryTrim`, `SupportsSessionKey`, `SupportsKill`).  `isinstance` instead of `hasattr` → type-checker catches regressions.  Universal hooks (`get_last_usage`) get no-op base defaults so callsites become unconditional.
**Verifiable bug today:** dual + tinkerclaw users get receipts with `model="llm"` instead of the real backend name because `get_last_usage` is missing.

### S4: Server reaches into `ConversationEngine._llm` + `_llm_config` private state  (TinkerBox · encapsulation+DIP · S · low)
[`server.py:1562-1563, 2249, 2260, 2268`](../dragon_voice/server.py#L2249).  The HTTP `/api/config` swap path doesn't do the conversation-engine swap, so HTTP updates pipeline but leaves text/REST chat on the old LLM.
**Fix:** Add `ConversationEngine.swap_llm(new_config, *, pool)`; both swap callsites use it.  Eliminates the drift between WS and HTTP paths.

### S5: `swap_backends` policy is duplicated 4 times  (TinkerBox · OCP+DRY · M · medium)
Same algorithm in `pipeline.swap_backends`, `pipeline.initialize`, `server.py:2243-2272` (ConvEngine LLM swap), and `handlers/config_api.py:80-99` (HTTP path that *misses* the ConvEngine swap).
**Fix:** Extract `class BackendPool` with `acquire(kind, key, factory) -> (backend, was_new)`.  Eliminates `_pooled_*` flags (pool tracks ownership).

### S6: Tool registration is open in *intent*, closed in *practice*  (TinkerBox · OCP · M · low)
[`lifecycle/startup.py:75-204`](../dragon_voice/lifecycle/startup.py#L75) — adding a new tool requires editing 5 places: tool file, ordinal-position registration in startup, CLAUDE.md, the hardcoded `priority_tools` list at [`tools/registry.py:366`](../dragon_voice/tools/registry.py#L366) for small-LLM compact format, and docs/historical/AUDIT-WAVE-14.md gauntlet.  Issue #134 was opened just to add ONE name to the priority list.
**Fix:** Tool self-registration via discovery loop.  Tools declare `requires: list[str]`; `ToolBootstrap` topo-sorts.  Replace hardcoded priority list with `Tool.priority: int = 50` + filter by `priority >= 70`.

### S7: SchedulerManager ↔ SurfaceManager coupling leaking through TurnGate  (TinkerBox · SRP+ISP · S · low)
The TurnGate (5 of 9 SurfaceManager methods, ~80 of 213 LOC) is bolted onto SurfaceManager because both need `session_id` as a key, but it's a cross-cutting concurrency primitive about session-level back-pressure on out-of-band emits — *not* about Tab5 surface ownership.  Already has 4 callers (scheduler + pipeline voice path + server text path + server cancel handler) — more than the rest of SurfaceManager combined.  **Youngest coupling, easiest to clean before it grows.**
**Fix:** Extract `class TurnGate` separately.  SurfaceManager embeds one; SchedulerManager + Pipeline + Server inject it directly.

### T2: handle_text_message dispatch is a 28-arm if/else  (TinkerTab · OCP · M · medium)
[`voice.c:700-1526`](https://github.com/lorcan35/TinkerTab/blob/main/main/voice.c#L700) — five `widget_*` arms have ~217 LOC of near-duplication.
**Fix:** `static const struct {const char *type; ws_msg_handler_t fn;} HANDLERS[]` table.  Factor `widget_from_cjson_common(root, &w)` helper.

### T3: debug_server.c handler dispatch is data-but-not-data  (TinkerTab · OCP · S · low)
43 `httpd_register_uri_handler` calls; identical struct literal each.  `max_uri_handlers = 56` stays in sync by hand.  Handler impls separated from registration by 2,700 lines.
**Fix:** `ENDPOINTS[]` table with `{uri, method, handler, no_auth}` co-located.  Compile-time count.

### T4: 41 handlers each call `check_auth(req)` by hand  (TinkerTab · OCP+SRP · S · low)
56 `check_auth` call sites.  CORS headers duplicated ~20 times, inconsistently.  Pair with T3 — endpoint table carries `no_auth` bool; dispatch wrapper handles auth + CORS centrally.

### T5: voice.c calls back into the UI directly — DIP inverted  (TinkerTab · DIP · M · medium)
[`voice.c:24-30`](https://github.com/lorcan35/TinkerTab/blob/main/main/voice.c#L24) includes `ui_voice/notes/chat/core/widget` headers; ~30 ui_*/chat_*/widget_* call-outs from voice.c.  voice.c (lower-level) knows about `ui_chat_push_message`, `widget_store_upsert`, etc.  Local `extern` declarations dodge circular includes — author knew the dependency was wrong way.
**Why it matters now:** **The desktop SDL2 simulator (CLAUDE.md Phase 2 #1) is blocked on this.**  voice.c can't be exercised in a simulator without the entire LVGL UI tree.
**Fix:** Define `voice_listener_t` interface in voice.h (`on_state`, `on_chat_text`, `on_chat_media`, `on_widget_upsert`, `on_toast`, `on_session_resume`, etc.); wire in `main.c`; voice.c stops including any ui_*/chat_*/widget header.

---

## Medium findings

### S8: `_format_compact` and `_format_full` violate OCP  (TinkerBox · OCP · S · low)
Tool prompt formatting hardcodes priority list + example tool calls in [`tools/registry.py:355-411`](../dragon_voice/tools/registry.py#L355).  Adding native OpenAI FC support means rewriting both methods.
**Fix:** Strategy pattern: `class ToolFormatter(Protocol)`, multiple impls (Legacy XML, Compact XML, OpenAI FC, Anthropic ToolUse).

### S9: `LLMConfig` is a god-config (mode-shaped fields per backend)  (TinkerBox · SRP+ISP · M · medium)
Single `LLMConfig` carries fields for every backend (ollama_*, openrouter_*, lmstudio_*, tinkerclaw_*, dual_*).  `_llm_sig` switches on backend name to know which model field is "the" model.  Adding a backend means widening the config + 4 introspection sites.
**Fix:** Per-backend dataclass (`OllamaSettings`, `OpenRouterSettings`, etc.).  Each backend ctor takes its own settings type only.

### S10: `LLMBackend.generate_stream_with_messages` default-impl pretends-to-format  (TinkerBox · LSP · S · low)
Base flattens messages into "System: …\nUser: …\nAssistant:" text and delegates to `generate_stream`.  Plausible for completion-only backends; *silently degrades* `lmstudio_llm` (which actually has `/chat/completions` but never overrode).
**Fix:** Make `generate_stream_with_messages` abstract too.  Provide opt-in `MessageFormattingMixin` for completion-only backends.

### S11: API `*Routes` classes uniform but duplicate constructor-injection per route  (TinkerBox · ISP · S · low)
11 `*Routes` classes, all `__init__(db, session_mgr, message_store, conversation=None)`.  `setup_all_routes` is 70 LOC of bespoke wiring with optional-feature guards.  No instance state — classes don't earn their keep.
**Fix:** Convert to plain functions: `def register_sessions(app, *, db, session_mgr, message_store, conversation)`.  Aligns with "no speculative abstractions" rule.

### S12: STT/TTS backends import `inference_executor` from `pipeline.py` — DIP inversion  (TinkerBox · DIP · S · low)
4 sites do `from dragon_voice.pipeline import inference_executor` from "lower" layers.  Cold imports slow (transitively pulls db + llm registry + memory + numpy).
**Fix:** Move `inference_executor` to `dragon_voice/runtime/executors.py`.

### S13: Hallucination-cleaning regexes scattered across 5 modules  (TinkerBox · SRP+DRY · M · medium)
`_HALLUCINATION_STOPS`, `_TOOL_MARKUP_RE`, `looks_like_useful_text` (twice — server *and* dual.py), `_COT_PREAMBLE_PATTERNS` all do "clean small-LLM output before user."  dual.py has explicit TODO referencing #79 which has landed but the extraction hasn't.
**Fix:** New `dragon_voice/text_utils.py` owning all of `strip_tool_markup`, `looks_like_useful_text`, `truncate_at_hallucination`, `sanitize_chain_of_thought`.

### S14: `ConversationEngine` ctor takes 5 concrete deps but stores only refs — half-DIP  (TinkerBox · DIP · S · low)
No protocols.  Duplicated `is_local = backend in ("ollama", ...)` at [conversation.py:267, 285](../dragon_voice/conversation.py#L267).
**Fix:** Narrow Protocols (`MessageContextSource`, `SessionTouchable`).  Add `LLMBackend.kind: Literal["local","cloud","gateway"]` so `is_local` becomes `llm.kind == "local"` — compile-time error if a new backend forgets.

### S15: `_handle_text_body` and `_process_utterance` duplicate empty-reply guard  (TinkerBox · SRP+DRY · S · low)
4 implementations of "if reply junk and any tool fired, synthesize wrap; else apology" (TC text + local text + vision + voice).
**Fix:** `class ReplyFinalizer.finalize(reply_text, tool_calls, *, on_event) -> final_text`.

### S16: Receipt emission coded thrice with subtle drift  (TinkerBox · SRP+DRY · S · low)
Voice path, text path, TC bypass path all hand-build receipts; field-set drift between them.  Concrete user-visible: per-turn billing dashboard mismatches.
**Fix:** `class ReceiptEmitter`.

### S17: `tools/registry.py` mixes registry + parser + formatter  (TinkerBox · SRP · S · low)
411 LOC: ~110 registry, ~200 parser (3 dialects with tolerance heuristics), ~70 formatter.  A parser bug-fix forces formatter test re-runs and vice versa.
**Fix:** Split into `tools/registry.py` (dict only), `tools/parser.py`, `tools/formatter.py`.

### S18: `lifecycle/startup.run_startup` mutates ~20 attributes on `server`  (TinkerBox · SRP · M · low)
Free function reaches into 20+ attrs by name.  Renaming `_session_mgr` breaks startup silently — Python doesn't catch attribute mutations at startup-define time.
**Fix:** `@dataclass class ServerComponents`; `run_startup(config) -> ServerComponents`; `VoiceServer.__init__(config, components)`.

### S19: `Database` is monolithic (40+ methods, no domain split)  (TinkerBox · SRP+ISP · L · medium)
One `Database` god-object owns devices/sessions/messages/notes/events/config/scheduler tables.  Every test touching any persistence imports all of db.py (~15s including aiosqlite + sqlite-vec).
**Fix:** Domain repos (`DeviceRepo`, `SessionRepo`, `MessageRepo`, `EventRepo`, `ConfigRepo`, `NotesRepo`).  `Database` becomes connection-pool + schema migrator.

### S20: Mode-aware logic ("voice_mode 0/1/2/3") scattered across 6+ sites  (TinkerBox · OCP · M · medium)
Each "mode" is a tuple of (stt_backend, tts_backend, llm_backend_resolver, system_prompt, max_tokens, timeout_s, fallback_to) but lives nowhere as a tuple.  Adding voice_mode 4 = lockstep edits.
**Fix:** `@dataclass class VoiceMode` + `ModeRegistry`.

### T6: service_registry is half-applied — Layer 0 platform init bypasses it entirely  (TinkerTab · DIP · M · medium)
Only 5 of ~20 subsystems flow through registry.  `service_dragon` is "nearly vestigial" (its own comment).  Boot is a 200-line god-function.
**Fix:** Wrap remaining subsystems as services.

### T7: `widget_t` is a tagged-union-without-being-one  (TinkerTab · ISP · M · medium)
Every widget instance carries the union of all 6 type payloads — ~37 KB PSRAM cost across the 32-slot store on payloads only one type at a time uses.  Adding a 7th type requires editing 5 places.
**Fix:** Actual tagged union + `widget_renderer_t` registry (function-pointer per type).

### T8: chat trio leaks state — ui_chat.c holds streaming state that belongs elsewhere  (TinkerTab · SRP · M · medium)
"Is the AI streaming?" tracked in two places (ui_chat.c + chat_msg_view).  ~70 LOC of reconciliation logic.  Concrete bug class: `closes #129` two-bubble bug was directly caused by this state distribution.
**Fix:** chat_msg_store as single source of truth (`begin_stream/append/end_stream`); ui_chat subscribes.

### T10: voice.c writers `strcat` into shared buffers without all readers taking the mutex  (TinkerTab · LSP-style contract · S · low)
[`voice.h:78-87`](https://github.com/lorcan35/TinkerTab/blob/main/main/voice.h#L78) literally documents the race.  ui_chat.c:379 still uses the unsafe getter.  Reading mid-`strcat` returns garbage.
**Fix:** Mark legacy getters `__attribute__((deprecated))`; migrate callsites to `_copy` variant; delete deprecated.

### T11: `ui_home.c` (1,729 LOC) does both "home shell" and "widget renderer"  (TinkerTab · SRP · M · medium)
Adding a new widget type means editing ui_home.  Pairs with T7.
**Fix:** Extract `widget_render.{c,h}`; per-type rendering in `widget_render_live/list/chart/media/prompt.c`.

### T17: 50+ static globals in voice.c are unstated coupling cost  (TinkerTab · encapsulation · S · low)
**Prep step for T1.**  Group state into per-concern structs (`voice_ws_state_t`, `voice_audio_state_t`) before splitting.  Makes T1 mechanical instead of archaeological.

---

## Low findings

### S21: `Tool` interface lacks args_schema validator — every tool re-validates ad-hoc  (TinkerBox · ISP · S · low)
Tools declare `parameters_schema` but registry doesn't validate.  Malformed args land as raw exceptions in `tool_args_invalid` frames — exact thing C1/B7 audits called UX leak.
**Fix:** ToolRegistry runs jsonschema validation against `parameters_schema` before `execute`.

### S22: `Tab5Surface._safe_send` convention duplicated across emit sites  (TinkerBox · SRP · S · low)
6 emit methods × ~25 LOC each.  7th widget type = 7th copy.
**Fix:** Internal `_emit(widget_type, base_msg)` helper.

### S23: `OpenRouterBackend` mixes legacy in-memory history + multi-turn + retry policy  (TinkerBox · SRP · S · low)
Two histories living in one class.  Bug class: `pipeline._post_process_dictation` uses `generate_stream` (legacy path) for dictation summary — would mutate `self._conversation` shared with future legacy text turn (currently no other legacy callers, but trap waits).
**Fix:** Drop legacy in-memory history.

### S24: `lifecycle/shutdown.run_shutdown` mirrors startup's god-mutator shape  (TinkerBox · SRP · part of S18 · low)
Folds into S18's `ServerComponents` extraction.

### S25: `progress_emit.emit_progress_pair` good but call sites all hand-rebuild legacy dicts  (TinkerBox · DRY · S · low)
**Fix:** Per-event-type helpers (`emit_tool_call`, `emit_tool_result`, `emit_dictation_summary`).

### T9: mode_manager is dead code today  (TinkerTab · SRP · S · low)
130 LOC for one mutex.  `voice_connect_async` is already idempotent.
**Fix:** Inline — delete files, move `s_mutex` into voice.c.

### T12: voice.c WS error class hidden through opaque logging  (TinkerTab · ISP · S · low)
Pair with T5 — `listener.on_ws_error(error_class, detail)`.  Or expose `voice_health_t` with `auth_fail_count`, `handshake_fail_count`, `using_fallback`.

### T13: widget upsert handlers in voice.c violate ISP via local `extern` declarations  (TinkerTab · ISP · S · low)
6 identical local `extern` blocks ~25 LOC duplicated.  Promote out of function bodies.

### T14: debug_server.c includes 12+ ui_*.h headers it doesn't always use  (TinkerTab · ISP · S · low)
**Fix:** Extract `ui_navigation.h` ("show this screen by name").

### T15: chat data path split across 5 files with overlapping responsibility  (TinkerTab · SRP · folded into T8 · low)

### T16: `service_*.c` siblings don't honor a uniform contract  (TinkerTab · LSP · S · low)
`audio_service_stop` mutes the speaker; `dragon_service_stop` is a no-op.  Document the start/stop contract; align.  Pair with T6.

---

## What both codebases do well (LEARNINGS-doc fodder)

### Server (TinkerBox)
- **W1 — `NotificationStore` Protocol is textbook OCP.**  ([scheduler/store.py:34-100](../dragon_voice/scheduler/store.py#L34))  Let `InMemoryNotificationStore` ship in ε1a and `SqliteNotificationStore` slot in for ε2 with **zero changes** to `SchedulerManager`.  Model the rest of the codebase should converge on.
- **W2 — Backend factory pattern (LLM/STT/TTS) uniform + lazy-imported.**  ImportError on optional deps doesn't take whole package down; adding a backend = one dict entry + new file.
- **W3 — `error_event` + `Severity`/`Scope` taxonomy.**  Every UX-gap fix walks back to a *consistent* error envelope.  Painful to retrofit; getting it in early was a big win.
- **W4 — Middleware decomposition into stateless functions.**  Right pattern — S1's recommendation is "do this for handlers too."

### Firmware (TinkerTab)
- **W5 — `chat_msg_store` SRP is clean.**  Pure C ring with no LVGL/PSRAM/voice imports — narrow API.  Good model to copy.
- **W6 — `task_worker` + `lv_async_call` discipline well-applied** in voice.c hot paths.  Cross-thread calls hop to the right task.  Hard to add later.
- **W7 — `widget_store.c` is a model bounded-cache module.**  PSRAM allocation once at boot, no per-widget malloc, eviction observability hooks.  317 LOC, one concern.
- **W8 — `service_registry` pattern shape is right.**  The problems are how few callers go through it (T6).

---

## Sequencing

Recommended order (ROI / risk):

**Phase 1 — Lowest-risk highest-leverage (1-2 weeks)**
1. **S7** TurnGate extraction — youngest coupling, easiest to clean before it grows.  ~1 PR.
2. **T3 + T4** debug_server endpoint table + auth/CORS centralization — 1 bundled PR; unblocks dashboard work.
3. **S4** `ConversationEngine.swap_llm` — closes WS↔HTTP drift.  ~1 PR.
4. **S12** `inference_executor` move — kills 4 reverse-imports.  ~1 PR.
5. **S25** + **S22** progress + surface emit helpers — small DRY wins.

**Phase 2 — Capability layer + protocol cleanup (2-3 weeks)**
6. **S3** LLM/STT/TTS capability protocols — eliminates `hasattr` graveyard.  3 PRs (one per capability).
7. **S5** `BackendPool` extraction — closes "swap policy x4" duplication.  ~1 PR.
8. **S10** + **S14** narrow protocols + `LLMBackend.kind`.  Pair.
9. **S13** `text_utils.py` extraction — kills 5-site regex scatter.  ~1 PR.
10. **T17** voice.c statics → per-concern structs (prep for T1).

**Phase 3 — God-class surgery (multi-sprint)**
11. **S2** VoicePipeline split into `AudioIngestor` / `TurnRunner` / `BackendOrchestrator`.  3-4 PRs.
12. **S1** VoiceServer split — `WsDispatcher` + per-command handler classes.  6+ PRs.
13. **T1** voice.c carve into 6 modules.  6 atomic PRs, line-by-line moves first.

**Phase 4 — Domain reshape (longest tail)**
14. **S19** `Database` → domain repos.  Multi-PR; touches every persistence consumer.
15. **S20** + **S9** mode + config reshape.  Schema migration required.
16. **T7** + **T11** widget renderer registry.

**Opportunistic cleanup (anytime)**
- S6, S8, S11, S15, S16, S17, S18, S21, S23, S24
- T9, T13, T14, T16

---

## How to use this doc

1. Each **Critical** + **High** finding gets a sub-issue filed under [#169](https://github.com/lorcan35/TinkerBox/issues/169) (TinkerTab findings cross-link to TinkerTab issues).
2. Refactor PRs reference both the sub-issue and `closes` it; LEARNINGS gets an entry for non-obvious decisions.
3. Update *this* file as findings ship (move from "open" to "closed", record actual PR # next to each).

This audit reflects the codebase as of 2026-04-26.  As individual findings ship, this doc gets stale fast; PRs that close a finding should bump the status line at the top.
