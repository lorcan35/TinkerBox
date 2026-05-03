"""Tests for ``dragon_voice.tinkerclaw_text_path``.

Pin every branch of the TC bypass extract:

  * ``TestPreconditionGate`` — non-TC modes return False (caller
    falls through to local ConvEngine path).
  * ``TestHappyPath`` — full chain: thinking-indicator → token
    stream with keepalive → llm_done → zero-cost receipt → rich
    media emit.
  * ``TestSessionKey`` — `set_session_key` only fires when LLM
    implements `SupportsSessionKey`.
  * ``TestDragonErrorFastFail`` — γ2-M6 fast-fail emits structured
    error_event + llm_done(0,"") and returns True (handled).
  * ``TestEmptyResponseWrap`` — W15-H09 apology fires when zero
    tools fired this turn.
  * ``TestRichMediaGate`` — rich media emit skipped when LLM
    returned zero tokens.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dragon_voice.errors import DragonError, Scope, Severity
from dragon_voice.tinkerclaw_text_path import handle_tinkerclaw_text_path


# ─── Test helpers ───────────────────────────────────────────────


def _make_ws(*, closed: bool = False) -> MagicMock:
    ws = MagicMock()
    ws.closed = closed
    ws.send_json = AsyncMock()
    return ws


def _make_safe_send_json() -> AsyncMock:
    return AsyncMock(return_value=True)


@asynccontextmanager
async def _noop_keepalive(ws, *, label: str):
    """Stand-in for VoiceServer._ws_keepalive_during_inference."""
    yield


def _make_keepalive_factory():
    """Returns a keepalive factory that records the label it was
    called with so tests can assert ``label="tc_text"``."""
    calls: list[dict] = []

    @asynccontextmanager
    async def _factory(ws, *, label: str):
        calls.append({"label": label, "ws": ws})
        yield

    _factory.calls = calls  # type: ignore[attr-defined]
    return _factory


def _make_conn_config(
    *,
    backend: str = "tinkerclaw",
    tinkerclaw_model: str = "",
) -> MagicMock:
    cfg = MagicMock()
    cfg.llm.backend = backend
    cfg.llm.tinkerclaw_model = tinkerclaw_model
    return cfg


def _make_llm(
    *,
    name: str = "tc_gateway",
    inner_model: str = "",
    tokens: list[str] | None = None,
    raises: Exception | None = None,
) -> MagicMock:
    """Build a fake TC LLM.  `tokens` controls the streamed yield;
    `raises` makes the stream raise mid-flight."""
    llm = MagicMock()
    llm.name = name
    llm._model = inner_model

    async def _stream(messages):
        if raises is not None:
            raise raises
        for tok in (tokens or []):
            yield tok

    llm.generate_stream_with_messages = _stream
    return llm


def _make_conversation(llm: Any) -> MagicMock:
    convo = MagicMock()
    convo.llm = llm
    return convo


# ─── TestPreconditionGate ───────────────────────────────────────


class TestPreconditionGate:
    @pytest.mark.asyncio
    async def test_returns_false_when_not_tc_mode(self):
        """Non-TC backend returns False so the caller falls through
        to the local ConvEngine path — no llm calls made."""
        ws = _make_ws()
        send = _make_safe_send_json()
        cfg = _make_conn_config(backend="ollama")  # NOT tinkerclaw
        llm = _make_llm(tokens=["should-not-stream"])
        convo = _make_conversation(llm)

        handled = await handle_tinkerclaw_text_path(
            ws,
            conn_state={},
            conn_config=cfg,
            text="hello",
            session_id="s1",
            conversation=convo,
            media_pipeline=None,
            ws_keepalive=_noop_keepalive,
            safe_send_json=send,
        )
        assert handled is False
        ws.send_json.assert_not_called()
        send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_returns_false_when_no_conversation(self):
        """No ConvEngine (test path / boot race) → False, no work."""
        ws = _make_ws()
        cfg = _make_conn_config(backend="tinkerclaw")

        handled = await handle_tinkerclaw_text_path(
            ws,
            conn_state={},
            conn_config=cfg,
            text="hi",
            session_id="s1",
            conversation=None,
            media_pipeline=None,
            ws_keepalive=_noop_keepalive,
            safe_send_json=_make_safe_send_json(),
        )
        assert handled is False
        ws.send_json.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_false_when_conversation_has_no_llm(self):
        """ConvEngine with `.llm = None` (post-shutdown) → False."""
        ws = _make_ws()
        cfg = _make_conn_config(backend="tinkerclaw")
        convo = MagicMock()
        convo.llm = None

        handled = await handle_tinkerclaw_text_path(
            ws,
            conn_state={},
            conn_config=cfg,
            text="hi",
            session_id="s1",
            conversation=convo,
            media_pipeline=None,
            ws_keepalive=_noop_keepalive,
            safe_send_json=_make_safe_send_json(),
        )
        assert handled is False

    @pytest.mark.asyncio
    async def test_returns_false_when_no_conn_config(self):
        """No conn_config (early test path) → False."""
        ws = _make_ws()
        llm = _make_llm()
        convo = _make_conversation(llm)

        handled = await handle_tinkerclaw_text_path(
            ws,
            conn_state={},
            conn_config=None,
            text="hi",
            session_id="s1",
            conversation=convo,
            media_pipeline=None,
            ws_keepalive=_noop_keepalive,
            safe_send_json=_make_safe_send_json(),
        )
        assert handled is False


# ─── TestHappyPath ──────────────────────────────────────────────


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_full_chain_emits_thinking_tokens_done_receipt(self):
        ws = _make_ws()
        send = _make_safe_send_json()
        keepalive = _make_keepalive_factory()
        cfg = _make_conn_config(
            backend="tinkerclaw",
            tinkerclaw_model="minimax/MiniMax-M2.5",
        )
        llm = _make_llm(
            name="tc_gateway",
            inner_model="minimax/MiniMax-M2.5",
            tokens=["Hello", " ", "world"],
        )
        convo = _make_conversation(llm)
        media_pipeline = MagicMock()

        with patch(
            "dragon_voice.tinkerclaw_text_path.emit_rich_media_for_text_turn",
            new=AsyncMock(),
        ) as rich:
            handled = await handle_tinkerclaw_text_path(
                ws,
                conn_state={
                    "session_id": "s-tc-1",
                    "tool_calls_this_turn": [{"name": "any"}],  # tools fired → no apology
                },
                conn_config=cfg,
                text="hello",
                session_id="s-tc-1",
                conversation=convo,
                media_pipeline=media_pipeline,
                ws_keepalive=keepalive,
                safe_send_json=send,
            )

        assert handled is True

        # Emitted frames: empty thinking + 3 tokens + llm_done + receipt
        # (receipt goes through safe_send_json, not ws.send_json)
        sent_types = [c.args[0]["type"] for c in ws.send_json.call_args_list]
        assert sent_types == ["llm", "llm", "llm", "llm", "llm_done"]

        # Token sequence: empty-string thinking, then "Hello", " ", "world"
        sent_texts = [c.args[0].get("text") for c in ws.send_json.call_args_list]
        assert sent_texts[0] == ""  # thinking
        assert sent_texts[1:4] == ["Hello", " ", "world"]
        # llm_done carries the joined text
        assert sent_texts[4] == "Hello world"

        # Receipt was sent through safe_send_json
        receipt_call = send.await_args
        assert receipt_call is not None
        payload = receipt_call.args[1]
        assert payload["type"] == "receipt"
        assert payload["model"] == "minimax/MiniMax-M2.5"
        assert payload["cost_mils"] == 0

        # Rich media emit fired with the joined response
        rich.assert_awaited_once()
        rich_kw = rich.await_args.kwargs
        assert rich_kw["response_text"] == "Hello world"
        assert rich_kw["log_label"] == "tc"

        # Keepalive called with label="tc_text"
        assert keepalive.calls == [{"label": "tc_text", "ws": ws}]

    @pytest.mark.asyncio
    async def test_ws_closed_before_thinking_skips_send(self):
        """If ws is closed before we emit the thinking indicator,
        we skip the send but still try to stream (the keepalive +
        token loop handle their own closed checks)."""
        ws = _make_ws(closed=True)
        send = _make_safe_send_json()
        cfg = _make_conn_config(backend="tinkerclaw")
        llm = _make_llm(tokens=[])  # no tokens
        convo = _make_conversation(llm)

        with patch(
            "dragon_voice.tinkerclaw_text_path.emit_rich_media_for_text_turn",
            new=AsyncMock(),
        ):
            handled = await handle_tinkerclaw_text_path(
                ws,
                conn_state={},
                conn_config=cfg,
                text="hi",
                session_id="s1",
                conversation=convo,
                media_pipeline=MagicMock(),
                ws_keepalive=_noop_keepalive,
                safe_send_json=send,
            )

        assert handled is True
        # No frames sent — ws was closed
        ws.send_json.assert_not_called()


# ─── TestSessionKey ─────────────────────────────────────────────


class TestSessionKey:
    @pytest.mark.asyncio
    async def test_session_key_set_when_supported(self):
        """LLM that implements SupportsSessionKey gets
        `set_session_key(session_id)` called."""
        from dragon_voice.llm.base import SupportsSessionKey

        class _SkLLM:
            name = "sk_llm"
            _model = "x"

            def set_session_key(self, key: str) -> None:
                self.last_key = key

            async def generate_stream_with_messages(self, messages):
                return
                yield  # pragma: no cover - empty generator

        llm = _SkLLM()
        assert isinstance(llm, SupportsSessionKey)

        ws = _make_ws()
        send = _make_safe_send_json()
        cfg = _make_conn_config(backend="tinkerclaw")
        convo = _make_conversation(llm)

        with patch(
            "dragon_voice.tinkerclaw_text_path.emit_rich_media_for_text_turn",
            new=AsyncMock(),
        ):
            await handle_tinkerclaw_text_path(
                ws,
                conn_state={"session_id": "sess-abc"},
                conn_config=cfg,
                text="hi",
                session_id="sess-abc",
                conversation=convo,
                media_pipeline=MagicMock(),
                ws_keepalive=_noop_keepalive,
                safe_send_json=send,
            )

        assert llm.last_key == "sess-abc"

    @pytest.mark.asyncio
    async def test_no_session_key_set_when_unsupported(self):
        """LLM lacking SupportsSessionKey does NOT get set_session_key
        called.  (Pins the Wave 21b isinstance switch — used to be
        a hasattr() that fired on backends like `dual` that exposed
        the method via __getattr__ accidentally.)"""

        class _NoSkLLM:
            name = "nosk"
            _model = "x"
            # explicitly NO set_session_key method

            async def generate_stream_with_messages(self, messages):
                return
                yield  # pragma: no cover

        llm = _NoSkLLM()
        ws = _make_ws()
        send = _make_safe_send_json()
        cfg = _make_conn_config(backend="tinkerclaw")
        convo = _make_conversation(llm)

        # The function returning True without an AttributeError IS
        # the pin — `set_session_key` was guarded by the isinstance
        # check.  No assertion on the missing method needed.
        with patch(
            "dragon_voice.tinkerclaw_text_path.emit_rich_media_for_text_turn",
            new=AsyncMock(),
        ):
            handled = await handle_tinkerclaw_text_path(
                ws,
                conn_state={"session_id": "s"},
                conn_config=cfg,
                text="hi",
                session_id="s",
                conversation=convo,
                media_pipeline=MagicMock(),
                ws_keepalive=_noop_keepalive,
                safe_send_json=send,
            )
        assert handled is True


# ─── TestDragonErrorFastFail ────────────────────────────────────


class TestDragonErrorFastFail:
    @pytest.mark.asyncio
    async def test_dragon_error_emits_structured_event_and_returns_true(self):
        """γ2-M6 (#106): TC gateway pre-flight fail → emit γ1
        error_event via safe_send_json + llm_done(0,"") via
        ws.send_json + return True (handled — caller MUST NOT
        fall through to local path)."""
        ws = _make_ws()
        send = _make_safe_send_json()
        cfg = _make_conn_config(backend="tinkerclaw")
        err = DragonError(
            "TinkerClaw gateway is offline",
            code="gateway_down",
            severity=Severity.FATAL,
            scope=Scope.LLM,
        )
        llm = _make_llm(raises=err)
        convo = _make_conversation(llm)

        handled = await handle_tinkerclaw_text_path(
            ws,
            conn_state={},
            conn_config=cfg,
            text="hi",
            session_id="s1",
            conversation=convo,
            media_pipeline=MagicMock(),
            ws_keepalive=_noop_keepalive,
            safe_send_json=send,
        )

        assert handled is True

        # Structured γ1 event went via safe_send_json
        send.assert_awaited_once()
        event_payload = send.await_args.args[1]
        # to_event() shape — at minimum has the error code
        assert event_payload.get("code") == "gateway_down"
        assert event_payload.get("severity") == Severity.FATAL.value

        # Then llm_done(0, "") went via ws.send_json (after the
        # opening empty thinking frame)
        sent_done = [
            c.args[0] for c in ws.send_json.call_args_list
            if c.args[0].get("type") == "llm_done"
        ]
        assert len(sent_done) == 1
        assert sent_done[0]["llm_ms"] == 0
        assert sent_done[0]["text"] == ""

    @pytest.mark.asyncio
    async def test_non_dragon_exception_does_not_swallow(self):
        """A plain RuntimeError MUST propagate — only DragonError
        gets the fast-fail wrap.  Caller's outer try/except handles
        unexpected failures."""
        ws = _make_ws()
        send = _make_safe_send_json()
        cfg = _make_conn_config(backend="tinkerclaw")
        llm = _make_llm(raises=RuntimeError("upstream blew up"))
        convo = _make_conversation(llm)

        with pytest.raises(RuntimeError, match="upstream blew up"):
            await handle_tinkerclaw_text_path(
                ws,
                conn_state={},
                conn_config=cfg,
                text="hi",
                session_id="s1",
                conversation=convo,
                media_pipeline=MagicMock(),
                ws_keepalive=_noop_keepalive,
                safe_send_json=send,
            )


# ─── TestEmptyResponseWrap ──────────────────────────────────────


class TestEmptyResponseWrap:
    @pytest.mark.asyncio
    async def test_empty_response_no_tools_fires_apology(self):
        """When LLM returned empty AND no tools fired → W15-H09
        apology synthesized + emitted via llm_done.text."""
        ws = _make_ws()
        send = _make_safe_send_json()
        cfg = _make_conn_config(backend="tinkerclaw")
        llm = _make_llm(tokens=[])  # zero tokens
        convo = _make_conversation(llm)

        with patch(
            "dragon_voice.tinkerclaw_text_path.emit_rich_media_for_text_turn",
            new=AsyncMock(),
        ):
            await handle_tinkerclaw_text_path(
                ws,
                conn_state={"tool_calls_this_turn": []},  # no tools
                conn_config=cfg,
                text="hi",
                session_id="s1",
                conversation=convo,
                media_pipeline=MagicMock(),
                ws_keepalive=_noop_keepalive,
                safe_send_json=send,
            )

        # llm_done.text carries the W15-H09 apology
        done_call = next(
            c for c in ws.send_json.call_args_list
            if c.args[0].get("type") == "llm_done"
        )
        assert "couldn't generate a response" in done_call.args[0]["text"]


# ─── TestRichMediaGate ──────────────────────────────────────────


class TestRichMediaGate:
    @pytest.mark.asyncio
    async def test_zero_tokens_skips_rich_media_emit(self):
        """`if full_response:` gate — zero LLM tokens means no rich
        media work to do.  The TC zero-cost receipt still fires."""
        ws = _make_ws()
        send = _make_safe_send_json()
        cfg = _make_conn_config(backend="tinkerclaw")
        llm = _make_llm(tokens=[])  # zero tokens
        convo = _make_conversation(llm)
        media_pipeline = MagicMock()

        with patch(
            "dragon_voice.tinkerclaw_text_path.emit_rich_media_for_text_turn",
            new=AsyncMock(),
        ) as rich:
            await handle_tinkerclaw_text_path(
                ws,
                conn_state={},
                conn_config=cfg,
                text="hi",
                session_id="s1",
                conversation=convo,
                media_pipeline=media_pipeline,
                ws_keepalive=_noop_keepalive,
                safe_send_json=send,
            )

        rich.assert_not_awaited()

        # But the TC zero-cost receipt still fires (sent via safe_send_json)
        assert send.await_count >= 1
        receipt = next(
            (c.args[1] for c in send.await_args_list
             if c.args[1].get("type") == "receipt"),
            None,
        )
        assert receipt is not None
        assert receipt["cost_mils"] == 0
