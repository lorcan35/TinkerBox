---
audience: operator
type: how-to
prerequisites: [Deploy on a Dragon](deploy-on-a-dragon.md), [API-First Architecture in CLAUDE.md](../../CLAUDE.md#api-first-architecture-62-rest-endpoints--1-websocket)
last-verified: 2026-05-29
est-time: 15 min
---
# Connect a Google integration

Use this when you need to connect a Google account — Calendar, Gmail, or
Tasks — to the [Dragon](../../GLOSSARY.md) so that the assistant's
`calendar_*`, `gmail_*`, and `tasks_*` tools start returning real data. These
tools are Dragon-native: once a provider is connected they work in every voice
mode that runs through Dragon's `ConversationEngine` (Local, Hybrid, Full
Cloud), not just the TinkerClaw sidecar. Assumes you can reach the Dragon's
REST API on port 3502 and have its bearer token.

The connect flow is **PKCE with a loopback redirect**. The original plan called
for the OAuth device-code grant (RFC 8628), but Google's verification wall
rejected device-code for the production Calendar / Gmail / Tasks scope
combination, so all three Google integrations ship on public-client PKCE
instead. Credentials are **keyed by email address**, so you can connect both a
work and a personal Google account to the same provider.

## The endpoint family

All four routes live under `/api/v1/integrations/` on port 3502. They sit
behind the Wave 13 bearer-token gate, so every call needs an
`Authorization: Bearer <token>` header (the `DRAGON_API_TOKEN` value from
`/home/radxa/.env`).

| Method | Path | Does |
|--------|------|------|
| `GET` | `/api/v1/integrations/list` | Lists available integrations + per-email connection state |
| `POST` | `/api/v1/integrations/connect` | Starts a connect flow; returns an `authorization_url` (PKCE) |
| `POST` | `/api/v1/integrations/disconnect` | Revokes tokens + deletes the credentials file for a `provider, email` |
| `GET` | `/api/v1/integrations/test/{provider}` | Smoke-tests the active connection; returns `{ok, detail}` |

The valid `provider` values are `google-calendar`, `google-gmail`, and
`google-tasks`. The three share one Google OAuth client, so the consent screen
covers whichever scopes the provider you are connecting needs.

## Before you start

Resolve the Dragon's current address — the LAN flips between the `192.168.1.x`
and `192.168.70.x` subnets and the host is DHCP-managed, so never assume a
hardcoded IP:

```bash
ping radxa-dragon-q6a
# → 64 bytes from 192.168.70.242: icmp_seq=1 ttl=64 time=1.2 ms
```

The examples below use `192.168.70.242` (verified 2026-05-04) and read the
bearer token into a shell variable so you do not paste it on every line:

```bash
DRAGON=192.168.70.242
TOKEN=$(ssh radxa@$DRAGON "grep -oP 'DRAGON_API_TOKEN=\K.*' /home/radxa/.env")
```

## Steps

### 1. See what is available and what is already connected

```bash
curl -s http://$DRAGON:3502/api/v1/integrations/list \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

```json
{
  "integrations": [
    {"provider": "google-calendar", "display_name": "Google Calendar",
     "connected": false, "accounts": []},
    {"provider": "google-gmail", "display_name": "Gmail",
     "connected": true, "accounts": ["emilesawayame@gmail.com"]},
    {"provider": "google-tasks", "display_name": "Google Tasks",
     "connected": false, "accounts": []}
  ]
}
```

`accounts` is per-email — a multi-account provider lists every Google address
you have connected to it.

### 2. Start the connect flow

`POST /api/v1/integrations/connect` with the provider you want. The Dragon
generates a PKCE `code_verifier`, retains it server-side, binds an ephemeral
loopback listener on `http://127.0.0.1:<port>/oauth/callback`, and returns the
Google consent URL with the matching `code_challenge` and a `state` nonce:

```bash
curl -s -X POST http://$DRAGON:3502/api/v1/integrations/connect \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"provider": "google-calendar"}' | python3 -m json.tool
```

```json
{
  "provider": "google-calendar",
  "authorization_url": "https://accounts.google.com/o/oauth2/v2/auth?client_id=...&code_challenge=...&state=...&redirect_uri=http%3A%2F%2F127.0.0.1%3A<port>%2Foauth%2Fcallback",
  "expires_in": 600
}
```

On a Tab5 this is the moment the **Settings → Integrations** screen renders the
URL as a QR code so you can scan it with a phone. From the command line, open
`authorization_url` in any browser.

### 3. Complete consent in the browser

Sign in to the Google account you want, grant the requested scopes, and let
Google redirect back to the Dragon's loopback listener with the authorization
code. The Dragon exchanges the code plus its retained `code_verifier` for
tokens, then writes them to disk keyed by the account's email address:

```
~/.tinkerclaw/integrations/<provider>/<email>.json   # mode 0o600, atomic write
```

A token-refresh writes back through the same path, so a connection survives
restarts and refreshes silently on a 401.

