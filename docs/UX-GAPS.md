# UX-Gap Audit — Sequenced Remediation Plan

**Audit date:** 2026-04-25
**Master tracking issue:** [#89](https://github.com/lorcan35/TinkerBox/issues/89)
**Source of truth:** this doc.  Status column updated by every PR that touches a gap.

---

## Why this exists

Three back-to-back deep-dive audits (one cross-cutting + four parallel cluster
deep-dives covering Dragon + Tab5 firmware) verified **21 distinct UX gaps**
plus **4 cross-cutting architectural patterns**.  This doc is the canonical
landscape: what's broken, where in the code, what fix shape, what phase it
belongs to.  GitHub issue [#89](https://github.com/lorcan35/TinkerBox/issues/89)
mirrors the same checklist for live progress.

If you land here cold (post-context-compaction or as a new contributor): start
with the [verdict matrix](#verdict-matrix) and the [phased plan](#phased-plan)
below.  Per-gap detail is at the bottom.

---

## Working principles (apply to every fix-PR)

Per user direction:

- **E2E like a real user** — bench against the live Dragon (port 3502) or sandbox
  (3513), not just unit tests
- **Read the code first** — never assume; cite file:line in PR descriptions
- **Stability check before declaring done** — long-uptime soak / repeated runs
  where applicable
- **Architectural understanding** documented in the PR description
- **One concern per PR** ([CLAUDE.md workflow](../CLAUDE.md))

---

## Verdict matrix

| ID | Gap | Sev | Effort | Phase | Status |
|---|---|---|---|---|---|
| C1 | Text-mode interruption broken | CRITICAL | ~3 h | 1 | **MERGED #92** |
| C2 | Multimodal interruption same blocking pattern | CRITICAL | +1 h | 1 | **MERGED #92** |
| L2 | `config_update` blocks WS read loop (~13 s worst case) | HIGH (was LOW) | ~2.5 h | 1 | **MERGED #92** |
| C3 | Config-swap race during inference | non-issue | 15 min docs | 1 | **MERGED #92** (doc note) |
| L4 | Cancel ack — late `llm` events arrive after Tab5 went READY | LOW | ~30 min | 1 | **MERGED #92 + TinkerTab#194** |
| H1 | Tokens buffered for full LLM phase during tool turns | HIGH | ~90 min | 2 | **MERGED #97** |
| H2 | TTS sentence-boundary delay (incl. code-block false splits) | HIGH | ~110 min | 2 | **MERGED #99** |
| H4 | Dictation post-process silent (no event during 10-20 s wait) | HIGH | ~95 min | 2 | **MERGED #95 + TinkerTab#195** |
| L3 | Piper TTS not killed on timeout | LOW | ~35 min | 2 | **MERGED #99** |
| H8 | Raw error strings on wrong UI surface (voice caption vs toast) | HIGH | ~4 h | 3 | **MERGED #105 + #107 + #109 + TinkerTab#197** |
| M1 | Tool parser silent on malformed JSON args | MED | ~3 h | 3 | **MERGED #105** |
| M2 | WS upgrade rejection plain text (401/503) | MED | ~3.5 h | 3 | OPEN |
| M5 | Device-id collision silent eviction | MED | ~3 h | 3 | **MERGED #109 + TinkerTab#197** |
| M6 | TC gateway down → 600 s timeout, no fast-fail | MED | ~4 h | 3 | **MERGED #107** |
| H6 | Idle paused sessions never purge | HIGH | ~2-3 h | 4 | OPEN |
| H7 | Media cleanup 1 h delay before first run | HIGH | ~1 h | 4 | OPEN |
| D-mem | Memory facts unbounded (downgraded — sqlite-vec confirmed loaded) | LOW (was MED) | ~1 h | 4 | OPEN |
| D-docs | Document ingest no size cap | MED | ~1 h | 4 | OPEN |
| L1 | POST 413 returns `Connection: close` | LOW | 10 min | 6 | OPEN |
| F-T1 | Scheduler tier 1 (in-process) | DESIGN | ~7 h | 5 | OPEN |
| F-T2 | Scheduler tier 2 (durable + offline queue) | DESIGN | +6 h | 5 | OPEN |

**Architectural patterns (collapse multiple gaps into single abstractions):**

| ID | Pattern | Replaces / improves | Phase | Status |
|---|---|---|---|---|
| α-arch | WS dispatcher async-task discipline | C1, C2, L2, parts of M5 | 1 | **MERGED #92** |
| β-arch | Progress event bus (single channel for all phases) | H1, H2, H4, future tool/TTS feedback | 6 | OPEN |
| γ-arch | `DragonError` taxonomy (severity + scope) | H8, M1, M2, M5, M6 | 3 | **γ1 + γ2 MERGED (#102, #105, #107, #109, TinkerTab#197)** (γ3 OPEN) |
| δ-arch | Declarative retention policy framework | H6, H7, D-mem, D-docs | 6 | OPEN |

---

## Phased plan

Ordering by impact / effort / dependency / blast radius.

### Phase 1 — WS dispatcher discipline (single PR, 4 gaps + Tab5 follow-up)

**Why first:** unblocks C1+C2+L2 simultaneously.  Pure server-side core.
Restores the "stop means stop" trust contract.

**PR α (Dragon):**
1. `_handle_text` becomes a tracked task (`conn_state["text_task"]`)
2. `_handle_user_media` same
3. `config_update` body extracted to `_handle_config_update` (refactor) and tasked
4. Cancel handler cancels active tasks + sends `cancel_ack` + kills mid-TTS Piper
5. `_handle_disconnect` cleans up the new task slots
6. New `tests/test_ws_dispatcher_cancellation.py` — current handler has zero direct tests

**PR α-Tab5:** `voice.c:752` — ignore `llm` events when state is READY/IDLE
(prevents late-token re-entry into PROCESSING after a successful cancel).

**Expected effort:** 6-8 h Dragon + 30 min Tab5.

**E2E acceptance:**
- Send a slow text turn, hit cancel mid-LLM → response stops within 1 s
- Same for vision turn
- Mode swap during text turn → swap waits for text to finish (preserved
  semantics) but WS read loop stays responsive to ping/cancel
- Tab5 cancel → orb drops to READY, late tokens don't pull it back

### Phase 2 — Streaming feedback during long ops (4 small independent PRs)

**Why second:** these are the most "device feels alive" wins.  Can run in parallel.

| PR | Scope | Effort |
|---|---|---|
| β1 | H4 dictation post-process events (`dictation_postprocessing` / `_error` / `_cancelled`) | ~95 min Dragon + ~30 min Tab5 |
| β2 | H1 incremental tool-marker detection (replace binary buffer gate) | ~90 min |
| β3 | H2 TTS timeout-flush + code-block guard (bundle with L3 Piper-kill since same file) | ~110 min + 35 min |
| — | architectural progress-bus deferred to Phase 6 | — |

**E2E acceptance:**
- Tool-calling turn → tokens stream during the LLM phase (no 60-90 s silent gap)
- Dictation save → user sees "Generating summary…" toast within 1 s of stopping speech
- Long reply with no early punctuation → first audio plays within ~500 ms
- TTS timeout doesn't leave a Piper subprocess running

### Phase 3 — Error visibility refactor (depends on Phase 1)

**Why third:** has architectural gravity (transient/fatal taxonomy) that
affects the whole protocol.  Cleaner if dispatcher cleanup landed first.

| PR | Scope | Effort |
|---|---|---|
| γ1 | `DragonError` class with `severity: TRANSIENT|FATAL` + `scope`; audit ~12 emission sites | **MERGED #102** |
| γ2 | H8 route to correct surface (toast vs caption); M1 `tool_args_invalid` frame; M5 `device_evicted` frame; M6 fast-fail TC health check | **MERGED #105 + #107 + #109 + TinkerTab#197** |
| γ3 | M2 WS upgrade JSON responses + Tab5 401-stops-retrying | ~3 h |

**E2E acceptance:**
- `[Ollama timeout after 300s]` no longer appears in voice overlay caption;
  becomes a friendly toast
- Wrong bearer token → Tab5 shows "Invalid Dragon token, check Settings" toast
  and STOPS retrying instead of looping forever
- Device-id collision → evicted device sees a clear "Another device claimed
  your session" message
- TC gateway down → first request fails in 5 s with friendly message + suggested
  fallback, not 600 s timeout

### Phase 4 — Long-uptime hygiene (low risk, easy wins)

**Why fourth:** invisible until device has been up for weeks.  No risk-bearing
PRs.

| PR | Scope | Effort |
|---|---|---|
| δ1 | H7 media cleanup runs immediately on startup | ~1 h |
| δ2 | H6 paused-session retention policy (`paused_session_retention_days`) | ~2-3 h |
| δ3 | D-docs document size cap; D-mem fact cap (low-priority since sqlite-vec confirmed loaded) | ~2 h |

**E2E acceptance:**
- Soak Dragon for ~1 hour with media uploads on startup → `/home/radxa/media`
  doesn't grow past 500 MB
- Paused session > N days old → messages auto-purge

### Phase 5 — Async push (scheduler design)

**Why fifth:** new capability surface.  Tier-1 is scaffolding for every future
"skill that fires later" (timer, reminder, deploy notifier, calendar, weather alert).

| PR | Scope | Effort |
|---|---|---|
| ε-design | RFC: scheduler architecture + Tab5 notification UI surface decision | ~2 h |
| ε1 | F-T1 in-process scheduler (asyncio task + REST API + ScheduleReminderTool + WS `notification` message) | ~4 h Dragon + ~3 h Tab5 |
| ε2 | F-T2 SQLite-backed durable + offline queue + boot replay | ~6 h Dragon |

**E2E acceptance:**
- "Remind me in 5 minutes" → 5 minutes later a card appears in chat with
  dismiss button, audio chime if appropriate
- Dragon restart mid-schedule (T2) → notification still fires after restart
- Tab5 offline at fire time (T2) → queued, replayed on reconnect

**Tab5 UI surface decision (locked-in by ε-design):** notifications land as
`widget_card` inline in the chat stream (existing renderer at voice.c:1098 +
chat_msg_view.c).  No new UI primitive needed.  Toast for urgent items is
v1.1 polish — `ui_home_show_toast()` already exists at [TinkerTab main/ui_home.c:1700](https://github.com/lorcan35/TinkerTab/blob/main/main/ui_home.c#L1700).

### Phase 6 — Architectural follow-ups

After individual fixes ship, collapse ad-hoc patterns into single abstractions.

| PR | Scope | Effort |
|---|---|---|
| β-arch | Progress event bus — single `progress` channel replaces ad-hoc events | ~3-4 h |
| δ-arch | Declarative retention policy framework — single loop, config-driven | ~2-3 h |
| L1 | POST 413 `Connection: close` cleanup | 10 min (bundle anywhere) |

---

## Verified context (the unknowns we resolved up front)

Investigated before locking the plan, so no work is gated on assumptions:

| Unknown | Resolution | Implication |
|---|---|---|
| Tab5 firmware velocity | Incremental build = 17.5 s; +30 s flash; recent cadence 5 PRs / 27 h | Not a bottleneck.  Tab5 PRs move at Dragon pace. |
| Tab5 toast UI primitive | `ui_home_show_toast()` exists, used 5+ places | No new UI primitive needed for Phase 3 + 5. -2 h Tab5 work. |
| Tab5 SDL2 simulator | `sim/tinkeros_sim` builds via `sim/Makefile` | UI changes testable on desktop without flashing. Big iteration win. |
| `sqlite_vec` loaded on Dragon? | Yes — `0.1.9`, `memory.py:75` loads extension | Memory search is O(log N) fast path.  D-mem severity DROPPED to LOW. |
| `_handle_text` direct test coverage | **Zero** | Phase 1 PR adds test scaffolding (~2-3 h additional). |

Residual unknowns (require prototyping, not investigation):

- TTS retry/timeout interaction with Phase 6 progress-bus
- Scheduler recurring-event abstraction (cron syntax vs declarative recurrence)
- Error rate-limiting / coalesce — defer until Phase 3 telemetry shows real rates

---

## Per-gap detail

Format: ID | code citation | what's actually wrong | fix shape | acceptance.

For deeper analysis incl. edge cases + Tab5-side trace + hardware constraints,
see the audit transcripts in `/tmp/claude-1000/-home-rebelforce/<session>/tasks/`
(ephemeral) or the discussion in [#89](https://github.com/lorcan35/TinkerBox/issues/89).

### C1 — Text-mode interruption broken
- **Cite:** `dragon_voice/server.py:822-824`
- **What:** `await self._handle_text(...)` is awaited inline in WS read loop.
  While it's pending (60-90 s on local), the loop can't read the next frame.
  Cancel sits in TCP buffer.  Dragon completes the turn, then reads cancel,
  then no-ops it.  User's "cancelled" response materializes anyway.
- **Fix:** Spawn handler as task tracked in `conn_state["text_task"]`.  Cancel
  handler cancels it.
- **Tab5-side change needed:** `voice.c:752` should ignore `llm` events when
  state is already READY/IDLE so late tokens don't re-enter PROCESSING.

### C2 — Multimodal interruption same pattern
- **Cite:** `dragon_voice/server.py:826-828` + `_handle_user_media` body 1886-1954
- **What:** Identical inline-await blocking as C1.  Vision turns can take 30+ s.
- **Fix:** Same shape — spawn as `conn_state["media_task"]`.
- **Edge:** the `to_thread(_read_and_encode, image_path)` at server.py:1915 is
  uninterruptible (ThreadPool doesn't propagate cancel).  Acceptable — image
  read is ~80 ms for an 8 MB JPEG.

### L2 — `config_update` blocks WS read loop
- **Cite:** `dragon_voice/server.py:838-1100`
- **What:** Massive (~250 LOC) handler awaited inline.  TC health check (5 s)
  + Ollama swap (5 s) + ConvEngine LLM swap (3 s) = up to ~13 s of unresponsive
  device on every mode toggle.
- **Fix:** Extract handler body into `_handle_config_update`; spawn as task
  tracked in `conn_state["config_task"]`.  Keep `conn_lock` semantics (text
  turn finishes before swap runs).

### C3 — Config-swap race (audit was wrong direction)
- **Cite:** `dragon_voice/server.py:989` + `1046`
- **What audit claimed:** Mode swap mid-inference can corrupt the response.
- **Reality:** swap acquires `conn_lock`; text turns hold `conn_lock`; swap
  WAITS for text to finish, never races it.  Swap explicitly updates
  `_conversation._llm = new_llm` at line 1046.
- **Fix:** Doc-only note explaining the serialization.  No code change.

### L4 — Cancel ack
- **Cite:** `dragon_voice/server.py:816-820`
- **What:** No ack frame on cancel.  Today: invisible because Tab5 transitions
  locally.  AFTER C1: stale `llm` tokens may arrive after Tab5 went READY,
  voice.c:752 unconditionally re-enters PROCESSING.
- **Fix (chosen path B):** Tab5-side — `voice.c:752` should ignore `llm` events
  when state is already READY/IDLE.  ~5 LOC, no server change needed.
  Rejected path A (server-side `cancel_ack`) since it's protocol bloat for the
  same observable behavior.

### H1 — Tokens buffered during tool detection
- **Cite:** `dragon_voice/conversation.py:258-266`
- **What:** When `tool_registry` is set, `yield token` is gated.  All tokens
  accumulate in `full_response` until the entire LLM output is ready.  For a
  60-90 s turn, Tab5 sees nothing during the LLM phase.
- **Comparison:** TC bypass at `server.py:1537-1542` does NOT buffer because
  it skips ConvEngine's tool-detection logic.
- **Fix:** Replace binary buffer gate with **incremental marker detection** —
  flush tokens as they arrive, watch the rolling buffer for `<tool>` /
  `<tool_call>` / `[NAME]{` opening markers.  Only buffer when a marker is in
  flight.

### H2 — TTS sentence-boundary delay
- **Cite:** `dragon_voice/pipeline.py:721-748`
- **What:** Buffers tokens until sentence boundary OR clause boundary
  (20+ chars local, 60+ chars cloud).  Worst case: replies with no punctuation
  hold 1.5+ s of audio.
- **Edge cases the audit found:**
  - Code blocks with `;` / `:` trigger false sentence breaks
  - Abbreviations (`Dr.`, `$19.95`) trigger false sentence ends
  - Quoted text with periods splits mid-quote
- **Fix:** 300 ms timeout-based flush + code-block detection.  Cloud mode
  unaffected (already responsive enough).

### H4 — Dictation post-process silent
- **Cite:** `dragon_voice/pipeline.py:478-530`
- **What:** Post-process spawned via `asyncio.ensure_future`, no event sent
  during the 10-20 s LLM call for title/summary generation.  Tab5 sees nothing
  between the `stt` event and the `dictation_summary` event.
- **Fix:** Three new events — `dictation_postprocessing` (immediately after
  `stt`), `dictation_postprocessing_error` (on LLM failure),
  `dictation_postprocessing_cancelled` (when new dictation supersedes).

### L3 — Piper TTS not killed on timeout
- **Cite:** `dragon_voice/pipeline.py:956-979`
- **What:** Except branch handles fallback for openrouter but doesn't call
  `kill_active_procs()`.  Real path that matters is **OpenRouter→Piper
  fallback**, not standalone Piper (which has its own timeout protection).
- **Fix:** Add `kill_active_procs()` call in the except block.  Bundle with
  PR β3.

### H8 — Raw errors on wrong UI surface
- **Cite:** `dragon_voice/pipeline.py:366-369, 594, 1953`; `dragon_voice/server.py:1481-1482`
- **What:** Errors emitted as `{"type":"error","message":"raw_string"}`.  Tab5
  ([voice.c:754-764](TinkerTab/main/voice.c#L754-L764)) puts message into
  voice-state caption buffer.  User sees implementation strings like
  "[Ollama timeout after 300s]" or "list index out of range" in the voice
  overlay caption.
- **Fix:** Part of γ-arch.  Define `DragonError(message, code, severity, scope,
  user_friendly)`.  Tab5 routes by severity:
  - `TRANSIENT` → `ui_home_show_toast()` (non-blocking, dismissible)
  - `FATAL` → caption + retry banner

### M1 — Tool parser silent on malformed JSON
- **Cite:** `dragon_voice/tools/registry.py:142-147` (and 162-167, 221-227)
- **What:** Parse failure logged as warning, no event emitted.  LLM continues
  without the tool firing.  User gets a generic response (or empty) with no
  signal that something was attempted.
- **Fix:** Emit `tool_failed` (or extend `tool_call` with `error` field) when
  a parse attempt fails.  Tab5 shows a transient toast.

### M2 — WS upgrade rejection plain text
- **Cite:** `dragon_voice/server.py:540-553`
- **What:** 401/503 returns `web.Response(text="Unauthorized", status=401)`,
  not JSON.  Tab5's `esp_websocket_client` doesn't surface the status to
  application layer cleanly → Tab5 retries 401 forever (battery drain on
  wrong-token devices).
- **Fix:** Return `web.json_response({"error":"Unauthorized","code":"auth_failed"}, status=401)`.
  Tab5-side: handle the status code, stop retrying after 3 auth failures, show
  toast pointing to Settings.

### M5 — Device-id collision silent eviction
- **Cite:** `dragon_voice/server.py:1220-1251`
- **What:** P13 evicts old WS connection when new one with same device_id
  registers.  Old client just sees TCP close, no `error` frame.
- **Fix:** Send `{"type":"error","code":"device_evicted",...}` to old WS
  before close.  Tab5 shows transient toast, doesn't auto-reconnect (would
  cause eviction loop).

### M6 — TC gateway down → 600 s timeout
- **Cite:** `dragon_voice/llm/tinkerclaw_llm.py:133-140` (init) + 184-204
  (request)
- **What:** Init logs warning when gateway down.  First request blocks for the
  full 600 s timeout.  No fast-fail.
- **Fix:** Pre-request 5 s health check with cached result (30 s TTL).  Fast
  fail on health-check failure with `config_update` suggesting fallback.

### H6 — Idle paused sessions never purge
- **Cite:** `dragon_voice/db.py:573-575`
- **What:** Purge query has `WHERE session_id NOT IN (SELECT id FROM sessions
  WHERE status IN ('active', 'paused'))`.  Sessions in `paused` state stay
  protected indefinitely.  Motion-sensor wakeups every 20 min keep
  `last_active_at` perpetually fresh, so cleanup loop never auto-ends them.
- **Fix:** New `paused_session_retention_days` config (default 30).
  Sessions in `paused` state with `last_active_at` older than this get
  auto-ended by the cleanup loop.  Then their messages purge normally.

### H7 — Media cleanup waits 1 h before first run
- **Cite:** `dragon_voice/lifecycle/purge.py:37-44`
- **What:** `await asyncio.sleep(3600)` BEFORE first cleanup.  Media uploads
  in the first hour after boot can pile up (esp. after a crash that left
  orphans).
- **Fix:** Run cleanup once on startup, then enter the 1 h periodic loop.
  ~5 LOC.

### D-mem — Memory facts unbounded
- **Cite:** `dragon_voice/memory.py:233-258`
- **Severity downgrade:** Audit feared O(N) Python fallback for cosine
  similarity.  Verified `sqlite_vec 0.1.9` IS loaded on Dragon
  (`memory.py:73-75`), so the fast O(log N) ANN path is active.
- **Remaining concern:** No cap on fact count.  At 100k facts, table size is
  ~300 MB.  Bench: search remains <50 ms.
- **Fix:** Optional per-session cap (e.g. 50 000 facts), default off.  Low
  priority.

### D-docs — Document ingest no size cap
- **Cite:** `dragon_voice/memory.py:363-390`
- **What:** No size check at ingest.  500 MB doc would lock the HTTP handler
  for ~40 minutes embedding chunks.
- **Fix:** 10 MB cap at ingest entry; reject with 413 + user-friendly message.

### L1 — POST 413 returns `Connection: close`
- **Cite:** `dragon_voice/api/media_routes.py:117`
- **What:** Aggressive `Connection: close` header.  No real impact today
  (Tab5 doesn't pipeline) but messy HTTP semantics.
- **Fix:** Remove the header.  10 min.

### F-T1 — Scheduler tier 1 (in-process)
- **What:** `dragon_voice/scheduler/manager.py` with asyncio task per
  scheduled notification.  `ScheduleReminderTool` LLM tool.  REST API at
  `/api/v1/scheduler/*`.  New WS `notification` message type rendered as
  `widget_card` in chat (existing primitives).
- **Notification surface decision:** chat-inline `widget_card` (re-uses
  existing handler at voice.c:1098 + chat_msg_view.c).  Toast (urgent items)
  v1.1 polish.
- **Limitation:** Lost on Dragon restart.  Tab5 offline = lost.  Foundation
  for Tier 2.

### F-T2 — Scheduler tier 2 (durable + offline queue)
- **What:** SQLite-backed `scheduled_notifications` + `notification_queue`
  tables.  Boot replay for unfired-but-due.  Per-session offline queue
  replayed on reconnect.
- **Risks:** Replay-spam cap (max 50 queued, 100 ms pacing).  Recurring
  runaway cap (max 100 open per session).
- **Out of scope (Tier 3):** LAN magic packet / BLE proximity wake.

---

## How to update this doc

When a PR closes a gap:
1. Flip the row's `Status` column from `OPEN` → `IN PROGRESS` → `MERGED`
2. Add the PR number next to the merged row
3. Tick the corresponding checkbox in [#89](https://github.com/lorcan35/TinkerBox/issues/89)

When a new gap is discovered:
1. Add a new row in the [verdict matrix](#verdict-matrix) with severity + phase
2. Add per-gap detail at the bottom
3. Update [#89](https://github.com/lorcan35/TinkerBox/issues/89) checklist

---

## See also

- [#89](https://github.com/lorcan35/TinkerBox/issues/89) — live tracking issue
- [`CLAUDE.md`](../CLAUDE.md) — repo overview, has "Active investigations" pointer to this doc
- [TinkerTab `CLAUDE.md`](https://github.com/lorcan35/TinkerTab/blob/main/CLAUDE.md) — Tab5 firmware overview (build/flash, voice flow, NVS keys)
- [`docs/protocol.md`](./protocol.md) — WS protocol contract between Dragon and Tab5
- [`LEARNINGS.md`](../LEARNINGS.md) — institutional knowledge entries (read before similar work)
