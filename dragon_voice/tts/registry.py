"""TTS backend registry — #338.

Future-proofs the TTS surface so adding a new model (Chatterbox-Turbo,
CosyVoice 2, MeloTTS, MatchaTTS, KittenTTS, …) is a one-decorator drop-in
rather than an edit of the central `create_tts` factory.

Usage in a backend module:

    from dragon_voice.tts.base import TTSBackend
    from dragon_voice.tts.registry import register_tts

    @register_tts("kokoro")
    class KokoroBackend(TTSBackend):
        ...

Then `dragon_voice/tts/__init__.py` only needs to `import` the backend
module once at startup — the decorator handles the registration.

The registry is intentionally a flat string→class map.  No priority,
no fallback, no auto-discovery — those are orchestration concerns the
caller (`config_swap.py`) already handles.
"""

from __future__ import annotations

from typing import Callable, TypeVar

from dragon_voice.tts.base import TTSBackend

_T = TypeVar("_T", bound=type[TTSBackend])

_REGISTRY: dict[str, type[TTSBackend]] = {}


def register_tts(name: str) -> Callable[[_T], _T]:
    """Decorator: register a TTSBackend subclass under `name`.

    Names are lower-cased on registration and lookup so config values
    match case-insensitively.  Re-registering the same name silently
    overrides the prior entry (lets a downstream patch swap in a
    drop-in replacement during tests).

    Raises TypeError if applied to a class that isn't a TTSBackend
    subclass — caught at import time, not at first use.
    """
    key = name.strip().lower()
    if not key:
        raise ValueError("register_tts: name must be non-empty")

    def _decorator(cls: _T) -> _T:
        if not isinstance(cls, type) or not issubclass(cls, TTSBackend):
            raise TypeError(
                f"register_tts('{key}') applied to {cls!r}, which is "
                f"not a TTSBackend subclass."
            )
        _REGISTRY[key] = cls
        return cls

    return _decorator


def get_backend_class(name: str) -> type[TTSBackend]:
    """Look up a registered backend class.  Raises KeyError with the
    full list of registered names if `name` is unknown."""
    key = (name or "").strip().lower()
    if key not in _REGISTRY:
        available = ", ".join(sorted(_REGISTRY.keys())) or "(none)"
        raise KeyError(
            f"Unknown TTS backend '{name}'. Available: {available}"
        )
    return _REGISTRY[key]


def list_backends() -> list[str]:
    """Return the registered backend names, alphabetically sorted."""
    return sorted(_REGISTRY.keys())


def is_registered(name: str) -> bool:
    return (name or "").strip().lower() in _REGISTRY


def _reset_for_tests() -> None:
    """Clear the registry — only used by unit tests that want to
    assert decorator behavior from a clean slate."""
    _REGISTRY.clear()
