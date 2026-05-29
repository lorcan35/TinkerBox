---
audience: developer
type: explanation
prerequisites: [docs/ARCHITECTURE.md](../ARCHITECTURE.md), [docs/explanation/how-the-stack-fits-together.md](how-the-stack-fits-together.md)
last-verified: 2026-05-29
---
# TinkerBox architecture — how it works and why

## The question

Why does the Dragon server look the way it does? Why is a session a different
thing from a WebSocket connection? Why can you never edit a message after it is
written? Why is there a separate package for middleware, another for handlers,
another for lifecycle? And why did a 2,747-line `server.py` get torn into four
sibling packages in a single umbrella issue?

This page builds the mental model. It is not a how-to — you will not flash
anything or run a deploy here. It explains the load-bearing design decisions so
that when you open `dragon_voice/`, the layout reads as deliberate rather than
accidental, and so that when you add a feature you put it where the existing
grain already runs.

For the cross-repo picture — how Dragon relates to the Tab5 firmware, the
TinkerClaw gateway, and the cloud — read
[How the stack fits together](how-the-stack-fits-together.md) first. This page
is the inside of the brain.

## The model

### Dragon is the brain; Tab5 is the face

The first decision is also the one that shapes everything else: **all
intelligence lives on Dragon.** The Tab5 device is a thin client. It captures
microphone PCM, paints an LVGL UI, drives a speaker and camera, and stores its
own WiFi and NVS settings — and nothing more. There is no AI logic on the Tab5.

Dragon, a Radxa Dragon Q6A (Qualcomm QCS6490, ARM64), owns the entire pipeline:
speech-to-text, the language model, text-to-speech, embeddings, session
management, the conversation engine, the REST API, the dashboard, and the
database. The two halves talk over a single WebSocket on port 3502, with the
wire contract pinned in [`protocol.md`](../protocol.md).

```
Tab5 (ESP32-P4) — the face          Dragon Q6A — the brain (this repo)
+--------------------+              +----------------------------------+
| LVGL UI            |   WS /ws/voice  | Voice server (:3502)           |
| Mic / Speaker      | <============>  |   STT  -> ConvEngine -> LLM     |
| Touch / Camera     |  PCM + JSON     |        -> tools/memory -> TTS   |
| WiFi / NVS         |                 |   SessionManager + MessageStore |
+--------------------+                 |   REST API  +  event bus  +  DB |
                                       +----------------------------------+
```

This split is not a layering preference. It is a deployment fact: the Tab5
cross-compiles to ESP32-P4 and flashes over USB, while Dragon is a Python
service that `scp`s into place and runs under systemd. Different toolchains,
different release cadences, different contributor expertise — so they are
different repos, and the protocol contract is what lets each evolve on its own
schedule.

### Eight decisions that hold the server together

These are durable design rules, captured from the original scaffolding research
and enforced ever since. They came from studying production voice stacks: the
ChatContext item model from LiveKit, Vocode's transcript metadata, Pipecat's
frame taxonomy, and StackFlow's `create/resume/pause/end` lifecycle verbs.

#### 1. Session != Connection

