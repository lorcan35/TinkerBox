"""IntegrationBackend ABC + lifecycle types (#341 / #342).

Every TinkerBox integration (Google Calendar, Gmail, Home Assistant,
Spotify, ...) implements this interface.  The methods are deliberately
narrow:

  * `display_name`, `description` — UI metadata for Tab5 Settings.
  * `auth_kind` — drives the Tab5 connect-modal shape.
  * `start_connect` — initiates the auth flow, returns the challenge
    the user needs to complete on their phone (device-code) or the
    fields they need to fill (static-token).
  * `poll_status` — Tab5 polls until `connected: true` or expiry.
  * `disconnect` — revoke + delete tokens.
  * `health_check` — smoke test; the REST `/test` endpoint calls this.

State persistence lives in `CredentialStore` (see credentials.py); the
backend owns the schema of what gets stored.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
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
class ConnectChallenge:
    """Returned by `start_connect` — what Tab5 should show the user.

    For OAuth device-code:
      * `verification_url` — what the QR code encodes (with `user_code`
        embedded as a query param so the phone autofills it).
      * `user_code` — the human-readable fallback ("ABC-123").
      * `expires_in_s` — countdown for the modal.
      * `interval_s` — how often Tab5 should poll the status endpoint.
      * `request_id` — opaque server-side handle.  Tab5 echoes this
        back to `/status` so the backend knows which in-flight flow.

    For static-token:
      * `prompt_fields` — list of field names Tab5 should render as
        text inputs (e.g. `["base_url", "long_lived_token"]`).
      * `verification_url` / `user_code` / `expires_in_s` / `interval_s`
        all unset.
    """

    kind: Literal["oauth-device", "static-token", "none"]
    request_id: str
    verification_url: Optional[str] = None
    user_code: Optional[str] = None
    expires_in_s: Optional[int] = None
    interval_s: Optional[int] = None
    prompt_fields: Optional[list[str]] = None


@dataclass
class ConnectionStatus:
    """Returned by `poll_status` — Tab5 keeps polling until terminal.

    * `state` — `connecting` while OAuth flow is mid-flight, `connected`
      on success, `error` on a non-recoverable failure, `expired` when
      the device-code timed out (Tab5 should close the modal).
    * `error` — human-readable string when `state` is `error` or
      `expired`.  Tab5 surfaces this in the modal.
    """

    state: Literal["connecting", "connected", "error", "expired"]
    request_id: str
    error: Optional[str] = None


class IntegrationBackend(ABC):
    """Interface every integration implements."""

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
        """User-facing name shown in Tab5 Settings.

        Examples: ``"Google Calendar"``, ``"Gmail"``, ``"Home Assistant"``.
        """
        ...

    @property
    def description(self) -> str:
        """Optional longer description for Tab5 Settings.  Default
        returns the display name; subclasses can override."""
        return self.display_name

    @property
    @abstractmethod
    def auth_kind(self) -> Literal["oauth-device", "static-token", "none"]:
        """Drives the Tab5 connect-modal shape."""
        ...

    @abstractmethod
    async def is_connected(self) -> bool:
        """True when credentials exist + are usable.  No network call
        — read-only.  For network reachability use `health_check`."""
        ...

    @abstractmethod
    async def start_connect(self, params: Optional[dict] = None) -> ConnectChallenge:
        """Initiate the auth flow.

        Args:
            params: For ``static-token`` integrations Tab5 posts the
                completed field values here (e.g. ``{"base_url": "...",
                "long_lived_token": "..."}``).  For OAuth device-code
                it's typically unused.
        """
        ...

    @abstractmethod
    async def poll_status(self, request_id: str) -> ConnectionStatus:
        """Tab5 polls every few seconds while the user completes the
        OAuth flow on their phone.  Returns terminal state when done.
        """
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Revoke + delete stored credentials.

        Best-effort: if the provider's revoke endpoint fails (network
        down, token already invalid), still delete the local creds —
        the user explicitly asked to disconnect.
        """
        ...

    @abstractmethod
    async def health_check(self, timeout_s: float = 5.0) -> tuple[bool, str]:
        """Cheap reachability probe.

        Returns ``(ok, detail)``.  REST ``GET /api/v1/integrations/{name}/test``
        calls this.  `ok=False` with a short detail when the provider
        is unreachable or our credentials are stale.
        """
        ...

    async def shutdown(self) -> None:
        """Release any held connections (HTTP sessions, etc.)  Default
        is a no-op; integrations with persistent connections override.
        """
        return None
