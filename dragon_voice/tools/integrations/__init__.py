"""TinkerBox integrations layer (#341 / Phase 1 #342).

Per-integration adapters that own OAuth lifecycle, credential storage,
and expose Dragon `Tool` instances usable across all voice modes.  See
`docs/PLAN-tinkerbox-integrations.md` for the architecture; this
package's __init__ just exposes the public surface + triggers
side-effect imports so the registry self-populates.

Public API:
  * `IntegrationBackend` — ABC every integration implements
  * `register_integration(name)` — decorator, mirrors `tts/registry.py`
  * `create_integration(name)` — factory by registered name
  * `list_integrations()` — registered names
  * `OAuthDeviceCodeClient` — shared device-code flow client
  * `CredentialStore` — per-integration JSON cred file (0o600)
"""

from __future__ import annotations

from dragon_voice.tools.integrations.base import (
    ConnectChallenge,
    ConnectionStatus,
    IntegrationBackend,
    IntegrationState,
)
from dragon_voice.tools.integrations.credentials import CredentialStore
from dragon_voice.tools.integrations.oauth import (
    DeviceCodeError,
    OAuthDeviceCodeClient,
    OAuthTokens,
)
from dragon_voice.tools.integrations.registry import (
    create_integration,
    get_integration_class,
    is_registered,
    list_integrations,
    register_integration,
)

# Side-effect imports so backends self-register on package load.
# Adding a new integration = drop a module + decorator + one import line.
from dragon_voice.tools.integrations.google import calendar as _google_calendar  # noqa: F401

__all__ = [
    "ConnectChallenge",
    "ConnectionStatus",
    "CredentialStore",
    "DeviceCodeError",
    "IntegrationBackend",
    "IntegrationState",
    "OAuthDeviceCodeClient",
    "OAuthTokens",
    "create_integration",
    "get_integration_class",
    "is_registered",
    "list_integrations",
    "register_integration",
]
