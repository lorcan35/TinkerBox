# Security

> Threat model, trust boundaries, auth-token lifecycle, data inventory,
> and disclosure path for TinkerClaw.

This is the Dragon-side security doc.  Tab5 firmware has its own
[SECURITY.md in TinkerTab](https://github.com/lorcan35/TinkerTab/blob/main/SECURITY.md)
covering the firmware-specific surface (debug HTTP server, USB/serial
access, OTA verification).

---

## TL;DR honest assessment

TinkerClaw is **a personal-use enthusiast project**, not a hardened
appliance.  The default deployment trusts the LAN, leans on
cloud-provider security for ngrok-exposed surfaces, runs ESP32-P4
firmware **without secure boot or flash encryption**, and persists
plaintext conversation history on Dragon.  This is fine for "home AI
assistant in my house" and not fine for "deploy this for users I
don't know."

If your threat model includes adversaries on your LAN, hostile
firmware modifications, or untrusted physical access to either device,
you'll need to layer additional protections.  This doc tells you
exactly where the boundaries are so you can decide.

---

## 1. Trust boundaries

```
┌───────────────────────────────────────────────────────────────┐
│ INTERNET (untrusted)                                          │
│   ┌─────────────────────────────┐                             │
│   │ ngrok tunnel:               │                             │
│   │  *.ngrok.dev → Dragon       │                             │
│   │  (TLS terminated by ngrok)  │                             │
│   └──────────────┬──────────────┘                             │
└──────────────────┼────────────────────────────────────────────┘
                   ↓ HTTPS, then plain HTTP localhost on Dragon
┌──────────────────┼────────────────────────────────────────────┐
│ LAN (assumed-trusted by default config)                       │
│                  ↓                                            │
│   ┌────────────────────────────┐    ┌──────────────────────┐  │
│   │ Dragon Q6A 192.168.1.91    │←──→│ Tab5 192.168.1.90    │  │
│   │  3500/3502 aiohttp         │ WS │  port 8080 debug srv │  │
│   │  18789 gateway (lo only)   │    │  (LAN-only)          │  │
│   │  11434 ollama   (lo only)  │    └──────────────────────┘  │
│   │  8888 searxng   (lo only)  │                              │
│   └─────┬──────────────────────┘                              │
│         ↓                                                     │
│   ┌────────────────────────────┐                              │
│   │ OpenRouter (cloud)         │  (Cloud / Hybrid mode only)  │
│   │  https://openrouter.ai/api │                              │
│   └────────────────────────────┘                              │
└───────────────────────────────────────────────────────────────┘
```

**Trust assumptions:**

| Boundary | What we trust | What we don't |
|----------|--------------|---------------|
| Internet → ngrok tunnel | TLS up to ngrok edge; ngrok's auth flow on the tunnel itself | ngrok's logging policy (treat as semi-public); free-tier replay via the ngrok dashboard |
| ngrok → Dragon | aiohttp bearer-token check on `/api/*` (per [`middleware/auth.py`](dragon_voice/middleware/auth.py)) | Anything in `PUBLIC_PREFIXES` (no auth) |
| LAN device → Dragon | Bearer token on `/api/*`, WS upgrade requires bearer too, deep-copied per-connection config | Default config trusts LAN — anyone on your WiFi can hit `/health`, `/call`, `/static/`, `/dashboard` |
| Dragon → ollama/searxng/gateway | localhost-only binding; no auth | Anyone with shell access on Dragon |
| Dragon → OpenRouter | `OPENROUTER_API_KEY` from `/home/radxa/.env`; HTTPS | OpenRouter sees full payload (audio, transcripts, photos) |
| Dragon → Tab5 (WS) | Bearer-auth on WS upgrade | TLS on LAN: NONE (plain `ws://` on LAN; `wss://` only via ngrok) |

---

## 2. Auth tokens

Three distinct tokens, all bearer-style, none currently rotated automatically.

### `DRAGON_API_TOKEN`

- **Purpose:** Gates Dragon's `/api/*` REST surface and the WS upgrade at `/ws/voice`.
- **Storage:** `/home/radxa/.env` on Dragon (loaded by systemd `EnvironmentFile=`). Survives `scp -r dragon_voice/` deploys because the env file isn't overwritten.
- **Generation:** Manually set by deployer on first install (`openssl rand -hex 32` typical). No auto-rotation.
- **Public endpoints (NO auth):** `/health` (no privacy concern), `/ws/voice` (the bearer is checked *during* the WS upgrade, not as a public-prefix bypass — see [`auth.py`](dragon_voice/middleware/auth.py)), `/dashboard`, `/api/media/{id}` (HMAC-signed URLs from `MediaUrlSigner` instead of bearer — W14-H04), `/call` + `/static/` (browser call client; sensitive routes stay under `/api/v1/*`).
- **If leaked:** Anyone with the token can: read all session history via `/api/v1/sessions/*`, store memory facts, ingest documents, send WS commands as the device. Rotate by editing `.env` + restarting `tinkerclaw-voice` + updating Tab5 NVS via `/settings`.

### `auth_tok` (Tab5 NVS)

- **Purpose:** Gates Tab5 firmware's debug HTTP server on port 8080 (LAN-only).
- **Storage:** Tab5 NVS partition, namespace `"settings"`, key `auth_tok`. 32 hex chars.
- **Generation:** Auto-generated on first boot via `esp_random()` if missing. Persists across reboots; only NVS erase rotates it.
- **Public endpoints (NO auth):** `/info` (uptime, heap, voice state — no secrets), `/selftest` (8-check health probe).
- **Logging:** Token print on serial log is *masked* (`05ee****b9f2`) to prevent accidental leak via screenshot/log paste. The unmasked value is recoverable via `esptool read_flash 0x9000 0x6000` if you have physical USB access — see TinkerTab's [`reference_tab5_debug_access.md`](https://github.com/lorcan35/TinkerTab/blob/main/docs/historical/) (ironically also lives in personal memory rather than a versioned doc — TODO).
- **If leaked:** Anyone on your LAN can drive the device remotely: tap, type, navigate, swap modes, take photos, reboot. NOT a path to Dragon.

### `OPENROUTER_API_KEY`

- **Purpose:** Cloud LLM + audio backend access.
- **Storage:** `/home/radxa/.env`.
- **If leaked:** Whoever has it can rack up your OpenRouter bill. Rotate via OpenRouter dashboard immediately. The `cap_mils` daily-budget enforcement on Tab5 only protects *you* from your own spending; it doesn't protect against an attacker.

### Other tokens

- **`TINKERCLAW_TOKEN`** (gateway) — localhost-only; mostly informational. Rotate by editing both `~/.tinkerclaw/tinkerclaw.json` and `dragon_voice/config.yaml`.
- **ngrok auth** — managed via `~/.ngrok/ngrok.yml`. ngrok's web dashboard shows tunnel traffic logs; treat them as semi-public.

---

## 3. Network exposure

By systemd unit, what's listening:

| Service | Port | Bind | Auth | ngrok-exposed? |
|---------|------|------|------|----------------|
| tinkerclaw-voice | 3502 | 0.0.0.0 (LAN) | `/api/*` + `/ws/voice` bearer; public prefixes have no auth | **Yes** (`tinkerclaw-voice.ngrok.dev`) |
| tinkerclaw-dashboard | 3500 | 0.0.0.0 (LAN) | proxies to 3502; bearer required on proxied calls | **Yes** (`tinkerclaw-dashboard.ngrok.dev`) |
| tinkerclaw-gateway | 18789 | 127.0.0.1 only | gateway-internal token | **Yes** (`tinkerclaw-gateway.ngrok.dev`) — be careful, this *is* tunnelled |
| ollama | 11434 | 127.0.0.1 only | none (localhost-trusted) | No |
| searxng | 8888 | 127.0.0.1 only | none | No |
| Chromium CDP | 9222 | 127.0.0.1 only | none | No |

**Implications for hostile-LAN scenarios:**
- Anyone on your WiFi can browse `http://192.168.1.91:3500` (dashboard) — proxied calls are bearer-auth-gated, but the dashboard *exists* publicly.
- Anyone on your WiFi can hit `http://192.168.1.91:3502/health` and `/call`.
- Anyone on your WiFi can attempt WS upgrades at `/ws/voice`; bearer rejection is constant-time so timing oracle is mitigated.
- The ngrok URLs are guessable if someone knows your account name. Bearer-token check still gates `/api/*`, so this is mostly a DoS / metadata-exposure concern, not unauthorized data access.

To harden: bind public services to specific interfaces, add a reverse proxy with mTLS, or move to `ngrok` paid tier with IP restrictions.

---

## 4. Data inventory

What gets persisted, where, and for how long.

### On Dragon

| Data | Storage | Retention | Cloud-exposed? |
|------|---------|-----------|----------------|
| Conversation messages (text, role, timestamps, model used, latency) | `~/tinkerclaw/tinkerclaw.db` SQLite, `messages` table | `database.message_retention_days` (default 30) | When voice_mode ≥ 1: prompts go to OpenRouter. The DB stays local. |
| Multimodal user content (photos, audio attachments) | `~/media/` files + `messages.content __mm__:` markers | 24-hour TTL via `lifecycle/purge.py: media_cleanup_loop` | Photos sent to OR with the prompt when in vision mode |
| Memory facts | `messages` table + `memory_facts` table (768-dim embeddings via nomic-embed-text) | Permanent unless deleted via API or memory-tab dashboard | Stays local unless cloud LLM is in the loop (which sees them in system prompt as RAG context) |
| Documents (ingested files) | `memory_documents` + `memory_chunks` tables | Permanent | Same as memory facts |
| Sessions | `sessions` table | `database.paused_session_retention_days` (default 30) | metadata only |
| Devices (registered Tab5s) | `devices` table | Permanent | metadata only |
| Events / audit log | `events` table | Same as messages | local |
| Notes (audio + transcripts) | `notes` table + audio files in `~/media/` | Until manually deleted | Transcribed via STT; cloud STT path sends audio to OpenRouter |

### Outbound to OpenRouter (Cloud / Hybrid modes)

When `voice_mode >= 1`:
- **STT:** Audio bytes → `openai/gpt-audio-mini` (16 kHz mono base64 WAV).
- **LLM:** Prompt + last 30 messages of context (incl. memory RAG, tool descriptions, multimodal content if vision).
- **TTS:** Text → audio.

OpenRouter's policy applies; per their docs they don't train on API traffic by default but log for abuse / billing.

### Outbound to Telegram (if `tinkerclaw-telegram` enabled)

- User messages from Telegram + bot replies. Stored at `/home/radxa/tinkerclaw/telegram/<chat_id>.json` for memory.

### Outbound to ngrok

- All traffic that passes through your tunnels is visible to ngrok. Traffic logs are accessible via ngrok dashboard.

---

## 5. Firmware integrity

### Tab5 (ESP32-P4)

- **Secure Boot:** **Disabled.** No bootloader signature verification. Anyone with USB-JTAG can flash arbitrary firmware.
- **Flash Encryption:** **Disabled.** NVS contents (incl. `auth_tok`, `wifi_pass`, `dragon_host`, etc.) are recoverable via `esptool read_flash`.
- **OTA Verification:** SHA256 hash check (`SEC07`, see CLAUDE.md). After download, the firmware is hashed and compared against `version.json` on Dragon. Mismatch → `ESP_ERR_INVALID_CRC` and abort. Hash comes from the same channel as the firmware though — so this protects against MitM on the OTA path *only if* `version.json` itself isn't compromised. We don't sign the OTA bundle.
- **OTA Rollback:** ESP-IDF auto-rollback enabled. New firmware boots in PENDING_VERIFY; if it crashes before `tab5_ota_mark_valid()`, bootloader reverts.

To harden Tab5 firmware integrity:
1. Enable Secure Boot V2 in sdkconfig (signed bootloader + app).
2. Enable Flash Encryption (encrypts the entire flash at rest).
3. Sign OTA bundles end-to-end with the same key.

This is a roughly 4-hour engineering effort + a key-management story we don't currently have.

### Dragon (Ubuntu ARM64)

- Standard Linux: SSH key auth, `radxa` user, sudo for systemd. No additional integrity verification of `dragon_voice/` itself; deploys are trust-on-first-deploy (you `scp` what you have).
- No code signing, no reproducible builds.

---

## 6. Known limitations / non-goals

These are honest gaps. Some are deliberate for an enthusiast project; some are tracked.

| Limitation | Tracked? | Workaround |
|-----------|----------|------------|
| Plain `ws://` on LAN (no encryption Tab5 ↔ Dragon) | Open issue | Use `conn_m=2` to force ngrok WSS; LAN traffic stays in your house |
| No bearer-token rotation | Not tracked | Manually rotate via `.env` + restart |
| No rate limiting on `/api/v1/*` beyond per-IP middleware (W15-H01) | Partially mitigated | Run on a private LAN only |
| `/dashboard` reachable from LAN without auth | By design (dashboard proxies to bearer-gated APIs) | Bind to localhost + reverse proxy if you don't want LAN access |
| ngrok tunnels expose service URLs to anyone who guesses the subdomain | By design (free tier limitation) | ngrok paid tier with IP restrictions; or close the tunnel when not needed |
| OpenRouter sees full conversation payloads in cloud mode | Inherent | Use Local mode for sensitive conversations |
| Tab5 firmware not signed | Tracked (Secure Boot V2 work item) | Don't put Tab5 in adversarial physical environments |
| MediaStore TTL of 24h could be shorter | Configurable | Edit `lifecycle/purge.py` |
| Memory facts can be RAG-injected into every cloud-mode call | By design | Use `forget` tool to remove specific facts; clear DB to wipe |

---

## 7. Reporting a vulnerability

This is a personal project; there's no formal SLA. That said:

- **For TinkerBox** (this repo): file a private issue or email the project owner. Don't open a public issue for findings that affect users in the wild (none currently exist publicly that I'm aware of).
- **For TinkerTab** (firmware): same path; cross-reference the issue in this repo.

