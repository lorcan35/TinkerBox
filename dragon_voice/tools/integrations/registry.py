"""Integration registry — decorator-driven plugin surface (#341).

Mirrors the TTS registry pattern from PR #339.  Adding a new
integration (e.g. ``OutlookCalendarIntegration``) is a one-decorator
drop-in:

    @register_integration("outlook-calendar")
    class OutlookCalendarIntegration(IntegrationBackend): ...

Then add the side-effect import to
``dragon_voice/tools/integrations/__init__.py`` so the decorator runs
on package load.

The registry intentionally stores **classes**, not instances — callers
construct the instance at use time, passing any needed deps (e.g. an
aiohttp session).
"""

from __future__ import annotations

from typing import Callable, TypeVar

from dragon_voice.tools.integrations.base import IntegrationBackend

_T = TypeVar("_T", bound=type[IntegrationBackend])

_REGISTRY: dict[str, type[IntegrationBackend]] = {}


def register_integration(name: str) -> Callable[[_T], _T]:
    """Decorator: register an IntegrationBackend subclass under `name`.

    Names are lower-cased on registration and lookup so config values
    match case-insensitively.  Re-registering the same name silently
    overrides (lets tests patch in a fake).  TypeError raised at import
    time if applied to a non-subclass — fails-fast.
    """
    key = name.strip().lower()
    if not key:
        raise ValueError("register_integration: name must be non-empty")

    def _decorator(cls: _T) -> _T:
        if not isinstance(cls, type) or not issubclass(cls, IntegrationBackend):
            raise TypeError(
                f"register_integration('{key}') applied to {cls!r}, which is "
                f"not an IntegrationBackend subclass."
            )
        _REGISTRY[key] = cls
        return cls

    return _decorator


def get_integration_class(name: str) -> type[IntegrationBackend]:
    """Look up a registered integration class.  Raises KeyError with
    the full list of registered names in the message if `name` is
    unknown."""
    key = (name or "").strip().lower()
    if key not in _REGISTRY:
        available = ", ".join(sorted(_REGISTRY.keys())) or "(none)"
        raise KeyError(
            f"Unknown integration '{name}'. Available: {available}"
        )
    return _REGISTRY[key]


def create_integration(name: str) -> IntegrationBackend:
    """Instantiate a registered integration by name.  No constructor
    args supported here — for integrations with config deps, look up
    the class via `get_integration_class` and instantiate explicitly.
    """
    cls = get_integration_class(name)
    return cls()


def list_integrations() -> list[str]:
    """Return registered integration names, alphabetically sorted."""
    return sorted(_REGISTRY.keys())


def is_registered(name: str) -> bool:
    return (name or "").strip().lower() in _REGISTRY


def _reset_for_tests() -> None:
    """Clear the registry — only used by unit tests."""
    _REGISTRY.clear()
