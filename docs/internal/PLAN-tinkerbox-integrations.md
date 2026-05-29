# PLAN: TinkerBox-native Integrations Layer

Status: **Shipped (Phases 1 & 2)** · last updated 2026-05-17
**Owner:** lorcan35
**Tracking:** TBD (parent issue) · child issues per integration

## Shipped

- [x] Phase 1: Google Calendar (#344)
- [x] Phase 1: Gmail single-account (#346) — pivoted from device-code to PKCE
- [x] Phase 2: Gmail multi-account with email-keyed credentials (#353)
- [x] Phase 2: Google Tasks (#102 backend + tools)
- [ ] Phase 3: Slack, Discord, Home Assistant (TBD)

## Why this exists

TinkerBox + TinkerTab today is fully functional as a voice assistant, but the agentic capability (web search, calendar, smart home, music, email, etc.) is concentrated in vmode=3 which routes through OpenClaw. That creates two problems:

1. **vmodes 0/1/2 are pure-chat without tools.** The user can ask "what's the weather?" in vmode=0 (Local Ollama) and get the answer, but can't ask "what's on my calendar today?" because no calendar tool exists at the Dragon level.
2. **Product positioning.** If every meaningful agent capability requires OpenClaw, TinkerClaw is effectively "OpenClaw with a Tab5 frontend." The user has explicitly said (2026-05-16) that TinkerClaw should have its own identity and not be totally dependent on OpenClaw.

The fix: build a first-class **Integrations** layer in Dragon that gives any vmode access to the table-stakes capabilities (Calendar, Email, Smart Home, Music, Maps, Translation), with OpenClaw remaining as an optional power-user backend for vmode=3.

## What this is NOT

* This is **not** a rewrite of OpenClaw — we will continue to support vmode=3 and the existing TinkerClaw gateway path.
* This is **not** an "ecosystem in a box" play — we are not building a skill marketplace ourselves. We ship a handful of well-curated integrations TinkerBox owns end-to-end.
* This is **not** for one-off third-party APIs — those still belong in OpenClaw's MCP layer.

## Architecture

### File layout

```
dragon_voice/
├── tools/
│   ├── registry.py                  # existing — Tool registration
│   ├── base.py                      # existing — Tool ABC
│   ├── integrations/                # NEW
│   │   ├── __init__.py
│   │   ├── base.py                  # IntegrationBackend ABC + lifecycle
│   │   ├── registry.py              # @register_integration decorator
│   │   ├── oauth.py                 # device-code flow + token-refresh helpers
│   │   ├── credentials.py           # ~/.tinkerclaw/integrations/{name}.json (0o600)
│   │   ├── google/
│   │   │   ├── __init__.py
│   │   │   ├── auth.py              # single Google OAuth client (Calendar + Gmail share)
│   │   │   ├── calendar.py          # GoogleCalendarIntegration
│   │   │   └── gmail.py             # GoogleGmailIntegration
│   │   ├── homeassistant/
│   │   │   ├── __init__.py
│   │   │   ├── adapter.py           # REST + websocket
│   │   │   └── entities.py          # entity cache + state polling
│   │   └── spotify/
│   │       ├── __init__.py
│   │       ├── auth.py
│   │       └── playback.py
│   ├── google_calendar_tool.py      # thin Tool wrapper -> GoogleCalendarIntegration
│   ├── gmail_tool.py
│   ├── ha_lights_tool.py
│   ├── ha_climate_tool.py
│   ├── ha_state_tool.py
│   └── spotify_tool.py
└── api/
    └── integrations.py              # NEW: REST routes
```

### Public REST surface

```
GET    /api/v1/integrations                     # list available + connection state
POST   /api/v1/integrations/{name}/connect      # start device-code flow
GET    /api/v1/integrations/{name}/status       # poll for completion
POST   /api/v1/integrations/{name}/disconnect   # revoke + delete tokens
GET    /api/v1/integrations/{name}/test         # smoke-test (read calendar, list lights, etc.)
```

### IntegrationBackend ABC

```python
class IntegrationBackend(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...                              # "google-calendar", "homeassistant", ...

    @property
    @abstractmethod
    def display_name(self) -> str: ...                      # "Google Calendar"

    @property
    @abstractmethod
    def auth_kind(self) -> Literal["oauth-device", "static-token", "none"]: ...

    @abstractmethod
    async def is_connected(self) -> bool: ...

    @abstractmethod
    async def start_connect(self) -> ConnectChallenge:      # device-code triple OR token-entry prompt
        ...

    @abstractmethod
    async def poll_status(self) -> ConnectionStatus: ...    # for async device-code flow

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    async def health_check(self) -> tuple[bool, str]: ...   # smoke test
```

### OAuth device-code flow (RFC 8628)

Dragon is headless. Standard "redirect back to localhost" OAuth doesn't work. Solution: device authorization grant.

1. User taps "Connect Google" on Tab5 Settings → Integrations.
2. Dragon hits Google's device-code endpoint → receives `verification_url`, `user_code`, `device_code`.
3. Tab5 displays:
   * A QR code containing `verification_url?user_code=ABC-123`.
   * The plain code `ABC-123` as a fallback for manual entry.
4. User scans QR on phone, signs in, types code if not already filled.
5. Dragon polls Google's token endpoint with `device_code` every 5 s until tokens returned.
6. Tokens saved to `~/.tinkerclaw/integrations/google.json` mode `0o600`.
7. Every API call goes through a refresh-on-401 wrapper.

Google + Spotify both support this flow natively. Other providers (Notion, Linear) may need PKCE + a small HTTP listener — we'll cross that bridge per-integration.

### Cross-vmode tool exposure

The new tools live in `ToolRegistry` like any other Dragon tool, so they're automatically available to:
* **vmode=0** (Local Ollama) — local LLM emits tool markers, Dragon's ConversationEngine executes.
* **vmode=1** (Hybrid) — same.
* **vmode=2** (Cloud) — Claude / GPT do tool calling natively; ConversationEngine wires the schema.
* **vmode=3** (TinkerClaw / OpenClaw) — Dragon exposes the same tools through a local MCP server (`tinkerbox-mcp` on e.g. `localhost:3503`); OpenClaw is configured to consume it. So vmode=3 sees TinkerBox tools alongside OpenClaw's own MCPs.

This is the key architectural promise: **a tool added once is available everywhere**.

## Integration shortlist for v1

Order driven by user value and dependency chain.

| # | Integration | Tools added | OAuth? | Notes |
|---|-------------|-------------|--------|-------|
| 1 | **Infrastructure** | (none — package skeleton) | — | Ships with Google Calendar as the proof-of-concept tool. |
| 2 | **Google Calendar** | `calendar_today`, `calendar_week`, `calendar_create`, `calendar_cancel` | Device-code | First integration. Single Google OAuth client covers Calendar + Gmail. |
| 3 | **Gmail** | `gmail_unread_count`, `gmail_read_latest`, `gmail_search`, `gmail_send`, `gmail_archive` | Reuses #2 | Read-mostly with `gmail.send` scope opt-in. |
| 4 | **Home Assistant** | `ha_light_on`, `ha_light_off`, `ha_get_temp`, `ha_set_temp`, `ha_lock_door`, `ha_get_state`, `ha_list_entities` | Long-lived token (no OAuth) | LAN-local — works in vmode=0. |
| 5 | **Currency converter** | `currency_convert` | None | Free API (exchangerate.host). |
| 6 | **Translate** | `translate` | None | LLM passthrough or Argos Translate for offline. |
| 7 | **OSRM maps** | `directions`, `geocode` | None | Self-hosted OSRM or public endpoint + Nominatim. |
| 8 | **Spotify** | `spotify_play`, `spotify_pause`, `spotify_skip`, `spotify_queue`, `spotify_devices` | Device-code | Audio routing to Tab5 speaker handled separately (existing TTS-like path). |
| 9 | **Notion** | `notion_search`, `notion_create_page`, `notion_append` | OAuth (PKCE) | Optional — only if user uses Notion. |

## Tab5 UX: Settings → Integrations

New section in `ui_settings.c`. Lists each available integration with:
* Display name + icon
* Status badge: ✓ Connected · ☐ Not connected · ⚠ Needs reauth · ✗ Error
* Last-tested timestamp
* Actions: Connect / Disconnect / Test

Connect modal (per-integration):
* **OAuth device-code** — QR code + plain code + "On your phone, visit `google.com/device` and enter `ABC-123`." Tab5 polls Dragon every 2 s until success.
* **Static token** — Plain input field for HA URL + token. Save → smoke-test → confirmed.
* **None** — Just enable / disable toggle.

Discoverability touch (related: orb-focused UI memory): when a new integration is connected, the home screen shows a one-time "Try: 'what's on my calendar?'" example chip near the orb for the next 24 h.

## "Stealing from OpenClaw" — fair use

OpenClaw is MIT-licensed. For each integration we want:

1. Read the OpenClaw MCP implementation (`extensions/*` and `src/agents/openclaw-tools.ts`).
2. Understand API surface choices (which Calendar fields exposed, which Gmail scopes, etc.).
3. Reimplement in Python in TinkerBox. Don't 1:1 port — adapt to Dragon's tool patterns and Python idioms.
4. Credit the inspiration in commit message: "Pattern adapted from openclaw/mcp-server-gmail".

This is fair-use, expected behavior in open-source, and avoids inheriting upstream bugs.

## Out of scope (explicitly)

* **Multi-user / voiceprint** — Tab5 device is single-tenant for v1.
* **Video calling** — separate path, already shipped.
* **Skill marketplace** — TinkerBox doesn't host a marketplace. Users wanting the broad ecosystem use OpenClaw via vmode=3.
* **Replacing OpenClaw's channels** — Telegram/WhatsApp/etc. push notifications stay on the W7-F GatewayConnector path.

## Open questions

1. **Spotify audio routing** — Spotify Connect requires a registered Spotify Connect device. Tab5 isn't one. Possible workaround: Dragon runs `librespot` and pipes audio to Tab5 as PCM via the existing voice-WS path. Needs investigation.
2. **Notion OAuth uses PKCE not device-code** — need a small HTTP listener on Dragon (port forwarded via ngrok? or proxied through OpenClaw's existing ngrok)?
3. **HomeAssistant discovery** — auto-discover the HA URL via mDNS, or require manual entry? mDNS is friendlier but Dragon is on a different network sometimes.
4. **Per-tool consent prompts** — should we add a G9-style confirm-gate for write tools (`calendar_create`, `gmail_send`, `ha_lock_door`)? Probably yes — surface as a "this action will…" toast on Tab5 before executing.

## Phasing

* **Phase 1 (infrastructure + Google Calendar)** — single PR. ~600 LOC. Closes parent issue's "infra" milestone.
* **Phase 2 (Gmail)** — PR. Reuses Phase 1's Google auth. ~250 LOC.
* **Phase 3 (HomeAssistant)** — PR. ~400 LOC including entity cache.
* **Phase 4 (currency + translate)** — small PR. ~150 LOC.
* **Phase 5 (OSRM)** — PR. ~250 LOC.
* **Phase 6 (Spotify)** — PR. Bigger because of audio routing. ~400 LOC.
* **Phase 7 (Notion)** — PR if user wants. ~250 LOC.

Each phase ships independently — no big-bang merge.

## Success criteria

After Phase 3 (the "tonight feels different" milestone):

* "What's on my calendar today?" — works in vmodes 0/1/2/3.
* "Do I have any unread emails from John?" — works.
* "Turn on the kitchen lights." — works in vmode=0 (offline) because HA is LAN-local.
* TinkerBox Settings → Integrations shows 3 connected services.
* OpenClaw is still usable for vmode=3 but no longer required for basic agentic UX.

## Related

* Memory: `~/.claude/projects/-home-rebelforce/memory/project_tinkerbox_integrations_layer.md`
* Feedback: `~/.claude/projects/-home-rebelforce/memory/feedback_tinkerbox_not_fully_openclaw_dependent.md`
* Open issue: [TBD parent tracking issue]
* Sister doc: `docs/protocol.md` will need new Tab5 → Dragon WS messages if integrations need to push to Tab5 (e.g. incoming-call event from HA doorbell).

## Architecture (as shipped — Phases 1 & 2)

The plan above describes the original device-code design.  The
shipped surface diverged on a few key points; this section is the
canonical reference for what actually exists on Dragon today.

### OAuth flow — PKCE, not device-code

Google's device-code grant proved fragile against the
unverified-app verification wall (issue #103) and the production
Calendar / Gmail / Tasks scopes — Google required public-client +
PKCE for the scope combinations we wanted.  All three Google
integrations (Calendar, Gmail, Tasks) ship with **PKCE + loopback
redirect** instead:

1. Tab5 taps "Connect" → Dragon `POST /api/v1/integrations/connect`
   returns an `authorization_url` with `code_verifier` retained
   server-side.
2. The URL contains a `state` nonce + a loopback redirect URI
   (`http://127.0.0.1:<port>/oauth/callback`) that Dragon binds
   ephemerally for the flow.
3. User completes the consent flow in a browser; Google redirects to
   Dragon's loopback listener with the auth code.
4. Dragon exchanges the code + `code_verifier` for tokens, persists
   them under the email-keyed credentials layout below, and emits
   a connection-state event.

### Multi-account credential storage layout

`~/.tinkerclaw/integrations/<provider>/` per-provider directory.
Inside each provider directory:

- `<email>.json` — one credentials file per Google account
  (mode `0o600`, atomic write).  Token-refresh writes back through
  the same path.
- `_legacy.json` — migration shim that captures the original
  single-account credentials format from Phase 1 (#346) so a Dragon
  upgrading from single-account → multi-account (#353) doesn't lose
  its existing connection.  Loader checks `_legacy.json` first and
  promotes it to `<email>.json` on first successful refresh.

This shape supports the user's "connect both my work and personal
Gmail" use case without forcing a re-pair on upgrade.

### REST endpoint family

Routes live at `/api/v1/integrations/*`:

- `POST /api/v1/integrations/connect` — start a connect flow for
  the given `provider`.  Returns `authorization_url` (PKCE) or the
  device-code triple (for providers that still use device-code).
- `POST /api/v1/integrations/disconnect` — revoke tokens + delete
  the credentials file for `<provider, email>`.
- `GET  /api/v1/integrations/list` — list available integrations
  with connection state (per-email for multi-account providers).
- `GET  /api/v1/integrations/test/{provider}` — smoke-test the
  active connection (read calendar / list unread / list tasks /
  etc.).  Returns `{ok: bool, detail: "..."}`.