A WebSocket connection is ephemeral — it dies on every network blip, every Tab5
reboot, every WiFi handoff. A [session](../../GLOSSARY.md#tinkerbox-specific-terms)
is durable. It survives disconnects. When a device reconnects and presents its
`session_id` in the `register` frame, Dragon resumes the same session with full
message history intact, rather than starting a blank conversation.

| Concept | Lifetime | Survives | Stored in |
|---|---|---|---|
| WebSocket connection | Until the next network blip | Reconnect resumes the session | in-memory only |
| Session | Until aged-out (default 30 min idle) or explicitly ended | Reboot, reconnect, mode swap | `sessions` table |
| Device | Forever (registered once) | Reboot, factory reset | `devices` table |

The lifecycle is `active -> paused -> active -> ended`, mirroring StackFlow's
verbs. A clean disconnect moves a session to `paused`; a matching reconnect
flips it back to `active`. This is why a Tab5 can lose WiFi mid-conversation,
reconnect, and pick up exactly where it left off — the connection was disposable
but the session was not.

#### 2. Conversation items are append-only

Messages are **never mutated after creation.** The `messages` table is a
write-once ledger: a row records `role`, `content`, `input_mode`, whether the
turn was `interrupted`, the `model` that generated it, and `latency_ms`. Once
written, it stays. There is no update path.

This is the cheapest possible source of truth for a conversation. Context
reconstruction is just "read the rows in order." There is no merge logic, no
last-writer-wins race, no question of whether the in-memory transcript and the
stored transcript agree — they cannot disagree, because the store only ever
grows. `MessageStore` (`dragon_voice/messages.py`) owns both the append and the
context builder that walks those rows into LLM input.

#### 3. The device is first-class

A device is not an anonymous socket. It registers once with a stable
`device_id` (the Tab5 uses its hardware MAC), a `hardware_id`, a `firmware_ver`,
a `platform`, and a `capabilities` map (`{mic, speaker, screen, camera, ...}`).
Online and offline status is tracked in real time in the `devices` table.

Making the device first-class is what lets Dragon answer "which Tab5s are live
right now?", target a specific device for a channel-message push, scope config
to one device, and check a capability flag before relying on a firmware feature.
A new firmware feature ships behind a flag in the `register` frame; Dragon reads
the flag before using it, so the two repos never have to release in lockstep.

#### 4. OpenAI message format as the universal context representation

Every backend — Ollama, OpenRouter, LM Studio, the NPU, the TinkerClaw gateway —
speaks a different dialect. Rather than thread N representations through the
conversation engine, Dragon uses **OpenAI chat-message format** as the one
internal representation and converts to each provider's shape at the adapter
boundary.

The payoff shows up in multimodal continuity. A photo is persisted as a
`__mm__:` JSON marker on a message row; `MessageStore.get_context(media_store=…)`
hydrates that marker back into an OpenAI `image_url` content array on the next
turn. The Ollama backend then translates the OpenAI array into Ollama's flat
`content + images` shape. Because the canonical form is OpenAI-format, you can
send a photo and ask "what color was the chair?" three turns later and the
follow-up still sees the image — the conversion happens once, at the edge,
per backend.

#### 5. Notes are sessions, not a parallel system

A note is a session tagged `type='recording'`. There is no second storage
engine, no duplicate lifecycle, no parallel "notes vs. conversations" code path.
Dictation produces a recording session whose transcript and LLM-generated title
and summary live on the same tables as everything else. One data model, reused.

#### 6. An event bus for decoupled real-time updates

Real-time consumers — the dashboard, the notes view, the skills surface, the
`agent_log` feed — all **subscribe** rather than reach into the pipeline.
Producers emit events (`session.created`, `device.connected`, `message.added`,
tool-call activity) and never know who is listening. The `events` table is the
durable side of this: an append-only audit log that doubles as the dashboard's
catch-up feed and as the data source behind `GET /api/v1/spend` (daily LLM cost
rolled up over `events`).

The bus is why adding a new observer is a local change — you subscribe, you do
not edit the pipeline. The `progress` event family (issue #123) is the same idea
on the wire: a single unified progress event the Tab5 subscribes to, instead of
a bespoke message type per phase.

#### 7. Scoped config: global -> device -> session

Configuration resolves by specificity. A value can be set at **global**,
**device**, or **session** scope, and the most specific scope wins. The `config`
table is keyed on `(key, scope, scope_id)`; a `GET /api/v1/config/{key}?resolve=true`
applies the cascade `session > device > global` and returns the effective value.

This is what lets two Tab5s on the same Dragon run in different voice modes at
once. Each WebSocket connection gets a `copy.deepcopy()` of the global config, so
one device switching to Cloud mode cannot corrupt another device's pipeline
config. Scope plus per-connection deep-copy is the whole isolation story —
there is no shared mutable config object for two clients to fight over.

#### 8. aiosqlite, and one db.py

Async SQLite via [`aiosqlite`](../../GLOSSARY.md#tinkerbox-specific-terms), in
WAL mode with foreign keys on, behind a **single** `dragon_voice/db.py` module.
No raw SQL is scattered across the codebase. Every table touch goes through one
async CRUD layer.

SQLite is the right call for a single-box server: no separate database process
to run or monitor, WAL mode gives concurrent readers alongside a writer, and
FTS5 plus `sqlite-vec` cover keyword and vector search without a second
datastore. Concentrating all of it in one module means the schema, the query
patterns, and the connection lifecycle have exactly one place to live and one
place to audit. The schema itself is `schema.sql` — 11 tables: 6 foundation
(`devices`, `sessions`, `messages`, `notes`, `events`, `config`), 3 memory
(`memory_facts`, `memory_documents`, `memory_chunks`), and 2 scheduler
(`scheduled_notifications`, `notification_queue`).

### A turn, from frame to speaker

The decisions above describe state. Here is how they cooperate during one voice
turn. The Tab5 sends `start`, streams binary PCM, and sends `stop`. Dragon then:

```
mic PCM (16 kHz int16)
  -> STT (Moonshine / Whisper.cpp / Vosk / OpenRouter)
  -> ConversationEngine.process_text_stream(session_id, text)
       (reads append-only history, injects recalled memory facts,
        runs the tool loop, picks a backend via the router)
  -> LLM stream
  -> sentence buffer (flush on sentence boundary for low-latency TTS)
  -> TTS (Piper / Kokoro / Edge / OpenRouter)
  -> resample to 16 kHz, pace at ~80% real-time, stream 4096-byte chunks
  -> speaker PCM
```

Every turn — voice, text, or vision — converges on
`ConversationEngine.process_text_stream`. The only exception is voice mode 3,
where Dragon becomes an audio pipe and the TinkerClaw gateway runs the turn
instead. The tool loop inside the engine parses one of three accepted XML
dialects, executes a tool, injects the result, and lets the model continue —
capped at three tool calls per turn to bound the loop. The trace for each turn
type lives in [`flows/`](../flows/).

### The #65 decomposition: why server.py became four packages

`server.py` started as a 2,747-line monolith holding everything: request
middleware, diagnostic endpoints, boot and shutdown logic, long-running
monitors, and the WebSocket voice handler family — all in one file. Umbrella
issue #65 (closed 2026-04-24) split it into four siblings:

```
dragon_voice/
  server.py       — VoiceServer class + create_app wiring + WS-voice handlers
  middleware/     — request-filter concerns (cors, security_headers, auth, rate_limit)
  handlers/       — diagnostic + status + config endpoints (debug, status, config_api)
  lifecycle/      — boot / shutdown / long-running monitors (startup, shutdown,
                    monitors, purge)
```

The split followed one rule — the **file-split smell test**: a file is too big
when it has more than one *reason to change*. Middleware changes for
ops and security reasons. Diagnostic endpoints change for dev and diagnostics
reasons. Lifecycle changes for ops and reliability reasons. Business endpoints
change for product reasons. Four stakeholders, four packages. Each extracted
module takes its dependencies explicitly — a server handle or specific args — so
each can be unit-tested without standing up a full `VoiceServer`. That testability
is the dividend: `lifecycle/monitors.py` has its own focused test suite because
it no longer drags the whole server in behind it.

The decomposition also followed **extract before decompose**: the first move was
to relocate code to its new home with identical behavior, and only later refactor
the internals. That keeps each diff reviewable in minutes. The `media_cleanup_loop`,
for instance, was inline in `server.py` before #65 and now lives in
`lifecycle/purge.py` — moved verbatim first, cleaned up after.

`server.py` was 1,803 lines right after the split and has since regrown to
~2,720 lines as UX-gap fixes, the multi-model router, and W7 agent handlers
accreted to the core WS handler family. That regrowth is expected and managed:
the package root now holds 60+ extracted sibling modules (the `*_handler.py`,
`*_swap.py`, `config_*.py`, `*_path.py`, `*_emit.py` families), each owning one
concern so tests can import it in isolation. When you add a slow path, a config
guard, or a new endpoint, the question to ask is "whose reason-to-change is
this?" — and the answer almost always points at an existing sibling rather than
at `server.py` itself.

## Why it's built this way

The through-line is that **state is durable and conversation is a ledger.** Every
one of the eight decisions exists to make a single-box, intermittently-connected
voice assistant survivable:

- Sessions outlive connections because the connection is the thing that fails.
- Messages are append-only because a conversation has no legitimate reason to be
  rewritten, and a write-once store has no race to lose.
- Devices are first-class because the server has to reason about specific
  hardware — capabilities, targeting, scoped config — not anonymous sockets.
- One internal message format and one db.py exist because the alternative is N
  representations and SQL scattered across the codebase, and both rot.

**Trade-offs taken on purpose.** SQLite means no horizontal database scaling —
acceptable, because Dragon is one box serving a household, not a fleet. The
append-only ledger trades storage growth for the elimination of mutation races;
a `periodic_purge_loop` in `lifecycle/purge.py` handles retention so the trade
stays bounded. Local-mode inference is slow (60-90 s per turn on the Q6A CPU) —
accepted as the cost of privacy, with Hybrid and Cloud modes as the explicit
opt-out when latency matters more.

**The constraint that drives the rest** is the hardware: 12 GB of LPDDR5, an
~8 GB effective ceiling after the OS and services, and CPU-bound local
inference. That budget is why the model representation is shared rather than
duplicated, why the router evicts unused models via `keep_alive_s`, and why the
voice mode picks how much of the work goes to the cloud. The architecture is the
shape of "do as much as possible on one modest ARM box, and let the user decide
when to spend money or trust on the cloud."

## See also

- [How the stack fits together](how-the-stack-fits-together.md) — the cross-repo view: Tab5 (face) ↔ Dragon (brain) ↔ gateway ↔ cloud.
- [`docs/ARCHITECTURE.md`](../ARCHITECTURE.md) — the system map, network topology, and hardware reference.
- [WebSocket protocol reference](../reference/websocket-protocol.md) · [`protocol.md`](../protocol.md) — the Tab5 ↔ Dragon wire contract.
- [Voice-turn flow](../flows/voice-turn.md) · [Vision-turn flow](../flows/vision-turn.md) — code-level traces of one request through the engine.
- [Router cookbook](../router-cookbook.md) · [Configure the multi-model router](../how-to/configure-the-multi-model-router.md) — the per-modality, per-tier LLM router.
- [`GLOSSARY.md`](../../GLOSSARY.md) — canonical terms for the stack.
