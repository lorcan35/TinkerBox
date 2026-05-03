"""Tests for ``dragon_voice.backend_swap.swap_pipeline_and_conversation_backends``.

Pin the eight branches:

  1. No pipeline (boot race) → True; ConvEngine swap still runs.
  2. Happy path (pipeline + ConvEngine present) → True; both swap.
  3. TinkerClaw with SupportsSessionKey → set_session_key called
     with the conn's session_id.
  4. TinkerClaw with non-SupportsSessionKey LLM → no set_session_key
     attempt (no AttributeError leak).
  5. Pipeline raises DragonError → False; structured error_event +
     revert sent.
  6. Pipeline raises generic Exception → False; γ-arch generic
     error_event ("backend_swap_failed") + revert.
  7. ConvEngine swap raises → True (silent — failure logged + swallowed).
  8. ConversationEngine None (boot) → True; only pipeline swap runs.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from dragon_voice.backend_swap import swap_pipeline_and_conversation_backends
from dragon_voice.errors import DragonError, Scope, Severity


def _make_ws(*, closed: bool = False) -> MagicMock:
    ws = MagicMock()
    ws.closed = closed
    return ws


def _make_safe_send_json() -> AsyncMock:
    return AsyncMock(return_value=True)


def _make_pipeline_with_llm(llm) -> MagicMock:
    """Build a Pipeline stub whose `_llm` is the given object."""
    p = MagicMock()
    p.swap_backends = AsyncMock()
    p._llm = llm
    return p


def _make_conn_state(*, pipeline=None, session_id="sess-X") -> dict:
    return {
        "pipeline": pipeline,
        "session_id": session_id,
        "ws_id": "ws-test",
    }


def _make_conn_config() -> MagicMock:
    cfg = MagicMock()
    return cfg


def _make_conversation_with_swap_llm(*, raises=None) -> MagicMock:
    conv = MagicMock()
    if raises is not None:
        conv.swap_llm = AsyncMock(side_effect=raises)
    else:
        conv.swap_llm = AsyncMock(return_value=(MagicMock(), True))
    return conv


# ── 1. No pipeline (boot race) ───────────────────────────────────


@pytest.mark.asyncio
async def test_no_pipeline_skips_pipeline_swap_runs_conv_swap():
    ws = _make_ws()
    conn = _make_conn_state(pipeline=None)
    cfg = _make_conn_config()
    conv = _make_conversation_with_swap_llm()
    send = _make_safe_send_json()

    out = await swap_pipeline_and_conversation_backends(
        ws,
        conn_state=conn,
        conn_config=cfg,
        llm_be="ollama",
        voice_mode=0,
        conversation=conv,
        backend_pool={},
        safe_send_json=send,
    )

    assert out is True
    conv.swap_llm.assert_awaited_once()
    send.assert_not_awaited()  # no error, no revert


# ── 2. Happy path ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_happy_path_swaps_both_returns_true():
    ws = _make_ws()
    pipeline = _make_pipeline_with_llm(MagicMock())
    conn = _make_conn_state(pipeline=pipeline)
    cfg = _make_conn_config()
    conv = _make_conversation_with_swap_llm()
    send = _make_safe_send_json()

    out = await swap_pipeline_and_conversation_backends(
        ws,
        conn_state=conn,
        conn_config=cfg,
        llm_be="ollama",
        voice_mode=0,
        conversation=conv,
        backend_pool={},
        safe_send_json=send,
    )

    assert out is True
    pipeline.swap_backends.assert_awaited_once_with(cfg)
    conv.swap_llm.assert_awaited_once()
    send.assert_not_awaited()


# ── 3-4. TinkerClaw session-key injection ────────────────────────


@pytest.mark.asyncio
async def test_tinkerclaw_with_supports_session_key_calls_set_session_key():
    """When the post-swap pipeline LLM implements SupportsSessionKey
    AND we're in TC mode, the session_id from conn_state is injected."""
    from dragon_voice.llm.base import SupportsSessionKey

    # Build a fake LLM that implements SupportsSessionKey
    class _SessionKeyLLM:
        def __init__(self):
            self.session_keys: list[str] = []

        def set_session_key(self, k: str) -> None:
            self.session_keys.append(k)

    fake_llm = _SessionKeyLLM()
    # Sanity: it really IS a SupportsSessionKey
    assert isinstance(fake_llm, SupportsSessionKey)

    ws = _make_ws()
    pipeline = _make_pipeline_with_llm(fake_llm)
    conn = _make_conn_state(pipeline=pipeline, session_id="sess-XYZ")
    cfg = _make_conn_config()
    conv = _make_conversation_with_swap_llm()

    out = await swap_pipeline_and_conversation_backends(
        ws,
        conn_state=conn,
        conn_config=cfg,
        llm_be="tinkerclaw",
        voice_mode=3,
        conversation=conv,
        backend_pool={},
        safe_send_json=_make_safe_send_json(),
    )

    assert out is True
    assert fake_llm.session_keys == ["sess-XYZ"]


