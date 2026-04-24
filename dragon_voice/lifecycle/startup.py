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

import asyncio
import logging
from typing import Any

import aiohttp
from aiohttp import web

from dragon_voice.conversation import ConversationEngine
from dragon_voice.db import Database
from dragon_voice.lifecycle import monitors, purge
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
    server._session_mgr = SessionManager(server._db)
    await server._session_mgr.start()

    # Message store
    server._message_store = MessageStore(server._db)

    # Memory service (agentic: facts + documents + RAG) + core tools
    server._memory_service = None
    server._tool_registry = None
    try:
        from dragon_voice.memory import MemoryService
        from dragon_voice.tools import ToolRegistry
        from dragon_voice.tools.web_search import WebSearchTool
        from dragon_voice.tools.datetime_tool import DateTimeTool

        server._memory_service = MemoryService(
            server._db,
            ollama_url=server._config.llm.ollama_url,
        )
        await server._memory_service.initialize()

        server._tool_registry = ToolRegistry()
        server._tool_registry.register(WebSearchTool(
            searxng_url=getattr(server._config.tools, "searxng_url", "")
        ))
        server._tool_registry.register(DateTimeTool())

        # Memory tools need memory_service
        from dragon_voice.tools.memory_tools import (
            ForgetFactTool, RecallFactsTool, StoreFactTool,
        )
        server._tool_registry.register(StoreFactTool(server._memory_service))
        server._tool_registry.register(RecallFactsTool(server._memory_service))
        # v4·D Gauntlet G9: two-step confirm-gated forget_fact tool.
        server._tool_registry.register(ForgetFactTool(server._memory_service))

        # Tier 1 tools.  Audit D8/K7 dedup (2026-04-20): TimerTool is
        # no longer registered here — TimesenseTool (registered below
        # after SurfaceManager init) covers "set a timer" AND emits
        # widget_live progress.  Keeping both caused the LLM to pick
        # TimerTool on short phrases, making the widget reference flow
        # unreachable.  TimerTool class file is retained for REST-only
        # callers; it's just not wired into the agentic loop.
        from dragon_voice.tools.calculator_tool import CalculatorTool
        from dragon_voice.tools.stock_ticker_tool import StockTickerTool
        from dragon_voice.tools.system_tool import SystemInfoTool
        from dragon_voice.tools.unit_converter_tool import UnitConverterTool
        from dragon_voice.tools.weather_tool import WeatherTool

        server._tool_registry.register(WeatherTool())
        server._tool_registry.register(CalculatorTool())
        server._tool_registry.register(UnitConverterTool())
        server._tool_registry.register(SystemInfoTool())
        server._tool_registry.register(StockTickerTool())

        logger.info(
            "Agentic modules initialized (tools: %d, memory: ok)",
            len(server._tool_registry.list_tools()),
        )
    except Exception as e:
        logger.warning("Agentic modules not available: %s", e)

    # v4·D Phase 4g stability fix (audit P0 #1): SurfaceManager so Tab5
    # widget_action events have somewhere to land.
    from dragon_voice.surfaces import SurfaceManager
    server._surface_mgr = SurfaceManager()
    logger.info("SurfaceManager initialized")

    # Register widget-emitting tools AFTER surface_mgr exists.
    if server._tool_registry is not None:
        try:
            from dragon_voice.tools.timesense_tool import TimesenseTool
            server._tool_registry.register(TimesenseTool(server._surface_mgr))
            logger.info("TimesenseTool registered (widget emitter)")
            # Wave 12 skill SDK reference — declarative
            # ``surface.prompt(on_action=handler)`` style.
            from dragon_voice.tools.quick_poll_tool import QuickPollTool
            server._tool_registry.register(QuickPollTool(server._surface_mgr))
            logger.info("QuickPollTool registered (declarative widget skill)")
        except Exception as e:
            logger.warning("TimesenseTool registration failed: %s", e)

    # Conversation engine (shared LLM backend for text/API input)
    server._conversation = ConversationEngine(
        server._db, server._message_store, server._config.llm,
        tool_registry=server._tool_registry,
        memory_service=server._memory_service,
    )
    await server._conversation.initialize()

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
        tool_registry=server._tool_registry,
        memory_service=server._memory_service,
        media_store=server._media_store,
        media_url_signer=server._media_url_signer,
    )

    # Notes API routes
    try:
        from dragon_voice.notes.api import setup_routes as setup_notes_routes
        from dragon_voice.notes.db import NotesDB
        from dragon_voice.notes.service import NotesService

        notes_db = NotesDB()
        # Wave 14 W14-C05: NotesDB.initialize is async now.  NotesService
        # awaits it internally, so we don't call it here.
        notes_svc = NotesService(server._config, notes_db)
        await notes_svc.initialize()
        server._notes_svc = notes_svc
        setup_notes_routes(app, notes_svc)
        logger.info("Notes API routes registered")

        # Register NoteTool now that NotesService is available.
        if server._tool_registry and server._notes_svc:
            from dragon_voice.tools.note_tool import NoteTool
            server._tool_registry.register(NoteTool(server._notes_svc))
            logger.info("Note tool registered (notes service available)")
    except Exception as e:
        logger.warning("Notes API not available: %s", e)

    # MCP servers (from config)
    try:
        from dragon_voice.mcp.bridge import bridge_mcp_server
        mcp_servers = getattr(server._config, "mcp_servers", [])
        for mcp in mcp_servers:
            count = await bridge_mcp_server(
                server._tool_registry,
                name=mcp.get("name", "mcp"),
                url=mcp.get("url"),
                token=mcp.get("token"),
            )
            logger.info("MCP %s: %d tools bridged", mcp.get("name"), count)
    except Exception as e:
        logger.warning("MCP bridge not available: %s", e)

    # Run initial message purge + schedule periodic (US-DQ14)
    retention_days = server._config.database.message_retention_days
    if retention_days > 0:
        try:
            result = await server._db.purge_old_messages(days=retention_days)
            logger.info(
                "Startup purge complete: %d messages, %d events removed (retention=%d days)",
                result["messages"], result["events"], retention_days,
            )
        except Exception as e:
            logger.warning("Startup purge failed: %s", e)

        server._purge_task = asyncio.create_task(
            purge.periodic_purge_loop(server, retention_days)
        )

    # Media cleanup (hourly, removes expired uploads)
    server._media_cleanup_task = asyncio.create_task(purge.media_cleanup_loop(server))

    # Start periodic memory monitor (A04)
    server._memory_monitor_task = asyncio.create_task(monitors.memory_monitor_loop(server))
    rss = monitors.get_rss_mb()
    logger.info(
        "Memory monitor started (RSS=%.0f MB, warn=%d MB, crit=%d MB)",
        rss, server._mem_warn_mb, server._mem_crit_mb,
    )

    logger.info("Foundation modules initialized")
