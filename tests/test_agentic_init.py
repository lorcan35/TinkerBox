"""Tests for ``dragon_voice.lifecycle.agentic_init``.

Pin the success path (8 tools registered) + the failure path
(missing optional dep doesn't block boot).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dragon_voice.lifecycle.agentic_init import init_agentic_modules


def _make_server() -> SimpleNamespace:
    """A minimal server stub with the fields agentic_init reads."""
    return SimpleNamespace(
        _db=MagicMock(),
        _config=SimpleNamespace(
            llm=SimpleNamespace(ollama_url="http://127.0.0.1:11434"),
            memory=SimpleNamespace(max_document_bytes=1_000_000),
            tools=SimpleNamespace(searxng_url="http://127.0.0.1:8888"),
        ),
        _memory_service=None,
        _tool_registry=None,
    )


# ─── Success path ────────────────────────────────────────────


class TestSuccessPath:
    @pytest.mark.asyncio
    async def test_initializes_memory_service_and_registers_8_core_tools(self):
        """Pin: post-init, server._tool_registry has all 8 tier-1
        tools and server._memory_service is the live MemoryService."""
        server = _make_server()

        # Patch MemoryService.initialize so we don't need a live
        # Ollama server in the test.
        with patch(
            "dragon_voice.memory.MemoryService"
        ) as MemSvc:
            mem_inst = MagicMock()
            mem_inst.initialize = AsyncMock()
            MemSvc.return_value = mem_inst

            await init_agentic_modules(server)

        assert server._memory_service is mem_inst
        assert server._tool_registry is not None
        # Pin the 8 tier-1 tools
        names = {t["name"] for t in server._tool_registry.list_tools()}
        # 10 tier-1 tools (UnitConverterTool registers as "convert")
        expected = {
            "web_search", "datetime",
            "remember", "recall", "forget_fact",
            "weather", "calculator", "convert",
            "system_info", "stock_ticker",
        }
        assert expected <= names

    @pytest.mark.asyncio
    async def test_timer_tool_NOT_registered_in_agentic_init(self):
        """Audit D8/K7 dedup pin: TimerTool deliberately not
        registered here — TimesenseTool (registered later in
        init_surfaces_and_scheduler) covers it.  Keeping both
        caused the LLM to pick TimerTool on short phrases,
        making the widget reference flow unreachable."""
        server = _make_server()

        with patch(
            "dragon_voice.memory.MemoryService"
        ) as MemSvc:
            MemSvc.return_value.initialize = AsyncMock()

            await init_agentic_modules(server)

        names = {t["name"] for t in server._tool_registry.list_tools()}
        assert "timer" not in names

    @pytest.mark.asyncio
    async def test_searxng_url_passed_to_websearch_tool(self):
        """Pin: WebSearchTool gets the configured searxng_url so
        the local SearXNG instance is honoured (vs falling back
        to DDG)."""
        server = _make_server()
        server._config.tools.searxng_url = "http://my-searxng:8888"

        with patch(
            "dragon_voice.memory.MemoryService"
        ) as MemSvc, patch(
            "dragon_voice.tools.web_search.WebSearchTool"
        ) as WebSearch:
            MemSvc.return_value.initialize = AsyncMock()
            WebSearch.return_value = MagicMock(name="ws")
            WebSearch.return_value.name = "web_search"

            await init_agentic_modules(server)

        # WebSearchTool constructed with the configured URL
        WebSearch.assert_called_once_with(searxng_url="http://my-searxng:8888")

    @pytest.mark.asyncio
    async def test_max_document_bytes_passed_to_memory(self):
        server = _make_server()
        server._config.memory.max_document_bytes = 5_000_000

        with patch(
            "dragon_voice.memory.MemoryService"
        ) as MemSvc:
            MemSvc.return_value.initialize = AsyncMock()

            await init_agentic_modules(server)

        # MemoryService constructed with the configured cap
        MemSvc.assert_called_once_with(
            server._db,
            ollama_url="http://127.0.0.1:11434",
            max_document_bytes=5_000_000,
        )


# ─── Failure isolation ──────────────────────────────────────


class TestFailureIsolation:
    @pytest.mark.asyncio
    async def test_memory_init_failure_does_not_propagate(self):
        """Pin the audit invariant: a missing optional dep (no
        Ollama for embeddings, etc.) MUST log at WARNING but NOT
        block boot.  The server starts in 'no agentic' mode."""
        server = _make_server()

        with patch(
            "dragon_voice.memory.MemoryService"
        ) as MemSvc:
            MemSvc.side_effect = RuntimeError("Ollama not running")
            # Must NOT raise.
            await init_agentic_modules(server)

        # Both fields left as None per the pre-extract null-check
        # contract that downstream init steps rely on.
        assert server._memory_service is None
        assert server._tool_registry is None

    @pytest.mark.asyncio
    async def test_partial_failure_after_some_tools_registered(self):
        """If memory_init succeeds but a tool registration fails
        partway through, the agentic chain still bails — there's
        no point keeping a half-registered tool set."""
        server = _make_server()

        with patch(
            "dragon_voice.memory.MemoryService"
        ) as MemSvc, patch(
            "dragon_voice.tools.calculator_tool.CalculatorTool"
        ) as Calc:
            MemSvc.return_value.initialize = AsyncMock()
            Calc.side_effect = ImportError("calc dep missing")

            # Must NOT raise.
            await init_agentic_modules(server)

        # tool_registry started but threw mid-build — left in
        # whatever state the partial init produced.  The audit
        # invariant cares about the boot not crashing, not about
        # any specific intermediate state here.

    @pytest.mark.asyncio
    async def test_no_tools_config_field_uses_empty_searxng(self):
        """Pin getattr fallback: server._config.tools missing the
        searxng_url attr → empty string passed to WebSearchTool
        (DDG fallback)."""
        server = _make_server()
        # Simulate config without the .tools.searxng_url attribute
        server._config.tools = SimpleNamespace()  # no searxng_url

        with patch(
            "dragon_voice.memory.MemoryService"
        ) as MemSvc, patch(
            "dragon_voice.tools.web_search.WebSearchTool"
        ) as WebSearch:
            MemSvc.return_value.initialize = AsyncMock()
            WebSearch.return_value = MagicMock(name="ws")
            WebSearch.return_value.name = "web_search"

            await init_agentic_modules(server)

        WebSearch.assert_called_once_with(searxng_url="")