@pytest.mark.asyncio
async def test_tinkerclaw_with_non_supporting_llm_skips_session_key():
    """If the post-swap LLM doesn't implement SupportsSessionKey,
    the isinstance() guard must skip the .set_session_key(...) call.

    Note: assertion is "function returns True without crashing" —
    on a Mock with `spec=[]`, accessing `.set_session_key` raises
    AttributeError, which would surface as a test failure here
    if the production guard didn't actually skip.
    """
    fake_llm = MagicMock(spec=[])  # empty spec — no methods at all
    ws = _make_ws()
    pipeline = _make_pipeline_with_llm(fake_llm)
    conn = _make_conn_state(pipeline=pipeline)
    cfg = _make_conn_config()

    out = await swap_pipeline_and_conversation_backends(
        ws,
        conn_state=conn,
        conn_config=cfg,
        llm_be="tinkerclaw",
        voice_mode=3,
        conversation=_make_conversation_with_swap_llm(),
        backend_pool={},
        safe_send_json=_make_safe_send_json(),
    )

    assert out is True  # absence-of-AttributeError IS the assertion


# ── 5. Pipeline raises DragonError → False, structured error ────


@pytest.mark.asyncio
async def test_pipeline_dragon_error_emits_structured_event_and_returns_false():
    ws = _make_ws()
    pipeline = _make_pipeline_with_llm(MagicMock())
    de = DragonError(
        code="custom_swap_failure",
        message="Specific structured message",
        severity=Severity.FATAL,
        scope=Scope.LLM,
    )
    pipeline.swap_backends = AsyncMock(side_effect=de)
    conn = _make_conn_state(pipeline=pipeline)
    cfg = _make_conn_config()
    send = _make_safe_send_json()

    out = await swap_pipeline_and_conversation_backends(
        ws,
        conn_state=conn,
        conn_config=cfg,
        llm_be="openrouter",
        voice_mode=2,
        conversation=_make_conversation_with_swap_llm(),
        backend_pool={},
        safe_send_json=send,
    )

    assert out is False
    # Two sends: structured DragonError event + revert
    assert send.await_count == 2
    err_payload = send.await_args_list[0].args[1]
    revert_payload = send.await_args_list[1].args[1]
    # DragonError emits its own structured payload via .to_event()
    assert err_payload.get("code") == "custom_swap_failure"
    assert revert_payload == {"type": "config_update", "voice_mode": 0}


# ── 6. Pipeline raises generic Exception → False, generic γ-arch ─


@pytest.mark.asyncio
async def test_pipeline_generic_exception_emits_friendly_event_and_returns_false():
    """A4 (audit #137): raw exception text must NOT leak into the
    user-visible payload — the γ-arch wrapper sends a friendly
    'backend_swap_failed' message instead."""
    ws = _make_ws()
    pipeline = _make_pipeline_with_llm(MagicMock())
    pipeline.swap_backends = AsyncMock(
        side_effect=ValueError("internal libopus version mismatch — DO NOT LEAK"),
    )
    conn = _make_conn_state(pipeline=pipeline)
    cfg = _make_conn_config()
    send = _make_safe_send_json()

    out = await swap_pipeline_and_conversation_backends(
        ws,
        conn_state=conn,
        conn_config=cfg,
        llm_be="openrouter",
        voice_mode=2,
        conversation=_make_conversation_with_swap_llm(),
        backend_pool={},
        safe_send_json=send,
    )

    assert out is False
    err_payload = send.await_args_list[0].args[1]
    assert err_payload.get("code") == "backend_swap_failed"
    # The raw exception text must NOT appear in the user-visible payload.
    payload_str = str(err_payload)
    assert "libopus version mismatch" not in payload_str
    assert "DO NOT LEAK" not in payload_str


# ── 7. ConvEngine swap failure is silent ─────────────────────────


@pytest.mark.asyncio
async def test_conv_swap_failure_is_logged_but_returns_true():
    """The ConvEngine swap is "best effort" — its failure should NOT
    block config_update and should NOT send any client-visible
    error.  The user-visible pipeline swap already succeeded; the
    ConvEngine drift is a "next text turn might be wrong" concern,
    not a "config_update is broken" one."""
    ws = _make_ws()
    pipeline = _make_pipeline_with_llm(MagicMock())
    conn = _make_conn_state(pipeline=pipeline)
    cfg = _make_conn_config()
    conv = _make_conversation_with_swap_llm(raises=RuntimeError("ConvEngine failed"))
    send = _make_safe_send_json()

    out = await swap_pipeline_and_conversation_backends(
        ws,
        conn_state=conn,
        conn_config=cfg,
        llm_be="ollama",
        voice_mode=0,
        conversation=conv,
        backend_pool={},
        safe_send_json=send,
    )

    assert out is True  # pipeline swap succeeded → True regardless
    send.assert_not_awaited()  # NO client-visible error
    conv.swap_llm.assert_awaited_once()


# ── 8. ConversationEngine None (boot) ─────────────────────────────


@pytest.mark.asyncio
async def test_conversation_none_skips_conv_swap():
    """During boot ConvEngine may not be wired; must not crash."""
    ws = _make_ws()
    pipeline = _make_pipeline_with_llm(MagicMock())
    conn = _make_conn_state(pipeline=pipeline)
    cfg = _make_conn_config()

    out = await swap_pipeline_and_conversation_backends(
        ws,
        conn_state=conn,
        conn_config=cfg,
        llm_be="ollama",
        voice_mode=0,
        conversation=None,
        backend_pool={},
        safe_send_json=_make_safe_send_json(),
    )

    assert out is True
    pipeline.swap_backends.assert_awaited_once()
