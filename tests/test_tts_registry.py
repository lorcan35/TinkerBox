"""#338: TTS backend registry — decorator-driven plugin surface.

Pins:
  * 4 shipped backends register on `dragon_voice.tts` import.
  * Decorator rejects non-TTSBackend classes at import time.
  * Unknown name raises with the registered list in the message.
  * `create_tts(config)` dispatches via the registry.
  * Re-registering the same name is allowed (test fixtures need it).
"""

from __future__ import annotations

import pytest

from dragon_voice.config import TTSConfig
from dragon_voice.tts import create_tts, list_backends
from dragon_voice.tts.base import TTSBackend
from dragon_voice.tts.registry import (
    get_backend_class,
    is_registered,
    register_tts,
    _reset_for_tests,
)


# ─── Smoke: shipped backends are registered on import ──────────────


def test_shipped_backends_registered():
    """The four shipped backends must self-register on import of the
    `dragon_voice.tts` package — that's the future-proof promise."""
    expected = {"piper", "kokoro", "edge_tts", "openrouter"}
    assert expected.issubset(set(list_backends()))


@pytest.mark.parametrize("name", ["piper", "kokoro", "edge_tts", "openrouter"])
def test_each_shipped_backend_class_is_TTSBackend(name):
    cls = get_backend_class(name)
    assert issubclass(cls, TTSBackend)


# ─── Decorator behaviour ───────────────────────────────────────────


def test_register_tts_rejects_non_backend_class():
    """Catching the misuse at import time avoids a runtime surprise."""
    with pytest.raises(TypeError, match="not a TTSBackend subclass"):

        @register_tts("bogus")
        class NotABackend:
            pass

    # Did NOT register
    assert not is_registered("bogus")


def test_register_tts_requires_nonempty_name():
    with pytest.raises(ValueError):
        register_tts("")
    with pytest.raises(ValueError):
        register_tts("   ")


def test_register_tts_is_case_insensitive():
    """Config files have mixed-case backend names in the wild — make
    the lookup forgiving."""
    assert is_registered("Piper")
    assert is_registered("PIPER")
    assert is_registered("piper")


def test_register_tts_returns_class_for_chaining():
    """The decorator must return the class so `@register_tts("x")
    class Foo: ...` doesn't accidentally rebind `Foo` to None."""

    class _Tmp(TTSBackend):
        async def initialize(self):
            ...

        async def synthesize(self, text):
            return b""

        async def shutdown(self):
            ...

        @property
        def sample_rate(self):
            return 24000

        @property
        def name(self):
            return "tmp"

    returned = register_tts("tmp_test")(_Tmp)
    assert returned is _Tmp


# ─── Lookup behaviour ──────────────────────────────────────────────


def test_get_backend_class_unknown_raises_with_list():
    with pytest.raises(KeyError) as excinfo:
        get_backend_class("does_not_exist")
    msg = str(excinfo.value)
    assert "does_not_exist" in msg
    # Available list is included so the error is actionable
    assert "piper" in msg.lower()


def test_create_tts_unknown_backend_raises_valueerror():
    """Public `create_tts` translates the registry KeyError to
    ValueError so callers don't need to catch both."""
    cfg = TTSConfig(backend="does_not_exist")
    with pytest.raises(ValueError, match="does_not_exist"):
        create_tts(cfg)


# ─── _reset_for_tests fixture ──────────────────────────────────────


def test_reset_for_tests_clears_and_breaks_lookup():
    """The reset helper must restore a clean slate.  Wraps the reset
    in a try/finally so other tests in this module still see the
    shipped registry afterward (test-order independent)."""
    from dragon_voice.tts.registry import _REGISTRY
    snapshot = dict(_REGISTRY)
    try:
        _reset_for_tests()
        assert list_backends() == []
        with pytest.raises(KeyError):
            get_backend_class("piper")
    finally:
        _REGISTRY.update(snapshot)
    # Sanity: snapshot restored the shipped backends
    assert {"piper", "kokoro", "edge_tts", "openrouter"}.issubset(
        set(list_backends())
    )
