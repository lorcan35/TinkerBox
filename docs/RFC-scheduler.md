# RFC: Phase 5 Scheduler — Async Push Surface for Dragon

**Status:** Approved for execution — this RFC IS PR1 (ε-design).
**Issue lineage:** [`docs/UX-GAPS.md`](UX-GAPS.md) Phase 5; F-T1 / F-T2 detail at lines 393-410; UI surface lock-in at lines 175-178.
**Date locked:** 2026-04-26.

This document is the executable design for the three follow-up PRs (ε1a, ε1b, ε2). When in doubt during implementation, read here first; if the code disagrees with the RFC, fix the code unless the RFC was wrong (in which case amend it in a follow-up PR with the correction reasoning recorded).

---

## Section A — Decisions locked in

For each of the eleven hard parts identified during planning, the call + 2-3 sentence justification.

### A1. Time semantics — relative vs absolute, recurring vs one-shot, time zones

**Decision:** Tier 1 supports BOTH relative (`"5m"`, `"2h30m"`) AND absolute (ISO 8601 timestamps with explicit timezone, e.g. `"2026-04-25T15:00:00-04:00"`). **No recurring in Tier 1.** Recurring lands in Tier 2 alongside SQLite — because durable storage is the prerequisite for handling "every weekday at 9am" sanely (an in-memory recurring job that survives a single boot is a lie we shouldn't tell).

Server clock is authoritative. The Dragon runs Linux + NTP (`CLAUDE.md` confirms ARM64 Ubuntu); Tab5's clock is iffy until it gets a timestamp from Dragon over the WS. All time math happens on Dragon, period — Tab5 just renders strings.

**Time-zone policy:** scheduled jobs store an absolute UTC `fire_at` epoch float (matches the existing `last_active_at REAL` convention in `schema.sql`). For the LLM-facing parser, "at 3pm" without a TZ resolves to **the Dragon process's local TZ** (`time.localtime()`). That's defensible because Dragon is single-user, in one physical room, and the LLM has no other clock to anchor to. We surface the resolved TZ in the tool's return value (`"fires_at_local": "3:00 PM EDT"`) so the LLM can confirm with the user when "at 3pm" resolved to something the user didn't expect.

### A2. Audio chime if appropriate

**Decision:** **No chime in Tier 1.** It's v1.1 polish. The audit says "if appropriate" — that hedge is doing real work. The audit's E2E acceptance phrase ("audio chime if appropriate") is aspirational, not a hard requirement.

Reasoning: each candidate path is bad in a different way:
- TTS-generated chime: 60-90 s on local mode (CLAUDE.md benchmarks). Worse than no chime.
- Pre-encoded chime on Tab5 + new field on `widget_card`: requires Tab5 firmware change in ε1. The audit explicitly says "ZERO Tab5 firmware changes needed for the basic notification render" — adding a chime field violates that constraint.
- Skip in Tier 1: free, and the visible widget_card already gets the user's attention.

When we revisit in v1.1: add `chime: bool` to the `widget_card` schema (additive, backward-compat — existing Tab5 ignores unknown fields per WIDGETS.md design rule #6) and ship a 200 ms tone sample baked into Tab5 firmware. That's a 3-line Tab5 PR + 1-line Dragon PR done independently.

### A3. Scope of "session" for scheduled jobs

**Decision:** **Device-scoped storage, session-scoped delivery preference.**

Resolution: store `device_id` on the scheduled job (NOT `session_id`). When the job fires:
1. Look up the device's currently-active session via `SessionManager` (one device ↔ at most one active session at a time per the architecture decisions in CLAUDE.md).
2. If active: deliver the `widget_card` to that session's WS via `SurfaceManager.surface_for(active_session_id, "scheduler")`.
3. If no active session OR device offline: in Tier 1, log + drop. In Tier 2, queue against `device_id` for replay on next register.

Why device, not session: sessions can rotate (paused → ended → new active session created on next wake). A reminder set Tuesday for Friday must outlive whatever session was active Tuesday. The TinkerBox concept is "device-first, single-user-per-device" (CLAUDE.md "Architecture Decisions"); reminders are user-facing artifacts, so they belong to the user's device — not to a transient session.

What about LLM-facing thread context? The LLM that fires the tool is in a session at the time of scheduling; we record the *originating* session_id as metadata on the job (for "you set this in your conversation about X" UX in v2), but the *delivery target* is the device's active session at fire time.

### A4. REST API shape for `/api/v1/scheduler/*`

**Decision:** Five endpoints, mirroring the `/api/v1/sessions` + `/api/v1/memory` shape exactly. No batch ops in Tier 1.

| Method | Path | Purpose |
|---|---|---|
| `POST`   | `/api/v1/scheduler/notifications` | Create a scheduled notification |
| `GET`    | `/api/v1/scheduler/notifications` | List pending (filter by device_id, status) |
| `GET`    | `/api/v1/scheduler/notifications/{id}` | Get one |
| `DELETE` | `/api/v1/scheduler/notifications/{id}` | Cancel |
| `PATCH`  | `/api/v1/scheduler/notifications/{id}` | Reschedule (change `fire_at` only) |

**Naming choice:** `notifications` not `reminders`. "Reminder" is a user-facing word; the REST resource is the underlying notification job. Future use cases (deploy notifier, weather alert, calendar pop) are notifications too — calling it `/reminders` would either misname them or force a parallel `/api/v1/scheduler/alerts` later. Pin the abstraction now.

Wire shapes are in Section C.

### A5. ScheduleReminderTool LLM-facing schema

**Decision:** **Tight schema.** Three fields: `when` (string, accepts both ISO timestamp and relative duration), `message` (string, the body), `title` (string, optional, ≤63 chars to match `widget_card.title` max). Optional `priority` field MAPPED to widget tone (`"normal"` → `info`, `"important"` → `warn`). No exotic fields like `recurring`, `expires`, `actions` in Tier 1.

Why tight: the local LLMs we ship with (CLAUDE.md benchmarks: ministral-3:3b at 7/10 correct-tool fires) struggle with field-rich tools. Adding `recurring`/`actions`/`expires` to the Tier 1 schema would tank the fire rate for the most basic case ("remind me in 5 minutes").

The `when` field accepts dual format because asking the LLM to convert "tomorrow at 3pm" to ISO before tool-call is asking too much — let the parser on Dragon handle it (Section C has the grammar). The LLM just passes through user phrasing unchanged.

### A6. Tab5-side action handling (dismiss / snooze)

**Decision:** **Dismiss is in Tier 1. Snooze is Tier 2.**

Dismiss falls out for free: the existing `SurfaceManager.handle_action` default-dismiss path (`manager.py:99-106`) already removes a card with no registered handler. Send the `widget_card` with `action=("Dismiss", "scheduler.dismiss")`; Tab5 renders the button; tap fires `widget_action`; SurfaceManager's default-dismiss closes the card. **Zero new code on either side for dismiss.**

Snooze is harder because:
- Need to compute the new fire time on Dragon ("snooze 10 min" → server-side relative math).
- Need a registered action handler (the scheduler's) instead of default-dismiss.
- Multiple snoozes need to chain without leaking memory.

Tier 2 ships snooze because that's where we already have durable storage to record the new fire_at. If we shipped snooze in Tier 1, snoozing then restarting Dragon would lose the snoozed reminder — bad UX, gives the appearance that snooze is broken.

### A7. WS `notification` vs `widget_card`

**Decision:** **Skip the new type. Reuse `widget_card`.** The audit's phrasing in line 396-397 ("New WS `notification` message type rendered as `widget_card`") is internally contradictory — it imagines a new type that does nothing the existing one doesn't.

Wire format (exactly what hits the WS at fire time):

```json
{
  "type": "widget_card",
  "skill_id": "scheduler",
  "card_id": "sched_<8hex>",
  "title": "Reminder",
  "body": "Take out the trash",
  "tone": "info",
  "icon": "bell",
  "action": {"label": "Dismiss", "event": "scheduler.dismiss"}
}
```

Tone: `info` for default, `warn` for `priority="important"`. `danger` reserved for system-fired alerts (gateway down, OTA failed) in v1.1. The `icon` field is optional — Tab5 renders a default chat-card glyph if absent, per the widget store contract.

Why this works: the existing `voice.c:1269-1283` handler routes `widget_card` to `ui_chat_push_card` with no skill awareness. So scheduler's cards land in chat the same as media cards do. Acceptable.

### A8. Tier 1 → Tier 2 migration path

**Decision:** Define a `NotificationStore` Protocol/ABC up front. `InMemoryNotificationStore` (Tier 1) and `SqliteNotificationStore` (Tier 2) both satisfy it. The `SchedulerManager` takes the store via constructor injection. Switching tiers is a one-line wiring change in `lifecycle/startup.py`. Manager logic doesn't change.

The Protocol is intentionally narrow:
- `async create(notification) -> Notification`
- `async get(notif_id) -> Notification | None`
- `async list_pending(device_id=None) -> list[Notification]`
- `async cancel(notif_id) -> bool`
- `async update_fire_at(notif_id, fire_at) -> bool`
- `async list_due(now) -> list[Notification]` — for boot replay (Tier 1 returns empty list since nothing survives boot)
- `async mark_fired(notif_id) -> None`

Critically: the manager's *job-firing* path (the asyncio task per notification) doesn't change between tiers. Tier 2 just adds `await store.mark_fired(...)` after WS send and adds a one-shot boot replay step in startup. The replay reads `list_due(now)` and calls the manager's `_schedule_in_memory(notif)` for each → reuses the same asyncio task scheduling code as Tier 1.

This is the single most important architectural decision in the RFC because it lets ε1 ship and bake without ε2 work being a rewrite.

### A9. Test surface

**Decision:** Six test files, ~25 new test functions. Pattern stolen from `test_paused_session_retention.py` (DB+config+integration triad).

| File | Tests | Pattern |
|---|---|---|
| `tests/test_scheduler_when_parser.py` | ~7 unit tests | Pure function, no asyncio. Cover `5m`, `2h30m`, ISO, ISO with TZ, `tomorrow at 3pm`, malformed → raise, far-future cap. |
| `tests/test_scheduler_manager.py` | ~5 async tests | Patch `asyncio.sleep` to skip wall-clock waits. Cover: scheduled job fires at right time, cancel before fire suppresses, manager shutdown cancels in-flight jobs, two jobs at same instant both fire. |
| `tests/test_scheduler_tool.py` | ~3 async tests | ScheduleReminderTool.execute returns expected dict shape, rejects malformed `when`, respects per-session runaway cap (Tier 2). |
| `tests/test_scheduler_api.py` | ~4 tests | aiohttp TestServer pattern (same as `test_ws_upgrade_errors.py`). POST creates, GET lists, DELETE cancels, PATCH reschedules. |
| `tests/test_scheduler_store_sqlite.py` (Tier 2) | ~3 async tests | Real aiosqlite + tmp_path (same as `test_paused_session_retention.py`). Insert + list_due + mark_fired round-trip. |
| `tests/test_scheduler_replay.py` (Tier 2) | ~3 async tests | Boot replay fires due-but-unfired notifications; offline queue replay paces at 100ms; runaway cap enforced. |

Total: ~25 tests for ~130 cumulative across phases 1-6 = ~19% of cumulative. Proportional, not bloated.

**Live E2E:** one scenario, manually run from workstation. Posts an "in 30s" reminder, opens the WS, waits, asserts widget_card frame received within 30-32s. Does NOT join CI (matches existing `test_api_e2e.py` policy).

**Tab5 firmware tests:** None for Tier 1. Existing widget_card path is exercised by every other widget-emitting tool.

### A10. Cumulative test count discipline

25 tests is the right size. The plan explicitly skips the obvious:
- "Notification fires when fire_at is past" (covered by manager tests; no separate "passes time correctly" test)
- "REST returns 404 on missing id" (covered by every other route test in the codebase via shared `json_error`)
- "Schema migration creates table" (covered by `test_foundation.py`'s schema apply test once we add the new tables to schema.sql)

### A11. Reference doc fidelity

The audit doc has two phrases overridden in this RFC: "audio chime if appropriate" (A2) and "New WS `notification` message type rendered as `widget_card`" (A7). The audit is a planning doc; an RFC's job is to make the calls the planning doc deferred.

---

## Section B — Module + file layout

### B.1 New module: `dragon_voice/scheduler/`

```
dragon_voice/scheduler/
  __init__.py              — Re-exports: SchedulerManager, Notification, NotificationStore,
                             InMemoryNotificationStore, SqliteNotificationStore (Tier 2)
  models.py                — @dataclass Notification(id, device_id, originating_session_id,
                             fire_at, title, body, tone, status, created_at, fired_at,
                             cancelled_at, recurrence (None in Tier 1)).  Plain data, no behaviour.
  store.py                 — NotificationStore Protocol + InMemoryNotificationStore.
                             Tier 2 adds SqliteNotificationStore here as a sibling class.
  manager.py               — SchedulerManager.  Owns:
                              * dict[notif_id -> asyncio.Task]
                              * a reference to NotificationStore
                              * a reference to SurfaceManager (for fire-time delivery)
                              * a reference to SessionManager (to look up device's active session)
                             Methods: schedule(notif) -> Notification, cancel(notif_id),
                             reschedule(notif_id, new_fire_at), shutdown() (cancels all tasks).
                             Fire-time delivery method _fire_one(notif) builds the
                             widget_card payload and calls surface.card(...).
  parser.py                — parse_when(s: str, now: float, tz: tzinfo) -> float
                             Pure function.  Accepts:
                              * "5m", "2h", "1h30m", "90s", "1d"  (relative duration)
                              * ISO 8601 with or without TZ (absolute)
                              * "tomorrow at 3pm", "today at 17:00" (best-effort natural)
                             Raises ValueError on garbage.  Caps at 365 days into the future.
```

**Why `parser.py` separate:** unit-tested without asyncio. Easy to fuzz. Pinning the parser as a pure function makes it the single source of truth for "what does 'when' mean" — REST endpoint and Tool both delegate to it, so the LLM and a curl client get identical semantics.

### B.2 New tool: `dragon_voice/tools/schedule_reminder_tool.py`

Sibling to `note_tool.py`, `quick_poll_tool.py`. Construction signature: `ScheduleReminderTool(scheduler_manager)`. Mirrors how `NoteTool(notes_service)` and `QuickPollTool(surface_manager)` take their primary collaborator at construction.

The tool reads `session_id` (and looks up `device_id` via session row) from `args` — the conversation engine already injects `session_id` (`conversation.py:223` confirmed). The tool itself is dumb — it parses `when`, builds a Notification, calls `scheduler_manager.schedule(notif)`, and returns the resulting object's id + resolved fire-time.

### B.3 New API: `dragon_voice/api/scheduler.py`

Sibling to `api/sessions.py`, `api/memory_routes.py`. Same class-with-`register(app)`-method shape. Constructor takes `scheduler_manager`.

Wire it in `dragon_voice/api/__init__.py`'s `setup_all_routes` behind a `if scheduler_manager:` guard, exactly like `if memory_service:`.

### B.4 Lifecycle wiring

**`startup.py` insertion point:** AFTER SurfaceManager init and AFTER `_session_mgr` exists, but BEFORE `_tool_registry.register` of widget-emitting tools. Because:
1. SchedulerManager needs both SurfaceManager + SessionManager + Database.
2. ScheduleReminderTool needs SchedulerManager at registration time.

**`shutdown.py` insertion point:** BEFORE the per-connection pipeline drain. Cancel all in-flight scheduler tasks first so they don't race against pipeline shutdown emitting to closed WS.

Pattern matches the cancel-then-await discipline that fixed W14-M09 in `shutdown.py:42-47`.

### B.5 Tier 2 SQLite tables

Add new tables to `schema.sql` directly — the database init's `_apply_schema` (`db.py:162-173`) uses `executescript` with `CREATE TABLE IF NOT EXISTS`, so adding tables to the file just works on the next Dragon restart. No new tooling needed.

```sql
-- ── Scheduled Notifications (F-T2) ────────────────────────────────
CREATE TABLE IF NOT EXISTS scheduled_notifications (
    id            TEXT PRIMARY KEY,                 -- short uuid
    device_id     TEXT,                             -- delivery target
    originating_session_id TEXT,                    -- session that scheduled it (UX context only)
    fire_at       REAL NOT NULL,                    -- UTC epoch float
    title         TEXT NOT NULL DEFAULT 'Reminder', -- ≤63 chars
    body          TEXT NOT NULL DEFAULT '',         -- ≤255 chars
    tone          TEXT NOT NULL DEFAULT 'info'
                  CHECK(tone IN ('info', 'warn', 'success', 'danger')),
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK(status IN ('pending', 'fired', 'cancelled', 'failed')),
    recurrence    TEXT,                             -- NULL in Tier 2.0; reserved for cron-like spec
    created_at    REAL NOT NULL,
    fired_at      REAL,
    cancelled_at  REAL,
    FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sched_pending_fire
    ON scheduled_notifications(status, fire_at) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_sched_device
    ON scheduled_notifications(device_id, fire_at DESC);


-- ── Notification Queue (per-device offline replay) ────────────────
CREATE TABLE IF NOT EXISTS notification_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id       TEXT NOT NULL,
    notification_id TEXT,                           -- FK to scheduled_notifications.id
    payload         TEXT NOT NULL,                  -- JSON: the full widget_card frame
    queued_at       REAL NOT NULL,
    FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_notif_queue_device
    ON notification_queue(device_id, queued_at ASC);
```

**Why `notification_queue` carries the rendered payload, not a recipe:** by the time a notification is queued, it has already fired. The fire decision (which session was active, what tone resolved to) is past. Storing the rendered JSON means replay is "drain queue, send each frame" — simple, no fire-time logic on replay path.

**Cap enforcement (Tier 2):** at queue-insert time, count rows for `device_id`; if ≥50, drop the oldest before insert.

---

## Section C — Wire format

### C.1 LLM-facing tool schema (ScheduleReminderTool.parameters_schema)

```python
{
    "type": "object",
    "properties": {
        "when": {
            "type": "string",
            "description": (
                "When to fire the reminder. Accepts: relative duration "
                "('5m', '2h30m', '1d'), ISO 8601 timestamp "
                "('2026-04-26T15:00:00-04:00'), or a natural phrase "
                "('tomorrow at 3pm', 'today at 17:00'). "
                "Bare times ('3pm') resolve in the server's local timezone."
            ),
        },
        "message": {
            "type": "string",
            "description": "The reminder text the user will see (1-255 chars).",
        },
        "title": {
            "type": "string",
            "description": "Optional short label (default 'Reminder', max 63 chars).",
        },
        "priority": {
            "type": "string",
            "enum": ["normal", "important"],
            "description": "Default 'normal'. 'important' renders with warn tone.",
        },
    },
    "required": ["when", "message"],
}
```

### C.2 Tool result the LLM sees

```python
# Success
{
    "scheduled": True,
    "notification_id": "sched_a1b2c3d4",
    "fires_at_iso": "2026-04-25T15:30:00-04:00",
    "fires_at_local": "3:30 PM EDT",
    "fires_in": "5 minutes",
    "title": "Reminder",
    "message": "Take out the trash",
    "cancel_hint": "User can dismiss when it fires; cancel via the dashboard.",
}

# Failure (parse error, runaway cap, etc.)
{
    "scheduled": False,
    "error": "Could not parse 'when': expected '5m', ISO timestamp, or 'tomorrow at 3pm' shape.",
}
```

Why include `fires_at_local` AND `fires_at_iso`: gives the LLM a string it can speak naturally to confirm with the user, without forcing the LLM to do TZ math itself.

### C.3 WS frame at fire time

Already documented in A7. Reusing exactly the `widget_card` shape there.

`card_id` shape: `f"sched_{notification_id[:8]}"` — deterministic, lets the dashboard correlate WS frame to DB row trivially.

### C.4 REST API endpoints

**POST `/api/v1/scheduler/notifications`**

Request:
```json
{
  "when": "5m",
  "message": "Take out the trash",
  "title": "Reminder",
  "priority": "normal",
  "device_id": "abc123"
}
```

Response 201:
```json
{
  "id": "sched_a1b2c3d4",
  "device_id": "abc123",
  "originating_session_id": null,
  "fire_at": 1745618400.0,
  "fires_at_iso": "2026-04-25T15:30:00-04:00",
  "title": "Reminder",
  "body": "Take out the trash",
  "tone": "info",
  "status": "pending",
  "created_at": 1745618100.0
}
```

Response 400 on parse failure:
```json
{"error": "Invalid 'when' value", "code": "scheduler_when_parse"}
```

REST callers (dashboard, curl) MUST pass `device_id` explicitly. Tool callers (LLM) inherit it from the active session as described in B.2.

**GET `/api/v1/scheduler/notifications`**

Query params: `device_id` (optional filter), `status` (optional, defaults to `pending`), pagination via shared `parse_pagination` helper.

Response 200: standard `paginated_response` shape.

**GET `/api/v1/scheduler/notifications/{id}`**

Response 200: single notification dict (same shape as POST response).
Response 404: `json_error("Notification not found", 404)` (mirrors session lookup pattern).

**DELETE `/api/v1/scheduler/notifications/{id}`**

Hard cancel. Sets status to `cancelled`, cancels in-flight asyncio task. Response 200: `{"status": "cancelled", "id": "..."}`. 404 if not found.

**PATCH `/api/v1/scheduler/notifications/{id}`**

Body: `{"when": "10m"}` — only `when` is patchable in Tier 1 (reschedule). Future fields can be added without protocol break.

Response 200: updated notification dict. 404 if not found. 400 on parse failure or if status != pending.

---

## Section D — Decomposition into shippable PRs

The audit's `ε-design` / `ε1` / `ε2` boundaries are reasonable but ε1 is split in half. Splitting ε1 into "models + manager + tool" and "REST + WS-fire + live-validate" gives reviewers two ~3-hour PRs instead of one ~7-hour PR. The TinkerBox `CLAUDE.md` scope discipline section explicitly says "Small is kind. Prefer 5 small PRs over 1 big one."

### PR 1: ε-design (this RFC) — ~2 h

This document. Lands as `docs/RFC-scheduler.md` + a "see also" link in `docs/UX-GAPS.md` Phase 5 section.

### PR 2: ε1a — Scheduler core (in-process) — ~3 h Dragon

**Files added:**
- `dragon_voice/scheduler/__init__.py`
- `dragon_voice/scheduler/models.py`
- `dragon_voice/scheduler/store.py` (Protocol + InMemoryNotificationStore only)
- `dragon_voice/scheduler/manager.py`
- `dragon_voice/scheduler/parser.py`
- `dragon_voice/tools/schedule_reminder_tool.py`

**Files modified:**
- `dragon_voice/lifecycle/startup.py` — wire SchedulerManager + register tool
- `dragon_voice/lifecycle/shutdown.py` — cancel scheduler tasks before pipeline drain

**Tests added:**
- `tests/test_scheduler_when_parser.py` (~7 tests)
- `tests/test_scheduler_manager.py` (~5 tests)
- `tests/test_scheduler_tool.py` (~3 tests)

**CI integration:** Add the three test files to `.github/workflows/ci.yml`'s named test list.

**Live validation step:** Local pytest pass. Then deploy to Dragon (`scp -r dragon_voice/ radxa@192.168.1.91:/home/radxa/`), restart `tinkerclaw-voice`, journalctl-tail for `SchedulerManager initialized` log line. No end-user behaviour change yet because no REST endpoint or LLM tool fire — but wiring is verified.

### PR 3: ε1b — REST + LLM tool fire + Tab5 E2E — ~2-3 h Dragon

**Files added:**
- `dragon_voice/api/scheduler.py`
- `tests/test_scheduler_api.py` (~4 tests)

**Files modified:**
- `dragon_voice/api/__init__.py` — register SchedulerRoutes in `setup_all_routes`
- `dragon_voice/lifecycle/startup.py` — pass `scheduler_manager=` kwarg into `setup_all_routes`
- `CLAUDE.md` — bump REST endpoint count, add scheduler row in the API table
- `docs/protocol.md` — note that `widget_card` may originate from scheduler

**Tab5 firmware changes:** zero. Per A7 the existing `widget_card` handler at `voice.c:1269` already renders these.

**Live validation step:**
1. Local pytest pass.
2. Deploy to Dragon.
3. Curl POST: `curl -X POST http://192.168.1.91:3502/api/v1/scheduler/notifications -H "Authorization: Bearer $DRAGON_API_TOKEN" -H "Content-Type: application/json" -d '{"when":"30s","message":"e2e test","device_id":"<tab5-device-id>"}'`
4. Open Tab5 (or watch journalctl `tinkerclaw-voice`) and assert a `widget_card` arrives ~30s later with body "e2e test".
5. Voice flow: connect to Tab5, say "remind me in one minute to check the oven", wait, observe widget_card.

### PR 4: ε2 — Durable + offline queue + boot replay — ~5-6 h Dragon

**Files modified:**
- `schema.sql` — add `scheduled_notifications` + `notification_queue` tables (Section B.5)
- `dragon_voice/scheduler/store.py` — add `SqliteNotificationStore`
- `dragon_voice/scheduler/manager.py` — add boot replay on `start()`, add offline queue write path in `_fire_one`, add snooze action handler
- `dragon_voice/scheduler/__init__.py` — export `SqliteNotificationStore`
- `dragon_voice/lifecycle/startup.py` — switch wiring from in-memory to SQLite store
- `dragon_voice/server.py` — on Tab5 register, call `await server._scheduler_mgr.replay_queued_for_device(device_id)`
- `dragon_voice/db.py` — add `scheduler_*` helpers near the bottom of `Database`

**Files added:**
- `tests/test_scheduler_store_sqlite.py` (~3 tests)
- `tests/test_scheduler_replay.py` (~3 tests)

**Live validation steps:**
1. Local pytest pass.
2. Deploy to Dragon.
3. Schedule a 2-minute reminder via REST.
4. `sudo systemctl restart tinkerclaw-voice` while it's pending.
5. After restart, observe `journalctl` for "boot replay: rescheduled N notifications".
6. Wait for original fire time, observe widget_card on Tab5.
7. Offline test: disconnect Tab5 WS. Schedule a 30s reminder. Wait 35s. Reconnect Tab5. Observe widget_card replays within 100ms × queue position.
8. Runaway test: schedule 105 reminders in a tight loop via REST; assert 100 succeed, last 5 reject with 429 + machine-readable code `scheduler_runaway_cap`.

---

## Section E — Risk register

P = Probability, I = Impact, M = Mitigation.

| # | Risk | P | I | Mitigation |
|---|---|---|---|---|
| R1 | Wall-clock drift between Dragon and Tab5 leads to "your 3pm reminder fired at 2:55pm" complaints. | Low | Low | Server clock authoritative (A1). Tab5 only renders strings; never does time math on schedule fields. Dragon NTP keeps local TZ accurate. |
| R2 | LLM schedules 1000 reminders by accident (prompt injection, model misinterpretation, toolcall storm). | Med | High | Per-device runaway cap of **100 pending notifications**. Tier 1 enforces in `SchedulerManager.schedule()` before insert; Tier 2 enforces at SQLite insert too (defence in depth). Cap rejection returns explicit `scheduler_runaway_cap` error in tool result so LLM stops trying. |
| R3 | Reminder fires while Tab5 is mid-voice-turn (TTS playing, LLM streaming). | Med | Med | Don't gate fire on session state. The `widget_card` lands in chat regardless of voice state — matches existing behaviour for tool_call/tool_result events. Tab5 already handles concurrent widget pushes via its priority queue. No interrupt logic needed. |
| R4 | Reminder fires while Tab5 is offline. Tier 1: lost. | High (T1) | Low | Documented limitation in audit + RFC. Tier 1 logs `dropped_offline_device` event so the dashboard can surface "Tab5 was offline; 2 reminders dropped" later. Tier 2 fixes properly via offline queue. |
| R5 | Reminder fires while Tab5 is offline. Tier 2: queue grows unbounded. | Med | Med | Per-device cap of **50 queued items**. At insert, count + drop oldest before insert. 100 ms pacing on replay (via `await asyncio.sleep(0.1)`) so replay doesn't slam Tab5's WS receive queue. |
| R6 | User says "at 3pm" without TZ; system resolves to Dragon TZ; user expected device TZ. | Low | Med | Tool result includes both `fires_at_iso` (with TZ) and `fires_at_local` (string). LLM can read these and confirm verbally. **No silent wrong behaviour** — the resolved time is always echoed back. |
| R7 | Tier 2.5 recurring reminder with bad spec runs every second forever. | Med | High | Recurring lands in 2.5/3, not now. When it does, validation: minimum interval ≥60s, maximum 100 active recurring per device, recurrence parser must round-trip. Out of Tier 1/2 scope; flagged here for the future. |
| R8 | Boot replay floods Tab5 with 50 due-but-unfired notifications after a multi-hour outage. | Med | Med | Boot replay (Tier 2) only fires notifications whose `fire_at` is within the last **15 minutes** by default. Older-than-15-min "due" reminders mark `failed` with reason `replay_window_expired`. Configurable via `DatabaseConfig.scheduler_replay_window_seconds` (default 900). |
| R9 | Snooze action handler leaks across Dragon restart in Tier 2. | Low | Low | Snooze is action-event handled by SurfaceManager → calls SchedulerManager.reschedule which writes to SQLite. The asyncio task is recreated on startup boot replay path. |
| R10 | The asyncio.create_task per notification leaks if SchedulerManager doesn't track them. | Med | Med | Manager holds a `dict[notif_id -> Task]` and adds to it in `schedule()`, removes in `_fire_one()` finally block, and in `cancel()`. Shutdown iterates dict and cancels all (matches the pattern in `shutdown.py:42-67`). RUF006 lint catches "fire and forget" task creation. |
| R11 | Two notifications with identical `fire_at` race on delivery to a session that just disconnected. | Low | Low | `surface.card()` (`base.py:406-412`) catches all send exceptions and logs warning. Notifications still get marked fired in DB regardless of WS send outcome. |

---

## Section F — Rollout sequence

Each step lands as a single git commit. PR boundaries match Section D.

1. **Commit 1 (PR1):** Add `docs/RFC-scheduler.md` with the body of this document. Add a "see also" link in `docs/UX-GAPS.md` Phase 5 section. **Verify:** `cat docs/RFC-scheduler.md | head -50` shows Section A.

2. **Commit 2 (PR2):** Add `dragon_voice/scheduler/` (5 files) + `dragon_voice/tools/schedule_reminder_tool.py`. Wire into `lifecycle/startup.py` and `lifecycle/shutdown.py`. Add three test files. Update `.github/workflows/ci.yml` named list. **Verify:** `pytest -q tests/test_scheduler_when_parser.py tests/test_scheduler_manager.py tests/test_scheduler_tool.py` all pass. Deploy to Dragon, restart, journalctl shows `SchedulerManager initialized`.

3. **Commit 3 (PR3):** Add `dragon_voice/api/scheduler.py`, register in `api/__init__.py`. Pass kwarg in startup. Add test file. Update `.github/workflows/ci.yml`. Update `CLAUDE.md` REST table. **Verify:** `pytest -q tests/test_scheduler_api.py` passes. Live curl POST a 30s reminder, observe widget_card on Tab5 (or in journalctl).

4. **Commit 4 (PR4):** Add `scheduled_notifications` + `notification_queue` to `schema.sql`. Add `SqliteNotificationStore` to `store.py`. Add boot replay + offline queue + snooze to `manager.py`. Wire device-register replay hook in `server.py`. Add Database helpers to `db.py`. Switch startup wiring. Add two test files. Update CI. **Verify:** local pytest, deploy, restart-mid-pending test, offline-replay test, runaway-cap test all pass.

Each commit is independently revertable. PR3 alone would leave a working in-process scheduler that LLM and REST can both drive — useful and shippable, just lossy on restart. PR4 is purely additive durability.

---

## Section G — Effort estimate

| PR | LOC added | LOC modified | Tests added | Wall-clock hours |
|---|---|---|---|---|
| PR1 (ε-design) | ~600 (RFC) | ~5 (UX-GAPS link) | 0 | 2 |
| PR2 (ε1a core) | ~450 | ~30 | 15 | 3 |
| PR3 (ε1b REST + E2E) | ~180 | ~25 | 4 | 2 |
| PR4 (ε2 durable) | ~350 | ~50 | 6 | 5 |
| **Total** | **~1580** | **~110** | **25** | **12** |

Audit said ~15h across all three PRs. Estimating 12. Difference: the audit treated ε1 as one ~7h PR; splitting it into two ~3h+2h PRs saves the rebasing and review-context-switch overhead. Tab5 firmware estimate (audit said ~3h) is **0** because we're reusing existing widget_card handlers.

If snooze ends up wanting Tab5-side polish (better-looking action button labels, snooze choice menu UI), that's a future ~2h Tab5 PR scoped separately.

LOC is approximate. The largest single file will be `manager.py` at ~250 LOC including the boot replay and offline queue methods (Tier 2 additions). Smallest will be `__init__.py` at ~10 LOC.

---

## Section H — Non-goals

This RFC explicitly does NOT do these things. Pinning so the executor doesn't drift:

1. **Recurring reminders.** Not in Tier 1 or Tier 2. "Every weekday at 9am" is a Tier 2.5 / Tier 3 conversation that requires a parser dialect decision (cron vs declarative recurrence) the audit explicitly defers (line 207).

2. **Cancel-by-LLM tool.** No `CancelReminderTool`. The LLM cannot cancel previously-scheduled reminders in v1. User cancels via dashboard or via the `widget_card`'s Dismiss button (which only dismisses the *fired* card, not the upcoming reminder). Adding LLM cancel needs a notification-id surface in the conversation context, which is a UX design problem we don't have time to solve cleanly here.

3. **Audio chime.** A2 explained why. v1.1 polish.

4. **Snooze in Tier 1.** A6 explained why. Snooze lands with Tier 2 (PR4).

5. **Cross-device delivery.** A reminder for device A never fires on device B. The TinkerBox concept is single-device-per-user; multi-device fan-out is a v2 feature that intersects with auth/identity work that doesn't exist yet.

6. **Push notification when Tab5 is offline.** No magic packet, no BLE, no LAN wake-up. Audit explicitly carves this out as Tier 3 (line 410). Confirmed.

7. **Calendar integration.** No iCal import, no Google Calendar sync, no `.ics` file ingest. Reminders are user-initiated only.

8. **Notification grouping / coalescing.** If 5 reminders fire within 10 seconds, 5 cards appear. No "you have 3 reminders" summary. Defer until real usage shows it's needed.

9. **Per-skill scheduler API.** Other skills (TimesenseTool, etc.) don't get to call `scheduler_mgr.schedule(...)` from their tool code in v1 — only `ScheduleReminderTool` and the REST API do. If we expose it later, the surface is `manager.surface_for(...)` analogue, but we don't pre-build it because YAGNI.

10. **Notification preview / "what's next".** No `/api/v1/scheduler/upcoming`, no Tab5 home-screen "next reminder" widget. Both can be added trivially later (the GET list endpoint already supports it filtered + sorted) but they're separate PRs with separate UX questions.

11. **Tab5-side notification persistence.** When Tab5 reboots after a notification fired, the widget_card is gone (Tab5's chat history is in PSRAM, not durably persisted). That's fine — the firing IS the notification; persistence-after-display is a different feature.

---

## Critical Files for Implementation

These five paths are the load-bearing files for execution. The first three are the new code surface; the last two are the wiring/test scaffolding to mirror.

- `dragon_voice/scheduler/manager.py` (NEW — `SchedulerManager` with `schedule`, `cancel`, `reschedule`, `_fire_one`, `start`/`shutdown`. Largest single file. Tier 2 additions live here.)
- `dragon_voice/scheduler/parser.py` (NEW — `parse_when` pure function. Single source of truth for time semantics shared by REST + LLM tool. Pinning as pure makes it fuzzable + cheap to unit test.)
- `dragon_voice/lifecycle/startup.py` (MODIFIED — the wiring spine. Get this wrong and the rest never runs.)
- `dragon_voice/tools/schedule_reminder_tool.py` (NEW — LLM-facing tool. Mirrors `note_tool.py`'s shape exactly. The `parameters_schema` here is the contract every local model in the gauntlet must execute against.)
- `tests/test_paused_session_retention.py` (REFERENCE, NOT MODIFIED — clone its DatabaseConfig + Database query + cleanup-loop integration triad pattern when writing `test_scheduler_store_sqlite.py` and `test_scheduler_manager.py`.)
