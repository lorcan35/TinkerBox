"""Post-validation, pre-swap config finalization helpers.

Wave 23 SOLID-audit follow-up — eighth sub-handler extract from
`_handle_config_update` (after #214/215/216/217/218/219/220).

This module owns the final two pre-swap steps: persisting the
new config to the session DB row, and propagating the OpenRouter
API key from `conn_config.llm` into `conn_config.stt` / `.tts`
for the modes that need cloud STT/TTS.

Both run AFTER:
  * select_backends_for_mode (#216) chose the backend names
  * validate_config_swap_prereqs (#217) confirmed prerequisites

And BEFORE:
  * swap_pipeline_and_conversation_backends (#219) actually swaps

## Two functions, one module

The DB persist (`persist_session_config_to_db`) and the conn_config
finalize (`apply_swap_config_to_conn`) are different concerns
(persistence vs config plumbing) but both are part of the
"finalization before swap" step.  Keeping them in one module
makes the natural call site one logical unit:

```python
await persist_session_config_to_db(...)
apply_swap_config_to_conn(...)
await swap_pipeline_and_conversation_backends(...)
```

Splitting into two modules would scatter the finalization phase
across the import graph.

## Failure isolation

`persist_session_config_to_db` swallows DB exceptions with a
warning log — matches pre-extract behaviour where a failed
session row update did NOT block the in-memory pipeline swap
(the in-memory state IS the source of truth at runtime; the
DB row is for cross-session resume).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from dragon_voice.voice_modes import VoiceMode

logger = logging.getLogger(__name__)


def _resolve_active_model_for_db(
    vmode: VoiceMode,
    conn_config: Any,
    llm_backend: str,
    llm_model_request: Optional[str],
) -> str:
    """Pick the active-model string to persist to the session row.

    Same shape as `config_update_ack._resolve_active_model` BUT with
    one extra fallback: when the chosen LLM backend isn't
    openrouter / tinkerclaw / ollama, fall through to the user's
    requested `llm_model` string (rather than `""`).  This matches
    the historical pre-extract behaviour (server.py:2218 used
    `str(llm_model or "")` here).

    The two paths could be unified in a follow-up if the divergence
    proves not to matter — but pinning behaviour first.
    """
    if vmode.is_cloud():
        return conn_config.llm.openrouter_model or ""
    if llm_backend == "tinkerclaw":
        return conn_config.llm.tinkerclaw_model or ""
    if llm_backend == "ollama":
        return conn_config.llm.ollama_model or ""
    return str(llm_model_request or "")


async def persist_session_config_to_db(
    db: Optional[Any],
    *,
    session_id: Optional[str],
    vmode: VoiceMode,
    conn_config: Any,                  # VoiceConfig
    llm_backend: str,
    llm_model_request: Optional[str],
) -> None:
    """Persist the post-swap config (system_prompt + voice_mode +
    llm_model) onto the session DB row.

    Chat v4·C (refs #27): the session drawer in chat-v4 surfaces the
    active mode fingerprint, and pipeline-resume reads voice_mode +
    llm_model from this row so a fresh device reconnection picks
    the right backends without waiting for a config_update from
    the client.

    No-op when:
      * `db` is None (test paths, embedded usage)
      * `session_id` is None / empty (boot race; no session yet)

    Failure isolation:
      DB exceptions are caught + logged at WARNING level.  A failed
      DB write must NOT block the in-memory pipeline swap — the
      in-memory state IS the runtime source of truth; the DB row
      is just for cross-session resume.
    """
    if not session_id or db is None:
        return

    active_model_db = _resolve_active_model_for_db(
        vmode, conn_config, llm_backend, llm_model_request,
    )
    try:
        await db.update_session(
            session_id,
            system_prompt=conn_config.llm.system_prompt,
            voice_mode=int(vmode),
            llm_model=active_model_db[:128],
        )
    except Exception:
        logger.warning(
            "Failed to update session system_prompt / mode",
        )


def apply_swap_config_to_conn(
    conn_config: Any,                  # VoiceConfig
    *,
    vmode: VoiceMode,
    stt_backend: str,
    tts_backend: str,
    llm_backend: str,
) -> None:
    """Finalize `conn_config` for the pending swap.

    Two mutations in place:

      1. Stamp the chosen backend names onto
         `conn_config.{stt,tts,llm}.backend` so the pipeline-swap
         code below picks them up.
      2. For modes that route STT/TTS through OpenRouter (Hybrid /
         Cloud / TC-with-cloud-suffix), propagate
         `conn_config.llm.openrouter_api_key` and
         `conn_config.llm.openrouter_url` over to the STT and TTS
         sub-configs.  Without this propagation the cloud STT/TTS
         backends crash on init with "missing API key" because
         their config slots were initialised from the local-only
         defaults.

    No return value — pure mutation.  Pinned by tests so a future
    refactor can't silently drop the API-key propagation step.
    """
    conn_config.stt.backend = stt_backend
    conn_config.tts.backend = tts_backend
    conn_config.llm.backend = llm_backend

    # Propagate API keys for cloud STT/TTS backends:
    #   - Hybrid + Cloud always need OpenRouter STT/TTS
    #   - TinkerClaw mode needs OpenRouter STT/TTS only when Tab5
    #     requested it via the "cloud" suffix in llm_model (the
    #     resolved stt_backend will be "openrouter" in that case).
    if vmode.needs_cloud_stt_tts() or (vmode.is_tinkerclaw() and stt_backend == "openrouter"):
        conn_config.stt.openrouter_api_key = conn_config.llm.openrouter_api_key
        conn_config.stt.openrouter_url = conn_config.llm.openrouter_url
        conn_config.tts.openrouter_api_key = conn_config.llm.openrouter_api_key
        conn_config.tts.openrouter_url = conn_config.llm.openrouter_url
