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

# Regex for sentence boundary detection
_SENTENCE_END = re.compile(r"[.!?]\s*$")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")

# Clause boundary for local mode — start TTS earlier on slow models
# Triggers on comma/semicolon/colon/dash with 20+ chars buffered
_CLAUSE_END = re.compile(r"[,;:\u2014—]\s*$")

# Phase 2 H2 (issue #94): word-boundary timeout-flush regex — finds the
# last whitespace position so the timeout flush splits at a word break,
# not mid-word.  Used only when the LLM has been silent on punctuation
# for >300 ms AND the buffer has enough content to make a meaningful
# TTS chunk.
_LAST_WORD_BOUNDARY = re.compile(r"\s\S*$")

# Phase 2 H2 (issue #94): triple-backtick toggle for code-block
# detection.  While inside a code block the clause-flush is suppressed
# (a colon in `def foo():` is structural, not a natural pause).  The
# sentence-flush (.!?) still applies because periods are rare in code
# blocks and a `.` is usually meaningful (e.g. `obj.method()`).
# Timeout-flush also suppressed in code so the whole block emits as
# one TTS unit.
_TRIPLE_BACKTICK = "```"

# Phase 2 H2 (issue #94): timeout-flush parameters.  300 ms is long
# enough that a normal punctuation-rich response never trips it
# (sentences land their `.` well within 300 ms of each other on any
# model > 5 tok/s) but short enough that an LLM rambling without
# punctuation still feels responsive.  Minimum buffer of 20 chars
# prevents micro-stuttered chunks.
_LOCAL_TIMEOUT_FLUSH_S = 0.30
_LOCAL_TIMEOUT_FLUSH_MIN_CHARS = 20

