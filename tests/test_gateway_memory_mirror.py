"""Tests for ``dragon_voice.gateway_memory_mirror`` (W7-C).

Validates that gateway-emitted ``remember`` tool calls are mirrored into
Dragon's local memory_service with ``source="gateway"``, and that all
other inputs / failure modes are silently skipped so the LLM stream is
never torn down by a mirror hiccup.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from dragon_voice.gateway_memory_mirror import mirror_gateway_tool_call


@pytest.fixture
def memory_service() -> MagicMock:
    """A minimal mock memory service whose store_fact is awaitable."""
    svc = MagicMock()
    svc.store_fact = AsyncMock(return_value={"id": "abc123"})
    return svc


# ── happy path ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_remember_with_fact_mirrors_to_memory_service(memory_service):
    result = await mirror_gateway_tool_call(
        "remember",
        {"fact": "User prefers dark mode"},
        memory_service,
        session_id="sess-1",
    )
    assert result is True
    memory_service.store_fact.assert_awaited_once_with(
        "User prefers dark mode", source="gateway", session_id="sess-1",
    )


@pytest.mark.asyncio
async def test_remember_accepts_content_alias(memory_service):
    """Some FC-trained models emit `content` instead of `fact`."""
    result = await mirror_gateway_tool_call(
        "remember",
        {"content": "User is allergic to peanuts"},
        memory_service,
    )
    assert result is True
    memory_service.store_fact.assert_awaited_once()
    args, kwargs = memory_service.store_fact.call_args
    assert args[0] == "User is allergic to peanuts"
    assert kwargs["source"] == "gateway"


@pytest.mark.asyncio
async def test_remember_accepts_text_alias(memory_service):
    result = await mirror_gateway_tool_call(
        "remember",
        {"text": "Stand-up is at 9 AM"},
        memory_service,
    )
    assert result is True
    args, _ = memory_service.store_fact.call_args
    assert args[0] == "Stand-up is at 9 AM"


@pytest.mark.asyncio
async def test_empty_session_id_passes_none(memory_service):
    """Empty string session_id should become None — store_fact accepts
    both but None is the canonical 'no session' signal."""
    await mirror_gateway_tool_call(
        "remember", {"fact": "X"}, memory_service, session_id="",
    )
    _, kwargs = memory_service.store_fact.call_args
    assert kwargs["session_id"] is None


# ── skip cases ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recall_not_mirrored(memory_service):
    """recall stays gateway-local — module docstring rationale."""
    result = await mirror_gateway_tool_call(
        "recall", {"query": "what does the user like?"}, memory_service,
    )
    assert result is False
    memory_service.store_fact.assert_not_called()


@pytest.mark.asyncio
async def test_other_tool_not_mirrored(memory_service):
    """web_search, datetime, etc. — only `remember` mirrors."""
    for tool in ("web_search", "datetime", "calculator", "browser"):
        result = await mirror_gateway_tool_call(
            tool, {"query": "x"}, memory_service,
        )
        assert result is False, f"{tool} should not mirror"
    memory_service.store_fact.assert_not_called()


@pytest.mark.asyncio
async def test_remember_with_no_fact_skips(memory_service):
    result = await mirror_gateway_tool_call(
        "remember", {}, memory_service,
    )
    assert result is False
    memory_service.store_fact.assert_not_called()


@pytest.mark.asyncio
async def test_remember_with_empty_fact_skips(memory_service):
    result = await mirror_gateway_tool_call(
        "remember", {"fact": "   "}, memory_service,
    )
    assert result is False
    memory_service.store_fact.assert_not_called()


@pytest.mark.asyncio
async def test_remember_with_nondict_args_skips(memory_service):
    """Streaming JSON parse may yield strings or lists when the model
    emits malformed tool_calls.  Don't crash; just skip."""
    for bad_args in ("not a dict", ["fact", "value"], None, 42):
        result = await mirror_gateway_tool_call(
            "remember", bad_args, memory_service,
        )
        assert result is False
    memory_service.store_fact.assert_not_called()


@pytest.mark.asyncio
async def test_no_memory_service_skips_silently():
    """Pre-init path — memory service can legitimately be None."""
    result = await mirror_gateway_tool_call(
        "remember", {"fact": "X"}, None,
    )
    assert result is False  # no service = no mirror


# ── failure isolation ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_memory_service_exception_swallowed():
    """store_fact failures (sqlite locked, embedding backend down, etc.)
    must NEVER propagate — they'd tear down the LLM stream."""
    svc = MagicMock()
    svc.store_fact = AsyncMock(
        side_effect=RuntimeError("embedding backend offline"),
    )
    # Should not raise.
    result = await mirror_gateway_tool_call(
        "remember", {"fact": "X"}, svc,
    )
    assert result is False  # error path returns False
    svc.store_fact.assert_awaited_once()  # but we did try


@pytest.mark.asyncio
async def test_multiple_remember_calls_all_mirrored(memory_service):
    """Per-call independence — N remember calls land N facts."""
    facts = ["fact 1", "fact 2", "fact 3"]
    for f in facts:
        await mirror_gateway_tool_call(
            "remember", {"fact": f}, memory_service,
        )
    assert memory_service.store_fact.await_count == 3
    actual_facts = [
        call.args[0] for call in memory_service.store_fact.call_args_list
    ]
    assert actual_facts == facts
