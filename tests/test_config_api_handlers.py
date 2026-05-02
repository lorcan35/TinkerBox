"""Smoke tests for ``dragon_voice/handlers/config_api.py``.

Covers the three branches of ``handle_set_config``:
  - invalid JSON -> 400
  - validation failure -> 400 with "details"
  - happy path -> 200, backend names updated, pipelines swap_backends called

``handle_get_config`` is a thin wrapper over ``config_to_dict`` and is
covered indirectly by the existing ``test_config_redact.py`` which
targets that helper.

Run:
    python3 -m pytest -v tests/test_config_api_handlers.py
"""
from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import web

from dragon_voice.handlers import config_api as cfg_mod


class _ReqWithJson:
    """Minimal stub for ``web.Request`` — just an async ``json()``.

    Raising from ``json()`` simulates an invalid-JSON body.
    """

    def __init__(self, body=None, raise_exc: Exception | None = None):
        self._body = body
        self._raise = raise_exc

    async def json(self):
        if self._raise is not None:
            raise self._raise
        return self._body


def _make_stub_backends(stt="stt_a", tts="tts_a", llm="llm_a"):
    """Build the nested-attr shape ``load_config`` returns."""
    cfg = MagicMock()
    cfg.stt.backend = stt
    cfg.tts.backend = tts
    cfg.llm.backend = llm
    cfg.validate.return_value = []
    return cfg


