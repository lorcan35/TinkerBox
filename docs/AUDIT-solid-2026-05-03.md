# TinkerBox SOLID Audit — 2026-05-03

> **⚠️ SUPERSEDED 2026-05-17:** This audit's P0 finding claimed
> `server.py` is 2,688 LOC.  Actual is **1,522 LOC** after 27
> extract-handler refactor commits landed post-audit (search
> `git log --oneline --grep "extract"`).  The SRP-1 violation is
> materially resolved.  Re-audit before treating any P0 here as
> actionable.

> Fresh pass after Waves 21a / 21b / 22b (PRs #205 / #206 / #207) closed
> the prior audit's three top-priority findings (capability registry,
> Protocol mixins, `ConversationEngine.swap_llm`).  Compares against
> [`SOLID-AUDIT.md`](SOLID-AUDIT.md) (2026-05-02 sweep) and the still-
> deferred items from [`UX-GAPS.md`](UX-GAPS.md).

## 1. Executive summary

The `llm/` subsystem is now in good shape — the three router-era debts
the prior audit ranked P0 (`F1` capability mismatch, `F3` `_llm` direct
access, `F5` optional features unenforced) have been closed by Waves
21–22 with proper Protocols + a centralised registry.  The dominant
residual debt has shifted *upward* into `server.py` itself: the WS
voice handler family is now ~1,100 LOC across `_handle_text_body`,
`_handle_config_update`, `_handle_user_media`, `_handle_register`,
the inline dispatcher, and the per-connection callback closures —
all on the same class with the same shared `conn_state` dict.  Three
new responsibilities have accreted into `server.py` since the prior
sweep (codec negotiation, video/audio relay, agent-log fan-out wiring)
without earning their own seam.  Three real LSP-class bugs were found
in `pipeline.py` / `handlers/config_api.py` / `api/system.py` that the
prior audits missed.

**Counts:** 2 P0, 12 P1, 14 P2, 4 P3, 4 anti-findings.  None are
ship-blockers — the system runs and is exercised by 556 unit tests —
but the WS dispatcher extraction (long-deferred Wave 22a) is now the
single highest-leverage refactor.

---

## 2. Findings

### Single Responsibility (SRP)

