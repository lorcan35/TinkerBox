"""Tests for ``dragon_voice.vision_capability.emit_vision_capability``.

Pre-extract the vision-capability advertisement was 80 LOC inline in
``server.py:_handle_config_update``.  Now that it's a free function
with explicit inputs we can pin behaviour with unit tests instead of
relying on the e2e harness.

Six branches we care about:

  1. **Router-active + cloud router pick** → router wins, mils > 0.
  2. **Router-active + local router pick** → router wins, mils = 0.
  3. **Router-active + no router pick** → falls through to substring
     gate; cloud substring matches → mils > 0.
  4. **No router + cloud substring miss** → can_see = False, mils = 0.
  5. **No router + local Ollama vision substring** → can_see = True,
     mils = 0.
  6. **send_json raises** → silently logged, no exception out.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from dragon_voice.vision_capability import emit_vision_capability
from dragon_voice.voice_modes import VoiceMode


def _make_conn_config(*, openrouter_model: str = "", ollama_model: str = "") -> MagicMock:
    """Minimal VoiceConfig stub — only the fields the emitter reads."""
    cfg = MagicMock()
    cfg.llm.openrouter_model = openrouter_model
    cfg.llm.ollama_model = ollama_model
    return cfg


def _make_ws() -> MagicMock:
    ws = MagicMock()
    ws.send_json = AsyncMock()
    return ws


def _make_router_conversation(spec):
    """Build a ConversationEngine stub whose `choose_vision_model`
    returns ``spec`` (or None)."""
    conv = MagicMock()
    conv.choose_vision_model = MagicMock(return_value=spec)
    return conv


def _spec(model_id: str, tier: str):
    """Minimal ModelSpec stub."""
    s = MagicMock()
    s.model_id = model_id
    s.tier = tier
    return s


# ─── Router branches ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_router_picks_cloud_vision_model_emits_priced_chip():
    ws = _make_ws()
    conv = _make_router_conversation(_spec("anthropic/claude-sonnet-4.6", "cloud"))

    await emit_vision_capability(
        ws,
        conversation=conv,
        vmode=VoiceMode.CLOUD,
        conn_config=_make_conn_config(openrouter_model="anthropic/claude-sonnet-4.6"),
        active_model="anthropic/claude-sonnet-4.6",
    )

    ws.send_json.assert_awaited_once()
    payload = ws.send_json.await_args.args[0]
    assert payload["type"] == "vision_capability"
    assert payload["can_see"] is True
    assert payload["model"] == "anthropic/claude-sonnet-4.6"
    assert payload["per_frame_mils"] > 0  # sonnet cloud → real mils
    # Router's choose was asked for VoiceMode.CLOUD as int(2)
    conv.choose_vision_model.assert_called_once_with(2)


@pytest.mark.asyncio
async def test_router_picks_local_vision_model_emits_zero_mils():
    """Local-tier sub-backends short-circuit to per_frame=0 regardless
    of what the canonical pricing table would say (which is OR-only)."""
    ws = _make_ws()
    conv = _make_router_conversation(
        _spec("hf.co/openbmb/MiniCPM-V-4-gguf:Q4_K_M", "local"),
    )

    await emit_vision_capability(
        ws,
        conversation=conv,
        vmode=VoiceMode.LOCAL,
        conn_config=_make_conn_config(),
        active_model="ministral-3:3b",
    )

    payload = ws.send_json.await_args.args[0]
    assert payload["can_see"] is True
    assert payload["model"] == "hf.co/openbmb/MiniCPM-V-4-gguf:Q4_K_M"
    assert payload["per_frame_mils"] == 0


# ─── Single-backend (non-router) substring fallback ───────────────


@pytest.mark.asyncio
async def test_no_router_cloud_substring_match_emits_priced_chip():
    """When ConversationEngine is non-router AND configured cloud model
    matches a known vision-vendor substring, advertise it."""
    ws = _make_ws()
    conv = _make_router_conversation(spec=None)  # router has no pick

    await emit_vision_capability(
        ws,
        conversation=conv,
        vmode=VoiceMode.CLOUD,
        conn_config=_make_conn_config(openrouter_model="anthropic/claude-opus-4.7"),
        active_model="anthropic/claude-opus-4.7",
    )

    payload = ws.send_json.await_args.args[0]
    assert payload["can_see"] is True
    assert payload["model"] == "anthropic/claude-opus-4.7"
    assert payload["per_frame_mils"] > 0  # OCP-2 fix: opus is now priced


@pytest.mark.asyncio
async def test_no_router_cloud_substring_miss_emits_no_capability():
    """An OpenRouter model that's not in the vision-vendor substring
    list (e.g. plain DeepSeek text-only) advertises ``can_see=False``."""
    ws = _make_ws()
    conv = _make_router_conversation(spec=None)

    await emit_vision_capability(
        ws,
        conversation=conv,
        vmode=VoiceMode.CLOUD,
        conn_config=_make_conn_config(openrouter_model="deepseek/deepseek-v4-flash"),
        active_model="deepseek/deepseek-v4-flash",
    )

    payload = ws.send_json.await_args.args[0]
    assert payload["can_see"] is False
    assert payload["model"] == ""
    assert payload["per_frame_mils"] == 0


@pytest.mark.asyncio
async def test_no_router_local_ollama_vision_substring_match():
    """Ollama models with 'vision' or 'llava' in the name advertise
    a free-tier vision capability."""
    ws = _make_ws()
    conv = _make_router_conversation(spec=None)

    await emit_vision_capability(
        ws,
        conversation=conv,
        vmode=VoiceMode.LOCAL,
        conn_config=_make_conn_config(ollama_model="llava:7b"),
        active_model="llava:7b",
    )

    payload = ws.send_json.await_args.args[0]
    assert payload["can_see"] is True
    assert payload["model"] == "llava:7b"
    assert payload["per_frame_mils"] == 0


@pytest.mark.asyncio
async def test_conversation_none_no_router_call():
    """During boot the engine may not be wired yet — must not crash."""
    ws = _make_ws()
    await emit_vision_capability(
        ws,
        conversation=None,
        vmode=VoiceMode.CLOUD,
        conn_config=_make_conn_config(openrouter_model="anthropic/claude-haiku-4.5"),
        active_model="anthropic/claude-haiku-4.5",
    )
    payload = ws.send_json.await_args.args[0]
    # Falls through to substring gate; haiku is in the cloud vision list.
    assert payload["can_see"] is True


# ─── Failure-isolation contract ───────────────────────────────────


@pytest.mark.asyncio
async def test_send_failure_swallowed():
    """If `ws.send_json` raises, the emitter logs + returns cleanly so
    a render failure on the chip doesn't tear down the config_update
    flow."""
    ws = _make_ws()
    ws.send_json = AsyncMock(side_effect=ConnectionResetError("client gone"))
    conv = _make_router_conversation(spec=None)

    # Must NOT raise.
    await emit_vision_capability(
        ws,
        conversation=conv,
        vmode=VoiceMode.LOCAL,
        conn_config=_make_conn_config(),
        active_model="ministral-3:3b",
    )
    ws.send_json.assert_awaited_once()