> **The unverified-app warning is expected.** If the OAuth client is still in
> Google's testing/unverified state you will see an "Google hasn't verified
> this app" interstitial. Click **Advanced → Go to (unsafe)** to proceed, and
> make sure the Google account is on the OAuth client's test-user allowlist —
> otherwise consent fails with `access_denied`. This verification wall is the
> reason the flow is PKCE and not device-code (issue #103).

### 4. Confirm the tokens landed

```bash
ssh radxa@$DRAGON "ls -l ~/.tinkerclaw/integrations/google-calendar/"
# → -rw------- 1 radxa radxa 1834 May 29 14:02 emilesawayame@gmail.com.json
```

The file is `0o600` — owner-only — so it is never world-readable. If you see a
`_legacy.json` alongside the email-keyed file, that is the single-account
migration shim from the Phase 1 storage layout; the loader promotes it to the
email-keyed name on the first successful refresh.

## Verify it worked

Smoke-test the live connection. `GET /api/v1/integrations/test/{provider}`
makes one real read against Google (list today's calendar, count unread mail,
or list tasks, depending on the provider) and returns whether it succeeded:

```bash
curl -s http://$DRAGON:3502/api/v1/integrations/test/google-calendar \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
# → {"ok": true, "detail": "3 events today"}
```

Then confirm `list` now shows the provider connected with your email under
`accounts`:

```bash
curl -s http://$DRAGON:3502/api/v1/integrations/list \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
# → "provider": "google-calendar", "connected": true, "accounts": ["emilesawayame@gmail.com"]
```

Finally, prove the tool is wired into the assistant. Run a stateless turn that
should trigger the calendar tool and watch it fire in the logs:

```bash
curl -s -X POST http://$DRAGON:3502/api/v1/completions \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "What is on my calendar today?"}'

ssh radxa@$DRAGON "journalctl -u tinkerclaw-voice -n 40 --no-pager | grep -i tool"
# → ToolRegistry.execute calendar_today ... ok
```

You can also watch tool activity on the dashboard's agent feed or via
`GET /api/v1/agent_log` — the calendar/gmail/tasks call lands there with a
`source: dragon` tag.

## Disconnecting

`POST /api/v1/integrations/disconnect` revokes the tokens with Google and
deletes the email-keyed credentials file. For a multi-account provider you must
name the email, otherwise the Dragon does not know which account to drop:

```bash
curl -s -X POST http://$DRAGON:3502/api/v1/integrations/disconnect \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"provider": "google-gmail", "email": "emilesawayame@gmail.com"}'
# → {"ok": true}
```

## Troubleshooting

- **Every call returns `401 Unauthorized`** → the `/api/v1/*` surface is behind
  the bearer-token gate. Send `Authorization: Bearer <DRAGON_API_TOKEN>` (the
  value lives in `/home/radxa/.env`, loaded by the systemd `EnvironmentFile=`).
  A blank `$TOKEN` is the usual cause — re-read it from `.env`.

- **Consent fails with `access_denied` on an unverified app** → the Google
  account is not on the OAuth client's test-user allowlist. Add it in the Google
  Cloud Console OAuth consent screen, or push the client through verification.
  This is the verification wall that forced PKCE over device-code (issue #103).

- **`authorization_url` works but the redirect never completes** → the loopback
  listener is bound only for the life of the flow (`expires_in` seconds, 600 by
  default) on `127.0.0.1`. If you opened the URL on a different machine, the
  browser's redirect to `http://127.0.0.1:<port>/oauth/callback` hits *that*
  machine's loopback, not the Dragon's. Complete consent in a browser on the
  Dragon, or use the Tab5 QR flow which round-trips through Dragon.

- **`test/{provider}` returns `{"ok": false, ...}` after a working connect** →
  the refresh token was revoked (you removed the app's access in your Google
  account, or the token expired while unused). Re-run the connect flow; the new
  tokens overwrite the email-keyed file in place.

- **Tools fire but return stale or empty data on a small Local model** → the
  connection is fine; this is the model, not the integration. On the
  hard-prose gauntlet small Local models confuse tool selection and arg
  extraction. Confirm with `test/{provider}` (which bypasses the LLM) before
  blaming the integration, and see the Local-model notes in
  [Swap the LLM backend](swap-the-llm-backend.md).

- **Connected the wrong Google account** → disconnect that specific email (see
  [Disconnecting](#disconnecting)), then connect again and sign in with the
  account you want. Credentials are email-keyed, so connecting a second account
  does not evict the first.

## Related

- [Deploy on a Dragon](deploy-on-a-dragon.md) — get a working tree onto the
  Dragon and confirm `tinkerclaw-voice` is healthy before connecting anything.
- [Swap the LLM backend](swap-the-llm-backend.md) — which model actually decides
  to call the `calendar_*` / `gmail_*` / `tasks_*` tools, and how tool-calling
  is wired per backend.
- [WebSocket protocol](../protocol.md) — the Tab5 ↔ Dragon contract the Tab5
  Settings → Integrations screen rides on.
- [`CLAUDE.md`](../../CLAUDE.md) — the full operator runbook, including the
  complete REST endpoint table.
