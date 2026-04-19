"""TTS backend factory — with process-level singletons.

See dragon_voice/stt/__init__.py for the rationale — same pattern
applies to Piper (~300 MB onnxruntime arena) and Kokoro (larger).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from dragon_voice.config import TTSConfig
from dragon_voice.tts.base import TTSBackend

logger = logging.getLogger(__name__)

_BACKENDS = {
    "piper": "dragon_voice.tts.piper_tts.PiperBackend",
    "kokoro": "dragon_voice.tts.kokoro_tts.KokoroBackend",
    "edge_tts": "dragon_voice.tts.edge_tts_backend.EdgeTTSBackend",
    "openrouter": "dragon_voice.tts.openrouter_tts.OpenRouterTTSBackend",
}

_singleton: Optional[TTSBackend] = None
_singleton_key: Optional[tuple[str, str]] = None
_singleton_lock = asyncio.Lock()


def _key_for(config: TTSConfig) -> tuple[str, str]:
    # Piper uses piper_model (voice file). Others use backend name as model.
    model = (
        getattr(config, "piper_model", None)
        or getattr(config, "model", None)
        or ""
    )
    return (config.backend.lower(), model)


def _build(config: TTSConfig) -> TTSBackend:
    backend_name = config.backend.lower()
    if backend_name not in _BACKENDS:
        available = ", ".join(sorted(_BACKENDS.keys()))
        raise ValueError(
            f"Unknown TTS backend '{backend_name}'. Available: {available}"
        )

    module_path, class_name = _BACKENDS[backend_name].rsplit(".", 1)

    import importlib

    try:
        module = importlib.import_module(module_path)
    except ImportError as e:
        raise ImportError(
            f"Cannot load TTS backend '{backend_name}': {e}. "
            f"Install the required package — see requirements.txt."
        ) from e

    cls = getattr(module, class_name)
    return cls(config)


async def get_tts_singleton(config: TTSConfig) -> TTSBackend:
    """Return an initialized TTS singleton for the given config."""
    global _singleton, _singleton_key

    key = _key_for(config)

    async with _singleton_lock:
        if _singleton is not None and _singleton_key == key:
            return _singleton

        if _singleton is not None:
            logger.info(
                "TTS singleton: evicting %s for %s", _singleton_key, key
            )
            try:
                await _singleton.shutdown()
            except Exception:
                logger.exception("TTS singleton eviction shutdown raised")
            _singleton = None
            _singleton_key = None

        inst = _build(config)
        await inst.initialize()
        _singleton = inst
        _singleton_key = key
        logger.info("TTS singleton: loaded %s", key)
        return _singleton


async def shutdown_tts_singleton() -> None:
    """Release the TTS singleton. Called on process shutdown only."""
    global _singleton, _singleton_key
    async with _singleton_lock:
        if _singleton is not None:
            try:
                await _singleton.shutdown()
            except Exception:
                logger.exception("TTS singleton final shutdown raised")
            _singleton = None
            _singleton_key = None


def create_tts(config: TTSConfig) -> TTSBackend:
    """DEPRECATED — prefer get_tts_singleton. Kept for legacy callers."""
    return _build(config)


__all__ = [
    "create_tts",
    "get_tts_singleton",
    "shutdown_tts_singleton",
    "TTSBackend",
]
