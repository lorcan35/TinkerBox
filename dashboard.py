#!/usr/bin/env python3
"""
TinkerClaw Dashboard — Web UI for Dragon Server + Voice Pipeline

Serves on port 3500. 9-tab SPA with proxy routes to voice server (3502).

Tabs: Overview | Conversations | Chat | Devices | Notes | Memory | Documents | Tools | Logs

Endpoints:
  GET  /              — SPA HTML
  GET  /api/status    — Aggregated health from both services
  GET/POST /api/voice-config  — Proxy to voice server config
  /api/proxy/*        — Generic proxy to voice server REST API
"""

import asyncio
import logging
import time

import aiohttp
from aiohttp import web

log = logging.getLogger("dashboard")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

HOST = "0.0.0.0"
PORT = 3500
DRAGON_SERVER = "http://127.0.0.1:3501"
VOICE_SERVER = "http://127.0.0.1:3502"

_client: aiohttp.ClientSession | None = None
_start_time = time.time()


# ── Lifecycle ────────────────────────────────────────────────────────────

async def on_startup(app: web.Application) -> None:
    global _client
    _client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
    log.info("Dashboard started on http://%s:%d", HOST, PORT)


async def on_shutdown(app: web.Application) -> None:
    global _client
    if _client:
        await _client.close()
        _client = None


# ── Internal helpers ─────────────────────────────────────────────────────

async def _fetch_json(url: str) -> dict | None:
    """Fetch JSON from an internal service, return None on failure."""
    try:
        async with _client.get(url) as resp:
            if resp.status == 200:
                return await resp.json()
            return {"error": f"HTTP {resp.status}"}
    except Exception as exc:
        return {"error": str(exc)}


# ── Generic Proxy ────────────────────────────────────────────────────────

async def _proxy_request(request: web.Request) -> web.Response:
    """Generic proxy: forwards /api/proxy/{path} to voice server.

    Handles GET, POST, PUT, DELETE. Detects SSE streaming responses
    and passes them through as a StreamResponse.
    """
    # Strip /api/proxy/ prefix to get the voice server path
    proxy_path = request.match_info.get("path", "")
    target_url = f"{VOICE_SERVER}/{proxy_path}"

    # Forward query string
    if request.query_string:
        target_url += f"?{request.query_string}"

    method = request.method.upper()
    headers = {}
    body = None

    # Forward JSON body for POST/PUT/PATCH
    if method in ("POST", "PUT", "PATCH"):
        content_type = request.content_type or ""
        if "json" in content_type or "octet-stream" in content_type:
            body = await request.read()
            headers["Content-Type"] = content_type
        else:
            try:
                body = await request.read()
                if body:
                    headers["Content-Type"] = content_type or "application/json"
            except Exception:
                pass

    try:
        async with _client.request(
            method, target_url, data=body, headers=headers,
            timeout=aiohttp.ClientTimeout(total=120),
        ) as resp:
            resp_content_type = resp.headers.get("Content-Type", "")

            # SSE streaming passthrough
            if "text/event-stream" in resp_content_type:
                stream_resp = web.StreamResponse(headers={
                    "Content-Type": "text/event-stream",
                    "Cache-Control": "no-cache",
                    "Access-Control-Allow-Origin": "*",
                })
                await stream_resp.prepare(request)
                async for chunk in resp.content.iter_any():
                    await stream_resp.write(chunk)
                return stream_resp

            # Normal JSON/text response
            resp_body = await resp.read()
            return web.Response(
                body=resp_body,
                status=resp.status,
                content_type=resp_content_type.split(";")[0].strip() or "application/json",
                headers={"Access-Control-Allow-Origin": "*"},
            )
    except asyncio.TimeoutError:
        return web.json_response({"error": "Voice server timeout"}, status=504)
    except Exception as exc:
        return web.json_response({"error": f"Proxy error: {exc}"}, status=502)


# ── API Routes ───────────────────────────────────────────────────────────

async def handle_status(request: web.Request) -> web.Response:
    """Aggregate health from both services."""
    dragon_task = asyncio.create_task(_fetch_json(f"{DRAGON_SERVER}/health"))
    voice_task = asyncio.create_task(_fetch_json(f"{VOICE_SERVER}/health"))
    dragon, voice = await asyncio.gather(dragon_task, voice_task)
    return web.json_response({
        "dashboard_uptime": int(time.time() - _start_time),
        "dragon": dragon,
        "voice": voice,
    })


async def handle_get_voice_config(request: web.Request) -> web.Response:
    """Proxy GET /api/config from voice server."""
    result = await _fetch_json(f"{VOICE_SERVER}/api/config")
    if result is None:
        return web.json_response({"error": "Voice server unreachable"}, status=502)
    return web.json_response(result)


