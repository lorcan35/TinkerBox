"""Server boot sequence — ``run_startup(server, app)``.

Orchestrates the VoiceServer boot in a specific order:

1.  Shared aiohttp ``ClientSession`` for the dashboard proxy (DQ08).
2.  ``Database`` init + migrations.
3.  ``SessionManager`` + ``MessageStore``.
4.  ``MemoryService`` + ``ToolRegistry`` + core tools.  Each section is
    wrapped in try/except so a missing optional dep (e.g. no memory
    backend configured) logs a warning but doesn't block boot.
5.  ``SurfaceManager`` + widget-emitting tools.
6.  ``ConversationEngine`` (shared LLM backend for text/API input).
7.  REST API routes + Notes API routes + MCP bridge.
8.  Retention purge (initial) + periodic purge task.
9.  Media cleanup + memory monitor periodic tasks.

Takes ``server`` as a single handle because the sequence mutates ~20
attributes on it.  A follow-up could introduce a ``StartupContext``
dataclass if we ever need multiple boot profiles, but today this is
one callsite that always wants to wire the same graph.
"""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
from aiohttp import web

from dragon_voice.conversation import ConversationEngine
from dragon_voice.db import Database
from dragon_voice.messages import MessageStore
from dragon_voice.sessions import SessionManager

logger = logging.getLogger(__name__)


async def run_startup(server: Any, app: web.Application) -> None:
    """Initialize foundation modules on server start.

    Mutates ``server`` in place — assigns the DB, session manager,
    message store, memory service, tool registry, surface manager,
    conversation engine, and the three background task handles
    (``_purge_task``, ``_media_cleanup_task``, ``_memory_monitor_task``).
    """
    logger.info("Initializing foundation modules...")

    # Shared client session for dashboard proxy (DQ08)
    server._proxy_session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=30)
    )

    # Database
    server._db = Database()
    await server._db.initialize()

    # Session manager (with background cleanup)
    # δ2 / H6 (issue #116): pass long-window paused retention so the
    # cleanup loop can end sessions that motion-sensor wakeups keep
    # touching but no real conversation has used in N days.
    server._session_mgr = SessionManager(
        server._db,
        paused_retention_days=server._config.database.paused_session_retention_days,
    )
    await server._session_mgr.start()

    # Message store
    server._message_store = MessageStore(server._db)

    # SOLID-audit follow-up: agentic init (MemoryService +
    # ToolRegistry + 8 tier-1 tools) extracted to
    # lifecycle/agentic_init.py.  Mutates _memory_service and
    # _tool_registry in place; whole chain wrapped in try/except
    # so a missing optional dep (no Ollama for embeddings) logs
    # at WARNING but doesn't block boot.
    from dragon_voice.lifecycle.agentic_init import init_agentic_modules
    await init_agentic_modules(server)

    # SOLID-audit follow-up: SurfaceManager + SchedulerManager
    # + widget/scheduler-tool registration extracted to
    # lifecycle/surfaces_scheduler_init.py.  Mutates _surface_mgr,
    # _scheduler_mgr, _scheduler_store; registers TimesenseTool +
    # QuickPollTool + ScheduleReminderTool on the existing
    # _tool_registry.  Layered try/except — failure of any
    # sub-step doesn't take down the others.
    from dragon_voice.lifecycle.surfaces_scheduler_init import (
        init_surfaces_and_scheduler,
    )
    await init_surfaces_and_scheduler(server)

    # Conversation engine (shared LLM backend for text/API input).
    # #183 PR 3: pass media_store so multimodal user messages persist
    # via add_message(media_id=...) and hydrate back to OpenAI
    # multimodal format on context build, enabling cross-modal
    # continuity (photo turn -> text follow-up still sees the photo).
    server._conversation = ConversationEngine(
        server._db, server._message_store, server._config.llm,
        tool_registry=server._tool_registry,
        memory_service=server._memory_service,
        media_store=server._media_store,
    )
    await server._conversation.initialize()

    # #179: serve the minimal video-call web client at /call
    # (matches the WS port so opening the page on a phone "just
    # works" without configuring a separate dashboard host).
    import os as _os
    _static_dir = _os.path.join(_os.path.dirname(__file__), "..", "static")
    if _os.path.isdir(_static_dir):
        app.router.add_static("/static/", _os.path.realpath(_static_dir),
                              show_index=False)
        async def _call_redirect(_req):
            from aiohttp import web as _web
            raise _web.HTTPFound("/static/call.html")
        app.router.add_get("/call", _call_redirect)

    # REST API routes (modular package)
    from dragon_voice.api import setup_all_routes
    setup_all_routes(
        app,
        db=server._db,
        session_mgr=server._session_mgr,
        message_store=server._message_store,
        conversation=server._conversation,
        voice_config=server._config,
        start_time=server._start_time,
        get_active_connections=lambda: len(server._active_connections),
        get_active_conn_dict=lambda: server._active_connections,
        tool_registry=server._tool_registry,
        memory_service=server._memory_service,
        media_store=server._media_store,
        media_url_signer=server._media_url_signer,
        # Phase 5 ε1b: scheduler REST surface lights up when the
        # manager initialised cleanly.  ε1a's startup wiring sets
        # `_scheduler_mgr = None` if init failed so the guard in
        # setup_all_routes does the right thing.
        scheduler_mgr=getattr(server, "_scheduler_mgr", None),
        # W7-B.2: live skills.status polling needs the gateway
        # connector.  Wired below (W7-F.2 channel-gateway init flips
        # server._gateway_connector from None to the real connector).
        # `agent_skills.py` calls the getter at request time, so it
        # sees whatever connector is live at *that* moment — including
        # any future hot-swap.
        get_gateway_connector=lambda: getattr(server, "_gateway_connector", None),
        # W4-D: pass the server handle so /api/v1/coredumps can read
        # `_config.coredump_scraper.save_dir` + LAST_RESULTS for last
        # per-target scrape outcomes.
        server=server,
    )

    # SOLID-audit follow-up: notes module + tool registration
    # extracted to lifecycle/notes_init.py.
    from dragon_voice.lifecycle.notes_init import init_notes_module
    await init_notes_module(server, app)

    # #341: TinkerBox-native integration tools (Google Calendar
    # today, plus Gmail / Home Assistant / Spotify as they land).
    # Backend instances are created lazily by each tool so REST
    # connect routes and tool calls share state.
    from dragon_voice.lifecycle.integrations_init import (
        init_integration_tools,
    )
    await init_integration_tools(server)

    # SOLID-audit follow-up: MCP server bridges extracted to
    # lifecycle/mcp_init.py.
    from dragon_voice.lifecycle.mcp_init import init_mcp_bridges
    await init_mcp_bridges(server)

    # SOLID-audit follow-up: background-task scheduling
    # (purge + media cleanup + memory monitor) extracted to
    # lifecycle/background_tasks_init.py.  Synchronous (only
    # spawns tasks) so the boot sequence never blocks on the
    # initial purge.
    from dragon_voice.lifecycle.background_tasks_init import (
        init_background_tasks,
    )
    init_background_tasks(server)

    # W7-F.2: swap the boot-default MockConnector for a real WS-RPC
    # gateway connector when channel_gateway.enabled is set.  Lazy
    # connect — failure here is only logged so a downed gateway
    # doesn't block Dragon boot; send_reply() will retry per-call.
    _init_channel_gateway(server)

    logger.info("Foundation modules initialized")


