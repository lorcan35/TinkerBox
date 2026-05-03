"""Fallback TTS cache — lazy-load + synthesize + shutdown.

Wave 23 SOLID-audit follow-up — thirty-first sub-extract.
Third slice from `dragon_voice/pipeline.py` (audit SRP-4: the
1622-LOC `VoicePipeline` class still owns 4+ responsibilities;
this is the TTS fallback lifecycle).

Symmetric counterpart of `FallbackSttCache` (PR #256).  When
the configured cloud TTS (OpenRouter) fails or times out, the
voice path falls back to local Piper to salvage the synthesis.
Without caching, repeated cloud failures pay the Piper cold-
load every time — expensive on Q6A ARM64 (~500 ms per cold
init).

This module owns the cache + lazy-load + shutdown lifecycle.
Unlike STT, TTS doesn't have a "swap to cloud" trigger that
warrants pre-warm — fallback firings are rare (cloud TTS is
quite reliable) so the lazy-load path is the only one needed.

## API

```python
cache = FallbackTtsCache()

# When cloud TTS fails mid-utterance:
audio = await cache.synthesize(text, timeout_s=90.0)

# On pipeline shutdown:
await cache.shutdown()
```

## Audit C3 / Phase 2 L3 (#137 / #94) closures preserved

* The 90 s default timeout matches the audit C3 budget for
  the local Piper path (Piper takes 15-25 s for a 200-word
  reply on Q6A ARM64; 30 s was too tight).
* On synth failure, `kill_active_procs` is invoked on the
  cached Piper (Phase 2 L3) so the in-flight subprocess
  doesn't leak the audio device + an FD until Python exits.
* `synthesize` re-raises the failure after killing — the
  caller decides whether to surface the error to the user
  (the voice path emits a config_update auto-revert frame).

## Why the imports are inside the methods

`create_tts` + `TTSConfig` imports are deferred to the first
synthesize call — keeps the module import cost zero for
connections that never hit the fallback path.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


# Audit C3 (#137): Piper takes 15-25 s for a 200-word reply on
# Q6A ARM64.  90 s leaves headroom; 30 s was too tight on the
# pre-#137 voice path.
_DEFAULT_FALLBACK_TIMEOUT_S = 90.0


class FallbackTtsCache:
    """Lazy-load + cache for the local Piper TTS fallback.

    Owns one piece of state: `_fallback_tts` — the cached
    Piper TTSBackend (or None if not yet loaded).
    """

    def __init__(self) -> None:
        self._fallback_tts: Optional[Any] = None  # TTSBackend

    async def synthesize(
        self,
        text: str,
        *,
        timeout_s: float = _DEFAULT_FALLBACK_TIMEOUT_S,
    ) -> bytes:
        """Borrow the cached Piper backend (or cold-load if
        absent), then synthesize `text` with a wait_for cap.

        On synth failure / timeout, calls `kill_active_procs`
        on the cached Piper (Phase 2 L3 / #94) to prevent the
        in-flight subprocess from leaking the audio device.
        Re-raises the failure — the caller decides what user-
        facing message to surface.

        Args:
            text: Text to synthesize.
            timeout_s: wait_for budget (default 90 s, audit C3).

        Returns:
            PCM audio bytes at the backend's sample rate.

        Raises:
            asyncio.TimeoutError: if synthesis takes > timeout_s.
            Exception: any other synth backend failure.
        """
        if self._fallback_tts is None:
            from dragon_voice.config import TTSConfig
            from dragon_voice.tts import create_tts
            self._fallback_tts = create_tts(TTSConfig(backend="piper"))
            await self._fallback_tts.initialize()
            logger.info("Pre-warmed fallback TTS (piper) cached")

        try:
            return await asyncio.wait_for(
                self._fallback_tts.synthesize(text), timeout=timeout_s,
            )
        except (Exception, asyncio.TimeoutError):
            # Phase 2 L3 (#94): kill any in-flight Piper subproc
            # before re-raising so the audio device + FD don't
            # leak.  hasattr-gated because non-Piper backends
            # don't expose this method.
            if hasattr(self._fallback_tts, "kill_active_procs"):
                self._fallback_tts.kill_active_procs()
            raise

    async def shutdown(self) -> None:
        """Release the cached backend.  Called from
        `VoicePipeline.shutdown`.

        Failure isolation: shutdown exceptions logged at DEBUG
        but never re-raised — pipeline shutdown shouldn't
        cascade.
        """
        if self._fallback_tts is not None:
            try:
                await self._fallback_tts.shutdown()
            except Exception:
                logger.debug("Fallback TTS shutdown raised", exc_info=True)
        self._fallback_tts = None

    @property
    def is_loaded(self) -> bool:
        """True iff the fallback Piper is loaded + ready to
        synthesize without paying the cold-load."""
        return self._fallback_tts is not None
