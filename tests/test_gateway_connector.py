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

from dragon_voice.channels.device_identity import (
    DeviceIdentity,
    _b64url_encode,
    _derive_raw_public_key,
    _fingerprint_public_key,
    _normalize_metadata,
    build_v3_payload,
    load_or_create_identity,
)
from dragon_voice.channels.gateway import (
    GatewayConnector,
    _extract_platform_id,
    _extract_recipient,
    _idem_key,
)


def _make_test_identity() -> DeviceIdentity:
    """Generate a throwaway identity without touching disk.

    Re-using ``load_or_create_identity`` here would persist to
    ``~/.dragon/identity/`` on the test runner, which is hostile to CI
    isolation and slow.  Generating in-memory keeps each test self-
    contained.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )
    priv = Ed25519PrivateKey.generate()
    pub_pem = priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    return DeviceIdentity(
        device_id=_fingerprint_public_key(pub_pem),
        public_key_pem=pub_pem,
        private_key_pem=priv_pem,
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
        self.last_challenge_nonce: str = ""
        self.connect_count: int = 0
        self.send_count: int = 0

    def _default_send_handler(
        self, params: dict
    ) -> tuple[bool, Optional[dict], str]:
        return True, {"messageId": f"fake-{uuid.uuid4().hex[:8]}"}, ""

    async def handle(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        # W7-F.4: gateway sends connect.challenge immediately after WS
        # open — the client uses that nonce in its device.nonce field.
        # Record what we issued so tests can check the connector echoed it.
        self.last_challenge_nonce = f"test-nonce-{uuid.uuid4().hex[:8]}"
        await ws.send_json({
            "type": "event",
            "event": "connect.challenge",
            "payload": {"nonce": self.last_challenge_nonce, "ts": 0},
        })
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
            # Injected throwaway identity — keeps the test from writing
            # to ``~/.dragon/identity/``.
            identity=_make_test_identity(),
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
            assert cp["role"] == "operator"
            # `send` requires WRITE scope per openclaw method-scopes.ts;
            # the connector includes WRITE alongside READ + ADMIN so
            # ancillary probes don't fail the gate.
            assert "operator.write" in cp["scopes"]
            # client.id must be one of the OpenClaw enum values; the
            # generic backend default is "gateway-client".
            assert cp["client"]["id"] == "test-client"  # set in _make_connector
            # W7-F.4 device identity present + signature shape sane.
            dev = cp["device"]
            assert isinstance(dev["id"], str) and len(dev["id"]) == 64  # sha256 hex
            assert isinstance(dev["publicKey"], str) and len(dev["publicKey"]) > 30
            assert isinstance(dev["signature"], str) and len(dev["signature"]) > 60
            assert isinstance(dev["signedAt"], int) and dev["signedAt"] > 0
            # The nonce MUST echo the server-issued connect.challenge nonce,
            # not a client-generated one.  Pre-W7-F.4 the connector
            # generated its own nonce → gateway rejected "device nonce mismatch".
            assert dev["nonce"] == self.gateway.last_challenge_nonce
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
            GatewayConnector(token="", identity=_make_test_identity())
        assert "token" in str(exc.value).lower()

    def test_whitespace_token_accepted_then_strips(self) -> None:
        # The constructor itself doesn't strip — that's the operator's
        # job in config wiring.  Make sure we at least don't crash on
        # an unusual but technically non-empty token.
        connector = GatewayConnector(
            token="  abc  ", identity=_make_test_identity(),
        )
        assert connector is not None


# ── W7-F.4: device_identity helpers ─────────────────────────────────


class TestBuildV3Payload:
    """Canonical pipe-delimited payload — must match OpenClaw byte-for-byte."""

    def test_basic_shape(self) -> None:
        out = build_v3_payload(
            device_id="abc123",
            client_id="gateway-client",
            client_mode="backend",
            role="operator",
            scopes=["operator.read", "operator.write"],
            signed_at_ms=1700000000000,
            token="tok",
            nonce="n1",
            platform="linux",
            device_family="",
        )
        assert out == (
            "v3|abc123|gateway-client|backend|operator|"
            "operator.read,operator.write|1700000000000|tok|n1|linux|"
        )

    def test_token_none_becomes_empty(self) -> None:
        out = build_v3_payload(
            device_id="d", client_id="c", client_mode="m", role="r",
            scopes=[], signed_at_ms=0, token=None, nonce="n",
            platform=None, device_family=None,
        )
        # Empty scopes -> empty segment; None token -> empty.
        assert "||" in out  # token segment is empty

    def test_scopes_comma_joined_no_spaces(self) -> None:
        out = build_v3_payload(
            device_id="d", client_id="c", client_mode="m", role="r",
            scopes=["a", "b", "c"], signed_at_ms=1, token="t", nonce="n",
        )
        assert "|a,b,c|" in out

    def test_metadata_lowercased(self) -> None:
        out = build_v3_payload(
            device_id="d", client_id="c", client_mode="m", role="r",
            scopes=[], signed_at_ms=1, token="t", nonce="n",
            platform="LINUX", device_family="X86_64",
        )
        # Trailing fields: platform + device_family, both lowercased.
        parts = out.rsplit("|", 2)
        assert parts[-2] == "linux"
        assert parts[-1] == "x86_64"


class TestDeviceIdentityHelpers:
    """Identity round-trip + crypto helpers match OpenClaw's TS encoding."""

    def test_device_id_is_sha256_hex_of_raw_public_key(self) -> None:
        ident = _make_test_identity()
        import hashlib
        raw = _derive_raw_public_key(ident.public_key_pem)
        assert len(raw) == 32  # ed25519 public key is 32 bytes
        assert ident.device_id == hashlib.sha256(raw).hexdigest()

    def test_public_key_b64url_strips_padding(self) -> None:
        ident = _make_test_identity()
        encoded = ident.public_key_b64url()
        assert "=" not in encoded
        assert "+" not in encoded
        assert "/" not in encoded
        # 32 raw bytes → 43-char base64url (no padding)
        assert len(encoded) == 43

    def test_signature_verifies_against_payload(self) -> None:
        """Round-trip: sign with our DeviceIdentity, verify with cryptography."""
        from cryptography.hazmat.primitives import serialization
        ident = _make_test_identity()
        payload = "v3|fake|fake|backend|operator|x|1|tok|n||"
        sig_b64u = ident.sign(payload)
        # Decode signature (add padding for stdlib decoder)
        import base64
        pad = "=" * ((4 - len(sig_b64u) % 4) % 4)
        sig = base64.urlsafe_b64decode(sig_b64u + pad)
        assert len(sig) == 64  # ed25519 signatures are 64 bytes
        # Verify with the public key (mirrors gateway's verifyDeviceSignature).
        pub = serialization.load_pem_public_key(ident.public_key_pem.encode())
        pub.verify(sig, payload.encode("utf-8"))  # raises if bad

    def test_b64url_encoder_matches_known_vector(self) -> None:
        # Known test vector from RFC 4648 §10 examples (URL-safe variant).
        assert _b64url_encode(b"") == ""
        assert _b64url_encode(b"f") == "Zg"
        assert _b64url_encode(b"foobar") == "Zm9vYmFy"

    def test_normalize_metadata_handles_none_and_whitespace(self) -> None:
        assert _normalize_metadata(None) == ""
        assert _normalize_metadata("") == ""
        assert _normalize_metadata("   ") == ""
        assert _normalize_metadata("  Linux  ") == "linux"