def _init_channel_gateway(server: Any) -> None:
    """Wire the gateway connector if channel_gateway.enabled is True.

    Reads ``channel_gateway.token`` (falling back to
    ``llm.tinkerclaw_token`` since both point at the same loopback
    gateway process).  Failure to construct the connector — missing
    token, bad URL — logs at WARNING and leaves the default
    MockConnector in place so reply ACKs still succeed locally.
    """
    cg = getattr(server._config, "channel_gateway", None)
    if cg is None or not getattr(cg, "enabled", False):
        return
    token = (cg.token or "").strip() or (server._config.llm.tinkerclaw_token or "").strip()
    if not token:
        logger.warning(
            "channel_gateway.enabled=True but no token configured "
            "(channel_gateway.token / llm.tinkerclaw_token both empty) — "
            "staying on MockConnector",
        )
        return
    try:
        from dragon_voice.channel_reply_handler import set_connector
        from dragon_voice.channels import GatewayConnector
        connector = GatewayConnector(
            url=cg.url,
            token=token,
            client_id=cg.client_id,
        )
        set_connector(connector)
        server._gateway_connector = connector
        logger.info("W7-F.2: channel_reply connector swapped to GatewayConnector(url=%s)", cg.url)
    except Exception:
        logger.exception(
            "channel_gateway init failed — staying on MockConnector",
        )
