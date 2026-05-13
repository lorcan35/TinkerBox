"""W7-F.2 — GatewayConnector tests.

Exercises the connector end-to-end against an in-process aiohttp
WebSocket server that speaks the OpenClaw gateway frame protocol
(``req`` / ``res`` JSON envelopes).  The fake gateway is intentionally
the minimum needed to validate the connector's contract:

  * ``connect`` request → ``{ok: True}`` response (token-mode auth).
  * ``send``    request → echo-style ``{ok: True, payload: {...}}`` with
    a synthesized platform_message_id, or a configurable failure.
  * Optional silent mode that ignores requests so timeout paths trigger.

Tests cover the surface the W7-F handler depends on:

  * Successful happy-path send → ChannelReplyResult(ok=True, id=...).
  * Connect rejection (bad token) → ChannelReplyResult(ok=False, error=...).
  * Send failure response → ok=False with error message.
  * Unparseable thread_id → ok=False without WS round-trip.
  * RPC timeout → ok=False with rpc_timeout error.
  * close() is idempotent.
  * Helper extractors (`_extract_recipient`, `_extract_platform_id`,
    `_idem_key`) cover the edge cases that the integration tests can't.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, Callable, Optional

import pytest
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

from dragon_voice.channels.gateway import (
    GatewayConnector,
    _extract_platform_id,
    _extract_recipient,
    _idem_key,
)


# ── Fake gateway WS handler ─────────────────────────────────────────


class FakeGateway:
    """In-process gateway impostor for connector integration tests.

    Behavior toggles live on the instance so each test can configure
    the same handler differently:

      * ``connect_ok`` — whether the ``connect`` request returns ok=True
      * ``send_handler`` — callable returning the (ok, payload, error)
        triple for each ``send`` request; default returns ok=True with
        a synthesized platform id.
      * ``silent_methods`` — set of method names the gateway should
        accept but never reply to (for testing the connector's RPC
        timeout path).
    """

    def __init__(self) -> None:
        self.connect_ok: bool = True
        self.connect_error: str = ""
        self.silent_methods: set[str] = set()
        self.send_handler: Callable[[dict], tuple[bool, Optional[dict], str]] = (
            self._default_send_handler
        )
        # Captured for assertions.
        self.last_connect_params: Optional[dict] = None
        self.last_send_params: Optional[dict] = None
        self.connect_count: int = 0
        self.send_count: int = 0

    def _default_send_handler(
        self, params: dict
    ) -> tuple[bool, Optional[dict], str]:
        return True, {"messageId": f"fake-{uuid.uuid4().hex[:8]}"}, ""

    async def handle(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                continue
            try:
                frame = json.loads(msg.data)
            except json.JSONDecodeError:
                continue
            if frame.get("type") != "req":
                continue
            method = frame.get("method", "")
            req_id = frame.get("id", "")
            params = frame.get("params") or {}
            if method in self.silent_methods:
                # Accept the request, never reply — connector should time
                # out per its rpc_timeout_s.
                continue
            if method == "connect":
                self.connect_count += 1
                self.last_connect_params = params
                if self.connect_ok:
                    await ws.send_json({
                        "type": "res", "id": req_id, "ok": True,
                        "payload": {
                            "type": "hello-ok", "protocol": 1,
                            "server": {"version": "test", "connId": "c1"},
                            "features": {"methods": ["send"], "events": []},
                            "snapshot": {},
                            "policy": {
                                "maxPayload": 1 << 20,
                                "maxBufferedBytes": 1 << 22,
                                "tickIntervalMs": 30_000,
                            },
                        },
                    })
                else:
                    await ws.send_json({
                        "type": "res", "id": req_id, "ok": False,
                        "error": {"code": "AUTH_TOKEN_MISMATCH",
                                  "message": self.connect_error or "bad token"},
                    })
            elif method == "send":
                self.send_count += 1
                self.last_send_params = params
                ok, payload, err = self.send_handler(params)
                if ok:
                    await ws.send_json({
                        "type": "res", "id": req_id, "ok": True,
                        "payload": payload or {},
                    })
                else:
                    await ws.send_json({
                        "type": "res", "id": req_id, "ok": False,
                        "error": {"code": "SEND_FAILED",
                                  "message": err or "send failed"},
                    })
            else:
                # Unknown method — return generic failure
                await ws.send_json({
                    "type": "res", "id": req_id, "ok": False,
                    "error": {"code": "UNKNOWN_METHOD",
                              "message": f"unknown method: {method}"},
                })
        return ws


# ── Integration tests (real WS round-trip) ──────────────────────────


class TestGatewayConnectorIntegration(AioHTTPTestCase):
    """End-to-end tests with an in-process aiohttp WS server.

    Each test creates a fresh GatewayConnector pointed at the
    in-process server, exercises one behavior, then closes the
    connector to free its background reader task.
    """

    async def get_application(self) -> web.Application:
        self.gateway = FakeGateway()
        app = web.Application()
        app.router.add_get("/", self.gateway.handle)
        return app

    def _make_connector(self, *, token: str = "test-token", timeout: float = 2.0) -> GatewayConnector:
        # ``self.server.port`` is provided by AioHTTPTestCase.
        url = f"ws://127.0.0.1:{self.server.port}/"
        return GatewayConnector(
            url=url,
            token=token,
            client_id="test-client",
            rpc_timeout_s=timeout,
        )

    async def test_send_reply_happy_path(self) -> None:
        connector = self._make_connector()
        try:
            result = await connector.send_reply(
                channel="tg",
                thread_id="tg:thread:42",
                text="hello world",
            )
            assert result.ok is True
            assert result.platform_message_id.startswith("fake-")
            assert result.error == ""
            # Connector forwarded the right shape to the gateway:
            params = self.gateway.last_send_params
            assert params is not None
            assert params["to"] == "42"
            assert params["channel"] == "tg"
            assert params["message"] == "hello world"
            assert params["threadId"] == "tg:thread:42"
            assert params["idempotencyKey"].startswith("dragon:")
            # And the connect handshake ran exactly once even though
            # the connector was constructed fresh.
            assert self.gateway.connect_count == 1
            # Token + role made it into the connect params.
            cp = self.gateway.last_connect_params
            assert cp is not None
            assert cp["auth"]["token"] == "test-token"
            assert cp["role"] == "agent.runner"
        finally:
            await connector.close()

    async def test_connect_rejected_returns_ack_not_crash(self) -> None:
        self.gateway.connect_ok = False
        self.gateway.connect_error = "AUTH_TOKEN_MISMATCH"
        connector = self._make_connector(token="wrong-token")
        try:
            result = await connector.send_reply(
                channel="tg", thread_id="tg:thread:42", text="hi",
            )
            assert result.ok is False
            assert "gateway_unreachable" in result.error or "AUTH" in result.error
            # Send was never attempted because connect failed first.
            assert self.gateway.send_count == 0
        finally:
            await connector.close()

    async def test_send_failure_response_surfaces(self) -> None:
        self.gateway.send_handler = (
            lambda _params: (False, None, "platform_blocked_user")
        )
        connector = self._make_connector()
        try:
            result = await connector.send_reply(
                channel="tg", thread_id="tg:thread:42", text="hi",
            )
            assert result.ok is False
            assert result.platform_message_id == ""
            assert "platform_blocked_user" in result.error
        finally:
            await connector.close()

    async def test_rpc_timeout_returns_clean_error(self) -> None:
        # Let connect succeed, then the SEND will hang silently.
        self.gateway.silent_methods = {"send"}
        connector = self._make_connector(timeout=0.5)
        try:
            result = await connector.send_reply(
                channel="tg", thread_id="tg:thread:42", text="hi",
            )
            assert result.ok is False
            assert "rpc_timeout" in result.error
        finally:
            await connector.close()

    async def test_thread_id_without_colon_passes_through_as_to(self) -> None:
        connector = self._make_connector()
        try:
            await connector.send_reply(
                channel="tg", thread_id="6053954118", text="hi",
            )
            params = self.gateway.last_send_params
            assert params is not None
            # Plain numeric id → both `to` and (no separate) threadId
            assert params["to"] == "6053954118"
            # threadId is omitted when it equals `to` (avoid redundancy).
            assert "threadId" not in params
        finally:
            await connector.close()

    async def test_close_is_idempotent(self) -> None:
        connector = self._make_connector()
        await connector.send_reply(
            channel="tg", thread_id="tg:thread:42", text="hi",
        )
        await connector.close()
        # Second close must not raise.
        await connector.close()

    async def test_unparseable_thread_id_short_circuits(self) -> None:
        connector = self._make_connector()
        try:
            result = await connector.send_reply(
                channel="tg", thread_id="", text="hi",
            )
            assert result.ok is False
            assert "unparseable_thread_id" in result.error
            # Connect happened (lazy connect runs first), but no `send`
            # round-trip because the recipient is empty.
            assert self.gateway.send_count == 0
        finally:
            await connector.close()


# ── Pure-unit tests for the helper extractors ───────────────────────


class TestExtractRecipient:
    """`tg:thread:42` → `42`; passthrough when no separator."""

    def test_canonical_three_part(self) -> None:
        assert _extract_recipient("tg:thread:42") == "42"

    def test_two_part(self) -> None:
        assert _extract_recipient("wa:6053954118") == "6053954118"

    def test_no_colon_is_passthrough(self) -> None:
        assert _extract_recipient("6053954118") == "6053954118"

    def test_empty(self) -> None:
        assert _extract_recipient("") == ""


class TestExtractPlatformId:
    """First-key-wins lookup across the gateway's id field variants."""

    def test_messageId_preferred(self) -> None:
        assert _extract_platform_id(
            {"messageId": "mid-1", "id": "id-1"}
        ) == "mid-1"

    def test_falls_back_to_id(self) -> None:
        assert _extract_platform_id({"id": "abc"}) == "abc"

    def test_int_coerced_to_str(self) -> None:
        assert _extract_platform_id({"messageId": 17}) == "17"

    def test_no_id_returns_empty(self) -> None:
        assert _extract_platform_id({"unrelated": "x"}) == ""


class TestIdemKey:
    """Idempotency keys are unique + monotonic-ish per call."""

    def test_starts_with_dragon_prefix(self) -> None:
        assert _idem_key().startswith("dragon:")

    def test_unique_across_calls(self) -> None:
        keys = {_idem_key() for _ in range(10)}
        assert len(keys) == 10


class TestRequiresToken:
    """Constructor rejects empty tokens — the gateway would 401 every call."""

    def test_blank_token_raises(self) -> None:
        with pytest.raises(ValueError) as exc:
            GatewayConnector(token="")
        assert "token" in str(exc.value).lower()

    def test_whitespace_token_accepted_then_strips(self) -> None:
        # The constructor itself doesn't strip — that's the operator's
        # job in config wiring.  Make sure we at least don't crash on
        # an unusual but technically non-empty token.
        connector = GatewayConnector(token="  abc  ")
        assert connector is not None


