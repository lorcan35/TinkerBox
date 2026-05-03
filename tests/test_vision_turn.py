"""Tests for ``dragon_voice.vision_turn``.

Pin every branch of the vision-turn handler so a future
refactor can't drift on:

  * Missing media → `media_not_found` (TRANSIENT/MEDIA)
  * No active LLM → `no_llm_available` (FATAL/LLM)
  * LLM lacking VISION cap → `vision_unsupported` (FATAL/LLM)
  * Mid-stream LLM exception → `vision_failed` + early return
    (no stray `llm_done` after a failed turn)
  * Happy path → llm tokens → llm_done
  * Per-turn tool tracker reset
  * Default prompt fallback when Tab5 omits `text`
  * #75 Phase 1b empty-reply wrap on tool-only turns
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from dragon_voice.errors import Scope, Severity
from dragon_voice.llm.base import Modality
from dragon_voice.vision_turn import handle_vision_turn


# ─── Test helpers ─────────────────────────────────────────────


def _make_ws(*, closed: bool = False) -> MagicMock:
    ws = MagicMock()
    ws.closed = closed
    ws.send_json = AsyncMock()
    return ws


def _make_safe_send_json() -> AsyncMock:
    return AsyncMock(return_value=True)


@asynccontextmanager
async def _noop_keepalive(ws, *, label: str):
    yield


def _make_keepalive_factory():
    calls: list[dict] = []

    @asynccontextmanager
    async def _factory(ws, *, label: str):
        calls.append({"label": label, "ws": ws})
        yield

    _factory.calls = calls  # type: ignore[attr-defined]
    return _factory


def _make_media_store(*, path: str | None = "/tmp/img.jpg") -> MagicMock:
    store = MagicMock()
    store.get_path = AsyncMock(return_value=path)
    return store


def _make_llm(*, has_vision: bool = True) -> MagicMock:
    llm = MagicMock()
    if has_vision:
        llm.capabilities = frozenset({Modality.TEXT, Modality.VISION})
    else:
        llm.capabilities = frozenset({Modality.TEXT})
    return llm


def _make_conversation(
    *,
    llm: Any | None = None,
    tokens: list[str] | None = None,
    raises: Exception | None = None,
) -> MagicMock:
    convo = MagicMock()
    convo._llm = llm
    captured: dict = {}

    async def _stream(**kwargs):
        captured.update(kwargs)
        if raises is not None:
            raise raises
        for tok in (tokens or []):
            yield tok

    convo.process_text_stream = _stream
    convo._captured = captured  # type: ignore[attr-defined]
    return convo


def _emitted_types(ws: MagicMock) -> list[str]:
    return [c.args[0].get("type") for c in ws.send_json.call_args_list]


def _emitted_codes(ws: MagicMock) -> list[str]:
    """Error codes from any error_event frames sent on ws."""
    out = []
    for c in ws.send_json.call_args_list:
        payload = c.args[0]
        if payload.get("type") == "error":
            out.append(payload.get("code"))
    return out


# ─── Error branches ───────────────────────────────────────────


class TestMissingMedia:
    @pytest.mark.asyncio
    async def test_media_not_found_emits_transient_media_error(self):
        ws = _make_ws()
        store = _make_media_store(path=None)  # not found
        convo = _make_conversation(llm=_make_llm())

        await handle_vision_turn(
            ws,
            cmd={"media_id": "stale-id"},
            conn_state={"session_id": "s1"},
            conversation=convo,
            media_store=store,
            ws_keepalive=_noop_keepalive,
            safe_send_json=_make_safe_send_json(),
        )

        assert "error" in _emitted_types(ws)
        assert _emitted_codes(ws) == ["media_not_found"]
        # No llm/llm_done frames emitted
        assert "llm" not in _emitted_types(ws)
        assert "llm_done" not in _emitted_types(ws)


class TestNoLLM:
    @pytest.mark.asyncio
    async def test_no_active_llm_emits_fatal_llm_error(self):
        ws = _make_ws()
        store = _make_media_store()
        # Conversation exists but `._llm` is None (post-shutdown).
        convo = MagicMock()
        convo._llm = None

        await handle_vision_turn(
            ws,
            cmd={"media_id": "abc"},
            conn_state={"session_id": "s1"},
            conversation=convo,
            media_store=store,
            ws_keepalive=_noop_keepalive,
            safe_send_json=_make_safe_send_json(),
        )

        assert _emitted_codes(ws) == ["no_llm_available"]
        # Severity check
        sev = ws.send_json.call_args_list[0].args[0]["severity"]
        assert sev == Severity.FATAL.value


class TestVisionUnsupported:
    @pytest.mark.asyncio
    async def test_text_only_llm_emits_fatal_vision_unsupported(self):
        ws = _make_ws()
        store = _make_media_store()
        convo = _make_conversation(llm=_make_llm(has_vision=False))

        await handle_vision_turn(
            ws,
            cmd={"media_id": "abc"},
            conn_state={"session_id": "s1"},
            conversation=convo,
            media_store=store,
            ws_keepalive=_noop_keepalive,
            safe_send_json=_make_safe_send_json(),
        )

        assert _emitted_codes(ws) == ["vision_unsupported"]
        sev = ws.send_json.call_args_list[0].args[0]["severity"]
        assert sev == Severity.FATAL.value
        scope = ws.send_json.call_args_list[0].args[0]["scope"]
        assert scope == Scope.LLM.value


# ─── Happy path ──────────────────────────────────────────────


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_streams_tokens_then_llm_done(self):
        ws = _make_ws()
        store = _make_media_store()
        convo = _make_conversation(
            llm=_make_llm(),
            tokens=["A ", "red ", "chair."],
        )
        keepalive = _make_keepalive_factory()
        conn_state = {"session_id": "s1"}

        await handle_vision_turn(
            ws,
            cmd={"media_id": "img-42", "text": "describe"},
            conn_state=conn_state,
            conversation=convo,
            media_store=store,
            ws_keepalive=keepalive,
            safe_send_json=_make_safe_send_json(),
        )

        # Per-turn tool tracker was reset
        assert conn_state["tool_calls_this_turn"] == []

        # 3 llm tokens then 1 llm_done
        types = _emitted_types(ws)
        assert types == ["llm", "llm", "llm", "llm_done"]
        # Joined text
        joined = "".join(
            c.args[0].get("text", "") for c in ws.send_json.call_args_list
            if c.args[0].get("type") == "llm"
        )
        assert joined == "A red chair."

        # ConvEngine got vision + media_id set
        cap = convo._captured
        assert cap["session_id"] == "s1"
        assert cap["text"] == "describe"
        assert cap["input_mode"] == "vision"
        assert cap["media_id"] == "img-42"

        # Keepalive was wrapped with label="vision"
        assert keepalive.calls == [{"label": "vision", "ws": ws}]

    @pytest.mark.asyncio
    async def test_default_prompt_when_text_missing(self):
        ws = _make_ws()
        store = _make_media_store()
        convo = _make_conversation(llm=_make_llm(), tokens=[])

        await handle_vision_turn(
            ws,
            cmd={"media_id": "img"},  # no `text`
            conn_state={"session_id": "s"},
            conversation=convo,
            media_store=store,
            ws_keepalive=_noop_keepalive,
            safe_send_json=_make_safe_send_json(),
        )

        assert convo._captured["text"] == "What's in this image?"


# ─── Conversation override from conn_state ───────────────────


class TestConversationOverride:
    @pytest.mark.asyncio
    async def test_per_connection_conv_takes_precedence_over_default(self):
        """If `conn_state["conversation"]` is set (per-connection
        ConvEngine), it must be used over the server default."""
        ws = _make_ws()
        store = _make_media_store()
        per_conn_llm = _make_llm()
        per_conn_convo = _make_conversation(llm=per_conn_llm, tokens=["x"])

        # Default convo has a NON-vision LLM — if we wrongly used
        # the default, the call would hit `vision_unsupported`.
        default_convo = _make_conversation(llm=_make_llm(has_vision=False))

        await handle_vision_turn(
            ws,
            cmd={"media_id": "img"},
            conn_state={
                "session_id": "s",
                "conversation": per_conn_convo,
            },
            conversation=default_convo,
            media_store=store,
            ws_keepalive=_noop_keepalive,
            safe_send_json=_make_safe_send_json(),
        )

        # Happy path emitted, not the unsupported error
        codes = _emitted_codes(ws)
        assert "vision_unsupported" not in codes
        assert "llm_done" in _emitted_types(ws)


# ─── Mid-turn exception ──────────────────────────────────────


class TestMidTurnException:
    @pytest.mark.asyncio
    async def test_llm_exception_emits_vision_failed_and_returns(self):
        """Mid-stream RuntimeError → emits vision_failed + early
        return.  No stray `llm_done` after a failed turn."""
        ws = _make_ws()
        store = _make_media_store()
        convo = _make_conversation(
            llm=_make_llm(),
            raises=RuntimeError("model crashed"),
        )

        await handle_vision_turn(
            ws,
            cmd={"media_id": "img"},
            conn_state={"session_id": "s"},
            conversation=convo,
            media_store=store,
            ws_keepalive=_noop_keepalive,
            safe_send_json=_make_safe_send_json(),
        )

        codes = _emitted_codes(ws)
        assert "vision_failed" in codes
        # Pin the early return: no llm_done frame after the error
        assert "llm_done" not in _emitted_types(ws)

    @pytest.mark.asyncio
    async def test_error_message_does_not_leak_python_exception_text(self):
        """Phase 3 γ1 closure — the user-facing message must NOT
        be `str(e)`.  Pre-fix this leaked things like
        'list index out of range' into Tab5's voice caption."""
        ws = _make_ws()
        store = _make_media_store()
        convo = _make_conversation(
            llm=_make_llm(),
            raises=IndexError("list index out of range"),
        )

        await handle_vision_turn(
            ws,
            cmd={"media_id": "img"},
            conn_state={"session_id": "s"},
            conversation=convo,
            media_store=store,
            ws_keepalive=_noop_keepalive,
            safe_send_json=_make_safe_send_json(),
        )

        msg = next(
            c.args[0]["message"] for c in ws.send_json.call_args_list
            if c.args[0].get("code") == "vision_failed"
        )
        assert "list index out of range" not in msg
        assert "Image analysis failed" in msg


