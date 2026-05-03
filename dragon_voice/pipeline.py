"""Voice pipeline orchestrator: Audio -> STT -> LLM -> TTS -> Audio.

Receives raw PCM audio, detects end-of-speech via simple VAD, transcribes
with STT, streams LLM response, buffers until sentence boundaries, and
synthesizes each sentence with TTS. Results are delivered via async callback.
"""

import asyncio
import logging
import re
import time

import aiohttp
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Awaitable, Optional

import numpy as np

from dragon_voice.config import VoiceConfig
from dragon_voice.errors import DragonError, Scope, Severity, error_event
from dragon_voice.progress import Phase, Stage
from dragon_voice.progress_emit import emit_progress_pair
from dragon_voice.stt import create_stt, STTBackend
from dragon_voice.tts import create_tts, TTSBackend
from dragon_voice.llm import create_llm, LLMBackend
from dragon_voice.llm.base import (
    SupportsClearHistory,
    SupportsHistoryTrim,
    SupportsSessionKey,
    SupportsUsage,
)
from dragon_voice.audio import resample_pcm16_async
from dragon_voice.tools.response_wrap import looks_like_useful_text, synthesize_wrap

logger = logging.getLogger(__name__)

# Dedicated thread pool for CPU-bound STT/TTS inference.
# Separates inference threads from the default executor (used for I/O-bound
# tasks like DB queries and HTTP requests) so they don't compete for GIL time.
inference_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="inference")


def shutdown_inference_executor(wait: bool = False) -> None:
    """v4·D audit P0 fix: let the voice server shut this pool down on
    application exit.  Previously zombie inference threads lingered on
    uvloop teardown; `wait=False` lets us shutdown without blocking the
    aiohttp shutdown sequence, while still releasing the worker
    handles. """
    try:
        inference_executor.shutdown(wait=wait, cancel_futures=True)
    except TypeError:
        # Python < 3.9 doesn't support cancel_futures
        inference_executor.shutdown(wait=wait)

# SOLID-audit follow-up (PR #264): token-flush helpers +
# constants extracted to dragon_voice.token_flush.  Re-exported
# here under their pre-extract names so existing tests +
# call sites keep working unchanged.
from dragon_voice.token_flush import (  # noqa: F401  (re-exports preserve names)
    CLAUSE_END as _CLAUSE_END,
    HALLUCINATION_STOPS as _HALLUCINATION_STOPS,
    LAST_WORD_BOUNDARY as _LAST_WORD_BOUNDARY,
    LOCAL_TIMEOUT_FLUSH_MIN_CHARS as _LOCAL_TIMEOUT_FLUSH_MIN_CHARS,
    LOCAL_TIMEOUT_FLUSH_S as _LOCAL_TIMEOUT_FLUSH_S,
    SENTENCE_END as _SENTENCE_END,
    SENTENCE_SPLIT as _SENTENCE_SPLIT,
    TRIPLE_BACKTICK as _TRIPLE_BACKTICK,
)


# VAD constants
_SILENCE_THRESHOLD = 500  # RMS amplitude below this = silence (int16 range)

# P06: Max audio buffer size — 5 minutes at 16kHz 16-bit mono = 9.6MB.
# Prevents unbounded memory growth from long dictation sessions or stuck clients.
MAX_AUDIO_BUFFER = 5 * 60 * 16000 * 2  # 9,600,000 bytes


# Wave 15 W15-C01: stable signatures for the shared backend pool.  Two
# pipelines with the same (backend, model) signature can share a
# backend instance — the per-connection state (audio buffer, VAD,
# conversation) lives on the pipeline itself, not on the backend.
def _stt_sig(stt_config) -> tuple:
    return ("stt", stt_config.backend, getattr(stt_config, "model", ""))


def _tts_sig(tts_config) -> tuple:
    return (
        "tts",
        tts_config.backend,
        getattr(tts_config, "piper_model", "")
        or getattr(tts_config, "kokoro_model", "")
        or "",
    )


def _llm_sig(llm_config) -> tuple:
    # Model name varies per backend — grab the one that's actually used
    # so a pool hit requires *identical* model too (otherwise reloading
    # the LLM is semantically required).
    be = llm_config.backend
    if be == "ollama":
        model = getattr(llm_config, "ollama_model", "")
    elif be == "openrouter":
        model = getattr(llm_config, "openrouter_model", "")
    elif be == "tinkerclaw":
        model = getattr(llm_config, "tinkerclaw_model", "")
    elif be == "lmstudio":
        model = getattr(llm_config, "lmstudio_model", "")
    elif be == "dual":
        # Two-part identity for the dual-model pipeline (#80): both
        # picker AND responder need to match for a pool hit, otherwise
        # swapping either half would silently reuse the old combo.
        picker = getattr(llm_config, "dual_picker_model", "")
        responder = getattr(llm_config, "dual_responder_model", "")
        model = f"{picker}|{responder}"
    else:
        model = ""
    return ("llm", be, model)


