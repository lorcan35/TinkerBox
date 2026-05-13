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
                 active=0, sessions=42, backend_pool=None) -> MagicMock:
    s = MagicMock()
    s._start_time = start_time
    s._stt_name = stt
    s._tts_name = tts
    s._llm_name = llm
    s._active_connections = {f"conn{i}": {} for i in range(active)}
    s._session_count = sessions
    # W4-B (audit 2026-05-11): handle_health probes backends out of
    # this pool.  Default = empty so legacy tests still see "no active
    # backend" rather than tripping on a MagicMock.
    s._backend_pool = backend_pool if backend_pool is not None else {}
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
    """Pre-W4-B shape coverage.  When `_backend_pool` is empty (the
    test default), each subsystem renders as `"(none)" ok=true detail=
    "no active backend"` — operators see "server up, no backends to
    probe yet" which is the honest answer when no Tab5 has connected."""

    def test_health_returns_json_with_per_subsystem_shape(self):
        server = _make_server(start_time=0.0, stt="stt_x", tts="tts_y",
                              llm="llm_z", active=5)
        req = MagicMock()

        async def go():
            return await status_mod.handle_health(req, server=server)

        resp: web.Response = asyncio.run(go())
        self.assertEqual(resp.status, 200)
        payload = json.loads(resp.body)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["active_connections"], 5)
        # New W4-B shape: per-subsystem dict, not flat strings.
        for sub in ("stt", "tts", "llm"):
            self.assertIn(sub, payload["backends"])
            entry = payload["backends"][sub]
            self.assertIn("name", entry)
            self.assertIn("ok", entry)
            self.assertIn("detail", entry)
            self.assertIs(entry["ok"], True)  # empty pool → no probe
            self.assertEqual(entry["detail"], "no active backend")
        self.assertIsInstance(payload["uptime_seconds"], float)
        self.assertGreater(payload["uptime_seconds"], 0.0)


# ── W4-B: per-subsystem honest probes ───────────────────────────────

from dragon_voice.llm.base import LLMBackend, Modality
from dragon_voice.stt.base import STTBackend
from dragon_voice.tts.base import TTSBackend


class _FakeSTT(STTBackend):
    """In-process STT backend with a tunable health_check."""

    def __init__(self, *, ok=True, detail="ok-stt", raise_=None, sleep=0.0):
        self._ok = ok
        self._detail = detail
        self._raise = raise_
        self._sleep = sleep

    async def initialize(self): pass
    async def transcribe(self, audio_bytes, sample_rate=16000): return ""
    async def shutdown(self): pass

    @property
    def name(self): return "fake-stt"

    async def health_check(self, timeout_s=2.0):
        if self._sleep:
            await asyncio.sleep(self._sleep)
        if self._raise is not None:
            raise self._raise
        return self._ok, self._detail


class _FakeTTS(TTSBackend):
    def __init__(self, *, ok=True, detail="ok-tts", raise_=None, sleep=0.0):
        self._ok = ok
        self._detail = detail
        self._raise = raise_
        self._sleep = sleep

    async def initialize(self): pass
    async def synthesize(self, text): return b""
    async def shutdown(self): pass

    @property
    def sample_rate(self): return 22050

    @property
    def name(self): return "fake-tts"

    async def health_check(self, timeout_s=2.0):
        if self._sleep:
            await asyncio.sleep(self._sleep)
        if self._raise is not None:
            raise self._raise
        return self._ok, self._detail


class _FakeLLM(LLMBackend):
    def __init__(self, *, ok=True, detail="ok-llm", raise_=None, sleep=0.0):
        self._ok = ok
        self._detail = detail
        self._raise = raise_
        self._sleep = sleep

    async def initialize(self): pass

    async def generate_stream(self, prompt, system_prompt=""):
        yield ""

    async def shutdown(self): pass

    @property
    def name(self): return "fake-llm"

    @property
    def capabilities(self): return frozenset({Modality.TEXT})

    async def health_check(self, timeout_s=2.0):
        if self._sleep:
            await asyncio.sleep(self._sleep)
        if self._raise is not None:
            raise self._raise
        return self._ok, self._detail