# ─── #75 Phase 1b empty-reply wrap ───────────────────────────


class TestEmptyReplyWrap:
    @pytest.mark.asyncio
    async def test_empty_text_with_tool_fire_synthesizes_wrap(self):
        """When LLM emitted near-empty text but a tool fired, the
        #75 Phase 1b wrap synthesizes a template ack so Tab5
        doesn't get an empty bubble."""
        ws = _make_ws()
        store = _make_media_store()
        convo = _make_conversation(
            llm=_make_llm(),
            tokens=[],  # no text
        )
        # Simulate ConversationEngine pushing a tool call into
        # conn_state during process_text_stream — we pre-populate
        # to mimic the post-stream state.
        conn_state = {
            "session_id": "s",
            "tool_calls_this_turn": [{"name": "note", "args": {}}],
        }

        # process_text_stream wipes tool_calls_this_turn=[], so we
        # need to repopulate after process_text_stream returns.
        # Patch the convo to set tool_calls after stream exits.
        async def _stream_then_record(**kwargs):
            # Stream nothing
            return
            yield  # pragma: no cover

        # We can't easily re-set conn_state mid-stream from inside
        # the convo mock without reaching back into kwargs.  The
        # simpler path: pre-populate AFTER the handler resets it
        # by patching the wrap entry conditions.  Easiest: mock
        # `looks_like_useful_text` + `synthesize_wrap`.
        from unittest.mock import patch

        with patch(
            "dragon_voice.vision_turn.looks_like_useful_text",
            return_value=False,
        ), patch(
            "dragon_voice.vision_turn.synthesize_wrap",
            return_value="Saved note.",
        ):
            # Re-set tool_calls_this_turn AFTER the handler clears
            # it on entry.  We do this by overriding the convo's
            # process_text_stream to set the conn_state entry
            # mid-stream.
            async def _stream_set_tool(**kwargs):
                conn_state["tool_calls_this_turn"] = [
                    {"name": "note", "args": {}}
                ]
                return
                yield  # pragma: no cover
            convo.process_text_stream = _stream_set_tool

            await handle_vision_turn(
                ws,
                cmd={"media_id": "img"},
                conn_state=conn_state,
                conversation=convo,
                media_store=store,
                ws_keepalive=_noop_keepalive,
                safe_send_json=_make_safe_send_json(),
            )

        # Wrap text was sent as an llm frame BEFORE llm_done
        sent = ws.send_json.call_args_list
        types = [c.args[0].get("type") for c in sent]
        assert types == ["llm", "llm_done"]
        assert sent[0].args[0]["text"] == "Saved note."

    @pytest.mark.asyncio
    async def test_empty_text_no_tool_fire_skips_wrap(self):
        """Empty text + zero tools → just llm_done (no wrap).
        Pre-extract this was the silent fallthrough."""
        ws = _make_ws()
        store = _make_media_store()
        convo = _make_conversation(llm=_make_llm(), tokens=[])

        await handle_vision_turn(
            ws,
            cmd={"media_id": "img"},
            conn_state={"session_id": "s"},
            conversation=convo,
            media_store=store,
            ws_keepalive=_noop_keepalive,
            safe_send_json=_make_safe_send_json(),
        )

        # Just llm_done — no wrap llm frame
        assert _emitted_types(ws) == ["llm_done"]
