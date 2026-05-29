---
audience: integrator
type: reference
prerequisites: [protocol.md](../protocol.md), [SECURITY.md](../../SECURITY.md)
last-verified: 2026-05-29
---
# REST API reference

Dragon is API-first: every capability the voice pipeline uses is also reachable
over HTTP, so any client — not just a Tab5 — can drive it. This page is the
authoritative list of the `/api` surface (62+ endpoints) served by
`tinkerclaw-voice` on **port 3502**, grouped by domain. The single WebSocket
endpoint (`/ws/voice`) is documented separately in the
[WebSocket protocol reference](../protocol.md).

Every endpoint below is served by the voice server. The
[dashboard](../../README.md#web-dashboard) on port 3500 does not own any of
these — it proxies them through `/api/proxy/` to port 3502.

## Base URL and conventions

| Property | Value |
|---|---|
| Host | `http://<dragon-ip>:3502` (LAN) or `https://tinkerclaw-voice.ngrok.dev` (tunnel) |
| Versioned prefix | `/api/v1/*` — sessions, messages, devices, config, events, agent log, media, system, tools, memory, documents, scheduler, spend, integrations, channels |
| Unversioned prefix | `/api/*` — notes (`/api/notes`), OTA (`/api/ota`), rich media (`/api/media`), video debug (`/api/video`), top-level config (`/api/config`) |
| Request body | JSON (`Content-Type: application/json`) unless noted (audio/media uploads use `application/octet-stream` or `image/*`) |
| Response body | JSON unless noted (SSE for `/chat` and `/completions`; binary for `/synthesize`, `/api/media/{id}`, `/api/ota/firmware.bin`) |
| ID format | Session IDs are 12-char hex; message/note IDs are short UUIDs; device IDs are the device's MAC/hardware ID |

## Authentication

The `/api/*` surface is gated by a bearer token (`DRAGON_API_TOKEN`) enforced in
[`dragon_voice/middleware/auth.py`](../../dragon_voice/middleware/auth.py). The
token lives in `/home/radxa/.env` on the Dragon and survives `scp -r dragon_voice/`
deploys. Send it on every authenticated request:

```bash
curl -s http://192.168.1.91:3502/api/v1/sessions \
  -H "Authorization: Bearer $DRAGON_API_TOKEN" | python3 -m json.tool
```

A few prefixes are intentionally **public (no bearer)** — they carry no secrets
or use their own access control:

| Public route | Why it is public |
|---|---|
| `/health` | Liveness probe — no privacy concern |
| `/ws/voice` | The bearer is checked *during* the WS upgrade, not bypassed |
| `/dashboard` | Static dashboard shell; the data calls it proxies are bearer-gated |
| `/api/media/{id}` | Access controlled by HMAC-signed, time-bounded URLs (`MediaUrlSigner`, W14-H04) instead of a bearer |
| `/call`, `/static/*` | Browser call client assets; sensitive routes stay under `/api/v1/*` |

Per-IP throttling runs in
[`dragon_voice/middleware/rate_limit.py`](../../dragon_voice/middleware/rate_limit.py)
(`DEFAULT_RATE_LIMIT_RULES`, W15-H01/W15-H06). On the wire, `config_update`
pushes from Tab5 are coalesced server-side; expect 500–1000 ms ACK latency under
back-pressure. See [`SECURITY.md`](../../SECURITY.md) for the full token
lifecycle, rotation, and network-exposure model.

## Sessions (`/api/v1/sessions`)

A session is the conversation container. Sessions survive WebSocket disconnects:
a device that reconnects resumes the same session with its full message history.
Sessions move through `active → paused → active → ended`.

| Method | Path | Purpose | Notes |
|---|---|---|---|
| GET | `/api/v1/sessions` | List sessions | Query: `device_id`, `status`, `limit`, `offset` |
| POST | `/api/v1/sessions` | Create a session | Body: `device_id`, `type`, `system_prompt`, `config` |
| GET | `/api/v1/sessions/{id}` | Get one session | — |
| POST | `/api/v1/sessions/{id}/end` | End a session permanently | Sets `status=ended`, `ended_at` |
| POST | `/api/v1/sessions/{id}/resume` | Resume a paused session | `paused → active` |
| POST | `/api/v1/sessions/{id}/pause` | Pause an active session | `active → paused` |
| PATCH | `/api/v1/sessions/{id}` | Update metadata | Body: `title`, `system_prompt`, `metadata` |
| GET | `/api/v1/sessions/{id}/context` | Get the formatted LLM context | OpenAI message-array shape built from history |

## Messages (`/api/v1/sessions/{id}/messages`)

Messages are append-only and never mutated after creation. The conversation
engine builds LLM context from this history automatically.

| Method | Path | Purpose | Notes |
|---|---|---|---|
| GET | `/api/v1/sessions/{id}/messages` | List messages in a session | Query: `limit`, `offset` |
| POST | `/api/v1/sessions/{id}/chat` | Send text, stream the LLM reply | **SSE stream** (see Examples) |
| GET | `/api/v1/messages/{id}` | Get a single message by ID | — |
| DELETE | `/api/v1/sessions/{id}/messages` | Purge all messages in a session | Clears history; keeps the session |

## Devices (`/api/v1/devices`)

Every connecting client registers as a device with its hardware ID, firmware
version, platform, and capability set. Online/offline state is tracked live.

| Method | Path | Purpose | Notes |
|---|---|---|---|
| GET | `/api/v1/devices` | List devices | Query: `online=true` to filter |
| GET | `/api/v1/devices/{id}` | Get one device | — |
| PATCH | `/api/v1/devices/{id}` | Update device | Body: `name`, `config` |
| DELETE | `/api/v1/devices/{id}` | Remove a device | — |

## Config store (`/api/v1/config`)

A scoped key-value store, distinct from the process `config.yaml`. Resolution
order is `session → device → global` — the most specific scope wins.

| Method | Path | Purpose | Notes |
|---|---|---|---|
| GET | `/api/v1/config` | List config entries | Query: `scope`, `scope_id` |
| GET | `/api/v1/config/{key}` | Get a value | Query: `scope`, `scope_id`, `resolve=true` |
| PUT | `/api/v1/config/{key}` | Set a value | Body: `value`, `scope`, `scope_id` |
| DELETE | `/api/v1/config/{key}` | Delete a key | — |

The unversioned `/api/config` endpoints are a separate concern — they hot-reload
the running pipeline's `config.yaml` (backend swap), not the scoped store. See
[Top-level config and health](#top-level-config-and-health) below.

## Events (`/api/v1/events`)

| Method | Path | Purpose | Notes |
|---|---|---|---|
| GET | `/api/v1/events` | List system events | Query: filter by `type`, `session`, `device` |

Event types include `session.created`, `device.connected`, and `message.added`.
The events table also backs the [spend roll-up](#spend-apiv1spend).

## Agent log (`/api/v1/agent_log`)

| Method | Path | Purpose | Notes |
|---|---|---|---|
| GET | `/api/v1/agent_log` | Cross-session tool-call activity feed | Last 64 entries; populated at the `ToolRegistry.execute` chokepoint (TT #328 Wave 12) |

Each entry is source-flagged (`tool_call`, `user_reply`, `channel_push`) so the
Tab5 Agents overlay can break out per-source counts.

## Media (`/api/v1/transcribe`, `/synthesize`, `/completions`)

Stateless single-shot pipeline primitives — no session needed.

| Method | Path | Purpose | Notes |
|---|---|---|---|
| POST | `/api/v1/transcribe` | STT: audio bytes → text | Body: raw PCM (`application/octet-stream`) or `audio/wav`. Optional `X-Sample-Rate` header (default 16000). Returns `{text, duration_s, stt_ms}` |
| POST | `/api/v1/synthesize` | TTS: text → audio bytes | Returns binary audio (PCM int16, resampled to 16 kHz) |
| POST | `/api/v1/completions` | Direct LLM completion | **SSE stream**; stateless, no session context — the dashboard Chat tab's "stateless mode" |

## System (`/api/v1/system`, `/backends`)

| Method | Path | Purpose | Notes |
|---|---|---|---|
| GET | `/api/v1/system` | System metrics | CPU, RAM, active connection count |
| GET | `/api/v1/backends` | List available backends | Installed STT / TTS / LLM backend keys |

## Tools (`/api/v1/tools`)

Direct access to the agentic tool layer. The tool catalog (`web_search`,
`remember`, `recall`, `datetime`, plus 6 more — 10 total) is the same set the
LLM invokes during a turn.

| Method | Path | Purpose | Notes |
|---|---|---|---|
| GET | `/api/v1/tools` | List available tools | Each carries a JSON schema for its arguments |
| POST | `/api/v1/tools/{name}/execute` | Execute a tool directly | Body = the tool's argument object; bypasses the LLM |

To add a tool to this surface, follow the
[add-a-tool how-to](../how-to/add-a-tool.md).

## Memory (`/api/v1/memory`)

Semantic fact store. Facts are embedded with Ollama `nomic-embed-text`
(768-dim) and auto-recalled into the system prompt before every LLM call.

| Method | Path | Purpose | Notes |
|---|---|---|---|
| GET | `/api/v1/memory` | List stored facts | — |
| POST | `/api/v1/memory` | Store a fact | Same store the LLM's `remember` tool writes to |
| DELETE | `/api/v1/memory/{id}` | Delete a fact | — |
| POST | `/api/v1/memory/search` | Semantic search facts | Body: `query`, `limit`; ranks by cosine similarity |

## Documents (`/api/v1/documents`)

Long-term knowledge ingest. Text is chunked (512 tokens / chunk, 50-token
overlap), embedded with `nomic-embed-text`, and stored in SQLite with
`sqlite-vec` for vector search.

| Method | Path | Purpose | Notes |
|---|---|---|---|
| POST | `/api/v1/documents` | Ingest a document | Chunks + embeds the text |
| GET | `/api/v1/documents` | List documents | — |
| DELETE | `/api/v1/documents/{id}` | Delete a document | Removes the document and all its chunks |
| POST | `/api/v1/documents/search` | Semantic search across chunks | Returns ranked chunks by cosine similarity |

## Notes (`/api/notes`)

Notes are session artifacts (`type='recording'`) — created from text or from a
raw audio upload, optionally enriched with an LLM-generated title and summary.

| Method | Path | Purpose | Notes |
|---|---|---|---|
| POST | `/api/notes` | Create a note from text | Body: `text`, `title` |
| GET | `/api/notes` | List notes | Query: `limit`, `offset` |
| GET | `/api/notes/{id}` | Get one note | — |
| PUT | `/api/notes/{id}` | Update a note | Body: `title`, `transcript`, `summary`, `tags` |
| DELETE | `/api/notes/{id}` | Delete a note | — |
| POST | `/api/notes/search` | Semantic search notes | Body: `query`, `limit` |
| POST | `/api/notes/from-audio` | Create a note from raw PCM audio | Transcribes then stores |

## Top-level config and health

Served at the root, outside `/api/v1`.

| Method | Path | Purpose | Notes |
|---|---|---|---|
| GET | `/` | HTML status page | Backend info + uptime |
| GET | `/health` | JSON health check | **Public** (no bearer) |
| GET | `/api/config` | Current process config | Secrets redacted |
| POST | `/api/config` | Hot-reload config / swap backends | Re-inits active pipelines in place — see [swap-the-llm-backend](../how-to/swap-the-llm-backend.md) |

## OTA (`/api/ota`)

Firmware delivery for Tab5. Deploy a build by copying `tinkertab.bin` to
`/home/radxa/ota/` and updating `version.json`.

| Method | Path | Purpose | Notes |
|---|---|---|---|
| GET | `/api/ota/check?current=VERSION` | Check for a firmware update | Compares against `/home/radxa/ota/version.json`; returns `{update, version, url, sha256}` |
| GET | `/api/ota/firmware.bin` | Download the firmware image | Streams `/home/radxa/ota/tinkertab.bin` in 8 KB chunks |

## Rich media (`/api/media`)

Dragon renders code blocks, markdown tables, and image URLs from LLM responses
into JPEGs for inline display on Tab5, and accepts camera uploads.

| Method | Path | Purpose | Notes |
|---|---|---|---|
| GET | `/api/media/{id}` | Serve a rendered media file | JPEG / PNG / WAV; `Cache-Control: max-age=3600`. **Public** via HMAC-signed URL |
| POST | `/api/media/upload` | Accept a BMP/JPEG from the Tab5 camera | Converts + resizes via Pillow; returns `media_id` for `user_media` WS reference |

## Video (debug) (`/api/video`)

| Method | Path | Purpose | Notes |
|---|---|---|---|
| POST | `/api/video/inject?device_id=X` | Push a JPEG as if from a paired Tab5 | Body = raw JPEG; wrapped with the `VID0` magic + 4-byte BE length. Exercises the Tab5 downlink decode path without a second device (#178) |

## Scheduler (`/api/v1/scheduler/notifications`)

Time-deferred reminders. A scheduled notification is delivered to a device over
the voice WS at fire time and is replayable from the SQLite-backed queue across
reboots.

| Method | Path | Purpose | Notes |
|---|---|---|---|
| POST | `/api/v1/scheduler/notifications` | Schedule a notification | Body: `when`, `message`, `device_id` |
| GET | `/api/v1/scheduler/notifications` | List notifications | Query: `device_id`, `status` |
| GET | `/api/v1/scheduler/notifications/{id}` | Get one notification | — |
| PATCH | `/api/v1/scheduler/notifications/{id}` | Reschedule | Body: new `when` |
| DELETE | `/api/v1/scheduler/notifications/{id}` | Cancel a pending notification | — |

## Spend (`/api/v1/spend`)

| Method | Path | Purpose | Notes |
|---|---|---|---|
| GET | `/api/v1/spend?day=YYYY-MM-DD` | Daily LLM-spend roll-up | Aggregated over the `events` table; empty `day` = today UTC. Backed by `dragon_voice/billing/spend_tracker.py` (W5-A) |

## Agent skills (`/api/v1/agent_skills`)

| Method | Path | Purpose | Notes |
|---|---|---|---|
| GET | `/api/v1/agent_skills` | Catalog of available agentic skills | Merges 8 static OpenClaw core tools with the tool names observed in `agent_log`. Tab5 fetches it on Agents-overlay open and on voice-mode change (W7-B) |

## Integrations (`/api/v1/integrations`)

Third-party OAuth-backed providers (Google Calendar, Gmail, Tasks). Connect
flows are OAuth/PKCE or device-code; credentials are stored per-email so a
provider can hold multiple accounts.

| Method | Path | Purpose | Notes |
|---|---|---|---|
| POST | `/api/v1/integrations/connect` | Start a connect flow | Body: `provider` (+ `email` for multi-account). Returns `authorization_url` or the device-code triple |
| POST | `/api/v1/integrations/disconnect` | Revoke and forget a connection | Body: `provider`, `email`. Revokes tokens + deletes the email-keyed credentials file |
| GET | `/api/v1/integrations/list` | List integrations + connection state | Per-email state for multi-account providers |
| GET | `/api/v1/integrations/test/{provider}` | Smoke-test the active connection | Reads calendar / unread mail / tasks. Returns `{ok, detail}` |

Walkthrough: [connect-an-integration how-to](../how-to/connect-an-integration.md).

## Channels (`/api/v1/debug/channel_message`)

Lets a messaging platform (Telegram, WhatsApp, Discord, Slack, Signal,
iMessage, Matrix, Email) push a message to Tab5 through Dragon. The debug
endpoint fans a synthetic frame; the real round-trip runs through the
`GatewayConnector` to OpenClaw.

| Method | Path | Purpose | Notes |
|---|---|---|---|
| POST | `/api/v1/debug/channel_message?device_id=X` | Push a synthetic `channel_message` to a connected Tab5 | Fans a JSON frame over the existing voice WS; mirrors the `video_inject` shape. Records a `channel_push` entry in `agent_log` (W7-F) |

The user's reply path is not REST — it returns over the WebSocket as a
`channel_reply` frame (`channel_reply_handler` → `GatewayConnector.send_reply` →
OpenClaw → platform API → `channel_reply_ack`). See the
[WebSocket protocol reference](../protocol.md) §20.

## Examples

### List active sessions for a device

```bash
curl -s "http://192.168.1.91:3502/api/v1/sessions?device_id=AA:BB:CC:DD:EE:FF&status=active" \
  -H "Authorization: Bearer $DRAGON_API_TOKEN" | python3 -m json.tool
```

### Stream a chat reply (SSE)

```bash
curl -N -X POST http://192.168.1.91:3502/api/v1/sessions/3f9a1c0b7e22/chat \
  -H "Authorization: Bearer $DRAGON_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"content": "What is the weather?"}'
# → data: {"token": "It's"}
# → data: {"token": " sunny"}
# → data: [DONE]
```

### Transcribe a raw-PCM clip

```bash
curl -s -X POST http://192.168.1.91:3502/api/v1/transcribe \
  -H "Authorization: Bearer $DRAGON_API_TOKEN" \
  -H "Content-Type: application/octet-stream" \
  -H "X-Sample-Rate: 16000" \
  --data-binary @clip.pcm
# → {"text": "hello there", "duration_s": 1.2, "stt_ms": 340}
```

### Store and search a memory fact

```bash
curl -s -X POST http://192.168.1.91:3502/api/v1/memory \
  -H "Authorization: Bearer $DRAGON_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text": "Emile prefers metric units"}'

curl -s -X POST http://192.168.1.91:3502/api/v1/memory/search \
  -H "Authorization: Bearer $DRAGON_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query": "units", "limit": 3}' | python3 -m json.tool
```

### Execute a tool directly (no LLM)

```bash
curl -s -X POST http://192.168.1.91:3502/api/v1/tools/web_search/execute \
  -H "Authorization: Bearer $DRAGON_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query": "QCS6490 datasheet"}' | python3 -m json.tool
```

## Related

- [WebSocket protocol reference](../protocol.md) — the `/ws/voice` contract (frames, channel messages, video/audio relay)
- [Architecture](../ARCHITECTURE.md) — what runs where and how the pieces talk
- [SECURITY.md](../../SECURITY.md) — token lifecycle, public-prefix list, network exposure
- [Multi-model router cookbook](../router-cookbook.md) — fleet config behind the `router` backend
- [Swap the LLM backend](../how-to/swap-the-llm-backend.md) · [Add a tool](../how-to/add-a-tool.md) · [Connect an integration](../how-to/connect-an-integration.md)
