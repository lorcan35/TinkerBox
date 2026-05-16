"""Google Calendar integration — auth-code-with-PKCE flow + multi-account (#347).

OAuth state machine (per-account):

  start_connect()
    → creates AuthCodeChallenge (PKCE pair + state)
    → stashes verifier + asyncio.Future in `self._flows[request_id]`
    → returns ConnectChallenge with the authorization_url
       (Tab5 shows QR + plain link)

  user opens auth URL on phone, signs in, grants scopes

  Google redirects to /api/v1/oauth/callback?code=…&state=…
    → IntegrationRoutes.oauth_callback handler calls
       integration.handle_callback(state, code, error)
    → we exchange code+verifier for tokens, decode id_token to find
       the verified email (= account_id), persist under that key,
       mark default if it's the first account

  Tab5 polls /api/v1/integrations/google-calendar/status/{request_id}
    → poll_status() reads the flow's future state, returns
       connected/expired/error AND echoes the new account_id+label
       once known so Tab5 can update its UI immediately.

Per-account state:
  self._accounts: dict[account_id, OAuthTokens]
  self._default_account_id: Optional[str]
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
    AccountInfo,
    ConnectChallenge,
    ConnectionStatus,
    IntegrationBackend,
)
from dragon_voice.tools.integrations.credentials import (
    LEGACY_ACCOUNT_ID,
    CredentialStore,
    ProviderCredentialDir,
)
from dragon_voice.tools.integrations.google.auth import (
    GoogleOAuthConfig,
    OAuthTokens,
    SCOPE_CALENDAR_EVENTS,
    SCOPE_CALENDAR_READ,
    extract_account_id_from_tokens,
    fetch_google_email,
    make_google_client,
    revoke_google_token,
)
from dragon_voice.tools.integrations.oauth import DeviceCodeError
from dragon_voice.tools.integrations.registry import register_integration

logger = logging.getLogger(__name__)

CALENDAR_API_BASE = "https://www.googleapis.com/calendar/v3"
PROVIDER_NAME = "google-calendar"


@register_integration(PROVIDER_NAME)
class GoogleCalendarIntegration(IntegrationBackend):
    """Google Calendar read + write via Calendar API v3 — multi-account."""

    _SCOPES = [SCOPE_CALENDAR_READ, SCOPE_CALENDAR_EVENTS]

    def __init__(self) -> None:
        self._provider_dir = ProviderCredentialDir(PROVIDER_NAME)
        # request_id → in-flight flow.  Survives across REST calls.
        self._flows: dict[str, "_AuthFlow"] = {}
        # account_id → OAuthTokens (lazy-loaded on first access).
        self._accounts: dict[str, OAuthTokens] = {}
        self._default_account_id: Optional[str] = None
        self._loaded = False
        self._load_lock = asyncio.Lock()

    # ── IntegrationBackend interface ────────────────────────────

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    @property
    def display_name(self) -> str:
        return "Google Calendar"

    @property
    def description(self) -> str:
        return "Read and create events on your primary Google Calendar."

    @property
    def auth_kind(self) -> Literal["oauth-device", "oauth-authcode", "static-token", "none"]:  # type: ignore[override]
        return "oauth-authcode"

    @property
    def supports_multi_account(self) -> bool:
        return True

    async def is_connected(self, account_id: Optional[str] = None) -> bool:
        await self._ensure_loaded()
        if account_id is None:
            return bool(self._accounts)
        return account_id in self._accounts

    async def list_accounts(self) -> list[AccountInfo]:
        await self._ensure_loaded()
        out: list[AccountInfo] = []
        for aid, tokens in self._accounts.items():
            out.append(AccountInfo(
                account_id=aid,
                display_label=_label_for_account_id(aid),
                default=(aid == self._default_account_id),
                scopes=list(tokens.scopes or []),
                connected_at=(tokens.extra or {}).get("connected_at"),
            ))
        out.sort(key=lambda a: (not a.default, a.account_id))
        return out

    async def start_connect(self, params: Optional[dict] = None) -> ConnectChallenge:
        del params  # unused for auth-code flow
        cfg = GoogleOAuthConfig.from_env(scopes=self._SCOPES)
        request_id = uuid.uuid4().hex
        client = make_google_client(cfg)
        chal = client.start(extra_params={
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
        })
        flow = _AuthFlow(
            request_id=request_id,
            state=chal.state,
            code_verifier=chal.code_verifier,
            cfg=cfg,
        )
        self._flows[request_id] = flow

        return ConnectChallenge(
            kind="oauth-authcode",
            request_id=request_id,
            verification_url=chal.authorization_url,
            user_code=None,
            expires_in_s=600,
            interval_s=2,
        )

    async def poll_status(self, request_id: str) -> ConnectionStatus:
        flow = self._flows.get(request_id)
        if flow is None:
            return ConnectionStatus(
                state="error", request_id=request_id, error="unknown request_id",
            )
        if flow.tokens is not None and flow.account_id is not None:
            return ConnectionStatus(
                state="connected", request_id=request_id,
                account_id=flow.account_id,
                account_label=_label_for_account_id(flow.account_id),
            )
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

        After exchange, decodes the id_token to find the verified
        email and persists tokens under that account_id.  If id_token
        is absent, falls back to the userinfo endpoint.  If both fail,
        persists under a placeholder account_id ("pending-<request_id>")
        which the user can disconnect later.
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

        account_id = await self._resolve_account_id(tokens)
        await self._ensure_loaded()
        is_first = not self._accounts
        # Stamp metadata on the token bundle so list_accounts can show
        # connected_at + we can flag the default.
        if tokens.extra is None:
            tokens.extra = {}
        tokens.extra.setdefault("connected_at", int(time.time()))
        if is_first:
            tokens.extra["default"] = True

        await self._persist_account(account_id, tokens)
        self._accounts[account_id] = tokens
        if is_first or self._default_account_id is None:
            self._default_account_id = account_id

        flow.account_id = account_id
        flow.tokens = tokens

    async def disconnect(self, account_id: Optional[str] = None) -> None:
        """Disconnect one account (or all when account_id is None)."""
        await self._ensure_loaded()
        targets = [account_id] if account_id is not None else list(self._accounts.keys())
        if not targets:
            return
        for aid in targets:
            tokens = self._accounts.get(aid)
            if tokens is not None and tokens.refresh_token:
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as session:
                    await revoke_google_token(tokens.refresh_token, session)
            store = self._provider_dir.store_for(aid)
            await store.delete()
            self._accounts.pop(aid, None)
        # Reassign default if we dropped it.
        if self._default_account_id not in self._accounts:
            self._default_account_id = next(iter(self._accounts), None)
            if self._default_account_id is not None:
                # Persist the new default flag.
                tokens = self._accounts[self._default_account_id]
                if tokens.extra is None:
                    tokens.extra = {}
                tokens.extra["default"] = True
                await self._persist_account(self._default_account_id, tokens)

    async def set_default_account(self, account_id: str) -> None:
        await self._ensure_loaded()
        if account_id not in self._accounts:
            raise DeviceCodeError(
                "unknown_account", f"no connected account with id {account_id!r}",
            )
        old_default = self._default_account_id
        self._default_account_id = account_id
        # Re-persist both files so the default flag is up to date.
        if old_default and old_default != account_id and old_default in self._accounts:
            old_tokens = self._accounts[old_default]
            if old_tokens.extra is None:
                old_tokens.extra = {}
            old_tokens.extra["default"] = False
            await self._persist_account(old_default, old_tokens)
        new_tokens = self._accounts[account_id]
        if new_tokens.extra is None:
            new_tokens.extra = {}
        new_tokens.extra["default"] = True
        await self._persist_account(account_id, new_tokens)

    async def health_check(
        self,
        account_id: Optional[str] = None,
        timeout_s: float = 5.0,
    ) -> tuple[bool, str]:
        await self._ensure_loaded()
        target = self._resolve_target_account(account_id)
        if target is None:
            return False, "not connected"
        try:
            data = await self._authed_get(
                f"{CALENDAR_API_BASE}/calendars/primary",
                timeout_s=timeout_s,
                account_id=target,
            )
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            return False, f"{type(e).__name__}: {e}"[:120]
        except DeviceCodeError as e:
            return False, f"auth: {e.description[:120]}"
        cal_id = data.get("id") if isinstance(data, dict) else None
        return True, f"connected to {cal_id or 'primary calendar'} as {_label_for_account_id(target)}"

    # ── Calendar API used by tools ──────────────────────────────

    async def list_events(
        self,
        time_min: Optional[datetime] = None,
        time_max: Optional[datetime] = None,
        max_results: int = 10,
        account_id: Optional[str] = None,
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
            account_id=account_id,
        )
        items = raw.get("items", []) if isinstance(raw, dict) else []
        target = self._resolve_target_account(account_id)
        return [
            dict(self._normalize_event(it), account=target) for it in items
        ]

    async def create_event(
        self,
        summary: str,
        start: datetime,
        end: datetime,
        location: Optional[str] = None,
        description: Optional[str] = None,
        account_id: Optional[str] = None,
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
            account_id=account_id,
        )
        target = self._resolve_target_account(account_id)
        return dict(self._normalize_event(raw), account=target)

    async def cancel_event(
        self, event_id: str, account_id: Optional[str] = None,
    ) -> bool:
        try:
            await self._authed_delete(
                f"{CALENDAR_API_BASE}/calendars/primary/events/{event_id}",
                account_id=account_id,
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

    def _resolve_target_account(self, account_id: Optional[str]) -> Optional[str]:
        if account_id is not None:
            return account_id if account_id in self._accounts else None
        return self._default_account_id

    async def _resolve_account_id(self, tokens: OAuthTokens) -> str:
        """Find the verified-email account_id for a freshly-issued
        token bundle.

        Tries id_token first (zero extra requests), then userinfo, then
        falls back to a placeholder so the connect flow can still
        complete and the user can clean up via disconnect+reconnect.
        """
        from_jwt = extract_account_id_from_tokens(tokens)
        if from_jwt:
            return from_jwt
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=8),
            ) as session:
                email = await fetch_google_email(tokens.access_token, session)
                if email:
                    return email
        except Exception:  # noqa: BLE001
            logger.warning("fetch_google_email raised", exc_info=True)
        placeholder = f"pending-{uuid.uuid4().hex[:8]}"
        logger.warning(
            "Could not determine account_id from id_token or userinfo — "
            "stored under placeholder %s.  User should disconnect + reconnect "
            "after granting openid+email scope.",
            placeholder,
        )
        return placeholder

    async def _persist_account(self, account_id: str, tokens: OAuthTokens) -> None:
        store = self._provider_dir.store_for(account_id)
        await store.save({"tokens": tokens.to_dict()})

    async def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        async with self._load_lock:
            if self._loaded:
                return
            await self._load_from_disk()
            self._loaded = True

    async def _load_from_disk(self) -> None:
        # Pre-#347 layout migration: flat {provider}.json → {provider}/_legacy.json
        self._provider_dir.migrate_legacy()
        accounts: dict[str, OAuthTokens] = {}
        default: Optional[str] = None
        for aid in self._provider_dir.list_accounts(include_reserved=True):
            store = self._provider_dir.store_for(aid)
            data = await store.load()
            if not data or "tokens" not in data:
                continue
            try:
                tokens = OAuthTokens.from_dict(data["tokens"])
            except (KeyError, ValueError, TypeError) as e:
                logger.warning(
                    "Stored Google Calendar tokens for %s malformed (%s) — "
                    "skipping.", aid, e,
                )
                continue
            accounts[aid] = tokens
            if (tokens.extra or {}).get("default"):
                default = aid
        if default is None and accounts:
            # No explicit default → first account wins.
            default = next(iter(accounts))
        self._accounts = accounts
        self._default_account_id = default

    async def _get_access_token(self, account_id: Optional[str] = None) -> str:
        await self._ensure_loaded()
        target = self._resolve_target_account(account_id)
        if target is None:
            raise DeviceCodeError(
                "not_connected",
                "Google Calendar is not connected.  Tap Settings → "
                "Integrations → Connect Google on the Tab5.",
            )
        tokens = self._accounts[target]
        if not tokens.is_expired():
            return tokens.access_token
        if not tokens.refresh_token:
            raise DeviceCodeError(
                "needs_reauth",
                f"No refresh token for {_label_for_account_id(target)} — "
                "please reconnect this Google account.",
            )
        cfg = GoogleOAuthConfig.from_env(scopes=tokens.scopes or self._SCOPES)
        async with make_google_client(cfg) as client:
            new_tokens = await client.refresh(tokens.refresh_token)
        # Preserve metadata (default flag, connected_at).
        if new_tokens.extra is None:
            new_tokens.extra = {}
        if tokens.extra:
            for k, v in tokens.extra.items():
                new_tokens.extra.setdefault(k, v)
        await self._persist_account(target, new_tokens)
        self._accounts[target] = new_tokens
        return new_tokens.access_token

    async def _authed_get(
        self,
        url: str,
        params: Optional[dict] = None,
        timeout_s: float = 15.0,
        account_id: Optional[str] = None,
    ) -> Any:
        token = await self._get_access_token(account_id)
        target = self._resolve_target_account(account_id)
        timeout = aiohttp.ClientTimeout(total=timeout_s)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            ) as resp:
                if resp.status == 401:
                    await self._force_refresh(target)
                    token = await self._get_access_token(account_id)
                    async with session.get(
                        url,
                        params=params,
                        headers={"Authorization": f"Bearer {token}"},
                    ) as resp2:
                        resp2.raise_for_status()
                        return await resp2.json()
                resp.raise_for_status()
                return await resp.json()

    async def _authed_post(
        self, url: str, body: dict, account_id: Optional[str] = None,
    ) -> Any:
        token = await self._get_access_token(account_id)
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

    async def _authed_delete(self, url: str, account_id: Optional[str] = None) -> None:
        token = await self._get_access_token(account_id)
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
        ) as session:
            async with session.delete(
                url,
                headers={"Authorization": f"Bearer {token}"},
            ) as resp:
                resp.raise_for_status()

    async def _force_refresh(self, account_id: Optional[str]) -> None:
        if account_id is None or account_id not in self._accounts:
            return
        tokens = self._accounts[account_id]
        if not tokens.refresh_token:
            return
        tokens.expires_at = int(time.time()) - 1

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


def _label_for_account_id(account_id: str) -> str:
    """Human-readable name for the account chip.

    `_legacy` and `pending-*` get friendlier labels so the user knows
    to reconnect.  Real email-shaped ids pass through unchanged.
    """
    if account_id == LEGACY_ACCOUNT_ID:
        return "Legacy connection (reconnect to identify)"
    if account_id.startswith("pending-"):
        return f"Unidentified Google account ({account_id})"
    return account_id


class _AuthFlow:
    """Internal: in-flight auth-code flow state."""

    __slots__ = (
        "request_id", "state", "code_verifier", "cfg",
        "expires_at", "tokens", "error", "account_id",
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
        self.account_id: Optional[str] = None