Acknowledge time: best-effort, typically within a week. Patch time: depends on severity. We'll credit you in the changelog if you'd like.

If you find something that affects ngrok-exposed surfaces (WS auth bypass, RCE via the WS dispatcher, etc.), please don't post the PoC publicly until a patch ships.

---

## 8. What we got right (so you know where to focus auditing)

- WS bearer auth is constant-time (no timing oracle on credential rejection).
- Per-connection deep-copy of config (one device's `config_update` can't corrupt another's pipeline).
- HMAC-signed media URLs (W14-H04 — the `MediaUrlSigner` makes `/api/media/{id}` safe to publish in chat without exposing arbitrary blob access).
- Dictation auto-stop, mic mute, quiet-hours all enforced server-side too (the device can't silently override Dragon-side privacy decisions).
- Multi-model router's tier-policy + capability-decline surfaces hostile model routing as an explicit error rather than a silent failure.

---

## 9. Auditor's checklist (for hackers)

If you're doing a security review, this is the order I'd suggest:

1. **Read [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) + [`docs/protocol.md`](docs/protocol.md)** to understand what crosses what boundary.
2. **Audit [`dragon_voice/middleware/auth.py`](dragon_voice/middleware/auth.py)** — the auth gate is small (~100 lines).
3. **Map the public-prefix list** in `auth.py:PUBLIC_PREFIXES` and verify each is safe by design.
4. **Review the WS dispatcher** in `dragon_voice/server.py: _handle_ws_voice` — every `cmd_type == "..."` branch is reachable post-`register`. Look for un-validated input → tool execution / file paths / DB queries.
5. **Check `MediaStore.put_blob`** path traversal — file IDs are SHA-256 derived, but verify the validation chain.
6. **Walk a vision turn end-to-end** via [`docs/flows/vision-turn.md`](docs/flows/vision-turn.md) and check what data leaves Dragon and what gets persisted.
7. **Pull NVS via esptool from a Tab5** to confirm `auth_tok` extraction is as advertised; this is the LAN-attack escalation path if someone gets physical USB access.
8. **Read [`LEARNINGS.md`](LEARNINGS.md)** — many entries are post-mortems for what we got wrong. The same patterns may exist elsewhere.

If you find something, see Section 7 above.

---

## See also

- [TinkerTab `SECURITY.md`](https://github.com/lorcan35/TinkerTab/blob/main/SECURITY.md) — firmware-side counterpart.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — trust boundaries and component layout.
- [`docs/protocol.md`](docs/protocol.md) — full WS contract.
- [`LEARNINGS.md`](LEARNINGS.md) — the war stories.
