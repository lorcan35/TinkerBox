#!/usr/bin/env python3
"""
TinkerClaw Dashboard — Web UI for Dragon Server + Voice Pipeline

Serves on port 3500. 6-tab SPA with proxy routes to voice server (3502).

Tabs: Overview | Conversations | Chat | Devices | Notes | Logs

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

    # Forward JSON body for POST/PUT
    if method in ("POST", "PUT"):
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
<style>
:root {
  --bg: #0f0f1a;
  --surface: #16213e;
  --surface2: #1a1a2e;
  --border: #0f3460;
  --accent: #ff6b35;
  --accent2: #06b6d4;
  --green: #53d769;
  --red: #ff3b30;
  --yellow: #ffcc00;
  --text: #e0e0e0;
  --muted: #6b7b8d;
  --radius: 8px;
}
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; font-size: 14px; }

/* ── Layout ── */
.app { display: flex; flex-direction: column; height: 100vh; }
header {
  background: var(--surface); padding: 12px 20px; border-bottom: 2px solid var(--accent);
  display: flex; align-items: center; gap: 16px; flex-shrink: 0;
}
header h1 { color: var(--accent); font-size: 1.3em; letter-spacing: 0.5px; white-space: nowrap; }
header .status { margin-left: auto; display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--muted); }
.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--muted); transition: background 0.3s; }
.dot.ok { background: var(--green); }
.dot.err { background: var(--red); }

nav {
  background: var(--surface); border-bottom: 1px solid var(--border);
  display: flex; overflow-x: auto; flex-shrink: 0; padding: 0 12px;
}
nav button {
  background: none; border: none; color: var(--muted); padding: 10px 18px;
  font-size: 13px; font-weight: 600; cursor: pointer; white-space: nowrap;
  border-bottom: 2px solid transparent; transition: all 0.2s;
  font-family: inherit;
}
nav button:hover { color: var(--text); }
nav button.active { color: var(--accent); border-bottom-color: var(--accent); }

.tab-content { flex: 1; overflow-y: auto; padding: 20px; }
.tab-panel { display: none; max-width: 1200px; margin: 0 auto; }
.tab-panel.active { display: block; }

/* ── Cards ── */
.card {
  background: var(--surface); border-radius: var(--radius); padding: 16px 20px;
  border: 1px solid var(--border); margin-bottom: 16px;
}
.card h2 {
  color: var(--accent); font-size: 0.9em; margin-bottom: 12px;
  text-transform: uppercase; letter-spacing: 1px;
}
.card-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 16px; margin-bottom: 16px; }
.row { display: flex; justify-content: space-between; padding: 4px 0; border-bottom: 1px solid rgba(15,52,96,0.3); }
.row:last-child { border-bottom: none; }
.label { color: var(--muted); }
.val { color: var(--green); font-weight: 600; }
.val.error { color: var(--red); }
.val.warn { color: var(--yellow); }

/* ── Badges ── */
.badge {
  display: inline-block; padding: 2px 8px; border-radius: 4px;
  font-size: 11px; font-weight: 700; text-transform: uppercase;
}
.badge.ok, .badge.active { background: rgba(83,215,105,0.15); color: var(--green); }
.badge.err, .badge.ended { background: rgba(255,59,48,0.15); color: var(--red); }
.badge.warn, .badge.paused { background: rgba(255,204,0,0.15); color: var(--yellow); }
.badge.info { background: rgba(6,182,212,0.15); color: var(--accent2); }
.badge.user { background: rgba(255,107,53,0.15); color: var(--accent); }
.badge.assistant { background: rgba(6,182,212,0.15); color: var(--accent2); }
.badge.system { background: rgba(107,123,141,0.15); color: var(--muted); }
.badge.online { background: rgba(83,215,105,0.15); color: var(--green); }
.badge.offline { background: rgba(255,59,48,0.15); color: var(--red); }

/* ── Forms ── */
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.form-group { display: flex; flex-direction: column; gap: 4px; }
.form-group.full { grid-column: 1 / -1; }
.form-group label { color: var(--muted); font-size: 12px; font-weight: 600; }
input, select, textarea {
  background: var(--bg); color: var(--text); border: 1px solid var(--border);
  border-radius: 4px; padding: 8px 10px; font-family: inherit; font-size: 13px;
}
input:focus, select:focus, textarea:focus { outline: none; border-color: var(--accent); }
textarea { resize: vertical; min-height: 60px; }

.btn {
  background: var(--accent); color: #fff; border: none; border-radius: 4px;
  padding: 8px 20px; font-weight: 700; cursor: pointer; font-family: inherit; font-size: 13px;
  transition: opacity 0.15s;
}
.btn:hover:not(:disabled) { opacity: 0.85; }
.btn:disabled { opacity: 0.4; cursor: default; }
.btn.secondary { background: var(--border); }
.btn.danger { background: var(--red); }
.btn.small { padding: 4px 12px; font-size: 12px; }

.btn-row { display: flex; gap: 10px; align-items: center; margin-top: 12px; }
.feedback { font-size: 12px; min-height: 1.2em; }
.feedback.ok { color: var(--green); }
.feedback.err { color: var(--red); }

/* ── Tables ── */
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { text-align: left; color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; padding: 8px 12px; border-bottom: 1px solid var(--border); }
td { padding: 10px 12px; border-bottom: 1px solid rgba(15,52,96,0.3); vertical-align: top; }
tr:hover td { background: rgba(15,52,96,0.2); }
tr.clickable { cursor: pointer; }

/* ── Split layout (Conversations) ── */
.split { display: flex; gap: 16px; height: calc(100vh - 160px); }
.split-left { width: 360px; min-width: 300px; overflow-y: auto; flex-shrink: 0; }
.split-right { flex: 1; overflow-y: auto; display: flex; flex-direction: column; }
.session-item {
  padding: 12px 16px; border-bottom: 1px solid rgba(15,52,96,0.3); cursor: pointer; transition: background 0.15s;
}
.session-item:hover, .session-item.active { background: rgba(255,107,53,0.08); }
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
.msg.user .bubble { background: #1e3a2e; border: 1px solid #2d5a40; color: #d1fae5; }
.msg.assistant .bubble { background: var(--surface2); border: 1px solid var(--border); color: #e0e7ff; }
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

/* ── Responsive ── */
@keyframes fadeIn { from { opacity: 0; transform: translateY(-4px); } to { opacity: 1; transform: translateY(0); } }

@media (max-width: 768px) {
  .split { flex-direction: column; height: auto; }
  .split-left { width: 100%; max-height: 300px; }
  .form-grid { grid-template-columns: 1fr; }
  .card-grid { grid-template-columns: 1fr; }
}
</style>
</head>
<body>
<div class="app">

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
  <button data-tab="logs">Logs</button>
</nav>

<div class="tab-content">

<!-- ═══════════════ OVERVIEW TAB ═══════════════ -->
<div class="tab-panel active" id="tab-overview">
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
    <div id="ov-devices"><span class="empty">Loading...</span></div>
  </div>

  <!-- Recent sessions -->
  <div class="card">
    <h2>Recent Sessions</h2>
    <div class="table-wrap">
      <table>
        <thead><tr><th>ID</th><th>Device</th><th>Type</th><th>Status</th><th>Messages</th><th>Last Active</th></tr></thead>
        <tbody id="ov-sessions"><tr><td colspan="6" class="empty">Loading...</td></tr></tbody>
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
      <div id="conv-list"><div class="empty">Loading sessions...</div></div>
    </div>
    <div class="split-right card" style="padding:0;">
      <div id="conv-header" style="padding:12px 16px; border-bottom:1px solid var(--border); display:none;">
        <div style="display:flex; justify-content:space-between; align-items:center;">
          <div>
            <span style="font-weight:700;">Session</span>
            <span id="conv-sid" style="font-family:monospace; color:var(--muted); font-size:12px;"></span>
          </div>
          <div style="display:flex; gap:8px;">
            <span class="badge" id="conv-badge"></span>
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
  <div class="card-grid" id="devices-grid">
    <div class="empty">Loading devices...</div>
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
    <div class="empty">Loading notes...</div>
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
    <div id="event-list"><div class="empty">Loading events...</div></div>
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
  currentTab = tab;
  onTabSwitch(tab);
});

function onTabSwitch(tab) {
  if (tab === 'overview') { refreshOverview(); }
  if (tab === 'conversations') { loadConversations(); }
  if (tab === 'chat') { loadChatSessions(); }
  if (tab === 'devices') { loadDevices(); }
  if (tab === 'notes') { loadNotes(); }
  if (tab === 'logs') { loadEvents(); startEventPoll(); }
  if (tab !== 'logs') stopEventPoll();
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
    if (r.ok) { fb.textContent = 'Config applied!'; fb.className = 'feedback ok'; setTimeout(refreshOverview, 1000); }
    else { fb.textContent = 'Error: '+(data.error||r.statusText); fb.className = 'feedback err'; }
  } catch(e) { fb.textContent = 'Failed: '+e.message; fb.className = 'feedback err'; }
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
  } catch(e) {}

  // Load messages
  try {
    const data = await api(P + '/api/v1/sessions/' + sid + '/messages?limit=500');
    const container = $('conv-messages');
    if (!data.items?.length) {
      container.innerHTML = '<div class="empty">No messages in this session</div>';
      return;
    }
    container.innerHTML = data.items.map(m => `
      <div class="msg ${m.role}">
        <div class="bubble">${escHtml(m.content)}</div>
        <div class="msg-meta">
          <span class="badge ${m.role}">${m.role}</span>
          ${m.input_mode ? '<span style="color:var(--muted)">via '+m.input_mode+'</span>' : ''}
          ${fmtTimeShort(m.created_at)}
          ${m.model ? '<span style="color:var(--muted)">'+m.model+'</span>' : ''}
        </div>
      </div>
    `).join('');
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
    loadConversations();
    selectSession(sess.id);
  } catch(e) { alert('Failed to create session: ' + e.message); }
}

async function endSelectedSession() {
  if (!selectedSessionId) return;
  if (!confirm('End this session?')) return;
  try {
    await api(P + '/api/v1/sessions/' + selectedSessionId + '/end', { method:'POST' });
    loadConversations();
    selectSession(selectedSessionId);
  } catch(e) { alert('Failed: ' + e.message); }
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
    } catch(e) { alert('Failed to create session'); return; }
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
      container.innerHTML = data.items.map(m => `
        <div class="msg ${m.role}">
          <div class="bubble">${escHtml(m.content)}</div>
        </div>
      `).join('');
      container.scrollTop = container.scrollHeight;
    } else {
      container.innerHTML = '<div class="empty" id="chat-empty">Session ready. Send a message!</div>';
    }
  } catch(e) {}
}

async function chatSend() {
  if (chatBusy || !chatSessionId) return;
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
    const r = await fetch(P + '/api/v1/sessions/' + chatSessionId + '/chat', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ text }),
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
    $('chat-status').textContent = 'ready';
    $('chat-input').focus();
    container.scrollTop = container.scrollHeight;
  }
}

// Chat textarea auto-resize + enter to send
$('chat-input').addEventListener('input', function() { this.style.height = ''; this.style.height = Math.min(this.scrollHeight, 120)+'px'; });
$('chat-input').addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); chatSend(); } });

// ── DEVICES ──
async function loadDevices() {
  try {
    const data = await api(P + '/api/v1/devices');
    const grid = $('devices-grid');
    if (!data.items?.length) {
      grid.innerHTML = '<div class="empty">No devices registered</div>';
      return;
    }
    grid.innerHTML = data.items.map(d => {
      let caps = {};
      try { caps = typeof d.capabilities === 'string' ? JSON.parse(d.capabilities) : (d.capabilities || {}); } catch(e) {}
      return `
        <div class="card device-card" onclick="toggleDeviceDetails(this)">
          <div style="display:flex; justify-content:space-between; align-items:center;">
            <h2 style="margin:0;">${d.name || 'Unknown Device'}</h2>
            <span class="badge ${d.is_online ? 'online' : 'offline'}">${d.is_online ? 'Online' : 'Offline'}</span>
          </div>
          <div style="margin-top:8px;">
            <div class="row"><span class="label">ID</span><span style="font-family:monospace; font-size:12px;">${truncId(d.id)}</span></div>
            <div class="row"><span class="label">Hardware</span><span>${d.hardware_id || '--'}</span></div>
            <div class="row"><span class="label">Platform</span><span>${d.platform || '--'}</span></div>
            <div class="row"><span class="label">Firmware</span><span>${d.firmware_ver || '--'}</span></div>
            <div class="row"><span class="label">Last Seen</span><span>${fmtTime(d.last_seen_at)}</span></div>
          </div>
          <div class="device-details">
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

// ── NOTES ──
async function loadNotes() {
  try {
    const data = await api(P + '/api/notes?limit=50');
    const grid = $('notes-grid');
    const notes = data.notes || [];
    if (!notes.length) {
      grid.innerHTML = '<div class="empty">No notes yet. Create one!</div>';
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
  }
}

function openNewNoteForm() { $('new-note-form').style.display = 'block'; }
function closeNewNoteForm() { $('new-note-form').style.display = 'none'; $('note-title').value = ''; $('note-text').value = ''; }

async function createNote() {
  const title = $('note-title').value.trim();
  const text = $('note-text').value.trim();
  if (!text) { alert('Content is required'); return; }
  try {
    await api(P + '/api/notes', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ title, text }),
    });
    closeNewNoteForm();
    loadNotes();
  } catch(e) { alert('Failed to create note: ' + e.message); }
}

async function deleteNote(id) {
  if (!confirm('Delete this note?')) return;
  try {
    await api(P + '/api/notes/' + id, { method:'DELETE' });
    loadNotes();
  } catch(e) { alert('Failed to delete: ' + e.message); }
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
  } catch(e) { alert('Search failed: ' + e.message); }
}

$('notes-search').addEventListener('keydown', e => { if (e.key === 'Enter') searchNotes(); });

// ── LOGS ──
async function loadEvents() {
  const type = $('log-type-filter').value;
  const qs = `?limit=100&since_id=0${type ? '&type='+type : ''}`;
  try {
    const data = await api(P + '/api/v1/events' + qs);
    const container = $('event-list');
    const events = data.items || [];
    if (!events.length) {
      container.innerHTML = '<div class="empty">No events recorded</div>';
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
