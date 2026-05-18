"""Voice pipeline construction + initialise for the register flow.

Wave 23 SOLID-audit follow-up — fourteenth sub-extract from
the WS-handler family in server.py (round 4 spillover, after
the thirteen prior extracts #227-#239).

After session_start lands and Tab5 sees the session confirmed,
the register flow has to:

  1. Reset `conn_config` to local defaults (Tab5 will send a
     `config_update` immediately with its actual mode, so we
     avoid initializing cloud backends only to swap them out).
  2. Construct a `VoicePipeline` with the per-connection deps
     (audio/event callbacks, conversation engine, session id,
     media pipeline, backend pool, tool callbacks, surface mgr).
  3. Call `initialize()` which loads Moonshine STT (~2 s on
     ARM64) + warms the active TTS backend.
  4. On init failure, emit a structured `pipeline_init_failed`
     error event so Tab5 surfaces it in the voice caption.

Pre-extract this 50-LOC chunk lived inline in
`_handle_register`.  Now lives in its own dedicated module.

## API

```python
pipeline = await build_and_initialize_pipeline(
    ws,
    *,
    ws_id,
    conn_config,
    on_audio,
    on_event,
    conversation,
    session_id,
    media_pipeline,
    backend_pool,
    on_tool_call,
    on_tool_result,
    on_tool_error,
    surface_mgr,
    safe_send_json,
)
```

Returns the live `VoicePipeline` on success, or `None` when
init failed (caller short-circuits the rest of register).

## Why local-defaults reset

The configured `backend` field could be set to "openrouter" or
"tinkerclaw" from a prior session's persisted config.  Building
a Moonshine pipeline by hand would still try to wire the cloud
STT/TTS even though the active mode is going to be Local on the
fresh register.  Resetting STT/TTS to "moonshine"/"piper" + the
LLM backend to its `local_backend` (or "ollama" fallback) means
the post-register `config_update` from Tab5 is a no-op or a
clean swap — never a "tear down half-built cloud + build local +
build cloud again" round-trip.

The condition `backend in ("openrouter", "tinkerclaw")` is
deliberately narrow: local-but-non-ollama defaults like
`lmstudio`, `npu_genie`, `dual` (PR #80), `router` (#185) are
left as-is so the user's custom local config survives the
reset.

## Failure semantics

Init failure → log at EXCEPTION (operator visible in journal),
emit `pipeline_init_failed` (FATAL/SESSION) so Tab5 shows the
banner, return None.  The raw exception text is NOT included in
the user-facing message — Tab5's caption is 128 chars and we
don't want Python tracebacks leaking to a non-developer user.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

from aiohttp import web

from dragon_voice.config import (
    SYSTEM_PROMPT_LOCAL,
    MAX_TOKENS_LOCAL,
)
from dragon_voice.errors import Scope, Severity, error_event
from dragon_voice.pipeline import VoicePipeline

logger = logging.getLogger(__name__)


SafeSendJson = Callable[[web.WebSocketResponse, dict], Awaitable[bool]]


# Cloud-backend names that trigger the LLM-backend reset.  Local
# variants (ollama, lmstudio, npu_genie, dual, router) are left
# as-is so user-customised local fleets survive the register.
_CLOUD_LLM_BACKENDS = ("openrouter", "tinkerclaw")


def _reset_conn_config_to_local_defaults(conn_config: Any) -> None:
    """Mutate conn_config so the pipeline initialises with local
    backends.  Tab5's post-register `config_update` will do the
    actual mode swap — this just avoids building cloud and then
    tearing it down.
    """
    conn_config.stt.backend = "moonshine"
    # Honor the user's YAML `tts.backend` if it's a local-eligible
    # backend; only force-reset to piper when conn_config arrived with
    # a cloud / unsupported value (preserves the NeuTTS Air premium
    # voice + Kokoro override paths).  See config_swap.py for the
    # parallel local_tts_override gate.
    if (conn_config.tts.backend or "").strip().lower() not in (
        "piper", "kokoro", "neutts_air",
    ):
        conn_config.tts.backend = "piper"
    if conn_config.llm.backend in _CLOUD_LLM_BACKENDS:
        # Use the user's `local_backend` preference when set,
        # else fall back to "ollama" — matches pre-extract.
        conn_config.llm.backend = conn_config.llm.local_backend or "ollama"
    conn_config.llm.system_prompt = SYSTEM_PROMPT_LOCAL
    conn_config.llm.max_tokens = MAX_TOKENS_LOCAL


async def build_and_initialize_pipeline(
    ws: web.WebSocketResponse,
    *,
    ws_id: str,
    conn_config: Any,                # VoiceConfig (deep-copied per connection)
    on_audio: Callable[[bytes], Awaitable[None]],
    on_event: Callable[[dict], Awaitable[None]],
    conversation: Any,               # ConversationEngine
    session_id: str,
    media_pipeline: Optional[Any],   # MediaPipeline (Optional in tests)
    backend_pool: Optional[Any],     # BackendPool (Optional)
    on_tool_call: Optional[Callable] = None,
    on_tool_result: Optional[Callable] = None,
    on_tool_error: Optional[Callable] = None,
    surface_mgr: Optional[Any] = None,
    safe_send_json: Optional[SafeSendJson] = None,
) -> Optional[VoicePipeline]:
    """Build + initialise a VoicePipeline for a freshly-registered
    session.  Returns the live pipeline on success, or ``None``
    when init failed (caller MUST short-circuit the rest of
    register).

    Always resets ``conn_config`` to local defaults first
    (see module docstring for the why), so the pipeline starts
    on Moonshine/Piper/local-LLM regardless of whatever cloud
    state was carried over from a prior session.

    On init failure: logs the exception (operator visible in
    journal), emits a FATAL ``pipeline_init_failed`` event so
    Tab5 surfaces it in the voice caption, returns None.

    Audit A2 (#142): tool callbacks (`on_tool_call`, etc.) are
    passed through so voice turns surface tool indicators just
    like text turns do.

    Audit B1 (#165): `surface_mgr` is passed through so the
    pipeline can gate scheduler-fired widgets behind LLM-token
    interleave protection.
    """
    _reset_conn_config_to_local_defaults(conn_config)

    pipeline = VoicePipeline(
        conn_config, on_audio, on_event,
        conversation_engine=conversation,
        session_id=session_id,
        media_pipeline=media_pipeline,
        backend_pool=backend_pool,
        on_tool_call=on_tool_call,
        on_tool_result=on_tool_result,
        on_tool_error=on_tool_error,
        surface_mgr=surface_mgr,
    )
    try:
        await pipeline.initialize()
    except Exception:
        logger.exception("Failed to initialize pipeline for %s", ws_id)
        if not ws.closed:
            # Don't leak the raw exception to the user — Tab5
            # surfaces this in the voice caption.  Operator can
            # see the actual exception in the journal via the
            # logger.exception above.
            err_frame = error_event(
                code="pipeline_init_failed",
                message="Voice pipeline failed to start.  Try reconnecting.",
                severity=Severity.FATAL, scope=Scope.SESSION,
            )
            if safe_send_json is not None:
                await safe_send_json(ws, err_frame)
            else:
                # Pre-extract path was raw ws.send_json; preserve
                # for callers that haven't wired safe_send_json yet.
                await ws.send_json(err_frame)
        return None

    logger.info("Pipeline ready for %s", ws_id)
    return pipeline