class VoicePipeline:
    """Orchestrates the full STT -> LLM -> TTS voice pipeline.

    One pipeline instance per WebSocket session. Manages audio buffering,
    VAD, transcription, LLM streaming, sentence buffering, and TTS synthesis.
    """

    def __init__(
        self,
        config: VoiceConfig,
        on_audio: Callable[[bytes], Awaitable[None]],
        on_event: Callable[[dict], Awaitable[None]],
        conversation_engine=None,
        session_id: str = "",
        media_pipeline=None,
        backend_pool: Optional[dict] = None,
        on_tool_call: Optional[Callable[[dict], Awaitable[None]]] = None,
        on_tool_result: Optional[Callable[[dict], Awaitable[None]]] = None,
        on_tool_error: Optional[Callable[[dict], Awaitable[None]]] = None,
        surface_mgr=None,
    ) -> None:
        """Initialize the pipeline.

        Args:
            config: Full voice configuration.
            on_audio: Async callback invoked with PCM int16 audio chunks
                     to send back to the client.
            on_event: Async callback invoked with JSON-serializable event
                     dicts (stt results, llm tokens, status, errors).
            conversation_engine: Optional ConversationEngine for multi-turn.
                               If provided, LLM calls go through the engine
                               (which stores messages in DB for context).
            session_id: Active session ID (required if conversation_engine is set).
            media_pipeline: Optional MediaPipeline for rich media detection.
            backend_pool: Optional dict for sharing backends across pipelines
                (see W15-C01).  When provided, compatible backends are
                borrowed from the pool instead of re-initialising them.
            on_tool_call / on_tool_result / on_tool_error: Optional async
                callbacks invoked when ConversationEngine fires a tool.
                Audit A2 (#142): voice path used to drop these silently;
                wiring them in lets the Tab5 chat surface tool indicators
                on voice turns the same way it already does on text turns.
        """
        self._config = config
        self._on_audio = on_audio
        self._on_event = on_event
        self._conversation_engine = conversation_engine
        self._session_id = session_id
        self._media_pipeline = media_pipeline
        # Audit B1 (#165): surface_mgr lets the pipeline tell the
        # SurfaceManager when a turn is in flight so out-of-band emits
        # (scheduler reminder fires) defer until the turn ends instead
        # of interleaving between LLM token frames.
        self._surface_mgr = surface_mgr
        self._on_tool_call = on_tool_call
        self._on_tool_result = on_tool_result
        self._on_tool_error = on_tool_error
        # Audit A3 (#142): per-utterance tool-call tracker.  Mirrors the
        # text path's `conn_state["tool_calls_this_turn"]`; populated by
        # the wrapped callbacks below and read by the empty-reply guard
        # so an FC-style tool-only turn produces a per-tool wrap instead
        # of the legacy generic apology.
        self._tool_calls_this_turn: list[dict] = []

        self._stt: Optional[STTBackend] = None
        self._tts: Optional[TTSBackend] = None
        self._llm: Optional[LLMBackend] = None

        # Wave 15 W15-C01: backend-pool support — when the server hands
        # us a pool dict (keyed by a stable signature tuple), we reuse
        # existing backend instances across WS reconnects instead of
        # reloading the ~140 MB Moonshine model every time Tab5 bounces.
        # When a backend is borrowed from the pool, _pooled_* flips True
        # so shutdown() knows to skip .shutdown() on it.
        self._backend_pool: Optional[dict] = backend_pool
        self._pooled_stt = False
        self._pooled_tts = False
        self._pooled_llm = False

        # Audio buffer for incoming PCM data
        self._audio_buffer = bytearray()
        self._last_voice_time = 0.0
        self._is_speaking = False

        # #173 / TinkerTab #262: per-direction audio codec.  Default
        # PCM (raw int16 binary frames as today).  set_uplink_codec()
        # is called by server.py after the WS register handshake when
        # the client advertised opus support.
        self._uplink_codec = "pcm"
        self._uplink_opus_dec = None  # OpusUplinkDecoder, lazy-init

        # Conversation history (last N turns) — only used without conversation_engine
        self._max_history = 10

        # Pipeline state
        self._processing = False
        self._cancelled = False
        self._swapping = False  # US-P01: drop incoming audio during backend swap
        self._tts_started = False
        self._tts_total_ms = 0.0
        self._process_task: Optional[asyncio.Task] = None
        self._post_process_task: Optional[asyncio.Task] = None  # DQ22: track post-processing task

        # Dictation mode state
        self._dictation_mode = False
        self._segment_buffer = bytearray()
        self._dictation_segments: list[str] = []

        # SOLID-audit follow-up: fallback STT lifecycle (pre-warm
        # + lazy-load + shutdown) extracted to FallbackSttCache.
        # Audit B6 (#154): cloud STT failures fall back to
        # Moonshine; without pre-warm, the first failure pays
        # the 1-3 s cold-load.
        from dragon_voice.fallback_stt_cache import FallbackSttCache
        self._fallback_stt_cache = FallbackSttCache()

        # SOLID-audit follow-up: fallback TTS cache mirrors the
        # STT one — when cloud TTS fails, fall back to local
        # Piper.  Lazy-load only (TTS doesn't have a "swap to
        # cloud" trigger that warrants pre-warm).
        from dragon_voice.fallback_tts_cache import FallbackTtsCache
        self._fallback_tts_cache = FallbackTtsCache()

    async def initialize(self) -> None:
        """Create and initialize all backends.

        W15-C01: when `self._backend_pool` is set, backends are borrowed
        by signature and their `.initialize()` runs at most once per
        server lifetime.  Tab5 reconnects no longer trigger a Moonshine
        model reload (~140 MB / reconnect leak).
        """
        logger.info("Initializing voice pipeline...")

        pool = self._backend_pool
        stt_key = _stt_sig(self._config.stt)
        tts_key = _tts_sig(self._config.tts)
        llm_key = _llm_sig(self._config.llm)

        stt_new = False
        tts_new = False
        llm_new = False

        if pool is not None and stt_key in pool:
            self._stt = pool[stt_key]
            self._pooled_stt = True
        else:
            self._stt = create_stt(self._config.stt)
            stt_new = True

        if pool is not None and tts_key in pool:
            self._tts = pool[tts_key]
            self._pooled_tts = True
        else:
            self._tts = create_tts(self._config.tts)
            tts_new = True

        if pool is not None and llm_key in pool:
            self._llm = pool[llm_key]
            self._pooled_llm = True
        else:
            self._llm = create_llm(self._config.llm)
            llm_new = True

        init_tasks = []
        if stt_new:
            init_tasks.append(self._stt.initialize())
        if tts_new:
            init_tasks.append(self._tts.initialize())
        if llm_new:
            init_tasks.append(self._llm.initialize())

        if init_tasks:
            await asyncio.gather(*init_tasks)

        # Register freshly-initialized backends in the pool so the next
        # pipeline can borrow them.  Flipping `_pooled_* = True` here
        # guarantees that when THIS pipeline shuts down it won't kill
        # the backend it just contributed — the pool is now the owner.
        if pool is not None:
            if stt_new:
                pool[stt_key] = self._stt
                self._pooled_stt = True
            if tts_new:
                pool[tts_key] = self._tts
                self._pooled_tts = True
            if llm_new:
                pool[llm_key] = self._llm
                self._pooled_llm = True

        logger.info(
            "Pipeline ready — STT=%s%s, TTS=%s%s, LLM=%s%s",
            self._stt.name, " (pooled)" if self._pooled_stt else "",
            self._tts.name, " (pooled)" if self._pooled_tts else "",
            self._llm.name, " (pooled)" if self._pooled_llm else "",
        )

    def set_uplink_codec(self, codec: str) -> str:
        """Switch the uplink decoder.  Returns the codec actually applied.

        If the requested codec can't be initialised (e.g. opuslib not
        importable), falls back to "pcm" and logs a warning so the
        caller's config_update reply matches reality.
        """
        codec = (codec or "pcm").lower()
        if codec == "pcm":
            self._uplink_codec = "pcm"
            self._uplink_opus_dec = None
            return "pcm"
        if codec == "opus":
            from . import audio_codec as ac
            try:
                self._uplink_opus_dec = ac.OpusUplinkDecoder()
            except ac.CodecUnavailable as e:
                logger.warning("OPUS uplink unavailable: %s — staying PCM", e)
                self._uplink_codec = "pcm"
                self._uplink_opus_dec = None
                return "pcm"
            self._uplink_codec = "opus"
            logger.info("Uplink codec: PCM -> OPUS")
            return "opus"
        logger.warning("set_uplink_codec: unknown codec %r — staying %s",
                       codec, self._uplink_codec)
        return self._uplink_codec

    def get_uplink_codec(self) -> str:
        return self._uplink_codec

    async def feed_audio(self, audio_bytes: bytes) -> None:
        """Feed incoming PCM int16 audio data into the pipeline.

        Buffers audio and uses simple VAD to detect end of speech.
        When silence is detected after speech, triggers processing.
        In dictation mode, Tab5 handles VAD — Dragon just buffers.

        #173: if uplink codec is OPUS, the incoming bytes are an OPUS
        packet — decode to PCM before buffering.
        """
        # US-P01: drop all incoming audio while backends are being swapped.
        # Prevents stale audio from accumulating during the swap window.
        if self._swapping:
            return

        # #173: decode OPUS upstream if active.  PCM is the bytes-as-is
        # path matching the legacy behavior.
        if self._uplink_codec == "opus" and self._uplink_opus_dec is not None:
            decoded = self._uplink_opus_dec.decode(audio_bytes)
            if not decoded:
                return  # decode failure — already logged
            audio_bytes = decoded

        # Audit C6 (#137): reset the buffer-cap-emitted latches at the
        # start of a fresh recording session (both buffers empty).
        # Without this a user who hit the cap, stopped, and started a
        # new recording would never see the warning again because the
        # latch from the previous session is still set.
        if not self._audio_buffer and not self._segment_buffer:
            self._dictation_cap_emitted = False
            self._audio_cap_emitted = False

        if self._dictation_mode:
            # Dictation: buffer in segment buffer, no Dragon-side VAD.
            # Tab5 sends {"type":"segment"} markers when it detects pauses.
            # P06: enforce buffer cap on segment buffer too
            if len(self._segment_buffer) + len(audio_bytes) > MAX_AUDIO_BUFFER:
                logger.warning(
                    "P06: segment buffer full (%d bytes), dropping audio",
                    len(self._segment_buffer),
                )
                # Audit C6 (#137): emit ONCE so Tab5 can show the user
                # "Recording length limit reached -- stopping dictation"
                # instead of silently capping at 5 min while they keep
                # talking.  Latched per buffer-fill so we don't spam at
                # 50 fps until they tap stop.
                if not getattr(self, "_dictation_cap_emitted", False):
                    self._dictation_cap_emitted = True
                    await self._on_event(error_event(
                        code="dictation_buffer_full",
                        message="Recording reached 5-minute limit — finish to save.",
                        severity=Severity.TRANSIENT, scope=Scope.MEDIA,
                    ))
                return
            self._segment_buffer.extend(audio_bytes)
            return

        if self._processing:
            return

        # P06: enforce buffer cap to prevent unbounded memory growth
        if len(self._audio_buffer) + len(audio_bytes) > MAX_AUDIO_BUFFER:
            logger.warning(
                "P06: audio buffer full (%d bytes), dropping audio",
                len(self._audio_buffer),
            )
            # Audit C6 (#137): same latched user-visible signal as the
            # dictation branch above so non-dictation long-utterance
            # cases (e.g. user holds the orb open without speaking)
            # also surface a clean "we hit the cap" frame.
            if not getattr(self, "_audio_cap_emitted", False):
                self._audio_cap_emitted = True
                await self._on_event(error_event(
                    code="audio_buffer_full",
                    message="Recording reached 5-minute limit — finishing.",
                    severity=Severity.TRANSIENT, scope=Scope.MEDIA,
                ))
            return
        self._audio_buffer.extend(audio_bytes)

        if not self._config.audio.vad_enabled:
            return

        # Simple energy-based VAD
        audio_i16 = np.frombuffer(audio_bytes, dtype=np.int16)
        if len(audio_i16) == 0:
            return

        rms = np.sqrt(np.mean(audio_i16.astype(np.float32) ** 2))

        now = time.monotonic()

        if rms > _SILENCE_THRESHOLD:
            self._is_speaking = True
            self._last_voice_time = now
        elif self._is_speaking:
            # Check if silence duration exceeds threshold
            silence_ms = (now - self._last_voice_time) * 1000
            if silence_ms >= self._config.audio.vad_silence_ms:
                logger.debug(
                    "VAD: silence detected after %.0fms, processing %d bytes",
                    silence_ms,
                    len(self._audio_buffer),
                )
                self._is_speaking = False
                # Trigger processing
                audio_data = bytes(self._audio_buffer)
                self._audio_buffer.clear()
                self._process_task = asyncio.create_task(
                    self._process_with_timeout(audio_data)
                )

    async def start_processing(self) -> None:
        """Manually trigger processing of buffered audio (e.g. on "stop" command)."""
        if self._processing:
            return

        # Audit C2 (#137): bumped threshold from 1600 (50 ms) to 4000
        # bytes (~250 ms).  Anything below is essentially noise — sub-
        # quarter-second of audio doesn't transcribe to anything
        # meaningful and just wastes Moonshine cycles.  Pre-fix below-
        # threshold returns were a silent `logger.debug` drop with no
        # Tab5 frame; the user tapped mic, said nothing, released, and
        # got no signal we ignored their tap.  Now: when the buffer is
        # non-trivially short (some audio captured but too little to
        # transcribe), surface a γ-arch TRANSIENT/STT toast so Tab5
        # can render "Didn't catch that — try speaking a bit longer."
        _MIN_AUDIO_BYTES = 4000  # ~250 ms at 16 kHz int16
        if len(self._audio_buffer) < _MIN_AUDIO_BYTES:
            buffered = len(self._audio_buffer)
            logger.debug("Audio buffer too small to process (%d bytes)", buffered)
            self._audio_buffer.clear()
            # Empty buffer = no actual user input (probably mic-disabled
            # or aborted before record); no toast.  Some bytes = user
            # tapped + released too fast; emit toast.
            if buffered > 0:
                try:
                    await self._on_event(error_event(
                        code="stt_too_short",
                        message="Didn't catch that — try speaking a bit longer.",
                        severity=Severity.TRANSIENT, scope=Scope.STT,
                    ))
                except Exception:
                    logger.debug("stt_too_short emit failed", exc_info=True)
            return

        audio_data = bytes(self._audio_buffer)
        logger.info(
            "Processing audio buffer: %d bytes (%.1fs at 16kHz)",
            len(audio_data), len(audio_data) / 32000,
        )
        self._audio_buffer.clear()

        self._is_speaking = False
        self._process_task = asyncio.create_task(
            self._process_with_timeout(audio_data)
        )

    async def _process_with_timeout(self, audio_data: bytes) -> None:
        """Run _process_utterance with a mode-aware safety timeout.

        Local mode (ollama/npu_genie): 300s (5 min) — slow ARM64 CPU + tool-calling chains.
        Cloud/Hybrid (openrouter): 60s — cloud LLM is fast, timeout means real failure.
        TinkerClaw: 300s (5 min) — agentic chains with tool execution.
        """
        backend = self._config.llm.backend
        if backend in ("ollama", "npu_genie", "lmstudio", "tinkerclaw"):
            timeout = 300
        else:
            timeout = 60
        try:
            await asyncio.wait_for(self._process_utterance(audio_data), timeout=timeout)
        except asyncio.TimeoutError:
            logger.error("Pipeline processing timed out after %ds (backend=%s)", timeout, backend)
            self._processing = False
            try:
                await self._on_event(error_event(
                    code="llm_timeout",
                    message="Thinking took too long — try a shorter question.",
                    severity=Severity.TRANSIENT, scope=Scope.LLM,
                ))
                # Send tts_end so Tab5 doesn't hang
                if self._tts_started:
                    await self._on_event({"type": "tts_end", "tts_ms": 0})
                    self._tts_started = False
            except (ConnectionError, RuntimeError) as e:
                # Wave 13 H5: narrow from `except Exception` — WS is the only
                # thing `_on_event` can fail on, and it's expected to fail
                # when the client already disconnected during timeout.
                logger.debug("timeout-recovery emit skipped (client gone): %s", e)

    async def cancel(self) -> None:
        """Cancel ongoing processing and clean up in-flight TTS subprocesses."""
        self._cancelled = True
        if self._process_task and not self._process_task.done():
            self._process_task.cancel()
            try:
                await self._process_task
            except asyncio.CancelledError:
                pass
        # DQ22: cancel lingering post-process task.
        # Audit B5 (#152): pre-fix this only called task.cancel() and
        # nulled the handle.  If the task was inside its LLM stream when
        # cancel ran, the CancelledError might not fire until after the
        # LLM returned -- by which point the task could have emitted a
        # stale `dictation_summary` to a now-closed WS, or raced a
        # later finish_dictation's emission.  Awaiting here makes the
        # cancellation observable: by the time cancel() returns, the
        # task is guaranteed CANCELLED (or completed cleanly).
        if self._post_process_task and not self._post_process_task.done():
            prev = self._post_process_task
            self._post_process_task = None
            prev.cancel()
            try:
                await prev
            except (asyncio.CancelledError, Exception):
                # Either cancellation propagated (normal) or the task
                # raised some other error (already logged in
                # _on_post_process_done callback).  Either way we don't
                # want it to escape `cancel()`.
                pass
        # Kill any in-flight Piper TTS subprocesses (US-P24)
        if self._tts and hasattr(self._tts, "kill_active_procs"):
            self._tts.kill_active_procs()
        self._processing = False
        self._cancelled = False
        self._audio_buffer.clear()
        self._segment_buffer.clear()
        # Audit C6 (#137): cancel ends the recording session — reset
        # the buffer-cap latches so the next session can emit again.
        self._dictation_cap_emitted = False
        self._audio_cap_emitted = False
        logger.info("Pipeline processing cancelled")

    # ── Dictation mode ─────────────────────────────────────────────

    async def process_segment(self) -> None:
        """Transcribe audio accumulated since the last segment marker.

        Called when Tab5 sends {"type":"segment"} (VAD pause detected).
        Sends stt_partial back with the transcribed text.
        """
        if len(self._segment_buffer) < 1600:  # < 50ms at 16kHz
            self._segment_buffer.clear()
            return

        audio_data = bytes(self._segment_buffer)
        self._segment_buffer.clear()

        try:
            t0 = time.monotonic()
            transcript = await self._stt.transcribe(
                audio_data, self._config.audio.input_sample_rate
            )
            stt_ms = (time.monotonic() - t0) * 1000

            if transcript.strip():
                self._dictation_segments.append(transcript.strip())
                await self._on_event({
                    "type": "stt_partial",
                    "text": transcript.strip(),
                    "stt_ms": round(stt_ms),
                })
                logger.info(
                    "Dictation segment (%.0fms, %d bytes): %s",
                    stt_ms, len(audio_data), transcript.strip()[:80],
                )
        except Exception:
            logger.exception("Dictation segment transcription failed")

    async def finish_dictation(self) -> str:
        """Finalize dictation: transcribe remaining audio, send full transcript.

        Called on {"type":"stop"} when in dictation mode.
        Skips LLM and TTS — only sends STT results.
        Returns the full transcript text for the caller to persist.
        """
        # Transcribe any remaining audio in the segment buffer
        if len(self._segment_buffer) >= 1600:
            audio_data = bytes(self._segment_buffer)
            self._segment_buffer.clear()
            # Wave 15 W15-H05: narrow `except Exception:` — the expected
            # failure set is aiohttp.ClientError (cloud STT), TimeoutError
            # (slow-model stall), and OSError/ValueError (audio decode /
            # invalid PCM).  A TypeError or AttributeError here signals
            # a pipeline bug and should surface, not be swallowed behind
            # "Final dictation segment transcription failed".  Also
            # surface the failure as a toast-worthy event so the user
            # knows their last segment did NOT land — silently dropping
            # a dictation tail was the actual user-visible regression.
            try:
                transcript = await self._stt.transcribe(
                    audio_data, self._config.audio.input_sample_rate
                )
                if transcript.strip():
                    self._dictation_segments.append(transcript.strip())
                    await self._on_event({
                        "type": "stt_partial",
                        "text": transcript.strip(),
                    })
            except (
                aiohttp.ClientError,
                asyncio.TimeoutError,
                OSError,
                ValueError,
                RuntimeError,
            ) as exc:
                logger.exception(
                    "Final dictation segment transcription failed "
                    "(session=%s): %s",
                    self._session_id or "?",
                    exc,
                )
                await self._on_event({
                    "type": "dictation_warning",
                    "message": "last segment dropped — transcript may be incomplete",
                })
        else:
            self._segment_buffer.clear()

        # Send full combined transcript
        full_text = " ".join(self._dictation_segments)
        await self._on_event({"type": "stt", "text": full_text})

        logger.info(
            "Dictation complete: %d segments, %d chars",
            len(self._dictation_segments), len(full_text),
        )

        # Reset dictation state
        self._dictation_segments.clear()
        self._segment_buffer.clear()
        self._audio_buffer.clear()
        self._dictation_mode = False

        # Post-process: generate title + summary via LLM (async, non-blocking)
        # DQ22: store the task so it can be cancelled on shutdown/cancel
        if full_text.strip() and len(full_text) > 20:
            # v4·D audit P1 fix: cancel any prior post-process task before
            # overwriting the handle.  Two rapid finish_dictation calls
            # previously leaked the first task -- it kept running while
            # the second task raced it to write title/summary.
            prev = self._post_process_task
            if prev and not prev.done():
                # Audit B5 (#152): await the cancellation so the prior
                # task can't race-emit a stale dictation_summary
                # between the LLM call returning and CancelledError
                # firing at the next await.
                self._post_process_task = None
                prev.cancel()
                try:
                    await prev
                except (asyncio.CancelledError, Exception):
                    pass
                # Phase 2 H4 (issue #94): tell Tab5 the prior post-process
                # was abandoned for the new one.  Without this, a user who
                # rapidly stops + restarts dictation could see a stale
                # summary land on top of their new transcript a few seconds
                # later.
                # β-arch (issue #123): double-write — legacy frame for
                # unmodified Tab5 firmware + new progress frame for the
                # unified bus.
                await emit_progress_pair(
                    self._on_event,
                    legacy={"type": "dictation_postprocessing_cancelled"},
                    phase=Phase.DICTATION_POST,
                    stage=Stage.CANCELLED,
                    code="dictation_post_cancelled",
                    message="Prior summary abandoned for new dictation.",
                    emit_legacy=self._config.progress_bus_emit_legacy,
                )
            # Phase 2 H4 (issue #94): emit a "still working" event so Tab5
            # can show "Generating summary..." instead of leaving the user
            # staring at the bare transcript for 10-20 s while the LLM
            # writes the title + summary.  Pre-fix, the only events between
            # `stt` (line 490) and `dictation_summary` were silence —
            # users assumed the device had hung.
            await emit_progress_pair(
                self._on_event,
                legacy={"type": "dictation_postprocessing"},
                phase=Phase.DICTATION_POST,
                stage=Stage.START,
                emit_legacy=self._config.progress_bus_emit_legacy,
            )
            self._post_process_task = asyncio.ensure_future(
                self._post_process_dictation(full_text)
            )
            # Prevent "Task exception was never retrieved" warnings
            self._post_process_task.add_done_callback(self._on_post_process_done)

        return full_text

    @staticmethod
    def _on_post_process_done(task: asyncio.Task) -> None:
        """Callback to suppress 'Task exception was never retrieved' (DQ22)."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.warning("Dictation post-processing task failed: %s", exc)

    async def _post_process_dictation(self, transcript: str) -> None:
        """Generate title + summary for completed dictation via LLM.

        SOLID-audit follow-up: implementation extracted to
        dictation_post.run_dictation_post_process.  This method
        resolves the active LLM (ConvEngine first, then
        pipeline._llm fallback) and forwards to the free
        function with the on_event callback + emit_legacy flag.
        """
        from dragon_voice.dictation_post import run_dictation_post_process

        llm = None
        if self._conversation_engine and self._conversation_engine.llm:
            llm = self._conversation_engine.llm
        elif self._llm:
            llm = self._llm

        await run_dictation_post_process(
            transcript,
            llm=llm,
            on_event=self._on_event,
            emit_legacy=self._config.progress_bus_emit_legacy,
        )

    # ── Ask mode (existing) ────────────────────────────────────────

    async def _process_utterance(self, audio_data: bytes) -> None:
        """Run the full STT -> LLM -> TTS pipeline on a chunk of audio."""
        self._processing = True
        self._cancelled = False
        self._tts_started = False
        self._tts_total_ms = 0.0
        # Audit A3 (#142): clear the per-utterance tool tracker so the
        # empty-reply guard at end-of-stream sees only this turn's fires.
        self._tool_calls_this_turn = []
        # Audit B1 (#165): mark the SurfaceManager turn as busy so
        # scheduler-fired widgets defer until we drain on the finally
        # below.  Idempotent + no-op if surface_mgr/session_id absent.
        if self._surface_mgr is not None and self._session_id:
            self._surface_mgr.mark_turn_start(self._session_id)
        pipeline_start = time.monotonic()

        try:
            # --- STT (with cloud fallback) ---
            t0 = time.monotonic()
            try:
                # Audit B6 (#154): tighter wait — pre-fix 15 s; anything
                # past ~10 s is already user-perceived "broken" so fast-
                # fail beats waiting.
                transcript = await asyncio.wait_for(
                    self._stt.transcribe(audio_data, self._config.audio.input_sample_rate),
                    timeout=10,
                )
            except (Exception, asyncio.TimeoutError) as stt_err:
                if self._config.stt.backend == "openrouter":
                    logger.error("Cloud STT failed: %s — falling back to local", stt_err)
                    # Audit B6 (#154): emit a progress event so Tab5 can
                    # show "Switching to local STT..." instead of silence
                    # while we transcribe locally.  Pre-fix the user saw
                    # the spinner for 1-3 s of Moonshine cold-load + 1 s
                    # of transcribe with no signal anything was happening.
                    await self._on_event(error_event(
                        code="stt_fallback_active",
                        message="Cloud STT slow — switching to local.",
                        severity=Severity.TRANSIENT, scope=Scope.STT,
                    ))
                    # Audit B6 + v4·D audit P1 fix: cache the fallback STT
                    # instance.  swap_backends pre-warms this in the
                    # background when switching INTO a cloud STT mode, so
                    # the first fallback turn after a blip no longer pays
                    # the 1-3 s Moonshine cold-load.
                    transcript = await self._ensure_fallback_stt_then_transcribe(
                        audio_data
                    )
                    # Notify Tab5: auto-disable cloud mode
                    await self._on_event({
                        "type": "config_update",
                        "error": "Cloud STT unavailable, reverted to local",
                        "voice_mode": 0,
                        "config": {"voice_mode": 0, "cloud_mode": False},
                    })
                else:
                    raise
            stt_ms = (time.monotonic() - t0) * 1000

            if not transcript.strip():
                logger.info("STT returned empty transcript (audio=%d bytes, backend=%s)",
                            len(audio_data), self._config.stt.backend)
                await self._on_event({"type": "stt", "text": "", "stt_ms": round(stt_ms)})
                # User-friendly error — Tab5 shows this on voice overlay
                await self._on_event(error_event(
                    code="stt_empty",
                    message="Couldn't hear you — try again.",
                    severity=Severity.TRANSIENT, scope=Scope.STT,
                ))
                return

            logger.info("STT (%.0fms): %s", stt_ms, transcript)
            await self._on_event({"type": "stt", "text": transcript, "stt_ms": round(stt_ms)})

            # Audit F4 (2026-04-20): emit STT receipt so Tab5's per-turn
            # transparency + budget tracker can see which STT backend ran
            # and how long it took.  Cost_mils=0 for local Moonshine;
            # OpenRouter STT cost would need per-audio-second pricing
            # which the STT class doesn't currently expose — stub at 0
            # and let the cloud-STT path surface its own charge later.
            try:
                _stt_backend = self._config.stt.backend or "stt"
                await self._on_event({
                    "type": "receipt",
                    "stage": "stt",
                    "model": _stt_backend,
                    "stt_ms": round(stt_ms),
                    "cost_mils": 0,
                })
            except Exception as _e:
                logger.debug("STT receipt emit failed: %s", _e)

            if self._cancelled:
                return

            # --- LLM (streaming) ---
            t0 = time.monotonic()
            sentence_buffer = ""
            full_response = ""

            # Choose LLM path
            if self._config.llm.backend == "tinkerclaw":
                # TinkerClaw mode: bypass ConversationEngine entirely.
                # Send only the latest user message — TinkerClaw owns context.
                # Wave 21b (#204): isinstance(SupportsSessionKey) over hasattr.
                if isinstance(self._llm, SupportsSessionKey) and self._session_id:
                    self._llm.set_session_key(self._session_id)
                llm_stream = self._llm.generate_stream_with_messages([
                    {"role": "user", "content": transcript}
                ])
            elif self._conversation_engine and self._session_id:
                # Multi-turn: routes through ConversationEngine which stores
                # messages in DB and builds context from history.
                #
                # Audit A2 (#142): pass tool callbacks so Tab5 sees
                # tool_call/tool_result/tool_args_invalid frames on voice
                # turns the same way it does on text turns.  The wrapper
                # callbacks below also populate self._tool_calls_this_turn
                # for the A3 per-tool-wrap guard at end-of-stream.
                audio_duration = len(audio_data) / (self._config.audio.input_sample_rate * 2)

                async def _voice_on_tool_call(call: dict) -> None:
                    try:
                        self._tool_calls_this_turn.append({
                            "tool": call.get("tool"),
                            "args": call.get("args") or {},
                        })
                    except Exception:
                        logger.debug("voice tool tracker pre-register suppressed", exc_info=True)
                    if self._on_tool_call is not None:
                        await self._on_tool_call(call)

                async def _voice_on_tool_result(result: dict) -> None:
                    try:
                        merged = False
                        for rec in self._tool_calls_this_turn:
                            if rec.get("tool") == result.get("tool") and "result" not in rec:
                                rec["result"] = result.get("result")
                                rec["execution_ms"] = result.get("execution_ms")
                                merged = True
                                break
                        if not merged:
                            self._tool_calls_this_turn.append(result)
                    except Exception:
                        logger.debug("voice tool tracker merge suppressed", exc_info=True)
                    if self._on_tool_result is not None:
                        await self._on_tool_result(result)

                async def _voice_on_tool_error(err: dict) -> None:
                    if self._on_tool_error is not None:
                        await self._on_tool_error(err)

                llm_stream = self._conversation_engine.process_text_stream(
                    session_id=self._session_id,
                    text=transcript,
                    input_mode="voice",
                    audio_duration_s=audio_duration,
                    on_tool_call=_voice_on_tool_call,
                    on_tool_result=_voice_on_tool_result,
                    on_tool_error=_voice_on_tool_error,
                )
            else:
                # Legacy stateless path (no session)
                llm_stream = self._llm.generate_stream(
                    transcript, self._config.llm.system_prompt
                )

            # v4·D audit P1 fix: scan only the new slice per token.
            # Previously we re-ran the regex on the full accumulated
            # response for every token -- O(N^2) and genuinely painful on
            # 4k-token replies.  Keep a watermark and only scan
            # (watermark - overlap) onward; overlap = longest marker
            # length so a marker straddling a token boundary still hits.
            scan_watermark = 0
            OVERLAP = 32
            # Phase 2 H2 (issue #94): track last successful TTS flush so
            # we can word-boundary-flush after _LOCAL_TIMEOUT_FLUSH_S of
            # punctuation silence.  Pre-fix, an LLM rambling without
            # punctuation could buffer 60+ chars before any TTS chunk
            # left Dragon, producing a noticeable mid-reply silence.
            # Also track triple-backtick code-block state so the clause
            # flush doesn't false-fire on `def foo():` style colons.
            last_flush_ts = time.monotonic()
            in_code_block = False
            # Audit C9 (#137): the fast-path flush decisions below
            # (clause-flush min chars + word-boundary timeout flush)
            # are TTS-bound, not LLM-bound — tiny cloud TTS chunks
            # cost a per-chunk network round-trip and cause choppy
            # playback (P08).  Pre-fix this gated on LLM backend, so
            # Hybrid mode (local LLM + cloud TTS) inherited the
            # local-mode flushing thresholds and hammered the
            # OpenRouter TTS endpoint with 20-char chunks.
            is_local_tts = self._config.tts.backend != "openrouter"
            async for token in llm_stream:
                if self._cancelled:
                    return

                full_response += token
                scan_start = max(0, scan_watermark - OVERLAP)
                halt_match = _HALLUCINATION_STOPS.search(full_response, scan_start)
                scan_watermark = len(full_response)
                if halt_match:
                    # Truncate at the marker — don't send the hallucinated part
                    logger.warning(
                        "LLM hallucination detected at pos %d: '%s' — truncating",
                        halt_match.start(),
                        halt_match.group()[:30],
                    )
                    # Only keep token content before the marker
                    keep_end = halt_match.start()
                    discard_start = len(full_response) - len(token)
                    if keep_end > discard_start:
                        # Part of this token is before the marker
                        partial = token[: keep_end - discard_start]
                        if partial.strip():
                            await self._on_event({"type": "llm", "text": partial})
                            sentence_buffer += partial
                    full_response = full_response[:keep_end]
                    break

                await self._on_event({"type": "llm", "text": token})
                sentence_buffer += token

                # Phase 2 H2 (issue #94): toggle code-block state on
                # every triple-backtick boundary in the new token.  We
                # only check the freshly-arrived token to avoid
                # double-counting a backtick that was already in
                # sentence_buffer.
                if _TRIPLE_BACKTICK in token:
                    n_marks = token.count(_TRIPLE_BACKTICK)
                    if n_marks % 2 == 1:
                        in_code_block = not in_code_block

                # Check for sentence boundary — flush to TTS
                # (Sentence-flush still fires inside code blocks because
                # `.` is rare in code AND meaningful when present —
                # `obj.method()` etc.  Suppressing it would cause a
                # whole code response to wait for end-of-stream.)
                if _SENTENCE_END.search(sentence_buffer):
                    sentences = _SENTENCE_SPLIT.split(sentence_buffer)
                    # Send all complete sentences, keep incomplete tail
                    remainder = ""
                    for i, sentence in enumerate(sentences):
                        if i < len(sentences) - 1 or _SENTENCE_END.search(sentence):
                            if sentence.strip():
                                await self._synthesize_and_send(sentence.strip())
                        else:
                            # Last fragment is incomplete — keep buffering
                            remainder = sentence
                    sentence_buffer = remainder
                    last_flush_ts = time.monotonic()
                # Clause-level flushing: flush on comma/semicolon/colon/dash
                # when buffer reaches a minimum length. Threshold is mode-aware:
                #  - Local backends (low latency): 20 chars — start TTS early
                #  - Cloud/hybrid backends (bursty tokens): 60 chars — buffer
                #    a full sentence-length clause to smooth over latency spikes
                #    and avoid choppy playback (P08)
                # Phase 2 H2: SKIP clause flush inside a code block — `:`
                # in `def foo():` is structural, not a natural pause.
                elif _CLAUSE_END.search(sentence_buffer) and not in_code_block:
                    clause_min_chars = 20 if is_local_tts else 60
                    if len(sentence_buffer) >= clause_min_chars:
                        if sentence_buffer.strip():
                            await self._synthesize_and_send(sentence_buffer.strip())
                        sentence_buffer = ""
                        last_flush_ts = time.monotonic()
                # Phase 2 H2 (issue #94): timeout word-boundary flush.
                # If neither sentence nor clause flush fired AND the LLM
                # has been silent on punctuation for >300 ms, flush at
                # the last word boundary so the user hears progress
                # instead of staring at a frozen orb.  Local mode only —
                # cloud TTS is fast and bursty, the existing 60-char
                # clause threshold smooths it well enough.  Skip inside
                # code blocks (would chop code mid-line).
                elif (
                    is_local_tts
                    and not in_code_block
                    and len(sentence_buffer) >= _LOCAL_TIMEOUT_FLUSH_MIN_CHARS
                    and (time.monotonic() - last_flush_ts) > _LOCAL_TIMEOUT_FLUSH_S
                ):
                    m = _LAST_WORD_BOUNDARY.search(sentence_buffer)
                    if m and m.start() >= 10:
                        # Flush up to (but not including) the whitespace,
                        # keep the partial trailing word for the next
                        # iteration.
                        flush_text = sentence_buffer[: m.start()].strip()
                        sentence_buffer = sentence_buffer[m.start():].lstrip()
                        if flush_text:
                            await self._synthesize_and_send(flush_text)
                            last_flush_ts = time.monotonic()

            # Flush remaining text
            if sentence_buffer.strip() and not self._cancelled:
                await self._synthesize_and_send(sentence_buffer.strip())

            # Empty-response guard.  Skip if cancelled — that's a user
            # action (stop button, new session) and silence is correct.
            #
            # Audit C5 (#142): use looks_like_useful_text instead of
            # `not full_response.strip()` so bracket-noise-only replies
            # (residual `<` / `[tool]` from FC models) trigger the wrap
            # too, matching the text path.
            #
            # Audit A3 (#142): if any tool fired this turn, synthesise a
            # per-tool wrap from the tracker (e.g. "Got it — magenta.")
            # instead of always emitting the legacy generic apology.  The
            # legacy W15-H09 fallback only fires when zero tools ran.
            if not self._cancelled and not looks_like_useful_text(full_response):
                tool_calls = self._tool_calls_this_turn
                if tool_calls:
                    fallback = synthesize_wrap(tool_calls)
                    logger.info(
                        "voice empty-reply guard: %d tool(s) fired but LLM "
                        "text was %r — emitting template wrap (%d chars)",
                        len(tool_calls), full_response[:30], len(fallback),
                    )
                else:
                    fallback = (
                        "Sorry, I couldn't generate a response for that. "
                        "Please try rephrasing, or try again in a moment."
                    )
                    logger.warning(
                        "W15-H09: LLM stream produced zero usable text — "
                        "emitting fallback response to avoid silent drop"
                    )
                await self._on_event({"type": "llm", "text": fallback})
                await self._synthesize_and_send(fallback)
                full_response = fallback

            llm_ms = (time.monotonic() - t0) * 1000
            logger.info("LLM (%.0fms): %s", llm_ms, full_response[:80])
            await self._on_event({"type": "llm_done", "llm_ms": round(llm_ms)})

            # Phase 3 per-turn receipt. Only emit when the LLM is the
            # OpenRouter backend (it's the only backend where we can
            # charge real money); local (ollama, npu_genie) turns are
            # free and don't need a receipt.  If the LLM exposes
            # get_last_usage() we compute cost from the pricing table.
            try:
                # Wave 21b (#204): isinstance(SupportsUsage) over hasattr.
                if isinstance(self._llm, SupportsUsage):
                    usage = self._llm.get_last_usage()
                    if usage and usage.get("total_tokens"):
                        from dragon_voice.llm.openrouter_llm import price_for_model
                        cost_mils = price_for_model(
                            usage["model"],
                            usage.get("prompt_tokens", 0),
                            usage.get("completion_tokens", 0),
                        )
                        await self._on_event({
                            "type": "receipt",
                            "stage": "llm",
                            "model": usage["model"],
                            "prompt_tokens":     usage.get("prompt_tokens", 0),
                            "completion_tokens": usage.get("completion_tokens", 0),
                            "total_tokens":      usage.get("total_tokens", 0),
                            "cost_mils":         cost_mils,
                            "llm_ms":            round(llm_ms),
                            # v4·D Gauntlet G2: surface retries so the chat
                            # bubble can stamp a "retried" chip instead of
                            # silently presenting a possibly-degraded reply.
                            "retried":           bool(usage.get("retried", False)),
                            "retry_reason":      usage.get("retry_reason", ""),
                        })
            except Exception as e:
                # Never let receipt emission break the turn.  v4·D audit
                # P0 fix: emit a MINIMAL receipt even when the usage-
                # based path failed so the Tab5 chat bubble still gets a
                # stamp and the day-budget accumulator still increments
                # by 0 (harmless but consistent).
                logger.warning("Receipt emit failed: %s -- emitting fallback", e)
                try:
                    fallback_model = getattr(self._llm, "name", "") or "llm"
                    await self._on_event({
                        "type": "receipt",
                        "stage": "llm",
                        "model": fallback_model,
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0,
                        "cost_mils": 0,
                        "llm_ms": round(llm_ms) if isinstance(llm_ms, (int, float)) else 0,
                        "retried": False,
                        "retry_reason": "receipt-fallback: " + type(e).__name__,
                    })
                except Exception:
                    logger.debug("fallback receipt also failed", exc_info=True)

            # Rich media detection on full response.
            # Audit D4 (#137): emit progress before render — voice path
            # is less prone to perceived stalls (TTS is playing) but
            # the dashboard chat view + non-speaking response_mode
            # benefit from the same progress signal as the text path.
            if self._media_pipeline and full_response:
                if self._media_pipeline.has_renderable_content(full_response):
                    await self._on_event({
                        "type": "media_rendering",
                        "stage": "start",
                    })
                try:
                    media_events = await self._media_pipeline.process_response(
                        full_response, self._session_id or ""
                    )
                    for event in media_events:
                        await self._on_event(event)
                except Exception as e:
                    logger.warning("Voice media detection failed: %s", e)

            # Send tts_end once after all sentences are done
            if self._tts_started and not self._cancelled:
                await self._on_event({
                    "type": "tts_end",
                    "tts_ms": round(self._tts_total_ms),
                })
                self._tts_started = False

            # Audit F5 (2026-04-20): emit TTS receipt so per-turn chat
            # bubbles can stamp the speech backend + time.  cost_mils=0
            # for local Piper; OpenRouter TTS cost left at 0 (same
            # rationale as the STT receipt).
            if self._tts_total_ms > 0:
                try:
                    _tts_backend = self._config.tts.backend or "tts"
                    await self._on_event({
                        "type": "receipt",
                        "stage": "tts",
                        "model": _tts_backend,
                        "tts_ms": round(self._tts_total_ms),
                        "cost_mils": 0,
                    })
                except Exception as _e:
                    logger.debug("TTS receipt emit failed: %s", _e)

            # Trim in-memory history on legacy path only.
            # Wave 21b (#204): isinstance(SupportsHistoryTrim) over hasattr.
            if not self._conversation_engine and isinstance(
                self._llm, SupportsHistoryTrim
            ):
                self._llm.trim_history(self._max_history)

        except asyncio.CancelledError:
            logger.info("Pipeline processing was cancelled")
        except DragonError as e:
            # γ2-M6 (issue #106): structured γ-arch errors (e.g. TC
            # gateway fast-fail) carry severity + scope already.
            # Forward as-is instead of collapsing to the generic
            # `pipeline_failed` toast — Tab5 routes by scope (γ2-H8).
            logger.warning(
                "Pipeline received structured error: %s (code=%s, scope=%s)",
                e.message, e.code, e.scope.value,
            )
            try:
                await self._on_event(e.to_event())
            except (ConnectionError, RuntimeError) as _e:
                logger.debug("structured-error notice not delivered: %s", _e)
        except Exception:
            logger.exception("Pipeline processing error")
            try:
                await self._on_event(error_event(
                    code="pipeline_failed",
                    message="Something went wrong — please try again.",
                    severity=Severity.TRANSIENT, scope=Scope.LLM,
                ))
            except (ConnectionError, RuntimeError) as _e:
                # Wave 13 H5: the WS is already torn down — don't mask the
                # original exception with a secondary send failure.
                logger.debug("pipeline-error notice not delivered: %s", _e)
        finally:
            total_ms = (time.monotonic() - pipeline_start) * 1000
            logger.info("Pipeline total: %.0fms", total_ms)
            self._processing = False
            # Audit B1 (#165): turn ended — drain any deferred
            # scheduler-fired widgets now that there's no LLM token
            # stream to interleave with.
            if self._surface_mgr is not None and self._session_id:
                try:
                    await self._surface_mgr.mark_turn_end(self._session_id)
                except Exception:
                    logger.exception("B1: turn-end drain failed")

            # Log OpenRouter API usage for cost tracking
            try:
                cost_data = {}
                if hasattr(self._stt, 'total_calls') and self._stt.total_calls > 0:
                    cost_data["stt_calls"] = self._stt.total_calls
                    cost_data["stt_backend"] = "openrouter"
                if hasattr(self._tts, 'total_calls') and self._tts.total_calls > 0:
                    cost_data["tts_calls"] = self._tts.total_calls
                    cost_data["tts_backend"] = "openrouter"
                if cost_data:
                    cost_data["pipeline_ms"] = round(total_ms)
                    await self._on_event({
                        "type": "api_usage",
                        **cost_data,
                    })
            except (ConnectionError, RuntimeError, AttributeError) as _e:
                # Wave 13 H5: cost tracking must never take down the pipeline.
                # Narrow from `Exception` to the handful of runtime errors we
                # actually expect (WS torn down, backend without total_calls).
                logger.debug("cost tracking emit skipped: %s", _e)

    async def speak_system(self, text: str) -> None:
        """Speak a short system message (not stored in conversation history).

        Used by the server for out-of-band alerts like budget auto-downgrade
        (Gauntlet G7-F) where the user needs to hear what happened even with
        the screen off.  Delivered through the live TTS path so it respects
        the currently-selected voice (Piper / OpenRouter) and inherits the
        existing pacing + resampling.

        SOLID-audit follow-up: the bracket invariant (snapshot
        `_tts_started`, run synth, conditionally emit `tts_end`)
        extracted to speak_system.speak_system_message so it's
        testable in isolation.  This wrapper kept for backward
        compat with existing call sites.
        """
        from dragon_voice.speak_system import speak_system_message
        await speak_system_message(self, text)

    async def _synthesize_and_send(self, text: str) -> None:
        """Synthesize a sentence, resample to 16kHz, and stream paced to client.

        Sends tts_start only once per utterance (first call). Paces audio
        chunks to ~80% of real-time to prevent Tab5 ring buffer overflow.
        tts_end is NOT sent here — caller sends it after all sentences.
        """
        if self._cancelled:
            return

        try:
            # Send tts_start only on first sentence
            if not self._tts_started:
                await self._on_event({"type": "tts_start"})
                self._tts_started = True

            t0 = time.monotonic()
            # Audit C3 (#137): mode-aware TTS budget.  Pre-fix the voice
            # path used a flat 30 s, which was OK for cloud but tight
            # for local Piper on a long sentence (Piper takes 15-25 s
            # for a 200-word reply on Q6A ARM64).  Match the text-path's
            # 90 s for non-OpenRouter / 30 s for OpenRouter.
            tts_timeout = 30 if self._config.tts.backend == "openrouter" else 90
            try:
                audio_bytes = await asyncio.wait_for(
                    self._tts.synthesize(text), timeout=tts_timeout
                )
            except (Exception, asyncio.TimeoutError) as tts_err:
                # Phase 2 L3 (issue #94): kill any in-flight Piper
                # subprocess BEFORE falling back / raising.  Piper has
                # its own internal timeout which usually catches stalls,
                # but the OpenRouter→Piper fallback path below could
                # trigger a SECOND Piper synthesis on top of an already-
                # stalled one.  Without explicit kill_active_procs the
                # zombie holds the audio device + an FD until the Python
                # process exits.
                if hasattr(self._tts, "kill_active_procs"):
                    self._tts.kill_active_procs()
                if self._config.tts.backend == "openrouter":
                    logger.error(
                        "Cloud TTS failed: %s — falling back to local", tts_err,
                    )
                    # SOLID-audit follow-up: fallback Piper
                    # cache (lazy-load + 90 s wait_for + L3
                    # kill_active_procs on stall) extracted to
                    # FallbackTtsCache.synthesize.  Re-raises
                    # on second failure so the user-facing
                    # config_update emit (below) is skipped.
                    try:
                        audio_bytes = await self._fallback_tts_cache.synthesize(
                            text, timeout_s=90.0,
                        )
                    except (Exception, asyncio.TimeoutError) as fb_err:
                        logger.error("Fallback Piper TTS also failed: %s", fb_err)
                        raise
                    await self._on_event({
                        "type": "config_update",
                        "error": "Cloud TTS unavailable, reverted to local",
                        "voice_mode": 0,
                        "config": {"voice_mode": 0, "cloud_mode": False},
                    })
                else:
                    raise
            tts_ms = (time.monotonic() - t0) * 1000

            if audio_bytes:
                # Resample from TTS sample rate to 16kHz for Tab5 playback.
                # Audit B8 (#137) + C8 (#137): shared async helper hops
                # large buffers off the event loop so cancels / voice
                # frames from other connections aren't delayed.
                tts_rate = self._tts.sample_rate if self._tts else 22050
                target_rate = self._config.audio.input_sample_rate  # 16000
                audio_bytes = await resample_pcm16_async(
                    audio_bytes, tts_rate, target_rate
                )

                logger.debug(
                    "TTS (%.0fms): %d bytes @ %dHz for '%.40s...'",
                    tts_ms,
                    len(audio_bytes),
                    target_rate,
                    text,
                )
                # Send audio in chunks, paced to ~80% real-time so Tab5
                # ring buffer doesn't overflow from burst sends.
                # 16kHz 16-bit mono = 32000 bytes/sec.
                # 4096 bytes = 128ms of audio → sleep ~100ms between chunks.
                chunk_size = 4096
                pace_sleep = (chunk_size / 2) / target_rate * 0.8  # ~0.1s
                for i in range(0, len(audio_bytes), chunk_size):
                    if self._cancelled:
                        return
                    chunk = audio_bytes[i : i + chunk_size]
                    await self._on_audio(chunk)
                    # Pace: sleep between chunks (skip first few for pre-buffer)
                    if i > chunk_size * 3:
                        await asyncio.sleep(pace_sleep)

            self._tts_total_ms += tts_ms

        except Exception:
            logger.exception("TTS synthesis/send failed for: %.40s...", text)

    def clear_history(self) -> None:
        """Clear conversation history."""
        # Wave 21b (#204): isinstance(SupportsClearHistory) over hasattr.
        if isinstance(self._llm, SupportsClearHistory):
            self._llm.clear_history()
        logger.info("Conversation history cleared")

    async def _ensure_fallback_stt_then_transcribe(self, audio_data: bytes) -> str:
        """Borrow the pre-warmed fallback STT (or cold-load if absent),
        then transcribe `audio_data`.

        SOLID-audit follow-up: lazy-load + transcribe extracted to
        FallbackSttCache.transcribe.  This wrapper keeps the
        existing call site unchanged.
        """
        return await self._fallback_stt_cache.transcribe(
            audio_data, sample_rate=self._config.audio.input_sample_rate,
        )

    def _schedule_fallback_stt_prewarm(self) -> None:
        """Spawn a background task that loads Moonshine into the
        fallback cache so the first cloud-STT failure doesn't
        pay the 1-3 s cold-load.

        SOLID-audit follow-up: extracted to
        FallbackSttCache.schedule_prewarm.  This wrapper keeps
        the existing call site unchanged.
        """
        self._fallback_stt_cache.schedule_prewarm()

    async def swap_backends(self, config: VoiceConfig) -> None:
        """Hot-swap backends based on new configuration.

        Only reinitializes backends that have actually changed.
        Cancels any in-flight processing first (US-P12) to avoid
        orphaned Piper subprocesses and partial audio.

        US-P01: Sets _swapping flag so feed_audio() drops incoming PCM
        during the swap window. Clears audio buffers after cancel to
        prevent stale audio from bleeding into the new backend.
        """
        # US-P01: Block audio ingestion during the entire swap
        self._swapping = True

        try:
            # Cancel in-flight processing before swapping (US-P12)
            if self._processing or (self._process_task and not self._process_task.done()):
                logger.info("Cancelling in-flight processing before backend swap")
                await self.cancel()
                await asyncio.sleep(0.1)  # let pending async tasks clean up

            # US-P01: Clear audio buffers AFTER cancel to ensure no stale
            # audio from the old backend context survives into the new one.
            # cancel() also clears these, but audio could have arrived
            # between cancel() returning and us reaching this point.
            self._audio_buffer.clear()
            self._segment_buffer.clear()
            self._is_speaking = False

            old_config = self._config
            self._config = config

            # W15-C01: pool-aware swap — prefer reusing existing
            # pooled backend for the NEW signature; only tear
            # down the old one if it wasn't pooled.
            #
            # SOLID-audit follow-up: the three near-identical
            # STT/TTS/LLM swap blocks dedupped to
            # pool_aware_swap.swap_one_backend (PR #258).
            from dragon_voice.pool_aware_swap import swap_one_backend

            pool = self._backend_pool
            init_tasks = []

            stt_changed = (
                config.stt.backend != old_config.stt.backend
                or config.stt.model != old_config.stt.model
            )
            if stt_changed:
                logger.info(
                    "Swapping STT: %s -> %s",
                    old_config.stt.backend, config.stt.backend,
                )
            stt_result = await swap_one_backend(
                kind="stt",
                config_changed=stt_changed,
                old_instance=self._stt,
                old_is_pooled=self._pooled_stt,
                new_signature=_stt_sig(config.stt),
                new_factory=lambda: create_stt(config.stt),
                pool=pool,
            )
            self._stt = stt_result.new_instance
            self._pooled_stt = stt_result.is_pooled
            if stt_result.init_task is not None:
                init_tasks.append(stt_result.init_task)

            tts_changed = config.tts.backend != old_config.tts.backend
            if tts_changed:
                logger.info(
                    "Swapping TTS: %s -> %s",
                    old_config.tts.backend, config.tts.backend,
                )
            tts_result = await swap_one_backend(
                kind="tts",
                config_changed=tts_changed,
                old_instance=self._tts,
                old_is_pooled=self._pooled_tts,
                new_signature=_tts_sig(config.tts),
                new_factory=lambda: create_tts(config.tts),
                pool=pool,
            )
            self._tts = tts_result.new_instance
            self._pooled_tts = tts_result.is_pooled
            if tts_result.init_task is not None:
                init_tasks.append(tts_result.init_task)

            llm_changed = (
                config.llm.backend != old_config.llm.backend
                or config.llm.ollama_model != old_config.llm.ollama_model
            )
            if llm_changed:
                logger.info(
                    "Swapping LLM: %s -> %s",
                    old_config.llm.backend, config.llm.backend,
                )
            llm_result = await swap_one_backend(
                kind="llm",
                config_changed=llm_changed,
                old_instance=self._llm,
                old_is_pooled=self._pooled_llm,
                new_signature=_llm_sig(config.llm),
                new_factory=lambda: create_llm(config.llm),
                pool=pool,
            )
            self._llm = llm_result.new_instance
            self._pooled_llm = llm_result.is_pooled
            if llm_result.init_task is not None:
                init_tasks.append(llm_result.init_task)

            if init_tasks:
                await asyncio.gather(*(t.instance.initialize() for t in init_tasks))
                # Register freshly-created backends in the pool
                # AFTER successful initialize so a failed init
                # doesn't leave a half-constructed backend in
                # the pool.
                if pool is not None:
                    for t in init_tasks:
                        pool[t.key] = t.instance
                logger.info("Backend swap complete")

            # Audit B6 (#154): when swapping INTO a cloud STT mode,
            # kick off a background prewarm of the Moonshine fallback so
            # the first cloud-STT failure doesn't pay the 1-3 s cold-load.
            # Cheap if Moonshine is already cached (no-op on re-swap).
            if config.stt.backend == "openrouter":
                self._schedule_fallback_stt_prewarm()
        finally:
            # US-P01: Re-enable audio ingestion after swap completes (or fails)
            self._swapping = False

    @property
    def stt_name(self) -> str:
        return self._stt.name if self._stt else "none"

    @property
    def tts_name(self) -> str:
        return self._tts.name if self._tts else "none"

    @property
    def llm_name(self) -> str:
        return self._llm.name if self._llm else "none"

    @property
    def tts_sample_rate(self) -> int:
        return self._tts.sample_rate if self._tts else 22050

    @property
    def is_processing(self) -> bool:
        return self._processing

    async def shutdown(self) -> None:
        """Shut down all backends.

        W15-C01: backends borrowed from the server-level pool are NOT
        shut down here — the pool owns their lifecycle and will release
        them on server shutdown.  Only backends this pipeline personally
        created (pool miss, or cloud-mode per-connection instance) get
        their `.shutdown()` called.

        Audit B6 (#154): also cancels the in-flight Moonshine fallback
        pre-warm task if shutdown happens before it finishes loading.
        """
        await self.cancel()
        tasks = []
        if self._stt and not self._pooled_stt:
            tasks.append(self._stt.shutdown())
        if self._tts and not self._pooled_tts:
            tasks.append(self._tts.shutdown())
        if self._llm and not self._pooled_llm:
            tasks.append(self._llm.shutdown())
        # SOLID-audit follow-up: fallback STT + TTS lifecycles
        # both extracted to FallbackSttCache + FallbackTtsCache.
        # Shutdown each in parallel with the primary backends.
        await self._fallback_stt_cache.shutdown()
        await self._fallback_tts_cache.shutdown()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("Voice pipeline shut down")