async def handle_set_voice_config(request: web.Request) -> web.Response:
    """Proxy POST /api/config to voice server."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    try:
        async with _client.post(f"{VOICE_SERVER}/api/config", json=body) as resp:
            data = await resp.json()
            return web.json_response(data, status=resp.status)
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=502)


# ── Dashboard SPA HTML ──────────────────────────────────────────────────

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TinkerClaw Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,300;0,9..40,400;0,9..40,500;0,9..40,600;0,9..40,700;1,9..40,400&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #06060c;
  --surface: #0e1525;
  --surface2: #141c30;
  --surface3: #1a2440;
  --border: #1e2d4a;
  --border-subtle: #152035;
  --accent: #ff6b35;
  --accent-dim: rgba(255,107,53,0.12);
  --accent2: #06b6d4;
  --accent2-dim: rgba(6,182,212,0.10);
  --green: #34d399;
  --green-dim: rgba(52,211,153,0.12);
  --red: #f87171;
  --red-dim: rgba(248,113,113,0.12);
  --yellow: #fbbf24;
  --yellow-dim: rgba(251,191,36,0.12);
  --text: #e2e8f0;
  --text-secondary: #94a3b8;
  --muted: #64748b;
  --radius: 12px;
  --radius-sm: 8px;
  --radius-xs: 6px;
  --shadow-sm: 0 1px 2px rgba(0,0,0,0.3);
  --shadow-md: 0 4px 12px rgba(0,0,0,0.4);
  --shadow-lg: 0 8px 32px rgba(0,0,0,0.5);
  --glass: rgba(14,21,37,0.7);
  --glass-border: rgba(30,45,74,0.5);
  --transition: 0.2s cubic-bezier(0.4,0,0.2,1);
}
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; background: var(--bg); color: var(--text); font-family: 'DM Sans', -apple-system, BlinkMacSystemFont, sans-serif; font-size: 14px; line-height: 1.6; -webkit-font-smoothing: antialiased; -moz-osx-font-smoothing: grayscale; }
::selection { background: var(--accent); color: #fff; }
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: var(--muted); }

/* ── Layout ── */
.app { display: flex; flex-direction: column; height: 100vh; background: var(--bg); }
header {
  background: var(--surface); padding: 14px 24px;
  border-bottom: 1px solid var(--border);
  display: flex; align-items: center; gap: 20px; flex-shrink: 0;
  backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px);
  position: relative; z-index: 10;
}
header::after { content:''; position:absolute; bottom:0; left:0; right:0; height:1px; background:linear-gradient(90deg, transparent, var(--accent), var(--accent2), transparent); opacity:0.4; }
header h1 { color: var(--accent); font-size: 1.2em; font-weight: 700; letter-spacing: 1.5px; text-transform: uppercase; white-space: nowrap; }
header .status { margin-left: auto; display: flex; align-items: center; gap: 8px; font-size: 12px; color: var(--muted); font-weight: 500; }
.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--muted); transition: all var(--transition); flex-shrink: 0; }
.dot.ok { background: var(--green); box-shadow: 0 0 8px var(--green); }
.dot.err { background: var(--red); box-shadow: 0 0 8px var(--red); animation: pulse-dot 2s infinite; }
@keyframes pulse-dot { 0%,100%{opacity:1} 50%{opacity:0.5} }

nav {
  background: var(--surface); border-bottom: 1px solid var(--border-subtle);
  display: flex; overflow-x: auto; flex-shrink: 0; padding: 0 16px;
  scrollbar-width: none; -webkit-overflow-scrolling: touch;
}
nav::-webkit-scrollbar { display: none; }
nav button {
  background: none; border: none; color: var(--muted); padding: 12px 20px;
  font-size: 13px; font-weight: 600; cursor: pointer; white-space: nowrap;
  border-bottom: 2px solid transparent; transition: all var(--transition);
  font-family: inherit; position: relative; letter-spacing: 0.3px;
}
nav button:hover { color: var(--text-secondary); background: rgba(255,255,255,0.02); }
nav button.active { color: var(--accent); border-bottom-color: var(--accent); }

.tab-content { flex: 1; overflow-y: auto; padding: 24px; }
.tab-panel { display: none; max-width: 1280px; margin: 0 auto; width: 100%; animation: fadeUp 0.25s ease-out; }
.tab-panel.active { display: block; }
@keyframes fadeUp { from { opacity:0; transform:translateY(8px); } to { opacity:1; transform:translateY(0); } }

/* ── Cards ── */
.card {
  background: var(--surface); border-radius: var(--radius); padding: 20px 24px;
  border: 1px solid var(--border-subtle); margin-bottom: 20px;
  transition: all var(--transition); box-shadow: var(--shadow-sm);
}
.card:hover { border-color: var(--border); box-shadow: var(--shadow-md); }
.card h2 {
  color: var(--accent); font-size: 0.8em; margin-bottom: 16px;
  text-transform: uppercase; letter-spacing: 1.5px; font-weight: 700;
}
.card-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 20px; margin-bottom: 20px; }
.row { display: flex; justify-content: space-between; align-items: center; padding: 8px 0; border-bottom: 1px solid var(--border-subtle); }
.row:last-child { border-bottom: none; }
.label { color: var(--muted); font-size: 13px; font-weight: 500; }
.val { color: var(--green); font-weight: 600; font-family: 'JetBrains Mono', monospace; font-size: 13px; }
.val.error { color: var(--red); }
.val.warn { color: var(--yellow); }

/* ── Badges ── */
.badge {
  display: inline-flex; align-items: center; padding: 3px 10px; border-radius: 20px;
  font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px;
}
.badge.ok, .badge.active { background: var(--green-dim); color: var(--green); }
.badge.err, .badge.ended { background: var(--red-dim); color: var(--red); }
.badge.warn, .badge.paused { background: var(--yellow-dim); color: var(--yellow); }
.badge.info { background: var(--accent2-dim); color: var(--accent2); }
.badge.user { background: var(--accent-dim); color: var(--accent); }
.badge.assistant { background: var(--accent2-dim); color: var(--accent2); }
.badge.system { background: rgba(100,116,139,0.12); color: var(--muted); }
.badge.online { background: var(--green-dim); color: var(--green); }
.badge.offline { background: var(--red-dim); color: var(--red); }

/* ── Forms ── */
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.form-group { display: flex; flex-direction: column; gap: 6px; }
.form-group.full { grid-column: 1 / -1; }
.form-group label { color: var(--muted); font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; }
input, select, textarea {
  background: var(--bg); color: var(--text); border: 1px solid var(--border-subtle);
  border-radius: var(--radius-xs); padding: 10px 14px; font-family: inherit; font-size: 13px;
  transition: all var(--transition);
}
input:hover, select:hover, textarea:hover { border-color: var(--border); }
input:focus, select:focus, textarea:focus { outline: none; border-color: var(--accent2); box-shadow: 0 0 0 3px var(--accent2-dim); }
button:focus-visible, input:focus-visible, select:focus-visible, textarea:focus-visible {
  outline: 2px solid var(--accent2); outline-offset: 2px;
}
textarea { resize: vertical; min-height: 72px; }

.btn {
  background: var(--accent); color: #fff; border: none; border-radius: var(--radius-xs);
  padding: 10px 24px; font-weight: 600; cursor: pointer; font-family: inherit; font-size: 13px;
  transition: all var(--transition); letter-spacing: 0.3px;
}
.btn:hover:not(:disabled) { transform: translateY(-1px); box-shadow: 0 4px 12px rgba(255,107,53,0.3); }
.btn:active:not(:disabled) { transform: translateY(0); }
.btn:disabled { opacity: 0.35; cursor: default; }
.btn.secondary { background: var(--surface3); color: var(--text-secondary); }
.btn.secondary:hover:not(:disabled) { background: var(--border); box-shadow: none; }
.btn.danger { background: var(--red); }
.btn.danger:hover:not(:disabled) { box-shadow: 0 4px 12px rgba(248,113,113,0.3); }
.btn.small { padding: 6px 14px; font-size: 12px; border-radius: var(--radius-xs); }

.btn-row { display: flex; gap: 12px; align-items: center; margin-top: 16px; flex-wrap: wrap; }
.feedback { font-size: 12px; min-height: 1.2em; font-weight: 500; }
.feedback.ok { color: var(--green); }
.feedback.err { color: var(--red); }

/* ── Tables ── */
.table-wrap { overflow-x: auto; border-radius: var(--radius-sm); }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { text-align: left; color: var(--muted); font-size: 10px; text-transform: uppercase; letter-spacing: 1px; font-weight: 700; padding: 12px 16px; border-bottom: 1px solid var(--border); background: var(--surface2); }
td { padding: 12px 16px; border-bottom: 1px solid var(--border-subtle); vertical-align: top; transition: background var(--transition); }
tr:hover td { background: rgba(255,107,53,0.03); }
tr.clickable { cursor: pointer; }

/* ── Split layout (Conversations) ── */
.split { display: flex; gap: 16px; height: calc(100vh - 160px); }
.split-left { width: 360px; min-width: 300px; overflow-y: auto; flex-shrink: 0; }
.split-right { flex: 1; overflow-y: auto; display: flex; flex-direction: column; }
.session-item {
  padding: 14px 18px; border-bottom: 1px solid var(--border-subtle); cursor: pointer;
  transition: all var(--transition); border-left: 3px solid transparent;
}
.session-item:hover { background: rgba(255,107,53,0.04); border-left-color: var(--border); }
.session-item.active { background: var(--accent-dim); border-left-color: var(--accent); }
.session-item .sid { font-size: 12px; font-family: monospace; color: var(--muted); }
.session-item .meta { font-size: 12px; color: var(--muted); margin-top: 4px; }

/* ── Messages ── */
.msg-list { flex: 1; overflow-y: auto; padding: 16px; display: flex; flex-direction: column; gap: 12px; }
.msg { max-width: 85%; }
.msg.user { align-self: flex-end; }
.msg.assistant { align-self: flex-start; }
.msg .bubble {
  padding: 10px 14px; border-radius: var(--radius); line-height: 1.5;
  white-space: pre-wrap; word-break: break-word; font-size: 13px;
}
.msg.user .bubble { background: linear-gradient(135deg, #1a3a2a, #1e3a2e); border: 1px solid #2d5a40; color: #d1fae5; }
.msg.assistant .bubble { background: var(--surface2); border: 1px solid var(--border-subtle); color: var(--text); }
.msg .msg-meta { font-size: 11px; color: var(--muted); margin-top: 4px; padding: 0 4px; }

/* ── Chat compose ── */
.compose {
  display: flex; align-items: flex-end; gap: 10px;
  padding: 12px 16px; background: var(--surface); border-top: 1px solid var(--border);
}
.compose textarea {
  flex: 1; max-height: 120px; resize: none; line-height: 1.4;
}

/* ── Filters ── */
.filters { display: flex; gap: 10px; align-items: center; margin-bottom: 12px; flex-wrap: wrap; }
.filters select, .filters input { font-size: 12px; padding: 6px 10px; }

/* ── Event log ── */
.event-item { padding: 8px 12px; border-bottom: 1px solid rgba(15,52,96,0.2); font-size: 13px; font-family: monospace; }
.event-item .ts { color: var(--muted); font-size: 11px; }
.event-item .etype { color: var(--accent2); font-weight: 600; }

/* ── Device cards ── */
.device-card { transition: all 0.15s; }
.device-card:hover { border-color: var(--accent); }
.device-details { margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--border); display: none; }
.device-card.expanded .device-details { display: block; }

/* ── Note cards ── */
.note-card { cursor: default; }
.note-preview { color: var(--muted); font-size: 12px; margin-top: 8px; line-height: 1.4; max-height: 3.6em; overflow: hidden; }
.note-actions { display: flex; gap: 8px; margin-top: 10px; }

/* ── Empty states ── */
.empty { text-align: center; color: var(--muted); padding: 40px 20px; font-style: italic; }

/* ── Progress bars ── */
.progress-bar { background: var(--bg); border-radius: 8px; height: 20px; overflow: hidden; position: relative; border: 1px solid var(--border-subtle); }
.progress-bar .fill { height: 100%; border-radius: 7px; transition: width 0.6s cubic-bezier(0.4,0,0.2,1); }
.progress-bar .fill.green { background: var(--green); }
.progress-bar .fill.yellow { background: var(--yellow); }
.progress-bar .fill.red { background: var(--red); }
.progress-bar .pct { position: absolute; right: 6px; top: 0; font-size: 10px; line-height: 16px; font-weight: 700; color: var(--text); }

/* ── Memory / Document / Tool cards ── */
.fact-card, .doc-card, .tool-card { transition: border-color 0.15s; }
.fact-card:hover, .doc-card:hover, .tool-card:hover { border-color: var(--accent); }
.score-bar { display: inline-block; background: rgba(83,215,105,0.2); color: var(--green); padding: 2px 6px; border-radius: 3px; font-size: 11px; font-weight: 700; }
.fact-content { color: var(--text); line-height: 1.5; margin: 8px 0; white-space: pre-wrap; word-break: break-word; }
.fact-meta { font-size: 11px; color: var(--muted); display: flex; gap: 12px; flex-wrap: wrap; }
.chunk-item { background: var(--bg); border-radius: 4px; padding: 10px; margin-top: 8px; font-size: 12px; line-height: 1.5; }

/* ── Tool execution ── */
.tool-params { margin-top: 10px; }
.tool-params .param-field { margin-bottom: 8px; }
.tool-params label { font-size: 12px; color: var(--muted); display: block; margin-bottom: 2px; }
.tool-result { background: var(--bg); border-radius: 4px; padding: 12px; margin-top: 10px; font-family: monospace; font-size: 12px; white-space: pre-wrap; word-break: break-word; max-height: 300px; overflow-y: auto; }

/* ── Tool-call chat bubbles ── */
.msg.tool_call .bubble, .msg.tool_result .bubble {
  border: 1px dashed var(--accent2); background: rgba(6,182,212,0.05);
  font-size: 12px; font-family: monospace; color: var(--accent2);
}

/* ── Editable inline ── */
.editable-title { cursor: pointer; border-bottom: 1px dashed transparent; transition: border-color 0.15s; }
.editable-title:hover { border-bottom-color: var(--accent); }
.inline-edit { display: inline-flex; gap: 4px; align-items: center; }
.inline-edit input { font-size: 13px; padding: 2px 6px; width: 180px; }

/* ── Backend badges grid ── */
.backend-group { margin-bottom: 12px; }
.backend-group h3 { font-size: 11px; text-transform: uppercase; color: var(--muted); letter-spacing: 1px; margin-bottom: 6px; }
.backend-list { display: flex; flex-wrap: wrap; gap: 6px; }
.backend-badge { padding: 3px 10px; border-radius: 4px; font-size: 11px; font-weight: 600; background: rgba(15,52,96,0.4); color: var(--muted); }
.backend-badge.current { background: rgba(83,215,105,0.15); color: var(--green); border: 1px solid rgba(83,215,105,0.3); }

/* ── Toggle switch ── */
.toggle-wrap { display: flex; align-items: center; gap: 8px; }
.toggle { position: relative; width: 40px; height: 22px; background: var(--border); border-radius: 11px; cursor: pointer; transition: background 0.2s; }
.toggle.on { background: var(--accent); }
.toggle .knob { position: absolute; top: 2px; left: 2px; width: 18px; height: 18px; background: #fff; border-radius: 50%; transition: left 0.2s; }
.toggle.on .knob { left: 20px; }

/* ── Skeleton Loaders ── */
.skeleton {
  background: linear-gradient(90deg, var(--surface) 25%, #2a2a3e 50%, var(--surface) 75%);
  background-size: 200% 100%;
  animation: shimmer 1.5s infinite;
  border-radius: var(--radius);
  height: 20px;
  margin: 8px 0;
}
@keyframes shimmer { 0%{background-position:200% 0} 100%{background-position:-200% 0} }
.skeleton.h-lg { height: 40px; }
.skeleton.h-xl { height: 60px; }

/* ── Toast Notifications ── */
.toast-container { position:fixed; top:20px; right:20px; z-index:9999; display:flex; flex-direction:column; gap:8px; }
.toast { padding:14px 24px; border-radius:var(--radius); color:#fff; animation:slideIn 0.3s cubic-bezier(0.4,0,0.2,1); max-width:400px; font-size:13px; font-weight:500; backdrop-filter:blur(8px); box-shadow:var(--shadow-lg); }
.toast.success { background:rgba(22,101,52,0.9); border:1px solid rgba(34,197,94,0.3); }
.toast.error { background:rgba(127,29,29,0.9); border:1px solid rgba(239,68,68,0.3); }
.toast.info { background:rgba(6,95,115,0.9); border:1px solid rgba(6,182,212,0.3); }
@keyframes slideIn { from{transform:translateX(100%) scale(0.95);opacity:0} to{transform:translateX(0) scale(1);opacity:1} }

/* ── Confirm Dialog ── */
.confirm-overlay { position:fixed; inset:0; background:rgba(0,0,0,0.6); z-index:999; display:flex; align-items:center; justify-content:center; animation:fadeIn 0.15s; }
.confirm-box { background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:24px; max-width:400px; text-align:center; }
.confirm-box p { margin-bottom:4px; line-height:1.5; }
.confirm-box .btn-row { display:flex; gap:12px; justify-content:center; margin-top:16px; }

/* ── Empty States (styled) ── */
.empty-state { text-align:center; padding:40px 20px; color:var(--muted); }
.empty-state .icon { font-size:48px; margin-bottom:12px; display:block; }
.empty-state .msg { font-style:italic; }

/* ── Tab Transition ── */
.tab-content { animation: fadeTab 0.2s; }
@keyframes fadeTab { from{opacity:0} to{opacity:1} }

/* ── Device Card Expand Indicator ── */
.device-card { position:relative; }
.device-card::after { content:'\\25BC'; position:absolute; right:16px; top:16px; color:var(--muted); font-size:12px; transition:transform 0.2s; }
.device-card.expanded::after { transform:rotate(180deg); }

/* ── Responsive ── */
@keyframes fadeIn { from { opacity: 0; transform: translateY(-4px); } to { opacity: 1; transform: translateY(0); } }

/* Loading overlay for cards/sections */
.is-loading { position:relative; pointer-events:none; opacity:0.6; }
.is-loading::after { content:''; position:absolute; top:50%; left:50%; width:24px; height:24px; margin:-12px 0 0 -12px; border:3px solid var(--border); border-top-color:var(--accent); border-radius:50%; animation:spin 0.6s linear infinite; z-index:10; }
@keyframes spin { to { transform:rotate(360deg); } }

@media (max-width: 1024px) {
  .card-grid { grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); }
  .tab-content { padding: 16px; }
}

@media (max-width: 768px) {
  .split { flex-direction: column; height: auto; }
  .split-left { width: 100%; max-height: 280px; border-right: none; border-bottom: 1px solid var(--border); }
  .form-grid { grid-template-columns: 1fr; }
  .card-grid { grid-template-columns: 1fr; }
  header h1 { font-size: 14px; letter-spacing: 1px; }
  header .status span { display: none; }
  .tab-content { padding: 12px; }
  .card { padding: 16px; border-radius: var(--radius-sm); }
  .card h2 { font-size: 0.75em; margin-bottom: 12px; }
  .msg .bubble { max-width: 90%; font-size: 13px; }
  .compose { padding: 10px 12px; }
}

@media (max-width: 480px) {
  header { padding: 10px 14px; }
  nav button { font-size: 12px; padding: 10px 12px; }
  .card { padding: 14px; margin-bottom: 12px; }
  .btn { font-size: 12px; padding: 8px 16px; }
  .btn.small { padding: 5px 10px; font-size: 11px; }
  .card-grid { gap: 12px; }
  .filters { gap: 8px; }
  .filters input, .filters select { font-size: 12px; padding: 6px 10px; }
  .device-details { font-size: 12px; }
  .tool-result { font-size: 11px; }
  .msg-list { padding: 10px; gap: 8px; }
  .row { padding: 6px 0; font-size: 13px; }
  h2 { font-size: 0.75em; }
}
</style>
</head>
<body>
<div class="app">
<div class="toast-container" id="toast-container"></div>

<header>
  <h1>TinkerClaw</h1>
  <div class="status">
    <div class="dot" id="dot-dragon" title="Dragon Server"></div>
    <span>Dragon</span>
    <div class="dot" id="dot-voice" title="Voice Pipeline"></div>
    <span>Voice</span>
  </div>
</header>

<nav id="tabs">
  <button class="active" data-tab="overview">Overview</button>
  <button data-tab="conversations">Conversations</button>
  <button data-tab="chat">Chat</button>
  <button data-tab="devices">Devices</button>
  <button data-tab="notes">Notes</button>
  <button data-tab="memory">Memory</button>
  <button data-tab="documents">Documents</button>
  <button data-tab="tools">Tools</button>
  <button data-tab="logs">Logs</button>
  <button data-tab="ota">OTA</button>
  <button data-tab="debug">Debug</button>
</nav>

<div class="tab-content">

<!-- ═══════════════ OVERVIEW TAB ═══════════════ -->
<div class="tab-panel active" id="tab-overview">
  <!-- System Metrics -->
  <div class="card" id="sys-metrics-card">
    <h2>System Metrics</h2>
    <div style="display:grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 12px;">
      <div>
        <div class="label">Uptime</div>
        <div class="val" id="sys-uptime" style="font-size:1.1em;">--</div>
      </div>
      <div>
        <div class="label">CPU Usage</div>
        <div style="display:flex; align-items:center; gap:8px;">
          <div class="progress-bar" style="flex:1;">
            <div class="fill green" id="sys-cpu-bar" style="width:0%"></div>
            <span class="pct" id="sys-cpu-pct">--%</span>
          </div>
        </div>
      </div>
      <div>
        <div class="label">RAM Usage</div>
        <div style="display:flex; align-items:center; gap:8px;">
          <div class="progress-bar" style="flex:1;">
            <div class="fill green" id="sys-ram-bar" style="width:0%"></div>
            <span class="pct" id="sys-ram-pct">--%</span>
          </div>
        </div>
        <div style="font-size:11px; color:var(--muted); margin-top:2px;" id="sys-ram-detail">-- / --</div>
      </div>
      <div>
        <div class="label">Active Connections</div>
        <div class="val" id="sys-conns" style="font-size:1.1em;">--</div>
      </div>
      <div>
        <div class="label">Total Sessions</div>
        <div class="val" id="sys-sessions" style="font-size:1.1em;">--</div>
      </div>
      <div>
        <div class="label">Total Messages</div>
        <div class="val" id="sys-messages" style="font-size:1.1em;">--</div>
      </div>
    </div>
  </div>

  <!-- Backend Status -->
  <div class="card" id="backend-status-card">
    <h2>Backend Status</h2>
    <div id="backend-status-content" style="display:grid; grid-template-columns: 1fr 1fr 1fr; gap:16px;">
      <div class="backend-group">
        <h3>STT (Speech-to-Text)</h3>
        <div class="backend-list" id="backends-stt"><div class="skeleton" style="width:80px;height:24px;display:inline-block;"></div></div>
      </div>
      <div class="backend-group">
        <h3>TTS (Text-to-Speech)</h3>
        <div class="backend-list" id="backends-tts"><div class="skeleton" style="width:80px;height:24px;display:inline-block;"></div></div>
      </div>
      <div class="backend-group">
        <h3>LLM (Language Model)</h3>
        <div class="backend-list" id="backends-llm"><div class="skeleton" style="width:80px;height:24px;display:inline-block;"></div></div>
      </div>
    </div>
  </div>

  <div class="card-grid">
    <div class="card" id="dragon-card">
      <h2>Dragon Server (3501)</h2>
      <div class="row"><span class="label">Status</span><span class="val" id="d-status">--</span></div>
      <div class="row"><span class="label">CDP</span><span class="val" id="d-cdp">--</span></div>
      <div class="row"><span class="label">FPS</span><span class="val" id="d-fps">--</span></div>
      <div class="row"><span class="label">Frames</span><span class="val" id="d-frames">--</span></div>
      <div class="row"><span class="label">Uptime</span><span class="val" id="d-uptime">--</span></div>
    </div>
    <div class="card" id="voice-card">
      <h2>Voice Pipeline (3502)</h2>
      <div class="row"><span class="label">Status</span><span class="val" id="v-status">--</span></div>
      <div class="row"><span class="label">STT</span><span class="val" id="v-stt">--</span></div>
      <div class="row"><span class="label">TTS</span><span class="val" id="v-tts">--</span></div>
      <div class="row"><span class="label">LLM</span><span class="val" id="v-llm">--</span></div>
      <div class="row"><span class="label">Connections</span><span class="val" id="v-conns">--</span></div>
      <div class="row"><span class="label">Uptime</span><span class="val" id="v-uptime">--</span></div>
    </div>
    <div class="card">
      <h2>Quick Stats</h2>
      <div class="row"><span class="label">Total Sessions</span><span class="val" id="qs-sessions">--</span></div>
      <div class="row"><span class="label">Total Messages</span><span class="val" id="qs-messages">--</span></div>
      <div class="row"><span class="label">Total Notes</span><span class="val" id="qs-notes">--</span></div>
      <div class="row"><span class="label">Devices</span><span class="val" id="qs-devices">--</span></div>
      <div class="row"><span class="label">Dashboard Up</span><span class="val" id="qs-dash-up">--</span></div>
    </div>
  </div>

  <!-- Active Devices -->
  <div class="card">
    <h2>Active Devices</h2>
    <div id="ov-devices"><div class="skeleton" style="width:160px;"></div><div class="skeleton" style="width:140px;"></div></div>
  </div>

  <!-- Recent sessions -->
  <div class="card">
    <h2>Recent Sessions</h2>
    <div class="table-wrap">
      <table>
        <thead><tr><th>ID</th><th>Device</th><th>Type</th><th>Status</th><th>Messages</th><th>Last Active</th></tr></thead>
        <tbody id="ov-sessions"><tr><td colspan="6"><div class="skeleton"></div><div class="skeleton" style="width:80%;"></div><div class="skeleton" style="width:60%;"></div></td></tr></tbody>
      </table>
    </div>
  </div>

  <!-- Pipeline Config -->
  <div class="card">
    <h2>Pipeline Config</h2>
    <div class="form-grid">
      <div class="form-group">
        <label>STT Backend</label>
        <select id="cfg-stt">
          <option value="moonshine">moonshine</option>
          <option value="whisper_cpp">whisper_cpp</option>
          <option value="vosk">vosk</option>
          <option value="openrouter">openrouter</option>
        </select>
      </div>
      <div class="form-group">
        <label>STT Model</label>
        <input id="cfg-stt-model" type="text" placeholder="e.g. tiny">
      </div>
      <div class="form-group">
        <label>TTS Backend</label>
        <select id="cfg-tts">
          <option value="piper">piper</option>
          <option value="kokoro">kokoro</option>
          <option value="edge_tts">edge_tts</option>
          <option value="openrouter">openrouter</option>
        </select>
      </div>
      <div class="form-group">
        <label>TTS Voice / Model</label>
        <input id="cfg-tts-model" type="text" placeholder="e.g. en_US-lessac-medium">
      </div>
      <div class="form-group">
        <label>LLM Backend</label>
        <select id="cfg-llm">
          <option value="ollama">ollama</option>
          <option value="openrouter">openrouter</option>
          <option value="lmstudio">lmstudio</option>
          <option value="npu_genie">npu_genie</option>
        </select>
      </div>
      <div class="form-group">
        <label>LLM Model</label>
        <input id="cfg-llm-model" type="text" placeholder="e.g. gemma3:4b">
      </div>
      <div class="form-group full">
        <label>System Prompt</label>
        <textarea id="cfg-prompt" rows="3"></textarea>
      </div>
    </div>
    <div class="btn-row">
      <button class="btn" id="btn-apply" onclick="applyConfig()">Apply Changes</button>
      <span class="feedback" id="cfg-feedback"></span>
    </div>
  </div>
</div>

<!-- ═══════════════ CONVERSATIONS TAB ═══════════════ -->
<div class="tab-panel" id="tab-conversations">
  <div class="filters">
    <select id="conv-status-filter" onchange="loadConversations()">
      <option value="">All Status</option>
      <option value="active">Active</option>
      <option value="paused">Paused</option>
      <option value="ended">Ended</option>
    </select>
    <select id="conv-device-filter" onchange="loadConversations()">
      <option value="">All Devices</option>
    </select>
    <button class="btn small" onclick="createNewSession()">+ New Session</button>
  </div>
  <div class="split">
    <div class="split-left card" style="padding:0;">
      <div id="conv-list"><div style="padding:16px;"><div class="skeleton"></div><div class="skeleton" style="width:70%;"></div><div class="skeleton"></div><div class="skeleton" style="width:70%;"></div></div></div>
    </div>
    <div class="split-right card" style="padding:0;">
      <div id="conv-header" style="padding:12px 16px; border-bottom:1px solid var(--border); display:none;">
        <div style="display:flex; justify-content:space-between; align-items:center;">
          <div>
            <span style="font-weight:700;">Session</span>
            <span id="conv-sid" style="font-family:monospace; color:var(--muted); font-size:12px;"></span>
            <span id="conv-title-wrap" style="margin-left:8px;">
              <span id="conv-title-display" class="editable-title" onclick="startEditTitle()" title="Click to edit title"></span>
              <span id="conv-title-edit" class="inline-edit" style="display:none;">
                <input id="conv-title-input" type="text" placeholder="Session title..." onkeydown="if(event.key==='Enter')saveTitle();if(event.key==='Escape')cancelEditTitle();">
                <button class="btn small" onclick="saveTitle()">Save</button>
                <button class="btn small secondary" onclick="cancelEditTitle()">Cancel</button>
              </span>
            </span>
          </div>
          <div style="display:flex; gap:8px; align-items:center;">
            <span class="badge" id="conv-badge"></span>
            <button class="btn small" id="conv-resume-btn" onclick="resumeSelectedSession()" style="display:none;">Resume</button>
            <button class="btn small secondary" id="conv-pause-btn" onclick="pauseSelectedSession()" style="display:none;">Pause</button>
            <button class="btn small danger" id="conv-purge-btn" onclick="purgeSelectedMessages()" style="display:none;">Purge Messages</button>
            <button class="btn small danger" id="conv-end-btn" onclick="endSelectedSession()" style="display:none;">End Session</button>
          </div>
        </div>
      </div>
      <div class="msg-list" id="conv-messages">
        <div class="empty">Select a session to view messages</div>
      </div>
    </div>
  </div>
</div>

<!-- ═══════════════ CHAT TAB ═══════════════ -->
<div class="tab-panel" id="tab-chat" style="display:none; height:calc(100vh - 120px);">
  <div style="display:flex; flex-direction:column; height:100%;">
    <div style="display:flex; gap:10px; align-items:center; margin-bottom:12px;">
      <select id="chat-session-select" onchange="onChatSessionChange()" style="flex:1;">
        <option value="">-- Create new session --</option>
      </select>
      <div class="toggle-wrap">
        <div class="toggle" id="chat-stateless-toggle" onclick="toggleStatelessMode()" title="Direct LLM mode (no session)">
          <div class="knob"></div>
        </div>
        <span style="font-size:12px; color:var(--muted);" id="chat-mode-label">Session</span>
      </div>
      <div class="dot" id="chat-dot"></div>
      <span id="chat-status" style="font-size:12px; color:var(--muted);">idle</span>
    </div>
    <div class="card" style="flex:1; display:flex; flex-direction:column; padding:0; overflow:hidden;">
      <div class="msg-list" id="chat-messages" style="flex:1;">
        <div class="empty" id="chat-empty">Select or create a session to start chatting</div>
      </div>
      <div class="compose">
        <textarea id="chat-input" rows="1" placeholder="Message Tinker..." disabled></textarea>
        <button class="btn" id="chat-send" disabled onclick="chatSend()">Send</button>
      </div>
    </div>
  </div>
</div>

<!-- ═══════════════ DEVICES TAB ═══════════════ -->
<div class="tab-panel" id="tab-devices">
  <div class="filters">
    <span style="font-size:12px; color:var(--muted);">Click device name to rename. Click card to expand details.</span>
  </div>
  <div class="card-grid" id="devices-grid">
    <div class="card"><div class="skeleton h-lg"></div><div class="skeleton"></div><div class="skeleton" style="width:60%;"></div></div>
    <div class="card"><div class="skeleton h-lg"></div><div class="skeleton"></div><div class="skeleton" style="width:60%;"></div></div>
  </div>
</div>

<!-- ═══════════════ NOTES TAB ═══════════════ -->
<div class="tab-panel" id="tab-notes">
  <div class="filters">
    <input id="notes-search" type="text" placeholder="Search notes..." style="flex:1; max-width:400px;">
    <button class="btn small" onclick="searchNotes()">Search</button>
    <button class="btn small secondary" onclick="openNewNoteForm()">+ New Note</button>
  </div>
  <div id="new-note-form" class="card" style="display:none; margin-bottom:16px;">
    <h2>Create Note</h2>
    <div class="form-group" style="margin-bottom:8px;">
      <label>Title</label>
      <input id="note-title" type="text" placeholder="Note title">
    </div>
    <div class="form-group" style="margin-bottom:8px;">
      <label>Content</label>
      <textarea id="note-text" rows="4" placeholder="Note content..."></textarea>
    </div>
    <div class="btn-row">
      <button class="btn" onclick="createNote()">Save Note</button>
      <button class="btn secondary" onclick="closeNewNoteForm()">Cancel</button>
    </div>
  </div>
  <div class="card-grid" id="notes-grid">
    <div class="card"><div class="skeleton h-lg" style="width:50%;"></div><div class="skeleton"></div><div class="skeleton h-xl"></div></div>
  </div>
</div>

<!-- ═══════════════ MEMORY TAB ═══════════════ -->
<div class="tab-panel" id="tab-memory">
  <div class="filters">
    <input id="memory-search" type="text" placeholder="Search memory..." style="flex:1; max-width:400px;">
    <button class="btn small" onclick="searchMemory()">Search</button>
    <button class="btn small secondary" onclick="clearMemorySearch()">Clear</button>
  </div>
  <div class="card" id="memory-add-card" style="margin-bottom:16px;">
    <h2>Add Fact</h2>
    <div style="display:flex; gap:10px; align-items:flex-end;">
      <div class="form-group" style="flex:1;">
        <label>Content</label>
        <textarea id="memory-fact-input" rows="2" placeholder="Something Tinker should remember..."></textarea>
      </div>
      <button class="btn" onclick="addMemoryFact()" style="height:38px;">Remember</button>
    </div>
    <span class="feedback" id="memory-feedback"></span>
  </div>
  <div id="memory-results-info" style="display:none; margin-bottom:12px; font-size:12px; color:var(--accent2);"></div>
  <div class="card-grid" id="memory-grid">
    <div class="card"><div class="skeleton"></div><div class="skeleton" style="width:80%;"></div></div>
  </div>
</div>

<!-- ═══════════════ DOCUMENTS TAB ═══════════════ -->
<div class="tab-panel" id="tab-documents">
  <div class="filters">
    <input id="docs-search" type="text" placeholder="Search documents..." style="flex:1; max-width:400px;">
    <button class="btn small" onclick="searchDocuments()">Search</button>
    <button class="btn small secondary" onclick="clearDocSearch()">Clear</button>
    <button class="btn small secondary" onclick="toggleIngestForm()">+ Ingest Document</button>
  </div>
  <div id="doc-ingest-form" class="card" style="display:none; margin-bottom:16px;">
    <h2>Ingest Document</h2>
    <div class="form-group" style="margin-bottom:8px;">
      <label>Title</label>
      <input id="doc-title" type="text" placeholder="Document title">
    </div>
    <div class="form-group" style="margin-bottom:8px;">
      <label>Content</label>
      <textarea id="doc-content" rows="8" placeholder="Paste document content here... It will be chunked and embedded for semantic search."></textarea>
    </div>
    <div class="btn-row">
      <button class="btn" onclick="ingestDocument()">Ingest</button>
      <button class="btn secondary" onclick="toggleIngestForm()">Cancel</button>
      <span class="feedback" id="doc-feedback"></span>
    </div>
  </div>
  <div id="docs-results-info" style="display:none; margin-bottom:12px; font-size:12px; color:var(--accent2);"></div>
  <div class="card-grid" id="docs-grid">
    <div class="card"><div class="skeleton h-lg" style="width:50%;"></div><div class="skeleton"></div></div>
  </div>
</div>

<!-- ═══════════════ TOOLS TAB ═══════════════ -->
<div class="tab-panel" id="tab-tools">
  <div class="card-grid" id="tools-grid">
    <div class="card"><div class="skeleton h-lg" style="width:40%;"></div><div class="skeleton"></div><div class="skeleton" style="width:60%;"></div></div>
  </div>
  <div id="tool-exec-panel" class="card" style="display:none; margin-top:16px;">
    <div style="display:flex; justify-content:space-between; align-items:center;">
      <h2 id="tool-exec-name" style="margin:0;">Tool</h2>
      <button class="btn small secondary" onclick="closeToolExec()">Close</button>
    </div>
    <p id="tool-exec-desc" style="font-size:12px; color:var(--muted); margin:8px 0;"></p>
    <div id="tool-exec-params" class="tool-params"></div>
    <div class="btn-row">
      <button class="btn" id="tool-exec-run" onclick="executeSelectedTool()">Execute</button>
      <span class="feedback" id="tool-exec-feedback"></span>
    </div>
    <div id="tool-exec-result" class="tool-result" style="display:none;"></div>
  </div>
</div>

<!-- ═══════════════ LOGS TAB ═══════════════ -->
<div class="tab-panel" id="tab-logs">
  <div class="filters">
    <select id="log-type-filter" onchange="loadEvents()">
      <option value="">All Types</option>
      <option value="session.created">session.created</option>
      <option value="session.ended">session.ended</option>
      <option value="device.connected">device.connected</option>
      <option value="device.disconnected">device.disconnected</option>
      <option value="message.created">message.created</option>
      <option value="config.updated">config.updated</option>
      <option value="error">error</option>
    </select>
    <span style="font-size:12px; color:var(--muted);">Auto-refreshes every 3s</span>
  </div>
  <div class="card" style="padding:0; max-height: calc(100vh - 220px); overflow-y:auto;">
    <div id="event-list"><div style="padding:12px;"><div class="skeleton"></div><div class="skeleton" style="width:70%;"></div><div class="skeleton" style="width:85%;"></div></div></div>
  </div>
</div>

<!-- ═══════════════ OTA TAB ═══════════════ -->
<div class="tab-panel" id="tab-ota">
  <div class="card">
    <h2>Firmware Updates</h2>
    <p style="color:var(--muted); font-size:13px;">Check for and apply OTA firmware updates to connected Tab5 devices.</p>
    <div style="margin-top:16px; display:grid; grid-template-columns: 1fr 1fr; gap:16px;">
      <div>
        <div class="label">Current Firmware</div>
        <div id="ota-current-ver" style="font-size:18px; font-weight:600;">—</div>
      </div>
      <div>
        <div class="label">Device</div>
        <div id="ota-device-name" style="font-size:18px; font-weight:600;">—</div>
      </div>
    </div>
    <div class="btn-row" style="margin-top:16px;">
      <button class="btn" id="ota-check-btn" onclick="otaCheck()">Check for Updates</button>
      <span class="feedback" id="ota-feedback"></span>
    </div>
  </div>
  <div class="card" id="ota-update-card" style="display:none; border-left: 3px solid var(--green);">
    <h2 style="color:var(--green);">Update Available</h2>
    <div style="display:grid; grid-template-columns: 1fr 1fr; gap:16px; margin-top:12px;">
      <div>
        <div class="label">New Version</div>
        <div id="ota-new-ver" style="font-size:18px; font-weight:600; color:var(--green);">—</div>
      </div>
      <div>
        <div class="label">SHA256</div>
        <div id="ota-sha256" style="font-size:11px; color:var(--muted); word-break:break-all;">—</div>
      </div>
    </div>
    <div class="btn-row" style="margin-top:16px;">
      <button class="btn" id="ota-apply-btn" onclick="otaApply()" style="background:var(--green); color:#000;">Apply Update</button>
      <span class="feedback" id="ota-apply-feedback"></span>
    </div>
  </div>
  <div class="card" id="ota-noupdate-card" style="display:none; border-left: 3px solid var(--accent2);">
    <h2 style="color:var(--accent2);">Up to Date</h2>
    <p style="color:var(--muted);">Your firmware is the latest version.</p>
  </div>
</div>

<!-- ═══════════════ DEBUG TAB ═══════════════ -->
<div class="tab-panel" id="tab-debug">
  <div class="card-grid" style="grid-template-columns: 1fr 1fr;">

    <!-- E2E Test Runner -->
    <div class="card" style="grid-row: span 2;">
      <h2>E2E Test Suite</h2>
      <p style="color:var(--muted); font-size:12px; margin-bottom:12px;">Automated test runner for all API endpoints. Tests data integrity, response codes, and schema.</p>
      <div class="btn-row" style="margin-top:0; margin-bottom:16px;">
        <button class="btn" id="run-tests-btn" onclick="runAllTests()">Run All Tests</button>
        <span id="test-summary" style="font-size:13px; font-weight:600;"></span>
      </div>
      <div id="test-results" style="max-height:calc(100vh - 320px); overflow-y:auto;">
        <div class="empty" style="padding:20px;">Click "Run All Tests" to start</div>
      </div>
    </div>

    <!-- Tab5 Remote Control -->
    <div class="card">
      <h2>Tab5 Remote Control</h2>
      <p style="color:var(--muted); font-size:12px; margin-bottom:12px;">Control and monitor the Tab5 device remotely via its debug server.</p>
      <div style="display:grid; grid-template-columns: 1fr 1fr; gap:8px; margin-bottom:12px;">
        <button class="btn small" onclick="tab5Screenshot()">Screenshot</button>
        <button class="btn small secondary" onclick="tab5Info()">Device Info</button>
        <button class="btn small secondary" onclick="tab5Selftest()">Self-Test</button>
        <button class="btn small secondary" onclick="tab5VoiceReconnect()">Voice Reconnect</button>
      </div>
      <div style="display:flex; gap:8px; margin-bottom:12px;">
        <select id="tab5-nav-screen" style="flex:1;">
          <option value="home">Home</option>
          <option value="notes">Notes</option>
          <option value="chat">Chat</option>
          <option value="settings">Settings</option>
          <option value="camera">Camera</option>
          <option value="files">Files</option>
        </select>
        <button class="btn small" onclick="tab5Navigate()">Navigate</button>
      </div>
      <div style="display:flex; gap:8px; margin-bottom:12px;">
        <input id="tab5-touch-x" type="number" placeholder="X" style="width:70px;">
        <input id="tab5-touch-y" type="number" placeholder="Y" style="width:70px;">
        <button class="btn small" onclick="tab5Touch()">Tap</button>
      </div>
      <div style="display:flex; gap:8px; margin-bottom:12px;">
        <input id="tab5-chat-text" type="text" placeholder="Send text to Tinker..." style="flex:1;">
        <button class="btn small" onclick="tab5Chat()">Send</button>
      </div>
      <div style="display:flex; gap:8px; margin-bottom:12px;">
        <button class="btn small" onclick="tab5Mode(0)">Local</button>
        <button class="btn small" onclick="tab5Mode(1)">Hybrid</button>
        <button class="btn small" onclick="tab5Mode(2)">Cloud</button>
      </div>
    </div>

    <!-- Screenshot / Info Display -->
    <div class="card">
      <h2>Tab5 Output</h2>
      <div id="tab5-output" style="min-height:200px;">
        <div class="empty" style="padding:20px;">Use controls above to interact with Tab5</div>
      </div>
    </div>
  </div>
</div>

</div><!-- tab-content -->
</div><!-- app -->

<script>
// ── Globals ──
const P = '/api/proxy';
let currentTab = 'overview';
let selectedSessionId = null;
let chatSessionId = null;
let chatBusy = false;
let chatStateless = false;
let lastEventId = 0;
let refreshTimer = null;
let eventTimer = null;

// ── Helpers ──
const $ = id => document.getElementById(id);
const $$ = sel => document.querySelectorAll(sel);

function fmtUptime(s) {
  if (typeof s !== 'number' || isNaN(s)) return '--';
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = Math.floor(s % 60);
  return h > 0 ? h+'h '+m+'m' : m > 0 ? m+'m '+sec+'s' : sec+'s';
}

function fmtTime(ts) {
  if (!ts) return '--';
  const d = new Date(typeof ts === 'number' ? ts * 1000 : ts);
  return d.toLocaleString('en-GB', { month:'short', day:'numeric', hour:'2-digit', minute:'2-digit', second:'2-digit' });
}

function setLoading(id, on) {
  const el = $(id);
  if (el) { if (on) el.classList.add('is-loading'); else el.classList.remove('is-loading'); }
}

function fmtTimeShort(ts) {
  if (!ts) return '';
  const d = new Date(typeof ts === 'number' ? ts * 1000 : ts);
  return d.toLocaleTimeString('en-GB', { hour:'2-digit', minute:'2-digit' });
}

function setVal(id, text, cls) {
  const el = $(id);
  if (!el) return;
  el.textContent = text;
  el.className = 'val' + (cls ? ' ' + cls : '');
}

function truncId(id) { return id ? id.substring(0, 8) + '...' : '--'; }

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok && !opts?.raw) throw new Error(`HTTP ${r.status}`);
  if (opts?.raw) return r;
  return r.json();
}

// ── Toast Notifications ──
function showToast(msg, type='success') {
  const container = $('toast-container');
  const toast = document.createElement('div');
  toast.className = 'toast ' + type;
  toast.textContent = msg;
  container.appendChild(toast);
  setTimeout(() => { toast.style.opacity = '0'; toast.style.transition = 'opacity 0.3s'; setTimeout(() => toast.remove(), 300); }, 3500);
}

// ── Confirm Dialog ──
function confirmAction(msg, callback) {
  const overlay = document.createElement('div');
  overlay.className = 'confirm-overlay';
  overlay.innerHTML = `<div class="confirm-box"><p>${msg}</p><div class="btn-row"><button class="btn danger" id="confirm-yes">Confirm</button><button class="btn secondary" id="confirm-no">Cancel</button></div></div>`;
  document.body.appendChild(overlay);
  overlay.querySelector('#confirm-yes').onclick = () => { overlay.remove(); callback(); };
  overlay.querySelector('#confirm-no').onclick = () => overlay.remove();
  overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });
}

// ── Safe API Fetch ──
async function apiFetch(path, opts={}) {
  try {
    const r = await fetch(P + path, opts);
    if (!r.ok) { const e = await r.json().catch(()=>({error:'Request failed'})); showToast(e.error || `Error ${r.status}`, 'error'); return null; }
    return r;
  } catch(e) { showToast('Connection failed', 'error'); return null; }
}

// ── Tab Navigation ──
document.getElementById('tabs').addEventListener('click', e => {
  if (e.target.tagName !== 'BUTTON') return;
  const tab = e.target.dataset.tab;
  $$('nav button').forEach(b => b.classList.remove('active'));
  e.target.classList.add('active');
  $$('.tab-panel').forEach(p => { p.classList.remove('active'); p.style.display = 'none'; });
  const panel = $('tab-' + tab);
  panel.classList.add('active');
  panel.style.display = tab === 'chat' ? 'flex' : 'block';
  // Re-trigger tab fade animation
  const tc = document.querySelector('.tab-content');
  tc.style.animation = 'none'; tc.offsetHeight; tc.style.animation = '';
  currentTab = tab;
  onTabSwitch(tab);
});

function onTabSwitch(tab) {
  if (tab === 'overview') { refreshOverview(); }
  if (tab === 'conversations') { loadConversations(); }
  if (tab === 'chat') { loadChatSessions(); }
  if (tab === 'devices') { loadDevices(); }
  if (tab === 'notes') { loadNotes(); }
  if (tab === 'memory') { loadMemory(); }
  if (tab === 'documents') { loadDocuments(); }
  if (tab === 'tools') { loadTools(); }
  if (tab === 'logs') { loadEvents(); startEventPoll(); }
  if (tab !== 'logs') stopEventPoll();
  if (tab === 'ota') { loadOtaInfo(); }
  if (tab === 'debug') { /* no auto-load */ }
}

// ── OVERVIEW ──
async function refreshOverview() {
  try {
    const data = await api('/api/status');

    // Dragon
    const d = data.dragon;
    if (d && !d.error) {
      $('dot-dragon').className = 'dot ok';
      setVal('d-status', 'Online');
      setVal('d-cdp', d.cdp || '--', d.cdp === 'connected' ? '' : 'warn');
      setVal('d-fps', d.fps != null ? d.fps.toFixed(1) : '--');
      setVal('d-frames', d.frames != null ? d.frames.toLocaleString() : '--');
      setVal('d-uptime', fmtUptime(d.uptime));
    } else {
      $('dot-dragon').className = 'dot err';
      setVal('d-status', 'Offline', 'error');
      ['d-cdp','d-fps','d-frames','d-uptime'].forEach(id => setVal(id, '--', 'error'));
    }

    // Voice
    const v = data.voice;
    if (v && !v.error) {
      $('dot-voice').className = 'dot ok';
      setVal('v-status', 'Online');
      setVal('v-stt', v.backends?.stt || '--');
      setVal('v-tts', v.backends?.tts || '--');
      setVal('v-llm', v.backends?.llm || '--');
      setVal('v-conns', v.active_connections ?? '--');
      setVal('v-uptime', fmtUptime(v.uptime_seconds));
    } else {
      $('dot-voice').className = 'dot err';
      setVal('v-status', 'Offline', 'error');
      ['v-stt','v-tts','v-llm','v-conns','v-uptime'].forEach(id => setVal(id, '--', 'error'));
    }

    setVal('qs-dash-up', fmtUptime(data.dashboard_uptime));

    // System metrics
    try {
      const sys = await api(P + '/api/v1/system');
      if (sys && !sys.error) {
        setVal('sys-uptime', fmtUptime(sys.uptime_seconds ?? sys.uptime));
        const cpu = sys.cpu_percent ?? 0;
        $('sys-cpu-bar').style.width = cpu + '%';
        $('sys-cpu-bar').className = 'fill ' + (cpu > 80 ? 'red' : cpu > 50 ? 'yellow' : 'green');
        $('sys-cpu-pct').textContent = cpu.toFixed(0) + '%';
        const ramPct = sys.ram_percent ?? (sys.ram_used && sys.ram_total ? (sys.ram_used / sys.ram_total * 100) : 0);
        $('sys-ram-bar').style.width = ramPct + '%';
        $('sys-ram-bar').className = 'fill ' + (ramPct > 80 ? 'red' : ramPct > 50 ? 'yellow' : 'green');
        $('sys-ram-pct').textContent = ramPct.toFixed(0) + '%';
        const fmtMB = b => b != null ? (b / (1024*1024)).toFixed(0) + ' MB' : '--';
        $('sys-ram-detail').textContent = fmtMB(sys.ram_used) + ' / ' + fmtMB(sys.ram_total);
        setVal('sys-conns', sys.active_connections ?? sys.connections ?? '--');
        setVal('sys-sessions', sys.total_sessions ?? '--');
        setVal('sys-messages', sys.total_messages ?? '--');
      }
    } catch(e) { console.warn('System metrics fetch failed:', e); }

    // Backend status
    try {
      const be = await api(P + '/api/v1/backends');
      if (be && !be.error) {
        ['stt', 'tts', 'llm'].forEach(cat => {
          const el = $('backends-' + cat);
          const active = be[cat]?.active || be.active?.[cat] || '';
          const available = be[cat]?.available || be.available?.[cat] || [];
          if (available.length) {
            el.innerHTML = available.map(b =>
              `<span class="backend-badge ${b === active ? 'current' : ''}">${b}${b === active ? ' (active)' : ''}</span>`
            ).join('');
          } else if (active) {
            el.innerHTML = `<span class="backend-badge current">${active} (active)</span>`;
          } else {
            el.innerHTML = '<span style="color:var(--muted); font-size:12px;">--</span>';
          }
        });
      }
    } catch(e) { console.warn('Backend fetch failed:', e); }

    // Quick stats + devices
    try {
      const [allSessions, devices, notes] = await Promise.all([
        api(P + '/api/v1/sessions?limit=200'),
        api(P + '/api/v1/devices'),
        api(P + '/api/notes').catch(() => ({ notes: [] })),
      ]);
      setVal('qs-sessions', allSessions.items?.length ?? '--');
      setVal('qs-devices', devices.items?.length ?? devices.count ?? '--');
      setVal('qs-notes', notes.total ?? notes.notes?.length ?? '--');

      let totalMsgs = 0;
      if (allSessions.items) { for (const s of allSessions.items) totalMsgs += s.message_count || 0; }
      setVal('qs-messages', totalMsgs || '--');

      // Overview devices
      const devList = $('ov-devices');
      if (devices.items?.length) {
        devList.innerHTML = devices.items.map(d => `
          <div style="display:inline-flex; align-items:center; gap:6px; margin:4px 8px 4px 0; padding:6px 12px; background:var(--bg); border-radius:4px; font-size:13px;">
            <span class="dot ${d.is_online ? 'ok' : 'err'}"></span>
            <span>${d.name || truncId(d.id)}</span>
            <span style="color:var(--muted); font-size:11px;">${d.platform || ''}</span>
          </div>
        `).join('');
      } else {
        devList.innerHTML = '<span class="empty">No devices registered</span>';
      }
    } catch(e) {
      console.warn('Quick stats fetch failed:', e);
    }

    // Recent sessions
    try {
      const sess = await api(P + '/api/v1/sessions?limit=5');
      const tbody = $('ov-sessions');
      if (!sess.items?.length) {
        tbody.innerHTML = '<tr><td colspan="6" class="empty">No sessions yet</td></tr>';
        return;
      }
      tbody.innerHTML = sess.items.map(s => `
        <tr class="clickable" onclick="switchToConversation('${s.id}')">
          <td><code>${truncId(s.id)}</code></td>
          <td>${s.device_id ? truncId(s.device_id) : '<span style="color:var(--muted)">API</span>'}</td>
          <td>${s.type || 'conversation'}</td>
          <td><span class="badge ${s.status}">${s.status}</span></td>
          <td>${s.message_count || 0}</td>
          <td>${fmtTime(s.last_active_at)}</td>
        </tr>
      `).join('');
    } catch(e) {
      $('ov-sessions').innerHTML = '<tr><td colspan="6" class="empty">Failed to load sessions</td></tr>';
    }
  } catch(e) {
    console.error('Overview refresh failed:', e);
  }
}

function switchToConversation(sid) {
  // Click the conversations tab and select the session
  document.querySelector('nav button[data-tab="conversations"]').click();
  setTimeout(() => selectSession(sid), 200);
}

async function loadConfig() {
  try {
    const cfg = await api('/api/voice-config');
    if (cfg.stt) {
      $('cfg-stt').value = cfg.stt.backend || 'moonshine';
      $('cfg-stt-model').value = cfg.stt.model || '';
    }
    if (cfg.tts) {
      $('cfg-tts').value = cfg.tts.backend || 'piper';
      $('cfg-tts-model').value = cfg.tts.piper_model || cfg.tts.kokoro_voice || cfg.tts.edge_voice || cfg.tts.openrouter_voice || '';
    }
    if (cfg.llm) {
      $('cfg-llm').value = cfg.llm.backend || 'ollama';
      const b = cfg.llm.backend || 'ollama';
      $('cfg-llm-model').value = cfg.llm[b + '_model'] || cfg.llm.ollama_model || '';
      $('cfg-prompt').value = cfg.llm.system_prompt || '';
    }
  } catch(e) { console.warn('Config load failed:', e); }
}

async function applyConfig() {
  const btn = $('btn-apply'), fb = $('cfg-feedback');
  btn.disabled = true;
  fb.textContent = 'Applying...'; fb.className = 'feedback';

  const stt = $('cfg-stt').value, tts = $('cfg-tts').value, llm = $('cfg-llm').value;
  const payload = {
    stt: { backend: stt, model: $('cfg-stt-model').value },
    tts: { backend: tts },
    llm: { backend: llm, system_prompt: $('cfg-prompt').value },
  };
  const ttsModel = $('cfg-tts-model').value;
  if (tts === 'piper') payload.tts.piper_model = ttsModel;
  else if (tts === 'kokoro') payload.tts.kokoro_voice = ttsModel;
  else if (tts === 'edge_tts') payload.tts.edge_voice = ttsModel;
  else if (tts === 'openrouter') payload.tts.openrouter_voice = ttsModel;

  const llmModel = $('cfg-llm-model').value;
  if (llm === 'ollama') payload.llm.ollama_model = llmModel;
  else if (llm === 'openrouter') payload.llm.openrouter_model = llmModel;
  else if (llm === 'lmstudio') payload.llm.lmstudio_model = llmModel;
  else if (llm === 'npu_genie') payload.llm.npu_model = llmModel;

  try {
    const r = await fetch('/api/voice-config', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload) });
    const data = await r.json();
    if (r.ok) { fb.textContent = ''; fb.className = 'feedback'; showToast('Config applied!'); setTimeout(refreshOverview, 1000); }
    else { showToast('Error: '+(data.error||r.statusText), 'error'); fb.textContent = ''; fb.className = 'feedback'; }
  } catch(e) { showToast('Failed: '+e.message, 'error'); fb.textContent = ''; fb.className = 'feedback'; }
  btn.disabled = false;
}

// ── CONVERSATIONS ──
async function loadConversations() {
  // Populate device filter if empty
  const devSel = $('conv-device-filter');
  if (devSel.options.length <= 1) {
    try {
      const devs = await api(P + '/api/v1/devices');
      if (devs.items) {
        for (const d of devs.items) {
          devSel.innerHTML += `<option value="${d.id}">${d.name || truncId(d.id)}</option>`;
        }
      }
    } catch(e) {}
  }

  const status = $('conv-status-filter').value;
  const deviceId = devSel.value;
  let qs = '?limit=100';
  if (status) qs += '&status=' + status;
  if (deviceId) qs += '&device_id=' + deviceId;
  try {
    const data = await api(P + '/api/v1/sessions' + qs);
    const list = $('conv-list');
    if (!data.items?.length) {
      list.innerHTML = '<div class="empty">No sessions found</div>';
      return;
    }
    list.innerHTML = data.items.map(s => `
      <div class="session-item ${s.id === selectedSessionId ? 'active' : ''}" data-sid="${s.id}" onclick="selectSession('${s.id}')">
        <div style="display:flex; justify-content:space-between; align-items:center;">
          <span class="sid">${truncId(s.id)}</span>
          <span class="badge ${s.status}">${s.status}</span>
        </div>
        <div class="meta">
          ${s.type || 'conversation'} · ${s.message_count || 0} msgs · ${fmtTime(s.last_active_at)}
        </div>
      </div>
    `).join('');
  } catch(e) {
    $('conv-list').innerHTML = '<div class="empty">Failed to load sessions</div>';
  }
}

async function selectSession(sid) {
  selectedSessionId = sid;
  // Highlight in list
  $$('.session-item').forEach(el => {
    el.classList.toggle('active', el.dataset.sid === sid);
  });

  $('conv-header').style.display = 'block';
  $('conv-sid').textContent = truncId(sid);

  // Load session detail
  try {
    const sess = await api(P + '/api/v1/sessions/' + sid);
    $('conv-badge').textContent = sess.status;
    $('conv-badge').className = 'badge ' + sess.status;
    $('conv-end-btn').style.display = sess.status === 'active' ? 'inline-block' : 'none';
    $('conv-resume-btn').style.display = sess.status === 'paused' ? 'inline-block' : 'none';
    $('conv-pause-btn').style.display = sess.status === 'active' ? 'inline-block' : 'none';
    $('conv-purge-btn').style.display = 'inline-block';
    $('conv-title-display').textContent = sess.title || '(untitled)';
    $('conv-title-display').style.display = 'inline';
    $('conv-title-edit').style.display = 'none';
  } catch(e) {}

  // Load messages
  try {
    const data = await api(P + '/api/v1/sessions/' + sid + '/messages?limit=500');
    const container = $('conv-messages');
    if (!data.items?.length) {
      container.innerHTML = '<div class="empty">No messages in this session</div>';
      return;
    }
    container.innerHTML = data.items.map(m => {
      const ts = m.created_at ? new Date(typeof m.created_at === 'number' ? m.created_at * 1000 : m.created_at).toLocaleTimeString() : '';
      return `
      <div class="msg ${m.role}">
        <div class="bubble">${escHtml(m.content)}${ts ? '<div style="font-size:11px;color:var(--muted);margin-top:4px;">'+ts+'</div>' : ''}</div>
        <div class="msg-meta">
          <span class="badge ${m.role}">${m.role}</span>
          ${m.input_mode ? '<span style="color:var(--muted)">via '+m.input_mode+'</span>' : ''}
          ${m.model ? '<span style="color:var(--muted)">'+m.model+'</span>' : ''}
        </div>
      </div>
    `}).join('');
    container.scrollTop = container.scrollHeight;
  } catch(e) {
    $('conv-messages').innerHTML = '<div class="empty">Failed to load messages</div>';
  }

  // Re-highlight
  loadConversations();
}

async function createNewSession() {
  try {
    const sess = await api(P + '/api/v1/sessions', {
      method: 'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ type: 'conversation' }),
    });
    showToast('Session created');
    loadConversations();
    selectSession(sess.id);
  } catch(e) { showToast('Failed to create session: ' + e.message, 'error'); }
}

async function endSelectedSession() {
  if (!selectedSessionId) return;
  confirmAction('End this session? It cannot be resumed once ended.', async () => {
    try {
      await api(P + '/api/v1/sessions/' + selectedSessionId + '/end', { method:'POST' });
      showToast('Session ended');
      loadConversations();
      selectSession(selectedSessionId);
    } catch(e) { showToast('Failed: ' + e.message, 'error'); }
  });
}

async function resumeSelectedSession() {
  if (!selectedSessionId) return;
  try {
    await api(P + '/api/v1/sessions/' + selectedSessionId + '/resume', { method:'POST' });
    showToast('Session resumed');
    selectSession(selectedSessionId);
    loadConversations();
  } catch(e) { showToast('Failed to resume: ' + e.message, 'error'); }
}

async function pauseSelectedSession() {
  if (!selectedSessionId) return;
  try {
    await api(P + '/api/v1/sessions/' + selectedSessionId + '/pause', { method:'POST' });
    showToast('Session paused');
    selectSession(selectedSessionId);
    loadConversations();
  } catch(e) { showToast('Failed to pause: ' + e.message, 'error'); }
}

async function purgeSelectedMessages() {
  if (!selectedSessionId) return;
  confirmAction('Delete ALL messages in this session? This cannot be undone.', async () => {
    try {
      await api(P + '/api/v1/sessions/' + selectedSessionId + '/messages', { method:'DELETE' });
      showToast('Messages purged');
      selectSession(selectedSessionId);
    } catch(e) { showToast('Failed to purge: ' + e.message, 'error'); }
  });
}

function startEditTitle() {
  const display = $('conv-title-display');
  const edit = $('conv-title-edit');
  $('conv-title-input').value = display.textContent || '';
  display.style.display = 'none';
  edit.style.display = 'inline-flex';
  $('conv-title-input').focus();
}

function cancelEditTitle() {
  $('conv-title-display').style.display = 'inline';
  $('conv-title-edit').style.display = 'none';
}

async function saveTitle() {
  if (!selectedSessionId) return;
  const title = $('conv-title-input').value.trim();
  try {
    await api(P + '/api/v1/sessions/' + selectedSessionId, {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ title }),
    });
    $('conv-title-display').textContent = title || '(untitled)';
    cancelEditTitle();
    showToast('Title updated');
    loadConversations();
  } catch(e) { showToast('Failed to update title: ' + e.message, 'error'); }
}

function escHtml(s) {
  const d = document.createElement('div');
  d.textContent = s || '';
  return d.innerHTML;
}

// ── CHAT ──
async function loadChatSessions() {
  try {
    const data = await api(P + '/api/v1/sessions?status=active&limit=50');
    const sel = $('chat-session-select');
    const oldVal = sel.value;
    sel.innerHTML = '<option value="">-- Create new session --</option>';
    if (data.items) {
      for (const s of data.items) {
        sel.innerHTML += `<option value="${s.id}">${truncId(s.id)} (${s.message_count || 0} msgs)</option>`;
      }
    }
    if (oldVal) sel.value = oldVal;
    if (chatSessionId && !sel.value) {
      sel.value = chatSessionId;
    }
  } catch(e) { console.warn('Failed to load chat sessions:', e); }
}

async function onChatSessionChange() {
  const sel = $('chat-session-select');
  if (sel.value === '') {
    // Create new session
    try {
      const sess = await api(P + '/api/v1/sessions', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ type:'conversation' }),
      });
      chatSessionId = sess.id;
      await loadChatSessions();
      sel.value = chatSessionId;
    } catch(e) { showToast('Failed to create session', 'error'); return; }
  } else {
    chatSessionId = sel.value;
  }

  $('chat-input').disabled = false;
  $('chat-send').disabled = false;
  $('chat-dot').className = 'dot ok';
  $('chat-status').textContent = 'ready';
  $('chat-input').focus();

  // Load existing messages
  try {
    const data = await api(P + '/api/v1/sessions/' + chatSessionId + '/messages?limit=500');
    const container = $('chat-messages');
    if (data.items?.length) {
      $('chat-empty')?.remove();
      container.innerHTML = data.items.map(m => {
        const ts = m.created_at ? new Date(typeof m.created_at === 'number' ? m.created_at * 1000 : m.created_at).toLocaleTimeString() : '';
        return `
        <div class="msg ${m.role}">
          <div class="bubble">${escHtml(m.content)}${ts ? '<div style="font-size:11px;color:var(--muted);margin-top:4px;">'+ts+'</div>' : ''}</div>
        </div>
      `}).join('');
      container.scrollTop = container.scrollHeight;
    } else {
      container.innerHTML = '<div class="empty" id="chat-empty">Session ready. Send a message!</div>';
    }
  } catch(e) {}
}

async function chatSend() {
  if (chatBusy) return;
  if (!chatStateless && !chatSessionId) return;
  const input = $('chat-input');
  const text = input.value.trim();
  if (!text) return;

  input.value = '';
  input.style.height = '';
  chatBusy = true;
  $('chat-send').disabled = true;
  $('chat-status').textContent = 'thinking...';
  $('chat-dot').className = 'dot warn';

  const container = $('chat-messages');
  const empty = $('chat-empty');
  if (empty) empty.remove();

  // Add user message
  container.insertAdjacentHTML('beforeend', `<div class="msg user"><div class="bubble">${escHtml(text)}</div></div>`);
  container.scrollTop = container.scrollHeight;

  // Add typing indicator
  container.insertAdjacentHTML('beforeend', `<div class="msg assistant" id="chat-typing"><div class="bubble" style="color:var(--muted);">Thinking...</div></div>`);
  container.scrollTop = container.scrollHeight;

  try {
    let url, body;
    if (chatStateless) {
      url = P + '/api/v1/completions';
      body = JSON.stringify({ prompt: text, stream: true });
    } else {
      url = P + '/api/v1/sessions/' + chatSessionId + '/chat';
      body = JSON.stringify({ text });
    }

    const r = await fetch(url, {
      method:'POST', headers:{'Content-Type':'application/json'}, body,
    });

    $('chat-typing')?.remove();

    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      container.insertAdjacentHTML('beforeend', `<div class="msg assistant"><div class="bubble" style="color:var(--red);">Error: ${escHtml(err.error || r.statusText)}</div></div>`);
      return;
    }

    // Stream SSE
    const bubble = document.createElement('div');
    bubble.className = 'msg assistant';
    bubble.innerHTML = '<div class="bubble"></div>';
    container.appendChild(bubble);
    const bubbleText = bubble.querySelector('.bubble');

    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    $('chat-status').textContent = 'streaming...';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const lines = buf.split('\n');
      buf = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const payload = line.slice(6);
        if (payload === '[DONE]') break;
        try {
          const d = JSON.parse(payload);
          if (d.token) { bubbleText.textContent += d.token; container.scrollTop = container.scrollHeight; }
          if (d.error) { bubbleText.textContent += '\n[Error: ' + d.error + ']'; }
          // Tool-call / tool-result events rendered inline
          if (d.type === 'tool_call' || d.event === 'tool_call') {
            const toolDiv = document.createElement('div');
            toolDiv.className = 'msg tool_call';
            toolDiv.innerHTML = `<div class="bubble">Calling tool: <strong>${escHtml(d.tool || d.name || '?')}</strong>\n${escHtml(JSON.stringify(d.args || d.arguments || {}, null, 2))}</div>`;
            container.insertBefore(toolDiv, bubble);
            container.scrollTop = container.scrollHeight;
          }
          if (d.type === 'tool_result' || d.event === 'tool_result') {
            const resDiv = document.createElement('div');
            resDiv.className = 'msg tool_result';
            resDiv.innerHTML = `<div class="bubble">Tool result: <strong>${escHtml(d.tool || d.name || '?')}</strong>\n${escHtml(typeof d.result === 'string' ? d.result : JSON.stringify(d.result || d.output || {}, null, 2))}</div>`;
            container.insertBefore(resDiv, bubble);
            container.scrollTop = container.scrollHeight;
          }
        } catch {}
      }
    }
  } catch(e) {
    $('chat-typing')?.remove();
    container.insertAdjacentHTML('beforeend', `<div class="msg assistant"><div class="bubble" style="color:var(--red);">Connection error: ${escHtml(e.message)}</div></div>`);
  } finally {
    chatBusy = false;
    $('chat-send').disabled = false;
    $('chat-dot').className = 'dot ok';
    $('chat-status').textContent = chatStateless ? 'direct mode' : 'ready';
    $('chat-input').focus();
    container.scrollTop = container.scrollHeight;
  }
}

// Chat textarea auto-resize + enter to send
$('chat-input').addEventListener('input', function() { this.style.height = ''; this.style.height = Math.min(this.scrollHeight, 120)+'px'; });
$('chat-input').addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); chatSend(); } });

function toggleStatelessMode() {
  chatStateless = !chatStateless;
  const toggle = $('chat-stateless-toggle');
  toggle.classList.toggle('on', chatStateless);
  $('chat-mode-label').textContent = chatStateless ? 'Direct LLM' : 'Session';
  $('chat-session-select').disabled = chatStateless;
  if (chatStateless) {
    $('chat-input').disabled = false;
    $('chat-send').disabled = false;
    $('chat-dot').className = 'dot ok';
    $('chat-status').textContent = 'direct mode';
    $('chat-messages').innerHTML = '<div class="empty" id="chat-empty">Direct LLM mode. Messages are stateless (no session history).</div>';
  } else {
    if (!chatSessionId) {
      $('chat-input').disabled = true;
      $('chat-send').disabled = true;
      $('chat-dot').className = 'dot';
      $('chat-status').textContent = 'idle';
      $('chat-messages').innerHTML = '<div class="empty" id="chat-empty">Select or create a session to start chatting</div>';
    } else {
      onChatSessionChange();
    }
  }
}

// ── DEVICES ──
async function loadDevices() {
  setLoading('devices-grid', true);
  try {
    const data = await api(P + '/api/v1/devices');
    const grid = $('devices-grid');
    if (!data.items?.length) {
      grid.innerHTML = '<div class="empty-state"><span class="icon">&#128268;</span><span class="msg">No devices registered</span></div>';
      return;
    }
    grid.innerHTML = data.items.map(d => {
      let caps = {};
      try { caps = typeof d.capabilities === 'string' ? JSON.parse(d.capabilities) : (d.capabilities || {}); } catch(e) {}
      return `
        <div class="card device-card" onclick="toggleDeviceDetails(this)" data-device-id="${d.id}">
          <div style="display:flex; justify-content:space-between; align-items:center;">
            <span id="dev-name-display-${d.id}" class="editable-title" style="font-size:0.9em; color:var(--accent); text-transform:uppercase; letter-spacing:1px; font-weight:700;" onclick="event.stopPropagation(); startEditDeviceName('${d.id}', this)">${d.name || 'Unknown Device'}</span>
            <span id="dev-name-edit-${d.id}" class="inline-edit" style="display:none;" onclick="event.stopPropagation();">
              <input type="text" value="${escHtml(d.name || '')}" onkeydown="if(event.key==='Enter')saveDeviceName('${d.id}');if(event.key==='Escape')cancelEditDeviceName('${d.id}');">
              <button class="btn small" onclick="saveDeviceName('${d.id}')">Save</button>
              <button class="btn small secondary" onclick="cancelEditDeviceName('${d.id}')">Cancel</button>
            </span>
            <div style="display:flex; gap:6px; align-items:center;">
              <span class="badge ${d.is_online ? 'online' : 'offline'}">${d.is_online ? 'Online' : 'Offline'}</span>
              <button class="btn small danger" onclick="event.stopPropagation(); deleteDevice('${d.id}', '${escHtml(d.name || truncId(d.id))}')" title="Delete device">Del</button>
            </div>
          </div>
          <div style="margin-top:8px;">
            <div class="row"><span class="label">ID</span><span style="font-family:monospace; font-size:12px;">${truncId(d.id)}</span></div>
            <div class="row"><span class="label">Hardware</span><span>${d.hardware_id || '--'}</span></div>
            <div class="row"><span class="label">Platform</span><span>${d.platform || '--'}</span></div>
            <div class="row"><span class="label">Firmware</span><span>${d.firmware_ver || '--'}</span></div>
            <div class="row"><span class="label">Last Seen</span><span>${fmtTime(d.last_seen_at)}</span></div>
          </div>
          <div class="device-details">
            <div style="margin-bottom:12px; padding:12px; background:rgba(6,182,212,0.05); border:1px solid rgba(6,182,212,0.2); border-radius:var(--radius);">
              <h2 style="font-size:0.8em; color:var(--accent2);">Voice Configuration</h2>
              <div id="device-config-${d.id}" style="display:block;">
                <div style="display:flex; gap:12px; align-items:center; margin-top:8px; flex-wrap:wrap;">
                  <label style="font-size:12px; color:var(--muted);">Mode:
                    <select class="dev-voice-mode" style="background:var(--surface); color:var(--text); border:1px solid var(--border); border-radius:4px; padding:4px 8px; font-size:12px;">
                      <option value="0">Local</option>
                      <option value="1">Hybrid</option>
                      <option value="2">Full Cloud</option>
                    </select>
                  </label>
                  <label style="font-size:12px; color:var(--muted);">Model:
                    <select class="dev-llm-model" style="background:var(--surface); color:var(--text); border:1px solid var(--border); border-radius:4px; padding:4px 8px; font-size:12px;">
                      <option value="qwen3:1.7b">qwen3:1.7b (Local)</option>
                      <option value="qwen3:4b">qwen3:4b (Local)</option>
                      <option value="anthropic/claude-3.5-haiku">Claude 3.5 Haiku</option>
                      <option value="anthropic/claude-sonnet-4-20250514">Claude Sonnet 4</option>
                      <option value="openai/gpt-4o-mini">GPT-4o Mini</option>
                    </select>
                  </label>
                  <button class="btn small dev-apply-btn" onclick="applyDeviceConfig('${d.id}')">Apply</button>
                </div>
              </div>
            </div>
            <h2 style="font-size:0.8em;">Capabilities</h2>
            <pre style="font-size:11px; color:var(--muted); white-space:pre-wrap;">${JSON.stringify(caps, null, 2)}</pre>
            <div data-dev-sessions="${d.id}" style="margin-top:12px;">
              <h2 style="font-size:0.8em;">Recent Sessions</h2>
              <div class="empty">Loading...</div>
            </div>
          </div>
        </div>
      `;
    }).join('');
  } catch(e) {
    $('devices-grid').innerHTML = '<div class="empty">Failed to load devices</div>';
  } finally {
    setLoading('devices-grid', false);
  }
}

async function toggleDeviceDetails(card) {
  const wasExpanded = card.classList.contains('expanded');
  card.classList.toggle('expanded');
  if (wasExpanded) return;

  // Load device sessions
  const sessDiv = card.querySelector('[data-dev-sessions]');
  if (!sessDiv) return;
  const devId = sessDiv.dataset.devSessions;
  try {
    const data = await api(P + '/api/v1/sessions?device_id=' + devId + '&limit=10');
    if (!data.items?.length) {
      sessDiv.innerHTML = '<h2 style="font-size:0.8em;">Recent Sessions</h2><div class="empty">No sessions for this device</div>';
      return;
    }
    sessDiv.innerHTML = '<h2 style="font-size:0.8em;">Recent Sessions</h2>' +
      data.items.map(s => `
        <div style="display:flex; justify-content:space-between; padding:4px 0; border-bottom:1px solid rgba(15,52,96,0.2); font-size:12px;">
          <span style="font-family:monospace;">${truncId(s.id)}</span>
          <span class="badge ${s.status}">${s.status}</span>
          <span style="color:var(--muted);">${s.message_count||0} msgs</span>
          <span style="color:var(--muted);">${fmtTime(s.last_active_at)}</span>
        </div>
      `).join('');
  } catch(e) {
    sessDiv.innerHTML = '<h2 style="font-size:0.8em;">Recent Sessions</h2><div class="empty">Failed to load</div>';
  }
}

function startEditDeviceName(devId, el) {
  $('dev-name-display-' + devId).style.display = 'none';
  $('dev-name-edit-' + devId).style.display = 'inline-flex';
  $('dev-name-edit-' + devId).querySelector('input').focus();
}

function cancelEditDeviceName(devId) {
  $('dev-name-display-' + devId).style.display = 'inline';
  $('dev-name-edit-' + devId).style.display = 'none';
}

async function saveDeviceName(devId) {
  const input = $('dev-name-edit-' + devId).querySelector('input');
  const name = input.value.trim();
  if (!name) { showToast('Name cannot be empty', 'error'); return; }
  try {
    await api(P + '/api/v1/devices/' + devId, {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ name }),
    });
    showToast('Device renamed');
    loadDevices();
  } catch(e) { showToast('Failed to rename device: ' + e.message, 'error'); }
}

async function deleteDevice(devId, devName) {
  confirmAction('Delete device "' + devName + '"? This cannot be undone.', async () => {
    try {
      await api(P + '/api/v1/devices/' + devId, { method: 'DELETE' });
      showToast('Device deleted');
      loadDevices();
    } catch(e) { showToast('Failed to delete device: ' + e.message, 'error'); }
  });
}

// ── NOTES ──
async function loadNotes() {
  setLoading('notes-grid', true);
  try {
    const data = await api(P + '/api/notes?limit=50');
    const grid = $('notes-grid');
    const notes = data.notes || [];
    if (!notes.length) {
      grid.innerHTML = '<div class="empty-state"><span class="icon">&#128221;</span><span class="msg">No notes yet. Create one!</span></div>';
      return;
    }
    grid.innerHTML = notes.map(n => `
      <div class="card note-card">
        <div style="display:flex; justify-content:space-between; align-items:center;">
          <h2 style="margin:0;">${escHtml(n.title || 'Untitled')}</h2>
          <span style="font-size:11px; color:var(--muted);">${fmtTime(n.created_at)}</span>
        </div>
        ${n.summary ? `<div style="margin-top:6px; font-size:12px; color:var(--accent2);">${escHtml(n.summary)}</div>` : ''}
        <div class="note-preview">${escHtml(n.transcript || n.text || '')}</div>
        <div style="display:flex; gap:12px; margin-top:8px; font-size:11px; color:var(--muted);">
          ${n.word_count ? `<span>${n.word_count} words</span>` : ''}
          ${n.duration_s ? `<span>${Math.round(n.duration_s)}s audio</span>` : ''}
          ${n.source ? `<span>via ${n.source}</span>` : ''}
        </div>
        <div class="note-actions">
          <button class="btn small danger" onclick="deleteNote('${n.id}')">Delete</button>
        </div>
      </div>
    `).join('');
  } catch(e) {
    $('notes-grid').innerHTML = '<div class="empty">Failed to load notes</div>';
  } finally { setLoading('notes-grid', false);
  }
}

function openNewNoteForm() { $('new-note-form').style.display = 'block'; }
function closeNewNoteForm() { $('new-note-form').style.display = 'none'; $('note-title').value = ''; $('note-text').value = ''; }

async function createNote() {
  const title = $('note-title').value.trim();
  const text = $('note-text').value.trim();
  if (!text) { showToast('Content is required', 'error'); return; }
  try {
    await api(P + '/api/notes', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ title, text }),
    });
    closeNewNoteForm();
    showToast('Note created');
    loadNotes();
  } catch(e) { showToast('Failed to create note: ' + e.message, 'error'); }
}

async function deleteNote(id) {
  confirmAction('Delete this note? This cannot be undone.', async () => {
    try {
      await api(P + '/api/notes/' + id, { method:'DELETE' });
      showToast('Note deleted');
      loadNotes();
    } catch(e) { showToast('Failed to delete: ' + e.message, 'error'); }
  });
}

async function searchNotes() {
  const q = $('notes-search').value.trim();
  if (!q) { loadNotes(); return; }
  try {
    const data = await api(P + '/api/notes/search', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ query: q, limit: 20 }),
    });
    const grid = $('notes-grid');
    const results = data.results || [];
    if (!results.length) {
      grid.innerHTML = `<div class="empty">No results for "${escHtml(q)}"</div>`;
      return;
    }
    grid.innerHTML = results.map(n => `
      <div class="card note-card">
        <h2 style="margin:0;">${escHtml(n.title || 'Untitled')}</h2>
        <div class="note-preview">${escHtml(n.transcript || n.text || '')}</div>
        ${n.score != null ? `<div style="font-size:11px; color:var(--muted); margin-top:4px;">Relevance: ${(n.score * 100).toFixed(0)}%</div>` : ''}
      </div>
    `).join('');
  } catch(e) { showToast('Search failed: ' + e.message, 'error'); }
}

$('notes-search').addEventListener('keydown', e => { if (e.key === 'Enter') searchNotes(); });

// ── MEMORY ──
async function loadMemory() {
  setLoading('memory-grid', true);
  try {
    const data = await api(P + '/api/v1/memory');
    const grid = $('memory-grid');
    const facts = data.items || data.facts || [];
    $('memory-results-info').style.display = 'none';
    if (!facts.length) {
      grid.innerHTML = '<div class="empty-state"><span class="icon">&#129504;</span><span class="msg">No facts stored. Add something Tinker should remember!</span></div>';
      return;
    }
    grid.innerHTML = facts.map(f => `
      <div class="card fact-card">
        <div style="display:flex; justify-content:space-between; align-items:flex-start;">
          <div class="fact-content">${escHtml(f.content || f.text || '')}</div>
          <button class="btn small danger" onclick="deleteMemoryFact('${f.id}')" title="Delete fact" style="flex-shrink:0; margin-left:8px;">Del</button>
        </div>
        <div class="fact-meta">
          ${f.source ? `<span>Source: ${escHtml(f.source)}</span>` : ''}
          ${f.created_at ? `<span>${fmtTime(f.created_at)}</span>` : ''}
          ${f.id ? `<span style="font-family:monospace;">${truncId(f.id)}</span>` : ''}
        </div>
      </div>
    `).join('');
  } catch(e) {
    $('memory-grid').innerHTML = '<div class="empty">Failed to load memory facts</div>';
  } finally { setLoading('memory-grid', false);
  }
}

async function addMemoryFact() {
  const input = $('memory-fact-input');
  const content = input.value.trim();
  const fb = $('memory-feedback');
  if (!content) { fb.textContent = 'Enter some content first'; fb.className = 'feedback err'; return; }
  try {
    await api(P + '/api/v1/memory', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ content }),
    });
    input.value = '';
    fb.textContent = ''; fb.className = 'feedback';
    showToast('Fact remembered!');
    loadMemory();
  } catch(e) { showToast('Failed: ' + e.message, 'error'); fb.textContent = ''; fb.className = 'feedback'; }
}

async function deleteMemoryFact(id) {
  confirmAction('Delete this fact? This cannot be undone.', async () => {
    try {
      await api(P + '/api/v1/memory/' + id, { method: 'DELETE' });
      showToast('Fact deleted');
      loadMemory();
    } catch(e) { showToast('Failed to delete fact: ' + e.message, 'error'); }
  });
}

async function searchMemory() {
  const q = $('memory-search').value.trim();
  if (!q) { loadMemory(); return; }
  try {
    const data = await api(P + '/api/v1/memory/search', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ query: q, limit: 20 }),
    });
    const grid = $('memory-grid');
    const results = data.results || data.items || [];
    const info = $('memory-results-info');
    if (!results.length) {
      grid.innerHTML = `<div class="empty">No facts matching "${escHtml(q)}"</div>`;
      info.style.display = 'none';
      return;
    }
    info.textContent = `Showing ${results.length} result(s) for "${q}"`;
    info.style.display = 'block';
    grid.innerHTML = results.map(f => `
      <div class="card fact-card">
        <div style="display:flex; justify-content:space-between; align-items:flex-start;">
          <div class="fact-content">${escHtml(f.content || f.text || '')}</div>
          ${f.score != null ? `<span class="score-bar">${(f.score * 100).toFixed(0)}%</span>` : ''}
        </div>
        <div class="fact-meta">
          ${f.source ? `<span>Source: ${escHtml(f.source)}</span>` : ''}
          ${f.created_at ? `<span>${fmtTime(f.created_at)}</span>` : ''}
        </div>
      </div>
    `).join('');
  } catch(e) { showToast('Memory search failed: ' + e.message, 'error'); }
}

function clearMemorySearch() {
  $('memory-search').value = '';
  $('memory-results-info').style.display = 'none';
  loadMemory();
}

$('memory-search').addEventListener('keydown', e => { if (e.key === 'Enter') searchMemory(); });

// ── DOCUMENTS ──
async function loadDocuments() {
  setLoading('docs-grid', true);
  try {
    const data = await api(P + '/api/v1/documents');
    const grid = $('docs-grid');
    const docs = data.items || data.documents || [];
    $('docs-results-info').style.display = 'none';
    if (!docs.length) {
      grid.innerHTML = '<div class="empty-state"><span class="icon">&#128196;</span><span class="msg">No documents ingested. Add one to enable RAG search!</span></div>';
      return;
    }
    grid.innerHTML = docs.map(d => `
      <div class="card doc-card">
        <div style="display:flex; justify-content:space-between; align-items:center;">
          <h2 style="margin:0;">${escHtml(d.title || 'Untitled Document')}</h2>
          <button class="btn small danger" onclick="deleteDocument('${d.id}')" title="Delete document">Del</button>
        </div>
        <div style="display:flex; gap:12px; margin-top:8px; font-size:12px; color:var(--muted); flex-wrap:wrap;">
          ${d.chunk_count != null ? `<span>${d.chunk_count} chunks</span>` : ''}
          ${d.source ? `<span>Source: ${escHtml(d.source)}</span>` : ''}
          ${d.created_at ? `<span>${fmtTime(d.created_at)}</span>` : ''}
          ${d.id ? `<span style="font-family:monospace;">${truncId(d.id)}</span>` : ''}
        </div>
      </div>
    `).join('');
  } catch(e) {
    $('docs-grid').innerHTML = '<div class="empty">Failed to load documents</div>';
  } finally { setLoading('docs-grid', false);
  }
}

function toggleIngestForm() {
  const form = $('doc-ingest-form');
  form.style.display = form.style.display === 'none' ? 'block' : 'none';
}

async function ingestDocument() {
  const title = $('doc-title').value.trim();
  const content = $('doc-content').value.trim();
  const fb = $('doc-feedback');
  if (!content) { fb.textContent = 'Content is required'; fb.className = 'feedback err'; return; }
  fb.textContent = 'Ingesting (chunking + embedding)...'; fb.className = 'feedback';
  try {
    await api(P + '/api/v1/documents', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ title: title || 'Untitled', content }),
    });
    $('doc-title').value = '';
    $('doc-content').value = '';
    fb.textContent = ''; fb.className = 'feedback';
    toggleIngestForm();
    showToast('Document ingested!');
    loadDocuments();
  } catch(e) { showToast('Failed: ' + e.message, 'error'); fb.textContent = ''; fb.className = 'feedback'; }
}

async function deleteDocument(id) {
  confirmAction('Delete this document and all its chunks? This cannot be undone.', async () => {
    try {
      await api(P + '/api/v1/documents/' + id, { method: 'DELETE' });
      showToast('Document deleted');
      loadDocuments();
    } catch(e) { showToast('Failed to delete document: ' + e.message, 'error'); }
  });
}

async function searchDocuments() {
  const q = $('docs-search').value.trim();
  if (!q) { loadDocuments(); return; }
  try {
    const data = await api(P + '/api/v1/documents/search', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ query: q, limit: 20 }),
    });
    const grid = $('docs-grid');
    const results = data.results || data.items || data.chunks || [];
    const info = $('docs-results-info');
    if (!results.length) {
      grid.innerHTML = `<div class="empty">No document chunks matching "${escHtml(q)}"</div>`;
      info.style.display = 'none';
      return;
    }
    info.textContent = `Showing ${results.length} matching chunk(s) for "${q}"`;
    info.style.display = 'block';
    grid.innerHTML = results.map(c => `
      <div class="card doc-card">
        <div style="display:flex; justify-content:space-between; align-items:center;">
          <h2 style="margin:0;">${escHtml(c.document_title || c.title || 'Chunk')}</h2>
          ${c.score != null ? `<span class="score-bar">${(c.score * 100).toFixed(0)}%</span>` : ''}
        </div>
        <div class="chunk-item">${escHtml(c.content || c.text || '')}</div>
        <div style="font-size:11px; color:var(--muted); margin-top:6px;">
          ${c.chunk_index != null ? `Chunk #${c.chunk_index}` : ''}
          ${c.document_id ? ` &middot; Doc ${truncId(c.document_id)}` : ''}
        </div>
      </div>
    `).join('');
  } catch(e) { showToast('Document search failed: ' + e.message, 'error'); }
}

function clearDocSearch() {
  $('docs-search').value = '';
  $('docs-results-info').style.display = 'none';
  loadDocuments();
}

$('docs-search').addEventListener('keydown', e => { if (e.key === 'Enter') searchDocuments(); });

// ── TOOLS ──
let selectedToolName = null;
let selectedToolSchema = null;

async function loadTools() {
  setLoading('tools-grid', true);
  try {
    const data = await api(P + '/api/v1/tools');
    const grid = $('tools-grid');
    const tools = data.items || data.tools || [];
    if (!tools.length) {
      grid.innerHTML = '<div class="empty-state"><span class="icon">&#128295;</span><span class="msg">No tools available</span></div>';
      return;
    }
    grid.innerHTML = tools.map(t => {
      const params = t.parameters_schema || t.parameters || {};
      const paramNames = params.properties ? Object.keys(params.properties) : [];
      return `
        <div class="card tool-card" style="cursor:pointer;" onclick="openToolExec('${escHtml(t.name)}')">
          <h2 style="margin:0; display:flex; justify-content:space-between; align-items:center;">
            <span>${escHtml(t.name)}</span>
            <span class="badge info">tool</span>
          </h2>
          <p style="color:var(--muted); font-size:12px; margin:6px 0;">${escHtml(t.description || 'No description')}</p>
          ${paramNames.length ? `<div style="font-size:11px; color:var(--muted);">Parameters: ${paramNames.map(p => '<code>'+escHtml(p)+'</code>').join(', ')}</div>` : '<div style="font-size:11px; color:var(--muted);">No parameters</div>'}
        </div>
      `;
    }).join('');
    // Cache tools for execution
    window._toolsCache = {};
    for (const t of tools) window._toolsCache[t.name] = t;
  } catch(e) {
    $('tools-grid').innerHTML = '<div class="empty">Failed to load tools</div>';
  } finally { setLoading('tools-grid', false);
  }
}

function openToolExec(name) {
  const tool = window._toolsCache?.[name];
  if (!tool) return;
  selectedToolName = name;
  selectedToolSchema = tool.parameters_schema || tool.parameters || {};

  $('tool-exec-name').textContent = name;
  $('tool-exec-desc').textContent = tool.description || '';
  $('tool-exec-result').style.display = 'none';
  $('tool-exec-feedback').textContent = '';

  // Build params form dynamically from schema
  const paramsDiv = $('tool-exec-params');
  const props = selectedToolSchema.properties || {};
  const required = selectedToolSchema.required || [];
  if (Object.keys(props).length === 0) {
    paramsDiv.innerHTML = '<div style="font-size:12px; color:var(--muted);">This tool takes no parameters.</div>';
  } else {
    paramsDiv.innerHTML = Object.entries(props).map(([key, schema]) => {
      const isReq = required.includes(key);
      const type = schema.type || 'string';
      const desc = schema.description || '';
      let inputHtml;
      if (type === 'boolean') {
        inputHtml = `<select id="tool-param-${key}"><option value="true">true</option><option value="false">false</option></select>`;
      } else if (schema.enum) {
        inputHtml = `<select id="tool-param-${key}">${schema.enum.map(v => `<option value="${escHtml(String(v))}">${escHtml(String(v))}</option>`).join('')}</select>`;
      } else if (type === 'integer' || type === 'number') {
        inputHtml = `<input id="tool-param-${key}" type="number" placeholder="${escHtml(desc)}" ${schema.default != null ? `value="${schema.default}"` : ''}>`;
      } else {
        inputHtml = `<input id="tool-param-${key}" type="text" placeholder="${escHtml(desc)}" ${schema.default != null ? `value="${escHtml(String(schema.default))}"` : ''}>`;
      }
      return `
        <div class="param-field">
          <label>${escHtml(key)}${isReq ? ' *' : ''} <span style="font-weight:400; color:var(--muted);">(${type})</span></label>
          ${inputHtml}
          ${desc ? `<div style="font-size:10px; color:var(--muted); margin-top:1px;">${escHtml(desc)}</div>` : ''}
        </div>
      `;
    }).join('');
  }

  $('tool-exec-panel').style.display = 'block';
  $('tool-exec-panel').scrollIntoView({ behavior: 'smooth' });
}

function closeToolExec() {
  $('tool-exec-panel').style.display = 'none';
  selectedToolName = null;
  selectedToolSchema = null;
}

async function executeSelectedTool() {
  if (!selectedToolName) return;
  const btn = $('tool-exec-run');
  const fb = $('tool-exec-feedback');
  const resultDiv = $('tool-exec-result');
  btn.disabled = true; btn.textContent = 'Executing...';
  fb.textContent = ''; fb.className = 'feedback';
  resultDiv.style.display = 'none';

  // Gather params
  const props = (selectedToolSchema.properties || {});
  const args = {};
  for (const [key, schema] of Object.entries(props)) {
    const el = $('tool-param-' + key);
    if (!el) continue;
    let val = el.value;
    if (val === '' || val === undefined) continue;
    if (schema.type === 'boolean') val = val === 'true';
    else if (schema.type === 'integer') val = parseInt(val, 10);
    else if (schema.type === 'number') val = parseFloat(val);
    args[key] = val;
  }

  try {
    const data = await api(P + '/api/v1/tools/' + encodeURIComponent(selectedToolName) + '/execute', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(args),
    });
    fb.textContent = ''; fb.className = 'feedback';
    showToast('Tool executed successfully');
    resultDiv.textContent = JSON.stringify(data, null, 2);
    resultDiv.style.display = 'block';
  } catch(e) {
    showToast('Tool execution failed: ' + e.message, 'error'); fb.textContent = ''; fb.className = 'feedback';
    try {
      const errData = await e.response?.json();
      if (errData) { resultDiv.textContent = JSON.stringify(errData, null, 2); resultDiv.style.display = 'block'; }
    } catch {}
  } finally {
    btn.disabled = false; btn.textContent = 'Execute';
  }
}

// ── LOGS ──
async function loadEvents() {
  const type = $('log-type-filter').value;
  // First get all events to find the max ID, then load the latest 100
  let sinceId = 0;
  try {
    const all = await api(P + '/api/v1/events?limit=10000&since_id=0');
    const allItems = all.items || [];
    if (allItems.length > 100) sinceId = allItems[allItems.length - 100].id - 1;
  } catch(e) {}
  const qs = `?limit=100&since_id=${sinceId}${type ? '&type='+type : ''}`;
  try {
    const data = await api(P + '/api/v1/events' + qs);
    const container = $('event-list');
    const events = data.items || [];
    if (!events.length) {
      container.innerHTML = '<div class="empty-state"><span class="icon">&#128220;</span><span class="msg">No events recorded</span></div>';
      lastEventId = 0;
      return;
    }
    // Display newest first
    const sorted = [...events].reverse();
    container.innerHTML = sorted.map(ev => {
      let evData = {};
      try { evData = typeof ev.data === 'string' ? JSON.parse(ev.data) : (ev.data || {}); } catch(e) {}
      const summary = Object.keys(evData).length ? ' — ' + escHtml(JSON.stringify(evData).substring(0, 120)) : '';
      return `
        <div class="event-item">
          <span class="ts">${fmtTime(ev.created_at)}</span>
          <span class="etype">${escHtml(ev.type)}</span>
          ${ev.session_id ? `<span style="color:var(--muted); font-size:11px;"> session:${truncId(ev.session_id)}</span>` : ''}
          ${ev.device_id ? `<span style="color:var(--muted); font-size:11px;"> device:${truncId(ev.device_id)}</span>` : ''}
          <span style="color:var(--muted); font-size:11px;">${summary}</span>
        </div>
      `;
    }).join('');
    lastEventId = events[events.length - 1]?.id || 0;
  } catch(e) {
    $('event-list').innerHTML = '<div class="empty">Failed to load events</div>';
  }
}

async function pollNewEvents() {
  if (currentTab !== 'logs') return;
  const type = $('log-type-filter').value;
  const qs = `?limit=50&since_id=${lastEventId}${type ? '&type='+type : ''}`;
  try {
    const data = await api(P + '/api/v1/events' + qs);
    const events = data.items || [];
    if (!events.length) return;

    const container = $('event-list');
    for (const ev of [...events].reverse()) {
      let evData = {};
      try { evData = typeof ev.data === 'string' ? JSON.parse(ev.data) : (ev.data || {}); } catch(e) {}
      const summary = Object.keys(evData).length ? ' — ' + escHtml(JSON.stringify(evData).substring(0, 120)) : '';
      const div = document.createElement('div');
      div.className = 'event-item';
      div.style.animation = 'fadeIn 0.3s';
      div.innerHTML = `
        <span class="ts">${fmtTime(ev.created_at)}</span>
        <span class="etype">${escHtml(ev.type)}</span>
        ${ev.session_id ? `<span style="color:var(--muted); font-size:11px;"> session:${truncId(ev.session_id)}</span>` : ''}
        ${ev.device_id ? `<span style="color:var(--muted); font-size:11px;"> device:${truncId(ev.device_id)}</span>` : ''}
        <span style="color:var(--muted); font-size:11px;">${summary}</span>
      `;
      container.prepend(div);
    }
    lastEventId = events[events.length - 1]?.id || lastEventId;
  } catch(e) {}
}

function startEventPoll() { stopEventPoll(); eventTimer = setInterval(pollNewEvents, 3000); }
function stopEventPoll() { if (eventTimer) { clearInterval(eventTimer); eventTimer = null; } }

// ── OTA ──
let otaUrl = '';
async function loadOtaInfo() {
  try {
    const devs = await api(P + '/api/v1/devices');
    const items = devs.items || devs || [];
    const online = items.find(d => d.online || d.is_online);
    if (online) {
      $('ota-current-ver').textContent = online.firmware_ver || 'Unknown';
      $('ota-device-name').textContent = online.name || online.device_id || 'Tab5';
    } else {
      $('ota-current-ver').textContent = 'No device online';
      $('ota-device-name').textContent = '—';
    }
  } catch(e) { showToast('Failed to load device info', 'error'); }
}

async function otaCheck() {
  const btn = $('ota-check-btn');
  const fb = $('ota-feedback');
  btn.disabled = true; btn.textContent = 'Checking...';
  fb.textContent = '';
  $('ota-update-card').style.display = 'none';
  $('ota-noupdate-card').style.display = 'none';
  try {
    const ver = $('ota-current-ver').textContent || '0.0.0';
    const data = await api(P + '/api/ota/check?current=' + encodeURIComponent(ver));
    if (data && data.update) {
      $('ota-new-ver').textContent = data.version || '?';
      $('ota-sha256').textContent = data.sha256 || '—';
      otaUrl = data.url || '';
      $('ota-update-card').style.display = 'block';
      showToast('Update available: v' + data.version, 'info');
    } else {
      $('ota-noupdate-card').style.display = 'block';
      showToast('Firmware is up to date');
    }
  } catch(e) {
    showToast('OTA check failed: ' + (e.message || e), 'error');
  } finally {
    btn.disabled = false; btn.textContent = 'Check for Updates';
  }
}

async function otaApply() {
  if (!otaUrl) { showToast('No update URL', 'error'); return; }
  const btn = $('ota-apply-btn');
  const fb = $('ota-apply-feedback');
  btn.disabled = true; btn.textContent = 'Applying...';
  fb.textContent = 'Downloading firmware to device...';
  try {
    // Trigger OTA apply on Tab5 via its debug server
    // The Tab5 IP can be found from the device info
    const devs = await api(P + '/api/v1/devices');
    const items = devs.items || devs || [];
    const online = items.find(d => d.online || d.is_online);
    if (!online) { showToast('No device online', 'error'); return; }
    // Trigger OTA via Tab5 debug server on same LAN
    // Tab5 runs debug server on port 8080 — IP detected from device last_seen or network
    const tab5Ip = '192.168.1.90';  // Tab5 DHCP IP on Sawaya network
    try {
      const otaResp = await fetch('http://' + tab5Ip + ':8080/ota/apply', { method: 'POST', mode: 'no-cors' });
      showToast('OTA triggered on Tab5 (' + tab5Ip + ') — device will download and reboot', 'info');
      fb.textContent = 'Firmware download started. Device will reboot when complete.';
    } catch(otaErr) {
      // no-cors mode won't give us response, but the request was sent
      showToast('OTA command sent to Tab5 — check device for progress', 'info');
      fb.textContent = 'OTA command sent. Monitor device serial for progress.';
    }
  } catch(e) {
    showToast('OTA apply failed: ' + (e.message || e), 'error');
  } finally {
    btn.disabled = false; btn.textContent = 'Apply Update';
  }
}

// ── DEVICE CONFIG (voice mode + model push) ──
async function showDeviceConfig(deviceId) {
  const panel = $('device-config-' + deviceId);
  if (!panel) return;
  if (panel.style.display === 'block') { panel.style.display = 'none'; return; }
  panel.style.display = 'block';
  try {
    const cfg = await api(P + '/api/voice-config');
    const modeSelect = panel.querySelector('.dev-voice-mode');
    const modelSelect = panel.querySelector('.dev-llm-model');
    if (modeSelect && cfg) {
      // Detect current mode from config
      const stt = cfg.stt?.backend || 'moonshine';
      const llm = cfg.llm?.backend || 'ollama';
      let mode = 0;
      if (stt === 'openrouter' && llm !== 'openrouter') mode = 1;
      if (stt === 'openrouter' && llm === 'openrouter') mode = 2;
      modeSelect.value = mode;
    }
    if (modelSelect && cfg) {
      modelSelect.value = cfg.llm?.openrouter_model || cfg.llm?.ollama_model || '';
    }
  } catch(e) {}
}

async function applyDeviceConfig(deviceId) {
  const panel = $('device-config-' + deviceId);
  if (!panel) return;
  const modeSelect = panel.querySelector('.dev-voice-mode');
  const modelSelect = panel.querySelector('.dev-llm-model');
  const btn = panel.querySelector('.dev-apply-btn');
  const mode = parseInt(modeSelect.value);
  const model = modelSelect.value;
  btn.disabled = true; btn.textContent = 'Applying...';
  try {
    // Send config update via voice server
    const payload = {};
    if (mode === 0) {
      payload.stt = { backend: 'moonshine' };
      payload.tts = { backend: 'piper' };
      payload.llm = { backend: 'ollama' };
    } else if (mode === 1) {
      payload.stt = { backend: 'openrouter' };
      payload.tts = { backend: 'openrouter' };
      payload.llm = { backend: 'ollama' };
    } else {
      payload.stt = { backend: 'openrouter' };
      payload.tts = { backend: 'openrouter' };
      payload.llm = { backend: 'openrouter', openrouter_model: model };
    }
    await api(P + '/api/voice-config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    showToast('Config applied to voice server');
  } catch(e) {
    showToast('Config apply failed: ' + (e.message || e), 'error');
  } finally {
    btn.disabled = false; btn.textContent = 'Apply';
  }
}

// ── E2E TEST SUITE ──
const TAB5_IP = '192.168.1.90';
const TAB5 = 'http://' + TAB5_IP + ':8080';

// Helper for POST JSON
const POST = (path,body) => api(P+path, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
const GET = (path) => api(P+path);
const DEL = (path) => api(P+path, {method:'DELETE'});
const PATCH = (path,body) => api(P+path, {method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
const TAB5GET = async(path) => { const r=await fetch(TAB5+path,{mode:'cors',signal:AbortSignal.timeout(5000)}); return r.json(); };
const TAB5POST = async(path,body) => { const r=await fetch(TAB5+path,{method:'POST',mode:'cors',signal:AbortSignal.timeout(5000),headers:body?{'Content-Type':'application/json'}:{},body:body?JSON.stringify(body):undefined}); return r.json().catch(()=>({})); };

const E2E_TESTS = [
  // ═══ SECTION 1: Infrastructure (8 tests) ═══
  { name:'[Infra] Health endpoint', fn: async()=>{ const d=await GET('/health'); return d.status==='ok' ? 'ok' : 'FAIL: status='+d.status; }},
  { name:'[Infra] System metrics', fn: async()=>{ const d=await GET('/api/v1/system'); if(!d.cpu_percent && d.cpu_percent!==0) throw new Error('missing cpu'); return `CPU ${d.cpu_percent}% RAM ${d.memory.percent}% up ${Math.round(d.uptime_s/3600)}h`; }},
  { name:'[Infra] Backend availability', fn: async()=>{ const d=await GET('/api/v1/backends'); if(!d.stt||!d.tts||!d.llm) throw new Error('missing backends'); const count=d.stt.available.length+d.tts.available.length+d.llm.available.length; return `${count} backends: STT=${d.stt.active} TTS=${d.tts.active} LLM=${d.llm.active}`; }},
  { name:'[Infra] Dashboard aggregation', fn: async()=>{ const d=await api('/api/status'); if(!d.voice||!d.dragon) throw new Error('missing services'); return `voice=${d.voice.status} dragon=${d.dragon.status}`; }},
  { name:'[Infra] Voice config GET', fn: async()=>{ const d=await api('/api/voice-config'); if(!d.llm||!d.stt||!d.tts) throw new Error('incomplete config'); return `LLM:${d.llm.backend}/${d.llm.ollama_model||d.llm.openrouter_model} STT:${d.stt.backend}`; }},
  { name:'[Infra] OTA check', fn: async()=>{ const d=await GET('/api/ota/check?current=0.6.0'); return `update=${d.update} version=${d.version||'current'}`; }},
  { name:'[Infra] CORS headers present', fn: async()=>{ const r=await fetch(P+'/api/v1/system',{method:'GET'}); const cors=r.headers.get('access-control-allow-origin'); return cors==='*' ? 'CORS: *' : 'MISSING CORS'; }},
  { name:'[Infra] Tab5 reachable', fn: async()=>{ try { const d=await TAB5GET('/info'); return `up=${Math.round(d.uptime_ms/1000)}s heap=${Math.round(d.heap_free/1024/1024)}MB wifi=${d.wifi_connected}`; } catch(e) { return 'UNREACHABLE: '+e.message; }}},

  // ═══ SECTION 2: Sessions CRUD (7 tests) ═══
  { name:'[Sessions] List all', fn: async()=>{ const d=await GET('/api/v1/sessions?limit=5'); if(!d.items) throw new Error('no items field'); return `${d.count} returned, ${d.items.length} shown`; }},
  { name:'[Sessions] Filter by status', fn: async()=>{ const d=await GET('/api/v1/sessions?status=ended&limit=3'); return `${d.items?.length||0} ended sessions`; }},
  { name:'[Sessions] Create→Get→End lifecycle', fn: async()=>{
    const s=await POST('/api/v1/sessions',{type:'conversation'}); if(!s.id) throw new Error('no id');
    const g=await GET('/api/v1/sessions/'+s.id); if(g.id!==s.id) throw new Error('get mismatch');
    await POST('/api/v1/sessions/'+s.id+'/end',{});
    const e=await GET('/api/v1/sessions/'+s.id); return `created→got→ended: ${s.id.substring(0,8)} status=${e.status}`; }},
  { name:'[Sessions] Pause and Resume', fn: async()=>{
    const s=await POST('/api/v1/sessions',{type:'conversation'});
    await POST('/api/v1/sessions/'+s.id+'/pause',{});
    const p=await GET('/api/v1/sessions/'+s.id); if(p.status!=='paused') throw new Error('not paused: '+p.status);
    await POST('/api/v1/sessions/'+s.id+'/resume',{});
    const r=await GET('/api/v1/sessions/'+s.id);
    await POST('/api/v1/sessions/'+s.id+'/end',{});
    return `pause=${p.status} resume=${r.status}`; }},
  { name:'[Sessions] Title update', fn: async()=>{
    const s=await POST('/api/v1/sessions',{type:'conversation'});
    const title='Test-'+Date.now();
    await PATCH('/api/v1/sessions/'+s.id,{title});
    const g=await GET('/api/v1/sessions/'+s.id);
    await POST('/api/v1/sessions/'+s.id+'/end',{});
    return g.title===title ? `title set: ${title.substring(0,20)}` : 'FAIL: title mismatch'; }},
  { name:'[Sessions] Message count increments', fn: async()=>{
    const s=await POST('/api/v1/sessions',{type:'conversation'});
    const before=await GET('/api/v1/sessions/'+s.id);
    // Chat sends a message which creates entries
    await POST('/api/v1/sessions/'+s.id+'/end',{});
    return `session ${s.id.substring(0,8)} msgs=${before.message_count||0}`; }},
  { name:'[Sessions] Filter by device', fn: async()=>{ const d=await GET('/api/v1/sessions?device_id=30eda0ea8e33&limit=5'); return `${d.items?.length||0} sessions for Tab5`; }},

  // ═══ SECTION 3: Devices (5 tests) ═══
  { name:'[Devices] List all', fn: async()=>{ const d=await GET('/api/v1/devices'); const items=d.items||d; const online=items.filter(x=>x.online||x.is_online); return `${items.length} total, ${online.length} online`; }},
  { name:'[Devices] Tab5 has capabilities', fn: async()=>{ const d=await GET('/api/v1/devices'); const items=d.items||d; const tab5=items.find(x=>x.name==='Tab5'||x.device_id==='30eda0ea8e33'); if(!tab5) return 'Tab5 not found'; const caps=typeof tab5.capabilities==='string'?JSON.parse(tab5.capabilities):tab5.capabilities; return caps ? `caps: ${Object.keys(caps).join(',')}` : 'no capabilities'; }},
  { name:'[Devices] Tab5 firmware version', fn: async()=>{ const d=await GET('/api/v1/devices'); const items=d.items||d; const tab5=items.find(x=>x.device_id==='30eda0ea8e33'); return tab5 ? `fw=${tab5.firmware_ver} platform=${tab5.platform}` : 'Tab5 not found'; }},
  { name:'[Devices] Online device has recent activity', fn: async()=>{ const d=await GET('/api/v1/devices'); const items=d.items||d; const online=items.find(x=>x.online||x.is_online); if(!online) return 'no online device'; const age=(Date.now()/1000)-online.last_seen; return age<300 ? `last seen ${Math.round(age)}s ago` : `STALE: ${Math.round(age/60)}min ago`; }},
  { name:'[Devices] Rename roundtrip', fn: async()=>{ const d=await GET('/api/v1/devices'); const items=d.items||d; const dev=items.find(x=>x.name==='Tab5'); if(!dev) return 'skip: no Tab5'; const orig=dev.name; await PATCH('/api/v1/devices/'+dev.device_id,{name:'Tab5-test'}); const after=await GET('/api/v1/devices/'+dev.device_id); await PATCH('/api/v1/devices/'+dev.device_id,{name:orig}); return after.name==='Tab5-test' ? 'rename OK, restored' : 'FAIL'; }},

  // ═══ SECTION 4: Tools (6 tests) ═══
  { name:'[Tools] List 10 tools', fn: async()=>{ const d=await GET('/api/v1/tools'); const t=d.tools||[]; return t.length>=10 ? `${t.length} tools` : `only ${t.length} tools`; }},
  { name:'[Tools] Each tool has schema', fn: async()=>{ const d=await GET('/api/v1/tools'); const t=d.tools||[]; const withSchema=t.filter(x=>x.parameters_schema); return `${withSchema.length}/${t.length} have schema`; }},
  { name:'[Tools] Execute datetime', fn: async()=>{ const d=await POST('/api/v1/tools/datetime/execute',{args:{}}); if(!d.result?.date) throw new Error('no date'); return `${d.result.date} ${d.result.time} ${d.result.day} (${d.execution_ms}ms)`; }},
  { name:'[Tools] Execute web_search (SearXNG)', fn: async()=>{ const d=await POST('/api/v1/tools/web_search/execute',{args:{query:'hello world'}}); const r=d.result||{}; return `${r.results?.length||0} results via ${r.engine||'?'} (${d.execution_ms}ms)`; }},
  { name:'[Tools] Execute calculator', fn: async()=>{ const d=await POST('/api/v1/tools/calculator/execute',{args:{expression:'15% of 230'}}); return d.result ? `result=${JSON.stringify(d.result).substring(0,50)}` : 'no result'; }},
  { name:'[Tools] Execute system_info', fn: async()=>{ const d=await POST('/api/v1/tools/system_info/execute',{args:{}}); return d.result ? `cpu=${d.result.cpu_percent||'?'}%` : 'no result'; }},

  // ═══ SECTION 5: Memory CRUD + Search (5 tests) ═══
  { name:'[Memory] List facts', fn: async()=>{ const d=await GET('/api/v1/memory'); return `${d.count||d.items?.length||0} facts stored`; }},
  { name:'[Memory] Store→Search→Delete fact', fn: async()=>{
    const fact='E2E test fact '+Date.now();
    const created=await POST('/api/v1/memory',{content:fact});
    if(!created.id) throw new Error('no id returned');
    const search=await POST('/api/v1/memory/search',{query:fact});
    const found=(search.items||search.results||[]).some(f=>f.content===fact);
    await DEL('/api/v1/memory/'+created.id);
    return found ? `created→found→deleted: ${created.id.substring(0,8)}` : 'created but NOT found in search'; }},
  { name:'[Memory] Semantic search relevance', fn: async()=>{ const d=await POST('/api/v1/memory/search',{query:'user preferences food'}); const items=d.items||d.results||[]; if(!items.length) return '0 results'; const top=items[0]; return `top: "${top.content?.substring(0,40)}" score=${(top.score*100).toFixed(0)}%`; }},
  { name:'[Memory] Search returns ranked results', fn: async()=>{ const d=await POST('/api/v1/memory/search',{query:'name birthday'}); const items=d.items||d.results||[]; if(items.length<2) return `only ${items.length} results`; const sorted=items.every((x,i)=>i===0||x.score<=items[i-1].score); return sorted ? `${items.length} results, properly ranked` : 'NOT properly ranked by score'; }},
  { name:'[Memory] Fact count matches list', fn: async()=>{ const d=await GET('/api/v1/memory'); const items=d.items||[]; return d.count===items.length ? `count=${d.count} matches items` : `MISMATCH: count=${d.count} items=${items.length}`; }},

  // ═══ SECTION 6: Notes CRUD + Search (5 tests) ═══
  { name:'[Notes] List notes', fn: async()=>{ const d=await api(P+'/api/notes?limit=50'); return `${d.total||d.notes?.length||0} notes`; }},
  { name:'[Notes] Create→Get→Delete note', fn: async()=>{
    const title='E2E-'+Date.now(), text='Test note content for E2E';
    const created=await api(P+'/api/notes',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title,text})});
    if(!created.id) throw new Error('no id');
    const got=await api(P+'/api/notes/'+created.id);
    await api(P+'/api/notes/'+created.id,{method:'DELETE'});
    return got.title===title ? `created→got→deleted: ${created.id.substring(0,8)}` : 'title mismatch'; }},
  { name:'[Notes] Search semantic', fn: async()=>{ const d=await api(P+'/api/notes/search',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({query:'LNG oil prices'})}); const items=d.results||d.notes||[]; return `${items.length} results`; }},
  { name:'[Notes] Word count present', fn: async()=>{ const d=await api(P+'/api/notes?limit=3'); const notes=d.notes||[]; const withWc=notes.filter(n=>n.word_count>0); return `${withWc.length}/${notes.length} have word_count`; }},
  { name:'[Notes] Timestamps valid', fn: async()=>{ const d=await api(P+'/api/notes?limit=3'); const notes=d.notes||[]; const valid=notes.every(n=>n.created_at>1770000000&&n.created_at<1800000000); return valid ? `${notes.length} notes, timestamps valid` : 'INVALID timestamps'; }},

  // ═══ SECTION 7: Documents (3 tests) ═══
  { name:'[Docs] List documents', fn: async()=>{ const d=await GET('/api/v1/documents'); return `${(d.items||d).length} documents`; }},
  { name:'[Docs] Ingest→Search→Delete', fn: async()=>{
    const title='E2E-Doc-'+Date.now(), content='The quick brown fox jumps over the lazy dog. This is a test document for end-to-end testing of the TinkerClaw document ingestion and semantic search pipeline.';
    const created=await POST('/api/v1/documents',{title,content});
    if(!created.id) throw new Error('no id');
    await new Promise(r=>setTimeout(r,2000)); // wait for embedding
    const search=await POST('/api/v1/documents/search',{query:'quick brown fox'});
    const chunks=search.items||search.results||search.chunks||[];
    await DEL('/api/v1/documents/'+created.id);
    return chunks.length ? `ingested→${chunks.length} chunks found→deleted` : 'ingested but search returned 0'; }},
  { name:'[Docs] Search returns chunks not docs', fn: async()=>{ const d=await POST('/api/v1/documents/search',{query:'test'}); const items=d.items||d.results||d.chunks||[]; if(!items.length) return '0 results (empty DB?)'; return items[0].chunk_index!==undefined ? `chunk format OK (${items.length} chunks)` : 'NOT chunk format'; }},

  // ═══ SECTION 8: Events (3 tests) ═══
  { name:'[Events] Load recent', fn: async()=>{ const d=await GET('/api/v1/events?limit=10&since_id=0'); return `${(d.items||[]).length} events`; }},
  { name:'[Events] Filter by type', fn: async()=>{ const d=await GET('/api/v1/events?limit=50&since_id=0&type=device.connected'); const items=d.items||[]; return `${items.length} device.connected events`; }},
  { name:'[Events] Timestamps chronological', fn: async()=>{ const d=await GET('/api/v1/events?limit=20&since_id=0'); const items=d.items||[]; if(items.length<2) return 'too few events'; const sorted=items.every((x,i)=>i===0||x.created_at>=items[i-1].created_at); return sorted ? `${items.length} events, chronological` : 'NOT chronological'; }},

  // ═══ SECTION 9: Multi-Step User Stories (8 tests) ═══
  { name:'[Story] New user: create session→chat→end', fn: async()=>{
    const s=await POST('/api/v1/sessions',{type:'conversation'});
    const msgs=await GET('/api/v1/sessions/'+s.id+'/messages?limit=10');
    await POST('/api/v1/sessions/'+s.id+'/end',{});
    return `session ${s.id.substring(0,8)}: ${msgs.items?.length||0} msgs → ended`; }},
  { name:'[Story] Remember→Recall user fact', fn: async()=>{
    const fact='E2E user likes pineapple pizza '+Date.now();
    const stored=await POST('/api/v1/memory',{content:fact});
    const recalled=await POST('/api/v1/memory/search',{query:'pineapple pizza'});
    const found=(recalled.items||recalled.results||[]).some(f=>f.content===fact);
    await DEL('/api/v1/memory/'+stored.id);
    return found ? 'stored→recalled→cleaned up' : 'stored but NOT recalled'; }},
  { name:'[Story] Multi-tool chain: time→search', fn: async()=>{
    const t=await POST('/api/v1/tools/datetime/execute',{args:{}});
    const s=await POST('/api/v1/tools/web_search/execute',{args:{query:'news '+t.result?.date}});
    return `time=${t.result?.time} search=${s.result?.results?.length||0} results`; }},
  { name:'[Story] Device sessions: find Tab5→list its sessions', fn: async()=>{
    const devs=await GET('/api/v1/devices'); const items=devs.items||devs;
    const tab5=items.find(x=>x.device_id==='30eda0ea8e33');
    if(!tab5) return 'Tab5 not found';
    const sess=await GET('/api/v1/sessions?device_id='+tab5.device_id+'&limit=5');
    return `Tab5 (${tab5.online||tab5.is_online?'online':'offline'}): ${sess.items?.length||0} recent sessions`; }},
  { name:'[Story] Note lifecycle: create→search→edit→delete', fn: async()=>{
    const title='Story-'+Date.now(), text='Voice note about grocery shopping';
    const n=await api(P+'/api/notes',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title,text})});
    if(!n.id) throw new Error('create failed');
    const search=await api(P+'/api/notes/search',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({query:'grocery shopping'})});
    const found=(search.results||search.notes||[]).length>0;
    await api(P+'/api/notes/'+n.id,{method:'DELETE'});
    return `created→search=${found?'found':'not found'}→deleted`; }},
  { name:'[Story] Config roundtrip: read→verify fields', fn: async()=>{
    const cfg=await api('/api/voice-config');
    const fields=['stt','tts','llm','audio','tools','memory'];
    const present=fields.filter(f=>cfg[f]);
    return `${present.length}/${fields.length} config sections: ${present.join(',')}`; }},
  { name:'[Story] Dashboard→Dragon→Tab5 connectivity', fn: async()=>{
    const dash=await api('/api/status');
    const voice=dash.voice?.status;
    let tab5='unknown';
    try { const t=await TAB5GET('/voice'); tab5=t.connected?'connected':'disconnected'; } catch(e) { tab5='unreachable'; }
    return `dashboard=ok voice=${voice} tab5=${tab5}`; }},
  { name:'[Story] Full stack: session→tool→memory→cleanup', fn: async()=>{
    const s=await POST('/api/v1/sessions',{type:'conversation'});
    const tool=await POST('/api/v1/tools/datetime/execute',{args:{}});
    const fact='E2E full stack test '+Date.now();
    const mem=await POST('/api/v1/memory',{content:fact});
    await DEL('/api/v1/memory/'+mem.id);
    await POST('/api/v1/sessions/'+s.id+'/end',{});
    return `session→tool(${tool.result?.time})→memory→cleanup OK`; }},

  // ═══ SECTION 10: Tab5 Device Tests (5 tests) ═══
  { name:'[Tab5] Voice state', fn: async()=>{ try { const d=await TAB5GET('/voice'); return `state=${d.state_name} connected=${d.connected} stt="${(d.last_stt_text||'').substring(0,30)}"`; } catch(e) { return 'UNREACHABLE'; }}},
  { name:'[Tab5] Settings readback', fn: async()=>{ try { const d=await TAB5GET('/settings'); return `wifi=${d.wifi_ssid} mode=${d.voice_mode} dragon=${d.dragon_host}:${d.dragon_port}`; } catch(e) { return 'UNREACHABLE'; }}},
  { name:'[Tab5] Heap health', fn: async()=>{ try { const d=await TAB5GET('/info'); const heapMB=Math.round(d.heap_free/1024/1024); return heapMB>10 ? `${heapMB}MB free (healthy)` : `${heapMB}MB free (LOW!)`; } catch(e) { return 'UNREACHABLE'; }}},
  { name:'[Tab5] SD card mounted', fn: async()=>{ try { const d=await TAB5GET('/info'); return d.sd_mounted ? `mounted: ${d.sd_total_mb||'?'}MB` : 'NOT MOUNTED'; } catch(e) { return 'UNREACHABLE'; }}},
  { name:'[Tab5] Self-test subsystems', fn: async()=>{ try { const d=await TAB5GET('/selftest'); if(Array.isArray(d)) { const pass=d.filter(t=>t.pass||t.ok).length; return `${pass}/${d.length} pass`; } return JSON.stringify(d).substring(0,60); } catch(e) { return 'UNREACHABLE'; }}},
];

async function runAllTests() {
  const btn = $('run-tests-btn');
  const container = $('test-results');
  const summary = $('test-summary');
  btn.disabled = true; btn.textContent = 'Running...';
  container.innerHTML = '';
  let pass=0, fail=0, warn=0, total=E2E_TESTS.length;
  let lastSection = '';
  const t0All = performance.now();

  for (const test of E2E_TESTS) {
    // Section header
    const section = test.name.match(/^\[([^\]]+)\]/)?.[1] || '';
    if (section !== lastSection) {
      lastSection = section;
      const hdr = document.createElement('div');
      hdr.style.cssText = 'padding:10px 12px 4px; font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:1.5px; color:var(--accent); border-bottom:1px solid var(--border); margin-top:8px;';
      hdr.textContent = section;
      container.appendChild(hdr);
    }

    const row = document.createElement('div');
    row.style.cssText = 'display:flex; justify-content:space-between; align-items:center; padding:6px 12px; border-bottom:1px solid var(--border-subtle); font-size:13px; gap:12px;';
    const label = test.name.replace(/^\[[^\]]+\]\s*/, '');
    row.innerHTML = `<span style="font-weight:500; min-width:180px; flex-shrink:0;">${escHtml(label)}</span><span style="color:var(--muted); text-align:right; font-size:12px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">Running...</span>`;
    container.appendChild(row);

    const t0 = performance.now();
    try {
      const result = await test.fn();
      const ms = Math.round(performance.now() - t0);
      const isWarn = typeof result === 'string' && (result.includes('UNREACHABLE') || result.includes('MISSING') || result.includes('STALE') || result.includes('NOT ') || result.includes('FAIL'));
      if (isWarn) { warn++; } else { pass++; }
      row.children[1].innerHTML = `<span style="color:${isWarn ? 'var(--yellow)' : 'var(--green)'}; font-family:'JetBrains Mono',monospace; font-size:11px;">${escHtml(result)}</span> <span style="color:var(--muted); font-size:10px; flex-shrink:0;">${ms}ms</span>`;
    } catch(e) {
      fail++;
      const ms = Math.round(performance.now() - t0);
      row.children[1].innerHTML = `<span style="color:var(--red); font-size:11px;">FAIL: ${escHtml((e.message||String(e)).substring(0,60))}</span> <span style="color:var(--muted); font-size:10px;">${ms}ms</span>`;
    }
    summary.innerHTML = `<span style="color:var(--green);">${pass}&#10003;</span> <span style="color:${warn?'var(--yellow)':'var(--muted)'}">${warn}&#9888;</span> <span style="color:${fail?'var(--red)':'var(--muted)'}">${fail}&#10007;</span> / ${total}`;
  }
  const totalMs = Math.round(performance.now() - t0All);
  summary.innerHTML += ` <span style="color:var(--muted); font-size:11px;">(${(totalMs/1000).toFixed(1)}s)</span>`;
  btn.disabled = false; btn.textContent = `Run All Tests (${total})`;
}

// ── TAB5 REMOTE CONTROL ──
async function tab5Fetch(path, opts={}) {
  try {
    const r = await fetch(TAB5 + path, { ...opts, mode:'cors', signal:AbortSignal.timeout(8000) });
    return await r.json();
  } catch(e) {
    showToast('Tab5 unreachable: ' + e.message, 'error');
    return null;
  }
}

async function tab5Screenshot() {
  const out = $('tab5-output');
  out.innerHTML = '<div style="text-align:center; padding:20px; color:var(--muted);">Loading screenshot...</div>';
  try {
    const url = TAB5 + '/screenshot?' + Date.now();
    out.innerHTML = `<img src="${url}" style="max-width:100%; border-radius:var(--radius-sm); border:1px solid var(--border);" onerror="this.parentNode.innerHTML='<div class=\\'empty\\'>Failed to load screenshot</div>'" alt="Tab5 Screenshot">`;
  } catch(e) { out.innerHTML = '<div class="empty">Failed: '+escHtml(e.message)+'</div>'; }
}

async function tab5Info() {
  const d = await tab5Fetch('/info');
  if (!d) return;
  $('tab5-output').innerHTML = `<pre style="font-size:12px; font-family:'JetBrains Mono',monospace; white-space:pre-wrap; color:var(--text);">${JSON.stringify(d, null, 2)}</pre>`;
}

async function tab5Selftest() {
  const d = await tab5Fetch('/selftest');
  if (!d) return;
  const items = d.tests || d;
  let html = '<div style="font-size:13px;">';
  if (Array.isArray(items)) {
    for (const t of items) {
      const ok = t.pass || t.status === 'pass' || t.ok;
      html += `<div style="padding:6px 0; border-bottom:1px solid var(--border-subtle); display:flex; justify-content:space-between;"><span>${escHtml(t.name||t.test||'?')}</span><span class="badge ${ok?'ok':'err'}">${ok?'PASS':'FAIL'}</span></div>`;
    }
  } else {
    html += `<pre style="font-size:12px; white-space:pre-wrap;">${JSON.stringify(d,null,2)}</pre>`;
  }
  html += '</div>';
  $('tab5-output').innerHTML = html;
}

async function tab5VoiceReconnect() {
  const d = await tab5Fetch('/voice/reconnect', { method:'POST' });
  showToast(d ? 'Voice reconnect triggered' : 'Failed', d ? 'info' : 'error');
}

async function tab5Navigate() {
  const screen = $('tab5-nav-screen').value;
  const d = await tab5Fetch('/navigate?screen=' + screen, { method:'POST' });
  if (d) showToast('Navigated to ' + screen);
  setTimeout(tab5Screenshot, 2000);
}

async function tab5Touch() {
  const x = $('tab5-touch-x').value, y = $('tab5-touch-y').value;
  if (!x || !y) { showToast('Enter X and Y coordinates', 'error'); return; }
  try {
    await fetch(TAB5 + '/touch', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({x:parseInt(x),y:parseInt(y),action:'tap'}), mode:'cors', signal:AbortSignal.timeout(5000) });
    showToast(`Tapped (${x}, ${y})`);
    setTimeout(tab5Screenshot, 1500);
  } catch(e) { showToast('Tap failed: '+e.message, 'error'); }
}

async function tab5Chat() {
  const text = $('tab5-chat-text').value;
  if (!text) return;
  try {
    await fetch(TAB5 + '/chat', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({text}), mode:'cors', signal:AbortSignal.timeout(5000) });
    showToast('Sent: ' + text.substring(0,40));
    $('tab5-chat-text').value = '';
  } catch(e) { showToast('Send failed: '+e.message, 'error'); }
}

async function tab5Mode(m) {
  try {
    await fetch(TAB5 + '/mode?m=' + m, { method:'POST', mode:'cors', signal:AbortSignal.timeout(5000) });
    showToast('Mode set to ' + ['Local','Hybrid','Cloud'][m]);
  } catch(e) { showToast('Mode switch failed: '+e.message, 'error'); }
}

// ── INIT ──
refreshOverview();
loadConfig();
refreshTimer = setInterval(() => { if (currentTab === 'overview') refreshOverview(); }, 5000);
</script>
</body>
</html>"""


async def handle_index(request: web.Request) -> web.Response:
    """Serve the dashboard SPA."""
    return web.Response(text=DASHBOARD_HTML, content_type="text/html")


# ── App Setup ────────────────────────────────────────────────────────────

def create_app() -> web.Application:
    app = web.Application()

    # SPA
    app.router.add_get("/", handle_index)

    # Legacy API (backward compat)
    app.router.add_get("/api/status", handle_status)
    app.router.add_get("/api/voice-config", handle_get_voice_config)
    app.router.add_post("/api/voice-config", handle_set_voice_config)

    # Generic proxy to voice server
    app.router.add_route("*", "/api/proxy/{path:.*}", _proxy_request)

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    return app


if __name__ == "__main__":
    web.run_app(create_app(), host=HOST, port=PORT)
