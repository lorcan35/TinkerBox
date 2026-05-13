"""W4-D: Dragon-side coredump scraper tests.

Exercises `dragon_voice/coredump_scraper.py` in isolation — no live
Tab5 needed.  Uses an in-process aiohttp test server as the Tab5
impostor so we can mock `/info` + `/coredump` responses.

Run:
    python3 -m pytest -v tests/test_coredump_scraper.py
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import aiohttp
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

from dragon_voice import coredump_scraper as cs
from dragon_voice.config import (
    CoredumpScraperConfig,
    CoredumpScraperTarget,
)


# Sample payload — small enough to hash quickly, big enough to be
# recognisably non-trivial.
_DUMP_BYTES = b"\xa4\x92\x00\x00" + bytes(range(256)) * 16  # 4100 bytes
_DUMP_SHA256 = hashlib.sha256(_DUMP_BYTES).hexdigest()


class _Tab5Impostor:
    """Tiny aiohttp app that mimics Tab5's `/info` + `/coredump`."""

    def __init__(self):
        self.coredump_present = True
        self.dump_body = _DUMP_BYTES
        self.required_token = "test-token"
        self.info_status = 200
        self.coredump_status = 200

    def routes(self) -> list:
        async def crashlog(req: web.Request) -> web.Response:
            # Bearer-gated, matches live Tab5 `/crashlog` shape.
            if self.info_status != 200:
                return web.Response(status=self.info_status)
            auth = req.headers.get("Authorization", "")
            if auth != f"Bearer {self.required_token}":
                return web.Response(status=401)
            return web.json_response({
                "reset_reason": "SW",
                "was_crash": False,
                "coredump_present": self.coredump_present,
                "exc_pc": 1234,
                "exc_task": "heap_wd",
            })

        async def coredump(req: web.Request) -> web.Response:
            if self.coredump_status != 200:
                return web.Response(status=self.coredump_status)
            auth = req.headers.get("Authorization", "")
            if auth != f"Bearer {self.required_token}":
                return web.Response(status=401)
            return web.Response(body=self.dump_body)

        return [
            web.get("/crashlog", crashlog),
            web.get("/coredump", coredump),
        ]


class _BaseScraperTest(AioHTTPTestCase):
    """Spins up the Tab5 impostor + makes a fresh temp save_dir per test."""

    async def get_application(self) -> web.Application:
        self.tab5 = _Tab5Impostor()
        app = web.Application()
        app.add_routes(self.tab5.routes())
        return app

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.save_dir = self._tmpdir.name
        # Clear the process-global LAST_RESULTS so one test's outcome
        # doesn't leak into the next.
        cs.LAST_RESULTS.clear()

    async def asyncTearDown(self):
        self._tmpdir.cleanup()
        cs.LAST_RESULTS.clear()
        await super().asyncTearDown()

    def _target(self, device_id="tab5-test", token="test-token") -> CoredumpScraperTarget:
        return CoredumpScraperTarget(
            device_id=device_id,
            host=self.server.host,
            port=self.server.port,
            token=token,
        )


class TestProbeInfo(_BaseScraperTest):
    async def test_returns_parsed_dict_on_200(self):
        async with aiohttp.ClientSession() as s:
            info, err = await cs.probe_info(
                s, self.server.host, self.server.port, timeout_s=2.0,
                token="test-token",
            )
        self.assertEqual(err, "")
        self.assertIsInstance(info, dict)
        self.assertTrue(info["coredump_present"])

    async def test_returns_none_with_error_on_non_200(self):
        self.tab5.info_status = 503
        async with aiohttp.ClientSession() as s:
            info, err = await cs.probe_info(
                s, self.server.host, self.server.port, timeout_s=2.0,
                token="test-token",
            )
        self.assertIsNone(info)
        self.assertIn("HTTP 503", err)

    async def test_401_on_wrong_token(self):
        async with aiohttp.ClientSession() as s:
            info, err = await cs.probe_info(
                s, self.server.host, self.server.port, timeout_s=2.0,
                token="wrong-token",
            )
        self.assertIsNone(info)
        self.assertIn("401", err)


class TestPullCoredump(_BaseScraperTest):
    async def test_returns_bytes_on_200(self):
        async with aiohttp.ClientSession() as s:
            body, err = await cs.pull_coredump(
                s, self.server.host, self.server.port,
                token="test-token", timeout_s=2.0,
            )
        self.assertEqual(err, "")
        self.assertEqual(body, _DUMP_BYTES)

    async def test_401_on_wrong_token(self):
        async with aiohttp.ClientSession() as s:
            body, err = await cs.pull_coredump(
                s, self.server.host, self.server.port,
                token="wrong-token", timeout_s=2.0,
            )
        self.assertIsNone(body)
        self.assertIn("401", err)


