"""Agentic-modules init step for `run_startup`.

Wave 23 SOLID-audit follow-up — twenty-sixth sub-extract.
First slice from `dragon_voice/lifecycle/startup.py` (audit
SRP-6: `run_startup` mutates 21 attributes in a single 281-LOC
sequence).

This module owns the "agentic" init sub-step:
  * `MemoryService` (facts + documents + RAG with Ollama
    embeddings)
  * `ToolRegistry` + 8 core tools (web_search, datetime, the 3
    memory tools, calculator, unit_converter, weather,
    system_info, stock_ticker)

Pre-extract this 55-LOC chunk lived inline in `run_startup`,
mutating `server._memory_service` and `server._tool_registry`
in one big try/except.  Now lives in its own dedicated module.

## API

```python
await init_agentic_modules(server)
```

Mutates ``server._memory_service`` and ``server._tool_registry``
in place.  Both are set to ``None`` first so a partial-init
failure leaves the server in a known state (and the downstream
``init_surfaces_and_scheduler`` step's null-check works).

## Failure isolation preserved

The outer try/except wraps the whole agentic init — a missing
optional dep (e.g. no Ollama running for memory embeddings)
logs a WARNING but doesn't block boot.  The server starts in
"no agentic" mode: voice/text still work, just no tool-calling
or memory.

## Tier-1 tool list

The eight tools registered here are the "always available"
tier — the agentic tools that ship with Dragon and don't need
an external service.  Skill SDK or MCP-bridged tools register
later (see `init_skills_and_scheduler` and the MCP bridge in
`run_startup`).

Audit D8/K7 dedup (2026-04-20): TimerTool deliberately NOT
registered here — TimesenseTool (registered later, after
SurfaceManager) covers "set a timer" AND emits widget_live
progress.  Keeping both caused the LLM to pick TimerTool on
short phrases, making the widget reference flow unreachable.
TimerTool class file is retained for REST-only callers; it's
just not wired into the agentic loop.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def init_agentic_modules(server: Any) -> None:
    """Initialise MemoryService + ToolRegistry + 8 core tools.

    Mutates `server._memory_service` and `server._tool_registry`
    in place.  Both fields are set to None up front so a partial-
    init failure leaves the server in a known state (downstream
    init steps null-check both before using).

    The whole chain is wrapped in try/except — a missing optional
    dep (no Ollama for embeddings, etc.) logs at WARNING but
    doesn't block boot.  Voice/text paths still work in "no
    agentic" mode.
    """
    server._memory_service = None
    server._tool_registry = None
    try:
        # Memory service first — embeddings backend for facts +
        # documents + RAG.  Configured via VoiceConfig.memory.
        from dragon_voice.memory import MemoryService
        from dragon_voice.tools import ToolRegistry
        from dragon_voice.tools.datetime_tool import DateTimeTool
        from dragon_voice.tools.web_search import WebSearchTool

        server._memory_service = MemoryService(
            server._db,
            ollama_url=server._config.llm.ollama_url,
            # δ3 / D-docs (issue #118): wire the configured ingest
            # cap so a runaway document upload can't OOM the embed.
            max_document_bytes=server._config.memory.max_document_bytes,
        )
        await server._memory_service.initialize()

        # Tool registry + tier-1 tools that don't depend on
        # memory_service.
        server._tool_registry = ToolRegistry()
        server._tool_registry.register(WebSearchTool(
            searxng_url=getattr(server._config.tools, "searxng_url", "")
        ))
        server._tool_registry.register(DateTimeTool())

        # Memory-dependent tools.
        from dragon_voice.tools.memory_tools import (
            ForgetFactTool,
            RecallFactsTool,
            StoreFactTool,
        )
        server._tool_registry.register(StoreFactTool(server._memory_service))
        server._tool_registry.register(RecallFactsTool(server._memory_service))
        # v4·D Gauntlet G9: two-step confirm-gated forget_fact tool.
        server._tool_registry.register(ForgetFactTool(server._memory_service))

        # Tier-1 utility tools.  Audit D8/K7 dedup (2026-04-20):
        # TimerTool deliberately NOT here — TimesenseTool
        # (registered later in `init_surfaces_and_scheduler`) covers
        # "set a timer" AND emits widget_live progress.  Keeping
        # both caused the LLM to pick TimerTool on short phrases,
        # making the widget reference flow unreachable.  TimerTool
        # class file is retained for REST-only callers but not
        # wired into the agentic loop.
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
