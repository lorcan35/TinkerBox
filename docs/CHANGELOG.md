# TinkerBox Changelog

Sprint history + dated benchmarks + Phase-completion notes for the
Dragon-server stack.

`CLAUDE.md` is the runbook (loaded into every conversation context) and
intentionally stays compact.  Anything dated, post-mortem, or
sprint-shaped lives here.  For active investigations + open work,
see the "Active Investigations" section at the top of `CLAUDE.md`.

For non-obvious root causes and Python/aiohttp/aiosqlite gotchas, see
[`LEARNINGS.md`](../LEARNINGS.md).  For audit-driven wave programs that
span both repos, see TinkerTab's
[`docs/AUDIT-state-of-stack-2026-05-11.md`](https://github.com/lorcan35/TinkerTab/blob/main/docs/AUDIT-state-of-stack-2026-05-11.md).

---

## Local LLM Benchmarks (Dragon Q6A, ARM64 CPU via Ollama)

**Re-benchmarked 2026-04-25** with the **10-prompt** gauntlet on the
post-#74/#76/#77 server (parser dialect widening + WS-keepalive-during-
inference + per-tool template wrap).  Previous table from 2026-04-24
used 5 prompts and showed 3 viable models; the 10-prompt + integration-
branch results below are dramatically better because two of the three
systemic blockers identified that day (WS keepalive expiry, empty-reply-
on-tool-fire) are now fixed.

The 10 prompts exercise: G1 datetime, G2 calculator (456×789 = 359,784
— deliberately not a memorized number), G3 store_fact, G4 unit_converter,
G5 timesense, G6 weather, G7 web_search, G8 recall_facts, G9 system_info,
G10 quick_poll.  See `docs/historical/AUDIT-WAVE-14.md` "Local-mode gauntlet Round 2 + 3"
for the full per-prompt matrix and the prior 5-prompt baseline.