class TestScrapeTarget(_BaseScraperTest):
    async def test_happy_path_archives_dump(self):
        async with aiohttp.ClientSession() as s:
            res = await cs.scrape_target(
                s, self._target(), self.save_dir,
                firmware_elf="", request_timeout_s=2.0,
            )
        self.assertTrue(res.ok)
        self.assertTrue(res.coredump_present)
        self.assertFalse(res.deduped)
        self.assertEqual(res.sha256, _DUMP_SHA256)
        self.assertEqual(res.saved_bytes, len(_DUMP_BYTES))
        # File actually written to disk under the device dir.
        bin_path = Path(res.saved_path)
        self.assertTrue(bin_path.exists())
        self.assertEqual(bin_path.read_bytes(), _DUMP_BYTES)
        # Sidecar .sha256 also written.
        sha_path = bin_path.with_suffix(".sha256")
        self.assertTrue(sha_path.exists())
        self.assertEqual(sha_path.read_text().strip(), _DUMP_SHA256)

    async def test_dedupes_when_sha_matches_existing(self):
        # First scrape: writes the file.
        async with aiohttp.ClientSession() as s:
            first = await cs.scrape_target(
                s, self._target(), self.save_dir,
                firmware_elf="", request_timeout_s=2.0,
            )
            second = await cs.scrape_target(
                s, self._target(), self.save_dir,
                firmware_elf="", request_timeout_s=2.0,
            )
        self.assertFalse(first.deduped)
        self.assertTrue(second.deduped)
        # Both point at the same path — the existing file.
        self.assertEqual(first.saved_path, second.saved_path)
        # And the dir has exactly one .bin file (no duplicate).
        device_dir = Path(self.save_dir) / "tab5-test"
        bins = list(device_dir.glob("dump-*.bin"))
        self.assertEqual(len(bins), 1)

    async def test_skips_when_coredump_not_present(self):
        self.tab5.coredump_present = False
        async with aiohttp.ClientSession() as s:
            res = await cs.scrape_target(
                s, self._target(), self.save_dir,
                firmware_elf="", request_timeout_s=2.0,
            )
        self.assertTrue(res.ok)
        self.assertFalse(res.coredump_present)
        self.assertIsNone(res.saved_path)
        # Nothing written to disk.
        device_dir = Path(self.save_dir) / "tab5-test"
        self.assertFalse(device_dir.exists())

    async def test_404_on_coredump_endpoint_surfaces_error(self):
        # Tab5 says coredump_present=true but /coredump returns 404
        # (race: dump was wiped between /info and /coredump).
        self.tab5.coredump_status = 404
        async with aiohttp.ClientSession() as s:
            res = await cs.scrape_target(
                s, self._target(), self.save_dir,
                firmware_elf="", request_timeout_s=2.0,
            )
        self.assertTrue(res.ok)  # /info worked
        self.assertTrue(res.coredump_present)  # /info said yes
        self.assertIsNone(res.saved_path)
        self.assertIn("404", res.error)

    async def test_unreachable_host_caught(self):
        # Use an obviously-unreachable port.
        target = CoredumpScraperTarget(
            device_id="tab5-test", host="127.0.0.1", port=1, token="x",
        )
        async with aiohttp.ClientSession() as s:
            res = await cs.scrape_target(
                s, target, self.save_dir,
                firmware_elf="", request_timeout_s=0.5,
            )
        self.assertFalse(res.ok)
        self.assertIn("crashlog ", res.error)
        # No file written.
        self.assertFalse((Path(self.save_dir) / "tab5-test").exists())

    async def test_incomplete_target_short_circuits(self):
        # Missing host — bail immediately, no HTTP attempted.
        target = CoredumpScraperTarget(
            device_id="tab5-test", host="", port=8080, token="x",
        )
        async with aiohttp.ClientSession() as s:
            res = await cs.scrape_target(
                s, target, self.save_dir,
                firmware_elf="", request_timeout_s=2.0,
            )
        self.assertFalse(res.ok)
        self.assertIn("incomplete", res.error)

    async def test_db_event_logged_on_successful_pull(self):
        # Mock the db so we can assert log_event was called.
        db_calls = []

        class _StubDB:
            async def log_event(self_db, event_type, session_id, device_id, payload):
                db_calls.append({
                    "type": event_type, "session_id": session_id,
                    "device_id": device_id, "payload": payload,
                })

        async with aiohttp.ClientSession() as s:
            await cs.scrape_target(
                s, self._target(), self.save_dir,
                firmware_elf="", request_timeout_s=2.0,
                db=_StubDB(),
            )
        self.assertEqual(len(db_calls), 1)
        self.assertEqual(db_calls[0]["type"], "tab5.coredump_pulled")
        self.assertEqual(db_calls[0]["device_id"], "tab5-test")
        self.assertEqual(db_calls[0]["payload"]["bytes"], len(_DUMP_BYTES))
        self.assertEqual(db_calls[0]["payload"]["sha256"], _DUMP_SHA256)

    async def test_dedupe_does_not_log_event(self):
        # Second scrape of the same blob is a no-op — no event row.
        db_calls = []

        class _StubDB:
            async def log_event(self_db, event_type, session_id, device_id, payload):
                db_calls.append(event_type)

        async with aiohttp.ClientSession() as s:
            await cs.scrape_target(
                s, self._target(), self.save_dir,
                firmware_elf="", request_timeout_s=2.0,
                db=_StubDB(),
            )
            await cs.scrape_target(
                s, self._target(), self.save_dir,
                firmware_elf="", request_timeout_s=2.0,
                db=_StubDB(),
            )
        self.assertEqual(len(db_calls), 1)  # only the first wrote


