"""Config get/set HTTP handlers.

* :func:`handle_get_config` — ``GET /config`` — dumps the current
  :class:`VoiceConfig` with secrets redacted (via
  :func:`dragon_voice.config.config_to_dict`).
* :func:`handle_set_config` — ``POST /config`` — hot-reloads the
  config file, applies a partial override from the request body,
  validates, then swaps backends on every active voice pipeline AND
  the shared :class:`ConversationEngine` (Wave 22b / DIP-2).

Backend swaps acquire each connection's ``conn_lock`` first (A06) to
prevent races with a concurrent WS ``config_update`` from the same
device.  Two concurrent ``swap_backends`` calls would interleave
shutdown/init of the same backend instance, producing use-after-free
errors on the shared Ollama / OpenRouter session objects.

After the per-pipeline swap, the conversation engine's ``_llm`` is
also rotated through :meth:`ConversationEngine.swap_llm` so the
HTTP path mirrors the WS ``config_update`` path landed in Wave 22b
(PR #207).  Pre-fix the HTTP path swapped only the pipeline backend,
leaving ``self._conversation._llm`` pointing at the *old* backend —
all subsequent text turns rendered through the stale instance.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from aiohttp import web

from dragon_voice.config import config_to_dict, load_config

logger = logging.getLogger(__name__)


async def handle_get_config(request: web.Request, *, server: Any) -> web.Response:
    """``GET /config`` — redacted config dump."""
    return web.json_response(config_to_dict(server._config, redact_secrets=True))


async def handle_set_config(request: web.Request, *, server: Any) -> web.Response:
    """``POST /config`` — partial hot-reload + backend swap on pipelines.

    Request body: JSON with ``stt``/``tts``/``llm``/``audio`` sections.
    Each section is a partial dict — only listed fields are overridden.
    The reload starts from the on-disk config (so a missing field in
    the body reverts to its file value, not its previous in-memory
    value).
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    logger.info("Config update requested: %s", list(body.keys()))

    try:
        # Reload full config from file first, then apply overrides.
        new_config = load_config()

        for section in ("stt", "tts", "llm", "audio"):
            overrides = body.get(section)
            if not overrides:
                continue
            target = getattr(new_config, section, None)
            if target is None:
                continue
            for k, v in overrides.items():
                if hasattr(target, k):
                    setattr(target, k, v)

        # Validate before applying.
        validation_errors = new_config.validate()
        if validation_errors:
            return web.json_response(
                {"error": "Config validation failed", "details": validation_errors},
                status=400,
            )

        server._config = new_config

        # Update displayed backend names for /status + /health.
        server._stt_name = new_config.stt.backend
        server._tts_name = new_config.tts.backend
        server._llm_name = new_config.llm.backend

        # A06: Swap backends on all active pipelines.  Acquire each
        # connection's conn_lock first to prevent races with a
        # concurrent WS config_update on the same connection — two
        # swap_backends() calls would interleave shutdown/init of the
        # backend, causing use-after-free errors.
        swap_errors = []
        for ws_id, conn in list(server._active_connections.items()):
            pipeline = conn.get("pipeline")
            if pipeline:
                lock = conn.get("conn_lock")
                logger.info("Swapping backends for connection %s", ws_id)
                try:
                    if lock:
                        async with lock:
                            await pipeline.swap_backends(new_config)
                    else:
                        await pipeline.swap_backends(new_config)
                except Exception as e:
                    logger.warning("Backend swap failed for %s: %s", ws_id, e)
                    swap_errors.append(str(e))

        # DIP-2 (audit 2026-05-03): also swap the shared ConversationEngine's
        # LLM backend.  Pre-fix the HTTP path only rotated per-pipeline
        # backends; ConversationEngine kept its old `_llm` reference and
        # every subsequent text turn rendered through the stale instance.
        # Wave 22b (PR #207) extracted ConversationEngine.swap_llm as the
        # canonical swap path used by both WS and HTTP handlers; this is
        # the missing wiring on the HTTP side.  voice_mode=None because
        # HTTP /api/config is a config-level swap (not per-connection
        # tier change); the router branch uses None as "leave tier alone".
        conv = getattr(server, "_conversation", None)
        if conv is not None:
            try:
                await conv.swap_llm(
                    new_config.llm,
                    pool=server._backend_pool,
                    voice_mode=None,
                )
            except Exception as e:  # noqa: BLE001 — best-effort, fall through
                logger.warning("ConversationEngine LLM swap failed: %s", e)
                swap_errors.append(f"conversation: {e}")

        pipelines_with_swap = sum(
            1 for c in server._active_connections.values() if c.get("pipeline")
        )
        return web.json_response(
            {
                "status": "ok",
                "message": f"Config updated, {pipelines_with_swap} pipelines reloaded",
                "backends": {
                    "stt": new_config.stt.backend,
                    "tts": new_config.tts.backend,
                    "llm": new_config.llm.backend,
                },
            }
        )

    except Exception as e:
        logger.exception("Config update failed")
        return web.json_response({"error": str(e)}, status=500)
