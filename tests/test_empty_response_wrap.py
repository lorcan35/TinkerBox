"""Tests for ``dragon_voice.empty_response_wrap.maybe_synthesize_empty_response_wrap``.

Pin every branch of the empty-response guard.

The two original call sites (TC bypass + local ConvEngine path)
diverged historically:

  * TC: passes `fallback_when_no_tools="Sorry, ..."` so the
    apology fires when no tools fired (W15-H09 legacy).
  * Local: passes `fallback_when_no_tools=None` so empty-with-
    no-tools just falls through.

These tests pin BOTH semantics so a future "let's unify them"
refactor doesn't silently change one branch.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from dragon_voice.empty_response_wrap import maybe_synthesize_empty_response_wrap


def _make_ws(*, closed: bool = False) -> MagicMock:
    ws = MagicMock()
    ws.closed = closed
    return ws


def _make_safe_send_json() -> AsyncMock:
    return AsyncMock(return_value=True)


# ─── Happy path: useful text passes through ──────────────────────


@pytest.mark.asyncio
async def test_useful_text_passes_through_unchanged():
    """Standard happy path: LLM produced useful prose; the guard
    is a no-op."""
    ws = _make_ws()
    send = _make_safe_send_json()

    out = await maybe_synthesize_empty_response_wrap(
        ws,
        response_text="Hello, the time is 12:30 PM.",
        tool_calls=[],
        safe_send_json=send,
        log_label="local",
    )

    assert out == "Hello, the time is 12:30 PM."
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_useful_text_with_tools_still_passes_through():
    """Even when tools fired, useful text means we keep the LLM's
    prose — the wrap only kicks in for empty/junk responses."""
    ws = _make_ws()
    send = _make_safe_send_json()

    out = await maybe_synthesize_empty_response_wrap(
        ws,
        response_text="Stored your fact: chair is magenta.",
        tool_calls=[{"tool": "remember", "args": {"fact": "chair is magenta"}}],
        safe_send_json=send,
        log_label="local",
    )

    assert out == "Stored your fact: chair is magenta."
    send.assert_not_awaited()


# ─── Empty + tools fired → synthesize wrap ───────────────────────


@pytest.mark.asyncio
async def test_empty_with_tools_synthesizes_wrap():
    """The xLAM / functiongemma case: tool fired, empty user-
    visible reply.  Synthesize a per-tool wrap and send it."""
    ws = _make_ws()
    send = _make_safe_send_json()

    out = await maybe_synthesize_empty_response_wrap(
        ws,
        response_text="",
        tool_calls=[
            {"tool": "remember", "args": {"fact": "magenta"}, "result": "stored"},
        ],
        safe_send_json=send,
        log_label="local",
    )

    # Returned a non-empty wrap string
    assert out != ""
    assert isinstance(out, str)
    # Sent via safe_send_json as an llm frame
    send.assert_awaited_once()
    payload = send.await_args.args[1]
    assert payload["type"] == "llm"
    assert payload["text"] == out


@pytest.mark.asyncio
async def test_bracket_noise_with_tools_treated_as_empty():
    """#75 phase 1b: response that's just bracket noise should
    trigger the wrap, not pass through as 'useful'."""
    ws = _make_ws()
    send = _make_safe_send_json()

    out = await maybe_synthesize_empty_response_wrap(
        ws,
        response_text="<>",  # bracket noise only
        tool_calls=[{"tool": "datetime", "args": {}, "result": "12:30"}],
        safe_send_json=send,
        log_label="local",
    )
    # Wrap fired
    assert out != "<>"
    send.assert_awaited_once()


# ─── Empty + no tools, no fallback → pass through (LOCAL semantics) ─


@pytest.mark.asyncio
async def test_empty_no_tools_no_fallback_passes_through():
    """LOCAL path semantics: empty text + no tools + no fallback
    → return unchanged.  Caller's llm_done emits empty text;
    Tab5 drops the bubble.  Pinned so a future "always send
    something" refactor doesn't silently flip this branch."""
    ws = _make_ws()
    send = _make_safe_send_json()

    out = await maybe_synthesize_empty_response_wrap(
        ws,
        response_text="",
        tool_calls=[],
        safe_send_json=send,
        log_label="local",
        fallback_when_no_tools=None,
    )

    assert out == ""
    send.assert_not_awaited()


# ─── Empty + no tools + fallback set → apology (TC semantics) ────


@pytest.mark.asyncio
async def test_empty_no_tools_with_fallback_sends_apology():
    """TC path semantics: empty + no tools + fallback_when_no_tools
    set → send the apology and return it."""
    ws = _make_ws()
    send = _make_safe_send_json()
    apology = "Sorry, I couldn't generate a response for that."

    out = await maybe_synthesize_empty_response_wrap(
        ws,
        response_text="",
        tool_calls=[],
        safe_send_json=send,
        log_label="tc",
        fallback_when_no_tools=apology,
    )

    assert out == apology
    send.assert_awaited_once()
    payload = send.await_args.args[1]
    assert payload["type"] == "llm"
    assert payload["text"] == apology


# ─── ws.closed handling ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_ws_closed_skips_send_but_still_returns_wrap():
    """If ws.closed is True, the wrap is still returned (so any
    downstream consumer like TTS / receipt sees the synthesised
    text) but the WS send is skipped."""
    ws = _make_ws(closed=True)
    send = _make_safe_send_json()

    out = await maybe_synthesize_empty_response_wrap(
        ws,
        response_text="",
        tool_calls=[{"tool": "remember", "args": {"fact": "x"}}],
        safe_send_json=send,
        log_label="local",
    )

    assert out != ""  # wrap returned
    send.assert_not_awaited()  # but no send


# ─── log_label provenance ───────────────────────────────────────


@pytest.mark.asyncio
async def test_log_label_distinguishes_tc_vs_local(caplog):
    """The log_label parameter discriminates TC vs local in logs.
    Caplog should see 'tc' when we pass log_label='tc'."""
    import logging
    caplog.set_level(logging.INFO, logger="dragon_voice.empty_response_wrap")
    ws = _make_ws()

    await maybe_synthesize_empty_response_wrap(
        ws,
        response_text="",
        tool_calls=[{"tool": "datetime", "args": {}, "result": "12:30"}],
        safe_send_json=_make_safe_send_json(),
        log_label="tc",
    )

    matched = [r for r in caplog.records if "tc text path" in r.getMessage().lower()]
    assert matched, f"no log lines mentioned 'tc text path': {[r.getMessage() for r in caplog.records]}"


@pytest.mark.asyncio
async def test_w15_h09_log_warning_on_no_tools_fallback(caplog):
    """The W15-H09 fallback path logs at WARNING, not INFO —
    pinned so an ops watcher can still distinguish 'wrap from
    tools' (informational) from 'fallback apology' (warning)."""
    import logging
    caplog.set_level(logging.WARNING, logger="dragon_voice.empty_response_wrap")
    ws = _make_ws()

    await maybe_synthesize_empty_response_wrap(
        ws,
        response_text="",
        tool_calls=[],
        safe_send_json=_make_safe_send_json(),
        log_label="tc",
        fallback_when_no_tools="Sorry, ...",
    )

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "expected at least one WARNING log line"
    assert any("W15-H09" in r.getMessage() for r in warnings)