class TestListArchived(unittest.TestCase):
    """`list_archived` walks save_dir + emits the JSON-ready listing."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.save_dir = self._tmpdir.name

    def tearDown(self):
        self._tmpdir.cleanup()

    def _write_dump(self, device_id: str, ts: int, body: bytes) -> Path:
        device_dir = Path(self.save_dir) / device_id
        device_dir.mkdir(parents=True, exist_ok=True)
        bin_path = device_dir / f"dump-{ts}.bin"
        bin_path.write_bytes(body)
        bin_path.with_suffix(".sha256").write_text(
            hashlib.sha256(body).hexdigest()
        )
        # set mtime to ts so newest-first ordering is deterministic
        import os
        os.utime(bin_path, (ts, ts))
        return bin_path

    def test_empty_dir_returns_empty_list(self):
        self.assertEqual(cs.list_archived(self.save_dir), [])

    def test_missing_dir_returns_empty_list(self):
        self.assertEqual(cs.list_archived("/tmp/does-not-exist-w4d"), [])

    def test_lists_dumps_newest_first(self):
        self._write_dump("tab5-a", 1000, b"x" * 10)
        self._write_dump("tab5-a", 2000, b"y" * 20)
        self._write_dump("tab5-b", 1500, b"z" * 30)
        items = cs.list_archived(self.save_dir)
        self.assertEqual(len(items), 3)
        # Newest first
        self.assertEqual(items[0]["ts"], 2000)
        self.assertEqual(items[1]["ts"], 1500)
        self.assertEqual(items[2]["ts"], 1000)
        # device_id correctly split out
        self.assertEqual(items[0]["device_id"], "tab5-a")
        self.assertEqual(items[1]["device_id"], "tab5-b")
        # SHA + bytes surfaced
        self.assertEqual(items[0]["bytes"], 20)
        self.assertTrue(items[0]["sha256"])
        # No symbolicate sidecar => null
        self.assertIsNone(items[0]["symbolicated"])

    def test_symbolicated_sidecar_surfaces(self):
        bin_path = self._write_dump("tab5-a", 1000, b"x" * 10)
        bin_path.with_suffix(".txt").write_text("decoded backtrace\n")
        items = cs.list_archived(self.save_dir)
        self.assertIsNotNone(items[0]["symbolicated"])
        self.assertTrue(items[0]["symbolicated"].endswith(".txt"))


# ── Config + loop gating ────────────────────────────────────────────


class TestConfigDefaults(unittest.TestCase):
    def test_disabled_by_default(self):
        cfg = CoredumpScraperConfig()
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.targets, [])

    def test_target_defaults(self):
        t = CoredumpScraperTarget()
        self.assertEqual(t.device_id, "")
        self.assertEqual(t.port, 8080)


class TestLoadConfigFromYAML(unittest.TestCase):
    """End-to-end: a yaml `coredump_scraper:` section parses into a
    typed config with a typed targets list."""

    def test_yaml_round_trip(self):
        import yaml
        from dragon_voice.config import load_config

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml",
                                         delete=False) as f:
            yaml.dump({
                "server": {},
                "stt": {"backend": "moonshine"},
                "tts": {"backend": "piper"},
                "llm": {"backend": "ollama"},
                "audio": {},
                "tools": {},
                "memory": {},
                "database": {},
                "coredump_scraper": {
                    "enabled": True,
                    "poll_interval_s": 30.0,
                    "save_dir": "/tmp/test-w4d",
                    "targets": [
                        {"device_id": "tab5-aaa", "host": "10.0.0.1",
                         "port": 8080, "token": "tok-1"},
                        {"device_id": "tab5-bbb", "host": "10.0.0.2",
                         "port": 8081, "token": "tok-2"},
                    ],
                },
            }, f)
            cfg_path = f.name
        cfg = load_config(cfg_path)
        self.assertTrue(cfg.coredump_scraper.enabled)
        self.assertEqual(cfg.coredump_scraper.poll_interval_s, 30.0)
        self.assertEqual(len(cfg.coredump_scraper.targets), 2)
        # Each is the typed dataclass, not a dict
        t0 = cfg.coredump_scraper.targets[0]
        self.assertIsInstance(t0, CoredumpScraperTarget)
        self.assertEqual(t0.device_id, "tab5-aaa")
        self.assertEqual(t0.token, "tok-1")


class TestSymbolicateBestEffort(unittest.TestCase):
    """`symbolicate` must never raise + handles missing elf."""

    def test_missing_elf_returns_error(self):
        import asyncio
        out, err = asyncio.run(cs.symbolicate(
            Path("/tmp/nonexistent.bin"),
            Path("/tmp/nonexistent.elf"),
            timeout_s=2.0,
        ))
        self.assertIsNone(out)
        self.assertIn("elf not found", err)


if __name__ == "__main__":
    unittest.main()