def _pool_with(stt=None, tts=None, llm=None) -> dict:
    """Build a backend_pool dict like the live server's.  Keys mirror
    the real signature-tuple convention; values are the backends."""
    pool: dict = {}
    if stt is not None:
        pool[("stt", "fake")] = stt
    if tts is not None:
        pool[("tts", "fake")] = tts
    if llm is not None:
        pool[("llm", "fake")] = llm
    return pool


class HealthProbeIntegrationTests(unittest.TestCase):
    def test_all_three_backends_ok_returns_status_ok(self):
        server = _make_server(backend_pool=_pool_with(
            stt=_FakeSTT(ok=True, detail="up"),
            tts=_FakeTTS(ok=True, detail="up"),
            llm=_FakeLLM(ok=True, detail="up"),
        ))
        resp = asyncio.run(status_mod.handle_health(MagicMock(), server=server))
        payload = json.loads(resp.body)
        self.assertEqual(payload["status"], "ok")
        for sub in ("stt", "tts", "llm"):
            self.assertTrue(payload["backends"][sub]["ok"])
            self.assertEqual(payload["backends"][sub]["detail"], "up")

    def test_one_backend_down_returns_degraded(self):
        server = _make_server(backend_pool=_pool_with(
            stt=_FakeSTT(ok=True),
            tts=_FakeTTS(ok=True),
            llm=_FakeLLM(ok=False, detail="connect refused: 11434"),
        ))
        resp = asyncio.run(status_mod.handle_health(MagicMock(), server=server))
        payload = json.loads(resp.body)
        self.assertEqual(payload["status"], "degraded")
        self.assertTrue(payload["backends"]["stt"]["ok"])
        self.assertTrue(payload["backends"]["tts"]["ok"])
        self.assertFalse(payload["backends"]["llm"]["ok"])
        self.assertIn("connect refused", payload["backends"]["llm"]["detail"])

    def test_probe_that_raises_treated_as_not_ok(self):
        server = _make_server(backend_pool=_pool_with(
            stt=_FakeSTT(ok=True),
            tts=_FakeTTS(ok=True),
            llm=_FakeLLM(raise_=RuntimeError("kaboom")),
        ))
        resp = asyncio.run(status_mod.handle_health(MagicMock(), server=server))
        payload = json.loads(resp.body)
        self.assertEqual(payload["status"], "degraded")
        self.assertFalse(payload["backends"]["llm"]["ok"])
        # Exception class name + message surface in detail so operators
        # can see what blew up without reading server logs.
        self.assertIn("RuntimeError", payload["backends"]["llm"]["detail"])
        self.assertIn("kaboom", payload["backends"]["llm"]["detail"])

    def test_probe_slower_than_timeout_reports_timeout(self):
        # _FakeSTT sleeps 3 s; probe budget is 2 s.  Must short-circuit.
        slow = _FakeSTT(sleep=3.0, ok=True, detail="should not appear")
        server = _make_server(backend_pool=_pool_with(
            stt=slow,
            tts=_FakeTTS(ok=True),
            llm=_FakeLLM(ok=True),
        ))
        # Patch the module's probe budget down so the test doesn't take
        # 3 s.  Sleep MUST exceed (probe_timeout + 0.5 s) — that's the
        # outer wait_for budget — or the backend returns OK before we
        # interrupt it.  0.05 s probe + 0.5 s buffer = 0.55 s; use 1.5 s
        # sleep so we're well over.
        slow._sleep = 1.5
        orig_probe = status_mod._HEALTH_PROBE_TIMEOUT_S
        orig_gather = status_mod._HEALTH_GATHER_BUDGET_S
        try:
            status_mod._HEALTH_PROBE_TIMEOUT_S = 0.05
            status_mod._HEALTH_GATHER_BUDGET_S = 2.5
            resp = asyncio.run(
                status_mod.handle_health(MagicMock(), server=server),
            )
        finally:
            status_mod._HEALTH_PROBE_TIMEOUT_S = orig_probe
            status_mod._HEALTH_GATHER_BUDGET_S = orig_gather
        payload = json.loads(resp.body)
        self.assertEqual(payload["status"], "degraded")
        self.assertFalse(payload["backends"]["stt"]["ok"])
        self.assertIn("exceeded", payload["backends"]["stt"]["detail"])

    def test_status_code_stays_200_even_when_degraded(self):
        # Critical contract: ngrok/systemd/Tab5 healthchecks treat any
        # non-2xx as "server gone."  Subsystem failure must NOT trip that
        # — the truth lives in the JSON body.
        server = _make_server(backend_pool=_pool_with(
            stt=_FakeSTT(ok=False),
            tts=_FakeTTS(ok=False),
            llm=_FakeLLM(ok=False),
        ))
        resp = asyncio.run(status_mod.handle_health(MagicMock(), server=server))
        self.assertEqual(resp.status, 200)
        payload = json.loads(resp.body)
        self.assertEqual(payload["status"], "degraded")

    def test_only_llm_in_pool_yields_synthetic_no_probe_for_others(self):
        # Active session is mode-3 (TinkerClaw) where Dragon's STT/TTS
        # are bypassed.  Only the LLM lands in the pool.  STT/TTS get
        # "no active backend" + ok=true (correct: nothing to probe).
        server = _make_server(backend_pool=_pool_with(
            llm=_FakeLLM(ok=True, detail="gateway up"),
        ))
        resp = asyncio.run(status_mod.handle_health(MagicMock(), server=server))
        payload = json.loads(resp.body)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["backends"]["stt"]["detail"], "no active backend")
        self.assertEqual(payload["backends"]["tts"]["detail"], "no active backend")
        self.assertEqual(payload["backends"]["llm"]["detail"], "gateway up")

    def test_uptime_and_active_connections_still_surface(self):
        server = _make_server(
            start_time=0.0, active=7,
            backend_pool=_pool_with(stt=_FakeSTT(), tts=_FakeTTS(), llm=_FakeLLM()),
        )
        resp = asyncio.run(status_mod.handle_health(MagicMock(), server=server))
        payload = json.loads(resp.body)
        self.assertEqual(payload["active_connections"], 7)
        self.assertIsInstance(payload["uptime_seconds"], float)
        self.assertGreater(payload["uptime_seconds"], 0.0)