#### SRP-1 [P0] — `server.py` WS handler family is now 1,100+ LOC of orchestration on one class
**Where:** [dragon_voice/server.py:470-2688](dragon_voice/server.py#L470)
**Smell:** `VoiceServer` carries the full WS dispatch surface
(`_handle_ws_voice` 525 LOC, `_handle_register` 521 LOC,
`_handle_text_body` 437 LOC, `_handle_config_update` 437 LOC,
`_handle_user_media` 107 LOC, plus closures for `on_audio` / `on_event`
/ `_on_tool_call` / `_on_tool_result` / `_on_tool_error` /
`_emit_via_ws` / `_surface_send` and a per-connection rate-limit clock
in the `_last_config_update_ts` slot).  All five command handlers thread
the same `conn_state` dict; mutations to a key in one handler are felt
by all the others with no type discipline.
**Why it's a violation:** One stakeholder (ops/SRE) cares about
keepalive + heartbeat + eviction; another (product) cares about text /
voice / config_update behaviour; a third (firmware integration) cares
about codec negotiation + video relay.  The class can only change for
all three reasons.  Adding a new WS command type touches the dispatcher
table inside `_handle_ws_voice` plus a new `_handle_*` method plus the
disconnect cleanup plus the cancel branch — four edits in the same
2.7 KLOC file.
**Concrete fix:** Re-open Wave 22a (deferred per the brief).  Extract a
`WsDispatcher` class that owns the routing table + per-connection
state.  Per-command `*Handler` classes (`TextHandler`, `MediaHandler`,
`ConfigHandler`, `RegisterHandler`, `CancelHandler`) take their deps
explicitly via constructor.  `conn_state` becomes a typed dataclass
(`ConnState`).  Behaviour-preserving move first; decompose internals
in follow-ups.
**Risk:** High blast radius — touches every WS-using test.  Mitigate by
landing the move PR with no behavioural changes (`conn_state["…"]`
becomes `state.…` mechanically) and a smoke test on the real Tab5
before merging.

#### SRP-2 [P1] — `_handle_text_body` mixes 7 responsibilities
**Where:** [dragon_voice/server.py:1640-2076](dragon_voice/server.py#L1640)
**Smell:** Single 437-LOC method handles: TC bypass dispatch, WS
keepalive bracket, empty-reply guard + wrap synthesis, receipt
emission, media detection + render + strip, TTS synth + resample +
chunked send + Piper-kill on timeout, and a per-turn cost receipt.
The local-vs-TC paths each duplicate keepalive + wrap + media-emit
with subtle differences (TC path emits `text_update` *before* media,
local path same; local path strips tool markup with a *second* inline
regex `_TOOL_RE_LOCAL` distinct from `conversation._TOOL_MARKUP_RE`).
**Why it's a violation:** Adding a new voice mode requires editing this
method; fixing a media-emit bug (e.g. order of `text_update`) requires
two edits at the two paths.  A mistake in one path slips through tests
that only exercise the other.
**Concrete fix:** Extract `TextTurnRunner.run(ctx, mode_strategy) ->
TurnResult` where `mode_strategy` is one of `LocalConvEngineStrategy`,
`TinkerClawBypassStrategy`, `RouterStrategy`.  Keepalive + receipt +
media post-processing become composable steps (`KeepaliveBracket`,
`ReceiptEmitter`, `MediaPostProcessor`) the runner applies uniformly.
**Risk:** Medium — paired with SRP-1 the strategy pattern falls out
naturally; tests already exist for the TC path (`test_tinkerclaw_streaming`)
and local path (`test_voice_path_tool_callbacks`).

#### SRP-3 [P1] — `_handle_config_update` is a 437-LOC mode-swap mega-procedure
**Where:** [dragon_voice/server.py:2078-2514](dragon_voice/server.py#L2078)
**Smell:** Rate-limit, codec negotiation, mode-tier policy resolution,
TC health probe, OpenRouter-key pre-flight, TC-token pre-flight, DB
session update, pipeline backend swap, conversation backend swap, name
display update, fleet_summary build, vision_capability advertise (with
hardcoded per-frame pricing nested inside!), and a `cap_downgrade`
TTS speak-system invocation — all in one method.  The vision-capability
section (lines 2438-2496) embeds a price-per-frame switch on model id
substrings (`"gpt-4o"`, `"sonnet"`, `"haiku"`, `"gemini"`) that is
disconnected from `_PRICING_MILS_PER_M` in `openrouter_llm.py`.
**Why it's a violation:** A mode-swap fix touches everything else.  The
inline pricing switch will silently drift from the canonical pricing
table (already missed Opus, GLM, Grok, Kimi, Qwen, etc.).  Voice-mode
`4` would require six lockstep edits (also flagged as the still-open
S20).
**Concrete fix:** (a) Extract a `VoiceMode` registry per `ModeRegistry`
(see SRP-7).  (b) Move the config_update handler into `ConfigHandler`
(SRP-1 follow-up) and split into `validate_preconditions`,
`apply_swap`, `advertise_capabilities`.  (c) Delete the inline
per-frame pricing block — call `price_for_model(model, 0, 1024)` /
`price_for_model(model, 4096, 0)` to derive a real per-frame cost from
the canonical table.
**Risk:** Medium.  Tests `test_config_update_rate_limit_signal`,
`test_missing_key_gamma_arch`, `test_tinkerclaw_health_check` already
pin the validation paths.

#### SRP-4 [P1] — `pipeline.py` still owns 5+ responsibilities — pool-aware swap, dictation post-process, fallback-STT prewarm + cache, hallucination-clean, receipt emission, surface turn-gating
**Where:** [dragon_voice/pipeline.py:146-1733](dragon_voice/pipeline.py#L146)
**Smell:** `VoicePipeline` (1,733 LOC) is the audio pipeline +
dictation orchestrator + backend pool client + per-turn ToolGate +
Receipt emitter + Hallucination filter + Fallback-STT manager +
SurfaceManager turn-gate caller.  Pool-identity helpers (`_stt_sig`,
`_tts_sig`, `_llm_sig`) live at module top-level because there is no
`BackendPool` class to host them.  The `_fallback_stt` cache and the
`_schedule_fallback_stt_prewarm` machinery live on the same instance
that runs the audio loop.
**Why it's a violation:** Same SRP problem as SRP-1, just narrower.
A unit test for "what happens when STT fails to a fallback" can't be
written without standing up the entire pipeline.
**Concrete fix:** Three extracts in priority order: (1) `BackendPool`
class encapsulating `pool: dict`, `_stt_sig/_tts_sig/_llm_sig`, and
`borrow(config, kind)` / `release(backend)`; (2) `FallbackStt`
service that owns prewarm + cache + transcribe-with-fallback; (3)
`ReceiptEmitter` consolidating the LLM + TTS + STT receipt formats
across pipeline + server.  S2 from prior audit, still open.
**Risk:** Low.  Each extract is self-contained.  Tests exist
(`test_backend_pool`, `test_b6_hybrid_first_utterance`).

#### SRP-5 [P1] — `tools/registry.py` still bundles registry + 3-dialect parser + 2-format formatter
**Where:** [dragon_voice/tools/registry.py:1-440](dragon_voice/tools/registry.py#L1)
**Smell:** 440 LOC: dict + parser walking 3 dialects (legacy,
`<tool_call>`, bracketed-name) + tolerance heuristics (xLAM
`[tool>` / `<tool]` / `[tool]` quirks, JSON brace walker, noise-ident
gate) + format-for-LLM (compact + full) + agent_log instrumentation.
The hardcoded `priority_tools` list (line 394) inside `_format_compact`
duplicates routing knowledge the router already has.
**Why it's a violation:** Adding a new dialect, a new format strategy,
or a new tool requires editing the same file.  The `_format_compact`
path is a one-way broadcast that doesn't know the model's declared
capabilities (could feed an FC-trained model the standard
`<tool_call>` format and a legacy model the legacy format).
**Concrete fix:** Three files: `registry.py` (dict + register/list/get
only), `parser.py` (the three dialects with tolerance heuristics +
brace walker), `formatter.py` (with strategy classes
`CompactFormatter`, `FullFormatter`, `FCFormatter`).  The
`ToolRegistry` consumes a `Formatter` to produce the LLM prompt.
S17 / F4 from prior audits — still open.
**Risk:** Medium.  `test_tool_parser` (387 LOC) covers the parser
heavily; the move would be largely test-preserved.

#### SRP-6 [P1] — `lifecycle/startup.run_startup` mutates 21 attributes on the server in a single 281-LOC sequence
**Where:** [dragon_voice/lifecycle/startup.py:40-321](dragon_voice/lifecycle/startup.py#L40)
**Smell:** Free function `run_startup(server, app)` reaches into
`server._proxy_session`, `server._db`, `server._session_mgr`,
`server._message_store`, `server._memory_service`,
`server._tool_registry`, `server._surface_mgr`, `server._scheduler_mgr`,
`server._scheduler_store`, `server._conversation`, `server._notes_svc`,
`server._purge_task`, `server._media_cleanup_task`,
`server._memory_monitor_task` — by name, with no compile-time
guarantee any of them exist.
**Why it's a violation:** Renaming `_session_mgr` to `session_mgr`
would silently break startup at runtime.  Adding a new boot step
forces an attribute decision that has nothing to do with the boot
order.
**Concrete fix:** Replace ~20 server attributes with a
`@dataclass ServerComponents` that boot returns; `run_startup(config)
-> ServerComponents`.  `VoiceServer` holds one `self._cmp:
ServerComponents`.  S18 from prior audit, still open.
**Risk:** Low — mechanical rename.  Tests
`test_lifecycle_monitors`/`test_status_handlers`/`test_config_api_handlers`
need their fixtures updated but not their assertions.

#### SRP-7 [P2] — `db.py` is still a 844-LOC monolith of 30+ method domains
**Where:** [dragon_voice/db.py:1-844](dragon_voice/db.py#L1)
**Smell:** Single `Database` class owns devices, sessions, messages,
notes, events, config, and migration helpers.  Every test that needs
*any* persistence pulls in aiosqlite + sqlite-vec + the full schema.
**Why it's a violation:** Test import cost (~15 s) + can't unit-test
"what does the message store do when a session row is missing" without
a full DB fixture.
**Concrete fix:** Extract `DeviceRepo`, `SessionRepo`, `MessageRepo`,
`NoteRepo`, `EventRepo`, `ConfigRepo` as thin classes on top of a
shared `Connection`.  `Database` becomes the connection lifecycle +
schema apply.  S19 from prior audit — still open.
**Risk:** Low per repo, but ~6 small PRs of mechanical work.  Defer
until DB methods grow further or a real new domain (e.g. budgets) is
added.

#### SRP-8 [P2] — `lifecycle/startup` registers 5 distinct subsystems whose order matters but isn't declared
**Where:** [dragon_voice/lifecycle/startup.py:48-313](dragon_voice/lifecycle/startup.py#L48)
**Smell:** Sequential side-effecting init: DB → SessionMgr →
MessageStore → MemoryService → ToolRegistry → SurfaceManager →
SchedulerManager (must be after SurfaceManager) → widget tools (must
be after SurfaceManager + Scheduler) → ConversationEngine (must be
after MessageStore + ToolRegistry + MemoryService + MediaStore) →
REST routes → Notes + NoteTool → MCP bridge → purge tasks → memory
monitor.  Order is encoded in the procedural shape — re-ordering two
lines silently produces an init failure ("foo not yet defined") at
the dependent line.
**Why it's a violation:** OCP — adding a new subsystem requires
finding the right place in the procedure rather than declaring its
deps.
**Concrete fix:** Pair with SRP-6.  Convert to a topological-sort init
of named modules with explicit `requires=[…]`.  Or: a builder pattern
`ServerComponentsBuilder().with_db().with_sessions(...).build()` that
makes the dependency graph explicit at the type level.
**Risk:** Low.  Boot is exercised by every test that builds an `app`.

#### SRP-9 [P2] — Hallucination/empty-reply heuristics live in 4 modules with subtle drift
**Where:** [dragon_voice/server.py:1880](dragon_voice/server.py#L1880),
[dragon_voice/conversation.py:34-41](dragon_voice/conversation.py#L34),
[dragon_voice/pipeline.py:88-93](dragon_voice/pipeline.py#L88),
[dragon_voice/llm/dual.py:44-67](dragon_voice/llm/dual.py#L44),
[dragon_voice/tools/response_wrap.py:43-70](dragon_voice/tools/response_wrap.py#L43)
**Smell:** `_TOOL_MARKUP_RE` (conversation), `_TOOL_RE_LOCAL`
(server inline), `_HALLUCINATION_STOPS` (pipeline), `_BRACKET_NOISE` +
`_RESIDUAL_XML_TAG` (response_wrap *and* duplicated in dual.py with an
explicit comment apologising for the cycle).  Each is "almost but not
quite" the same — the server's `_TOOL_RE_LOCAL` allows stray `>` after
`</args>` that conversation's regex lacks.
**Why it's a violation:** A bug in one regex doesn't get caught in
the others.  S13 from prior audit — partially addressed by
`response_wrap.py` (#79) but the consolidation never landed.
**Concrete fix:** Extract `dragon_voice/text_utils.py` with the
canonical `strip_tool_markup`, `looks_like_useful_text`, and
`_HALLUCINATION_STOPS`.  Delete the duplicates from `dual.py` (now
that the cycle from server.py is gone, it can import from
`text_utils`).
**Risk:** Trivial.  Tests `test_looks_like_useful_text` already pin
the response_wrap copy.

#### SRP-10 [P2] — Receipt emission still hand-built in 3 places with subtle field drift
**Where:** [dragon_voice/pipeline.py:1199-1252](dragon_voice/pipeline.py#L1199),
[dragon_voice/server.py:1768-1782](dragon_voice/server.py#L1768) (TC bypass),
[dragon_voice/server.py:2029-2067](dragon_voice/server.py#L2029) (text path),
[dragon_voice/pipeline.py:1286-1297](dragon_voice/pipeline.py#L1286) (TTS receipt)
**Smell:** Four sites build a `{"type": "receipt", "stage": …, …}`
event with hand-typed field sets.  TC path always sends `cost_mils=0`
+ `prompt_tokens=0`; OpenRouter sites compute via `price_for_model`.
TTS sites omit `prompt_tokens` entirely.  Drift across sites is the
exact bug-class S16 was filed against.
**Concrete fix:** `ReceiptEmitter.emit_llm(usage, on_event)`,
`emit_tts(backend, ms, on_event)`, `emit_tc(model, on_event)`.  One
shape per stage.
**Risk:** Trivial; payloads are pure dicts.

#### SRP-11 [P2] — `media/pipeline.py` mixes detection + render + URL signing + SSRF guard
**Where:** [dragon_voice/media/pipeline.py:1-540](dragon_voice/media/pipeline.py#L1)
**Smell:** 540 LOC: code-block detection, Pygments render, table
detection, Pillow render, URL detection + SSRF guard, download via
aiohttp, resize, MediaStore write, signed URL build.  Three ranks of
abstraction in one file.
**Concrete fix:** `media/detect.py` (regex matchers), `media/render.py`
(Pygments + Pillow), `media/proxy.py` (SSRF + download).  The
existing `MediaPipeline` becomes a 100-LOC orchestrator.
**Risk:** Low.  41 unit tests already exist.

#### SRP-12 [P3] — `dragon_voice/audio_codec.py` mixes uplink + downlink + capability negotiation in 164 LOC
**Where:** [dragon_voice/audio_codec.py:1-164](dragon_voice/audio_codec.py#L1)
**Smell:** Module-level dispatch; minor.  Cited only because it lives
on the `_handle_register` boundary and SRP-1's extraction will move
the call site, so a co-located clean-up makes sense.
**Concrete fix:** Defer until SRP-1 lands.

---

### Open–Closed (OCP)

#### OCP-1 [P1] — Mode-aware logic ("voice_mode 0/1/2/3") still scattered across 6 sites and 17 inline comparisons
**Where:** [dragon_voice/server.py:2151-2496](dragon_voice/server.py#L2151)
(17 `voice_mode == N` comparisons), [dragon_voice/llm/router.py:47](dragon_voice/llm/router.py#L47)
(`TIER_FOR_MODE`), [dragon_voice/pipeline.py](dragon_voice/pipeline.py)
(mode-aware timeouts).
**Smell:** Each voice mode is a tuple of (stt_backend, tts_backend,
llm_backend_resolver, system_prompt, max_tokens, timeout_s, fallback,
tier_set).  No `VoiceMode` dataclass.  Adding voice_mode 4
("scratchpad" / "skill-only" / etc.) requires lockstep edits in
config.py + server.py + pipeline.py + router.py.
**Why it's a violation:** OCP.  The class of "voice modes" should be
open for extension, closed for modification.  Today it's the opposite.
**Concrete fix:**
```python
@dataclass(frozen=True)
class VoiceMode:
    id: int
    name: str
    stt_backend: str
    tts_backend: str
    llm_resolver: Callable[[LLMConfig], str]  # picks ollama / openrouter / tinkerclaw
    system_prompt: str
    max_tokens: int
    pipeline_timeout_s: int
    router_tier: frozenset[str] | None  # None = bypass router (TC mode)
    fallback_to: int | None  # mode id to revert to on key-missing/health-fail

VOICE_MODES = ModeRegistry({
    0: VoiceMode(id=0, name="local", ...),
    1: VoiceMode(id=1, name="hybrid", ...),
    2: VoiceMode(id=2, name="cloud", ...),
    3: VoiceMode(id=3, name="tinkerclaw", ...),
})
```
Then `_handle_config_update` becomes `mode = VOICE_MODES[voice_mode];
mode.apply(conn_config); await pipeline.swap_backends(...)`.
S20 / out-of-scope #2 from prior audit — still open.
**Risk:** Medium.  17 sites to migrate but each is mechanical.

#### OCP-2 [P1] — Vision-capability per-frame pricing duplicated in `_handle_config_update` instead of querying `_PRICING_MILS_PER_M`
**Where:** [dragon_voice/server.py:2462-2469](dragon_voice/server.py#L2462)
**Smell:** Inline switch on substrings (`"gpt-4o"`, `"sonnet"`,
`"haiku"`, `"gemini"`) hardcodes per-frame mils (1200/4500/400/200)
disconnected from the canonical pricing table.  Vendor-tier price
ranges (Opus 4.7, Grok 4.20, Kimi K2.6, Qwen 3.6) silently default to
0.
**Why it's a violation:** OCP — adding a new vision model requires
editing this switch *and* `_PRICING_MILS_PER_M` *and* possibly
`_OPENROUTER_CAPS`.
**Concrete fix:** Replace with `price_for_model(model_id,
prompt_tokens=1024, completion_tokens=0)` and a follow-up
`vision_per_frame_mils(model)` helper that codifies a vision-frame
token-cost approximation (typical: 1k tokens per 1024-px image side).
**Risk:** Trivial.  No tests pin the magic numbers.

#### OCP-3 [P1] — `_format_compact` hardcodes a 6-tool priority list disconnected from the router's selection
**Where:** [dragon_voice/tools/registry.py:394-397](dragon_voice/tools/registry.py#L394)
**Smell:** `priority_tools = ["web_search", "datetime", "remember",
"recall", "calculator", "schedule_reminder"]` — adding a new "must be
in the local prompt" tool requires editing this constant.  Also
duplicates routing knowledge the model-side prompt should pull from
elsewhere (the model's declared capabilities + the registered tool
set).
**Why it's a violation:** OCP — new local tools shipped without
editing this list won't appear in the local-mode prompt and small
models won't know about them.
**Concrete fix:** Each `Tool` declares a `priority: int = 50`
attribute; `_format_compact` selects the lowest 5-7 by priority.
Default 50 means "not in compact prompt"; explicit 10/20/30 puts the
tool above the cutoff.  Existing 6 priority_tools get priority < 50.
**Risk:** Low.  Test impact ~3 cases in `test_tool_parser`.

#### OCP-4 [P2] — Adding a new STT/TTS/LLM backend requires editing the module's `_BACKENDS` dict + capability_registry
**Where:** [dragon_voice/llm/__init__.py:6-14](dragon_voice/llm/__init__.py#L6),
[dragon_voice/stt/__init__.py](dragon_voice/stt/__init__.py),
[dragon_voice/tts/__init__.py](dragon_voice/tts/__init__.py),
[dragon_voice/llm/capability_registry.py:172-177](dragon_voice/llm/capability_registry.py#L172)
**Smell:** Backend factory is a static dict mapping name → import path.
Two registration sites for an LLM backend (factory + capability detector).
**Concrete fix:** Move backend self-registration into the backend
module: `LLMBackend.register_factory("name", cls)` at module import.
The factory dict becomes populated implicitly.  Mid-priority — current
shape works but adds a step for plugin-style backends.

#### OCP-5 [P2] — `LLMConfig` god-config: 18 fields for 7 backends, single dataclass, both single-backend AND `fleet[]` paths coexist
**Where:** [dragon_voice/config.py:88-134](dragon_voice/config.py#L88)
**Smell:** `ollama_*`, `openrouter_*`, `lmstudio_*`, `npu_genie_*`
(`genie_model_dir`/`genie_config`), `tinkerclaw_*`, `dual_*`, plus
`fleet: list[ModelSpec]` from #185.  Two valid configurations of the
same dataclass: legacy single-backend (`backend: ollama` +
`ollama_model: ...`) and router-fleet (`backend: router` + `fleet:
[...]`).  Validation can't distinguish them.
**Concrete fix:** Per-backend dataclasses (`OllamaConfig`,
`OpenRouterConfig`, etc.) under a `backend_config: Union[…]`.  Or a
sub-dict approach (`config.llm.backends["ollama"] = OllamaConfig(...)`).
S9 from prior audit — still open.
**Risk:** Medium.  Touches every config-parsing test.

#### OCP-6 [P3] — `voice_mode == 3` is checked specifically all over the place — TC bypass is special-cased inline
**Where:** [dragon_voice/server.py:1649](dragon_voice/server.py#L1649),
[dragon_voice/server.py:2155](dragon_voice/server.py#L2155),
[dragon_voice/server.py:2166](dragon_voice/server.py#L2166),
[dragon_voice/server.py:2183](dragon_voice/server.py#L2183),
[dragon_voice/server.py:2201](dragon_voice/server.py#L2201),
[dragon_voice/server.py:2260](dragon_voice/server.py#L2260)
**Smell:** TC = mode 3 is hardcoded at 6+ sites, each with a slightly
different "bypass / use TC / pre-validate" dance.  Subsumed by OCP-1's
`VoiceMode` registry.
**Concrete fix:** Same as OCP-1.

---

### Liskov Substitution (LSP)

#### LSP-1 [P1] — `LLMBackend.generate_stream_with_messages` default impl silently degrades multimodal turns to text-only
**Where:** [dragon_voice/llm/base.py:67-110](dragon_voice/llm/base.py#L67),
[dragon_voice/llm/lmstudio_llm.py](dragon_voice/llm/lmstudio_llm.py)
(does not override; uses default impl)
**Smell:** Base default formats messages as `f"{role}: {content}"` and
delegates to `generate_stream`.  Works for text-only; for a multimodal
content array (`[{"type": "image_url", ...}, {"type": "text", ...}]`)
the f-string becomes `"User: [{'type': 'image_url'...}]"` — the model
sees the literal Python repr of the content array, never the image.
LM Studio inherits this default; it has `/chat/completions` natively
but never uses the path.
**Why it's a violation:** LSP.  `LLMBackend` says
`generate_stream_with_messages(messages: list[dict])` — caller assumes
multimodal works.  LM Studio violates that.  The router happily picks
LM Studio for a vision turn (declared via lmstudio capability_registry
delegating to vision-substring) and the request silently corrupts.
**Concrete fix:** (1) `LMStudioBackend.generate_stream_with_messages`
override that hits `/chat/completions` directly — the OpenAI-format
multimodal content array is what LM Studio expects natively.  (2)
Either remove the broken default impl from `LLMBackend` (raise
`NotImplementedError`) or rename it to `generate_stream_format_compat`
to make the degradation explicit.  S10 from prior audit — partially
resolved (Ollama overrides correctly via #186) but LM Studio still
broken.
**Risk:** Low.  Add a `test_lmstudio_multimodal` that asserts the
content array survives.

#### LSP-2 [P1] — `OllamaBackend.get_last_usage` returns `dict` while `OpenRouterBackend.get_last_usage` returns the same dict but with one different key (`retried` only on OR), and callers consume both expecting the OR shape
**Where:** [dragon_voice/llm/ollama_llm.py:91-108](dragon_voice/llm/ollama_llm.py#L91),
[dragon_voice/llm/openrouter_llm.py](dragon_voice/llm/openrouter_llm.py)
(`_last_retried`, `_last_retry_reason` populated on retry),
[dragon_voice/server.py:2055-2056](dragon_voice/server.py#L2055)
**Smell:** Receipt emit reads `usage.get("retried", False)` /
`usage.get("retry_reason", "")`.  Ollama's usage dict never carries
those keys → defaults always `False`/`""` for local turns.  The
SupportsUsage Protocol declares the method but not the dict shape.
**Why it's a violation:** LSP weakly — both backends conform to the
Protocol's signature but their return contracts differ structurally.
**Concrete fix:** Convert the usage dict to a `UsageRecord`
dataclass with all fields explicit (`retried: bool = False`,
`retry_reason: str = ""`).  Backends construct a typed object, not
a dict.  Eliminates the silent default-on-missing-key path.
**Risk:** Low.  Strong typing is a net win; existing callers already
treat missing keys as default values.

#### LSP-3 [P1] — `_BACKENDS` re-exported for diagnostic enumeration but the diagnostic API exposes import-path strings, not capability metadata
**Where:** [dragon_voice/api/system.py:11-13](dragon_voice/api/system.py#L11),
[dragon_voice/api/system.py:133-148](dragon_voice/api/system.py#L133)
**Smell:** `GET /api/v1/backends` returns sorted backend names from
`_BACKENDS.keys()`.  No way to surface declared capabilities from this
endpoint — the dashboard "Backends" tab can't show "ollama supports
[text, vision, tool_calling]" without separate discovery.
**Why it's a violation:** Mild LSP — backends report their existence
but not their capabilities through the API surface.
**Concrete fix:** Add `available: list[{name, capabilities,
requires_api_key, etc.}]` instead of bare strings.
**Risk:** Trivial.  Minor API change.

#### LSP-4 [P2] — `STTBackend` / `TTSBackend` ABCs lack `kill_active_procs` / `total_calls` / `sample_rate` for some implementations
**Where:** [dragon_voice/stt/base.py:1-36](dragon_voice/stt/base.py#L1),
[dragon_voice/tts/base.py:1-42](dragon_voice/tts/base.py#L1),
[dragon_voice/pipeline.py:580](dragon_voice/pipeline.py#L580),
[dragon_voice/pipeline.py:1349-1352](dragon_voice/pipeline.py#L1349),
[dragon_voice/server.py:2007](dragon_voice/server.py#L2007)
**Smell:** `pipeline.py` does `if hasattr(self._tts, 'total_calls')`
(line 1349/1352), `if hasattr(self._tts, "kill_active_procs")` (1431,
1456); server.py does the same `kill_active_procs` hasattr at 2007.
Only Piper implements `kill_active_procs`; only the OpenRouter
backends carry `total_calls`.  No equivalent of `SupportsKill` /
`SupportsCallCounter` Protocol like the LLM side got in Wave 21b.
**Why it's a violation:** ISP / LSP — same pattern Wave 21b solved
on the LLM side, just not yet applied to STT/TTS.
**Concrete fix:** Mirror Wave 21b: `dragon_voice/stt/base.py` +
`dragon_voice/tts/base.py` get `@runtime_checkable Protocol`s
`SupportsKillProcs`, `SupportsCallCounter`, `SupportsSampleRate`
(latter is currently abstract on TTS but not STT — STT doesn't expose
it).  Replace 5 hasattr sites with isinstance.
**Risk:** Trivial.  Tests `test_text_path_tts_kill_on_timeout` and
`test_tts_flush_and_kill` already pin the kill behaviour.

#### LSP-5 [P2] — `pipeline._tts.sample_rate` accessed directly with no contract that all TTS backends carry it
**Where:** [dragon_voice/server.py:1971](dragon_voice/server.py#L1971),
[dragon_voice/api/synthesize.py:109](dragon_voice/api/synthesize.py#L109)
**Smell:** `tts_rate = pipeline._tts.sample_rate` — works because
`TTSBackend` ABC declares it abstract.  But the client code reaches
through `pipeline._tts` (private!), which is the actual smell.
**Concrete fix:** Add `pipeline.tts_sample_rate` (property already
exists on pipeline at line 1686, but the consumer in server.py reads
the private `_tts.sample_rate`).  Migrate the 2 sites.
**Risk:** Trivial.

#### LSP-6 [P3] — Abstract `LLMBackend.capabilities` returns text-only default; concrete backends now mostly delegate to capability_registry — but the default is still a footgun for new backends
**Where:** [dragon_voice/llm/base.py:124-131](dragon_voice/llm/base.py#L124)
**Smell:** New backend that forgets to override `capabilities` /
register a detector defaults to `{TEXT}`.  Router silently fails to
route vision turns to it.
**Concrete fix:** Make `capabilities` abstract (no default).  Force
new backends to declare or explicitly opt into text-only.
**Risk:** Low.  6 backends; all currently override.

---

### Interface Segregation (ISP)

#### ISP-1 [P1] — `ConversationEngine` is one fat class; consumers depend on the whole thing for narrow needs
**Where:** [dragon_voice/conversation.py:127](dragon_voice/conversation.py#L127)
(class def), consumed by:
- [dragon_voice/api/sessions.py:172](dragon_voice/api/sessions.py#L172) (`_build_context` for `get_context` route)
- [dragon_voice/api/messages.py:71](dragon_voice/api/messages.py#L71) (`process_text_stream` for `/chat`)
- [dragon_voice/api/completions.py](dragon_voice/api/completions.py) (direct LLM)
- [dragon_voice/server.py](dragon_voice/server.py) (4+ paths: text, vision, swap, fleet_summary)
**Smell:** Routes that only need stateless completion pull in
`ConversationEngine` (which carries DB, MessageStore, ToolRegistry,
MemoryService, MediaStore).  `api/sessions.py:get_context` reaches
into private `_build_context` to format LLM context.
**Why it's a violation:** Tests for those routes have to fixture the
whole engine.  A direct-LLM caller pays for memory injection it
doesn't want.
**Concrete fix:** Split into roles — `ContextBuilder` (build_context,
trim, augment), `TurnRunner` (process_text_stream + tool loop),
`LLMSwapper` (swap_llm + fleet_summary).  Routes consume only the
role they need.
**Risk:** Medium.  Migration of 4+ call sites.

#### ISP-2 [P1] — `VoicePipeline` exposes 12 methods + 6 properties; cancel handlers only need 1, status handlers only need 4
**Where:** [dragon_voice/pipeline.py:146](dragon_voice/pipeline.py#L146)
**Smell:** Cancel handler in server.py needs `pipeline.cancel()`.
Status handler in handlers/status.py needs `stt_name`, `tts_name`,
`llm_name`, `is_processing`.  Both pull the whole `VoicePipeline`.
**Concrete fix:** `Cancellable` Protocol, `PipelineStatus` Protocol.
Tied to SRP-4.
**Risk:** Low; defer until SRP-4 lands.

#### ISP-3 [P2] — `VoiceServer` is the dependency surface for everything that needs *anything* on the server (lifecycle/, handlers/)
**Where:** [dragon_voice/handlers/config_api.py:29](dragon_voice/handlers/config_api.py#L29),
[dragon_voice/handlers/status.py](dragon_voice/handlers/status.py),
[dragon_voice/handlers/debug.py](dragon_voice/handlers/debug.py)
**Smell:** Handlers take `server: Any` and reach into 5-15 attributes
(`server._config`, `server._active_connections`, `server._db`,
`server._stt_name`, `server._purge_task`, `server._mem_warn_mb`, etc.).
The handler doesn't say which attributes it depends on; rename one
attribute and one handler silently breaks at runtime.
**Concrete fix:** Pair with SRP-6 / SRP-8.  Handlers take typed deps
(e.g. `handle_status(request, *, status_data: StatusSnapshot,
backends: BackendNames)`).
**Risk:** Low — tests `test_status_handlers` etc. already exist.

#### ISP-4 [P2] — `setup_all_routes` takes 13 keyword args; routes that need 1-3 of them get them all
**Where:** [dragon_voice/api/__init__.py:30-44](dragon_voice/api/__init__.py#L30)
**Smell:** Single setup function aggregates every backend dependency.
Each `Routes` class then unpacks only what it needs.  Adding a route
with a new dep means changing the function signature.
**Concrete fix:** Pass a single `ApiContext` dataclass with optional
fields; each Routes class extracts what it needs from it.
**Risk:** Trivial.

#### ISP-5 [P3] — `_BACKENDS` dicts re-exported as `_BACKENDS` (private name) by `system.py` for the `/api/v1/backends` route
**Where:** [dragon_voice/api/system.py:11-13](dragon_voice/api/system.py#L11)
**Smell:** Privacy-violating import from sibling package.  Mild.
**Concrete fix:** Public `available_backends() -> list[str]` factory
function on each subpackage.

---

### Dependency Inversion (DIP)

#### DIP-1 [P1] — `STT/TTS/Lifecycle/API` import `inference_executor` directly from `pipeline.py`
**Where:** [dragon_voice/stt/moonshine_stt.py:13](dragon_voice/stt/moonshine_stt.py#L13),
[dragon_voice/tts/piper_tts.py:19](dragon_voice/tts/piper_tts.py#L19),
[dragon_voice/lifecycle/shutdown.py:107](dragon_voice/lifecycle/shutdown.py#L107),
[dragon_voice/api/system.py:104](dragon_voice/api/system.py#L104)
**Smell:** 4 sites do `from dragon_voice.pipeline import
inference_executor`.  This forces an STT-only (or TTS-only) test
import to load `pipeline.py` which transitively pulls in the LLM
factory + memory service + Surface manager.  Test startup tax.
**Why it's a violation:** DIP — high-level pipeline depends on the
shared executor, fine; STT and TTS are *lower*-level than pipeline
and shouldn't reach back.
**Concrete fix:** Move `inference_executor` to a top-level
`dragon_voice/runtime.py` (or `runtime/executors.py`).  Pipeline and
the 4 consumers all import from runtime.  S12 from prior audit —
still open.
**Risk:** Low — mechanical move.

#### DIP-2 [P1] — `handlers/config_api.py` HTTP swap path is a parallel implementation that still drifts from the WS swap path
**Where:** [dragon_voice/handlers/config_api.py:80-99](dragon_voice/handlers/config_api.py#L80)
**Smell:** Now that Wave 22b extracted `ConversationEngine.swap_llm`,
the WS path uses it — the HTTP path still calls `pipeline.swap_backends`
directly *and* doesn't call `ConversationEngine.swap_llm`.  After an
HTTP `POST /api/config`, `_conversation._llm` still points at the old
backend; subsequent `_handle_text` turns use the stale LLM.
**Why it's a violation:** **REAL BUG**, not just OCP/DRY.  Test
gap: `test_config_api_handlers` doesn't assert ConvEngine post-state.
F6 from prior audit — still open and still wrong, just rotated to a
different stale target.
**Concrete fix:** Add `await server._conversation.swap_llm(
new_config.llm, pool=server._backend_pool, voice_mode=...)` after
the pipeline swap loop.  Or factor the dual-swap into a single helper
on the server.
**Risk:** Low.  Add a unit test asserting the new state of
`_conversation._llm` after `POST /api/config`.

#### DIP-3 [P1] — Lifecycle (boot, shutdown) imports concrete classes; should depend on factories/Protocols
**Where:** [dragon_voice/lifecycle/startup.py:31-35](dragon_voice/lifecycle/startup.py#L31)
(`from dragon_voice.conversation import ConversationEngine`),
[dragon_voice/lifecycle/startup.py:76](dragon_voice/lifecycle/startup.py#L76)
(MemoryService + ToolRegistry inside try/except), etc.
**Smell:** Boot directly imports the concrete `ConversationEngine`,
`Database`, `SessionManager`, `MessageStore`, `MemoryService` classes.
Substituting a different message-store implementation (e.g. in-memory
for tests) requires monkey-patching.
**Concrete fix:** Pair with SRP-6.  Boot calls factory functions
that return Protocol-typed objects.
**Risk:** Defer until SRP-6.

#### DIP-4 [P2] — `media/pipeline.py` does its own URL signing via `MediaUrlSigner` injected at ctor — but tests can't substitute a no-op signer easily because the constructor expects the real class
**Where:** [dragon_voice/media/pipeline.py](dragon_voice/media/pipeline.py),
[dragon_voice/media/url_signer.py](dragon_voice/media/url_signer.py)
**Smell:** Concrete-class injection (`url_signer: MediaUrlSigner`)
instead of a `Signer` Protocol.
**Concrete fix:** Define `class Signer(Protocol): def sign(self, path:
str, ttl_s: int) -> str: ...`.
**Risk:** Trivial.

#### DIP-5 [P2] — `dragon_voice.api.scheduler` reaches into `_mgr._store` (private) for list/get
**Where:** [dragon_voice/api/scheduler.py:154](dragon_voice/api/scheduler.py#L154),
[dragon_voice/api/scheduler.py:170](dragon_voice/api/scheduler.py#L170),
[dragon_voice/api/scheduler.py:190](dragon_voice/api/scheduler.py#L190),
[dragon_voice/api/scheduler.py:226](dragon_voice/api/scheduler.py#L226)
**Smell:** REST handler reaches into `SchedulerManager._store` for
`list_all` / `get`.  These are read paths that should be on the
manager's public surface.
**Concrete fix:** Add `SchedulerManager.list(...)` / `.get(id)` /
`.list_pending(device_id)` as the public API; REST consumes them.
**Risk:** Trivial.  Test impact 1-2 cases.

#### DIP-6 [P2] — `api/sessions.py:get_context` reaches into private `ConversationEngine._build_context` and `_tool_registry`
**Where:** [dragon_voice/api/sessions.py:173-179](dragon_voice/api/sessions.py#L173)
**Smell:** Same DIP smell as DIP-5 — REST reaches into private
attribute on a higher-level service.
**Concrete fix:** Promote `_build_context` to public API
(`ConversationEngine.build_context(session_id, query)`).  S6
follow-up to ISP-1.
**Risk:** Trivial.

#### DIP-7 [P2] — `surfaces/manager.py` couples `Tab5Surface` ctor to `SurfaceManager` instance via private attribute injection (`surface._manager = self`, `surface._session_id = session_id`)
**Where:** [dragon_voice/surfaces/manager.py:55-58](dragon_voice/surfaces/manager.py#L55)
**Smell:** Bidirectional coupling — surface holds back-pointer to
its manager so `surface.prompt(on_action=...)` can register handlers.
**Concrete fix:** Inject the manager via constructor (or a
`SurfaceContext` dataclass).
**Risk:** Trivial.

#### DIP-8 [P3] — `tools/registry.py` lazy-imports `agent_log` inside `execute()` to avoid import cycle
**Where:** [dragon_voice/tools/registry.py:99-104](dragon_voice/tools/registry.py#L99)
**Smell:** Lazy import inside hot path indicates a layering problem
(registry → api → registry cycle latent).  Currently fine because
agent_log is leaf-level; flag for awareness.
**Concrete fix:** Move agent_log import to module top once the cycle
is broken.

---

### Cross-cutting / encapsulation

#### ENC-1 [P1] — `_handle_user_media` still reaches `getattr(conv, "_llm", None)` and `llm_backend.capabilities` instead of going through ConvEngine
**Where:** [dragon_voice/server.py:2546](dragon_voice/server.py#L2546)
**Smell:** Wave 22b moved the swap path to a public API; the read
path (capability check before vision turn) still cracks `_llm` open.
Adds `ConversationEngine.supports(modality) -> bool` and the smell
disappears.
**Concrete fix:** `ConversationEngine.supports_vision() -> bool` (or
generalised `supports(Modality.VISION)`).
**Risk:** Trivial.

#### ENC-2 [P1] — `pipeline._llm` still poked from server.py at 1 site; full encapsulation of the pipeline's LLM not yet enforced
**Where:** [dragon_voice/server.py:2333](dragon_voice/server.py#L2333)
**Smell:** `pipe_llm = getattr(pipeline, "_llm", None)` followed by
`pipe_llm.set_session_key(...)`.  The session-key injection should be
a method on the pipeline (`pipeline.set_session_key(key)` that
internally tries the backend if it `isinstance(SupportsSessionKey)`).
**Concrete fix:** `VoicePipeline.set_session_key(key: str)` →
internally `if isinstance(self._llm, SupportsSessionKey): ...`.
**Risk:** Trivial.

#### ENC-3 [P2] — Pipeline private state still poked from inside server.py's `start` / `stop` / `clear` branches
**Where:** [dragon_voice/server.py:766-770](dragon_voice/server.py#L766)
(`pipeline._audio_buffer.clear()`, `pipeline._dictation_mode = ...`,
`pipeline._segment_buffer.clear()`, `pipeline._dictation_segments.clear()`)
**Smell:** `start` command for dictation reaches into pipeline private
attributes.  `pipeline.set_mode("dictate")` would be the encapsulated
form.
**Concrete fix:** Add `pipeline.start_recording(mode: str)` that
encapsulates the four state mutations.
**Risk:** Trivial.

---

## 3. Recommended next 3 PRs

In priority order, with realistic effort:

### PR-1: Ship Wave 22a — `WsDispatcher` + per-command handlers (SRP-1, SRP-2, SRP-3)
- **Files:** `dragon_voice/server.py` → split into
  `dragon_voice/server.py` (just `VoiceServer` + `create_app` +
  middleware adapters) + `dragon_voice/ws/dispatcher.py` +
  `dragon_voice/ws/handlers/{register,text,config,media,cancel}.py`
  + `dragon_voice/ws/state.py` (typed `ConnState` dataclass).
- **LOC:** ~2,200 LOC moved (no decompose), ~200 net new (handler
  scaffolding).  Three follow-up PRs decompose `_handle_text_body`,
  `_handle_config_update` internals.
- **Risk:** High blast radius; tests touch most of WS surface.  Mitigate
  by behaviour-preserving move (no decompose) in PR-1, validated
  against the live Tab5 in a staging deploy before merge.
- **Closes:** SRP-1 (move) + lays the foundation for SRP-2/3/9/10.

### PR-2: Fix HTTP/WS swap-path divergence (DIP-2)
- **Files:** `dragon_voice/handlers/config_api.py`,
  `tests/test_config_api_handlers.py`.
- **LOC:** ~30 lines changed + ~40 lines of new test.
- **Risk:** Trivial.  This is a real bug — `POST /api/config` leaves
  ConvEngine on stale LLM until the WS path runs.
- **Closes:** DIP-2 + completes Wave 22b's intent.

### PR-3: VoiceMode registry (OCP-1, OCP-2, OCP-6) and the inline pricing kill (OCP-2)
- **Files:** `dragon_voice/voice_modes.py` (new, ~120 LOC),
  `dragon_voice/server.py` (delete 17 `voice_mode == N` sites),
  `dragon_voice/pipeline.py` (mode-aware timeouts), `tests/test_voice_modes.py`.
- **LOC:** ~150 net new, ~250 net deleted from server.py.  Drops 17
  inline mode comparisons + the disconnected vision-pricing switch.
- **Risk:** Medium — covered by `test_missing_key_gamma_arch`,
  `test_tinkerclaw_health_check`, `test_config_update_rate_limit_signal`.
  Add `test_voice_mode_registry` to lock down the mode tuples.
- **Closes:** OCP-1, OCP-2, OCP-6, S20.

---

## 4. Anti-findings (patterns considered + rejected)

These look like SOLID violations from a textbook angle but are correct
or low-risk in this codebase:

### AF-1 — Capability detector functions live in `capability_registry.py` *and* delegate from each backend's `capabilities` property
At first glance this looks like DRY violation (two places mention each
backend's caps).  In fact it's exactly right: the per-backend property
is the public LSP-conforming surface; the registry is the
discoverability + central-audit point.  The delegate pattern
(`detect("ollama", model_id)` from inside `OllamaBackend.capabilities`)
is a one-liner; the alternative (registry-only with no property) would
break LSP for callers that just have an `LLMBackend` reference.

### AF-2 — `*Routes` classes have no instance state but exist as classes
Prior audit (S11) flagged this as "API Routes as classes — should be
plain functions".  Re-evaluating: 11 `*Routes` classes each have 5-15
KB of code organised under one umbrella, and the "class with no state"
is a familiar aiohttp pattern.  Migrating to plain functions would
save ~50 LOC of `__init__` boilerplate at the cost of dispersing a
clean grouping.  Net negative.  Defer indefinitely.

### AF-3 — `dual.py` duplicates `_looks_like_useful_text` from `response_wrap.py`
Looks like duplication.  Comment in `dual.py:46-50` documents the
late-import-cycle constraint.  Now that Wave 21a/21b/22b reshuffled
imports, this *could* be merged — but `response_wrap.py` already lives
in `tools/`, and `dual.py` lives in `llm/`.  An import from llm into
tools is a layering inversion.  The clean fix is the `text_utils.py`
extraction (SRP-9), not "delete the dual.py copy".  Until SRP-9
lands, the duplication is fine.

### AF-4 — `LLMBackend.capabilities` defaults to `{TEXT}` instead of being abstract
LSP-6 above flagged this as a "footgun for new backends".  But (a)
the default is conservative (text-only is the safe baseline for an
unknown backend), and (b) all 6 current backends override.  Making it
abstract forces every test fixture / mock backend to implement it
even when irrelevant.  Net cost > net benefit.

---

## 5. Status of prior-audit findings (delta from 2026-05-02 sweep)

| Prior ID | Status today | Note |
|----------|--------------|------|
| F1 (P0 router cap mismatch) | **CLOSED** | Wave 21a (#205) — `capability_registry.py` is the central audit point.  All 6 backends delegate.  31 unit tests cover declarations. |
| F2 (P0 WS handler regrowth) | **OPEN** | Promoted to SRP-1.  Wave 22a explicitly deferred per audit instructions; reaffirmed as P0. |
| F3 (P1 ConvEngine._llm direct access) | **CLOSED** | Wave 22b (#207) — `ConversationEngine.swap_llm` + `fleet_summary` exist.  Server uses them for the swap + summary paths.  Two read paths still poke `_llm` (ENC-1, ENC-2) — sub-P1 cleanups. |
| F4 (P1 tool parsing tangled) | **OPEN** | Promoted to SRP-5; `parse_tool_calls_with_errors` (γ2-M1) added the error-surfacing variant but no extraction. |
| F5 (P1 hasattr scattered) | **CLOSED** | Wave 21b (#206) — `SupportsUsage`, `SupportsHistoryTrim`, `SupportsClearHistory`, `SupportsSessionKey` Protocols.  9/14 hasattr sites migrated to isinstance.  Remaining 5 are TTS/STT — flagged as LSP-4 (mirror Wave 21b on the audio side). |
| F6 (P1 HTTP/WS swap drift) | **STILL-OPEN** | Promoted to DIP-2.  Now affects the *new* canonical path (Wave 22b's swap_llm) — HTTP path doesn't call it. |
| F7 (P1 capability dict staleness) | **STILL-OPEN** | `_OPENROUTER_CAPS` last sync 2026-04-27 per CLAUDE.md.  Manual maintenance.  Live-fetch follow-up still queued. |
| S1 (server.py god-object) | **REGRESSED** | 2,706 LOC (was 2,719 in prior sweep — slight drop from #199 mypy cleanup, but the structure is unchanged).  Promoted to SRP-1. |
| S2 (pipeline.py multi-responsibility) | **OPEN** | 1,733 LOC (essentially flat).  SRP-4. |
| S7 (TurnGate in SurfaceManager) | **OPEN** | Still embedded in SurfaceManager (50/213 LOC).  Same 4+ callers.  Not promoted; defer until next wave. |
| S13 (hallucination heuristics scattered) | **OPEN** | Promoted to SRP-9.  `response_wrap.py` is the canonical home but the consolidation never landed. |
| S16 (receipt drift) | **OPEN** | SRP-10. |
| S18 (run_startup mutates 20+ attrs) | **OPEN** | SRP-6.  Now mutates 21 attrs (added `_scheduler_mgr`, `_scheduler_store`). |
| S19 (Database monolithic) | **OPEN** | SRP-7. |
| S20 (mode logic scattered) | **OPEN** | OCP-1.  Recommended for PR-3. |
| D-mem | **DEFERRED** | Per brief — defer until 100k+ facts. |
| δ-arch | **DEFERRED** | Per brief — 3-caller threshold not met. |

---

## Appendix A — Measurements (2026-05-03)

| File | LOC | Δ vs prior audit |
|------|-----|------------------|
| `dragon_voice/server.py` | 2,706 | -13 (mypy cleanups) |
| `dragon_voice/pipeline.py` | 1,733 | +12 |
| `dragon_voice/conversation.py` | 656 | +173 (Wave 22b swap_llm + fleet_summary) |
| `dragon_voice/db.py` | 844 | unchanged |
| `dragon_voice/tools/registry.py` | 440 | +1 |
| `dragon_voice/llm/openrouter_llm.py` | 542 | +35 (#187 catalog refresh) |
| `dragon_voice/llm/router.py` | 313 | new in #185 (was 0 in pre-prior audit baseline) |
| `dragon_voice/llm/capability_registry.py` | 177 | new in #205 |
| `dragon_voice/lifecycle/startup.py` | 320 | +12 (scheduler ε1a/ε2 wiring) |
| `dragon_voice/api/scheduler.py` | 264 | new in #130 |
| `dragon_voice/scheduler/store.py` | 528 | +290 (ε2 SqliteNotificationStore) |
| Test suite | ~556 unit + 29 e2e | +47 unit since prior |

WS handler family LOC under `VoiceServer`:
- `_handle_ws_voice` (dispatcher): 525
- `_handle_register`: 521
- `_handle_text` + `_handle_text_body`: 477
- `_handle_config_update`: 437
- `_handle_user_media`: 107
- `_handle_disconnect`: 65
- `_spawn_handler_task`: 80
- `_ws_keepalive_during_inference`: 98
- **Total under VoiceServer: ~2,310 LOC** (out of 2,706 file LOC)
