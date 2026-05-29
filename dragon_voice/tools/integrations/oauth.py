"""OAuth 2.0 helpers for TinkerBox integrations (#341).

Two flows supported:

* **Authorization Code with PKCE** (RFC 7636) — used for Google
  Calendar / Gmail / most modern providers.  Dragon hosts a public
  ``/api/v1/oauth/callback`` route (reached via the existing ngrok
  tunnel) so the user can complete consent on their phone browser.
  No client-secret required for PKCE, but Google's web-app client
  ships with one — we include it when present.

* **Device Authorization Grant** (RFC 8628) — used by providers that
  support it without scope restrictions (some Spotify scopes, etc.).
  Dragon polls the token endpoint; user enters a code on their phone.
  KEPT for future integrations that prefer it.

Both flows produce the same ``OAuthTokens`` dataclass.

### Why we pivoted from device-code to auth-code for Google

Google's device-code flow restricts the allowed scopes to a small set
(email/profile/openid/Drive/Photos/YouTube).  Calendar + Gmail are NOT
in that list — Google forces these scopes through the
authorization-code flow.  See:
https://developers.google.com/identity/protocols/oauth2/limited-input-device#allowedscopes
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import secrets
import time
import urllib.parse
from dataclasses import dataclass
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)


class DeviceCodeError(Exception):
    """Raised when an OAuth flow fails terminally.  Name kept for
    backward compat with the existing API + tests; covers both
    device-code and auth-code failures."""

    def __init__(self, code: str, description: str) -> None:
        self.code = code
        self.description = description
        super().__init__(f"{code}: {description}")


# ─── Tokens ────────────────────────────────────────────────────────


@dataclass
class OAuthTokens:
    """Provider-agnostic token bundle.

    `expires_at` is an absolute unix timestamp.  `refresh_token` may
    be None when the provider doesn't issue one or when a refresh
    response omitted it (callers preserve the prior value in that
    case)."""

    access_token: str
    refresh_token: Optional[str]
    token_type: str
    expires_at: int
    scopes: list[str]
    extra: dict = None  # type: ignore[assignment]

    def is_expired(self, skew_s: int = 60) -> bool:
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


# ─── PKCE helpers (RFC 7636) ──────────────────────────────────────


def _pkce_pair() -> tuple[str, str]:
    """Generate a (code_verifier, code_challenge) pair.

    code_verifier:  43-128 char [A-Za-z0-9-._~] random string.
    code_challenge: BASE64URL-NOPAD(SHA256(code_verifier)).
    """
    verifier = secrets.token_urlsafe(64)  # ~86 chars after urlsafe
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


# ─── Authorization Code with PKCE ─────────────────────────────────


@dataclass
class AuthCodeChallenge:
    """What `OAuthAuthCodeClient.start` returns.

    Tab5 displays a QR with `authorization_url`; user opens it on
    their phone, signs in + consents; Google redirects to our
    ngrok-tunneled callback URL; the callback handler calls
    `OAuthAuthCodeClient.exchange_code(state, code)` to mint tokens.
    """

    authorization_url: str
    state: str
    code_verifier: str
    expires_in: int = 600  # PKCE state TTL; we expire after 10 min


class OAuthAuthCodeClient:
    """Authorization Code flow with PKCE.

    One instance per provider.  Holds the in-flight PKCE state
    (`state` → `code_verifier`) so the callback can find it.

    Typical use:

        client = OAuthAuthCodeClient(
            client_id=..., client_secret=..., scope=...,
            auth_url="https://accounts.google.com/o/oauth2/v2/auth",
            token_url="https://oauth2.googleapis.com/token",
            redirect_uri="https://tinkerclaw-voice.ngrok.dev/api/v1/oauth/callback",
        )
        chal = await client.start(extra_params={"access_type": "offline", "prompt": "consent"})
        # ... Tab5 shows QR, user authorizes ...
        # callback receives ?code=...&state=...
        tokens = await client.exchange_code(state=..., code=...)
    """

    def __init__(
        self,
        *,
        client_id: str,
        scope: str,
        auth_url: str,
        token_url: str,
        redirect_uri: str,
        client_secret: Optional[str] = None,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._scope = scope
        self._auth_url = auth_url
        self._token_url = token_url
        self._redirect_uri = redirect_uri
        self._session = session
        self._owns_session = session is None
        # state -> {code_verifier, expires_at, future, completed}
        self._pending: dict[str, dict] = {}

    async def __aenter__(self) -> "OAuthAuthCodeClient":
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
            )
            self._owns_session = True
        return self

    async def __aexit__(self, *exc_info) -> None:  # noqa: ANN001
        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()

    def start(self, extra_params: Optional[dict] = None) -> AuthCodeChallenge:
        """Generate PKCE pair + return the authorization URL.

        Doesn't hit the network — just builds the URL.  Caller passes
        provider-specific extras (e.g. Google needs
        `access_type=offline` + `prompt=consent` to mint a
        refresh_token on the first turn).
        """
        verifier, challenge = _pkce_pair()
        state = secrets.token_urlsafe(24)
        params = {
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "response_type": "code",
            "scope": self._scope,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
        if extra_params:
            params.update(extra_params)
        url = f"{self._auth_url}?{urllib.parse.urlencode(params)}"
        # The waiter's future is created lazily in wait_for_callback() on the
        # running loop that actually awaits it.  start() is a synchronous
        # URL-builder that may be called with no event loop at all (py3.12
        # made asyncio.get_event_loop() raise in that case), and a future
        # must live on the loop that awaits it — so we defer creation.  If
        # the provider callback beats the waiter, resolve_callback() buffers
        # the outcome for wait_for_callback() to honor immediately.
        self._pending[state] = {
            "code_verifier": verifier,
            "expires_at": time.time() + 600,
            "future": None,
        }
        return AuthCodeChallenge(
            authorization_url=url,
            state=state,
            code_verifier=verifier,
        )

    def resolve_callback(self, state: str, code: Optional[str], error: Optional[str]) -> None:
        """Called from the HTTP callback handler.  Wakes the
        `wait_for_callback` future."""
        entry = self._pending.get(state)
        if entry is None:
            logger.warning("OAuth callback for unknown state %r — ignoring", state[:12])
            return
        future = entry.get("future")
        if future is None:
            # Callback arrived before wait_for_callback() parked — buffer the
            # outcome so the waiter can honor it as soon as it starts waiting.
            entry["pending_code"] = code
            entry["pending_error"] = error
            return
        if future.done():
            return
        if error:
            future.set_exception(DeviceCodeError(code=error, description=error))
        else:
            future.set_result(code)

    async def wait_for_callback(self, state: str, timeout_s: int = 600) -> str:
        """Block until the callback completes the flow for this state.

        Returns the auth code on success; raises DeviceCodeError on
        provider-side denial or timeout.
        """
        entry = self._pending.get(state)
        if entry is None:
            raise DeviceCodeError("unknown_state", "no pending flow for state")
        # If the callback already resolved before we parked, honor it now.
        if "pending_code" in entry or "pending_error" in entry:
            self._pending.pop(state, None)
            err = entry.get("pending_error")
            if err:
                raise DeviceCodeError(code=err, description=err)
            return entry.get("pending_code")
        future = asyncio.get_running_loop().create_future()
        entry["future"] = future
        try:
            code = await asyncio.wait_for(future, timeout=timeout_s)
            return code
        except asyncio.TimeoutError:
            raise DeviceCodeError(
                "expired_token",
                "user did not complete authorization in time",
            ) from None
        finally:
            # GC the entry — only one shot per state.
            self._pending.pop(state, None)

    async def exchange_code(self, state: str, code: str) -> OAuthTokens:
        """Exchange the auth code + PKCE verifier for tokens.

        The state was looked up to find the verifier; the
        `wait_for_callback` future has already been resolved (and
        the entry removed) — so we capture the verifier BEFORE
        calling resolve_callback or use the explicit verifier the
        caller passes.  In practice this method is called from inside
        ``GoogleCalendarIntegration._handle_callback`` which has both.
        """
        assert self._session is not None, "Use 'async with OAuthAuthCodeClient(...)'"
        # Caller is expected to pass the verifier directly (we no
        # longer have the state entry once wait_for_callback returned)
        # — so this method actually doesn't need state.  But we keep
        # the parameter for API symmetry; callers can pass any value.
        del state
        return await self._exchange(code)

    async def _exchange(self, code: str, code_verifier: str | None = None) -> OAuthTokens:
        """Used internally by exchange_code_with_verifier."""
        assert self._session is not None
        payload = {
            "client_id": self._client_id,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": self._redirect_uri,
        }
        if code_verifier:
            payload["code_verifier"] = code_verifier
        if self._client_secret:
            payload["client_secret"] = self._client_secret
        async with self._session.post(self._token_url, data=payload) as resp:
            data = await resp.json()
            if resp.status != 200:
                raise DeviceCodeError(
                    code=data.get("error", "exchange_failed"),
                    description=data.get(
                        "error_description", "Code exchange rejected by provider",
                    ),
                )
        return _tokens_from_response(data)

    async def exchange_code_with_verifier(
        self, code: str, code_verifier: str,
    ) -> OAuthTokens:
        """The canonical exchange call — pass verifier explicitly."""
        assert self._session is not None
        return await self._exchange(code, code_verifier=code_verifier)

    async def refresh(self, refresh_token: str) -> OAuthTokens:
        """Exchange a refresh token for a new access token."""
        assert self._session is not None
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
        tokens = _tokens_from_response(data)
        if not tokens.refresh_token:
            tokens.refresh_token = refresh_token
        return tokens


# ─── Device Authorization Grant (kept for non-Google providers) ───


@dataclass
class DeviceCodeChallenge:
    """Returned by the device-code endpoint (RFC 8628 §3.2)."""

    device_code: str
    user_code: str
    verification_url: str
    expires_in: int
    interval: int = 5
    verification_url_complete: Optional[str] = None


class OAuthDeviceCodeClient:
    """Provider-agnostic device-code OAuth client.

    Kept for providers that allow Calendar/Gmail-equivalent scopes
    through device-code (some Spotify scopes, etc.).  See
    `OAuthAuthCodeClient` for Google.
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
        assert self._session is not None
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
        assert self._session is not None
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
                return _tokens_from_response(data)
            err = data.get("error", "")
            if err in ("authorization_pending", "slow_down"):
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
                    interval += 5
                    await asyncio.sleep(interval)
                    continue
                raise
            if tokens is not None:
                return tokens
            await asyncio.sleep(interval)

    async def refresh(self, refresh_token: str) -> OAuthTokens:
        assert self._session is not None
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
        tokens = _tokens_from_response(data)
        if not tokens.refresh_token:
            tokens.refresh_token = refresh_token
        return tokens
