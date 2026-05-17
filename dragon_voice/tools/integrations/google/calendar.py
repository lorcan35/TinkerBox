"""Google Calendar integration — auth-code-with-PKCE flow (#342).

OAuth state machine:

  start_connect()
    → creates AuthCodeChallenge (PKCE pair + state)
    → stashes verifier + asyncio.Future in `self._flows[state]`
    → returns ConnectChallenge with the authorization_url
       (Tab5 shows QR + plain link)

  user opens auth URL on phone, signs in, grants scopes

  Google redirects to /api/v1/oauth/callback?code=…&state=…
    → IntegrationRoutes.oauth_callback handler calls
       integration.handle_callback(state, code, error)
    → we exchange code+verifier for tokens, persist, mark future done

  Tab5 polls /api/v1/integrations/google-calendar/status/{request_id}
    → poll_status() reads the flow's future state, returns connected/expired/error
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

import aiohttp

from dragon_voice.tools.integrations.base import (
    ConnectChallenge,
    ConnectionStatus,
    IntegrationBackend,
)
from dragon_voice.tools.integrations.credentials import CredentialStore
from dragon_voice.tools.integrations.google.auth import (
    GoogleOAuthConfig,
    OAuthTokens,
    SCOPE_CALENDAR_EVENTS,
    SCOPE_CALENDAR_READ,
    make_google_client,
    revoke_google_token,
)
from dragon_voice.tools.integrations.oauth import DeviceCodeError
from dragon_voice.tools.integrations.registry import register_integration

logger = logging.getLogger(__name__)

CALENDAR_API_BASE = "https://www.googleapis.com/calendar/v3"


@register_integration("google-calendar")
class GoogleCalendarIntegration(IntegrationBackend):
    """Google Calendar read + write via Calendar API v3."""

    _SCOPES = [SCOPE_CALENDAR_READ, SCOPE_CALENDAR_EVENTS]

    def __init__(self) -> None:
        self._store = CredentialStore("google-calendar")
        # request_id → in-flight flow.  Survives across REST calls so
        # Tab5 polling sees terminal state.
        self._flows: dict[str, "_AuthFlow"] = {}
        self._tokens: Optional[OAuthTokens] = None
        self._tokens_loaded = False

    # ── IntegrationBackend interface ────────────────────────────

    @property
    def name(self) -> str:
        return "google-calendar"

    @property
    def display_name(self) -> str:
        return "Google Calendar"

    @property
    def description(self) -> str:
        return "Read and create events on your primary Google Calendar."

    @property
    def auth_kind(self) -> Literal["oauth-device", "oauth-authcode", "static-token", "none"]:  # type: ignore[override]
        return "oauth-authcode"

    async def is_connected(self) -> bool:
        await self._ensure_tokens_loaded()
        return self._tokens is not None

    async def start_connect(self, params: Optional[dict] = None) -> ConnectChallenge:
        del params  # unused for auth-code flow
        cfg = GoogleOAuthConfig.from_env(scopes=self._SCOPES)
        request_id = uuid.uuid4().hex
        # Build the auth URL.  Google needs access_type=offline +
        # prompt=consent to mint a refresh_token on the first turn.
        client = make_google_client(cfg)
        chal = client.start(extra_params={
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
        })
        # `client.start` adds the entry to client._pending[state]
        # already, but we don't own that lifecycle — Dragon process
        # holds the entry until the callback resolves it.  We DO need
        # our own flow record so poll_status finds it.
        flow = _AuthFlow(
            request_id=request_id,
            state=chal.state,
            code_verifier=chal.code_verifier,
            cfg=cfg,
        )
        self._flows[request_id] = flow

        return ConnectChallenge(
            kind="oauth-device",  # Tab5 modal reuses the QR/code UX shape
            request_id=request_id,
            verification_url=chal.authorization_url,
            user_code=None,  # auth-code flow doesn't have one
            expires_in_s=600,
            interval_s=2,
        )

    async def poll_status(self, request_id: str) -> ConnectionStatus:
        flow = self._flows.get(request_id)
        if flow is None:
            return ConnectionStatus(
                state="error", request_id=request_id, error="unknown request_id",
            )
        if flow.tokens is not None:
            return ConnectionStatus(state="connected", request_id=request_id)
        if flow.error is not None:
            state_label = "expired" if flow.error.code == "expired_token" else "error"
            return ConnectionStatus(
                state=state_label,  # type: ignore[arg-type]
                request_id=request_id,
                error=flow.error.description,
            )
        if time.time() > flow.expires_at:
            flow.error = DeviceCodeError(
                "expired_token", "user did not complete authorization in time",
            )
            return ConnectionStatus(
                state="expired", request_id=request_id, error=flow.error.description,
            )
        return ConnectionStatus(state="connecting", request_id=request_id)

    async def handle_callback(
        self, state: str, code: Optional[str], error: Optional[str],
    ) -> None:
        """Called by the /api/v1/oauth/callback route handler.

        Looks up the flow by `state`, exchanges code+verifier for
        tokens, persists.  Best-effort: any exception becomes
        `flow.error` so the next poll_status returns it.
        """
        flow = self._find_flow_by_state(state)
        if flow is None:
            logger.warning("OAuth callback for unknown state %r", state[:12])
            return
        if error:
            flow.error = DeviceCodeError(code=error, description=error)
            return
        if not code:
            flow.error = DeviceCodeError(
                "missing_code", "callback missing both code and error",
            )
            return
        try:
            async with make_google_client(flow.cfg) as client:
                tokens = await client.exchange_code_with_verifier(
                    code=code, code_verifier=flow.code_verifier,
                )
        except DeviceCodeError as e:
            flow.error = e
            return
        except Exception as e:  # noqa: BLE001
            flow.error = DeviceCodeError(
                code="exchange_failed",
                description=f"{type(e).__name__}: {e}"[:120],
            )
            return
        await self._persist_tokens(tokens)
        flow.tokens = tokens

    async def disconnect(self) -> None:
        await self._ensure_tokens_loaded()
        if self._tokens is not None and self._tokens.refresh_token:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
            ) as session:
                await revoke_google_token(self._tokens.refresh_token, session)
        await self._store.delete()
        self._tokens = None
        self._tokens_loaded = True

    async def health_check(self, timeout_s: float = 5.0) -> tuple[bool, str]:
        await self._ensure_tokens_loaded()
        if self._tokens is None:
            return False, "not connected"
        try:
            data = await self._authed_get(
                f"{CALENDAR_API_BASE}/calendars/primary",
                timeout_s=timeout_s,
            )
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            return False, f"{type(e).__name__}: {e}"[:120]
        except DeviceCodeError as e:
            return False, f"auth: {e.description[:120]}"
        cal_id = data.get("id") if isinstance(data, dict) else None
        return True, f"connected to {cal_id or 'primary calendar'}"

    # ── Calendar API used by tools ──────────────────────────────

    async def list_events(
        self,
        time_min: Optional[datetime] = None,
        time_max: Optional[datetime] = None,
        max_results: int = 10,
    ) -> list[dict[str, Any]]:
        params = {
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": str(max_results),
        }
        if time_min is not None:
            params["timeMin"] = time_min.astimezone(timezone.utc).isoformat()
        else:
            params["timeMin"] = datetime.now(timezone.utc).isoformat()
        if time_max is not None:
            params["timeMax"] = time_max.astimezone(timezone.utc).isoformat()
        raw = await self._authed_get(
            f"{CALENDAR_API_BASE}/calendars/primary/events", params=params,
        )
        items = raw.get("items", []) if isinstance(raw, dict) else []
        return [self._normalize_event(it) for it in items]

    async def create_event(
        self,
        summary: str,
        start: datetime,
        end: datetime,
        location: Optional[str] = None,
        description: Optional[str] = None,
    ) -> dict[str, Any]:
        body = {
            "summary": summary,
            "start": {"dateTime": start.astimezone(timezone.utc).isoformat()},
            "end": {"dateTime": end.astimezone(timezone.utc).isoformat()},
        }
        if location:
            body["location"] = location
        if description:
            body["description"] = description
        raw = await self._authed_post(
            f"{CALENDAR_API_BASE}/calendars/primary/events", body=body,
        )
        return self._normalize_event(raw)

    async def cancel_event(self, event_id: str) -> bool:
        try:
            await self._authed_delete(
                f"{CALENDAR_API_BASE}/calendars/primary/events/{event_id}",
            )
        except aiohttp.ClientResponseError as e:
            logger.warning("cancel_event %s failed: %s", event_id, e)
            return False
        return True

    # ── Internals ───────────────────────────────────────────────

    def _find_flow_by_state(self, state: str) -> Optional["_AuthFlow"]:
        for flow in self._flows.values():
            if flow.state == state:
                return flow
        return None

    async def _persist_tokens(self, tokens: OAuthTokens) -> None:
        await self._store.save({"tokens": tokens.to_dict()})
        self._tokens = tokens
        self._tokens_loaded = True

    async def _ensure_tokens_loaded(self) -> None:
        if self._tokens_loaded:
            return
        data = await self._store.load()
        if data and "tokens" in data:
            try:
                self._tokens = OAuthTokens.from_dict(data["tokens"])
            except (KeyError, ValueError, TypeError) as e:
                logger.warning(
                    "Stored Google Calendar tokens malformed (%s) — "
                    "treating as disconnected.",
                    e,
                )
                self._tokens = None
        else:
            self._tokens = None
        self._tokens_loaded = True

    async def _get_access_token(self) -> str:
        await self._ensure_tokens_loaded()
        if self._tokens is None:
            raise DeviceCodeError(
                "not_connected",
                "Google Calendar is not connected.  Tap Settings → "
                "Integrations → Connect Google on the Tab5.",
            )
        if not self._tokens.is_expired():
            return self._tokens.access_token
        if not self._tokens.refresh_token:
            raise DeviceCodeError(
                "needs_reauth",
                "No refresh token — please reconnect Google.",
            )
        cfg = GoogleOAuthConfig.from_env(scopes=self._tokens.scopes or self._SCOPES)
        async with make_google_client(cfg) as client:
            new_tokens = await client.refresh(self._tokens.refresh_token)
        await self._persist_tokens(new_tokens)
        return new_tokens.access_token

    async def _authed_get(
        self,
        url: str,
        params: Optional[dict] = None,
        timeout_s: float = 15.0,
    ) -> Any:
        token = await self._get_access_token()
        timeout = aiohttp.ClientTimeout(total=timeout_s)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            ) as resp:
                if resp.status == 401:
                    await self._force_refresh()
                    token = await self._get_access_token()
                    async with session.get(
                        url,
                        params=params,
                        headers={"Authorization": f"Bearer {token}"},
                    ) as resp2:
                        resp2.raise_for_status()
                        return await resp2.json()
                resp.raise_for_status()
                return await resp.json()

    async def _authed_post(self, url: str, body: dict) -> Any:
        token = await self._get_access_token()
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
        ) as session:
            async with session.post(
                url,
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            ) as resp:
                resp.raise_for_status()
                return await resp.json()

    async def _authed_delete(self, url: str) -> None:
        token = await self._get_access_token()
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
        ) as session:
            async with session.delete(
                url,
                headers={"Authorization": f"Bearer {token}"},
            ) as resp:
                resp.raise_for_status()

    async def _force_refresh(self) -> None:
        if self._tokens is None or not self._tokens.refresh_token:
            return
        self._tokens.expires_at = int(time.time()) - 1

    @staticmethod
    def _normalize_event(raw: dict) -> dict[str, Any]:
        start = raw.get("start", {})
        end = raw.get("end", {})
        all_day = "date" in start and "dateTime" not in start
        return {
            "id": raw.get("id"),
            "summary": raw.get("summary", "(no title)"),
            "location": raw.get("location"),
            "start_iso": start.get("dateTime") or start.get("date"),
            "end_iso": end.get("dateTime") or end.get("date"),
            "all_day": all_day,
            "attendees": [
                a.get("email") for a in raw.get("attendees", [])
                if isinstance(a, dict) and a.get("email")
            ],
            "hangout_link": raw.get("hangoutLink"),
            "html_link": raw.get("htmlLink"),
        }


class _AuthFlow:
    """Internal: in-flight auth-code flow state."""

    __slots__ = (
        "request_id", "state", "code_verifier", "cfg",
        "expires_at", "tokens", "error",
    )

    def __init__(
        self,
        *,
        request_id: str,
        state: str,
        code_verifier: str,
        cfg: GoogleOAuthConfig,
    ) -> None:
        self.request_id = request_id
        self.state = state
        self.code_verifier = code_verifier
        self.cfg = cfg
        self.expires_at = time.time() + 600
        self.tokens: Optional[OAuthTokens] = None
        self.error: Optional[DeviceCodeError] = None