class BackendABCDefaultHealthCheckTests(unittest.TestCase):
    """Each backend ABC's default health_check returns (True, 'no probe').
    Local backends (Moonshine/Piper/Vosk/Kokoro) inherit this so they
    don't need overrides — they're healthy as long as initialize()
    completed."""

    def test_stt_default_health_check(self):
        class _MinSTT(STTBackend):
            async def initialize(self): pass
            async def transcribe(self, audio_bytes, sample_rate=16000): return ""
            async def shutdown(self): pass
            @property
            def name(self): return "min"
        ok, detail = asyncio.run(_MinSTT().health_check())
        self.assertTrue(ok)
        self.assertEqual(detail, "no probe")

    def test_tts_default_health_check(self):
        class _MinTTS(TTSBackend):
            async def initialize(self): pass
            async def synthesize(self, text): return b""
            async def shutdown(self): pass
            @property
            def sample_rate(self): return 22050
            @property
            def name(self): return "min"
        ok, detail = asyncio.run(_MinTTS().health_check())
        self.assertTrue(ok)
        self.assertEqual(detail, "no probe")

    def test_llm_default_health_check(self):
        class _MinLLM(LLMBackend):
            async def initialize(self): pass
            async def generate_stream(self, prompt, system_prompt=""):
                yield ""
            async def shutdown(self): pass
            @property
            def name(self): return "min"
        ok, detail = asyncio.run(_MinLLM().health_check())
        self.assertTrue(ok)
        self.assertEqual(detail, "no probe")


if __name__ == "__main__":
    unittest.main()
