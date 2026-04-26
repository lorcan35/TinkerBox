"""WebSocket server for Dragon Voice.

Serves the voice pipeline over WebSocket and provides HTTP endpoints
for health checks, status, configuration, and the REST API.

Integrates: Database, SessionManager, MessageStore, ConversationEngine, API routes.

refs #16, #17, #18
"""

import asyncio
import contextlib
import copy
import json
import logging
import os
import time
from typing import AsyncIterator, Optional

import aiohttp
from aiohttp import web, WSMsgType

from dragon_voice.media.store import MediaStore
from dragon_voice.media.pipeline import MediaPipeline
from dragon_voice.config import (
    VoiceConfig,
    SYSTEM_PROMPT_LOCAL, SYSTEM_PROMPT_HYBRID, SYSTEM_PROMPT_CLOUD,
    MAX_TOKENS_LOCAL, MAX_TOKENS_HYBRID, MAX_TOKENS_CLOUD,
)
from dragon_voice.conversation import ConversationEngine
from dragon_voice.db import Database
from dragon_voice.errors import DragonError, Scope, Severity, error_event
from dragon_voice.progress import Phase, Stage
from dragon_voice.progress_emit import emit_progress_pair
from dragon_voice.messages import MessageStore
from dragon_voice.handlers import (
    config_api as _handlers_config_api,
    debug as _handlers_debug,
    status as _handlers_status,
)
from dragon_voice.lifecycle import (
    monitors as _lc_monitors,
    purge as _lc_purge,
    shutdown as _lc_shutdown,
    startup as _lc_startup,
)
from dragon_voice.middleware import (
    auth as _mw_auth,
    cors as _mw_cors,
    rate_limit as _mw_rate_limit,
    security_headers as _mw_security_headers,
)
from dragon_voice.pipeline import VoicePipeline
from dragon_voice.sessions import SessionManager
from dragon_voice.audio import resample_pcm16_async
from dragon_voice.tools.response_wrap import looks_like_useful_text, synthesize_wrap


logger = logging.getLogger(__name__)


