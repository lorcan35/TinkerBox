"""Fallback STT cache — pre-warm + lazy-load + shutdown.

Wave 23 SOLID-audit follow-up — thirtieth sub-extract.
Second slice from `dragon_voice/pipeline.py` (audit SRP-4: the
1733-LOC `VoicePipeline` class owns 5+ responsibilities; the
fallback-STT lifecycle is one of them).

Cloud STT (OpenRouter) can fail mid-call (timeout, API error,
network glitch) and Dragon falls back to local Moonshine to
salvage the user's words.  Without pre-warm, the first cloud
failure pays the 1-3 s Moonshine cold-load before any
transcription — the user hears nothing for the bridge window
and assumes Dragon broke.

This module owns the pre-warm + lazy-load + shutdown lifecycle:

  * `schedule_prewarm()` — fire-and-forget background load
    when the active STT swaps to `openrouter` (audit B6 / #154).
  * `transcribe(audio, sample_rate)` — borrow the pre-warmed
    Moonshine OR cold-load if the prewarm hasn't finished yet.
  * `shutdown()` — cancel any in-flight prewarm + shutdown the
    cached backend.

Pre-extract these were two methods + scattered state on
VoicePipeline (`_fallback_stt`, `_fallback_prewarm_task`).  Now
encapsulated in a single class.

## API

```python
cache = FallbackSttCache()

# When swap_backends sets STT to cloud:
cache.schedule_prewarm()

# When cloud STT fails mid-utterance:
transcript = await cache.transcribe(audio, sample_rate=16000)

# On pipeline shutdown:
await cache.shutdown()
```

## Race guard preserved

`schedule_prewarm` is idempotent — if a fallback already exists
or a prewarm is already in flight, it no-ops.  When a real
cloud-STT failure cold-loads in parallel with the prewarm,
last-writer-wins; the loser is shut down to avoid leaking a
Moonshine instance.

## Why the imports are inside the methods

`create_stt` + `STTConfig` imports are deferred to the first
prewarm/transcribe call — keeps the module import cost zero
for connections that never hit the fallback path.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class FallbackSttCache:
    """Pre-warm + lazy-load + shutdown lifecycle for the
    Moonshine fallback STT backend.

    Owns two pieces of state:
      * `_fallback_stt` — the cached Moonshine STTBackend (or
        None if not yet loaded).
      * `_prewarm_task` — the in-flight prewarm asyncio.Task
        (or None when not running / completed).

    Both are private; callers should use the three public
    methods (schedule_prewarm, transcribe, shutdown).
    """

    def __init__(self) -> None:
        self._fallback_stt: Optional[Any] = None  # STTBackend
        self._prewarm_task: Optional[asyncio.Task] = None

    def schedule_prewarm(self) -> None:
        """Spawn a background task that loads Moonshine into the
        cache so the first cloud-STT failure doesn't pay the
        1-3 s cold-load.

        Audit B6 (#154): called from `VoicePipeline.swap_backends`
        whenever the new STT backend is `openrouter`.
        Idempotent — if a fallback already exists or a prewarm
        is already in flight, no-ops.
        """
        if self._fallback_stt is not None:
            return
        if self._prewarm_task is not None and not self._prewarm_task.done():
            return

        # download.moonshine.ai returns 404 for all models since 2026-05.
        # Tiny was never cached locally; medium-only path works because the
        # cache predates the URL outage. Skip prewarm to avoid log spam;
        # the lazy path in transcribe() will surface a clear error if
        # ever invoked.
        logger.info('Fallback STT prewarm skipped (moonshine.ai download endpoint dead; medium-only operation)')
        return

        async def _prewarm() -> None:
            try:
                from dragon_voice.config import STTConfig
                from dragon_voice.stt import create_stt
                stt = create_stt(STTConfig(backend="moonshine"))
                await stt.initialize()
                # Race guard: a real cloud-STT failure could have
                # cold-loaded one in parallel.  Last-writer-wins;
                # the loser is shut down to avoid leaking a
                # Moonshine instance.
                if self._fallback_stt is None:
                    self._fallback_stt = stt
                    logger.info(
                        "Pre-warmed fallback STT (moonshine) cached "
                        "(background)",
                    )
                else:
                    await stt.shutdown()
            except Exception as e:
                logger.warning("B6 fallback-STT prewarm failed: %s", e)

        self._prewarm_task = asyncio.create_task(
            _prewarm(), name="b6_fallback_stt_prewarm",
        )

    async def transcribe(
        self,
        audio_data: bytes,
        *,
        sample_rate: int,
    ) -> str:
        """Borrow the pre-warmed fallback STT (or cold-load if
        absent), then transcribe `audio_data`.

        Audit B6 (#154): keeps the fallback path single-line at
        the callsite while preserving the v4·D P1 caching
        behaviour and respecting the swap_backends pre-warm.
        """
        if self._fallback_stt is None:
            from dragon_voice.config import STTConfig
            from dragon_voice.stt import create_stt
            self._fallback_stt = create_stt(STTConfig(backend="moonshine"))
            await self._fallback_stt.initialize()
            logger.info(
                "Pre-warmed fallback STT (moonshine) cached (lazy path)",
            )
        return await self._fallback_stt.transcribe(audio_data, sample_rate)

    async def shutdown(self) -> None:
        """Cancel any in-flight prewarm + shutdown the cached
        backend.  Called from `VoicePipeline.shutdown`.

        Audit B6 (#154): cancel the prewarm so shutdown doesn't
        have to wait for a 1-3 s model load just to immediately
        throw it away.
        """
        # Cancel + await the prewarm task so it can't outlive
        # us.  Both CancelledError and any exception from the
        # task body are swallowed.
        if self._prewarm_task is not None and not self._prewarm_task.done():
            self._prewarm_task.cancel()
            try:
                await self._prewarm_task
            except (asyncio.CancelledError, Exception):
                pass
        self._prewarm_task = None

        # Shutdown the cached backend if loaded.
        if self._fallback_stt is not None:
            try:
                await self._fallback_stt.shutdown()
            except Exception:
                logger.debug("Fallback STT shutdown raised", exc_info=True)
        self._fallback_stt = None

    @property
    def is_loaded(self) -> bool:
        """True iff the fallback backend is loaded + ready to
        transcribe without paying the cold-load.  Used by tests
        + diagnostics; production code calls `transcribe`
        directly which handles the lazy-load path itself."""
        return self._fallback_stt is not None