class TestLoadOrCreateIdentityPersistence:
    """Persisted identity survives a process restart at the same path."""

    def test_first_call_creates_file(self, tmp_path: Any) -> None:
        path = str(tmp_path / "device.json")
        ident = load_or_create_identity(path=path)
        import os
        assert os.path.exists(path)
        # File permissions are 0o600 (best-effort).
        mode = os.stat(path).st_mode & 0o777
        assert mode == 0o600
        assert len(ident.device_id) == 64
        assert "BEGIN PRIVATE KEY" in ident.private_key_pem
        assert "BEGIN PUBLIC KEY" in ident.public_key_pem

    def test_second_call_returns_same_identity(self, tmp_path: Any) -> None:
        path = str(tmp_path / "device.json")
        a = load_or_create_identity(path=path)
        b = load_or_create_identity(path=path)
        assert a.device_id == b.device_id
        assert a.public_key_pem == b.public_key_pem
        assert a.private_key_pem == b.private_key_pem

    def test_corrupt_file_regenerates(self, tmp_path: Any) -> None:
        path = str(tmp_path / "device.json")
        # Write garbage that's neither valid JSON nor matches schema.
        with open(path, "w") as f:
            f.write("{this is not JSON")
        ident = load_or_create_identity(path=path)
        # Should succeed by regenerating, not raise.
        assert len(ident.device_id) == 64