class SetConfigHandlerTests(unittest.TestCase):
    def test_invalid_json_returns_400(self):
        req = _ReqWithJson(raise_exc=json.JSONDecodeError("bad", "x", 0))
        server = MagicMock()

        async def go():
            return await cfg_mod.handle_set_config(req, server=server)

        resp: web.Response = asyncio.run(go())
        self.assertEqual(resp.status, 400)
        payload = json.loads(resp.body)
        self.assertEqual(payload["error"], "Invalid JSON")

    def test_validation_failure_returns_400_with_details(self):
        cfg = _make_stub_backends()
        cfg.validate.return_value = ["stt.backend unknown"]
        server = MagicMock()
        server._active_connections = {}

        req = _ReqWithJson(body={"stt": {"backend": "nonsense"}})

        async def go():
            with patch.object(cfg_mod, "load_config", return_value=cfg):
                return await cfg_mod.handle_set_config(req, server=server)

        resp = asyncio.run(go())
        self.assertEqual(resp.status, 400)
        payload = json.loads(resp.body)
        self.assertEqual(payload["error"], "Config validation failed")
        self.assertEqual(payload["details"], ["stt.backend unknown"])
        # Server config should NOT have been swapped in.
        self.assertNotEqual(server._config, cfg)

    def test_happy_path_swaps_pipeline_backends(self):
        cfg = _make_stub_backends(stt="new_stt", tts="new_tts", llm="new_llm")
        # Two connections, one with a pipeline (swap_backends expected),
        # one without (skipped).
        pipeline = MagicMock()
        pipeline.swap_backends = AsyncMock()
        server = MagicMock()
        server._config = MagicMock()
        server._active_connections = {
            "ws_with_pipeline": {"pipeline": pipeline, "conn_lock": None},
            "ws_without_pipeline": {"pipeline": None},
        }

        req = _ReqWithJson(body={"llm": {"backend": "new_llm"}})

        async def go():
            with patch.object(cfg_mod, "load_config", return_value=cfg):
                return await cfg_mod.handle_set_config(req, server=server)

        resp = asyncio.run(go())
        self.assertEqual(resp.status, 200)
        payload = json.loads(resp.body)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["backends"],
                         {"stt": "new_stt", "tts": "new_tts", "llm": "new_llm"})

        # Server state should now reflect new backend names.
        self.assertEqual(server._stt_name, "new_stt")
        self.assertEqual(server._tts_name, "new_tts")
        self.assertEqual(server._llm_name, "new_llm")
        self.assertIs(server._config, cfg)

        # swap_backends called exactly once on the pipeline-holding conn.
        pipeline.swap_backends.assert_awaited_once_with(cfg)

    def test_happy_path_also_swaps_conversation_llm(self):
        """DIP-2 (audit 2026-05-03): the HTTP swap path must rotate the
        shared ConversationEngine's LLM, not just per-pipeline backends.

        Pre-fix the WS config_update path called swap_llm but the HTTP
        path didn't — leaving ConvEngine on the stale backend after
        every POST /api/config.  This test pins the canonical wiring.
        """
        cfg = _make_stub_backends(stt="x_stt", tts="x_tts", llm="x_llm")
        cfg.llm = MagicMock()  # the slice swap_llm receives
        pipeline = MagicMock()
        pipeline.swap_backends = AsyncMock()
        conversation = MagicMock()
        conversation.swap_llm = AsyncMock(return_value=(MagicMock(), True))
        backend_pool: dict = {}

        server = MagicMock()
        server._config = MagicMock()
        server._conversation = conversation
        server._backend_pool = backend_pool
        server._active_connections = {
            "ws_a": {"pipeline": pipeline, "conn_lock": None},
        }

        req = _ReqWithJson(body={"llm": {"backend": "x_llm"}})

        async def go():
            with patch.object(cfg_mod, "load_config", return_value=cfg):
                return await cfg_mod.handle_set_config(req, server=server)

        resp = asyncio.run(go())
        self.assertEqual(resp.status, 200)
        # Both swap surfaces hit, in order: pipeline first, then ConvEngine.
        pipeline.swap_backends.assert_awaited_once_with(cfg)
        conversation.swap_llm.assert_awaited_once_with(
            cfg.llm, pool=backend_pool, voice_mode=None,
        )

    def test_conversation_swap_failure_does_not_500(self):
        """If swap_llm raises (e.g. backend init throws), the HTTP
        handler keeps the per-pipeline swap result and returns 200 so
        the caller still sees the partial-success state."""
        cfg = _make_stub_backends(stt="x_stt", tts="x_tts", llm="x_llm")
        cfg.llm = MagicMock()
        pipeline = MagicMock()
        pipeline.swap_backends = AsyncMock()
        conversation = MagicMock()
        conversation.swap_llm = AsyncMock(side_effect=RuntimeError("init blew up"))

        server = MagicMock()
        server._config = MagicMock()
        server._conversation = conversation
        server._backend_pool = {}
        server._active_connections = {
            "ws_a": {"pipeline": pipeline, "conn_lock": None},
        }

        req = _ReqWithJson(body={"llm": {"backend": "x_llm"}})

        async def go():
            with patch.object(cfg_mod, "load_config", return_value=cfg):
                return await cfg_mod.handle_set_config(req, server=server)

        resp = asyncio.run(go())
        # Per-pipeline swap succeeded; conversation swap failure is a
        # warning, not a 500 — caller can re-issue if it cares.
        self.assertEqual(resp.status, 200)
        pipeline.swap_backends.assert_awaited_once()
        conversation.swap_llm.assert_awaited_once()

    def test_no_conversation_attribute_does_not_500(self):
        """During boot, server._conversation may not exist yet — the
        handler should still succeed (skips the ConvEngine swap)."""
        cfg = _make_stub_backends()
        cfg.llm = MagicMock()
        server = MagicMock(spec=["_active_connections", "_config", "_stt_name",
                                 "_tts_name", "_llm_name", "_backend_pool"])
        server._active_connections = {}
        server._backend_pool = {}
        # Note: no _conversation attribute set.

        req = _ReqWithJson(body={"llm": {"backend": "new_llm"}})

        async def go():
            with patch.object(cfg_mod, "load_config", return_value=cfg):
                return await cfg_mod.handle_set_config(req, server=server)

        resp = asyncio.run(go())
        self.assertEqual(resp.status, 200)


class GetConfigHandlerTests(unittest.TestCase):
    def test_returns_redacted_config(self):
        server = MagicMock()
        server._config = MagicMock()
        req = MagicMock()

        async def go():
            with patch.object(cfg_mod, "config_to_dict",
                              return_value={"stt": {"backend": "x"}}) as mock_dump:
                resp = await cfg_mod.handle_get_config(req, server=server)
                mock_dump.assert_called_once_with(server._config, redact_secrets=True)
                return resp

        resp: web.Response = asyncio.run(go())
        self.assertEqual(resp.status, 200)
        payload = json.loads(resp.body)
        self.assertEqual(payload, {"stt": {"backend": "x"}})


if __name__ == "__main__":
    unittest.main()
