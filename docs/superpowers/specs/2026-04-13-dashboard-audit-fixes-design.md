# Dashboard Audit Fixes & New Features — Design Spec

**Date:** 2026-04-13
**Scope:** Fix 6 bugs + add 2 new features to TinkerClaw Dashboard (dashboard.py)
**File:** `/home/rebelforce/projects/TinkerBox/dashboard.py` (2,180 lines, single-file SPA)

## Context

Dashboard audit found 14/15 API endpoints working. 6 bugs affect core functionality. 2 new features requested (OTA tab, Device Config push). All changes are in the single `dashboard.py` file — Python backend (~130 lines) + embedded HTML/CSS/JS (~2,000 lines).

## Bug Fixes

### Fix 1: Notes Tab 404 (CRITICAL)

**Problem:** Dashboard calls `/api/proxy/api/notes` but the voice server's notes routes are registered at `/api/notes` (no `/v1/` prefix) via the notes module. The proxy strips `/api/proxy/` and forwards to `http://127.0.0.1:3502/api/notes` — which should work. But the voice server may only register notes under a different path.

**Investigation needed:** Check where notes routes are registered in `server.py` (`setup_notes_routes(app, notes_svc)`) and what path prefix they use. The 404 suggests the notes API routes aren't being hit by the proxy.

**Fix approach:** 
- Verify the exact notes route paths on voice server (3502)
- If routes are at `/api/notes`, the proxy should work — debug why 404
- If the dashboard is calling the wrong path, fix the JS `api()` calls

### Fix 2: Config POST Not Proxied (CRITICAL)

**Problem:** Line 1149 in JS: `fetch('/api/voice-config', {...})` POSTs to the dashboard's own endpoint, not through the proxy to the voice server.

**Fix:** Change to `fetch(P + '/api/voice-config', {...})` where `P = '/api/proxy'`. The dashboard already has a GET handler for `/api/voice-config` that proxies correctly — the POST handler at line 2169 also proxies. But the JS bypasses the proxy prefix for the POST. Just add the `P +` prefix.

### Fix 3: CORS Headers on Proxy Responses (HIGH)

**Problem:** Only SSE streaming responses get `Access-Control-Allow-Origin: *`. Regular JSON proxy responses don't.

**Fix:** Add CORS headers dict to the `web.Response()` return in `_proxy_request()` at line ~120:
```python
return web.Response(
    body=resp_body,
    status=resp.status,
    content_type=...,
    headers={"Access-Control-Allow-Origin": "*"},
)
```

### Fix 4: Tool Execute Double-Click (HIGH)

**Problem:** Execute button not disabled during API call.

**Fix:** In `executeTool()` JS function:
- Disable button + show "Executing..." text on click
- Re-enable on response (success or error)
- Add `pointer-events: none` during loading

### Fix 5: Loading States (UX)

**Problem:** No visual feedback during API calls across all tabs.

**Fix:** Add a global `setLoading(elementId, loading)` helper that:
- Adds/removes `.loading` class (shows spinner overlay)
- Disables interactive elements inside the container
- CSS: `.loading { opacity: 0.6; pointer-events: none; }` with spinner pseudo-element

Apply to: Overview refresh, session list load, chat send, device list, notes list, memory list, document list, tool list, event poll.

### Fix 6: Error Toasts (UX)

**Problem:** Silent error handling on config load, event polling, and other API failures.

**Fix:** Add a toast notification system:
- `showToast(message, type='error'|'success'|'info')` function
- Fixed position bottom-right, auto-dismiss after 5s
- CSS: dark surface with colored left border (red=error, green=success, cyan=info)
- Apply to all `catch(e)` blocks that currently swallow errors silently

## New Features

### Feature 1: OTA Firmware Update Tab

**Tab name:** "OTA" (10th tab, after Logs)

**API endpoints used:**
- `GET /api/ota/check?current=VERSION` → `{update: bool, version: string, url: string, sha256: string}`
- `GET /api/ota/firmware.bin` → binary firmware download (for reference, not used by dashboard)

**UI design (polished, matching existing dark theme):**
- Card layout matching Overview tab style
- **Current firmware section:** Shows device firmware version from device info, partition info (ota_0/ota_1)
- **Check for updates button:** Calls OTA check endpoint, shows result
- **Update available card:** Green accent, shows new version + size + SHA256
- **Apply Update button:** Triggers Tab5 OTA apply via debug server (`POST http://TAB5_IP:8080/ota/apply`)
- **Progress indicator:** Polling-based progress bar (Tab5 reports download %)
- **Rollback info:** Shows current partition, boot count, rollback status

### Feature 2: Device Config Push

**Location:** Devices tab, expanded device detail card

**UI design:**
- New "Configuration" section inside each device's expandable details
- **Voice Mode selector:** Dropdown (Local / Hybrid / Full Cloud)
- **LLM Model picker:** Dropdown populated from backends API (enabled only in Cloud mode)
- **Apply Config button:** Sends config_update via voice WS or REST API
- **Status feedback:** Shows "Applied" or error message

**API approach:** 
- Use `POST /api/proxy/api/v1/config` or send via the voice WebSocket
- Tab5 receives `config_update` over its existing WS connection
- Dashboard shows confirmation when Dragon ACKs the config change

## Implementation Order

1. Fix 2 (Config POST) — one line change
2. Fix 3 (CORS) — one line change  
3. Fix 4 (Tool double-click) — 5 lines
4. Fix 6 (Error toasts) — add toast system (~30 lines CSS + JS)
5. Fix 5 (Loading states) — add helper + apply to all tabs (~50 lines)
6. Fix 1 (Notes 404) — investigate + fix route
7. Feature 1 (OTA tab) — new tab HTML + JS (~150 lines)
8. Feature 2 (Device Config) — add to devices tab (~100 lines)

## Verification

After implementation:
1. **Notes tab:** Navigate to Notes, see note cards load. Create a note. Search.
2. **Config apply:** Change LLM model in Overview → Apply → verify voice server config changed via `/health`
3. **CORS:** Fetch from a different origin (browser console on another domain) → no CORS error
4. **Tool execute:** Click Execute rapidly → only one request sent
5. **Loading states:** Slow network simulation → spinners visible during fetches
6. **Error toasts:** Stop voice server → dashboard shows error toast on next refresh
7. **OTA tab:** Navigate to OTA → Check Update → see result
8. **Device config:** Expand Tab5 → change voice mode → verify Tab5 switches

## Critical Files

| File | Change |
|------|--------|
| `dashboard.py` | ALL changes — single file SPA |
| `dragon_voice/server.py` | Verify notes route registration |
| `dragon_voice/api/synthesize.py` | Verify OTA endpoint paths |
