#!/usr/bin/env python3
"""
TinkerClaw Dashboard — Web UI for Dragon Server + Voice Pipeline

Serves on port 3500. Aggregates status from:
  - Dragon Server (port 3501) — CDP screencast bridge
  - Voice Server  (port 3502) — STT/TTS/LLM pipeline

Endpoints:
  GET  /              — Dashboard HTML (single-page app)
  GET  /api/status    — Aggregated health from both services
  GET  /api/voice-config   — Proxy to voice server GET /api/config
  POST /api/voice-config   — Proxy to voice server POST /api/config
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

# Shared client session (created on startup, closed on shutdown)
_client: aiohttp.ClientSession | None = None
_start_time = time.time()


# ── Lifecycle ────────────────────────────────────────────────────────────

async def on_startup(app: web.Application) -> None:
    global _client
    _client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))
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
        async with _client.post(
            f"{VOICE_SERVER}/api/config",
            json=body,
        ) as resp:
            data = await resp.json()
            return web.json_response(data, status=resp.status)
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=502)


# ── Dashboard HTML ───────────────────────────────────────────────────────

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TinkerClaw Dashboard</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: 'Courier New', Courier, monospace;
    background: #1a1a2e;
    color: #e0e0e0;
    min-height: 100vh;
    padding: 0;
  }

  header {
    background: #0f3460;
    padding: 16px 24px;
    border-bottom: 2px solid #ff6b35;
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  header h1 {
    color: #ff6b35;
    font-size: 1.4em;
    letter-spacing: 1px;
  }
  header .refresh-dot {
    width: 10px; height: 10px;
    border-radius: 50%;
    background: #53d769;
    display: inline-block;
    transition: opacity 0.3s;
  }
  header .refresh-dot.fetching { opacity: 0.3; }

  .container { max-width: 900px; margin: 0 auto; padding: 20px; }

  .status-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 16px;
    margin-bottom: 20px;
  }
  @media (max-width: 600px) {
    .status-grid { grid-template-columns: 1fr; }
  }

  .card {
    background: #16213e;
    border-radius: 8px;
    padding: 16px 20px;
    border: 1px solid #0f3460;
  }
  .card h2 {
    color: #ff6b35;
    font-size: 1em;
    margin-bottom: 12px;
    text-transform: uppercase;
    letter-spacing: 1px;
  }
  .card .row {
    display: flex;
    justify-content: space-between;
    padding: 4px 0;
    border-bottom: 1px solid #0f346033;
  }
  .card .row:last-child { border-bottom: none; }
  .card .label { color: #8899aa; }
  .card .val { color: #53d769; font-weight: bold; }
  .card .val.error { color: #ff3b30; }
  .card .val.warn { color: #ffcc00; }

  .status-badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 0.85em;
    font-weight: bold;
  }
  .status-badge.ok { background: #53d76933; color: #53d769; }
  .status-badge.err { background: #ff3b3033; color: #ff3b30; }

  .config-card {
    background: #16213e;
    border-radius: 8px;
    padding: 20px;
    border: 1px solid #0f3460;
    margin-bottom: 20px;
  }
  .config-card h2 {
    color: #ff6b35;
    font-size: 1em;
    margin-bottom: 16px;
    text-transform: uppercase;
    letter-spacing: 1px;
  }

  .form-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px;
  }
  @media (max-width: 600px) {
    .form-grid { grid-template-columns: 1fr; }
  }
  .form-group { display: flex; flex-direction: column; gap: 4px; }
  .form-group label { color: #8899aa; font-size: 0.85em; }
  .form-group select, .form-group input, .form-group textarea {
    background: #1a1a2e;
    color: #e0e0e0;
    border: 1px solid #0f3460;
    border-radius: 4px;
    padding: 8px 10px;
    font-family: inherit;
    font-size: 0.9em;
  }
  .form-group select:focus, .form-group input:focus, .form-group textarea:focus {
    outline: none;
    border-color: #ff6b35;
  }
  .form-group.full { grid-column: 1 / -1; }
  .form-group textarea { resize: vertical; min-height: 60px; }

  .btn-row { margin-top: 16px; display: flex; gap: 12px; align-items: center; }
  .btn {
    background: #ff6b35;
    color: #fff;
    border: none;
    border-radius: 4px;
    padding: 10px 24px;
    font-family: inherit;
    font-size: 0.95em;
    font-weight: bold;
    cursor: pointer;
    letter-spacing: 0.5px;
  }
  .btn:hover { background: #e55a28; }
  .btn:disabled { opacity: 0.5; cursor: default; }
  .feedback {
    font-size: 0.85em;
    padding: 4px 0;
    min-height: 1.2em;
  }
  .feedback.ok { color: #53d769; }
  .feedback.err { color: #ff3b30; }

  .devices-card {
    background: #16213e;
    border-radius: 8px;
    padding: 16px 20px;
    border: 1px solid #0f3460;
  }
  .devices-card h2 {
    color: #ff6b35;
    font-size: 1em;
    margin-bottom: 12px;
    text-transform: uppercase;
    letter-spacing: 1px;
  }
  .devices-card .empty { color: #555; font-style: italic; }

  footer {
    text-align: center;
    color: #333;
    padding: 20px;
    font-size: 0.8em;
  }
</style>
</head>
<body>

<header>
  <h1>TinkerClaw Dashboard</h1>
  <span class="refresh-dot" id="dot" title="Auto-refreshing every 5s"></span>
</header>

<div class="container">

  <!-- Status Cards -->
  <div class="status-grid">
    <div class="card" id="dragon-card">
      <h2>Dragon Server</h2>
      <div class="row"><span class="label">Status</span><span class="val" id="d-status">--</span></div>
      <div class="row"><span class="label">CDP</span><span class="val" id="d-cdp">--</span></div>
      <div class="row"><span class="label">FPS</span><span class="val" id="d-fps">--</span></div>
      <div class="row"><span class="label">Frames</span><span class="val" id="d-frames">--</span></div>
      <div class="row"><span class="label">Uptime</span><span class="val" id="d-uptime">--</span></div>
    </div>
    <div class="card" id="voice-card">
      <h2>Voice Pipeline</h2>
      <div class="row"><span class="label">Status</span><span class="val" id="v-status">--</span></div>
      <div class="row"><span class="label">STT</span><span class="val" id="v-stt">--</span></div>
      <div class="row"><span class="label">TTS</span><span class="val" id="v-tts">--</span></div>
      <div class="row"><span class="label">LLM</span><span class="val" id="v-llm">--</span></div>
      <div class="row"><span class="label">Sessions</span><span class="val" id="v-sessions">--</span></div>
      <div class="row"><span class="label">Uptime</span><span class="val" id="v-uptime">--</span></div>
    </div>
  </div>

  <!-- Pipeline Config -->
  <div class="config-card">
    <h2>Pipeline Config</h2>
    <div class="form-grid">
      <div class="form-group">
        <label for="cfg-stt">STT Backend</label>
        <select id="cfg-stt">
          <option value="moonshine">moonshine</option>
          <option value="whisper_cpp">whisper_cpp</option>
          <option value="vosk">vosk</option>
        </select>
      </div>
      <div class="form-group">
        <label for="cfg-stt-model">STT Model</label>
        <input id="cfg-stt-model" type="text" placeholder="e.g. tiny">
      </div>
      <div class="form-group">
        <label for="cfg-tts">TTS Backend</label>
        <select id="cfg-tts">
          <option value="piper">piper</option>
          <option value="kokoro">kokoro</option>
          <option value="edge_tts">edge_tts</option>
        </select>
      </div>
      <div class="form-group">
        <label for="cfg-tts-model">TTS Voice / Model</label>
        <input id="cfg-tts-model" type="text" placeholder="e.g. en_US-lessac-medium">
      </div>
      <div class="form-group">
        <label for="cfg-llm">LLM Backend</label>
        <select id="cfg-llm">
          <option value="ollama">ollama</option>
          <option value="openrouter">openrouter</option>
          <option value="lmstudio">lmstudio</option>
        </select>
      </div>
      <div class="form-group">
        <label for="cfg-llm-model">LLM Model</label>
        <input id="cfg-llm-model" type="text" placeholder="e.g. gemma3:4b">
      </div>
      <div class="form-group full">
        <label for="cfg-prompt">System Prompt</label>
        <textarea id="cfg-prompt" rows="3"></textarea>
      </div>
    </div>
    <div class="btn-row">
      <button class="btn" id="btn-apply" onclick="applyConfig()">Apply Changes</button>
      <span class="feedback" id="cfg-feedback"></span>
    </div>
  </div>

  <!-- Connected Devices -->
  <div class="devices-card">
    <h2>Connected Devices</h2>
    <div id="devices-list"><span class="empty">(none currently connected)</span></div>
  </div>

</div>

<footer>TinkerClaw Dashboard &mdash; port 3500</footer>

<script>
const $ = (id) => document.getElementById(id);

function fmtUptime(seconds) {
  if (typeof seconds !== 'number' || isNaN(seconds)) return '--';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  if (h > 0) return h + 'h ' + m + 'm';
  if (m > 0) return m + 'm ' + s + 's';
  return s + 's';
}

function setVal(id, text, cls) {
  const el = $(id);
  el.textContent = text;
  el.className = 'val' + (cls ? ' ' + cls : '');
}

function statusBadge(data) {
  if (!data || data.error) return ['Offline', 'error'];
  if (data.status === 'ok') return ['OK', ''];
  return [data.status || '??', 'warn'];
}

async function refreshStatus() {
  const dot = $('dot');
  dot.classList.add('fetching');
  try {
    const resp = await fetch('/api/status');
    const data = await resp.json();

    // Dragon
    const d = data.dragon;
    if (d && !d.error) {
      const [st, cls] = statusBadge(d);
      setVal('d-status', st, cls);
      setVal('d-cdp', d.cdp || '--', d.cdp === 'connected' ? '' : 'warn');
      setVal('d-fps', d.fps != null ? d.fps.toFixed(1) : '--');
      setVal('d-frames', d.frames != null ? d.frames.toLocaleString() : '--');
      setVal('d-uptime', fmtUptime(d.uptime));
    } else {
      setVal('d-status', 'Offline', 'error');
      setVal('d-cdp', '--', 'error');
      setVal('d-fps', '--', 'error');
      setVal('d-frames', '--', 'error');
      setVal('d-uptime', '--', 'error');
    }

    // Voice
    const v = data.voice;
    if (v && !v.error) {
      const [st, cls] = statusBadge(v);
      setVal('v-status', st, cls);
      setVal('v-stt', v.backends ? v.backends.stt : '--');
      setVal('v-tts', v.backends ? v.backends.tts : '--');
      setVal('v-llm', v.backends ? v.backends.llm : '--');
      setVal('v-sessions', v.active_sessions != null ? v.active_sessions : '--');
      setVal('v-uptime', fmtUptime(v.uptime_seconds));
    } else {
      setVal('v-status', 'Offline', 'error');
      setVal('v-stt', '--', 'error');
      setVal('v-tts', '--', 'error');
      setVal('v-llm', '--', 'error');
      setVal('v-sessions', '--', 'error');
      setVal('v-uptime', '--', 'error');
    }
  } catch (e) {
    setVal('d-status', 'Error', 'error');
    setVal('v-status', 'Error', 'error');
  }
  dot.classList.remove('fetching');
}

async function loadConfig() {
  try {
    const resp = await fetch('/api/voice-config');
    if (!resp.ok) return;
    const cfg = await resp.json();

    if (cfg.stt) {
      $('cfg-stt').value = cfg.stt.backend || 'moonshine';
      $('cfg-stt-model').value = cfg.stt.model || '';
    }
    if (cfg.tts) {
      $('cfg-tts').value = cfg.tts.backend || 'piper';
      // Show current voice/model depending on backend
      $('cfg-tts-model').value = cfg.tts.piper_model || cfg.tts.kokoro_voice || cfg.tts.edge_voice || '';
    }
    if (cfg.llm) {
      $('cfg-llm').value = cfg.llm.backend || 'ollama';
      // Show model for current backend
      const backend = cfg.llm.backend || 'ollama';
      if (backend === 'ollama') $('cfg-llm-model').value = cfg.llm.ollama_model || '';
      else if (backend === 'openrouter') $('cfg-llm-model').value = cfg.llm.openrouter_model || '';
      else if (backend === 'lmstudio') $('cfg-llm-model').value = cfg.llm.lmstudio_model || '';
      $('cfg-prompt').value = cfg.llm.system_prompt || '';
    }
  } catch (e) {
    console.error('Failed to load config:', e);
  }
}

async function applyConfig() {
  const btn = $('btn-apply');
  const fb = $('cfg-feedback');
  btn.disabled = true;
  fb.textContent = 'Applying...';
  fb.className = 'feedback';

  const sttBackend = $('cfg-stt').value;
  const ttsBackend = $('cfg-tts').value;
  const llmBackend = $('cfg-llm').value;

  // Build the config payload
  const payload = {
    stt: { backend: sttBackend, model: $('cfg-stt-model').value },
    tts: { backend: ttsBackend },
    llm: {
      backend: llmBackend,
      system_prompt: $('cfg-prompt').value,
    },
  };

  // Map TTS model to the correct config key
  const ttsModel = $('cfg-tts-model').value;
  if (ttsBackend === 'piper') payload.tts.piper_model = ttsModel;
  else if (ttsBackend === 'kokoro') payload.tts.kokoro_voice = ttsModel;
  else if (ttsBackend === 'edge_tts') payload.tts.edge_voice = ttsModel;

  // Map LLM model to the correct config key
  const llmModel = $('cfg-llm-model').value;
  if (llmBackend === 'ollama') payload.llm.ollama_model = llmModel;
  else if (llmBackend === 'openrouter') payload.llm.openrouter_model = llmModel;
  else if (llmBackend === 'lmstudio') payload.llm.lmstudio_model = llmModel;

  try {
    const resp = await fetch('/api/voice-config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (resp.ok) {
      fb.textContent = 'Config applied successfully.';
      fb.className = 'feedback ok';
      // Refresh status after a short delay
      setTimeout(refreshStatus, 1000);
    } else {
      fb.textContent = 'Error: ' + (data.error || resp.statusText);
      fb.className = 'feedback err';
    }
  } catch (e) {
    fb.textContent = 'Failed to reach server: ' + e.message;
    fb.className = 'feedback err';
  }
  btn.disabled = false;
}

// Init
refreshStatus();
loadConfig();
setInterval(refreshStatus, 5000);
</script>

</body>
</html>"""


async def handle_index(request: web.Request) -> web.Response:
    """Serve the dashboard SPA."""
    return web.Response(text=DASHBOARD_HTML, content_type="text/html")


# ── App Setup ────────────────────────────────────────────────────────────

def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/status", handle_status)
    app.router.add_get("/api/voice-config", handle_get_voice_config)
    app.router.add_post("/api/voice-config", handle_set_voice_config)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    return app


if __name__ == "__main__":
    web.run_app(create_app(), host=HOST, port=PORT)