| Model | Size | Median latency | Correct-tool fires | User-visible replies | Math correct (G2) | Verdict |
|-------|------|----------------|--------------------|----------------------|-------------------|---------|
| **ministral-3:3b** ⭐ | 2.8 GB | 65 s | **7/10** | 5/10 | ✅ 359,784 | **Current default.** Best correct-tool rate; warm conversational replies; only model that fired the real `weather` tool with a useful result. |
| **gemma3:4b** ⭐ | 3.1 GB | 53 s | 6/10 | **7/10** | ✅ tool fired (wrap render gap on G2) | **First alternative.** Best visible-reply rate.  CLAUDE.md previously wrote it off as "OK format, bad answers" — provably wrong now that #77's wrap renders the tool result for it. |
| xLAM-2-1b-fc-r (HF, GGUF) | 1.3 GB | **24 s** | 3/10 | 5/10 | ❌ picked web_search for math | Fastest by 2×.  Half the prompts work cleanly; other half wrong-tool selection (web_search for math), a fourth XML dialect (`[recall query=…]`) the parser doesn't catch, or an honest refusal.  Useful as a tool-picker head in a future dual-model pipeline; bad as a standalone default. |
| qwen3:1.7b | 1.4 GB | 50 s | 0/10 | 5/10 | n/a (ws-reset on G2) | Talks well, never uses tools.  Honest refusals on weather/sysinfo are a feature, not a bug.  OK for chat-only turns; useless for agentic ones. |
| llama3.2:3b | 1.9 GB | 38 s | 1/10 | 4/10 | ✅ 359784 (when it didn't ws-reset) | **Most flaky** — 5/10 connection resets.  When it doesn't reset, math is correct.  Wait for #75-style resilience fixes before using. |
| phi4-mini:latest | 2.5 GB | 92 s | 1/10 | 5/10 | n/a (ws-reset on G2) | Slow AND fakes most tool acks ("Got it" without firing `store_fact`).  Worst combination. |
| qwen2.5:3b (HF) | ~2 GB | 62 s | 1/5 | 1/5 | ❌ 356,184 | (5-prompt baseline only) Fluent, occasionally honest, arithmetic wrong, leaks `[datetime]` templates. |
| hermes3:3b (HF) | 2.0 GB | 52 s | 0/5 | 0 | ❌ 356,664 | (5-prompt baseline only) Talks well, tools zero. |
| qwen3:0.6b (old default) | 0.5 GB | 18 s | 1/5 | 1/5 | ❌ silent | (5-prompt baseline only) Fastest, but emits malformed XML and goes silent on most tool prompts. |
| qwen3:4b | 2.5 GB | 92 s | 0/10 ⚠️ | 0/10 ⚠️ | n/a (no content) | **Phase-1a fixed the connection** — 92 s × 10 prompts no longer triggers P13 eviction.  But the model itself produces no usable content; can't be fixed server-side. |
| qwen3.5:4b | 3.2 GB | 95 s | 0/10 ⚠️ | 0/10 ⚠️ | — | Same as qwen3:4b — keepalive holds, content empty. |
| nemotron-3-nano:4b | 2.6 GB | 92 s | 1/10 ⚠️ | 0/10 ⚠️ | — | Same shape; one tool fired, no visible reply. |
| distil-home-assistant-functiongemma | 2.4 GB | 81 s | **0/10** | **0/10** | ❌ never finished calc | **Worst tested model.**  Despite the "FunctionGemma" branding, emits zero tool markers and dumps raw chain-of-thought fragments truncated mid-thought (`"Let me calculate that. First, I need to multiply 456 by 789. Hmm, that's a big number. Maybe I can use the…"`).  No facts stored, no math answered, no tools fired across all 10 prompts.  `mem_facts_added_last_3min=0` confirms G3 fact-store didn't reach the DB either.  Failure class 3 (fluent hallucinator) at its worst.  Reject. |

⚠️ "0/10 user-visible" on the 4B-class block is post-#76 keepalive — pre-fix
they were 0/5 with P13 eviction errors.  The connection now stays open the
full 92 s, which is the entire point of #76; the model not producing useful
content is upstream of any server fix.

**Three failure classes observed:**
1. **Too small to tool-call** — qwen3 0.6b/1.7b emit malformed XML or give up.
   #74's parser widening helped some FC-trained models, but qwen3-base is
   still in this bucket.
2. **Too slow for keepalive** — 4B-class models take 92 s+ per turn.  #76
   solved the connection-drop part; the model latency itself is the next
   problem (see "When to use what" → NPU path below).
3. **Fluent hallucinators** — llama3.2:3b / hermes3:3b / phi4-mini write
   confident chatty answers that never actually invoke a tool.  Dangerous
   because replies look right at a glance (wrong math, fake timers, recalled
   "memories" that were never stored).  #77's wrap can't help this class
   because there's no real tool result to wrap.

**Current default:** `ministral-3:3b` — set in `dragon_voice/config.yaml`
(`ollama_model: "ministral-3:3b"`).  Post-#74/#76/#77 it scores **7/10
correct-tool fires + 5/10 visible replies** on the 10-prompt gauntlet —
roughly 2× the baseline (3/5 + 3/5 on the 5-prompt 2026-04-24 audit).  Math
correct on G2 (359,784 — the deliberately-not-memorized test).  The known
gap is widget emission (G5 timesense, G10 quick_poll) — works on zero
tested models because of a system-prompt / tool-format mismatch upstream
of model choice.

**When to use what:**
- **Default voice + chat** → `ministral-3:3b`.  Best balance of tool fires
  (7/10), visible replies (5/10), and conversational warmth.
- **Higher reply-rate, slightly slower** → `gemma3:4b`.  7/10 visible
  replies, 1 GB more RAM, 12 s slower per turn.  Worth A/B against ministral
  in real user sessions.
- **Sub-second tool selection (dual-model pipeline, opt-in)** → `xLAM-2-1b-fc-r`
  picks tools fast (24 s median) but doesn't write conversational replies.
  Pair with a small responder model to combine strengths.  Not a default
  candidate alone.  PR #80 ships `backend: "dual"` for this — works
  end-to-end on individual turns but **fails the sustained-gauntlet
  validation gate on Dragon Q6A (11 GB RAM)** because xLAM + ministral
  exceeds practical headroom and Ollama evicts the LRU model under
  pressure.  See `docs/PLAN-dual-model-pipeline.md` "Validation results"
  + LEARNINGS #80 for the matrix.  Default stays single-model on Dragon;
  dual is recommended for ≥ 16 GB hardware only.
- **Agentic chains where reliability matters** → mode 2 (Cloud, OpenRouter
  model picked per `llm_model`) or mode 3 (TinkerClaw Gateway).  Local mode
  with #74/#76/#77 is now usable, but cloud is still better for long chains.
- **Voice latency-critical turns** → the NPU Genie path (`docs/npu-setup.md`)
  is the real escape hatch; until it lands, Local mode is inherently second-
  best on accuracy and third-best on speed behind mode 2/3.

## Current Sprint: Complete (April 2026)

**Phase 0 (Foundation) and Phase 1 (Voice Features) are both complete.** The agentic sprint (tool-calling, memory, documents) is also done. Dragon is a fully functional API-first voice assistant server with agentic capabilities.

### Issues
| # | Title | Status |
|---|-------|--------|
| #16 | Session management infrastructure | DONE (sessions.py, db.py) |
| #17 | Multi-turn conversation engine | DONE (conversation.py, messages.py) |
| #18 | Unified voice + text input | DONE (server.py handles both voice and text) |
| #21 | REST API framework | DONE (api/ package, 53 endpoints — see header) |
| #19 | Notes feature | DONE (notes/ module wired into server.py, API routes registered) |
| — | Cloud mode (OpenRouter STT+TTS) | DONE (openrouter_stt.py, openrouter_tts.py, config_update WS command) |
| — | Dictation mode + post-processing | DONE (dictation in pipeline.py, auto-generated title/summary) |
| #20 | Tab5 SD card storage | DONE (SDMMC 4-bit, FAT32, coexists with WiFi SDIO, notes.js + WAV recordings) |
| #22 | Dashboard conversation viewer | DONE (11-tab SPA: Overview, Conversations, Chat, Devices, Notes, Logs, Memory, Documents, Tools, OTA, Debug) |
| — | Agentic pipeline (tool-calling) | DONE (ToolRegistry, XML parsing, web_search, remember, recall, datetime) |
| — | Memory + RAG | DONE (MemoryService, facts CRUD, document ingestion, semantic search) |
| — | E2E test suite | DONE (55 tests via Debug tab + 29 API tests) |
| — | Settings crash fix (WDT) | DONE (f_getfree cached at boot, esp_task_wdt_reset fed between settings sections) |
| — | Tolerant tool parser | DONE (handles stray `>`, missing `</args>`, small model XML quirks) |
| — | Response timeout (local mode) | DONE (disabled/5 min for local mode, 35s for cloud mode) |
| — | Default local LLM | DONE (ministral-3:3b, ~65 s median, 7/10 correct-tool fires post-#74/#76/#77) — switched from qwen3:0.6b on 2026-04-24 after the 11-model re-benchmark, then upgraded again on 2026-04-25 with the 10-prompt gauntlet on the integration branch.  See Local LLM Benchmarks section + `docs/historical/AUDIT-WAVE-14.md` "Local-mode gauntlet Round 2 + 3". |
| — | Rich Media Chat | DONE (MediaPipeline renders code/tables/images as JPEG, MediaStore with 24h cleanup, camera uploads, 44 tests) |

### Schema
See `schema.sql` — **11 tables**: 6 foundation (devices, sessions, messages, notes, events, config), 3 memory (memory_facts, memory_documents, memory_chunks), and 2 scheduler (scheduled_notifications, notification_queue).

### Acceptance Tests (must pass before features)
- Create session → send 5 messages → retrieve full history
- List devices → see which are online
- Hot-swap LLM backend mid-session
- Paginate through old sessions via REST API
- Dashboard shows live conversation via WebSocket events
