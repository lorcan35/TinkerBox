"""TTS package — registry-driven factory.

#338: replaced the hardcoded if/elif `create_tts` ladder with a
decorator-based registry (`dragon_voice/tts/registry.py`).  Backends
self-register via `@register_tts("name")` when their module is
imported; this `__init__` imports the four shipped backends once at
package load so the registry is populated before any caller hits
`create_tts(...)`.

Adding a new TTS backend (Chatterbox, CosyVoice 2, MeloTTS, …) is a
one-decorator change:

    # dragon_voice/tts/cosyvoice_tts.py
    from dragon_voice.tts.base import TTSBackend
    from dragon_voice.tts.registry import register_tts

    @register_tts("cosyvoice")
    class CosyVoiceBackend(TTSBackend): ...

    # dragon_voice/tts/__init__.py — add one line:
    from dragon_voice.tts import cosyvoice_tts  # noqa: F401
"""

from __future__ import annotations

from dragon_voice.config import TTSConfig
from dragon_voice.tts.base import TTSBackend
from dragon_voice.tts.registry import (
    get_backend_class,
    is_registered,
    list_backends,
    register_tts,
)
from dragon_voice.tts.text_cleaner import clean_for_tts

# Import backend modules so their @register_tts decorators run on
# package load.  Order is alphabetical for predictability; the
# registry doesn't care.
from dragon_voice.tts import (  # noqa: F401
    edge_tts_backend,
    kokoro_tts,
    openrouter_tts,
    piper_tts,
)


def create_tts(config: TTSConfig) -> TTSBackend:
    """Create a TTS backend from configuration.

    Uses the registry built by `@register_tts("name")` decorators on
    each backend class.  The 4 shipped backends are imported above so
    they're registered before this function is called.

    Raises ValueError if the requested backend name isn't registered,
    ImportError if the backend module's runtime deps are missing
    (each backend raises a clear "pip install X" message inside its
    own `initialize()` — we don't try to detect that here).
    """
    name = (config.backend or "").strip()
    try:
        cls = get_backend_class(name)
    except KeyError as e:
        raise ValueError(str(e)) from e
    return cls(config)


__all__ = [
    "TTSBackend",
    "clean_for_tts",
    "create_tts",
    "get_backend_class",
    "is_registered",
    "list_backends",
    "register_tts",
]
