"""Gmail integration — auth-code-with-PKCE + multi-account (#341 / #347 / Phase 2).

Mirrors ``GoogleCalendarIntegration``'s shape (same OAuth flow, same
multi-account state machine), but talks to Gmail API v1 instead of
Calendar v3.  Each Google account that connects Gmail gets its own
token bundle at ``{integrations_dir}/gmail/{email}.json`` — sibling
to ``{integrations_dir}/google-calendar/{email}.json``.

Common use cases the LLM should be able to drive:
  * "What unread emails do I have?"     → list_messages(query="is:unread")
  * "Read me the one from X"            → get_message_body(message_id)
  * "Reply: yes, sounds good"           → send_message(in_reply_to=...)
  * "Archive that"                      → modify_labels(remove=["INBOX"])
  * "Search for emails about Y"         → list_messages(query="Y")

Scopes used:
  * ``gmail.readonly`` — list + read messages
  * ``gmail.modify``   — label changes (archive, mark read, star)
  * ``gmail.send``     — send/reply

Plus the always-on ``openid email`` so we can extract account_id from
the id_token.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
import uuid
from email.message import EmailMessage
from typing import Any, Literal, Optional

import aiohttp

from dragon_voice.tools.integrations.base import (
    AccountInfo,
    ConnectChallenge,
    ConnectionStatus,
    IntegrationBackend,
)
from dragon_voice.tools.integrations.credentials import ProviderCredentialDir
from dragon_voice.tools.integrations.google.auth import (
    GoogleOAuthConfig,
    OAuthTokens,
    SCOPE_GMAIL_MODIFY,
    SCOPE_GMAIL_READ,
    SCOPE_GMAIL_SEND,
    extract_account_id_from_tokens,
    fetch_google_email,
    make_google_client,
    revoke_google_token,
)
from dragon_voice.tools.integrations.oauth import DeviceCodeError
from dragon_voice.tools.integrations.registry import register_integration

logger = logging.getLogger(__name__)

GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
PROVIDER_NAME = "gmail"


@register_integration(PROVIDER_NAME)
class GmailIntegration(IntegrationBackend):
    """Gmail read + send + label-modify via Gmail API v1."""

    _SCOPES = [SCOPE_GMAIL_READ, SCOPE_GMAIL_MODIFY, SCOPE_GMAIL_SEND]

    def __init__(self) -> None:
        self._provider_dir = ProviderCredentialDir(PROVIDER_NAME)
        self._flows: dict[str, "_AuthFlow"] = {}
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
        return "Gmail"

    @property
    def description(self) -> str:
        return "Read, search, and send email through your Google account."

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
                display_label=aid,
                default=(aid == self._default_account_id),
                scopes=list(tokens.scopes or []),
                connected_at=(tokens.extra or {}).get("connected_at"),
            ))
        out.sort(key=lambda a: (not a.default, a.account_id))
        return out

    async def start_connect(self, params: Optional[dict] = None) -> ConnectChallenge:
        del params
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
                account_id=flow.account_id, account_label=flow.account_id,
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
        flow = self._find_flow_by_state(state)
        if flow is None:
            logger.warning("Gmail OAuth callback for unknown state %r", state[:12])
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
        if self._default_account_id not in self._accounts:
            self._default_account_id = next(iter(self._accounts), None)
            if self._default_account_id is not None:
                tokens = self._accounts[self._default_account_id]
                if tokens.extra is None:
                    tokens.extra = {}
                tokens.extra["default"] = True
                await self._persist_account(self._default_account_id, tokens)

    async def set_default_account(self, account_id: str) -> None:
        await self._ensure_loaded()
        if account_id not in self._accounts:
            raise DeviceCodeError(
                "unknown_account", f"no connected Gmail account {account_id!r}",
            )
        old_default = self._default_account_id
        self._default_account_id = account_id
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
                f"{GMAIL_API_BASE}/profile",
                timeout_s=timeout_s, account_id=target,
            )
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            return False, f"{type(e).__name__}: {e}"[:120]
        except DeviceCodeError as e:
            return False, f"auth: {e.description[:120]}"
        email = data.get("emailAddress") if isinstance(data, dict) else None
        total = data.get("messagesTotal") if isinstance(data, dict) else None
        return True, f"connected to {email or target} ({total or '?'} total messages)"

    # ── Gmail API used by tools ─────────────────────────────────

    async def list_messages(
        self,
        query: Optional[str] = None,
        label_ids: Optional[list[str]] = None,
        max_results: int = 10,
        account_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """List messages matching ``query`` / ``label_ids``.

        Returns a list of summaries (id, snippet, from, subject, date,
        unread bool, labels).  Two round trips: one to enumerate ids,
        one batched-format=metadata pull per id.  Hard-capped at 25
        ids per call to keep latency sane.
        """
        params = {"maxResults": str(min(max_results, 25))}
        if query:
            params["q"] = query
        if label_ids:
            for lid in label_ids:
                params.setdefault("labelIds", []).append(lid)  # type: ignore[union-attr]
        raw = await self._authed_get(
            f"{GMAIL_API_BASE}/messages", params=params, account_id=account_id,
        )
        ids = [m["id"] for m in (raw.get("messages") or []) if "id" in m]
        target = self._resolve_target_account(account_id)
        if not ids:
            return []
        # Fan-out the metadata fetches concurrently.
        summaries = await asyncio.gather(*[
            self._fetch_message_metadata(mid, account_id=account_id) for mid in ids
        ], return_exceptions=True)
        out = []
        for mid, summary in zip(ids, summaries):
            if isinstance(summary, Exception):
                logger.warning("Failed to fetch metadata for %s: %s", mid, summary)
                continue
            out.append(dict(summary, account=target))
        return out

    async def get_message_body(
        self, message_id: str, account_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Fetch and decode the message body (text/plain preferred).

        Returns ``{id, from, subject, date, body_text, body_html, snippet}``.
        ``body_text`` is the decoded text/plain part if present, else
        the HTML stripped to plain (lossy — Gmail's HTML is verbose).
        """
        raw = await self._authed_get(
            f"{GMAIL_API_BASE}/messages/{message_id}",
            params={"format": "full"},
            account_id=account_id,
        )
        headers = _extract_headers(raw)
        body_text, body_html = _extract_body(raw)
        target = self._resolve_target_account(account_id)
        return {
            "id": message_id,
            "from": headers.get("From"),
            "to": headers.get("To"),
            "subject": headers.get("Subject", "(no subject)"),
            "date": headers.get("Date"),
            "snippet": raw.get("snippet", ""),
            "body_text": body_text,
            "body_html": body_html,
            "labels": raw.get("labelIds", []),
            "account": target,
        }

    async def send_message(
        self,
        to: str,
        subject: str,
        body: str,
        in_reply_to: Optional[str] = None,
        cc: Optional[str] = None,
        account_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Compose + send.

        When ``in_reply_to`` is the Gmail-internal message_id of the
        original, we look up its RFC 2822 Message-Id + thread-id so the
        reply threads correctly in the recipient's inbox.
        """
        msg = EmailMessage()
        msg["To"] = to
        if cc:
            msg["Cc"] = cc
        msg["Subject"] = subject
        msg.set_content(body)

        thread_id: Optional[str] = None
        if in_reply_to:
            try:
                original = await self._authed_get(
                    f"{GMAIL_API_BASE}/messages/{in_reply_to}",
                    params={"format": "metadata", "metadataHeaders": "Message-Id"},
                    account_id=account_id,
                )
                rfc_msg_id = _extract_headers(original).get("Message-Id")
                if rfc_msg_id:
                    msg["In-Reply-To"] = rfc_msg_id
                    msg["References"] = rfc_msg_id
                thread_id = original.get("threadId")
            except Exception as e:  # noqa: BLE001
                logger.warning("Failed to load in_reply_to %s: %s", in_reply_to, e)

        raw_bytes = msg.as_bytes()
        raw_b64 = base64.urlsafe_b64encode(raw_bytes).rstrip(b"=").decode("ascii")
        body_payload: dict[str, Any] = {"raw": raw_b64}
        if thread_id:
            body_payload["threadId"] = thread_id
        resp = await self._authed_post(
            f"{GMAIL_API_BASE}/messages/send", body=body_payload,
            account_id=account_id,
        )
        target = self._resolve_target_account(account_id)
        return {
            "sent": True,
            "id": resp.get("id"),
            "thread_id": resp.get("threadId"),
            "labels": resp.get("labelIds", []),
            "account": target,
        }

    async def modify_labels(
        self,
        message_id: str,
        add: Optional[list[str]] = None,
        remove: Optional[list[str]] = None,
        account_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Add or remove labels on a message.

        Common patterns:
          * Archive: ``remove=["INBOX"]``
          * Mark as read: ``remove=["UNREAD"]``
          * Star: ``add=["STARRED"]``
        """
        body: dict[str, Any] = {}
        if add:
            body["addLabelIds"] = add
        if remove:
            body["removeLabelIds"] = remove
        if not body:
            return {"modified": False, "id": message_id, "reason": "no labels supplied"}
        resp = await self._authed_post(
            f"{GMAIL_API_BASE}/messages/{message_id}/modify",
            body=body, account_id=account_id,
        )
        target = self._resolve_target_account(account_id)
        return {
            "modified": True,
            "id": resp.get("id", message_id),
            "labels": resp.get("labelIds", []),
            "account": target,
        }

    # ── Internals (mirror GoogleCalendarIntegration) ────────────

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
            logger.warning("Gmail fetch_google_email raised", exc_info=True)
        placeholder = f"pending-{uuid.uuid4().hex[:8]}"
        logger.warning(
            "Could not determine Gmail account_id — stored under %s.  User "
            "should disconnect + reconnect after granting openid+email.",
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
                logger.warning("Gmail tokens for %s malformed (%s) — skipping.", aid, e)
                continue
            accounts[aid] = tokens
            if (tokens.extra or {}).get("default"):
                default = aid
        if default is None and accounts:
            default = next(iter(accounts))
        self._accounts = accounts
        self._default_account_id = default

    async def _get_access_token(self, account_id: Optional[str] = None) -> str:
        await self._ensure_loaded()
        target = self._resolve_target_account(account_id)
        if target is None:
            raise DeviceCodeError(
                "not_connected",
                "Gmail is not connected.  Tap Settings → Integrations → "
                "Connect Gmail on the Tab5.",
            )
        tokens = self._accounts[target]
        if not tokens.is_expired():
            return tokens.access_token
        if not tokens.refresh_token:
            raise DeviceCodeError(
                "needs_reauth",
                f"No refresh token for {target} — please reconnect this account.",
            )
        cfg = GoogleOAuthConfig.from_env(scopes=tokens.scopes or self._SCOPES)
        async with make_google_client(cfg) as client:
            new_tokens = await client.refresh(tokens.refresh_token)
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
                url, params=params,
                headers={"Authorization": f"Bearer {token}"},
            ) as resp:
                if resp.status == 401:
                    await self._force_refresh(target)
                    token = await self._get_access_token(account_id)
                    async with session.get(
                        url, params=params,
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
                url, json=body,
                headers={"Authorization": f"Bearer {token}"},
            ) as resp:
                resp.raise_for_status()
                return await resp.json()

    async def _force_refresh(self, account_id: Optional[str]) -> None:
        if account_id is None or account_id not in self._accounts:
            return
        tokens = self._accounts[account_id]
        if not tokens.refresh_token:
            return
        tokens.expires_at = int(time.time()) - 1

    async def _fetch_message_metadata(
        self, message_id: str, account_id: Optional[str] = None,
    ) -> dict[str, Any]:
        raw = await self._authed_get(
            f"{GMAIL_API_BASE}/messages/{message_id}",
            params={
                "format": "metadata",
                "metadataHeaders": ["Subject", "From", "Date"],
            },
            account_id=account_id,
        )
        headers = _extract_headers(raw)
        return {
            "id": message_id,
            "snippet": raw.get("snippet", ""),
            "from": headers.get("From"),
            "subject": headers.get("Subject", "(no subject)"),
            "date": headers.get("Date"),
            "labels": raw.get("labelIds", []),
            "unread": "UNREAD" in (raw.get("labelIds") or []),
        }


def _extract_headers(raw: dict) -> dict[str, str]:
    """Convert Gmail's [{name,value}] header list to a flat dict."""
    out: dict[str, str] = {}
    payload = raw.get("payload") or {}
    for h in payload.get("headers") or []:
        name = h.get("name")
        value = h.get("value")
        if name and value:
            out[name] = value
    return out


def _extract_body(raw: dict) -> tuple[Optional[str], Optional[str]]:
    """Walk a Gmail message payload tree, return (text, html) bodies.

    Bodies are base64url-encoded in ``payload.body.data`` (when the
    message is single-part) or under ``payload.parts[i].body.data``
    (multipart).  Either or both may be missing.
    """
    payload = raw.get("payload") or {}
    text: Optional[str] = None
    html: Optional[str] = None

    def _walk(part: dict) -> None:
        nonlocal text, html
        mime = part.get("mimeType", "")
        body = part.get("body") or {}
        data = body.get("data")
        if data:
            try:
                # Gmail uses url-safe base64 without padding.
                padded = data + "=" * (-len(data) % 4)
                decoded = base64.urlsafe_b64decode(padded).decode(
                    "utf-8", errors="replace",
                )
            except (ValueError, TypeError):
                decoded = None
            if decoded:
                if mime == "text/plain" and text is None:
                    text = decoded
                elif mime == "text/html" and html is None:
                    html = decoded
        for sub in part.get("parts") or []:
            _walk(sub)

    _walk(payload)
    return text, html


class _AuthFlow:
    __slots__ = (
        "request_id", "state", "code_verifier", "cfg",
        "expires_at", "tokens", "error", "account_id",
    )

    def __init__(
        self, *,
        request_id: str, state: str, code_verifier: str, cfg: GoogleOAuthConfig,
    ) -> None:
        self.request_id = request_id
        self.state = state
        self.code_verifier = code_verifier
        self.cfg = cfg
        self.expires_at = time.time() + 600
        self.tokens: Optional[OAuthTokens] = None
        self.error: Optional[DeviceCodeError] = None
        self.account_id: Optional[str] = None