class VoiceServer:
    """Aiohttp-based WebSocket server for the Dragon Voice pipeline.

    Manages device registration, session lifecycle, conversation persistence,
    and the voice pipeline (STT -> LLM -> TTS).
    """

    def __init__(self, config: VoiceConfig) -> None:
        self._config = config
        self._app: Optional[web.Application] = None
        self._start_time = time.time()

        # Legacy counters (kept for backward compat on status page)
        self._session_count = 0

        # Active WebSocket sessions: ws_id -> {pipeline, session_id, device_id}
        self._active_connections: dict[str, dict] = {}
        self._max_connections = 10
        self._purge_task: Optional[asyncio.Task] = None
        self._memory_monitor_task: Optional[asyncio.Task] = None

        # Memory thresholds (MB) — Dragon has 8GB total, Ollama ~1.5GB, TinkerClaw ~300MB
        self._mem_warn_mb = 2048   # Force GC above this
        self._mem_crit_mb = 3072   # Restart pipeline above this (after GC)

        # Backend names for status page
        self._stt_name = config.stt.backend
        self._tts_name = config.tts.backend
        self._llm_name = config.llm.backend

        # Shared aiohttp client session for dashboard proxy (DQ08: avoids per-request FD churn)
        self._proxy_session: Optional[aiohttp.ClientSession] = None

        # Foundation modules (initialized in on_startup)
        self._db: Optional[Database] = None
        self._session_mgr: Optional[SessionManager] = None
        self._message_store: Optional[MessageStore] = None
        self._conversation: Optional[ConversationEngine] = None
        self._notes_svc = None

        # Media handling (rich media detection + user image uploads).
        # Wave 14 W14-H04: construct the URL signer from server.api_token
        # so /api/media/{id} URLs are HMAC-signed + time-bounded.  Falls
        # back to unsigned URLs when api_token is blank (dev bootstrap).
        from dragon_voice.media.url_signer import MediaUrlSigner
        self._media_url_signer = MediaUrlSigner(
            secret=getattr(self._config.server, "api_token", "") or ""
        )
        self._media_store = MediaStore()
        self._media_pipeline = MediaPipeline(
            self._media_store, url_signer=self._media_url_signer
        )
        self._media_cleanup_task: Optional[asyncio.Task] = None

        # Wave 15 W15-C01: shared backend pool so STT/TTS/LLM instances
        # persist across Tab5 WS reconnects.  Key = stable signature
        # tuple (see pipeline._stt_sig / _tts_sig / _llm_sig).  Every
        # VoicePipeline takes this dict and borrows/returns backends.
        # Backends are only shut down on server shutdown; per-pipeline
        # shutdown is a no-op for pooled instances.
        self._backend_pool: dict = {}

        # Wave 15 W15-H01 + W15-H06: rate-limit bucket store.  Keyed on
        # (client_ip, method, path) tuple; value is
        # {"window_start": float, "count": int}.  See
        # `_rate_limit_middleware` for semantics.
        self._rate_buckets: dict[tuple, dict] = {}

    def create_app(self) -> web.Application:
        """Create and configure the aiohttp application."""
        app = web.Application(
            client_max_size=32 * 1024 * 1024,  # 32MB for audio uploads
            # Wave 14 W14-M06: _security_headers_middleware is OUTERMOST
            # (first in the list) so every response — including the 401
            # from _auth_middleware before handler even runs — gets the
            # defensive headers stamped on its way out.  CORS runs next
            # and can't override because the security middleware uses
            # `if k not in response.headers` semantics.
            middlewares=[
                self._security_headers_middleware,
                self._cors_middleware,
                self._auth_middleware,
                # Wave 15 W15-H01 + W15-H06: rate limiter runs AFTER auth
                # so only authenticated clients count against the budget
                # — unauthenticated hits are already rejected cheaply.
                self._rate_limit_middleware,
            ],
        )

        # HTTP routes (legacy)
        app.router.add_get("/", self._handle_status)
        app.router.add_get("/health", self._handle_health)
        app.router.add_post("/debug/widget_chart", self._debug_widget_chart)
        app.router.add_post("/debug/widget_prompt", self._debug_widget_prompt)
        app.router.add_post("/debug/widget_card", self._debug_widget_card)
        app.router.add_post("/debug/widget_media", self._debug_widget_media)
        app.router.add_get("/api/config", self._handle_get_config)
        app.router.add_post("/api/config", self._handle_set_config)

        # Wave 15 W15-C01: tracemalloc-backed mem-diff probe.  Returns
        # top-N allocation growers since baseline so we can pinpoint
        # the RSS leak.  Bearer-gated via the auth middleware.
        app.router.add_get("/debug/mem", self._handle_debug_mem)

        # Dashboard proxy — forwards /dashboard* to localhost:3500
        app.router.add_route("*", "/dashboard{path:.*}", self._proxy_dashboard)

        # WebSocket route
        app.router.add_get("/ws/voice", self._handle_ws_voice)

        # Lifecycle hooks
        app.on_startup.append(self._on_startup)
        app.on_shutdown.append(self._on_shutdown)

        self._app = app
        return app

    # Middleware — thin adapters over the stateless handlers in
    # ``dragon_voice/middleware/``.  Each adapter binds instance state
    # (config, rate-limit buckets) and forwards to the shared handler.
    # See dragon_voice/middleware/__init__.py for the migration plan.

    @web.middleware
    async def _cors_middleware(self, request: web.Request, handler):
        return await _mw_cors.handle_cors(request, handler)

    @web.middleware
    async def _security_headers_middleware(self, request: web.Request, handler):
        return await _mw_security_headers.handle_security_headers(request, handler)

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler):
        expected = (getattr(self._config.server, "api_token", "") or "").strip()
        return await _mw_auth.handle_auth(request, handler, expected_token=expected)

    @web.middleware
    async def _rate_limit_middleware(self, request: web.Request, handler):
        return await _mw_rate_limit.handle_rate_limit(
            request, handler, buckets=self._rate_buckets
        )

    # --------------------------------------------------------------- Dashboard proxy

    async def _proxy_dashboard(self, request: web.Request) -> web.StreamResponse:
        """Reverse proxy /dashboard* to the dashboard on localhost:3500.

        Rewrites paths: /dashboard/foo → /foo on port 3500.
        This lets the dashboard be accessed via the ngrok tunnel at
        https://tinkerbox.ngrok.dev/dashboard without a separate tunnel.
        """
        path = request.match_info.get("path", "")
        target = f"http://127.0.0.1:3500{path}"
        if request.query_string:
            target += f"?{request.query_string}"

        try:
            session = self._proxy_session
            if session is None or session.closed:
                session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
                self._proxy_session = session

            method = request.method
            headers = {k: v for k, v in request.headers.items()
                       if k.lower() not in ("host", "content-length", "transfer-encoding")}
            body = await request.read() if request.can_read_body else None

            async with session.request(method, target, headers=headers, data=body) as resp:
                response = web.StreamResponse(
                    status=resp.status,
                    headers={k: v for k, v in resp.headers.items()
                             if k.lower() not in ("transfer-encoding", "content-encoding")},
                )
                response.content_type = resp.content_type
                await response.prepare(request)
                async for chunk in resp.content.iter_any():
                    await response.write(chunk)
                await response.write_eof()
                return response
        except Exception as e:
            logger.warning("Dashboard proxy failed: %s", e)
            return web.json_response(
                {"error": f"Dashboard not reachable: {e}"},
                status=502,
            )

    # --------------------------------------------------------------- Lifecycle
    #
    # Thin adapters over dragon_voice/lifecycle/.  The orchestration
    # logic (~470 LOC) moved to startup.py, shutdown.py, monitors.py,
    # purge.py so server.py stays focused on request routing + the
    # class definition.  These methods preserve the aiohttp on_startup
    # / on_shutdown signatures expected by ``create_app``.

    async def _on_startup(self, app: web.Application) -> None:
        await _lc_startup.run_startup(self, app)

    async def _periodic_purge(self, days: int) -> None:
        await _lc_purge.periodic_purge_loop(self, days)

    async def _media_cleanup_loop(self):
        await _lc_purge.media_cleanup_loop(self)

    @staticmethod
    def _get_rss_mb() -> float:
        return _lc_monitors.get_rss_mb()

    @staticmethod
    def _get_cpu_temp() -> float:
        return _lc_monitors.get_cpu_temp()

    async def _memory_monitor(self) -> None:
        await _lc_monitors.memory_monitor_loop(self)

    async def _on_shutdown(self, app: web.Application) -> None:
        await _lc_shutdown.run_shutdown(self, app)

    # ------------------------------------------------------------------ HTTP

    # Status + health — thin adapters over dragon_voice/handlers/status.py.

    async def _handle_status(self, request: web.Request) -> web.Response:
        return await _handlers_status.handle_status(request, server=self)

    async def _handle_health(self, request: web.Request) -> web.Response:
        return await _handlers_status.handle_health(request, server=self)

    # Debug / diagnostic handlers — thin adapters over the functions in
    # ``dragon_voice/handlers/debug.py``.  The URL routing + bearer-auth
    # middleware wiring still lives in ``create_app``; only the handler
    # bodies moved.

    async def _handle_debug_mem(self, request: web.Request) -> web.Response:
        return await _handlers_debug.handle_debug_mem(request, server=self)

    async def _debug_widget_chart(self, request: web.Request) -> web.Response:
        return await _handlers_debug.handle_debug_widget_chart(
            request, surface_mgr=self._surface_mgr,
        )

    async def _debug_widget_prompt(self, request: web.Request) -> web.Response:
        return await _handlers_debug.handle_debug_widget_prompt(
            request, surface_mgr=self._surface_mgr,
        )

    async def _debug_widget_card(self, request: web.Request) -> web.Response:
        return await _handlers_debug.handle_debug_widget_card(
            request, surface_mgr=self._surface_mgr,
        )

    async def _debug_widget_media(self, request: web.Request) -> web.Response:
        return await _handlers_debug.handle_debug_widget_media(
            request, surface_mgr=self._surface_mgr,
        )




    # Config get/set — thin adapters over dragon_voice/handlers/config_api.py.

    async def _handle_get_config(self, request: web.Request) -> web.Response:
        return await _handlers_config_api.handle_get_config(request, server=self)

    async def _handle_set_config(self, request: web.Request) -> web.Response:
        return await _handlers_config_api.handle_set_config(request, server=self)

    # --------------------------------------------------------------- WebSocket

    @staticmethod
    async def _safe_send_json(ws: web.WebSocketResponse, msg: dict) -> bool:
        """Non-raising ws.send_json — returns True on success.

        aiohttp raises ConnectionResetError (and bare Exception in some
        paths) when the transport is mid-close. Handlers that call
        send_json without guarding see the whole register/event flow
        fail and the session gets paused even though the WS is simply
        about to reconnect (refs #31). Use this helper everywhere.

        Returns False silently if the WS is closed or the send fails;
        callers should treat that as "client will reconnect and we'll
        replay on the next session_start" — not a fatal error.
        """
        if ws.closed:
            return False
        try:
            await ws.send_json(msg)
            return True
        except (ConnectionResetError, RuntimeError, asyncio.CancelledError):
            # aiohttp uses RuntimeError("closing transport") on some paths
            return False
        except Exception as e:
            logger.debug("_safe_send_json suppressed: %s", e)
            return False

    @staticmethod
    async def _safe_send_bytes(ws: web.WebSocketResponse, data: bytes) -> bool:
        """Non-raising ws.send_bytes — returns True on success. Same rationale as _safe_send_json."""
        if ws.closed:
            return False
        try:
            await ws.send_bytes(data)
            return True
        except (ConnectionResetError, RuntimeError, asyncio.CancelledError):
            return False
        except Exception as e:
            logger.debug("_safe_send_bytes suppressed: %s", e)
            return False

    @staticmethod
    @contextlib.asynccontextmanager
    async def _ws_keepalive_during_inference(
        ws: web.WebSocketResponse,
        *,
        interval_s: float = 5.0,
        ping_timeout_s: float = 5.0,
        label: str = "inference",
    ) -> AsyncIterator[None]:
        """Fire WS-level PING frames every `interval_s` seconds while the
        context is active.  Keeps Tab5's WS library from tripping its
        PONG-watch timeout (and triggering a reconnect → P13 eviction)
        when an LLM stream takes longer than Tab5's own tolerance window.

        The outer `web.WebSocketResponse(heartbeat=...)` timer is sized
        for idle sockets (60 s between pings).  That cadence is too slow
        for a 90 s Ollama turn on a local 4 B model — the event loop
        gets starved, the heartbeat task doesn't tick, Tab5 declares
        Dragon dead, Tab5 reconnects, and the P13 "Device already has
        connection" guard evicts the original WS mid-stream.  This
        helper pings 12× more often, only during inference, and stops
        the moment the caller exits the `async with`.

        Usage::

            async with self._ws_keepalive_during_inference(ws):
                async for token in llm.generate_stream_with_messages(...):
                    await self._safe_send_json(ws, {"type": "llm", "text": token})

        Parameters
        ----------
        ws : WebSocketResponse
            The live WS to ping.  If already closed, the helper is a no-op.
        interval_s : float, default 5.0
            Seconds between PING frames.  5 s is safely under every
            Tab5 firmware revision's PONG-watch window (30–45 s).
        ping_timeout_s : float, default 5.0
            How long to wait for the individual `ws.ping()` call to
            return before treating it as a failed ping.  Short so GIL
            contention from inference doesn't pile up.
        label : str, default "inference"
            Free-form tag used in the exit log so multiple concurrent
            helpers are distinguishable.

        Notes
        -----
        * Replaces and generalises the ad-hoc `_keepalive()` pattern
          that was previously inlined in `_handle_text` (TC path only).
        * Safe to use on a WS that has no in-flight work; the helper
          runs its own task that simply exits on context exit.
        * Exceptions from `ws.ping()` (connection reset, cancelled)
          are swallowed — the surrounding inference loop's own
          `ws.closed` check is the authoritative closure signal.
        """
        if ws.closed:
            yield
            return

        alive = True

        async def _tick() -> None:
            while alive:
                try:
                    await asyncio.sleep(interval_s)
                except asyncio.CancelledError:
                    break
                if not alive or ws.closed:
                    break
                try:
                    await asyncio.wait_for(ws.ping(), timeout=ping_timeout_s)
                except asyncio.TimeoutError:
                    logger.debug(
                        "ws_keepalive(%s) ping timeout after %.1fs — event loop stalled?",
                        label, ping_timeout_s,
                    )
                    # Keep trying; caller's loop is the real arbiter of
                    # whether the stream is still worth waiting for.
                    continue
                except (ConnectionResetError, RuntimeError):
                    break
                except Exception as e:
                    logger.debug("ws_keepalive(%s) suppressed: %s", label, e)
                    break

        task = asyncio.create_task(_tick())
        try:
            yield
        finally:
            alive = False
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _handle_ws_voice(self, request: web.Request) -> web.WebSocketResponse:
        """Main voice WebSocket endpoint.

        Protocol (see docs/protocol.md):
          Tab5 -> Dragon:
            - JSON: register, start, stop, cancel, text, record_start, record_stop
            - Binary: raw PCM int16 16kHz mono audio

          Dragon -> Tab5:
            - JSON: session_start, stt, llm, tts_start, tts_end, note_created,
                    config_update, error, event
            - Binary: PCM int16 audio at config.tts_sample_rate
        """
        # Wave 14 W14-C04: authenticate the WS upgrade request.  Prior to
        # this, /ws/voice was publicly reachable and the `register` frame
        # only required a non-empty device_id — so any attacker with the
        # ngrok URL could impersonate any Tab5, hijack sessions, and burn
        # OpenRouter/TinkerClaw budget.
        #
        # Contract:
        #   - If server.api_token is configured: client MUST present
        #       `Authorization: Bearer <token>` on the upgrade request,
        #       matched with hmac.compare_digest.  Mismatch → 401.
        #   - If server.api_token is blank (unprovisioned/dev): allow the
        #       handshake but log-warn so the operator knows they're
        #       running unauthenticated.  This matches the pattern of
        #       letting first-run bootstraps work without breaking Tab5
        #       flashes mid-upgrade.
        expected_token = (getattr(self._config.server, "api_token", "") or "").strip()
        if expected_token:
            auth_header = request.headers.get("Authorization", "")
            supplied = auth_header[7:].strip() if auth_header.startswith("Bearer ") else ""
            import hmac as _hmac
            if not supplied or not _hmac.compare_digest(supplied, expected_token):
                logger.warning(
                    "WS /ws/voice: rejecting unauthenticated upgrade from %s (header_present=%s)",
                    request.remote, bool(auth_header))
                # γ3-Dragon (issue #111): JSON body with `code` so future
                # ops tooling / dashboard introspection can distinguish
                # auth failure from other 401 sources without parsing
                # prose.  Tab5 (γ3-Tab5 follow-up) still uses the raw
                # status code for its stop-retry decision since
                # esp_websocket_client doesn't expose the body cleanly.
                return web.json_response(
                    {
                        "code": "auth_failed",
                        "message": "Invalid Dragon token — check Settings.",
                    },
                    status=401,
                )
        else:
            logger.warning(
                "WS /ws/voice: server.api_token not configured — allowing "
                "unauthenticated WS. Set DRAGON_API_TOKEN to enforce.")

        # Reject if at connection limit
        if len(self._active_connections) >= self._max_connections:
            logger.warning("Connection limit reached (%d), rejecting", self._max_connections)
            # γ3-Dragon (issue #111): same JSON-body treatment as the
            # 401 path above — see comment there for rationale.
            return web.json_response(
                {
                    "code": "server_full",
                    "message": "Dragon is at capacity — try again in a moment.",
                },
                status=503,
            )

        # v4·D connectivity audit -- ROOT CAUSE FIX #3.
        #
        # Enable aiohttp's built-in WS heartbeat at 30 s with a 60 s
        # pong-wait window.  Previously heartbeat=None meant the
        # server never sent WS-level pings -- dead sockets could only
        # be detected by a failed send.  With heartbeat enabled, aiohttp
        # emits a PING every `heartbeat` seconds and closes the
        # connection if the peer hasn't replied within `receive_timeout`.
        # Paired with Tab5's new TCP-level keepalive, both sides now
        # notice a half-open socket in well under 60 s instead of
        # waiting for the app layer to try a write and fail.
        ws = web.WebSocketResponse(
            max_msg_size=10 * 1024 * 1024,
            heartbeat=60.0,
            # 2026-04-23 (#58): 120 → 600 s.  Previously Tab5 WS got dropped
            # mid-turn when TC was running a long agent task — heartbeat
            # PING goes out every 60 s but if the TC response hasn't started
            # streaming within 120 s (normal for MiniMax-M2.5 + tools), the
            # aiohttp server-side receive_timeout would yank the connection
            # and Tab5 would see the flap as "Dragon unreachable".
            receive_timeout=600.0,
            autoping=True,
        )
        await ws.prepare(request)

        # Per-connection config: deep copy so mutations (voice_mode switch,
        # model changes) don't bleed between connections.  self._config
        # remains the immutable server default for new connections.
        conn_config = copy.deepcopy(self._config)

        # Per-connection processing lock (US-P10): serializes voice and text
        # requests so they don't corrupt shared state (conversation context,
        # LLM backend, TTS output path).  One lock per connection — different
        # devices are independent.
        conn_lock = asyncio.Lock()

        ws_id = f"ws{self._session_count}"
        self._session_count += 1
        peer = request.remote or "unknown"
        logger.info("WebSocket connected: %s (ws_id=%s)", peer, ws_id)

        # Server-side keepalive: ping every 15s to prevent ngrok idle timeout.
        # ngrok drops WS connections after ~30s of silence. Detects dead
        # connections via send-failure counter + response timeout (US-DQ20).
        _keepalive_running = True
        # Shared flag: last time ANY message was received from the client.
        # Updated in the main message loop; read by the keepalive task.
        _last_client_msg_time = time.monotonic()

        async def _ws_keepalive():
            nonlocal _last_client_msg_time
            fail_count = 0
            while _keepalive_running and not ws.closed:
                await asyncio.sleep(15)  # 15s < ngrok's ~30s idle threshold
                if ws.closed or not _keepalive_running:
                    break
                # Send JSON pong (data frame) — ngrok counts data frames as activity.
                # 5s timeout prevents sends from blocking indefinitely when the
                # event loop is delayed by GIL contention from inference (US-DQ05).
                try:
                    await asyncio.wait_for(
                        ws.send_json({"type": "pong"}), timeout=5.0
                    )
                    fail_count = 0
                except asyncio.TimeoutError:
                    fail_count += 1
                    logger.warning(
                        "Keepalive send timed out for %s (%d/3) — event loop may be blocked",
                        ws_id, fail_count,
                    )
                    if fail_count >= 3:
                        logger.warning("Keepalive: 3 consecutive timeouts, closing WS %s", ws_id)
                        try:
                            await ws.close()
                        except Exception:
                            pass
                        break
                    continue
                except Exception:
                    fail_count += 1
                    logger.warning("Keepalive send failed for %s (%d/3)", ws_id, fail_count)
                    if fail_count >= 3:
                        logger.warning("Keepalive: 3 consecutive send failures, closing WS %s", ws_id)
                        try:
                            await ws.close()
                        except Exception:
                            pass
                        break
                    continue

                # NOTE: The old 30s "no client message" silence check was removed.
                # Tab5 migrated to esp_websocket_client (voice.c commit 3af34b0) which
                # uses WS-level PING/PONG control frames at 15s interval. Control
                # frames do NOT update _last_client_msg_time (aiohttp handles them
                # internally and they never surface to the message loop), so the
                # silence check produced a 30-45s false-positive close every cycle.
                # Liveness is now detected by: (1) WS-level ping/pong timeout on
                # Tab5 side (45s), (2) this task's send-failure counter above
                # (3 consecutive send failures), and (3) TCP RST propagation.
                # _last_client_msg_time is left as-is for potential future use.

        _keepalive_task = asyncio.create_task(_ws_keepalive())

        # Connection state — populated after register
        conn_state: dict = {
            "ws_id": ws_id,
            "pipeline": None,
            "session_id": None,
            "device_id": None,
            "registered": False,
            "mode": "ask",  # "ask" or "dictate"
            "config": conn_config,  # per-connection config (deep copy of server default)
            "conn_lock": conn_lock,  # A06: stored so HTTP config handler can serialize
            "_on_audio": None,   # stored for pipeline re-init (A04)
            "_on_event": None,
            # Wave 14 W14-C06: per-connection background tasks (e.g.
            # cap_downgrade speak_system, out-of-band TTS) tracked here so
            # _handle_disconnect can cancel them before closing the pipeline.
            "bg_tasks": set(),
        }
        self._active_connections[ws_id] = conn_state

        # Callbacks for the pipeline.  v4·D audit P0 fix: route through
        # the shared _safe_send_* helpers so transient disconnects mid-
        # stream don't raise ConnectionResetError up into the pipeline's
        # tight loop (which used to swallow real exceptions).
        async def on_audio(audio_bytes: bytes) -> None:
            if ws.closed:
                return
            await self._safe_send_bytes(ws, audio_bytes)

        async def on_event(event: dict) -> None:
            if not ws.closed:
                await self._safe_send_json(ws, event)
            # Persist API usage events for cost tracking
            if event.get("type") == "api_usage" and self._db:
                try:
                    await self._db.add_event(
                        "api_usage",
                        session_id=conn_state.get("session_id"),
                        device_id=conn_state.get("device_id"),
                        data={k: v for k, v in event.items() if k != "type"},
                    )
                except Exception as e:
                    logger.debug("Callback error: %s", e)

        # Store callback refs for pipeline re-init (A04 memory monitor)
        conn_state["_on_audio"] = on_audio
        conn_state["_on_event"] = on_event

        try:
            async for msg in ws:
                _last_client_msg_time = time.monotonic()

                if msg.type == WSMsgType.BINARY:
                    # Raw PCM audio data — forward to pipeline
                    # Note: feed_audio is NOT locked (US-P10) — it only
                    # appends to the audio buffer and the VAD check is
                    # lightweight. The heavy processing (_process_utterance)
                    # is triggered via asyncio.create_task inside feed_audio
                    # and that task is serialized by the pipeline's own
                    # _processing flag. Locking here would block audio
                    # ingestion during LLM/TTS processing.
                    pipeline = conn_state.get("pipeline")
                    if pipeline:
                        await pipeline.feed_audio(msg.data)

                elif msg.type == WSMsgType.TEXT:
                    try:
                        cmd = json.loads(msg.data)
                    except json.JSONDecodeError:
                        logger.warning("Invalid JSON from %s: %s", ws_id, msg.data[:100])
                        continue

                    cmd_type = cmd.get("type", "")

                    if cmd_type == "register":
                        await self._handle_register(ws, conn_state, cmd, on_audio, on_event, conn_config)

                    elif cmd_type == "start":
                        pipeline = conn_state.get("pipeline")
                        if pipeline:
                            mode = cmd.get("mode", "ask")
                            conn_state["mode"] = mode
                            pipeline._audio_buffer.clear()
                            pipeline._dictation_mode = (mode == "dictate")
                            if mode == "dictate":
                                pipeline._segment_buffer.clear()
                                pipeline._dictation_segments.clear()
                            logger.info("Connection %s: start (mode=%s, audio buffer cleared)", ws_id, mode)

                    elif cmd_type == "segment":
                        pipeline = conn_state.get("pipeline")
                        if pipeline and conn_state.get("mode") == "dictate":
                            logger.info("Connection %s: segment marker", ws_id)
                            async with conn_lock:  # US-P10: serialize with text
                                await pipeline.process_segment()

                    elif cmd_type == "stop":
                        pipeline = conn_state.get("pipeline")
                        if pipeline:
                            mode = conn_state.get("mode", "ask")
                            buf_size = len(pipeline._audio_buffer) + len(pipeline._segment_buffer)
                            logger.info("Connection %s: stop (mode=%s, buffer=%d bytes)", ws_id, mode, buf_size)
                            async with conn_lock:  # US-P10: serialize with text
                                if mode == "dictate":
                                    transcript = await pipeline.finish_dictation()
                                    # Auto-save dictation to Dragon notes DB
                                    if transcript and len(transcript.strip()) > 10 and self._notes_svc:
                                        try:
                                            note = await self._notes_svc.create_from_text(
                                                transcript.strip(), title=""
                                            )
                                            logger.info("Auto-created note %s from dictation (%d chars)",
                                                        note.id, len(transcript))
                                            if not ws.closed:
                                                await ws.send_json({
                                                    "type": "note_created",
                                                    "note_id": note.id,
                                                    "title": note.title,
                                                    "transcript": transcript[:200],
                                                })
                                        except Exception as e:
                                            logger.error("Failed to auto-create dictation note: %s", e)
                                else:
                                    await pipeline.start_processing()

                    elif cmd_type == "clear":
                        pipeline = conn_state.get("pipeline")
                        if pipeline:
                            pipeline.clear_history()
                        # End current session and create a fresh one (clears DB context)
                        old_sid = conn_state.get("session_id")
                        device_id = conn_state.get("device_id")
                        if old_sid and self._session_mgr:
                            await self._session_mgr.end_session(old_sid)
                            # closes #56: create_session returns a single dict,
                            # NOT a (dict, bool) tuple — that's
                            # get_or_create_session.  The old tuple-unpack
                            # raised ValueError and tore down the WS handler,
                            # leaving Tab5 dead after a 'clear' + mode-swap
                            # sequence.  Symptom: Tab5 sat in RECONNECTING and
                            # every /chat returned 'voice not connected' with
                            # a blank chat view.
                            session = await self._session_mgr.create_session(
                                device_id=device_id, session_type="conversation"
                            )
                            conn_state["session_id"] = session["id"]
                            logger.info("Connection %s: history cleared, new session %s",
                                        ws_id, session["id"])
                            if not ws.closed:
                                await ws.send_json({
                                    "type": "session_start",
                                    "session_id": session["id"],
                                    "device_id": device_id,
                                    "resumed": False,
                                    "message_count": 0,
                                })
                        else:
                            logger.info("Connection %s: conversation history cleared", ws_id)

                    elif cmd_type == "cancel":
                        # Phase 1 (issue #91): cancel needs to reach in-flight
                        # text/media handler tasks too, not just the voice
                        # pipeline.  Today text/media are awaited inline above
                        # which blocks the WS read loop — a cancel frame from
                        # Tab5 sits in the TCP buffer until the inline await
                        # returns, by which point the response has already
                        # finished and Tab5 sees the cancelled tokens
                        # materialise after the user gave up.
                        pipeline = conn_state.get("pipeline")
                        cancelled_what: list[str] = []
                        handler_tasks = conn_state.setdefault("handler_tasks", {})
                        for slot in ("text", "media", "config"):
                            t = handler_tasks.get(slot)
                            if t and not t.done():
                                t.cancel()
                                try:
                                    await t
                                except (asyncio.CancelledError, Exception):
                                    # task may have raised mid-cancel; we
                                    # logged it; don't propagate to the WS
                                    # loop or the loop dies on us
                                    pass
                                handler_tasks[slot] = None
                                cancelled_what.append(slot)
                        # Always cancel the pipeline.  The text path calls
                        # pipeline._tts.synthesize() directly (see _handle_text),
                        # so a Piper subprocess can be alive even when no
                        # handler-task slot was occupied or _processing is False.
                        # pipeline.cancel() is idempotent.  (audit A1, #137)
                        if pipeline:
                            logger.info("Connection %s: cancel → pipeline.cancel", ws_id)
                            await pipeline.cancel()
                            cancelled_what.append("pipeline")
                        # Audit B1 (#165): also drop any scheduler-fired
                        # widgets that deferred during this turn — user
                        # cancelled the turn, so any reminder that fired
                        # during it should also disappear (a fresh fire
                        # cycle will pop on the next turn-idle window if
                        # the scheduler still wants to deliver it).
                        sid = conn_state.get("session_id")
                        if self._surface_mgr is not None and sid:
                            dropped = self._surface_mgr.discard_deferred(sid)
                            if dropped:
                                logger.info(
                                    "Connection %s: cancel → discarded %d deferred widget(s)",
                                    ws_id, dropped,
                                )
                                cancelled_what.append(f"deferred:{dropped}")
                        # Send ack so Tab5 has a positive signal that cancel
                        # landed — matters because Tab5 transitions to READY
                        # locally on cancel-send and may otherwise see late
                        # `llm` tokens that were already in TCP flight.
                        # Tab5-side fix at voice.c:752 covers the late-token
                        # case directly; this ack is the protocol-clean half
                        # of the same change.
                        await self._safe_send_json(ws, {
                            "type": "cancel_ack",
                            "cancelled": cancelled_what,
                        })

                    elif cmd_type == "text":
                        # Phase 1 (issue #91): spawn as a task so the WS read
                        # loop stays free for cancel/ping/voice frames.
                        # `conn_lock` is acquired INSIDE the task to preserve
                        # the prior US-P10 serialization with voice/segment
                        # paths.  If a previous text turn is still running
                        # (Tab5 normally queues with "+1 QUEUED" but be
                        # defensive) we wait for it before queuing the next.
                        await self._spawn_handler_task(
                            conn_state, "text",
                            self._handle_text, ws, conn_state, cmd,
                            conn_lock=conn_lock,
                        )

                    elif cmd_type == "user_media":
                        await self._spawn_handler_task(
                            conn_state, "media",
                            self._handle_user_media, ws, conn_state, cmd,
                            conn_lock=conn_lock,
                        )

                    elif cmd_type == "record_start" or cmd_type == "record_stop":
                        # Superseded by dictation mode (start with mode=dictate)
                        logger.info("Connection %s: %s (use mode=dictate instead)", ws_id, cmd_type)

                    elif cmd_type == "ping":
                        # ESP-IDF sends application-level pings (LEARNINGS.md #11)
                        await ws.send_json({"type": "pong"})

                    elif cmd_type == "config_update":
                        # Phase 1 (issue #91): config_update body extracted
                        # to _handle_config_update + spawned as task so the
                        # WS read loop stays free during the (potentially
                        # ~13 s) backend-swap window.  `conn_lock` is
                        # acquired inside the handler — same US-P01
                        # serialization with text/voice as before; the
                        # difference is cancel/ping/voice frames stay
                        # responsive instead of queueing.
                        # Coalesce: if a previous config_update is still
                        # in-flight, cancel it (last-write-wins matches
                        # user intent — the latest mode toggle is the one
                        # they want).
                        await self._spawn_handler_task(
                            conn_state, "config",
                            self._handle_config_update, ws, conn_state, conn_config, cmd,
                            conn_lock=conn_lock, coalesce=True,
                        )

                    elif cmd_type == "widget_action":
                        # v4·D Phase 4g (audit P0 fix): Tab5 fires this
                        # when the user taps a prompt choice / live action
                        # button / list row.  Before this branch existed,
                        # every interactive widget tap was silently
                        # dropped into the "Unknown command" logger.
                        sid = conn_state.get("session_id")
                        cid = cmd.get("card_id")
                        ev  = cmd.get("event")
                        payload = cmd.get("payload") or {}
                        logger.info("widget_action: session=%s card=%s event=%s",
                                    sid, cid, ev)
                        if sid and cid and ev and self._surface_mgr is not None:
                            try:
                                await self._surface_mgr.handle_action(
                                    sid, cid, ev, payload,
                                )
                            except Exception:
                                logger.exception("widget_action dispatch failed")

                    elif cmd_type == "config_ack":
                        logger.debug("Connection %s: config_ack %s", ws_id, cmd.get("applied"))

                    else:
                        logger.warning("Unknown command from %s: %s", ws_id, cmd_type)

                elif msg.type == WSMsgType.ERROR:
                    logger.error("WebSocket error for %s: %s", ws_id, ws.exception())
                    break

        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("WebSocket handler error for %s", ws_id)
        finally:
            # Stop keepalive
            _keepalive_running = False
            _keepalive_task.cancel()
            # Clean up: pause session, mark device offline, shut down pipeline
            await self._handle_disconnect(conn_state)
            self._active_connections.pop(ws_id, None)
            logger.info("WebSocket disconnected: %s (ws_id=%s)", peer, ws_id)

        return ws

    async def _handle_register(
        self,
        ws: web.WebSocketResponse,
        conn_state: dict,
        cmd: dict,
        on_audio,
        on_event,
        conn_config: VoiceConfig,
    ) -> None:
        """Handle device registration message."""
        device_id = cmd.get("device_id", "")
        hardware_id = cmd.get("hardware_id", "")
        requested_session = cmd.get("session_id")

        if not device_id:
            await ws.send_json(error_event(
                code="session_invalid",
                message="device_id is required",
                severity=Severity.FATAL, scope=Scope.SESSION,
            ))
            return

        ws_id = conn_state["ws_id"]
        logger.info("Registering device %s (hw=%s) on connection %s", device_id, hardware_id, ws_id)

        # P13: Evict stale connections for the same device_id.
        # Race condition: new connection arrives before aiohttp detects old TCP close.
        # The old keepalive task is still running, and its pipeline isn't shut down yet.
        for old_ws_id, old_conn in list(self._active_connections.items()):
            if old_ws_id == ws_id:
                continue  # Skip ourselves
            if old_conn.get("device_id") == device_id and old_conn.get("registered"):
                logger.warning(
                    "P13: Device %s already has connection %s — evicting stale connection",
                    device_id, old_ws_id,
                )
                # γ2-M5 (issue #108): tell the old client why it's being
                # disconnected BEFORE we tear its pipeline down.  Pre-fix
                # the client just saw TCP close and had no signal that
                # another instance had claimed the slot — Tab5 would then
                # auto-reconnect into the same eviction loop.  FATAL/DEVICE
                # is the "operator action needed; do NOT auto-reconnect"
                # signal Tab5 (γ2-H8) routes to the caption + retry banner.
                old_on_event = old_conn.get("_on_event")
                if old_on_event:
                    try:
                        await old_on_event(error_event(
                            code="device_evicted",
                            message="Another device claimed this session.",
                            severity=Severity.FATAL,
                            scope=Scope.DEVICE,
                        ))
                    except Exception as e:
                        # Stale / closed WS — eviction must still proceed.
                        # The user-visible signal is best-effort; the new
                        # connection's success matters more.
                        logger.debug(
                            "P13: device_evicted notice not delivered to %s: %s",
                            old_ws_id, e,
                        )

                # Shut down the old pipeline
                old_pipeline = old_conn.get("pipeline")
                if old_pipeline:
                    try:
                        await old_pipeline.shutdown()
                    except Exception as e:
                        logger.warning("P13: old pipeline shutdown failed for %s: %s", old_ws_id, e)
                    old_conn["pipeline"] = None

                # Pause the old session (not end — it might be resumed by the new connection)
                old_sid = old_conn.get("session_id")
                if old_sid and self._session_mgr:
                    await self._session_mgr.pause_session(old_sid)

                # Mark as unregistered so _handle_disconnect won't mark device offline
                old_conn["registered"] = False

                # Remove from active connections — _handle_disconnect will be a no-op
                self._active_connections.pop(old_ws_id, None)

                logger.info("P13: Evicted stale connection %s for device %s", old_ws_id, device_id)

        # Upsert device in DB.
        #
        # Audit D2 (#137): the `devices` table has a UNIQUE constraint on
        # `hardware_id`, so a second `device_id` registering with a
        # `hardware_id` that's already claimed raises sqlite3.IntegrityError
        # which used to bubble up to the WS handler and drop the connection
        # with no Tab5 signal.  Catch it specifically and emit a γ-arch
        # FATAL/DEVICE error so the user sees what happened.
        import sqlite3 as _sqlite3
        try:
            await self._db.upsert_device(
                device_id=device_id,
                hardware_id=hardware_id,
                name=cmd.get("name", ""),
                firmware_ver=cmd.get("firmware_ver", ""),
                platform=cmd.get("platform", ""),
                capabilities=cmd.get("capabilities"),
            )
        except _sqlite3.IntegrityError as e:
            if "hardware_id" in str(e).lower():
                logger.warning(
                    "D2 hardware_id collision: device_id=%s wanted hw=%s but "
                    "hw is already claimed by another device — rejecting register",
                    device_id, hardware_id,
                )
                if not ws.closed:
                    await self._safe_send_json(ws, error_event(
                        code="hardware_id_collision",
                        message="This hardware ID is already registered to another device.",
                        severity=Severity.FATAL,
                        scope=Scope.DEVICE,
                    ))
                return
            raise

        # v4·D audit P0 fix: expose the widget subset of client capabilities
        # on conn_state so skills can pull it via SurfaceManager and
        # downgrade emissions (smaller lists, lower-res media) for low-end
        # clients.  The register frame already ships this under
        # capabilities.widgets; pluck it out for quick lookup.
        caps = cmd.get("capabilities") or {}
        widget_caps = caps.get("widgets") if isinstance(caps, dict) else None
        conn_state["widget_capabilities"] = widget_caps or {
            "types": ["live", "card"],
            "list_max_items": 3, "chart_max_points": 8,
            "prompt_max_choices": 2,
        }
        logger.info("widget_capabilities for %s: %s",
                    device_id, conn_state["widget_capabilities"])
        await self._db.add_event(
            "device.connected", device_id=device_id,
            data={"platform": cmd.get("platform", ""), "firmware_ver": cmd.get("firmware_ver", "")}
        )

        # Get or create session
        session, resumed = await self._session_mgr.get_or_create_session(
            device_id=device_id,
            requested_session_id=requested_session,
            system_prompt=conn_config.llm.system_prompt,
        )
        session_id = session["id"]

        # Update connection state FIRST (before slow pipeline init)
        conn_state["session_id"] = session_id
        conn_state["device_id"] = device_id
        conn_state["registered"] = True
        conn_state["response_mode"] = "always_speak"  # voice device gets TTS

        # v4·D Phase 4g: register this connection's surface with the
        # shared SurfaceManager.  Skills dispatch widget_* emissions
        # through here and widget_action events route back via
        # handle_action().
        # v4·D audit P1: route surface + tool-event sends through the
        # _safe_send_json helper so a transient close mid-widget-emit
        # doesn't bubble into the WS handler and tear the session down.
        if self._surface_mgr is not None:
            async def _surface_send(msg: dict):
                if not ws.closed:
                    await self._safe_send_json(ws, msg)
            await self._surface_mgr.register_session(session_id, _surface_send, caps=conn_state.get("widget_capabilities"))

        # Phase 5 ε2 (issue #131): replay any queued offline
        # notifications for this device.  Hook fires AFTER
        # SurfaceManager.register_session so the scheduler manager
        # can find a live Tab5Surface for this session.  Best-effort:
        # a queue-drain failure logs a warning but doesn't block
        # registration (the user's reminders just stay queued for
        # the next register).
        scheduler_mgr = getattr(self, "_scheduler_mgr", None)
        if scheduler_mgr is not None:
            try:
                replayed = await scheduler_mgr.replay_queued_for_device(
                    device_id,
                )
                if replayed > 0:
                    logger.info(
                        "Scheduler offline-queue replay: delivered %d "
                        "frame(s) to %s on session %s",
                        replayed, device_id, session_id,
                    )
            except Exception as e:
                logger.warning(
                    "Scheduler offline-queue replay failed for %s: %s",
                    device_id, e,
                )

        # Store tool event callbacks per-connection (NOT on shared conversation engine)
        if self._tool_registry:
            # β-arch (issue #123): adapter so emit_progress_pair (which
            # takes an OnEvent: Callable[[dict], Awaitable[None]]) can
            # use the existing _safe_send_json swallow.  Returning
            # True/False from the helper is harmless — the pair helper
            # awaits but discards the return.
            async def _emit_via_ws(ev: dict) -> None:
                await self._safe_send_json(ws, ev)

            # Read the bus transition flag once at registration time —
            # not hot-reloaded.
            _emit_legacy = bool(getattr(
                conn_state.get("config"),
                "progress_bus_emit_legacy",
                True,
            ))

            async def _on_tool_call(call):
                # #75 phase 1b: pre-register the call + args in the
                # per-turn tracker so `_on_tool_result` can merge the
                # result into the same record.  The wrap synthesiser
                # reads both sides (e.g. `remember` needs the `fact`
                # from args to write "Got it — {fact}.").
                try:
                    conn_state.setdefault("tool_calls_this_turn", []).append({
                        "tool": call.get("tool"),
                        "args": call.get("args") or {},
                    })
                except Exception:
                    logger.debug("tool_calls_this_turn pre-register suppressed", exc_info=True)
                if not ws.closed:
                    # β-arch (issue #123): pair-emit — legacy
                    # `tool_call` for unmodified Tab5 + new
                    # progress.tool.start with the same payload nested.
                    await emit_progress_pair(
                        _emit_via_ws,
                        legacy={
                            "type": "tool_call",
                            "tool": call["tool"],
                            "args": call["args"],
                        },
                        phase=Phase.TOOL,
                        stage=Stage.START,
                        payload={"tool": call["tool"], "args": call["args"]},
                        emit_legacy=_emit_legacy,
                    )

            async def _on_tool_result(result):
                if ws.closed:
                    return
                # β-arch (issue #123): pair-emit — legacy
                # `tool_result` (with all result fields spread at top
                # level) + new progress.tool.done with the same fields
                # nested in payload for the unified bus.
                await emit_progress_pair(
                    _emit_via_ws,
                    legacy={"type": "tool_result", **result},
                    phase=Phase.TOOL,
                    stage=Stage.DONE,
                    payload={
                        "tool": result.get("tool"),
                        "result": result.get("result"),
                        "execution_ms": result.get("execution_ms"),
                    },
                    emit_legacy=_emit_legacy,
                )
                # #75 phase 1b: merge result into the most-recent
                # pre-registered call for this tool name (fills the
                # FIRST pending slot so same-tool-twice-in-one-turn
                # still maps 1:1).  If no pre-register exists (some
                # code paths emit tool_result only), append the bare
                # result so the wrap still has something to describe.
                try:
                    tracker = conn_state.setdefault("tool_calls_this_turn", [])
                    merged = False
                    for rec in tracker:
                        if rec.get("tool") == result.get("tool") and "result" not in rec:
                            rec["result"] = result.get("result")
                            rec["execution_ms"] = result.get("execution_ms")
                            merged = True
                            break
                    if not merged:
                        tracker.append(result)
                except Exception:
                    logger.debug("tool_calls_this_turn merge suppressed", exc_info=True)
                # v4·D Phase 4c: auto-emit widget_list for web_search results
                # so the Tab5 home live-slot surfaces the top hits without
                # the LLM having to orchestrate a widget call itself.
                try:
                    if result.get("tool") == "web_search":
                        payload = result.get("result") or {}
                        hits = payload.get("results") or []
                        query = payload.get("query", "")
                        items = []
                        for r in hits[:5]:
                            t = str(r.get("title") or r.get("snippet") or "")[:79]
                            if not t:
                                continue
                            items.append({"text": t, "value": ""})
                        if items:
                            await ws.send_json({
                                "type": "widget_list",
                                "skill_id": "web_search",
                                "card_id": f"ws_{session_id[:8]}",
                                "title": (query[:60] or "Web results"),
                                "tone": "info",
                                "priority": 70,
                                "items": items,
                            })
                except Exception:
                    logger.debug("widget_list auto-emit failed", exc_info=True)

            async def _on_tool_error(err: dict):
                """γ2-M1 (issue #104): emit a structured tool error
                frame when the parser swallows malformed JSON args.

                Pre-fix the failure was a silent `logger.warning` —
                the LLM continued without firing the tool and the
                user saw an empty/generic reply with zero signal that
                anything was attempted.  Now we surface a TRANSIENT
                error in the TOOL scope so Tab5 (γ2-H8) can render a
                non-blocking toast.

                Audit B4 (#137): the err dict's `code` and `message`
                fields are honoured so ConvEngine can signal e.g.
                `tool_call_limit_reached` distinct from the original
                `tool_args_invalid` parse failure.  Default codes
                preserve back-compat with callers that pre-date B4.

                The raw args are deliberately NOT included in the
                user-facing message — they may contain prompt-injection
                content from the LLM and Tab5's caption isn't a safe
                place to render arbitrary text.  Server log already
                carries the full failure for ops debugging.
                """
                if ws.closed:
                    return
                tool_name = err.get("name") or "(unknown)"
                code = err.get("code") or "tool_args_invalid"
                message = err.get("message") or (
                    f"Tool '{tool_name}' had invalid arguments — skipped."
                )
                # β-arch (issue #123): pair-emit — legacy γ1 error
                # frame (already structured per #102) + new
                # progress.tool.error frame for the unified bus.
                # Both carry the same code/message/severity/scope
                # so γ2-H8 routing applies regardless of which
                # frame Tab5 reads.
                await emit_progress_pair(
                    _emit_via_ws,
                    legacy=error_event(
                        code=code,
                        message=message,
                        severity=Severity.TRANSIENT,
                        scope=Scope.TOOL,
                    ),
                    phase=Phase.TOOL,
                    stage=Stage.ERROR,
                    code=code,
                    message=message,
                    severity=Severity.TRANSIENT,
                    scope=Scope.TOOL,
                    emit_legacy=_emit_legacy,
                )

            conn_state["on_tool_call"] = _on_tool_call
            conn_state["on_tool_result"] = _on_tool_result
            conn_state["on_tool_error"] = _on_tool_error

        # Send session_start IMMEDIATELY — before slow pipeline init.
        # Use _safe_send_json so a transient transport close (the Tab5
        # register-before-receive-task-running race, refs #31 + TT #76)
        # doesn't propagate an exception up to the WS handler and force
        # a session pause. If the send drops, the client will reconnect
        # shortly and we'll replay session_start on the next handshake.
        if not await self._safe_send_json(ws, {
            "type": "session_start",
            "session_id": session_id,
            "device_id": device_id,
            "resumed": resumed,
            "message_count": session.get("message_count", 0),
            "config": {
                "stt": conn_config.stt.backend,
                "tts": conn_config.tts.backend,
                "llm": conn_config.llm.backend,
                "tts_sample_rate": conn_config.audio.input_sample_rate,
                "response_mode": "match_input",
                "system_prompt": conn_config.llm.system_prompt,
            },
        }):
            logger.info("session_start send dropped on %s — client likely reconnecting", ws_id)
            return

        logger.info(
            "Device %s registered on session %s (resumed=%s, ws_id=%s)",
            device_id, session_id, resumed, ws_id,
        )

        # Audit C8/K15 (2026-04-20): on resume, replay the tail of the
        # message history so Tab5 chat can rehydrate its local store.
        # Previously session_start carried only message_count and Tab5
        # had to fetch via REST (which it never did) -- so a reconnect
        # lost the conversation from the user's view even though it was
        # on disk. Cap at 20 messages (most recent) to keep the WS frame
        # small; Tab5 can still fetch full history via
        # /api/v1/sessions/{id}/messages.
        if resumed and self._message_store is not None:
            try:
                msgs = await self._message_store.get_messages(
                    session_id, limit=20, offset=0
                )
                # Return the LAST 20 (get_messages returns ascending, so
                # slice the tail).
                tail = msgs[-20:] if len(msgs) > 20 else msgs
                items = []
                for m in tail:
                    role = m.get("role")
                    content = m.get("content")
                    if not role or not content:
                        continue
                    items.append({
                        "role": role,
                        "content": content,
                        "timestamp": m.get("created_at"),
                    })
                if items and not await self._safe_send_json(ws, {
                    "type": "session_messages",
                    "session_id": session_id,
                    "items": items,
                }):
                    logger.info("session_messages replay dropped on %s", ws_id)
                else:
                    logger.info("Replayed %d messages for session %s",
                                len(items), session_id)
            except Exception as e:
                logger.warning("session_messages replay failed: %s", e)

        # Reset conn_config to local defaults before pipeline init.
        # Tab5 will immediately send config_update with its actual mode,
        # so this avoids initializing cloud backends only to swap them out.
        # Only swap out when the configured backend is a cloud one — local
        # backends (ollama, lmstudio, npu_genie, dual) are already what
        # the user wants for mode 0/1, and re-pointing them at "ollama"
        # silently breaks any local-but-non-ollama default (notably the
        # dual-model pipeline added in #80).
        conn_config.stt.backend = "moonshine"
        conn_config.tts.backend = "piper"
        if conn_config.llm.backend in ("openrouter", "tinkerclaw"):
            conn_config.llm.backend = conn_config.llm.local_backend or "ollama"
        conn_config.llm.system_prompt = SYSTEM_PROMPT_LOCAL
        conn_config.llm.max_tokens = MAX_TOKENS_LOCAL

        # NOW initialize the voice pipeline (slow: Moonshine load ~2s)
        # This happens AFTER session_start is sent so Tab5 doesn't timeout.
        # Audit A2 (#142): pass the per-conn tool callbacks built above so
        # voice turns surface tool indicators just like text turns do.
        pipeline = VoicePipeline(
            conn_config, on_audio, on_event,
            conversation_engine=self._conversation,
            session_id=session_id,
            media_pipeline=self._media_pipeline,
            backend_pool=self._backend_pool,
            on_tool_call=conn_state.get("on_tool_call"),
            on_tool_result=conn_state.get("on_tool_result"),
            on_tool_error=conn_state.get("on_tool_error"),
            # Audit B1 (#165): surface_mgr lets the pipeline gate
            # scheduler-fired widgets so they don't interleave with
            # LLM token frames.
            surface_mgr=self._surface_mgr,
        )
        try:
            await pipeline.initialize()
        except Exception as e:
            logger.exception("Failed to initialize pipeline for %s", ws_id)
            if not ws.closed:
                # Don't leak the raw exception to the user — Tab5
                # surfaces this in the voice caption.  Operator can
                # see the actual `e` in the journal.
                await ws.send_json(error_event(
                    code="pipeline_init_failed",
                    message="Voice pipeline failed to start.  Try reconnecting.",
                    severity=Severity.FATAL, scope=Scope.SESSION,
                ))
            return

        conn_state["pipeline"] = pipeline
        logger.info("Pipeline ready for %s", ws_id)

    async def _spawn_handler_task(
        self,
        conn_state: dict,
        slot: str,
        coro_func,
        *args,
        conn_lock: asyncio.Lock | None = None,
        coalesce: bool = False,
    ) -> None:
        """Spawn a per-connection command handler as an asyncio task.

        Phase 1 of the UX-gap remediation (issue #91, see docs/UX-GAPS.md).
        The text/media/config handlers used to be awaited inline in the
        WS read loop, blocking it from receiving cancel/ping/voice frames
        for the full duration of the handler.  This helper detaches them
        as tracked tasks in `conn_state["handler_tasks"][slot]` so the
        cancel handler can selectively kill any of them, and so
        `_handle_disconnect` can clean them up on WS close.

        Args:
            conn_state: per-connection state dict (lives across the WS handler).
            slot: name of the task slot ("text", "media", "config").
            coro_func: the handler coroutine function to invoke.
            *args: positional args passed to the handler.
            conn_lock: if provided, acquired inside the spawned task before
                       calling the handler (preserves prior US-P10 serialization
                       semantics with the voice path).
            coalesce: if True, cancel any in-flight task in the same slot
                      before spawning the new one (last-write-wins — matches
                      user intent for config_update mode toggles).  If False
                      (default) the new task simply waits for `prev` to
                      finish before starting (matches Tab5's "+1 QUEUED"
                      stash semantics for text input).
        """
        handler_tasks = conn_state.setdefault("handler_tasks", {})
        prev = handler_tasks.get(slot)
        if prev and not prev.done():
            if coalesce:
                prev.cancel()
                try:
                    await prev
                except (asyncio.CancelledError, Exception):
                    # cancellation may surface as the underlying handler's
                    # exception; suppressed because we're about to replace
                    # it anyway.
                    pass
            else:
                # Wait for the previous handler in this slot to finish before
                # spawning the new one (preserves ordering for text turns).
                try:
                    await prev
                except (asyncio.CancelledError, Exception):
                    pass

        async def _run() -> None:
            try:
                if conn_lock is not None:
                    async with conn_lock:
                        await coro_func(*args)
                else:
                    await coro_func(*args)
            except asyncio.CancelledError:
                # Cancelled mid-handler — let it propagate so the task
                # transitions to CANCELLED state.  Resource cleanup is
                # the handler's responsibility (we've already cancelled
                # any pipeline TTS subprocess in the cancel cmd path).
                raise
            except Exception:
                # Handler raised — already logged inside the handler's
                # own try/except.  Don't propagate to the WS loop or
                # the loop dies on us; the WS catch at the outer
                # `async for msg in ws` is for transport-level errors,
                # not handler-level ones.
                logger.exception(
                    "handler_tasks[%s] raised — task will exit",
                    slot,
                )

        task = asyncio.create_task(_run(), name=f"ws_handler:{slot}")
        handler_tasks[slot] = task

    async def _handle_text(
        self, ws: web.WebSocketResponse, conn_state: dict, cmd: dict
    ) -> None:
        """Handle text input message — goes directly to conversation engine."""
        session_id = conn_state.get("session_id")
        if not session_id or not self._conversation:
            await ws.send_json(error_event(
                code="session_invalid",
                message="Not registered — send register first.",
                severity=Severity.FATAL, scope=Scope.SESSION,
            ))
            return

        content = cmd.get("content", "").strip()
        if not content:
            return

        text = content
        logger.info("Text input on session %s: %s", session_id, text[:80])

        # #75 phase 1b: reset the per-turn tool-call tracker.  Each
        # incoming text starts a new turn; the `_on_tool_result`
        # callback accumulates here so the end-of-turn empty-reply guard
        # can synthesise a template wrap from what actually fired.
        conn_state["tool_calls_this_turn"] = []

        # Audit B1 (#165): mark turn busy so scheduler-fired widgets
        # defer until this text turn completes — prevents the
        # `llm token / widget_card / llm token` interleave.
        if self._surface_mgr is not None:
            self._surface_mgr.mark_turn_start(session_id)
        try:
            await self._handle_text_body(ws, conn_state, cmd, text, session_id, content)
        finally:
            if self._surface_mgr is not None:
                try:
                    await self._surface_mgr.mark_turn_end(session_id)
                except Exception:
                    logger.exception("B1: turn-end drain failed for text turn")

    async def _handle_text_body(
        self, ws: web.WebSocketResponse, conn_state: dict, cmd: dict,
        text: str, session_id: str, content: str,
    ) -> None:
        """Body of _handle_text, wrapped by the B1 turn-gate bracket above."""

        # TinkerClaw mode: bypass ConversationEngine, use ConversationEngine's
        # swapped LLM (not pipeline._llm which may be stale after swap race)
        conn_cfg = conn_state.get("config")
        if conn_cfg and conn_cfg.llm.backend == "tinkerclaw" and self._conversation and self._conversation._llm:
            llm = self._conversation._llm
            logger.info("_handle_text TinkerClaw bypass via ConvEngine LLM: %s", llm.name)
            if hasattr(llm, 'set_session_key'):
                llm.set_session_key(conn_state.get("session_id", ""))

            # Send a "thinking" indicator immediately to keep the WS alive.
            # TinkerClaw agent can take 10-30s before first token (memory recall,
            # skill execution). Without this, ngrok kills the idle connection.
            if not ws.closed:
                await ws.send_json({"type": "llm", "text": ""})

            # #75 phase 1a: WS-level PING every 5 s while the LLM is
            # generating.  Previously inlined here as `_keepalive()` at
            # 10 s; now the shared helper lives on the class so the
            # vision-upload path (below in _handle_user_media) gets the
            # same protection instead of running bare.  5 s is safely
            # under every Tab5 firmware's PONG-watch window (30–45 s);
            # the outer `WebSocketResponse(heartbeat=60)` timer is too
            # slow for a 90 s 4 B-class Ollama turn.  See docs/AUDIT.md
            # "Local-mode gauntlet" for the observed P13-eviction race.
            full_response = []
            try:
                async with self._ws_keepalive_during_inference(ws, label="tc_text"):
                    async for token in llm.generate_stream_with_messages([
                        {"role": "user", "content": text}
                    ]):
                        full_response.append(token)
                        if not ws.closed:
                            await ws.send_json({"type": "llm", "text": token})
            except DragonError as e:
                # γ2-M6 (issue #106): TC gateway pre-flight health check
                # failed in ≤ 5 s.  Emit the structured γ1 error frame
                # so Tab5 can surface a FATAL/GATEWAY banner — pre-fix
                # the user waited the full 600 s sock_read timeout
                # before the connection-error fallback fired.
                logger.warning(
                    "TC text path fast-failed: %s (code=%s)",
                    e.message, e.code,
                )
                if not ws.closed:
                    await self._safe_send_json(ws, e.to_event())
                    await ws.send_json({
                        "type": "llm_done", "llm_ms": 0, "text": "",
                    })
                return

            response_text = "".join(full_response)

            # Wave 15 W15-H09 + #75 phase 1b: empty-response guard.  When
            # MiniMax / the TinkerClaw agent halts after a failed tool
            # call without formulating a user-facing reply, OR when an
            # FC-trained local model (xLAM, functiongemma, …) only
            # emitted tool calls with no natural-language wrap, we'd
            # otherwise send llm_done with text="" and Tab5 silently
            # drops the chat bubble.
            #
            # Preference order:
            #   1. If any tool fired this turn, synthesise a per-tool
            #      template wrap from `conn_state["tool_calls_this_turn"]`
            #      (response_wrap.synthesize_wrap).  Fast, deterministic,
            #      describes what actually ran — users get a useful ack
            #      like "Got it — magenta." rather than a frustrating
            #      "Sorry, I couldn't generate a response."
            #   2. Fall through to the legacy W15-H09 generic apology
            #      when there were zero tool fires (e.g. real LLM error).
            # #75 phase 1b refinement: trigger wrap when reply is
            # bracket-noise-only too, not just strict-empty.  See
            # looks_like_useful_text above for the heuristic.
            if not looks_like_useful_text(response_text):
                tool_calls = conn_state.get("tool_calls_this_turn") or []
                if tool_calls:
                    fallback = synthesize_wrap(tool_calls)
                    logger.info(
                        "#75 phase 1b: TC path produced near-empty LLM text "
                        "(%r) but %d tool(s) fired — emitting template wrap (%d chars)",
                        response_text[:30], len(tool_calls), len(fallback),
                    )
                else:
                    fallback = (
                        "Sorry, I couldn't generate a response for that. "
                        "Please try rephrasing, or try again in a moment."
                    )
                    logger.warning(
                        "W15-H09: TinkerClaw text path produced zero tokens — "
                        "emitting fallback response"
                    )
                if not ws.closed:
                    await ws.send_json({"type": "llm", "text": fallback})
                response_text = fallback

            logger.info("TinkerClaw text response (%d chars): %s",
                        len(response_text), response_text[:80])
            if not ws.closed:
                await ws.send_json({"type": "llm_done", "llm_ms": 0, "text": response_text})

            # v4·D connectivity polish: emit a zero-cost receipt on the
            # TinkerClaw bypass path so the chat bubble gets stamped
            # ("claw-agent · FREE") instead of no stamp at all.  TC turns
            # don't expose token counts the way OpenRouter does; we just
            # surface the engine name so transparency-per-bubble still
            # holds.
            if not ws.closed:
                # Wave 8 audit #2 (A4/F3/J12): the first-turn fallback was
                # the bare string "tinkerclaw" which shows up in chat
                # bubbles as a generic stamp until the gateway populates
                # `_model`. Fall back to the LLMConfig default
                # ("minimax/MiniMax-M2.5") when both `name` and `_model`
                # are empty so the first bubble stamp is still honest.
                inner = getattr(llm, "_model", "") or ""
                conf_default = getattr(conn_cfg.llm, "tinkerclaw_model", "") or ""
                tc_model = (
                    inner
                    or getattr(llm, "name", None)
                    or conf_default
                    or "minimax/MiniMax-M2.5"
                )
                try:
                    await ws.send_json({
                        "type": "receipt",
                        "stage": "llm",
                        "model": tc_model,
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0,
                        "cost_mils": 0,          # TC bills to its own gateway
                        "llm_ms": 0,
                        "retried": False,
                        "retry_reason": "",
                    })
                except Exception:
                    logger.debug("TC receipt emit failed", exc_info=True)

            # Rich media detection for TinkerClaw responses too.
            # Audit D4 (#137): emit a progress signal BEFORE rendering
            # if there's renderable content, so Tab5 doesn't perceive
            # the 1-3 s code-block render as a stalled reply.
            if full_response and self._media_pipeline:
                if not ws.closed and self._media_pipeline.has_renderable_content(response_text):
                    await self._safe_send_json(ws, {
                        "type": "media_rendering",
                        "stage": "start",
                    })
                try:
                    media_events = await self._media_pipeline.process_response(
                        response_text, session_id
                    )
                    # Audit D6 (TC path): send text_update BEFORE media events
                    # so Tab5's last-bubble targeting still points at the
                    # text bubble when the clear arrives.
                    if media_events:
                        logger.info("Sent %d media events for TinkerClaw response", len(media_events))
                        cleaned = self._media_pipeline.strip_rendered_content(response_text, media_events)
                        logger.info("Text stripped: %d→%d chars", len(response_text), len(cleaned))
                        if not ws.closed:
                            await ws.send_json({"type": "text_update", "text": cleaned})
                            logger.info("Sent text_update (D6 TC) with %d chars", len(cleaned))
                    for event in media_events:
                        if not ws.closed:
                            await ws.send_json(event)
                except Exception as e:
                    logger.warning("TinkerClaw media detection failed: %s", e)

            return

        try:
            # Stream LLM response via conversation engine.
            #
            # Wave 10 audit #78 fix: buffer tokens client-side before
            # forwarding so a stray `<tool>...</tool><args>...</args>`
            # block emitted mid-stream by qwen3:1.7b (or any small model
            # with a shaky tool-call grammar) never reaches the chat
            # bubble. conversation.py already strips markup on the
            # final-yield fallthrough path, but the happy path yields
            # one token at a time — a raw `<tool>` tag can land in Tab5
            # before the LLM finishes emitting the closing `</tool>`.
            #
            # Strategy: hold tokens in a rolling buffer. When the buffer
            # contains a complete `<tool>...</args>` block, strip it
            # before flushing. Emit the remaining prefix on every tick
            # so streaming latency stays low for normal text.
            full_response = []
            pending = ""  # tokens not yet safe to forward
            import re as _re
            _TOOL_RE_LOCAL = _re.compile(
                r"<tool>[\s\S]*?</tool>\s*<args>[\s\S]*?</args>\s*>?",
                _re.IGNORECASE,
            )
            # #75 phase 1a: same PING-during-inference protection used
            # on the TC + vision paths above.  Local Ollama + ConversationEngine
            # text turns on 4 B-class models routinely exceed 60 s, which
            # trips Tab5's PONG-watch (~30 s) without this helper and
            # triggers the P13 eviction race.
            async with self._ws_keepalive_during_inference(ws, label="local_text"):
                async for token in self._conversation.process_text_stream(
                    session_id=session_id,
                    text=content,
                    input_mode="text",
                    on_tool_call=conn_state.get("on_tool_call"),
                    on_tool_result=conn_state.get("on_tool_result"),
                    on_tool_error=conn_state.get("on_tool_error"),
                ):
                    full_response.append(token)
                    pending += token
                    # Strip any complete tool blocks sitting in the pending
                    # buffer. Substitute in-place so remaining prose still
                    # flushes below.
                    stripped = _TOOL_RE_LOCAL.sub("", pending)
                    if stripped != pending:
                        pending = stripped
                    # Hold back the tail if it looks like a partial tool
                    # marker so we don't flush `<tool>dat` to the client and
                    # then have to retract it.
                    hold_at = -1
                    for marker in ("<tool>", "<tool", "</tool", "<args", "</args"):
                        idx = pending.rfind(marker)
                        if idx >= 0 and idx > hold_at:
                            hold_at = idx
                    if hold_at >= 0:
                        flush, pending = pending[:hold_at], pending[hold_at:]
                    else:
                        flush, pending = pending, ""
                    if flush and not ws.closed:
                        await ws.send_json({"type": "llm", "text": flush})
                # End-of-stream: flush whatever remains, stripped one more time.
                pending = _TOOL_RE_LOCAL.sub("", pending)
                if pending and not ws.closed:
                    await ws.send_json({"type": "llm", "text": pending})

            response_text = _TOOL_RE_LOCAL.sub("", "".join(full_response))

            # #75 phase 1b: if the local model fired tools but never
            # produced user-facing text (common with FC-trained small
            # models like xLAM that emit tool calls then stop), wrap
            # the tool results in a templated natural-language ack so
            # Tab5 doesn't render an empty bubble.  Same intent as the
            # TC-path guard above, just applied to the local/conversation
            # engine flow.
            if not looks_like_useful_text(response_text):
                tool_calls = conn_state.get("tool_calls_this_turn") or []
                if tool_calls:
                    wrap = synthesize_wrap(tool_calls)
                    logger.info(
                        "#75 phase 1b: local text path emitted near-empty text "
                        "(%r) with %d tool fire(s) — sending template wrap (%d chars)",
                        response_text[:30], len(tool_calls), len(wrap),
                    )
                    if not ws.closed:
                        await ws.send_json({"type": "llm", "text": wrap})
                    response_text = wrap

            if not ws.closed:
                await ws.send_json({"type": "llm_done", "llm_ms": 0})

            # Rich media detection — scan response for image/chart/map references.
            # Audit D6: send text_update BEFORE media events. Tab5's
            # ui_chat_update_last_message targets the *last* chat bubble. If
            # we send media first, the image becomes "last" and the empty-
            # string text_update removes the wrong row. text_update first
            # clears the streamed markdown bubble; media events then append
            # the rendered JPEG below.
            if full_response:
                # Audit D4 (#137): emit progress before render so Tab5
                # doesn't perceive the 1-3 s code-block render as a
                # stalled reply.
                if not ws.closed and self._media_pipeline.has_renderable_content(response_text):
                    await self._safe_send_json(ws, {
                        "type": "media_rendering",
                        "stage": "start",
                    })
                try:
                    media_events = await self._media_pipeline.process_response(
                        response_text, session_id
                    )
                    logger.info("MediaPipeline: %d event(s) for response len=%d",
                                len(media_events), len(response_text))
                    if media_events:
                        cleaned = self._media_pipeline.strip_rendered_content(
                            response_text, media_events
                        )
                        logger.info("strip_rendered_content: %d->%d chars",
                                    len(response_text), len(cleaned))
                        if not ws.closed:
                            await ws.send_json({"type": "text_update", "text": cleaned})
                            logger.info("Sent text_update (D6) with %d chars", len(cleaned))
                    for event in media_events:
                        if not ws.closed:
                            await ws.send_json(event)
                except Exception as e:
                    logger.warning("Media detection failed: %s", e)

            # Synthesize TTS for the text response (only if response_mode != match_input)
            # match_input = text in, text out. always_speak = always TTS.
            pipeline = conn_state.get("pipeline")
            response_mode = conn_state.get("response_mode", "always_speak")
            if (pipeline and pipeline._tts and response_text.strip()
                    and not ws.closed and response_mode != "match_input"):
                try:
                    await ws.send_json({"type": "tts_start"})
                    t0 = time.monotonic()
                    # v4·D audit P1: mode-aware TTS synth budget.  Piper
                    # can take 15-25 s on Q6A ARM64 for a 200-word reply;
                    # cloud gpt-audio-mini is fast but still needs a
                    # cushion when OpenRouter edge adds latency.  The
                    # hardcoded 30 s was too tight in practice for local
                    # and wasteful for cloud.
                    tts_backend = (conn_cfg.tts.backend if conn_cfg else "piper")
                    tts_timeout = 90 if tts_backend != "openrouter" else 30
                    audio_bytes = await asyncio.wait_for(
                        pipeline._tts.synthesize(response_text),
                        timeout=tts_timeout,
                    )
                    tts_ms = (time.monotonic() - t0) * 1000

                    if audio_bytes:
                        # Audit B8 (#137) + C8 (#137): shared async
                        # resample with the voice-path TTS branch.
                        # Long replies (~150 KB) hop to a worker thread
                        # so this WS read loop stays free for cancels /
                        # other frames.
                        tts_rate = pipeline._tts.sample_rate
                        target_rate = conn_cfg.audio.input_sample_rate if conn_cfg else 16000
                        audio_bytes = await resample_pcm16_async(
                            audio_bytes, tts_rate, target_rate
                        )

                        chunk_size = 4096
                        pace_sleep = (chunk_size / 2) / target_rate * 0.8
                        for i in range(0, len(audio_bytes), chunk_size):
                            chunk = audio_bytes[i:i + chunk_size]
                            if not ws.closed:
                                await ws.send_bytes(chunk)
                            if i > chunk_size * 3:
                                await asyncio.sleep(pace_sleep)

                    if not ws.closed:
                        await ws.send_json({"type": "tts_end", "tts_ms": round(tts_ms)})
                        # Audit F5 (2026-04-20): TTS receipt for text-path
                        # synthesis so chat bubbles surface the TTS backend
                        # that spoke the reply.
                        try:
                            await ws.send_json({
                                "type": "receipt",
                                "stage": "tts",
                                "model": tts_backend,
                                "tts_ms": round(tts_ms),
                                "cost_mils": 0,
                            })
                        except Exception:
                            pass
                except Exception:
                    logger.exception("TTS for text input failed")
                    # Always send tts_end so Tab5 doesn't hang in SPEAKING
                    if not ws.closed:
                        await ws.send_json({"type": "tts_end", "tts_ms": 0})

            logger.info("Text response on session %s: %s", session_id, response_text[:80])

            # Phase 3 per-turn receipt for text-path turns. Voice-path
            # receipts are emitted from pipeline._process_utterance; the
            # text path reaches the LLM via ConversationEngine directly
            # and bypasses pipeline entirely, so we emit here too.
            try:
                convo = conn_state.get("conversation") or self._conversation
                cur_llm = getattr(convo, "_llm", None)
                if cur_llm is not None and hasattr(cur_llm, "get_last_usage"):
                    usage = cur_llm.get_last_usage()
                    if usage and usage.get("total_tokens"):
                        from dragon_voice.llm.openrouter_llm import price_for_model
                        cost_mils = price_for_model(
                            usage["model"],
                            usage.get("prompt_tokens", 0),
                            usage.get("completion_tokens", 0),
                        )
                        if not ws.closed:
                            await ws.send_json({
                                "type":              "receipt",
                                "stage":             "llm",
                                "model":             usage["model"],
                                "prompt_tokens":     usage.get("prompt_tokens", 0),
                                "completion_tokens": usage.get("completion_tokens", 0),
                                "total_tokens":      usage.get("total_tokens", 0),
                                "cost_mils":         cost_mils,
                                # v4·D Gauntlet G2 surface retries
                                "retried":           bool(usage.get("retried", False)),
                                "retry_reason":      usage.get("retry_reason", ""),
                            })
                        logger.info(
                            "Receipt emitted (text): model=%s tok=%d+%d=%d cost_mils=%d",
                            usage["model"],
                            usage.get("prompt_tokens", 0),
                            usage.get("completion_tokens", 0),
                            usage.get("total_tokens", 0),
                            cost_mils,
                        )
            except Exception:
                logger.exception("Text-path receipt emit failed")

        except Exception:
            logger.exception("Text processing error on session %s", session_id)
            if not ws.closed:
                await ws.send_json(error_event(
                    code="llm_failed",
                    message="Text processing failed — please try again.",
                    severity=Severity.TRANSIENT, scope=Scope.LLM,
                ))

    async def _handle_config_update(
        self,
        ws: web.WebSocketResponse,
        conn_state: dict,
        conn_config: VoiceConfig,
        cmd: dict,
    ) -> None:
        """Apply a Tab5 config_update — mode swap, model swap, key updates.

        Phase 1 (issue #91): extracted from the inline WS dispatcher so it
        can run as a tracked asyncio task without blocking the WS read loop.
        Behavior is identical to the prior inline body — only difference is
        former `continue` (skip remaining loop iterations) is now `return`
        (exit this handler invocation).

        C3 note (audit was wrong direction): the swap acquires `conn_lock`
        which serializes it with text/voice paths.  Swap WAITS for any
        in-flight inference to complete before running — never races it.
        After Phase 1's task discipline, the WS read loop stays free even
        while this handler is awaiting `conn_lock`, so cancel/ping/voice
        frames remain responsive.
        """
        ws_id = conn_state.get("ws_id", "?")

        # v4·D audit P1: rate-limit config_update to 2/sec/conn.  A buggy
        # skill or trigger-happy test harness could storm mode swaps that
        # each do heavy backend init.
        #
        # Audit C1 (#137): pre-fix this was a silent `logger.debug` +
        # `return` — Tab5's mode-toggle UI sat on its previous local
        # state and the user assumed the swap landed.  Now we emit a
        # γ-arch TRANSIENT/SESSION error so Tab5 (γ2-H8) can render a
        # toast like "Slow down — give the swap a moment."  Defensive:
        # only emit if the WS is still open.
        _now_cfg = time.monotonic()
        _last_cfg = conn_state.get("_last_config_update_ts", 0.0)
        if _now_cfg - _last_cfg < 0.5:
            logger.debug("config_update rate-limited on %s", ws_id)
            if not ws.closed:
                await self._safe_send_json(ws, error_event(
                    code="config_update_rate_limited",
                    message="Mode swap rate-limited — try again in a moment.",
                    severity=Severity.TRANSIENT,
                    scope=Scope.SESSION,
                ))
            return
        conn_state["_last_config_update_ts"] = _now_cfg

        # Three-tier voice mode: 0=local, 1=hybrid, 2=cloud, 3=tinkerclaw
        voice_mode = cmd.get("voice_mode")
        llm_model = cmd.get("llm_model")
        # Backward compat: old binary cloud_mode toggle
        cloud_mode = cmd.get("cloud_mode")
        if cloud_mode is not None and voice_mode is None:
            voice_mode = 2 if cloud_mode else 0

        if voice_mode is not None:
            # STT+TTS: local for mode 0, cloud for mode 1+2+3
            if voice_mode == 0:
                stt_be, tts_be = "moonshine", "piper"
            elif voice_mode == 3:
                # TinkerClaw mode: default local STT/TTS
                # "cloud" suffix in llm_model → use OpenRouter STT/TTS
                if llm_model and "cloud" in llm_model.lower():
                    stt_be, tts_be = "openrouter", "openrouter"
                else:
                    stt_be, tts_be = "moonshine", "piper"
            else:
                stt_be, tts_be = "openrouter", "openrouter"

            # LLM backend selection
            if voice_mode == 3:
                # TinkerClaw mode — gateway handles everything
                llm_be = "tinkerclaw"
                if llm_model:
                    conn_config.llm.tinkerclaw_model = llm_model
            elif voice_mode == 2:
                llm_be = "openrouter"
                if llm_model:
                    conn_config.llm.openrouter_model = llm_model
            else:
                llm_be = conn_config.llm.local_backend or "ollama"
                if llm_model and llm_be == "ollama" and "/" not in llm_model:
                    conn_config.llm.ollama_model = llm_model
                    logger.info("Local model switched to: %s", llm_model)

            # Apply mode-aware system prompt and max_tokens
            # Mode 3 (TinkerClaw): skip — TinkerClaw owns personality
            if voice_mode == 3:
                pass  # TinkerClaw manages its own prompts and limits
            elif voice_mode == 0:
                conn_config.llm.system_prompt = SYSTEM_PROMPT_LOCAL
                conn_config.llm.max_tokens = MAX_TOKENS_LOCAL
            elif voice_mode == 1:
                conn_config.llm.system_prompt = SYSTEM_PROMPT_HYBRID
                conn_config.llm.max_tokens = MAX_TOKENS_HYBRID
            else:
                conn_config.llm.system_prompt = SYSTEM_PROMPT_CLOUD
                conn_config.llm.max_tokens = MAX_TOKENS_CLOUD

            logger.info("Connection %s: voice_mode=%d → stt=%s tts=%s llm=%s model=%s tokens=%d",
                        ws_id, voice_mode, stt_be, tts_be, llm_be,
                        conn_config.llm.openrouter_model if voice_mode == 2 else "(local)",
                        conn_config.llm.max_tokens)

            # Validate TinkerClaw gateway is reachable before switching to mode 3
            if voice_mode == 3:
                try:
                    tc_url = (conn_config.llm.tinkerclaw_url or "http://localhost:18789").rstrip("/")
                    async with aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=5)
                    ) as tc_session:
                        async with tc_session.get(f"{tc_url}/health") as tc_resp:
                            if tc_resp.status != 200:
                                logger.error("TinkerClaw health check returned %d", tc_resp.status)
                                raise RuntimeError(f"health check returned {tc_resp.status}")
                    logger.info("TinkerClaw gateway health OK at %s", tc_url)
                except Exception as tc_err:
                    logger.error("TinkerClaw gateway not reachable: %s", tc_err)
                    # Audit G5 (2026-04-20): revert to Local so Tab5 doesn't
                    # sit wedged on mode 3 showing an error. Matches the
                    # OpenRouter-key-missing path below.
                    # Audit D5 (#137): same γ-arch shape as B7 / OR-key
                    # for consistency — error_event + plain config_update
                    # revert.
                    if not ws.closed:
                        await self._safe_send_json(ws, error_event(
                            code="tc_gateway_unreachable",
                            message="TinkerClaw gateway is not reachable — reverted to local.",
                            severity=Severity.FATAL,
                            scope=Scope.GATEWAY,
                        ))
                        await self._safe_send_json(ws, {
                            "type": "config_update",
                            "voice_mode": 0,
                        })
                    return

            # Validate API key for cloud modes (1=Hybrid, 2=Cloud need OpenRouter)
            # Mode 3 (TinkerClaw) doesn't need Dragon's OpenRouter key — uses own gateway.
            #
            # Audit D5 (#137): pre-fix the toast was a bare
            # `config_update.error` raw-string that didn't tell the user
            # what happened next.  Migrated to the same γ-arch error_event
            # + revert pattern as B7 (TC token check) for consistency.
            if voice_mode in (1, 2) and not conn_config.llm.openrouter_api_key:
                logger.error("Cloud mode requested but no API key configured")
                if not ws.closed:
                    await self._safe_send_json(ws, error_event(
                        code="openrouter_key_missing",
                        message="OpenRouter key not configured — reverted to local.",
                        severity=Severity.FATAL,
                        scope=Scope.LLM,
                    ))
                    await self._safe_send_json(ws, {
                        "type": "config_update",
                        "voice_mode": 0,
                    })
                return

            # B7 (audit, #137): TC mode needs a token; without one the
            # backend's __init__ raises ValueError, which would leak via
            # the A4 raw-exception path below.  Validate up-front like
            # the OpenRouter key check above so the user sees a clean
            # γ-arch error and a clean revert instead of a stack trace.
            if voice_mode == 3 and not (conn_config.llm.tinkerclaw_token or "").strip():
                logger.error("TC mode requested but tinkerclaw_token is blank")
                if not ws.closed:
                    await self._safe_send_json(ws, error_event(
                        code="tc_token_missing",
                        message="TinkerClaw token not configured — reverted to local",
                        severity=Severity.FATAL,
                        scope=Scope.GATEWAY,
                    ))
                    await self._safe_send_json(ws, {
                        "type": "config_update",
                        "voice_mode": 0,
                    })
                return

            # Update session system prompt in DB for conversation engine
            # Chat v4·C (refs #27): also persist voice_mode + llm_model
            # onto the session row so the drawer surfaces the active
            # mode fingerprint and pipeline-resume picks the right
            # backends without a fresh config_update from the client.
            sid = conn_state.get("session_id")
            if sid and self._db:
                # Resolve the best "active model" string to persist,
                # matching the client-visible payload below.
                if voice_mode == 2:
                    active_model_db = conn_config.llm.openrouter_model or ""
                elif llm_be == "tinkerclaw":
                    active_model_db = conn_config.llm.tinkerclaw_model or ""
                elif llm_be == "ollama":
                    active_model_db = conn_config.llm.ollama_model or ""
                else:
                    active_model_db = str(llm_model or "")
                try:
                    await self._db.update_session(
                        sid,
                        system_prompt=conn_config.llm.system_prompt,
                        voice_mode=int(voice_mode),
                        llm_model=active_model_db[:128],
                    )
                except Exception:
                    logger.warning(
                        "Failed to update session system_prompt / mode"
                    )

            # Apply config
            conn_config.stt.backend = stt_be
            conn_config.tts.backend = tts_be
            conn_config.llm.backend = llm_be

            # Propagate API keys for cloud STT/TTS backends (modes 1-2, or mode 3 with cloud STT)
            if voice_mode in (1, 2) or (voice_mode == 3 and stt_be == "openrouter"):
                conn_config.stt.openrouter_api_key = conn_config.llm.openrouter_api_key
                conn_config.stt.openrouter_url = conn_config.llm.openrouter_url
                conn_config.tts.openrouter_api_key = conn_config.llm.openrouter_api_key
                conn_config.tts.openrouter_url = conn_config.llm.openrouter_url

            # Hot-swap backends on pipeline AND conversation engine.
            # Phase 1 (issue #91): conn_lock acquisition was previously here in
            # the inline body; now handled by `_spawn_handler_task` on the
            # outer task wrapper.  Same US-P01 serialization with stop/text
            # handlers is preserved.
            pipeline = conn_state.get("pipeline")
            if pipeline:
                try:
                    # swap_backends() now handles cancel internally
                    # and sets _swapping flag to drop audio during swap
                    await pipeline.swap_backends(conn_config)
                    # Inject session key for TinkerClaw conversation continuity
                    if llm_be == "tinkerclaw" and hasattr(pipeline, '_llm'):
                        if hasattr(pipeline._llm, 'set_session_key'):
                            pipeline._llm.set_session_key(
                                conn_state.get("session_id", ""))
                except DragonError as de:
                    # Already a γ-arch structured error — emit verbatim.
                    logger.warning("Backend swap failed (DragonError): %s", de.message)
                    await self._safe_send_json(ws, de.to_event())
                    await self._safe_send_json(ws, {
                        "type": "config_update",
                        "voice_mode": 0,
                    })
                    return
                except Exception:
                    # A4 (audit, #137): the prior code did
                    # `f"Backend swap failed: {e}"` which leaked raw
                    # Python exception text (e.g. the multi-line
                    # OpenRouter / TC ValueError) into Tab5's voice
                    # caption.  Send a γ-arch error event with a
                    # user-friendly message instead; the full trace is
                    # still in the logs via logger.exception below.
                    logger.exception(
                        "Backend swap failed for %s",
                        conn_state.get("ws_id", "?"),
                    )
                    await self._safe_send_json(ws, error_event(
                        code="backend_swap_failed",
                        message="Couldn't switch backends — reverted to local",
                        severity=Severity.FATAL,
                        scope=Scope.LLM,
                    ))
                    await self._safe_send_json(ws, {
                        "type": "config_update",
                        "voice_mode": 0,
                    })
                    return

            # Also swap ConversationEngine LLM (used by _handle_text).
            # W15-C01: prefer the pooled instance so we don't re-load Ollama /
            # re-open the aiohttp session on every config_update.  Only the
            # pipeline owns the shutdown of a pooled backend.
            if self._conversation:
                try:
                    from dragon_voice.llm import create_llm
                    from dragon_voice.pipeline import _llm_sig
                    new_key = _llm_sig(conn_config.llm)
                    pooled = self._backend_pool.get(new_key)
                    old_llm = self._conversation._llm
                    if pooled is not None:
                        new_llm = pooled
                    else:
                        new_llm = create_llm(conn_config.llm)
                        await new_llm.initialize()
                        self._backend_pool[new_key] = new_llm
                    # Only shutdown the OLD one if nobody in the pool
                    # references it (i.e. it wasn't pooled).
                    if old_llm is not None and old_llm not in self._backend_pool.values():
                        await old_llm.shutdown()
                    self._conversation._llm = new_llm
                    # 2026-04-23 (#58): also swap _llm_config so the
                    # compact-vs-full tool prompt logic in
                    # ConversationEngine._augment_context_with_tools picks the
                    # right format for the active backend.  Without this,
                    # cloud-mode agents stayed on the top-5 compact tool list
                    # and never saw weather, stock_ticker, timesense_timer,
                    # quick_poll, note, system_info, or unit_converter.
                    self._conversation._llm_config = conn_config.llm
                    logger.info("ConversationEngine LLM swapped to %s%s (backend=%s)",
                                new_llm.name, " (pooled)" if pooled else "", conn_config.llm.backend)
                except Exception as e:
                    logger.exception("ConversationEngine LLM swap failed: %s", e)

            # Update displayed names
            self._stt_name = stt_be
            self._tts_name = tts_be
            self._llm_name = llm_be

            # Confirm to Tab5
            if not ws.closed:
                # Report actual model for any mode
                if voice_mode == 2:
                    active_model = conn_config.llm.openrouter_model
                elif llm_be == "tinkerclaw":
                    active_model = conn_config.llm.tinkerclaw_model
                elif llm_be == "ollama":
                    active_model = conn_config.llm.ollama_model
                else:
                    active_model = ""
                await ws.send_json({
                    "type": "config_update",
                    "config": {
                        "stt": stt_be, "tts": tts_be,
                        "llm": llm_be,
                        "llm_model": active_model,
                        "voice_mode": voice_mode,
                        "cloud_mode": voice_mode >= 1,
                    },
                })

                # v4·D Phase 4b vision capability advertisement.  Tab5's
                # camera screen renders a "VISION · <model> READY" chip based
                # on this.
                try:
                    vm = conn_config.llm.openrouter_model.lower() \
                        if voice_mode == 2 else ""
                    om = conn_config.llm.ollama_model.lower() \
                        if voice_mode == 0 else ""
                    vision_model = ""
                    per_frame_mils = 0
                    if voice_mode == 2:
                        if "gpt-4o" in vm:
                            vision_model = active_model
                            per_frame_mils = 1200  # ~$0.012/frame
                        elif "sonnet" in vm:
                            vision_model = active_model
                            per_frame_mils = 4500  # ~$0.045/frame
                        elif "haiku" in vm:
                            # Haiku 3.5 supports vision per OR
                            vision_model = active_model
                            per_frame_mils = 400
                    elif voice_mode == 0:
                        if "vision" in om or "llava" in om:
                            vision_model = active_model
                            per_frame_mils = 0  # local = free
                    await ws.send_json({
                        "type":           "vision_capability",
                        "can_see":        bool(vision_model),
                        "model":          vision_model,
                        "per_frame_mils": per_frame_mils,
                    })
                except Exception:
                    logger.exception("vision_capability emit failed")

            # v4·D Gauntlet G7-F: speak a short alert when the Tab5
            # auto-downgrades because the daily cap was hit.
            try:
                if cmd.get("reason") == "cap_downgrade":
                    pipeline = conn_state.get("pipeline")
                    if pipeline and hasattr(pipeline, "speak_system"):
                        # Wave 14 W14-C06: track the task so
                        # _handle_disconnect can cancel it if the user
                        # closes mid-utterance.
                        bg = conn_state["bg_tasks"]
                        t = asyncio.create_task(pipeline.speak_system(
                            "Daily budget cap reached. Switched back to local mode."
                        ))
                        bg.add(t)
                        t.add_done_callback(bg.discard)
            except Exception:
                logger.exception("cap_downgrade alert failed")

    async def _handle_user_media(self, ws, conn_state, cmd):
        """Handle image/audio uploaded by Tab5 for multimodal LLM analysis."""
        import base64
        media_id = cmd.get("media_id", "")
        text = cmd.get("text", "What's in this image?")
        session_id = conn_state.get("session_id", "")
        conn_config = conn_state.get("config", self._config)

        image_path = await self._media_store.get_path(media_id)
        if not image_path:
            if not ws.closed:
                await ws.send_json(error_event(
                    code="media_not_found",
                    message="Image not found — please retake the photo.",
                    severity=Severity.TRANSIENT, scope=Scope.MEDIA,
                ))
            return

        backend = conn_config.llm.backend
        if backend == "ollama" and "vision" not in conn_config.llm.ollama_model:
            if not ws.closed:
                await ws.send_json(error_event(
                    code="vision_unsupported",
                    message="Image analysis needs Cloud or TinkerClaw mode.",
                    severity=Severity.FATAL, scope=Scope.LLM,
                ))
            return

        # Wave 13 H2: image may be up to ~8 MB (camera JPEG) -- reading on the
        # event loop thread stalls every other WS connection. Offload the
        # blocking read + base64 to the default executor.
        def _read_and_encode(path: str) -> str:
            with open(path, "rb") as f:
                return base64.b64encode(f.read()).decode()
        image_b64 = await asyncio.to_thread(_read_and_encode, image_path)

        messages = [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                {"type": "text", "text": text},
            ]
        }]

        # #75 phase 1b: reset per-turn tool tracker on this path too.
        conn_state["tool_calls_this_turn"] = []

        full_response = []
        llm = conn_state.get("conversation")
        if llm and hasattr(llm, '_llm'):
            llm_backend = llm._llm
        else:
            llm_backend = None

        if not llm_backend:
            if not ws.closed:
                await ws.send_json(error_event(
                    code="no_llm_available",
                    message="No language model is configured.  Check Settings.",
                    severity=Severity.FATAL, scope=Scope.LLM,
                ))
            return

        try:
            # #75 phase 1a: same PING-during-inference protection as the
            # TC text path above — vision-upload LLM turns can run 30+ s
            # on multimodal models, long enough for Tab5's PONG-watch to
            # trip without the helper.
            async with self._ws_keepalive_during_inference(ws, label="vision"):
                async for token in llm_backend.generate_stream_with_messages(messages):
                    full_response.append(token)
                    if not ws.closed:
                        await ws.send_json({"type": "llm", "text": token})
        except Exception as e:
            logger.error("user_media LLM failed: %s", e)
            if not ws.closed:
                # Phase 3 γ1: was raw `str(e)` — leaked Python exception
                # text (e.g. "list index out of range") into Tab5's voice
                # caption.  Now a stable, user-friendly message keyed by
                # `vision_failed`; cause kept in the server log only.
                await ws.send_json(error_event(
                    code="vision_failed",
                    message="Image analysis failed — please try again.",
                    severity=Severity.TRANSIENT,
                    scope=Scope.LLM,
                ))
            return

        # #75 phase 1b: vision path gets the same empty-reply guard as
        # the text paths.  Multimodal models can fire a tool (e.g.
        # `note` to save a snapshot caption) and stop without text.
        if not looks_like_useful_text("".join(full_response)):
            tool_calls = conn_state.get("tool_calls_this_turn") or []
            if tool_calls:
                wrap = synthesize_wrap(tool_calls)
                logger.info(
                    "#75 phase 1b: vision path emitted near-empty text with "
                    "%d tool fire(s) — sending template wrap (%d chars)",
                    len(tool_calls), len(wrap),
                )
                if not ws.closed:
                    await ws.send_json({"type": "llm", "text": wrap})
                full_response.append(wrap)

        if not ws.closed:
            await ws.send_json({"type": "llm_done", "llm_ms": 0})

        if full_response and self._message_store:
            try:
                await self._message_store.add_message(
                    session_id=session_id,
                    role="assistant",
                    content="".join(full_response),
                    input_mode="vision",
                )
            except Exception as e:
                logger.warning("Failed to store vision response: %s", e)

    async def _handle_disconnect(self, conn_state: dict) -> None:
        """Handle WebSocket disconnect: pause session, mark device offline."""
        session_id = conn_state.get("session_id")
        device_id = conn_state.get("device_id")
        ws_id = conn_state.get("ws_id")
        pipeline = conn_state.get("pipeline")

        # Wave 14 W14-C06: cancel any per-connection background tasks
        # (e.g. cap_downgrade speak_system) before we shut down the pipeline
        # they depend on. Without this, the orphan task holds the Piper
        # subprocess + TTS lock past WS close.
        bg_tasks = conn_state.get("bg_tasks") or set()
        if bg_tasks:
            for t in list(bg_tasks):
                t.cancel()
            await asyncio.gather(*bg_tasks, return_exceptions=True)

        # Phase 1 (issue #91): cancel any in-flight per-command handler
        # tasks (text/media/config).  These are spawned by
        # `_spawn_handler_task` and tracked in
        # `conn_state["handler_tasks"]`.  Without this, a slow text
        # turn that's still streaming to the LLM when Tab5 disconnects
        # would keep generating tokens (and writing assistant
        # messages to the DB on the now-dead session) until naturally
        # complete.
        handler_tasks = conn_state.get("handler_tasks") or {}
        live = [t for t in handler_tasks.values() if t and not t.done()]
        if live:
            for t in live:
                t.cancel()
            await asyncio.gather(*live, return_exceptions=True)

        # v4·D Phase 4g: unregister the session's surface so skills that
        # kept a reference to it start seeing dropped sends explicitly.
        if session_id and self._surface_mgr is not None:
            try:
                await self._surface_mgr.unregister_session(session_id)
            except Exception:
                logger.debug("surface unregister failed")

        try:
            # Pause session (not end — it can be resumed)
            if session_id and self._session_mgr:
                await self._session_mgr.pause_session(session_id)

            # Mark device offline ONLY if no other active connection for same device.
            if device_id and self._db:
                other_active = any(
                    c.get("device_id") == device_id and c.get("registered")
                    for cid, c in self._active_connections.items()
                    if cid != ws_id
                )
                if not other_active:
                    await self._db.set_device_online(device_id, False)
                    await self._db.add_event(
                        "device.disconnected", device_id=device_id,
                        data={"session_id": session_id},
                    )
        except (RuntimeError, Exception) as e:
            # Database may be closed during server shutdown — safe to ignore
            logger.debug("_handle_disconnect db access failed (shutdown?): %s", e)

        # Shut down pipeline
        if pipeline:
            await pipeline.shutdown()


def run_server(config: VoiceConfig) -> None:
    """Start the voice server (blocking)."""
    server = VoiceServer(config)
    app = server.create_app()

    logger.info(
        "Starting Dragon Voice Server on %s:%d",
        config.server.host,
        config.server.port,
    )

    web.run_app(
        app,
        host=config.server.host,
        port=config.server.port,
        print=lambda msg: logger.info(msg),
    )
