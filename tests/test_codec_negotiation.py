"""Tests for ``dragon_voice.codec_negotiation.maybe_swap_uplink_codec``.

Pin the four branches:

  1. Happy path: cmd carries audio_uplink_codec + pipeline is wired
     → set_uplink_codec called; ACK sent with the actually-applied
     codec.
  2. Backward-compat: legacy `audio_codec` alias is recognised.
  3. No codec field → no-op.
  4. No pipeline (boot race) → no-op.

Plus the fallback-observability invariant: when set_uplink_codec
returns a different codec than requested (e.g. opus → pcm because
libopus missing), the ACK echoes the *applied* one, not the requested.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from dragon_voice.codec_negotiation import maybe_swap_uplink_codec


def _make_ws(*, closed: bool = False) -> MagicMock:
    ws = MagicMock()
    ws.closed = closed
    return ws


def _make_pipeline_returning(applied_codec: str) -> MagicMock:
    p = MagicMock()
    p.set_uplink_codec = MagicMock(return_value=applied_codec)
    return p


def _make_safe_send_json() -> AsyncMock:
    return AsyncMock(return_value=True)


# ── Happy path ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_canonical_field_swap_applies_and_acks():
    ws = _make_ws()
    pipeline = _make_pipeline_returning("opus")
    conn = {"pipeline": pipeline}
    send = _make_safe_send_json()

    await maybe_swap_uplink_codec(
        ws,
        cmd={"audio_uplink_codec": "opus"},
        conn_state=conn,
        safe_send_json=send,
    )

    pipeline.set_uplink_codec.assert_called_once_with("opus")
    send.assert_awaited_once()
    payload = send.await_args.args[1]
    assert payload == {
        "type": "config_update",
        "audio_uplink_codec": "opus",
        "reason": "codec_negotiation",
    }


@pytest.mark.asyncio
async def test_legacy_audio_codec_alias_recognised():
    """Older Tab5 firmware may send the field as `audio_codec`."""
    ws = _make_ws()
    pipeline = _make_pipeline_returning("pcm")
    send = _make_safe_send_json()

    await maybe_swap_uplink_codec(
        ws,
        cmd={"audio_codec": "pcm"},
        conn_state={"pipeline": pipeline},
        safe_send_json=send,
    )

    pipeline.set_uplink_codec.assert_called_once_with("pcm")
    assert send.await_args.args[1]["audio_uplink_codec"] == "pcm"


@pytest.mark.asyncio
async def test_canonical_field_takes_precedence_over_legacy_alias():
    """If both fields are present (transitional firmware), the
    canonical name wins."""
    ws = _make_ws()
    pipeline = _make_pipeline_returning("opus")
    send = _make_safe_send_json()

    await maybe_swap_uplink_codec(
        ws,
        cmd={"audio_uplink_codec": "opus", "audio_codec": "pcm"},
        conn_state={"pipeline": pipeline},
        safe_send_json=send,
    )
    pipeline.set_uplink_codec.assert_called_once_with("opus")


# ── Fallback observability ───────────────────────────────────────


@pytest.mark.asyncio
async def test_ack_echoes_applied_codec_not_requested():
    """When the pipeline falls back (e.g. opus requested, libopus
    missing → applied='pcm'), the ACK must echo the APPLIED codec
    so the client sees the fallback.  This is the whole point of
    the codec-negotiation reply pattern from #173."""
    ws = _make_ws()
    pipeline = _make_pipeline_returning("pcm")  # fallback
    send = _make_safe_send_json()

    await maybe_swap_uplink_codec(
        ws,
        cmd={"audio_uplink_codec": "opus"},  # requested opus
        conn_state={"pipeline": pipeline},
        safe_send_json=send,
    )

    payload = send.await_args.args[1]
    assert payload["audio_uplink_codec"] == "pcm"  # NOT "opus"


# ── No-op branches ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_codec_field_is_noop():
    ws = _make_ws()
    pipeline = _make_pipeline_returning("opus")
    send = _make_safe_send_json()

    await maybe_swap_uplink_codec(
        ws,
        cmd={"voice_mode": 0},  # no codec field
        conn_state={"pipeline": pipeline},
        safe_send_json=send,
    )

    pipeline.set_uplink_codec.assert_not_called()
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_pipeline_is_noop():
    """Connection still booting — pipeline not wired yet.  Codec
    selection happens at pipeline init from conn_config; client
    will see the final codec via session_start."""
    ws = _make_ws()
    send = _make_safe_send_json()

    await maybe_swap_uplink_codec(
        ws,
        cmd={"audio_uplink_codec": "opus"},
        conn_state={"pipeline": None},
        safe_send_json=send,
    )
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_ws_closed_skips_ack():
    """Pipeline still gets the swap, but the ACK is best-effort —
    silent skip when the WS is closed."""
    ws = _make_ws(closed=True)
    pipeline = _make_pipeline_returning("opus")
    send = _make_safe_send_json()

    await maybe_swap_uplink_codec(
        ws,
        cmd={"audio_uplink_codec": "opus"},
        conn_state={"pipeline": pipeline},
        safe_send_json=send,
    )

    pipeline.set_uplink_codec.assert_called_once_with("opus")
    send.assert_not_awaited()
