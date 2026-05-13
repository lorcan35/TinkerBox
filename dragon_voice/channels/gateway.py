"""OpenClaw gateway WS-RPC connector — W7-F.2.

Persistent WebSocket to the OpenClaw gateway (boots on ``localhost:18789``)
that forwards Tab5-originated `channel_reply` text to a real messaging
platform (Telegram, WhatsApp, Discord, …) by invoking the gateway's
``send`` RPC method.

Auth model: plain bearer token in the ``connect`` request's
``auth.token``.  No HMAC device-identity handshake — the gateway runs
as a sibling process on the same Dragon machine and the WS bind is
loopback-only (see TinkerBox CLAUDE.md ▸ Service Map, port 18789
localhost-only).  Anything that can reach 127.0.0.1:18789 already has
root-equivalent on the device.

Wire frames (see openclaw protocol/schema/frames.ts):
  Client → Server   ``{"type":"req", "id":"<uuid>", "method":"<name>", "params":{...}}``
  Server → Client   ``{"type":"res", "id":"<uuid>", "ok":bool, "payload":?, "error":?}``
  Server → Client   ``{"type":"event", "event":"<name>", "payload":?}``  (ignored here)

Per-call flow:
  1. ``send_reply`` calls ``_ensure_connected`` (lazy open + handshake).
  2. Builds a ``send`` RPC ``req`` with a fresh idempotency key.
  3. Awaits the matching ``res``; maps payload/error → ChannelReplyResult.

We deliberately keep this connector *minimal*:
  * One persistent WS, single in-flight queue, no client-side retry.
  * No outbound events emitted (the gateway pushes events for incoming
    platform messages — those are W7-G's concern, not the reply path).
  * Reconnect = drop + lazy reconnect on next ``send_reply``; we do
    not transparently retry an in-flight send because the caller
    (channel_reply_handler) already ACKs the user via Tab5 and a
    retried send could double-deliver.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

import aiohttp

from dragon_voice.channels.base import ChannelReplyResult
from dragon_voice.channels.device_identity import (
    DeviceIdentity,
    build_v3_payload,
    load_or_create_identity,
)

logger = logging.getLogger(__name__)

# OpenClaw gateway protocol negotiation window.  The server enforces
# ``maxProtocol >= PROTOCOL_VERSION >= minProtocol`` (see
# ``openclaw/src/gateway/protocol/schema/protocol-schemas.ts``).
# Quote a generous range so a server bump within the [MIN, MAX] window
# doesn't require a connector redeploy.  ``MAX`` should be raised in
# lockstep with the gateway when the wire shape changes incompatibly;
# ``MIN`` stays at 1 (the lowest server-supported version).
GATEWAY_PROTOCOL_MIN = 1
GATEWAY_PROTOCOL_MAX = 3

# Per-RPC budget.  Sends to Telegram/WhatsApp/etc. round-trip in
# <1 s on the happy path; 15 s leaves comfortable headroom for cold
# auth probes (first send after the gateway boots can be slower while
# the channel plugin warms up).
DEFAULT_RPC_TIMEOUT_S = 15.0

# WS ping interval.  aiohttp's heartbeat=N sends a ping every N seconds
# and treats a missing pong as the connection being dead.  30 s is well
# under typical NAT/firewall idle timeouts (60-300 s) and matches the
# gateway's own tick interval.
WS_HEARTBEAT_S = 30.0

# W7-F.5: Tab5 emits two-letter channel ids (the same short names that
# show up in NVS keys + UI labels: ch_tg_on, "[tg] Alice", …).  The
# OpenClaw gateway plugin registry resolves channels by their
# canonical plugin id (``telegram``, ``whatsapp``, …) with no built-in
# aliases for the two-letter forms.  Map at the connector boundary so
# Tab5's short names work end-to-end without firmware churn.
#
# Unknown ids fall through unchanged — keeps "telegram" or "wa-foo" or
# any future plugin id callers might pass working without a code
# change, while still translating the common Tab5 vocabulary.
_TAB5_CHANNEL_ALIASES = {
    "tg": "telegram",
    "wa": "whatsapp",
    "dc": "discord",
    "sl": "slack",
    "sg": "signal",
    "im": "imessage",
    "ma": "matrix",
    "em": "email",
}


@dataclass
class _RpcResult:
    """Internal result envelope for a single RPC round-trip."""

    ok: bool
    payload: Any = None
    error: str = ""
    error_code: str = ""


@dataclass
class _PendingCall:
    """Tracks one in-flight request by its frame ``id``."""

    future: asyncio.Future
    method: str
    sent_at: float = field(default_factory=time.monotonic)


class GatewayConnector:
    """WS-RPC connector to a loopback OpenClaw gateway.

    Public surface mirrors the ``ChannelConnector`` Protocol (see
    ``channels/base.py``) — one async ``send_reply`` returning a
    ``ChannelReplyResult``.  Construct with the gateway URL + bearer
    token; the actual WS connection is opened lazily on first call so
    Dragon can instantiate this at startup without blocking boot if
    the gateway is briefly unreachable.

    Thread-safety: one connector instance is meant to be used by the
    asyncio loop that owns it.  All public methods are coroutines;
    there are no synchronous accessors that could race.
    """

    def __init__(
        self,
        *,
        url: str = "ws://127.0.0.1:18789",
        token: str,
        client_id: str = "gateway-client",
        client_version: str = "0.1.0",
        rpc_timeout_s: float = DEFAULT_RPC_TIMEOUT_S,
        session: Optional[aiohttp.ClientSession] = None,
        identity: Optional[DeviceIdentity] = None,
    ) -> None:
        if not token:
            raise ValueError(
                "GatewayConnector requires a bearer token — set "
                "channel_gateway.token in config.yaml or "
                "CHANNEL_GATEWAY_TOKEN in env."
            )
        self._url = url
        self._token = token
        self._client_id = client_id
        self._client_version = client_version
        self._rpc_timeout_s = rpc_timeout_s
        # W7-F.4: ed25519 device identity that lets the gateway grant the
        # requested scopes without a manual pairing approval (loopback
        # backend-self-pairing path — see device_identity.py docstring).
        # Tests may inject a pre-built identity to avoid filesystem
        # writes; production callers pass None and we load (or generate)
        # the persistent ``~/.dragon/identity/device.json`` keypair.
        self._identity = identity if identity is not None else load_or_create_identity()

        # Allow tests to inject a shared aiohttp session so they don't
        # have to spin up a real TCP listener.  Production callers
        # leave this None and we create our own.
        self._owned_session = session is None
        self._session: Optional[aiohttp.ClientSession] = session
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._pending: dict[str, _PendingCall] = {}
        self._connect_lock = asyncio.Lock()
        self._connected = False
        # W7-F.4: gateway sends ``event: connect.challenge`` with a nonce
        # immediately after the WS opens; the device.nonce field in our
        # connect-req must echo that exact nonce or the gateway rejects
        # the handshake with "device nonce mismatch".  The reader loop
        # fulfills this future when the challenge arrives; the handshake
        # waits on it before signing the v3 payload.
        self._connect_challenge_nonce: Optional[asyncio.Future[str]] = None

    # ── public API ──────────────────────────────────────────────────

    async def send_reply(
        self,
        channel: str,
        thread_id: str,
        text: str,
        in_reply_to: str = "",
    ) -> ChannelReplyResult:
        """Forward a single reply via the gateway ``send`` method."""
        try:
            await self._ensure_connected()
        except Exception as e:  # noqa: BLE001 — surface to ACK, not crash
            logger.warning("gateway: connect failed for send_reply: %s", e)
            return ChannelReplyResult(
                ok=False,
                platform_message_id="",
                error=f"gateway_unreachable: {e}",
            )

        to = _extract_recipient(thread_id)
        if not to:
            return ChannelReplyResult(
                ok=False,
                platform_message_id="",
                error=f"unparseable_thread_id: {thread_id!r}",
            )

        # W7-F.5: Tab5 sends two-letter short ids; gateway expects the
        # canonical plugin id (telegram/whatsapp/...).  Translate at
        # the connector boundary; unknown ids pass through unchanged.
        gateway_channel = _TAB5_CHANNEL_ALIASES.get(channel.strip().lower(), channel)

        params: dict[str, Any] = {
            "to": to,
            "channel": gateway_channel,
            "message": text,
            "idempotencyKey": _idem_key(),
        }
        # Pass thread_id through so the gateway can group replies in
        # the same conversation (Telegram message_thread_id, Discord
        # thread, etc.).  Optional in the gateway schema.
        if thread_id and thread_id != to:
            params["threadId"] = thread_id

        result = await self._rpc("send", params)
        if not result.ok:
            return ChannelReplyResult(
                ok=False,
                platform_message_id="",
                error=result.error or "send_failed",
            )

        payload = result.payload or {}
        platform_id = _extract_platform_id(payload)
        return ChannelReplyResult(
            ok=True,
            platform_message_id=platform_id,
        )

    async def close(self) -> None:
        """Tear down the WS + any pending RPCs.  Idempotent."""
        if self._reader_task is not None and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._reader_task = None
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        self._ws = None
        if self._owned_session and self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None
        self._connected = False
        self._fail_pending("connector closed")

    # ── connect / handshake ─────────────────────────────────────────

    async def _ensure_connected(self) -> None:
        if self._connected and self._ws is not None and not self._ws.closed:
            return
        async with self._connect_lock:
            if self._connected and self._ws is not None and not self._ws.closed:
                return
            await self._connect_locked()

    async def _connect_locked(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._owned_session = True
        # W7-F.4: prepare the connect-challenge future BEFORE opening the
        # WS so the reader's dispatch path can always fulfill it without
        # racing with our `await` below.
        loop = asyncio.get_event_loop()
        self._connect_challenge_nonce = loop.create_future()
        self._ws = await self._session.ws_connect(
            self._url, heartbeat=WS_HEARTBEAT_S,
        )
        # Start the reader BEFORE the handshake so the connect response
        # — and the connect.challenge event that PRECEDES it — has a
        # future waiting for it.
        self._reader_task = asyncio.create_task(self._reader_loop())
        try:
            await self._handshake()
        except Exception:
            # Handshake failed — tear the connection down so the next
            # send_reply gets a clean lazy-reconnect attempt.
            await self._teardown_after_failure()
            raise
        self._connected = True
        logger.info(
            "GatewayConnector: connected to %s as %s",
            self._url, self._client_id,
        )

    async def _handshake(self) -> None:
        # W7-F.3 + W7-F.4: connect-param contract for the OpenClaw gateway.
        #   * role / client.id / scopes / protocol — covered in W7-F.3.
        #   * device — W7-F.4: the ed25519 identity binds the connect frame
        #                so the gateway's scope-grant rule
        #                (``message-handler.ts:L510-512``) doesn't clear
        #                the self-declared scopes.  Loopback + gateway-client
        #                + backend mode + token auth additionally satisfies
        #                ``shouldSkipBackendSelfPairing`` in
        #                handshake-auth-helpers.ts, so no manual pairing
        #                approval is needed for a fresh keypair.
        #   * nonce  — W7-F.4: gateway sends a connect.challenge event
        #                immediately after WS open with a server-issued
        #                nonce; the device.nonce field MUST echo that
        #                value (``message-handler.ts:L611``) or the gateway
        #                rejects with "device nonce mismatch".
        scopes = ["operator.read", "operator.write", "operator.admin"]
        role = "operator"
        client_mode = "backend"
        platform = "linux"
        device_family: str = ""
        signed_at_ms = int(time.time() * 1000)
        # Wait up to half the RPC budget for the connect.challenge event —
        # cheap event from the server, but bound the wait so a silent or
        # broken gateway doesn't hang send_reply forever.
        assert self._connect_challenge_nonce is not None
        try:
            nonce = await asyncio.wait_for(
                self._connect_challenge_nonce,
                timeout=max(2.0, self._rpc_timeout_s / 2),
            )
        except asyncio.TimeoutError as e:
            raise RuntimeError("gateway never sent connect.challenge") from e
        payload = build_v3_payload(
            device_id=self._identity.device_id,
            client_id=self._client_id,
            client_mode=client_mode,
            role=role,
            scopes=scopes,
            signed_at_ms=signed_at_ms,
            token=self._token,
            nonce=nonce,
            platform=platform,
            device_family=device_family,
        )
        signature = self._identity.sign(payload)
        connect_params = {
            "minProtocol": GATEWAY_PROTOCOL_MIN,
            "maxProtocol": GATEWAY_PROTOCOL_MAX,
            "client": {
                "id": self._client_id,
                "version": self._client_version,
                "platform": platform,
                "mode": client_mode,
            },
            "role": role,
            "scopes": scopes,
            "auth": {"token": self._token},
            "caps": [],
            "device": {
                "id": self._identity.device_id,
                "publicKey": self._identity.public_key_b64url(),
                "signature": signature,
                "signedAt": signed_at_ms,
                "nonce": nonce,
            },
        }
        result = await self._rpc("connect", connect_params, ensure_connected=False)
        if not result.ok:
            raise RuntimeError(
                f"gateway connect rejected: {result.error or result.error_code or 'unknown'}"
            )

    async def _teardown_after_failure(self) -> None:
        if self._reader_task is not None and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._reader_task = None
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        self._ws = None
        self._connected = False
        self._fail_pending("connect failed")

    # ── RPC core ────────────────────────────────────────────────────

    async def _rpc(
        self,
        method: str,
        params: dict,
        *,
        ensure_connected: bool = True,
    ) -> _RpcResult:
        if ensure_connected:
            await self._ensure_connected()
        if self._ws is None or self._ws.closed:
            return _RpcResult(ok=False, error="ws_not_open")

        req_id = uuid.uuid4().hex
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[req_id] = _PendingCall(future=fut, method=method)

        try:
            await self._ws.send_json(
                {"type": "req", "id": req_id, "method": method, "params": params},
            )
        except (aiohttp.ClientError, ConnectionError) as e:
            self._pending.pop(req_id, None)
            return _RpcResult(ok=False, error=f"send_failed: {e}")

        try:
            return await asyncio.wait_for(fut, timeout=self._rpc_timeout_s)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            return _RpcResult(ok=False, error="rpc_timeout")

    # ── reader loop ─────────────────────────────────────────────────

    async def _reader_loop(self) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    self._handle_text_frame(msg.data)
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    # Gateway uses JSON-only frames; binaries are unexpected.
                    logger.debug("gateway: ignoring binary frame (%d bytes)", len(msg.data))
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.ERROR,
                ):
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("gateway: reader loop crashed")
        finally:
            self._connected = False
            self._fail_pending("gateway disconnected")

    def _handle_text_frame(self, data: str) -> None:
        try:
            frame = json.loads(data)
        except json.JSONDecodeError:
            logger.warning("gateway: bad JSON frame: %.120s", data)
            return
        frame_type = frame.get("type")
        if frame_type == "res":
            self._dispatch_response(frame)
        elif frame_type == "event":
            # W7-F.4: the very first event after WS open is
            # ``connect.challenge`` carrying the server-issued nonce
            # that our device.nonce field must echo.  Hand it to the
            # handshake's pending future.
            if frame.get("event") == "connect.challenge":
                payload = frame.get("payload") or {}
                challenge_nonce = payload.get("nonce")
                if (
                    isinstance(challenge_nonce, str)
                    and challenge_nonce
                    and self._connect_challenge_nonce is not None
                    and not self._connect_challenge_nonce.done()
                ):
                    self._connect_challenge_nonce.set_result(challenge_nonce.strip())
                return
            # W7-G will handle inbound platform messages.  For now,
            # log + drop so the WS stays healthy.
            logger.debug(
                "gateway: dropping event=%s payload=%.120s",
                frame.get("event"), str(frame.get("payload")),
            )
        else:
            logger.debug("gateway: ignoring frame type=%s", frame_type)

    def _dispatch_response(self, frame: dict) -> None:
        req_id = frame.get("id")
        pending = self._pending.pop(req_id, None) if req_id else None
        if pending is None or pending.future.done():
            return
        ok = bool(frame.get("ok"))
        err = frame.get("error") or {}
        pending.future.set_result(
            _RpcResult(
                ok=ok,
                payload=frame.get("payload"),
                error=str(err.get("message", "")),
                error_code=str(err.get("code", "")),
            )
        )

    def _fail_pending(self, reason: str) -> None:
        for pending in list(self._pending.values()):
            if not pending.future.done():
                pending.future.set_result(
                    _RpcResult(ok=False, error=reason),
                )
        self._pending.clear()


# ── helpers ─────────────────────────────────────────────────────────


def _idem_key() -> str:
    """Idempotency key for a single ``send`` invocation.

    Gateway's dedupe ring keys on this — same key within the ring's
    TTL returns the cached result.  Format: ``dragon:<epoch-ms>:<rand>``.
    """
    return f"dragon:{int(time.time() * 1000)}:{secrets.token_hex(4)}"


def _extract_recipient(thread_id: str) -> str:
    """Pull the platform recipient out of Tab5's thread_id.

    Tab5 emits thread ids in the form ``<channel>:<kind>:<id>`` —
    e.g. ``tg:thread:42``, ``wa:chat:6053954118``.  The platform
    recipient is the last colon-segment.  If the thread_id has no
    colons we treat it as already-resolved and pass it through.
    """
    if not thread_id:
        return ""
    if ":" not in thread_id:
        return thread_id
    return thread_id.rsplit(":", 1)[-1]


def _extract_platform_id(payload: dict) -> str:
    """Best-effort: find the platform-specific message id in a send res.

    Different gateway channel plugins return the id under different
    keys (``messageId``, ``id``, ``platform_message_id``).  We try
    them in order; falling back to an empty string is fine — the
    user-facing ACK can still report ``ok=true`` without an id.
    """
    for key in ("messageId", "platform_message_id", "id", "platformMessageId"):
        val = payload.get(key)
        if isinstance(val, (str, int)) and str(val).strip():
            return str(val)
    return ""
