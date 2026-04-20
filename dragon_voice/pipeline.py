"""Voice pipeline orchestrator: Audio -> STT -> LLM -> TTS -> Audio.

Receives raw PCM audio, detects end-of-speech via simple VAD, transcribes
with STT, streams LLM response, buffers until sentence boundaries, and
synthesizes each sentence with TTS. Results are delivered via async callback.
"""

import asyncio
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Awaitable, Optional

import numpy as np

from dragon_voice.config import VoiceConfig
from dragon_voice.stt import create_stt, STTBackend
from dragon_voice.tts import create_tts, TTSBackend
from dragon_voice.llm import create_llm, LLMBackend

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
        """
        self._config = config
        self._on_audio = on_audio
        self._on_event = on_event
        self._conversation_engine = conversation_engine
        self._session_id = session_id
        self._media_pipeline = media_pipeline

        self._stt: Optional[STTBackend] = None
        self._tts: Optional[TTSBackend] = None
        self._llm: Optional[LLMBackend] = None

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
        """Create and initialize all backends."""
        logger.info("Initializing voice pipeline...")

        self._stt = create_stt(self._config.stt)
        self._tts = create_tts(self._config.tts)
        self._llm = create_llm(self._config.llm)

        # Initialize in parallel
        await asyncio.gather(
            self._stt.initialize(),
            self._tts.initialize(),
            self._llm.initialize(),
        )

        logger.info(
            "Pipeline ready — STT=%s, TTS=%s, LLM=%s",
            self._stt.name,
            self._tts.name,
            self._llm.name,
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

        if self._dictation_mode:
            # Dictation: buffer in segment buffer, no Dragon-side VAD.
            # Tab5 sends {"type":"segment"} markers when it detects pauses.
            # P06: enforce buffer cap on segment buffer too
            if len(self._segment_buffer) + len(audio_bytes) > MAX_AUDIO_BUFFER:
                logger.warning(
                    "P06: segment buffer full (%d bytes), dropping audio",
                    len(self._segment_buffer),
                )
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
                await self._on_event({"type": "error", "message": f"Processing timed out after {timeout}s"})
                # Send tts_end so Tab5 doesn't hang
                if self._tts_started:
                    await self._on_event({"type": "tts_end", "tts_ms": 0})
                    self._tts_started = False
            except Exception:
                pass

    async def cancel(self) -> None:
        """Cancel ongoing processing and clean up in-flight TTS subprocesses."""
        self._cancelled = True
        if self._process_task and not self._process_task.done():
            self._process_task.cancel()
            try:
                await self._process_task
            except asyncio.CancelledError:
                pass
        # DQ22: cancel lingering post-process task
        if self._post_process_task and not self._post_process_task.done():
            self._post_process_task.cancel()
            self._post_process_task = None
        # Kill any in-flight Piper TTS subprocesses (US-P24)
        if self._tts and hasattr(self._tts, "kill_active_procs"):
            self._tts.kill_active_procs()
        self._processing = False
        self._cancelled = False
        self._audio_buffer.clear()
        self._segment_buffer.clear()
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
            except Exception:
                logger.exception("Final dictation segment transcription failed")
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
                prev.cancel()
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
            await self._on_event({
                "type": "dictation_summary",
                "title": title,
                "summary": summary,
            })
        except Exception:
            logger.exception("Dictation post-processing failed")

    # ── Ask mode (existing) ────────────────────────────────────────

    async def _process_utterance(self, audio_data: bytes) -> None:
        """Run the full STT -> LLM -> TTS pipeline on a chunk of audio."""
        self._processing = True
        self._cancelled = False
        self._tts_started = False
        self._tts_total_ms = 0.0
        pipeline_start = time.monotonic()

        try:
            # --- STT (with cloud fallback) ---
            t0 = time.monotonic()
            try:
                transcript = await asyncio.wait_for(
                    self._stt.transcribe(audio_data, self._config.audio.input_sample_rate),
                    timeout=15,
                )
            except (Exception, asyncio.TimeoutError) as stt_err:
                if self._config.stt.backend == "openrouter":
                    logger.error("Cloud STT failed: %s — falling back to local", stt_err)
                    # v4·D audit P1 fix: cache the fallback STT instance
                    # on the pipeline so repeated cloud failures don't
                    # re-init Moonshine (1-3 s blocking model load) every
                    # single time.
                    if not getattr(self, "_fallback_stt", None):
                        from dragon_voice.stt import create_stt
                        from dragon_voice.config import STTConfig
                        self._fallback_stt = create_stt(STTConfig(backend="moonshine"))
                        await self._fallback_stt.initialize()
                        logger.info("Pre-warmed fallback STT (moonshine) cached")
                    transcript = await self._fallback_stt.transcribe(
                        audio_data, self._config.audio.input_sample_rate
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
                await self._on_event({"type": "error",
                                      "message": "Couldn't hear you — try again"})
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
                # messages in DB and builds context from history
                audio_duration = len(audio_data) / (self._config.audio.input_sample_rate * 2)
                llm_stream = self._conversation_engine.process_text_stream(
                    session_id=self._session_id,
                    text=transcript,
                    input_mode="voice",
                    audio_duration_s=audio_duration,
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

                # Check for sentence boundary — flush to TTS
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
                # Clause-level flushing: flush on comma/semicolon/colon/dash
                # when buffer reaches a minimum length. Threshold is mode-aware:
                #  - Local backends (low latency): 20 chars — start TTS early
                #  - Cloud/hybrid backends (bursty tokens): 60 chars — buffer
                #    a full sentence-length clause to smooth over latency spikes
                #    and avoid choppy playback (P08)
                elif _CLAUSE_END.search(sentence_buffer):
                    is_local = self._config.llm.backend in ("ollama", "npu_genie", "lmstudio")
                    clause_min_chars = 20 if is_local else 60
                    if len(sentence_buffer) >= clause_min_chars:
                        if sentence_buffer.strip():
                            await self._synthesize_and_send(sentence_buffer.strip())
                        sentence_buffer = ""

            # Flush remaining text
            if sentence_buffer.strip() and not self._cancelled:
                await self._synthesize_and_send(sentence_buffer.strip())

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

            # Rich media detection on full response
            if self._media_pipeline and full_response:
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
        except Exception:
            logger.exception("Pipeline processing error")
            try:
                await self._on_event(
                    {"type": "error", "message": "Processing failed — see server logs"}
                )
            except Exception:
                pass
        finally:
            total_ms = (time.monotonic() - pipeline_start) * 1000
            logger.info("Pipeline total: %.0fms", total_ms)
            self._processing = False

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
            except Exception:
                pass  # Cost tracking is best-effort

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
            try:
                audio_bytes = await asyncio.wait_for(
                    self._tts.synthesize(text), timeout=30
                )
            except (Exception, asyncio.TimeoutError) as tts_err:
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
                    audio_bytes = await self._fallback_tts.synthesize(text)
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
                # Resample from TTS sample rate to 16kHz for Tab5 playback
                tts_rate = self._tts.sample_rate if self._tts else 22050
                target_rate = self._config.audio.input_sample_rate  # 16000

                if tts_rate != target_rate:
                    audio_i16 = np.frombuffer(audio_bytes, dtype=np.int16)
                    # Simple linear interpolation resample
                    ratio = target_rate / tts_rate
                    new_len = int(len(audio_i16) * ratio)
                    indices = np.arange(new_len) / ratio
                    indices_floor = indices.astype(np.int32)
                    indices_floor = np.clip(indices_floor, 0, len(audio_i16) - 2)
                    frac = indices - indices_floor
                    resampled = (
                        audio_i16[indices_floor] * (1 - frac)
                        + audio_i16[indices_floor + 1] * frac
                    ).astype(np.int16)
                    audio_bytes = resampled.tobytes()

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

            tasks = []

            # Check if STT backend changed
            if (
                config.stt.backend != old_config.stt.backend
                or config.stt.model != old_config.stt.model
            ):
                logger.info("Swapping STT: %s -> %s", old_config.stt.backend, config.stt.backend)
                if self._stt:
                    await self._stt.shutdown()
                self._stt = create_stt(config.stt)
                tasks.append(self._stt.initialize())

            # Check if TTS backend changed
            if config.tts.backend != old_config.tts.backend:
                logger.info("Swapping TTS: %s -> %s", old_config.tts.backend, config.tts.backend)
                if self._tts:
                    await self._tts.shutdown()
                self._tts = create_tts(config.tts)
                tasks.append(self._tts.initialize())

            # Check if LLM backend changed
            if (
                config.llm.backend != old_config.llm.backend
                or config.llm.ollama_model != old_config.llm.ollama_model
            ):
                logger.info("Swapping LLM: %s -> %s", old_config.llm.backend, config.llm.backend)
                if self._llm:
                    await self._llm.shutdown()
                self._llm = create_llm(config.llm)
                tasks.append(self._llm.initialize())

            if tasks:
                await asyncio.gather(*tasks)
                logger.info("Backend swap complete")
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
        """Shut down all backends."""
        await self.cancel()
        tasks = []
        if self._stt:
            tasks.append(self._stt.shutdown())
        if self._tts:
            tasks.append(self._tts.shutdown())
        if self._llm:
            tasks.append(self._llm.shutdown())
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
