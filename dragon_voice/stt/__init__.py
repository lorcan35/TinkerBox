"""STT backend factory — with process-level singletons.

Models (Moonshine ONNX ~2.5 GB mmap, Whisper.cpp ~500 MB) are expensive
to load and their C-extension mmap arenas don't release cleanly on GC.
Pre-fix-5 every VoicePipeline.initialize() rebuilt + re-initialized
backends, stacking arena copies and driving the RSS leak tracked in
#29 (60 MB → 3.6 GB in 25 min of normal use).

Fix: cache initialized backends by config fingerprint. A VoicePipeline
switching from mode A to mode B shuts down the old singleton explicitly
(fix 6 of #29 releases the mmap via del + gc.collect) before loading
the new one. Within a mode, every pipeline restart shares the same
already-loaded backend — no reload.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from dragon_voice.config import STTConfig
from dragon_voice.stt.base import STTBackend

logger = logging.getLogger(__name__)

_BACKENDS = {
    "whisper_cpp": "dragon_voice.stt.whisper_cpp.WhisperCppBackend",
    "moonshine": "dragon_voice.stt.moonshine_stt.MoonshineBackend",
    "vosk": "dragon_voice.stt.vosk_stt.VoskBackend",
    "openrouter": "dragon_voice.stt.openrouter_stt.OpenRouterSTTBackend",
}

# ── Process-wide singleton cache ──────────────────────────────────
# Exactly ONE STT instance per (backend, model) key. Switching modes
# evicts the prior singleton so its mmap releases.
_singleton: Optional[STTBackend] = None
_singleton_key: Optional[tuple[str, str]] = None
_singleton_lock = asyncio.Lock()


def _key_for(config: STTConfig) -> tuple[str, str]:
    return (config.backend.lower(), getattr(config, "model", "") or "")


def _build(config: STTConfig) -> STTBackend:
    """Resolve the backend class and instantiate (uninitialized)."""
    backend_name = config.backend.lower()
    if backend_name not in _BACKENDS:
        available = ", ".join(sorted(_BACKENDS.keys()))
        raise ValueError(
            f"Unknown STT backend '{backend_name}'. Available: {available}"
        )

    module_path, class_name = _BACKENDS[backend_name].rsplit(".", 1)

    import importlib

    try:
        module = importlib.import_module(module_path)
    except ImportError as e:
        raise ImportError(
            f"Cannot load STT backend '{backend_name}': {e}. "
            f"Install the required package — see requirements.txt."
        ) from e

    cls = getattr(module, class_name)
    return cls(config)


async def get_stt_singleton(config: STTConfig) -> STTBackend:
    """Return an initialized STT singleton for the given config.

    First call for a given (backend, model) key loads + initializes
    the model. Subsequent calls with the same key return the cached
    instance. A different key shuts down the prior singleton first.

    Concurrency: guarded by _singleton_lock so two pipeline starts
    can't race to load the model twice.
    """
    global _singleton, _singleton_key

    key = _key_for(config)

    async with _singleton_lock:
        if _singleton is not None and _singleton_key == key:
            # Fast path — same config, return cached already-initialized
            return _singleton

        # Evict previous singleton if any — pair with fix 6's gc.collect
        if _singleton is not None:
            logger.info(
                "STT singleton: evicting %s for %s", _singleton_key, key
            )
            try:
                await _singleton.shutdown()
            except Exception:
                logger.exception("STT singleton eviction shutdown raised")
            _singleton = None
            _singleton_key = None

        # Build + initialize new
        inst = _build(config)
        await inst.initialize()
        _singleton = inst
        _singleton_key = key
        logger.info("STT singleton: loaded %s", key)
        return _singleton


async def shutdown_stt_singleton() -> None:
    """Release the STT singleton. Called on process shutdown only."""
    global _singleton, _singleton_key
    async with _singleton_lock:
        if _singleton is not None:
            try:
                await _singleton.shutdown()
            except Exception:
                logger.exception("STT singleton final shutdown raised")
            _singleton = None
            _singleton_key = None


def create_stt(config: STTConfig) -> STTBackend:
    """DEPRECATED — prefer get_stt_singleton.

    Kept so any legacy caller that builds an STT outside the pipeline
    keeps working. Does NOT use the singleton cache (builds a fresh
    instance every call), so memory pressure returns if called at
    scale. VoicePipeline switched to get_stt_singleton in fix 5 of #29.
    """
    return _build(config)


__all__ = [
    "create_stt",
    "get_stt_singleton",
    "shutdown_stt_singleton",
    "STTBackend",
]