# Hallucination stop patterns — LLMs sometimes simulate user turns or continue
# generating after answering. Truncate response at these markers.
_HALLUCINATION_STOPS = re.compile(
    r"(?:^|\n\n\n|\n)(User:|Human:|Assistant:|<\|end|<\|im_end)",
    re.IGNORECASE,
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

    async def feed_audio(self, audio_bytes: bytes) -> None:
        """Feed incoming PCM int16 audio data into the pipeline.

        Buffers audio and uses simple VAD to detect end of speech.
        When silence is detected after speech, triggers processing.
        In dictation mode, Tab5 handles VAD — Dragon just buffers.
        """
        # US-P01: drop all incoming audio while backends are being swapped.
        # Prevents stale audio from accumulating during the swap window.
        if self._swapping:
            return

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

        if len(self._audio_buffer) < 1600:  # Less than 50ms at 16kHz
            logger.debug("Audio buffer too small to process (%d bytes)", len(self._audio_buffer))
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
        """Generate title + summary for completed dictation via LLM."""
        llm = None
        if self._conversation_engine and self._conversation_engine.llm:
            llm = self._conversation_engine.llm
        elif self._llm:
            llm = self._llm

        if not llm:
            logger.warning("No LLM available for dictation post-processing")
            # Phase 2 H4 (issue #94): tell Tab5 the post-process won't run.
            # Pre-fix this would silently log and leave Tab5 waiting for a
            # `dictation_summary` event that never arrives — UI gets stuck
            # on the "Generating summary..." caption forever.
            # β-arch (issue #123): pair-emit — legacy frame for unmodified
            # Tab5 + new progress.error frame carrying the γ1 error
            # taxonomy so γ2-H8 routing applies.
            await emit_progress_pair(
                self._on_event,
                legacy={
                    "type": "dictation_postprocessing_error",
                    "error": "no_llm_available",
                    "message": "Note saved — summary unavailable (LLM offline)",
                },
                phase=Phase.DICTATION_POST,
                stage=Stage.ERROR,
                code="no_llm_available",
                message="Note saved — summary unavailable (LLM offline)",
                severity=Severity.TRANSIENT,
                scope=Scope.LLM,
                emit_legacy=self._config.progress_bus_emit_legacy,
            )
            return

        prompt = (
            "Given this voice transcript, provide:\n"
            "1. A short title (max 8 words)\n"
            "2. A 1-2 sentence summary\n\n"
            f"Transcript: {transcript[:2000]}\n\n"
            "Respond in this exact format:\n"
            "TITLE: <title>\nSUMMARY: <summary>"
        )

        try:
            response = ""
            async for token in llm.generate_stream(prompt, "You are a concise note summarizer."):
                response += token

            title = "Untitled Note"
            summary = transcript[:200]
            for line in response.split("\n"):
                line = line.strip()
                if line.upper().startswith("TITLE:"):
                    title = line[6:].strip().strip('"')
                elif line.upper().startswith("SUMMARY:"):
                    summary = line[8:].strip().strip('"')

            logger.info("Dictation summary: title='%s'", title)
            # β-arch (issue #123): pair-emit — legacy `dictation_summary`
            # carries title/summary at the top level; the new progress
            # frame nests them in `payload` so the bus is uniform.
            await emit_progress_pair(
                self._on_event,
                legacy={
                    "type": "dictation_summary",
                    "title": title,
                    "summary": summary,
                },
                phase=Phase.DICTATION_POST,
                stage=Stage.DONE,
                payload={"title": title, "summary": summary},
                emit_legacy=self._config.progress_bus_emit_legacy,
            )
        except asyncio.CancelledError:
            # Phase 2 H4 (issue #94): the cancelled-side event is emitted
            # by `finish_dictation` BEFORE it spawns a new task — we don't
            # double-emit here.  Just propagate.  (Caller handles via
            # add_done_callback.)
            raise
        except Exception as e:
            logger.exception("Dictation post-processing failed")
            # Phase 2 H4 (issue #94): user-visible error so Tab5 can clear
            # the "Generating summary..." caption + show a toast.  The
            # transcript is already in the chat from the prior `stt` event,
            # so the user hasn't lost data — they just don't get the
            # auto-generated title/summary.
            # β-arch (issue #123): pair-emit so the new progress.error
            # frame carries the γ1 taxonomy.  `code` uses the exception
            # class name (matches the legacy `error` field convention).
            await emit_progress_pair(
                self._on_event,
                legacy={
                    "type": "dictation_postprocessing_error",
                    "error": type(e).__name__,
                    "message": "Note saved — summary generation failed",
                },
                phase=Phase.DICTATION_POST,
                stage=Stage.ERROR,
                code=type(e).__name__,
                message="Note saved — summary generation failed",
                severity=Severity.TRANSIENT,
                scope=Scope.LLM,
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
                if hasattr(self._llm, 'set_session_key') and self._session_id:
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
                if hasattr(self._llm, "get_last_usage"):
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

            # Trim in-memory history on legacy path only
            if not self._conversation_engine and hasattr(self._llm, "trim_history"):
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
        """
        if not text or not self._tts:
            return
        prev_started = self._tts_started
        try:
            await self._synthesize_and_send(text)
        except Exception:
            logger.exception("speak_system failed: %s", text[:40])
        finally:
            # Close the utterance so the Tab5 flushes its ring buffer.
            if self._tts_started and not prev_started:
                try:
                    await self._on_event({
                        "type": "tts_end",
                        "tts_ms": round(self._tts_total_ms),
                    })
                except Exception:
                    pass
                self._tts_started = False

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
                    logger.error("Cloud TTS failed: %s — falling back to local", tts_err)
                    # v4·D audit P1 fix: cache fallback Piper instance so
                    # repeated cloud failures don't re-initialize it
                    # (expensive cold start every time).
                    if not getattr(self, "_fallback_tts", None):
                        from dragon_voice.tts import create_tts
                        from dragon_voice.config import TTSConfig
                        self._fallback_tts = create_tts(TTSConfig(backend="piper"))
                        await self._fallback_tts.initialize()
                        logger.info("Pre-warmed fallback TTS (piper) cached")
                    # Phase 2 L3 (issue #94): the fallback Piper itself
                    # can stall — wrap with a wait_for and kill its
                    # procs on a second timeout.  Without the second
                    # guard a TTS-down scenario with no second fallback
                    # could leak indefinitely.
                    # Audit C3 (#137): use the same 90 s budget as the
                    # primary path now that we know Piper can need it.
                    try:
                        audio_bytes = await asyncio.wait_for(
                            self._fallback_tts.synthesize(text), timeout=90
                        )
                    except (Exception, asyncio.TimeoutError) as fb_err:
                        if hasattr(self._fallback_tts, "kill_active_procs"):
                            self._fallback_tts.kill_active_procs()
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
        if hasattr(self._llm, "clear_history"):
            self._llm.clear_history()
        logger.info("Conversation history cleared")

    async def _ensure_fallback_stt_then_transcribe(self, audio_data: bytes) -> str:
        """Borrow the pre-warmed fallback STT (or cold-load if absent),
        then transcribe `audio_data`.

        Audit B6 (#154): keeps the fallback path single-line at the
        callsite while preserving the v4·D P1 caching behaviour and
        respecting the new `swap_backends` pre-warm.
        """
        if not getattr(self, "_fallback_stt", None):
            from dragon_voice.stt import create_stt
            from dragon_voice.config import STTConfig
            self._fallback_stt = create_stt(STTConfig(backend="moonshine"))
            await self._fallback_stt.initialize()
            logger.info("Pre-warmed fallback STT (moonshine) cached (lazy path)")
        return await self._fallback_stt.transcribe(
            audio_data, self._config.audio.input_sample_rate
        )

    def _schedule_fallback_stt_prewarm(self) -> None:
        """Spawn a background task that loads Moonshine into
        `self._fallback_stt` so the first cloud-STT failure doesn't
        pay the 1-3 s cold-load.

        Audit B6 (#154): called from `swap_backends` whenever the new
        STT backend is `openrouter`.  Idempotent — if a fallback already
        exists or a prewarm is already in flight, no-ops.
        """
        if getattr(self, "_fallback_stt", None):
            return
        prev = getattr(self, "_fallback_prewarm_task", None)
        if prev is not None and not prev.done():
            return

        async def _prewarm() -> None:
            try:
                from dragon_voice.stt import create_stt
                from dragon_voice.config import STTConfig
                stt = create_stt(STTConfig(backend="moonshine"))
                await stt.initialize()
                # Race guard: a real cloud-STT failure could have
                # cold-loaded one in parallel.  Last writer wins; the
                # loser is discarded.
                if not getattr(self, "_fallback_stt", None):
                    self._fallback_stt = stt
                    logger.info("Pre-warmed fallback STT (moonshine) cached (background)")
                else:
                    await stt.shutdown()
            except Exception as e:
                logger.warning("B6 fallback-STT prewarm failed: %s", e)

        self._fallback_prewarm_task = asyncio.create_task(
            _prewarm(), name="b6_fallback_stt_prewarm"
        )

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

            # W15-C01: pool-aware swap — prefer reusing existing pooled
            # backend for the NEW signature; only tear down the old one
            # if it wasn't pooled.
            pool = self._backend_pool
            tasks = []

            # STT
            if (
                config.stt.backend != old_config.stt.backend
                or config.stt.model != old_config.stt.model
            ):
                logger.info("Swapping STT: %s -> %s", old_config.stt.backend, config.stt.backend)
                if self._stt and not self._pooled_stt:
                    await self._stt.shutdown()
                new_key = _stt_sig(config.stt)
                if pool is not None and new_key in pool:
                    self._stt = pool[new_key]
                    self._pooled_stt = True
                else:
                    self._stt = create_stt(config.stt)
                    self._pooled_stt = False
                    tasks.append((self._stt, new_key, "stt"))

            # TTS
            if config.tts.backend != old_config.tts.backend:
                logger.info("Swapping TTS: %s -> %s", old_config.tts.backend, config.tts.backend)
                if self._tts and not self._pooled_tts:
                    await self._tts.shutdown()
                new_key = _tts_sig(config.tts)
                if pool is not None and new_key in pool:
                    self._tts = pool[new_key]
                    self._pooled_tts = True
                else:
                    self._tts = create_tts(config.tts)
                    self._pooled_tts = False
                    tasks.append((self._tts, new_key, "tts"))

            # LLM
            if (
                config.llm.backend != old_config.llm.backend
                or config.llm.ollama_model != old_config.llm.ollama_model
            ):
                logger.info("Swapping LLM: %s -> %s", old_config.llm.backend, config.llm.backend)
                if self._llm and not self._pooled_llm:
                    await self._llm.shutdown()
                new_key = _llm_sig(config.llm)
                if pool is not None and new_key in pool:
                    self._llm = pool[new_key]
                    self._pooled_llm = True
                else:
                    self._llm = create_llm(config.llm)
                    self._pooled_llm = False
                    tasks.append((self._llm, new_key, "llm"))

            if tasks:
                await asyncio.gather(*(t[0].initialize() for t in tasks))
                # Register freshly-created backends in the pool.
                if pool is not None:
                    for backend, key, _kind in tasks:
                        pool[key] = backend
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
        # Audit B6 (#154): cancel any in-flight Moonshine pre-warm so
        # shutdown doesn't have to wait for a 1-3 s model load just to
        # immediately throw it away.
        prewarm = getattr(self, "_fallback_prewarm_task", None)
        if prewarm is not None and not prewarm.done():
            prewarm.cancel()
            try:
                await prewarm
            except (asyncio.CancelledError, Exception):
                pass
            self._fallback_prewarm_task = None
        # Pre-warmed fallback backends (from P1 STT/TTS cache fix).
        fb_stt = getattr(self, "_fallback_stt", None)
        fb_tts = getattr(self, "_fallback_tts", None)
        if fb_stt: tasks.append(fb_stt.shutdown())
        if fb_tts: tasks.append(fb_tts.shutdown())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._fallback_stt = None
        self._fallback_tts = None
        logger.info("Voice pipeline shut down")
