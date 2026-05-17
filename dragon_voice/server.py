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

from dragon_voice.backend_swap import swap_pipeline_and_conversation_backends
from dragon_voice.cap_downgrade import maybe_speak_cap_downgrade_alert
from dragon_voice.codec_negotiation import (
    maybe_swap_uplink_codec,
    negotiate_uplink_codec_at_register,
)
from dragon_voice.config_finalize import (
    apply_swap_config_to_conn,
    persist_session_config_to_db,
)
from dragon_voice.config_swap import select_backends_for_mode
from dragon_voice.config_swap_guards import validate_config_swap_prereqs
from dragon_voice.config_update_ack import emit_config_update_ack
from dragon_voice.config_update_rate_limit import (
    check_config_update_rate_limit,
)
from dragon_voice.conn_state import ConnState
from dragon_voice.device_upsert import upsert_device_with_collision_guard
from dragon_voice.session_handshake import (
    emit_session_start,
    replay_session_message_tail,
)
from dragon_voice.empty_response_wrap import maybe_synthesize_empty_response_wrap
from dragon_voice.text_path_tts import synthesize_and_stream_text_response
from dragon_voice.tinkerclaw_text_path import handle_tinkerclaw_text_path
from dragon_voice.local_text_stream import stream_local_text_with_tool_filter
from dragon_voice.vision_turn import handle_vision_turn
from dragon_voice.ws_voice_admission import check_ws_voice_admission
from dragon_voice.ws_keepalive import run_ws_keepalive
from dragon_voice.binary_frame_dispatch import dispatch_binary_frame
from dragon_voice.cancel_handler import handle_cancel_command
from dragon_voice.start_handler import handle_start_command  # W6-A
from dragon_voice.clear_handler import handle_clear_command
from dragon_voice.stop_handler import handle_stop_command
from dragon_voice.pipeline_init import build_and_initialize_pipeline
from dragon_voice.widget_capabilities_init import init_widget_capabilities
from dragon_voice.disconnect_handler import handle_disconnect as _disconnect_chain
from dragon_voice.handler_task_spawn import spawn_handler_task
from dragon_voice.channel_reply_handler import handle_channel_reply
from dragon_voice.widget_action_handler import handle_widget_action
from dragon_voice.text_turn_gate import invoke_with_text_turn_gate
from dragon_voice.pipeline_callbacks import PipelineCallbacks
from dragon_voice.rich_media_emit import emit_rich_media_for_text_turn
from dragon_voice.stale_conn_eviction import evict_stale_connections_for_device
from dragon_voice.surface_register import register_surface_and_replay_scheduler
from dragon_voice.text_path_receipt import (
    emit_text_path_llm_receipt,
    emit_tinkerclaw_zero_cost_receipt,
)
from dragon_voice.tool_event_emitter import ToolEventEmitter
from dragon_voice.media.store import MediaStore
from dragon_voice.media.pipeline import MediaPipeline
from dragon_voice.vision_capability import emit_vision_capability
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
from dragon_voice.voice_modes import VoiceMode
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
        self._active_connections: dict[str, ConnState] = {}
        self._max_connections = 10
        self._purge_task: Optional[asyncio.Task] = None
        self._memory_monitor_task: Optional[asyncio.Task] = None

        # Memory thresholds (MB) — Dragon has 11 GB total.  Ollama runs in
        # a separate process and doesn't count toward voice-server RSS.
        # Post-W15-C01 (STT/TTS/LLM pooled across reconnects), Moonshine's
        # ~2.5 GB plus Piper + Python steady-state lands the voice server
        # at ~2.7 GB resident — the prior 2048/3072 thresholds fired warn
        # constantly and triggered crit-restart on legitimate steady-state
        # peaks (#29).  Bumped so warn fires above the floor and crit
        # gives real headroom before the pipeline-restart hammer.
        self._mem_warn_mb = 3072   # Force GC above this
        self._mem_crit_mb = 4096   # Restart pipeline above this (after GC)

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
            Tab5 firmware revision's PONG-watch window (current
            firmware: 180 s pong budget; older firmware: 30–45 s).
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
        # SOLID-audit follow-up: WS auth + connection-cap admission
        # gate extracted to ws_voice_admission.check_ws_voice_admission.
        # That module owns: W14-C04 bearer-token check via
        # hmac.compare_digest, the dev-mode unauthenticated bypass
        # warning, the >= max_connections cap, and the γ3-Dragon
        # (#111) JSON error-frame shape for both 401 / 503.  Returns
        # None to proceed; a ready-to-return Response when rejected.
        expected_token = (getattr(self._config.server, "api_token", "") or "").strip()
        rejection = check_ws_voice_admission(
            request,
            expected_token=expected_token,
            active_connection_count=len(self._active_connections),
            max_connections=self._max_connections,
        )
        if rejection is not None:
            return rejection

        # WS heartbeat budget (2026-05-17 dictation regression fix).
        #
        # Background: aiohttp's `heartbeat=N` sends a server-initiated
        # PING every N seconds AND silently uses N/2 as the PONG-wait
        # window.  At heartbeat=60 that's a 30 s PONG budget — which
        # turned out to be tight enough to bite during long-form
        # dictation: Tab5's esp_websocket_client serves RX + TX + auto-
        # PONG on a single task, and while a 30 s audio segment is
        # streaming up the TX queue can starve PONG handling.  The
        # journal smoking-gun: `WebSocket error for ws12: No PONG
        # received after 30.0 seconds`, after which Tab5 reconnects
        # and the in-flight transcribe POST fails with "network error"
        # on the Tab5 UI.
        #
        # Fix: bump heartbeat to 180 s so the implicit PONG-watch
        # window is 90 s — well outside any realistic TX-backpressure
        # delay on Tab5.  Receive_timeout stays at 600 s (10 min idle
        # → drop) as the dead-socket guard.  Tab5 still sends its own
        # 15-s PINGs which Dragon auto-PONGs via aiohttp, so liveness
        # detection works both directions.  The bigger window only
        # affects how aggressively Dragon probes silent connections;
        # half-open sockets are still caught by TCP keepalive on the
        # Tab5 side (10 s idle + 5 s × 3 probes ≈ 25 s).
        ws = web.WebSocketResponse(
            max_msg_size=10 * 1024 * 1024,
            heartbeat=180.0,
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

        # SOLID-audit follow-up: server-side keepalive task
        # extracted to ws_keepalive.run_ws_keepalive.  That module
        # owns: 15s JSON-pong loop (ngrok counts data frames),
        # 5s send-timeout per ping (US-DQ05 GIL-contention guard),
        # 3-consecutive-failure WS-close threshold (US-DQ20).
        # See module docstring for the 195 s dead-detection budget
        # tuned around #75 LLM-flap and Tab5's pingpong window.
        _keepalive_stop = asyncio.Event()
        _keepalive_task = asyncio.create_task(
            run_ws_keepalive(ws, ws_id=ws_id, stop_event=_keepalive_stop)
        )

        # Shared flag: last time ANY message was received from the
        # client.  Updated in the main message loop; left for
        # potential future silence-check use (the old 30s silence
        # check was removed when Tab5 migrated to
        # esp_websocket_client whose control frames don't surface
        # to the message loop).
        _last_client_msg_time = time.monotonic()

        # Connection state — populated after register.
        #
        # SOLID-audit follow-up (2026-05-03): converted from untyped
        # dict literal to typed ConnState dataclass.  All field
        # defaults that were previously inlined here now live on the
        # dataclass (pipeline=None, registered=False, mode="ask",
        # bg_tasks via default_factory=set, ...).  Existing
        # `state.get("key")` / `state["key"] = ...` patterns continue
        # to work via the dict-protocol shim on ConnState — no
        # downstream caller needs to change in this PR.
        conn_state: ConnState = ConnState(
            ws_id=ws_id,
            ws=ws,                # #177: route handlers (video_inject) need the live WS
            conn_lock=conn_lock,  # A06: stored so HTTP config handler can serialize
            config=conn_config,   # per-connection config (deep copy of server default)
        )
        self._active_connections[ws_id] = conn_state

        # SOLID-audit follow-up: pipeline callbacks bundled into
        # PipelineCallbacks class.  on_audio routes through
        # safe_send_bytes (v4·D audit P0 fix); on_event forwards
        # via safe_send_json AND persists api_usage events to the
        # DB events table for cost tracking.  Per-event conn_state
        # lookup so a `clear` cmd that swaps session_id is
        # reflected on the next persisted event.
        _pipeline_callbacks = PipelineCallbacks(
            ws,
            conn_state=conn_state,
            safe_send_bytes=self._safe_send_bytes,
            safe_send_json=self._safe_send_json,
            db=self._db,
            # W5-B: server-side daily-cap trigger.  Reads
            # daily_cap_cents on every api_usage; emits
            # cap_downgrade once per UTC day when exceeded.
            # `conn_config` here is the per-WS deep copy; the
            # billing dataclass is shared by reference (no
            # per-connection overrides for it today).
            billing_config=getattr(conn_config, "billing", None),
        )
        on_audio = _pipeline_callbacks.on_audio
        on_event = _pipeline_callbacks.on_event

        # Store callback refs for pipeline re-init (A04 memory monitor)
        conn_state["_on_audio"] = on_audio
        conn_state["_on_event"] = on_event

        try:
            async for msg in ws:
                _last_client_msg_time = time.monotonic()

                if msg.type == WSMsgType.BINARY:
                    # SOLID-audit follow-up: VID0 / AUD0 / raw PCM
                    # routing extracted to
                    # binary_frame_dispatch.dispatch_binary_frame.
                    # That module owns: VID0 → video relay, AUD0 →
                    # peer broadcast (sender excluded; closed peers
                    # skipped; per-peer send failures swallowed at
                    # DEBUG), raw PCM → pipeline.feed_audio.
                    await dispatch_binary_frame(
                        msg.data,
                        conn_state=conn_state,
                        active_connections=self._active_connections,
                    )

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
                        # W6-A (audit 2026-05-11): inline branch extracted to
                        # start_handler.handle_start_command.  Owns: mode +
                        # turn_id stash on conn_state, audio buffer clear,
                        # dictation segment buffer reset, log line.
                        await handle_start_command(ws_id, conn_state, cmd)

                    elif cmd_type == "segment":
                        pipeline = conn_state.get("pipeline")
                        if pipeline and conn_state.get("mode") == "dictate":
                            logger.info("Connection %s: segment marker", ws_id)
                            async with conn_lock:  # US-P10: serialize with text
                                await pipeline.process_segment()

                    elif cmd_type == "stop":
                        # SOLID-audit follow-up: stop chain extracted
                        # to stop_handler.handle_stop_command.  That
                        # module owns: pipeline.finish_dictation OR
                        # start_processing dispatch by mode, the
                        # >10-char dictation auto-note gate, the
                        # create_from_text failure swallow, the
                        # 200-char transcript truncation in the
                        # note_created frame, and the conn_lock
                        # serialise (US-P10) with concurrent text
                        # cmd handlers.
                        await handle_stop_command(
                            ws,
                            ws_id=ws_id,
                            conn_state=conn_state,
                            conn_lock=conn_lock,
                            notes_svc=self._notes_svc,
                        )

                    elif cmd_type == "clear":
                        # SOLID-audit follow-up: clear chain extracted
                        # to clear_handler.handle_clear_command.  That
                        # module owns: pipeline.clear_history,
                        # end_session + create_session DB swap (with
                        # the #56 single-dict-return contract pinned),
                        # conn_state.session_id update, and the
                        # session_start emit.
                        await handle_clear_command(
                            ws,
                            ws_id=ws_id,
                            conn_state=conn_state,
                            session_mgr=self._session_mgr,
                        )

                    elif cmd_type == "cancel":
                        # SOLID-audit follow-up: cancel chain extracted
                        # to cancel_handler.handle_cancel_command.  That
                        # module owns: in-flight handler-task cancel
                        # (text/media/config slots), pipeline.cancel
                        # (audit A1 / #137 — idempotent), deferred-widget
                        # discard (audit B1 / #165), and the cancel_ack
                        # emit with per-source breakdown.
                        await handle_cancel_command(
                            ws,
                            ws_id=ws_id,
                            conn_state=conn_state,
                            surface_mgr=self._surface_mgr,
                            safe_send_json=self._safe_send_json,
                        )

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
                        # SOLID-audit follow-up: widget_action chain
                        # extracted to widget_action_handler.handle_widget_action.
                        # That module owns: input field validation
                        # (session_id / card_id / event present),
                        # surface_mgr presence guard, dispatch via
                        # handle_action, and the failure-isolation
                        # try/except (a buggy skill must NOT tear
                        # down the WS read loop).
                        await handle_widget_action(
                            cmd=cmd,
                            conn_state=conn_state,
                            surface_mgr=self._surface_mgr,
                        )

                    elif cmd_type == "config_ack":
                        logger.debug("Connection %s: config_ack %s", ws_id, cmd.get("applied"))

                    elif cmd_type == "channel_reply":
                        # W7-F stub (TT #471 round-trip closure).  Handler
                        # extracted to channel_reply_handler.py for standalone
                        # unit testing (see tests/test_channel_reply_handler.py).
                        # That module owns: agent_log.record_call/_result with
                        # source="user_reply", stub platform_message_id minting,
                        # and the ACK send.  Real gateway forwarding (W7-F.2)
                        # will swap the stub for a real WS-RPC dispatch.
                        await handle_channel_reply(cmd, ws, ws_id, logger)

                    elif cmd_type == "ready_ack":
                        # #334: end-of-turn observability.  Tab5 emits this
                        # after the TTS playback ring drains and the orb
                        # transitions back to READY in voice.c, so Dragon
                        # can see that the turn actually completed on the
                        # device side (vs Dragon's old assumption that
                        # tts_end == done).  Pure observability: log +
                        # cheap, no state change.
                        logger.debug(
                            "Connection %s: ready_ack mode=%s",
                            ws_id, cmd.get("mode"),
                        )

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
            # Stop keepalive — set the event for a clean exit on
            # the next wake; cancel for the harder-edged interrupt
            # of the in-flight asyncio.sleep.  Both are wired so
            # the task can't outlive the handler.
            _keepalive_stop.set()
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
        # SOLID-audit follow-up: extracted to
        # stale_conn_eviction.evict_stale_connections_for_device.
        # The whole γ2-M5 device_evicted notify + pipeline shutdown +
        # session pause + active_conns cleanup chain lives there now,
        # backed by 11 unit tests pinning every failure-isolation
        # branch (notify failure, shutdown failure, missing pipeline,
        # missing on_event, multi-evict, etc.).
        await evict_stale_connections_for_device(
            active_connections=self._active_connections,
            session_mgr=self._session_mgr,
            device_id=device_id,
            new_ws_id=ws_id,
        )

        # SOLID-audit follow-up: device DB upsert + D2 collision guard
        # extracted to device_upsert.upsert_device_with_collision_guard.
        # Returns False when a hardware_id collision was caught (γ-arch
        # error already sent); short-circuit out.
        if not await upsert_device_with_collision_guard(
            ws,
            db=self._db,
            device_id=device_id,
            hardware_id=hardware_id,
            name=cmd.get("name", ""),
            firmware_ver=cmd.get("firmware_ver", ""),
            platform=cmd.get("platform", ""),
            capabilities=cmd.get("capabilities"),
            safe_send_json=self._safe_send_json,
        ):
            return

        # SOLID-audit follow-up: widget-capability init extracted
        # to widget_capabilities_init.init_widget_capabilities.
        # That module owns: pluck `capabilities.widgets`,
        # default-fallback shape (types/list/chart/prompt caps),
        # the deep-copy of the default constant so two
        # connections sharing the fallback can't mutate each
        # other's caps via the shared `types` list reference.
        caps = cmd.get("capabilities") or {}
        init_widget_capabilities(
            conn_state,
            capabilities=caps if isinstance(caps, dict) else None,
            device_id=device_id,
        )
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

        # SOLID-audit follow-up: Surface + Scheduler wiring extracted
        # to surface_register.register_surface_and_replay_scheduler.
        # Hook ordering (register before replay) is preserved + pinned
        # by a dedicated test.
        await register_surface_and_replay_scheduler(
            ws,
            surface_mgr=self._surface_mgr,
            scheduler_mgr=getattr(self, "_scheduler_mgr", None),
            session_id=session_id,
            device_id=device_id,
            widget_capabilities=conn_state.get("widget_capabilities"),
            safe_send_json=self._safe_send_json,
        )

        # SOLID-audit follow-up: tool-event callbacks (the three
        # async closures `_on_tool_call`, `_on_tool_result`,
        # `_on_tool_error`) extracted to ToolEventEmitter — a small
        # stateful class that captures the per-connection state
        # (ws, conn_state, session_id, safe_send_json, emit_legacy)
        # at construction and exposes the three callbacks as methods.
        # Pre-extract this was ~176 LOC of inline closures with all
        # the β-arch pair-emit + tracker bookkeeping + web_search
        # auto-widget logic intertwined; now it's 19 unit tests
        # pinning every branch.
        if self._tool_registry:
            tool_emitter = ToolEventEmitter(
                ws=ws,
                conn_state=conn_state,
                session_id=session_id,
                safe_send_json=self._safe_send_json,
                emit_legacy=bool(getattr(
                    conn_state.get("config"),
                    "progress_bus_emit_legacy",
                    True,
                )),
            )
            conn_state["on_tool_call"] = tool_emitter.on_tool_call
            conn_state["on_tool_result"] = tool_emitter.on_tool_result
            conn_state["on_tool_error"] = tool_emitter.on_tool_error

        # SOLID-audit follow-up: session_start emit + session_messages
        # replay extracted to session_handshake module.  Both run
        # BEFORE the slow pipeline init so Tab5 sees the session
        # confirmed even if Moonshine takes 2 s to load.
        if not await emit_session_start(
            ws,
            session_id=session_id,
            device_id=device_id,
            resumed=resumed,
            message_count=session.get("message_count", 0),
            ws_id=ws_id,
            conn_config=conn_config,
            conversation=self._conversation,
            voice_mode=conn_state.get("voice_mode", 0),
            safe_send_json=self._safe_send_json,
        ):
            return  # transport drop — Tab5 will reconnect

        logger.info(
            "Device %s registered on session %s (resumed=%s, ws_id=%s)",
            device_id, session_id, resumed, ws_id,
        )

        # Audit C8/K15 (2026-04-20): on resume, replay the tail of the
        # message history so Tab5 chat can rehydrate its local store.
        if resumed:
            await replay_session_message_tail(
                ws,
                session_id=session_id,
                ws_id=ws_id,
                message_store=self._message_store,
                safe_send_json=self._safe_send_json,
            )

        # SOLID-audit follow-up: pipeline construction +
        # local-defaults reset + initialize + failure-emit
        # extracted to pipeline_init.build_and_initialize_pipeline.
        # That module owns: cloud-config-leakage protection
        # (resets stt/tts/llm to local-tier when carrying over
        # from a prior session), VoicePipeline construction with
        # all per-connection deps (tool callbacks A2/#142,
        # surface_mgr B1/#165), and the structured
        # pipeline_init_failed FATAL emit on init failure.
        # Returns the live pipeline on success, None on failure.
        pipeline = await build_and_initialize_pipeline(
            ws,
            ws_id=ws_id,
            conn_config=conn_config,
            on_audio=on_audio,
            on_event=on_event,
            conversation=self._conversation,
            session_id=session_id,
            media_pipeline=self._media_pipeline,
            backend_pool=self._backend_pool,
            on_tool_call=conn_state.get("on_tool_call"),
            on_tool_result=conn_state.get("on_tool_result"),
            on_tool_error=conn_state.get("on_tool_error"),
            surface_mgr=self._surface_mgr,
            safe_send_json=self._safe_send_json,
        )
        if pipeline is None:
            return  # init failed; FATAL frame already emitted

        conn_state["pipeline"] = pipeline

        # SOLID-audit follow-up: register-time codec negotiation
        # (#173 / TinkerTab #262) extracted to
        # codec_negotiation.negotiate_uplink_codec_at_register.
        # That function picks the best mutual codec from
        # capabilities.audio_codec, applies it on the pipeline,
        # and sends config_update only when the result is non-PCM
        # (so legacy clients without the capability stay on PCM
        # with no extra round-trip).  Failure-isolated: codec
        # negotiation is a voice-quality optimisation, never
        # blocks register.
        await negotiate_uplink_codec_at_register(
            ws,
            pipeline=pipeline,
            capabilities=caps if isinstance(caps, dict) else None,
            device_id=device_id,
            safe_send_json=self._safe_send_json,
        )

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

        SOLID-audit follow-up: implementation extracted to
        handler_task_spawn.spawn_handler_task.  That module owns:
        the queue-vs-coalesce dispatch, conn_lock acquisition,
        the cancel/exception isolation in the spawned task, and
        the task naming.  Wrapper kept for backward compat with
        existing call sites; could be inlined in a follow-up.
        """
        await spawn_handler_task(
            conn_state, slot, coro_func, *args,
            conn_lock=conn_lock, coalesce=coalesce,
        )

    async def _handle_text(
        self, ws: web.WebSocketResponse, conn_state: dict, cmd: dict
    ) -> None:
        """Handle text input message — goes directly to conversation engine.

        SOLID-audit follow-up: precondition guard + per-turn
        tool-tracker reset + B1 turn-busy bracket extracted to
        text_turn_gate.invoke_with_text_turn_gate.  That module
        owns the gating; `_handle_text_body` owns the actual
        LLM/TTS work and is invoked as the body callable.
        """
        # W4-B (cross-stack audit 2026-05-11): Tab5 stamps a turn_id
        # on every text frame.  Store on conn_state so downstream
        # emits + log lines can echo it back, enabling cross-system
        # trace correlation with Tab5 obs events.
        turn_id = cmd.get("turn_id") or "-"
        conn_state["turn_id"] = turn_id
        logger.info(
            "Text turn enqueued (turn_id=%s, session=%s)",
            turn_id, conn_state.get("session_id", "-"),
        )
        await invoke_with_text_turn_gate(
            ws,
            conn_state=conn_state,
            cmd=cmd,
            conversation=self._conversation,
            surface_mgr=self._surface_mgr,
            body_fn=self._handle_text_body,
        )

    async def _handle_text_body(
        self, ws: web.WebSocketResponse, conn_state: dict, cmd: dict,
        text: str, session_id: str, content: str,
    ) -> None:
        """Body of _handle_text, wrapped by the B1 turn-gate bracket above."""

        # SOLID-audit follow-up: TinkerClaw bypass branch
        # extracted to tinkerclaw_text_path.handle_tinkerclaw_text_path.
        # That module owns: precondition gate (voice_mode 3 + live
        # ConvEngine LLM), session-key set, thinking indicator,
        # token streaming with #75 PING-during-inference keepalive,
        # γ2-M6 (#106) DragonError fast-fail, empty-response wrap
        # with W15-H09 apology fallback, llm_done, TC zero-cost
        # receipt, and rich media emit (dedup with local path).
        # Returns True when handled (we return); False when not in
        # TC mode (we fall through to the local ConvEngine path).
        conn_cfg = conn_state.get("config")
        if await handle_tinkerclaw_text_path(
            ws,
            conn_state=conn_state,
            conn_config=conn_cfg,
            text=text,
            session_id=session_id,
            conversation=self._conversation,
            media_pipeline=self._media_pipeline,
            ws_keepalive=self._ws_keepalive_during_inference,
            safe_send_json=self._safe_send_json,
            message_store=self._message_store,
        ):
            return

        try:
            # SOLID-audit follow-up: local-path token streaming +
            # mid-stream tool-marker stripping extracted to
            # local_text_stream.stream_local_text_with_tool_filter.
            # That module owns: rolling buffer, complete-block
            # strip, partial-marker hold-back, end-of-stream
            # final-strip, and the #75 Phase 1a
            # WS-keepalive-during-inference wrap.  Returns
            # (full_response, response_text) so downstream stages
            # (rich-media gate, empty-response wrap, TTS) get the
            # same shape they had pre-extract.
            full_response, response_text = await stream_local_text_with_tool_filter(
                ws,
                conversation=self._conversation,
                session_id=session_id,
                content=content,
                conn_state=conn_state,
                ws_keepalive=self._ws_keepalive_during_inference,
            )

            # SOLID-audit follow-up: same empty-response guard as
            # the TC path above, but with fallback_when_no_tools=None
            # to preserve the local-path semantics where empty +
            # no tools just falls through (the legacy "let it pass"
            # behaviour — Tab5 drops the empty bubble silently).
            response_text = await maybe_synthesize_empty_response_wrap(
                ws,
                response_text=response_text,
                tool_calls=conn_state.get("tool_calls_this_turn") or [],
                safe_send_json=self._safe_send_json,
                log_label="local",
                fallback_when_no_tools=None,
            )

            if not ws.closed:
                await ws.send_json({"type": "llm_done", "llm_ms": 0})

            # SOLID-audit follow-up: rich media detection extracted to
            # rich_media_emit.emit_rich_media_for_text_turn (dedup with
            # the TC bypass branch above).  Audit D6 ordering invariant
            # (text_update BEFORE media events) is preserved + pinned
            # by tests/test_rich_media_emit.py.
            if full_response:
                await emit_rich_media_for_text_turn(
                    ws,
                    response_text=response_text,
                    media_pipeline=self._media_pipeline,
                    session_id=session_id,
                    safe_send_json=self._safe_send_json,
                    log_label="local",
                )

            # SOLID-audit follow-up: text-path TTS synthesis +
            # streaming extracted to
            # text_path_tts.synthesize_and_stream_text_response.
            # That module owns: precondition guards (no pipeline,
            # whitespace-only, ws.closed, match_input mode),
            # mode-aware timeout budget (90s local / 30s cloud),
            # async resample, paced byte streaming, the F5 TTS
            # receipt, the audit-L3 zombie-Piper-kill on timeout,
            # and the always-tts_end invariant.
            await synthesize_and_stream_text_response(
                ws,
                pipeline=conn_state.get("pipeline"),
                response_text=response_text,
                response_mode=conn_state.get("response_mode", "always_speak"),
                conn_config=conn_cfg,
                safe_send_json=self._safe_send_json,
            )

            logger.info("Text response on session %s: %s", session_id, response_text[:80])

            # SOLID-audit follow-up: Phase 3 per-turn receipt
            # extracted to text_path_receipt.emit_text_path_llm_receipt.
            # Voice-path receipts are emitted from
            # pipeline._process_utterance; the text path reaches the
            # LLM via ConversationEngine directly and bypasses
            # pipeline entirely, so we emit here too.
            convo = conn_state.get("conversation") or self._conversation
            await emit_text_path_llm_receipt(
                ws,
                conversation=convo,
                safe_send_json=self._safe_send_json,
            )

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

        # SOLID-audit follow-up: per-connection rate-limit gate
        # extracted to
        # config_update_rate_limit.check_config_update_rate_limit.
        # That module owns: 0.5 s minimum interval enforcement,
        # γ-arch TRANSIENT/SESSION emit on block (audit C1 / #137
        # closure — pre-fix was silent debug log + return), and
        # the per-connection timestamp stash.  Returns False on
        # rate-limit; caller short-circuits.
        if not await check_config_update_rate_limit(
            ws,
            conn_state=conn_state,
            ws_id=ws_id,
            safe_send_json=self._safe_send_json,
        ):
            return

        # Three-tier voice mode: 0=local, 1=hybrid, 2=cloud, 3=tinkerclaw
        voice_mode = cmd.get("voice_mode")
        llm_model = cmd.get("llm_model")
        # Backward compat: old binary cloud_mode toggle
        cloud_mode = cmd.get("cloud_mode")
        if cloud_mode is not None and voice_mode is None:
            voice_mode = 2 if cloud_mode else 0

        # #173 / TinkerTab #262: client-driven codec switch.  Tab5 may
        # send config_update with audio_uplink_codec to swap mid-session
        # (e.g. from a Settings toggle).  Apply via pipeline; reply with
        # the codec actually applied so a fallback (opus -> pcm because
        # libopus missing) is observable on the client.  Extracted to
        # codec_negotiation.maybe_swap_uplink_codec in the SOLID-audit
        # follow-up (sister of vision_capability / cap_downgrade /
        # config_swap_guards).
        await maybe_swap_uplink_codec(
            ws,
            cmd=cmd,
            conn_state=conn_state,
            safe_send_json=self._safe_send_json,
        )

        if voice_mode is not None:
            # OCP-1 (audit 2026-05-03): convert raw int → VoiceMode enum
            # at the boundary so all branches below use semantic
            # predicates instead of magic-number comparisons.  Invalid
            # values fall through to LOCAL semantically so a malformed
            # WS frame can never wedge the server in an unknown state.
            vmode = VoiceMode.from_int(voice_mode)
            if vmode is None:
                logger.warning(
                    "config_update: unknown voice_mode=%s — treating as LOCAL",
                    voice_mode,
                )
                vmode = VoiceMode.LOCAL

            # SOLID-audit follow-up: backend selection extracted to
            # config_swap.select_backends_for_mode().  The function
            # owns the (vmode, llm_model) → (stt, tts, llm) mapping AND
            # the matching mutation of conn_config.llm (model id +
            # mode-aware system_prompt + max_tokens).  Behaviour is
            # bit-for-bit identical to the prior inline chain;
            # tests/test_config_swap.py pins all 12 branches.
            sel = select_backends_for_mode(vmode, conn_config, llm_model=llm_model)
            stt_be, tts_be, llm_be = sel.stt_backend, sel.tts_backend, sel.llm_backend

            logger.info("Connection %s: voice_mode=%d (%s) → stt=%s tts=%s llm=%s model=%s tokens=%d",
                        ws_id, int(vmode), vmode.name, stt_be, tts_be, llm_be,
                        conn_config.llm.openrouter_model if vmode.is_cloud() else "(local)",
                        conn_config.llm.max_tokens)

            # SOLID-audit follow-up: TC gateway / OR key / TC token
            # validation guards extracted to config_swap_guards.
            # Returns False iff any guard failed (in which case it
            # already sent the γ-arch error_event + revert frames);
            # caller's only obligation is to short-circuit out.
            if not await validate_config_swap_prereqs(
                ws,
                vmode=vmode,
                conn_config=conn_config,
                safe_send_json=self._safe_send_json,
            ):
                return

            # SOLID-audit follow-up: session DB persist + conn_config
            # finalize (backend names + cloud STT/TTS API key
            # propagation) extracted to config_finalize.{persist_session_config_to_db,
            # apply_swap_config_to_conn}.  Both run AFTER validation +
            # backend selection, BEFORE the actual swap.
            await persist_session_config_to_db(
                self._db,
                session_id=conn_state.get("session_id"),
                vmode=vmode,
                conn_config=conn_config,
                llm_backend=llm_be,
                llm_model_request=llm_model,
            )
            apply_swap_config_to_conn(
                conn_config,
                vmode=vmode,
                stt_backend=stt_be,
                tts_backend=tts_be,
                llm_backend=llm_be,
            )

            # SOLID-audit follow-up: pipeline swap + ConvEngine swap
            # extracted to backend_swap.swap_pipeline_and_conversation_backends.
            # Phase 1 (issue #91): conn_lock acquisition was previously here in
            # the inline body; now handled by `_spawn_handler_task` on the
            # outer task wrapper.  Same US-P01 serialization with stop/text
            # handlers is preserved.
            #
            # Returns False on pipeline.swap_backends failure (γ-arch
            # error_event + revert frames already sent); short-circuit out.
            # Returns True even if the ConvEngine swap fails (logged but
            # silent — pipeline swap is the user-visible path).
            if not await swap_pipeline_and_conversation_backends(
                ws,
                conn_state=conn_state,
                conn_config=conn_config,
                llm_be=llm_be,
                voice_mode=int(vmode),
                conversation=self._conversation,
                backend_pool=self._backend_pool,
                safe_send_json=self._safe_send_json,
            ):
                return

            # Update displayed names
            self._stt_name = stt_be
            self._tts_name = tts_be
            self._llm_name = llm_be

            # SOLID-audit follow-up: ACK build + active_model
            # resolution + fleet_summary inclusion all extracted to
            # config_update_ack.emit_config_update_ack.  Returns the
            # resolved active_model string so the vision_capability
            # emit downstream can reuse it for the substring-fallback
            # display name.
            active_model = await emit_config_update_ack(
                ws,
                vmode=vmode,
                conn_config=conn_config,
                backends=sel,
                conversation=self._conversation,
                safe_send_json=self._safe_send_json,
            )

            # v4·D Phase 4b vision capability advertisement.  Tab5's
            # camera screen renders a "VISION · <model> READY" chip based
            # on this.  Both this and the ACK builder above check
            # ws.closed internally, so they're safe to call
            # unconditionally — the ws.closed guard that used to wrap
            # both is gone post-extract.
            await emit_vision_capability(
                ws,
                conversation=self._conversation,
                vmode=vmode,
                conn_config=conn_config,
                active_model=active_model,
            )

            # v4·D Gauntlet G7-F: speak a short alert when the Tab5
            # auto-downgrades because the daily cap was hit.
            # Extracted to cap_downgrade.maybe_speak_cap_downgrade_alert
            # in the SOLID-audit follow-up — this and the vision-
            # capability emit above were the two cleanest "different
            # axis of change" sub-responsibilities to split out of
            # _handle_config_update.
            maybe_speak_cap_downgrade_alert(cmd, conn_state)

    async def _handle_user_media(self, ws, conn_state, cmd):
        """Handle image/audio uploaded by Tab5 for multimodal LLM analysis.

        #183 PR 3: route through ConversationEngine instead of bypassing it.
        The vision turn becomes a first-class conversation turn — it gets
        memory injection, tool-calling, and (most importantly) the
        multimodal user message is persisted so subsequent text turns can
        still see the image (cross-modal continuity).

        SOLID-audit follow-up: vision-turn body extracted to
        vision_turn.handle_vision_turn.  That module owns: media-id
        lookup, capability-driven vision check, per-turn tool
        tracker reset, ConvEngine streaming with the keepalive
        wrap, the #75 Phase 1b empty-reply wrap, and the always-
        llm_done invariant.
        """
        await handle_vision_turn(
            ws,
            cmd=cmd,
            conn_state=conn_state,
            conversation=self._conversation,
            media_store=self._media_store,
            ws_keepalive=self._ws_keepalive_during_inference,
            safe_send_json=self._safe_send_json,
        )

    async def _handle_disconnect(self, conn_state: dict) -> None:
        """Handle WebSocket disconnect: pause session, mark device offline.

        SOLID-audit follow-up: full cleanup chain extracted to
        disconnect_handler.handle_disconnect.  That module owns:
        bg_task cancel (W14-C06), handler_task cancel (Phase 1
        / #91), surface unregister (Phase 4g), session pause
        (NOT end — resumable on reconnect), multi-tab-safe
        device offline marking, and pipeline shutdown.
        """
        await _disconnect_chain(
            conn_state,
            active_connections=self._active_connections,
            session_mgr=self._session_mgr,
            db=self._db,
            surface_mgr=self._surface_mgr,
        )


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
