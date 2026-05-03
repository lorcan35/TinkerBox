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


# ─── negotiate_uplink_codec_at_register (PR #241) ─────────────


from dragon_voice.codec_negotiation import negotiate_uplink_codec_at_register


class TestRegisterTimeCodecNego:
    @pytest.mark.asyncio
    async def test_opus_capable_client_gets_config_update(self):
        """Tab5 advertises [pcm, opus] → Dragon picks opus, applies
        on pipeline, sends config_update so Tab5 switches encoder."""
        from unittest.mock import patch
        ws = _make_ws()
        pipeline = _make_pipeline_returning("opus")
        send = _make_safe_send_json()

        with patch(
            "dragon_voice.audio_codec.negotiate_uplink",
            return_value="opus",
        ):
            await negotiate_uplink_codec_at_register(
                ws,
                pipeline=pipeline,
                capabilities={"audio_codec": ["pcm", "opus"]},
                device_id="dev1",
                safe_send_json=send,
            )

        pipeline.set_uplink_codec.assert_called_once_with("opus")
        send.assert_awaited_once()
        frame = send.await_args.args[1]
        assert frame["type"] == "config_update"
        assert frame["audio_uplink_codec"] == "opus"
        assert frame["reason"] == "codec_negotiation"

    @pytest.mark.asyncio
    async def test_pcm_only_client_skips_config_update(self):
        """Backward-compat: legacy clients without the capability
        OR clients that only advertise PCM stay on PCM with no
        config_update round-trip."""
        from unittest.mock import patch
        ws = _make_ws()
        pipeline = _make_pipeline_returning("pcm")
        send = _make_safe_send_json()

        with patch(
            "dragon_voice.audio_codec.negotiate_uplink",
            return_value="pcm",
        ):
            await negotiate_uplink_codec_at_register(
                ws,
                pipeline=pipeline,
                capabilities={"audio_codec": ["pcm"]},
                device_id="dev2",
                safe_send_json=send,
            )

        # set_uplink_codec still called (pipeline knows it's PCM)
        pipeline.set_uplink_codec.assert_called_once_with("pcm")
        # But no config_update — PCM is the default
        send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_opus_request_falls_back_to_pcm_no_ack(self):
        """Tab5 requests opus, Dragon's pipeline.set_uplink_codec
        falls back to PCM (libopus missing on Dragon).  Result:
        PCM applied + no config_update emit (since result == pcm)."""
        from unittest.mock import patch
        ws = _make_ws()
        # Pipeline returns "pcm" even though "opus" requested
        pipeline = _make_pipeline_returning("pcm")
        send = _make_safe_send_json()

        with patch(
            "dragon_voice.audio_codec.negotiate_uplink",
            return_value="opus",
        ):
            await negotiate_uplink_codec_at_register(
                ws,
                pipeline=pipeline,
                capabilities={"audio_codec": ["pcm", "opus"]},
                device_id="dev3",
                safe_send_json=send,
            )

        pipeline.set_uplink_codec.assert_called_once_with("opus")
        send.assert_not_awaited()  # applied=pcm → no client switch

    @pytest.mark.asyncio
    async def test_no_capabilities_skips(self):
        ws = _make_ws()
        pipeline = _make_pipeline_returning("pcm")
        send = _make_safe_send_json()

        await negotiate_uplink_codec_at_register(
            ws,
            pipeline=pipeline,
            capabilities=None,
            device_id="dev4",
            safe_send_json=send,
        )

        pipeline.set_uplink_codec.assert_not_called()
        send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_audio_codec_field_skips(self):
        """Capabilities block exists but `audio_codec` is missing —
        legacy field set without codec advertisement."""
        ws = _make_ws()
        pipeline = _make_pipeline_returning("pcm")
        send = _make_safe_send_json()

        await negotiate_uplink_codec_at_register(
            ws,
            pipeline=pipeline,
            capabilities={"widgets": {"types": ["live"]}},
            device_id="dev5",
            safe_send_json=send,
        )

        pipeline.set_uplink_codec.assert_not_called()

    @pytest.mark.asyncio
    async def test_audio_codec_not_a_list_skips(self):
        """Defensive: if the field somehow isn't a list (string,
        dict, etc.), silently skip rather than crashing."""
        ws = _make_ws()
        pipeline = _make_pipeline_returning("pcm")
        send = _make_safe_send_json()

        await negotiate_uplink_codec_at_register(
            ws,
            pipeline=pipeline,
            capabilities={"audio_codec": "opus"},  # string, not list
            device_id="dev6",
            safe_send_json=send,
        )

        pipeline.set_uplink_codec.assert_not_called()

    @pytest.mark.asyncio
    async def test_exception_in_negotiate_does_not_propagate(self):
        """audio_codec.negotiate_uplink could raise; whole chain
        wrapped in try/except so it never tears down register."""
        from unittest.mock import patch
        ws = _make_ws()
        pipeline = _make_pipeline_returning("pcm")
        send = _make_safe_send_json()

        with patch(
            "dragon_voice.audio_codec.negotiate_uplink",
            side_effect=RuntimeError("malformed"),
        ):
            # Must NOT raise.
            await negotiate_uplink_codec_at_register(
                ws,
                pipeline=pipeline,
                capabilities={"audio_codec": ["pcm", "opus"]},
                device_id="dev7",
                safe_send_json=send,
            )

        send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_exception_in_set_uplink_codec_does_not_propagate(self):
        from unittest.mock import patch
        ws = _make_ws()
        pipeline = MagicMock()
        pipeline.set_uplink_codec = MagicMock(
            side_effect=RuntimeError("backend dead"),
        )
        send = _make_safe_send_json()

        with patch(
            "dragon_voice.audio_codec.negotiate_uplink",
            return_value="opus",
        ):
            # Must NOT raise.
            await negotiate_uplink_codec_at_register(
                ws,
                pipeline=pipeline,
                capabilities={"audio_codec": ["pcm", "opus"]},
                device_id="dev8",
                safe_send_json=send,
            )

    @pytest.mark.asyncio
    async def test_ws_closed_skips_ack_but_still_sets_codec(self):
        from unittest.mock import patch
        ws = _make_ws(closed=True)
        pipeline = _make_pipeline_returning("opus")
        send = _make_safe_send_json()

        with patch(
            "dragon_voice.audio_codec.negotiate_uplink",
            return_value="opus",
        ):
            await negotiate_uplink_codec_at_register(
                ws,
                pipeline=pipeline,
                capabilities={"audio_codec": ["pcm", "opus"]},
                device_id="dev9",
                safe_send_json=send,
            )

        pipeline.set_uplink_codec.assert_called_once_with("opus")
        # ACK skipped (ws closed)
        send.assert_not_awaited()
