"""Tests for ``dragon_voice.binary_frame_dispatch``.

Pin every routing branch + the per-peer dead-connection swallow
on AUD0 fan-out.

Test classes:

  * ``TestVideoFrame`` — VID0-tagged → video_upstream.on_frame
  * ``TestCallAudioBroadcast`` — AUD0-tagged → peer fan-out;
    sender excluded; closed peers skipped; per-peer send
    failures swallowed
  * ``TestRawPCM`` — unprefixed → pipeline.feed_audio
  * ``TestNoPipelineFallthrough`` — raw PCM with no pipeline
    attached is a clean no-op (boot race)
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dragon_voice.binary_frame_dispatch import dispatch_binary_frame


def _make_pipeline() -> MagicMock:
    p = MagicMock()
    p.feed_audio = AsyncMock()
    return p


def _make_peer_conn(*, session_id: str, ws_closed: bool = False) -> dict:
    peer_ws = MagicMock()
    peer_ws.closed = ws_closed
    peer_ws.send_bytes = AsyncMock()
    return {"session_id": session_id, "ws": peer_ws}


# ─── VID0 routing ─────────────────────────────────────────────


class TestVideoFrame:
    @pytest.mark.asyncio
    async def test_vid0_frame_routes_to_video_handler(self):
        msg = b"VID0" + b"\x00" * 100  # any payload after magic

        with patch(
            "dragon_voice.video_upstream.parse_video_frame.peek",
            return_value=True,
        ), patch(
            "dragon_voice.video_upstream.get_handler"
        ) as get_h, patch(
            "dragon_voice.audio_codec.peek_call_audio_magic",
            return_value=False,
        ):
            handler = MagicMock()
            handler.on_frame = AsyncMock()
            get_h.return_value = handler

            await dispatch_binary_frame(
                msg,
                conn_state={"session_id": "s1", "device_id": "d1"},
                active_connections={},
            )

            handler.on_frame.assert_awaited_once()
            kw = handler.on_frame.await_args.kwargs
            assert kw["session_id"] == "s1"
            assert kw["device_id"] == "d1"
            assert kw["wire_bytes"] == msg

    @pytest.mark.asyncio
    async def test_vid0_frame_does_not_call_audio_or_pipeline(self):
        """Pin the early return: VID0 → video handler ONLY.  No
        peer fan-out, no pipeline.feed_audio."""
        msg = b"VID0" + b"junk"
        pipeline = _make_pipeline()

        with patch(
            "dragon_voice.video_upstream.parse_video_frame.peek",
            return_value=True,
        ), patch(
            "dragon_voice.video_upstream.get_handler"
        ) as get_h, patch(
            "dragon_voice.audio_codec.peek_call_audio_magic",
            return_value=False,
        ) as audio_peek:
            handler = MagicMock()
            handler.on_frame = AsyncMock()
            get_h.return_value = handler

            await dispatch_binary_frame(
                msg,
                conn_state={"pipeline": pipeline, "session_id": "s"},
                active_connections={},
            )

            # peek_call_audio_magic NOT consulted (we returned
            # early on VID0).
            audio_peek.assert_not_called()
            pipeline.feed_audio.assert_not_awaited()


# ─── AUD0 routing ─────────────────────────────────────────────


class TestCallAudioBroadcast:
    @pytest.mark.asyncio
    async def test_aud0_broadcasts_to_other_peers_skipping_sender(self):
        msg = b"AUD0" + b"\x01\x02\x03"
        sender = _make_peer_conn(session_id="s-sender")
        peer1 = _make_peer_conn(session_id="s-peer-1")
        peer2 = _make_peer_conn(session_id="s-peer-2")

        active = {
            "ws-sender": sender,
            "ws-peer-1": peer1,
            "ws-peer-2": peer2,
        }

        with patch(
            "dragon_voice.video_upstream.parse_video_frame.peek",
            return_value=False,
        ), patch(
            "dragon_voice.audio_codec.peek_call_audio_magic",
            return_value=True,
        ):
            await dispatch_binary_frame(
                msg,
                conn_state={"session_id": "s-sender"},
                active_connections=active,
            )

        # Sender does NOT echo back to itself.
        sender["ws"].send_bytes.assert_not_awaited()
        # Both other peers received the frame verbatim.
        peer1["ws"].send_bytes.assert_awaited_once_with(msg)
        peer2["ws"].send_bytes.assert_awaited_once_with(msg)

    @pytest.mark.asyncio
    async def test_aud0_skips_closed_peers(self):
        msg = b"AUD0" + b"x"
        live_peer = _make_peer_conn(session_id="live")
        dead_peer = _make_peer_conn(session_id="dead", ws_closed=True)

        active = {"a": live_peer, "b": dead_peer}

        with patch(
            "dragon_voice.video_upstream.parse_video_frame.peek",
            return_value=False,
        ), patch(
            "dragon_voice.audio_codec.peek_call_audio_magic",
            return_value=True,
        ):
            await dispatch_binary_frame(
                msg,
                conn_state={"session_id": "sender"},
                active_connections=active,
            )

        live_peer["ws"].send_bytes.assert_awaited_once_with(msg)
        dead_peer["ws"].send_bytes.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_aud0_skips_peers_with_no_ws(self):
        """Pre-register peer (ws=None) is silently skipped."""
        msg = b"AUD0" + b"x"
        peer_no_ws = {"session_id": "preregister", "ws": None}
        live_peer = _make_peer_conn(session_id="live")

        active = {"a": peer_no_ws, "b": live_peer}

        with patch(
            "dragon_voice.video_upstream.parse_video_frame.peek",
            return_value=False,
        ), patch(
            "dragon_voice.audio_codec.peek_call_audio_magic",
            return_value=True,
        ):
            await dispatch_binary_frame(
                msg,
                conn_state={"session_id": "sender"},
                active_connections=active,
            )

        live_peer["ws"].send_bytes.assert_awaited_once_with(msg)

    @pytest.mark.asyncio
    async def test_aud0_one_peer_send_failure_does_not_break_fanout(self):
        """A peer's send_bytes raising MUST NOT prevent the rest
        of the fan-out.  Pin so a future refactor can't drop the
        per-peer try/except."""
        msg = b"AUD0" + b"x"
        broken = _make_peer_conn(session_id="broken")
        broken["ws"].send_bytes = AsyncMock(
            side_effect=ConnectionResetError("peer left")
        )
        good = _make_peer_conn(session_id="good")

        active = {"a": broken, "b": good}

        with patch(
            "dragon_voice.video_upstream.parse_video_frame.peek",
            return_value=False,
        ), patch(
            "dragon_voice.audio_codec.peek_call_audio_magic",
            return_value=True,
        ):
            # Must not raise.
            await dispatch_binary_frame(
                msg,
                conn_state={"session_id": "sender"},
                active_connections=active,
            )

        good["ws"].send_bytes.assert_awaited_once_with(msg)

    @pytest.mark.asyncio
    async def test_aud0_does_not_call_pipeline(self):
        msg = b"AUD0" + b"x"
        pipeline = _make_pipeline()

        with patch(
            "dragon_voice.video_upstream.parse_video_frame.peek",
            return_value=False,
        ), patch(
            "dragon_voice.audio_codec.peek_call_audio_magic",
            return_value=True,
        ):
            await dispatch_binary_frame(
                msg,
                conn_state={"session_id": "s", "pipeline": pipeline},
                active_connections={},
            )

        pipeline.feed_audio.assert_not_awaited()


# ─── Raw PCM routing ─────────────────────────────────────────


class TestRawPCM:
    @pytest.mark.asyncio
    async def test_unprefixed_routes_to_pipeline_feed_audio(self):
        msg = b"\x00\x01\x02\x03" * 16  # plausible raw PCM
        pipeline = _make_pipeline()

        with patch(
            "dragon_voice.video_upstream.parse_video_frame.peek",
            return_value=False,
        ), patch(
            "dragon_voice.audio_codec.peek_call_audio_magic",
            return_value=False,
        ):
            await dispatch_binary_frame(
                msg,
                conn_state={"session_id": "s", "pipeline": pipeline},
                active_connections={},
            )

        pipeline.feed_audio.assert_awaited_once_with(msg)


# ─── No-pipeline fallthrough ─────────────────────────────────


class TestNoPipelineFallthrough:
    @pytest.mark.asyncio
    async def test_raw_pcm_with_no_pipeline_is_silent_noop(self):
        """Pre-register / boot race: pipeline not attached yet.
        A raw PCM frame must NOT raise — silent no-op."""
        msg = b"\x00\x01"

        with patch(
            "dragon_voice.video_upstream.parse_video_frame.peek",
            return_value=False,
        ), patch(
            "dragon_voice.audio_codec.peek_call_audio_magic",
            return_value=False,
        ):
            # Must not raise.
            await dispatch_binary_frame(
                msg,
                conn_state={"session_id": "s"},  # no pipeline key
                active_connections={},
            )

    @pytest.mark.asyncio
    async def test_pipeline_read_fresh_per_frame(self):
        """Pin the per-frame `conn_state.get("pipeline")` lookup
        so a config-update mid-call (which hot-swaps the
        pipeline) is reflected on the very next frame."""
        msg = b"\x00\x01"
        first = _make_pipeline()
        second = _make_pipeline()
        conn_state = {"session_id": "s", "pipeline": first}

        with patch(
            "dragon_voice.video_upstream.parse_video_frame.peek",
            return_value=False,
        ), patch(
            "dragon_voice.audio_codec.peek_call_audio_magic",
            return_value=False,
        ):
            await dispatch_binary_frame(
                msg, conn_state=conn_state, active_connections={},
            )
            # Hot-swap mid-stream.
            conn_state["pipeline"] = second
            await dispatch_binary_frame(
                msg, conn_state=conn_state, active_connections={},
            )

        first.feed_audio.assert_awaited_once_with(msg)
        second.feed_audio.assert_awaited_once_with(msg)
