"""OAuth 2.0 Device Authorization Grant (RFC 8628) client (#341).

Dragon runs headless — there's no browser, no local web server with a
redirect URI for the standard OAuth code flow.  The device
authorization grant is designed exactly for this case:

  1. Client (Dragon) hits the provider's device-code endpoint, gets
     back a `device_code`, a `user_code`, and a `verification_url`.
  2. Dragon shows the user the URL + code (Tab5 renders as QR).
  3. User completes the OAuth dance on their phone.
  4. Dragon polls the token endpoint with `device_code` until the
     provider returns access + refresh tokens.

Supported by Google, Spotify, GitHub, and many others.

This module is provider-agnostic — provider-specific bits
(client_id, scopes, endpoint URLs) are passed in.  See
``google/auth.py`` for the Google-specific wrapper.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)


class DeviceCodeError(Exception):
    """Raised when the device-code flow fails terminally.

    Distinct from `aiohttp.ClientError` so callers can catch only the
    flow-specific errors (`expired_token`, `access_denied`, etc.).
    """

    def __init__(self, code: str, description: str) -> None:
        self.code = code
        self.description = description
        super().__init__(f"{code}: {description}")


@dataclass
class DeviceCodeChallenge:
    """The triple returned by the device-code endpoint.

    All fields per RFC 8628 §3.2.  `interval` is the recommended poll
    interval (seconds); providers can ask the client to back off by
    returning `slow_down` from the token endpoint, but we honor the
    initial `interval` unless slow_down arrives.
    """

    device_code: str
    user_code: str
    verification_url: str
    expires_in: int
    interval: int = 5
    # Some providers (Google) also return verification_url_complete
    # which embeds the user_code as a query param — convenient for QR.
    verification_url_complete: Optional[str] = None


@dataclass
class OAuthTokens:
    """Token bundle returned by the token endpoint.

    `expires_at` is an absolute unix timestamp so refresh logic doesn't
    need to track issue time separately.  `refresh_token` may be None
    if the provider doesn't issue one (rare for device flow).  `scopes`
    is the granted scope set returned by the provider — may differ from
    requested if the user denied some scopes.
    """

    access_token: str
    refresh_token: Optional[str]
    token_type: str
    expires_at: int  # absolute unix timestamp
    scopes: list[str]
    extra: dict = None  # type: ignore[assignment]

    def is_expired(self, skew_s: int = 60) -> bool:
        """Treat the token as expired `skew_s` seconds before its
        actual expiry to avoid in-flight 401s during a refresh window.
        """
        return time.time() + skew_s >= self.expires_at

    def to_dict(self) -> dict:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "token_type": self.token_type,
            "expires_at": self.expires_at,
            "scopes": self.scopes,
            "extra": self.extra or {},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "OAuthTokens":
        return cls(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            token_type=data.get("token_type", "Bearer"),
            expires_at=int(data["expires_at"]),
            scopes=list(data.get("scopes", [])),
            extra=dict(data.get("extra", {})),
        )


class OAuthDeviceCodeClient:
    """Provider-agnostic device-code OAuth client.

    One instance per provider (Google, Spotify, etc.) — the
    `client_id`, `scope`, `device_code_url`, and `token_url` are
    provider-specific.  `client_secret` is optional (Google's device
    flow uses a client secret, Spotify's doesn't).
    """

    def __init__(
        self,
        *,
        client_id: str,
        scope: str,
        device_code_url: str,
        token_url: str,
        client_secret: Optional[str] = None,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._scope = scope
        self._device_code_url = device_code_url
        self._token_url = token_url
        self._session = session
        self._owns_session = session is None

    async def __aenter__(self) -> "OAuthDeviceCodeClient":
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
            )
            self._owns_session = True
        return self

    async def __aexit__(self, *exc_info) -> None:  # noqa: ANN001
        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()

    async def start(self) -> DeviceCodeChallenge:
        """POST to the device-code endpoint, return the challenge.

        Raises DeviceCodeError when the provider returns a non-2xx
        response with a structured error body (rare at this stage —
        usually only happens if the `client_id` is invalid).
        """
        assert self._session is not None, "Use 'async with OAuthDeviceCodeClient(...)'"
        payload = {"client_id": self._client_id, "scope": self._scope}
        async with self._session.post(self._device_code_url, data=payload) as resp:
            data = await resp.json()
            if resp.status != 200:
                raise DeviceCodeError(
                    code=data.get("error", "unknown_error"),
                    description=data.get(
                        "error_description", "Failed to start device-code flow",
                    ),
                )
        return DeviceCodeChallenge(
            device_code=data["device_code"],
            user_code=data["user_code"],
            verification_url=data["verification_url"],
            verification_url_complete=data.get("verification_url_complete"),
            expires_in=int(data.get("expires_in", 1800)),
            interval=int(data.get("interval", 5)),
        )

    async def poll_once(self, device_code: str) -> Optional[OAuthTokens]:
        """One poll of the token endpoint.

        Returns:
            * `OAuthTokens` — flow completed, user authorized.
            * `None` — still pending (RFC 8628 `authorization_pending`
              or `slow_down`).  Caller should sleep and try again.

        Raises:
            DeviceCodeError — terminal failure (`expired_token`,
            `access_denied`, etc.).  Caller stops polling.
        """
        assert self._session is not None, "Use 'async with OAuthDeviceCodeClient(...)'"
        payload = {
            "client_id": self._client_id,
            "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        }
        if self._client_secret:
            payload["client_secret"] = self._client_secret

        async with self._session.post(self._token_url, data=payload) as resp:
            data = await resp.json()
            if resp.status == 200:
                return self._tokens_from_response(data)
            err = data.get("error", "")
            if err in ("authorization_pending", "slow_down"):
                # Not done yet.
                return None
            raise DeviceCodeError(
                code=err or "unknown_error",
                description=data.get(
                    "error_description", "Device-code poll failed"
                ),
            )

    async def poll_until_done(
        self,
        challenge: DeviceCodeChallenge,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> OAuthTokens:
        """Convenience: poll on `challenge.interval` until terminal.

        Honors `expires_in` from the challenge — raises
        ``DeviceCodeError("expired_token", ...)`` when the device-code
        TTL is up.  Cancel via `cancel_event` (Tab5 closing the modal).
        """
        deadline = time.time() + challenge.expires_in
        interval = challenge.interval
        while True:
            if cancel_event and cancel_event.is_set():
                raise DeviceCodeError("cancelled", "user cancelled flow")
            if time.time() >= deadline:
                raise DeviceCodeError(
                    "expired_token", "device-code expired before user completed flow"
                )
            try:
                tokens = await self.poll_once(challenge.device_code)
            except DeviceCodeError as e:
                if e.code == "slow_down":
                    # Provider asked us to back off; bump interval.
                    interval += 5
                    await asyncio.sleep(interval)
                    continue
                # Anything else is terminal.
                raise
            if tokens is not None:
                return tokens
            await asyncio.sleep(interval)

    async def refresh(self, refresh_token: str) -> OAuthTokens:
        """Exchange a refresh token for a new access token.

        Raises DeviceCodeError on terminal failure (refresh token
        revoked / expired — user has to reconnect).
        """
        assert self._session is not None, "Use 'async with OAuthDeviceCodeClient(...)'"
        payload = {
            "client_id": self._client_id,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
        if self._client_secret:
            payload["client_secret"] = self._client_secret

        async with self._session.post(self._token_url, data=payload) as resp:
            data = await resp.json()
            if resp.status != 200:
                raise DeviceCodeError(
                    code=data.get("error", "refresh_failed"),
                    description=data.get(
                        "error_description", "Refresh token rejected"
                    ),
                )
        # Refresh responses may omit `refresh_token` — providers expect
        # the client to reuse the old one.
        tokens = self._tokens_from_response(data)
        if not tokens.refresh_token:
            tokens.refresh_token = refresh_token
        return tokens

    @staticmethod
    def _tokens_from_response(data: dict) -> OAuthTokens:
        expires_in = int(data.get("expires_in", 3600))
        scope_str = data.get("scope", "")
        scopes = scope_str.split() if scope_str else []
        return OAuthTokens(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            token_type=data.get("token_type", "Bearer"),
            expires_at=int(time.time()) + expires_in,
            scopes=scopes,
            extra={
                k: v for k, v in data.items()
                if k not in {
                    "access_token", "refresh_token", "token_type",
                    "expires_in", "scope",
                }
            },
        )
