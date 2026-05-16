"""#341 / #342 — IntegrationBackend ABC + registry decorator behavior.

Mirrors the shape of `tests/test_tts_registry.py` since the
registry is intentionally the same pattern.
"""

from __future__ import annotations

import pytest

from dragon_voice.tools.integrations import (
    IntegrationBackend,
    create_integration,
    get_integration_class,
    is_registered,
    list_integrations,
    register_integration,
)
from dragon_voice.tools.integrations.registry import _REGISTRY, _reset_for_tests


def test_google_calendar_registered_on_import():
    """Side-effect import in __init__.py must register the bundled
    integrations on package load."""
    assert "google-calendar" in list_integrations()
    cls = get_integration_class("google-calendar")
    assert issubclass(cls, IntegrationBackend)


def test_decorator_rejects_non_subclass():
    with pytest.raises(TypeError, match="not an IntegrationBackend"):

        @register_integration("bogus-not-an-integration")
        class NotAnIntegration:
            pass

    assert not is_registered("bogus-not-an-integration")


def test_decorator_requires_nonempty_name():
    with pytest.raises(ValueError):
        register_integration("")
    with pytest.raises(ValueError):
        register_integration("   ")


def test_lookup_is_case_insensitive():
    assert is_registered("Google-Calendar")
    assert is_registered("GOOGLE-CALENDAR")
    assert is_registered("google-calendar")


def test_get_unknown_raises_with_available_list():
    with pytest.raises(KeyError) as excinfo:
        get_integration_class("does-not-exist")
    assert "does-not-exist" in str(excinfo.value)
    assert "google-calendar" in str(excinfo.value).lower()


def test_create_integration_returns_instance():
    integ = create_integration("google-calendar")
    assert isinstance(integ, IntegrationBackend)
    assert integ.name == "google-calendar"
    assert integ.display_name == "Google Calendar"
    assert integ.auth_kind == "oauth-device"


def test_reset_clears_then_restores_from_snapshot():
    """The reset helper used by tests must allow restoring after."""
    snapshot = dict(_REGISTRY)
    try:
        _reset_for_tests()
        assert list_integrations() == []
        with pytest.raises(KeyError):
            get_integration_class("google-calendar")
    finally:
        _REGISTRY.update(snapshot)
    assert "google-calendar" in list_integrations()
