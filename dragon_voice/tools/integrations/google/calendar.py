"""Google Calendar integration — first proof-of-concept (#341 / #342).

Implements `IntegrationBackend` using the shared Google device-code
auth (`google/auth.py`) and the Calendar v3 REST API.

Public methods used by tools:
  * `list_events(time_min, time_max, max_results)` → list[dict]
  * `create_event(...)` → dict
  * `cancel_event(event_id)` → bool

OAuth state lives in `~/.tinkerclaw/integrations/google-calendar.json`
managed by the shared `CredentialStore`.  The cred file holds the
OAuthTokens dict + the active OAuth config snapshot (so we know which
scopes were granted).
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
    GoogleOAuthNotConfiguredError,
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

    # Scopes we request when initiating connect.  Read + write events
    # on the user's primary calendar — both fit in a single consent
    # screen so the device-flow UX stays one-step.
    _SCOPES = [SCOPE_CALENDAR_READ, SCOPE_CALENDAR_EVENTS]

    def __init__(self) -> None:
        self._store = CredentialStore("google-calendar")
        # In-flight device-code flows, keyed by request_id.  A flow
        # lives in this dict from `start_connect` to either successful
        # `poll_status` (terminal `connected`) or expiry/cancellation.
        self._flows: dict[str, _DeviceFlow] = {}
        # Lazily-loaded token cache; written through to `_store` on
        # every refresh.  None means "not loaded yet" — load on first
        # use.
        self._tokens: Optional[OAuthTokens] = None
        self._tokens_loaded = False
        # Background flow tasks — keep handles so we don't drop them
        # (RUF006 lint catches asyncio-dangling-task).
        self._tasks: set[asyncio.Task] = set()

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
    def auth_kind(self) -> Literal["oauth-device", "static-token", "none"]:
        return "oauth-device"

    async def is_connected(self) -> bool:
        await self._ensure_tokens_loaded()
        return self._tokens is not None

    async def start_connect(self, params: Optional[dict] = None) -> ConnectChallenge:
        del params  # unused for OAuth device flow
        try:
            cfg = GoogleOAuthConfig.from_env(scopes=self._SCOPES)
        except GoogleOAuthNotConfiguredError:
            raise

        request_id = uuid.uuid4().hex
        flow = _DeviceFlow(request_id=request_id, cfg=cfg)
        self._flows[request_id] = flow

        # Kick off the device-code flow.  `start` returns the
        # challenge synchronously; `poll_until_done` runs in a
        # background task and parks the result on the flow object so
        # `poll_status` can read it without re-issuing requests.
        flow.challenge = await flow.start()
        flow.poll_task = asyncio.create_task(self._run_flow(flow))
        self._tasks.add(flow.poll_task)
        flow.poll_task.add_done_callback(self._tasks.discard)

        return ConnectChallenge(
            kind="oauth-device",
            request_id=request_id,
            verification_url=(
                flow.challenge.verification_url_complete
                or flow.challenge.verification_url
            ),
            user_code=flow.challenge.user_code,
            expires_in_s=flow.challenge.expires_in,
            interval_s=flow.challenge.interval,
        )

    async def poll_status(self, request_id: str) -> ConnectionStatus:
        flow = self._flows.get(request_id)
        if flow is None:
            return ConnectionStatus(
                state="error",
                request_id=request_id,
                error="unknown request_id",
            )
        if flow.tokens is not None:
            return ConnectionStatus(state="connected", request_id=request_id)
        if flow.error is not None:
            state = "expired" if flow.error.code == "expired_token" else "error"
            return ConnectionStatus(
                state=state,  # type: ignore[arg-type]
                request_id=request_id,
                error=flow.error.description,
            )
        return ConnectionStatus(state="connecting", request_id=request_id)

    async def disconnect(self) -> None:
        await self._ensure_tokens_loaded()
        if self._tokens is not None and self._tokens.refresh_token:
            # Best-effort revoke — delete local creds regardless.
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
            ) as session:
                await revoke_google_token(self._tokens.refresh_token, session)
        await self._store.delete()
        self._tokens = None
        self._tokens_loaded = True  # stays loaded (just empty)

    async def health_check(self, timeout_s: float = 5.0) -> tuple[bool, str]:
        await self._ensure_tokens_loaded()
        if self._tokens is None:
            return False, "not connected"
        try:
            events = await self._authed_get(
                f"{CALENDAR_API_BASE}/calendars/primary",
                timeout_s=timeout_s,
            )
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            return False, f"{type(e).__name__}: {e}"[:120]
        except DeviceCodeError as e:
            return False, f"auth: {e.description[:120]}"
        cal_id = events.get("id") if isinstance(events, dict) else None
        return True, f"connected to {cal_id or 'primary calendar'}"

    # ── Calendar-specific public API used by the tool wrappers ──

    async def list_events(
        self,
        time_min: Optional[datetime] = None,
        time_max: Optional[datetime] = None,
        max_results: int = 10,
    ) -> list[dict[str, Any]]:
        """List events on the user's primary calendar between
        `time_min` and `time_max`.  Defaults to "from now, no upper
        bound, 10 results."

        Returns a normalized list of event dicts:
          {id, summary, location, start_iso, end_iso, all_day, attendees, hangout_link}
        """
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
        """Create an event on the primary calendar."""
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
        """Delete an event from the primary calendar.  Returns True
        on success."""
        try:
            await self._authed_delete(
                f"{CALENDAR_API_BASE}/calendars/primary/events/{event_id}",
            )
        except aiohttp.ClientResponseError as e:
            logger.warning("cancel_event %s failed: %s", event_id, e)
            return False
        return True

    # ── Internals ───────────────────────────────────────────────

    async def _run_flow(self, flow: "_DeviceFlow") -> None:
        """Background coroutine: poll until tokens or terminal error,
        then persist tokens to the store."""
        try:
            async with make_google_client(flow.cfg) as client:
                tokens = await client.poll_until_done(flow.challenge)
            await self._persist_tokens(tokens)
            flow.tokens = tokens
        except DeviceCodeError as e:
            flow.error = e
        except Exception as e:  # noqa: BLE001
            flow.error = DeviceCodeError(
                code="poll_failed",
                description=f"{type(e).__name__}: {e}"[:120],
            )

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
                    "treating as disconnected.  User should reconnect.",
                    e,
                )
                self._tokens = None
        else:
            self._tokens = None
        self._tokens_loaded = True

    async def _get_access_token(self) -> str:
        """Return a valid access token, refreshing if necessary."""
        await self._ensure_tokens_loaded()
        if self._tokens is None:
            raise DeviceCodeError(
                code="not_connected",
                description="Google Calendar is not connected.  Visit Tab5 "
                "Settings → Integrations → Connect Google.",
            )
        if not self._tokens.is_expired():
            return self._tokens.access_token
        # Refresh.
        if not self._tokens.refresh_token:
            raise DeviceCodeError(
                code="needs_reauth",
                description="No refresh token — please reconnect Google.",
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
                    # Token rejected — try one refresh, then retry once.
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
        """Force a refresh even if our cached `expires_at` says it's
        still good.  Used when the API returned 401 unexpectedly."""
        if self._tokens is None or not self._tokens.refresh_token:
            return
        # Set expiry to a past time so the next `_get_access_token`
        # call triggers refresh.
        self._tokens.expires_at = int(time.time()) - 1

    @staticmethod
    def _normalize_event(raw: dict) -> dict[str, Any]:
        """Reduce Google's verbose event payload to what voice + chat
        UIs actually need."""
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


class _DeviceFlow:
    """Internal: in-flight device-code flow state."""

    __slots__ = ("request_id", "cfg", "challenge", "tokens", "error", "poll_task")

    def __init__(self, *, request_id: str, cfg: GoogleOAuthConfig) -> None:
        self.request_id = request_id
        self.cfg = cfg
        self.challenge: Optional[Any] = None  # DeviceCodeChallenge
        self.tokens: Optional[OAuthTokens] = None
        self.error: Optional[DeviceCodeError] = None
        self.poll_task: Optional[asyncio.Task] = None

    async def start(self):  # noqa: ANN201
        """Kick off the device-code endpoint call.  Returns the
        challenge that becomes the `verification_url` + `user_code`
        Tab5 will render."""
        async with make_google_client(self.cfg) as client:
            return await client.start()


def helpful_relative_time(target: datetime, now: Optional[datetime] = None) -> str:
    """"in 2 hours" / "yesterday" / "next Tuesday at 9am" — used by the
    tool wrappers to phrase replies naturally for TTS.

    Kept small + deterministic; for richer phrasing the LLM can
    rewrite the tool result before speaking."""
    now = now or datetime.now(timezone.utc)
    target = target.astimezone(timezone.utc)
    delta = target - now
    if delta < timedelta(0):
        delta = -delta
        direction = "ago"
    else:
        direction = "from now"
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs} seconds {direction}"
    if secs < 3600:
        return f"{secs // 60} minutes {direction}"
    if secs < 86400:
        return f"{secs // 3600} hours {direction}"
    return f"{secs // 86400} days {direction}"
