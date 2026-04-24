"""Smoke tests for ``dragon_voice/handlers/status.py``.

Exercises the thin wiring — ``/status`` returns an HTML page with the
current backend names; ``/health`` returns JSON with a stable shape.

Run:
    python3 -m pytest -v tests/test_status_handlers.py
"""
from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import MagicMock

from aiohttp import web

from dragon_voice.handlers import status as status_mod


def _make_server(*, start_time=1000.0, stt="moonshine", tts="piper", llm="qwen",
                 active=0, sessions=42) -> MagicMock:
    s = MagicMock()
    s._start_time = start_time
    s._stt_name = stt
    s._tts_name = tts
    s._llm_name = llm
    s._active_connections = {f"conn{i}": {} for i in range(active)}
    s._session_count = sessions
    return s


class StatusHandlerTests(unittest.TestCase):
    def test_status_returns_html_200(self):
        server = _make_server(stt="moonshine", tts="piper", llm="qwen3:0.6b")
        req = MagicMock()

        async def go():
            return await status_mod.handle_status(req, server=server)

        resp: web.Response = asyncio.run(go())
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.content_type, "text/html")
        body = resp.text
        self.assertIn("moonshine", body)
        self.assertIn("piper", body)
        self.assertIn("qwen3:0.6b", body)

    def test_status_shows_active_connection_count(self):
        server = _make_server(active=3)
        req = MagicMock()

        async def go():
            return await status_mod.handle_status(req, server=server)

        resp = asyncio.run(go())
        self.assertIn("Active Connections", resp.text)
        # The count literal "3" should appear between the val span tags.
        self.assertIn('class="val">3<', resp.text)


class HealthHandlerTests(unittest.TestCase):
    def test_health_returns_json_with_expected_shape(self):
        server = _make_server(start_time=0.0, stt="stt_x", tts="tts_y", llm="llm_z", active=5)
        req = MagicMock()

        async def go():
            return await status_mod.handle_health(req, server=server)

        resp: web.Response = asyncio.run(go())
        self.assertEqual(resp.status, 200)
        payload = json.loads(resp.body)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["active_connections"], 5)
        self.assertEqual(payload["backends"], {"stt": "stt_x", "tts": "tts_y", "llm": "llm_z"})
        # uptime_seconds should be a non-negative number (we set
        # start_time=0 so it's the epoch-relative time).
        self.assertIsInstance(payload["uptime_seconds"], float)
        self.assertGreater(payload["uptime_seconds"], 0.0)


if __name__ == "__main__":
    unittest.main()
