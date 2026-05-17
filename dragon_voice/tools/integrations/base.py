"""IntegrationBackend ABC + lifecycle types (#341 / #347).

Every TinkerBox integration (Google Calendar, Gmail, Home Assistant,
Spotify, ...) implements this interface.  The methods are deliberately
narrow:

  * `display_name`, `description` — UI metadata for Tab5 Settings.
  * `auth_kind` — drives the Tab5 connect-modal shape.
  * `start_connect` — initiates the auth flow, returns the challenge
    the user needs to complete on their phone (auth-code / device-code)
    or the fields they need to fill (static-token).
  * `poll_status` — Tab5 polls until `connected: true` or expiry.
  * `list_accounts` — enumerate connected accounts under this provider.
    Single-account providers (Home Assistant) return at most one;
    multi-account providers (Google, Spotify, Notion) return any
    number.
  * `disconnect` — revoke + delete tokens for one account (or all when
    account_id omitted).
  * `health_check` — smoke test; the REST `/test` endpoint calls this.

State persistence lives in `CredentialStore` (see credentials.py); the
backend owns the schema of what gets stored.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal, Optional


class IntegrationState(str, Enum):
    """Top-level integration state for the Tab5 UI badge."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    NEEDS_REAUTH = "needs_reauth"
    ERROR = "error"


@dataclass
class AccountInfo:
    """One connected account under a multi-account integration.

    * ``account_id`` — stable handle the integration uses to address
      this account (Google: email; Spotify: user id; HA: host).  Tab5
      shows it on the connected-account chip; tools take it as an
      argument when the user wants to target a specific account.
    * ``display_label`` — human-readable name (often the same as
      account_id for email-shaped ids).
    * ``default`` — exactly one account per integration may be the
      default.  Tools fall back to it when no account is specified.
    * ``scopes`` — granted OAuth scopes (informational; helps Tab5
      explain capability gaps).
    * ``connected_at`` — unix timestamp of first successful auth.
    """

    account_id: str
    display_label: str
    default: bool = False
    scopes: list[str] = field(default_factory=list)
    connected_at: Optional[int] = None


@dataclass
class ConnectChallenge:
    """Returned by `start_connect` — what Tab5 should show the user.

    For OAuth (device-code AND authorization-code-with-PKCE):
      * `verification_url` — what the QR code encodes.  For device-code
        flows the `user_code` is embedded as a query param; for
        auth-code flows the URL is the full Google auth URL with
        `code_challenge`+`state`.
      * `user_code` — human-readable fallback for device-code flows.
        ``None`` for auth-code flows.
      * `expires_in_s` — countdown for the modal.
      * `interval_s` — how often Tab5 should poll the status endpoint.
      * `request_id` — opaque server-side handle.  Tab5 echoes this
        back to `/status` so the backend knows which in-flight flow.

    For static-token:
      * `prompt_fields` — list of field names Tab5 should render as
        text inputs (e.g. `["base_url", "long_lived_token"]`).
    """

    kind: Literal["oauth-device", "oauth-authcode", "static-token", "none"]
    request_id: str
    verification_url: Optional[str] = None
    user_code: Optional[str] = None
    expires_in_s: Optional[int] = None
    interval_s: Optional[int] = None
    prompt_fields: Optional[list[str]] = None


@dataclass
class ConnectionStatus:
    """Returned by `poll_status` — Tab5 keeps polling until terminal.

    * `state` — `connecting` while flow is mid-flight, `connected` on
      success, `error` on a non-recoverable failure, `expired` when
      the flow timed out.
    * `error` — human-readable string when `state` is `error`/`expired`.
    * `account_id` — populated when `state == "connected"` so Tab5
      knows which account just landed.  ``None`` mid-flight.
    * `account_label` — human-readable name (email) for the new account.
    """

    state: Literal["connecting", "connected", "error", "expired"]
    request_id: str
    error: Optional[str] = None
    account_id: Optional[str] = None
    account_label: Optional[str] = None


class IntegrationBackend(ABC):
    """Interface every integration implements.

    Default semantics for the account_id parameter on methods that
    take it:  ``None`` means *the default account* if one is set,
    otherwise fall back to whatever single-account behaviour made
    sense pre-#347.  Single-account providers can ignore the parameter.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable id used in REST routes + registry lookup.

        Examples: ``"google-calendar"``, ``"gmail"``, ``"homeassistant"``.
        Lower-kebab-case; no spaces.
        """
        ...

    @property
    @abstractmethod
    def display_name(self) -> str:
        """User-facing name shown in Tab5 Settings."""
        ...

    @property
    def description(self) -> str:
        """Optional longer description for Tab5 Settings.  Default
        returns the display name; subclasses can override."""
        return self.display_name

    @property
    @abstractmethod
    def auth_kind(self) -> Literal["oauth-device", "oauth-authcode", "static-token", "none"]:
        """Drives the Tab5 connect-modal shape."""
        ...

    @property
    def supports_multi_account(self) -> bool:
        """True when the integration can hold more than one connected
        account (Google, Spotify, Notion).  Default False — Home
        Assistant and static-token integrations override to True only
        if they actually multiplex."""
        return False

    @abstractmethod
    async def is_connected(self, account_id: Optional[str] = None) -> bool:
        """True when credentials exist + are usable.

        When ``account_id`` is None: True if ANY account is connected.
        When provided: True only if that specific account is connected.
        No network call — read-only.
        """
        ...

    @abstractmethod
    async def list_accounts(self) -> list[AccountInfo]:
        """Return all currently-connected accounts under this provider.

        Empty list when nothing is connected.  Order is stable but
        otherwise unspecified.  Exactly one entry has ``default=True``
        when any are connected.
        """
        ...

    @abstractmethod
    async def start_connect(self, params: Optional[dict] = None) -> ConnectChallenge:
        """Initiate the auth flow.

        For multi-account providers, the result account_id isn't known
        until after the callback resolves and we hit ``userinfo``.
        Tab5 just receives a ``request_id`` here and polls.
        """
        ...

    @abstractmethod
    async def poll_status(self, request_id: str) -> ConnectionStatus:
        """Tab5 polls every few seconds while the user completes the
        flow on their phone.  Returns terminal state when done."""
        ...

    @abstractmethod
    async def disconnect(self, account_id: Optional[str] = None) -> None:
        """Revoke + delete stored credentials.

        When ``account_id`` is None: disconnect ALL accounts.  When
        provided: disconnect just that one (other accounts remain
        connected; the default is reassigned if the disconnected one
        was the default).

        Best-effort: if the provider's revoke endpoint fails, still
        delete the local creds — the user explicitly asked to
        disconnect.
        """
        ...

    @abstractmethod
    async def health_check(
        self,
        account_id: Optional[str] = None,
        timeout_s: float = 5.0,
    ) -> tuple[bool, str]:
        """Cheap reachability probe.

        When ``account_id`` is None: probe the default account.
        Returns ``(ok, detail)``.
        """
        ...

    async def set_default_account(self, account_id: str) -> None:  # noqa: ARG002
        """Optional — multi-account providers override to mark an
        account as the default.  Single-account providers leave the
        default (no-op)."""
        return None

    async def shutdown(self) -> None:
        """Release any held connections (HTTP sessions, etc.)  Default
        is a no-op; integrations with persistent connections override.
        """
        return None
