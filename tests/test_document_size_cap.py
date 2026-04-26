"""Unit tests for δ3 (D-docs): document ingest size cap.

Issue #118, refs #89, refs #94.

Pre-fix ``MemoryService.ingest_document`` had no size check at the
entry — a 500 MB document would lock the HTTP handler for ~40
minutes embedding chunks (200 chunks × 200 ms embed each, plus the
risk of OOM-ing Dragon's 8 GB RAM during batch embedding).

The fix adds a ``MemoryConfig.max_document_bytes`` (default 10 MB)
and rejects oversized content at BOTH:
  - the API layer (HTTP 413 + structured JSON ``code:
    "document_too_large"``)
  - the service layer (``DocumentTooLargeError`` raised pre-chunking
    so a non-HTTP caller can't bypass)

Tests cover:
  * Config default + dataclass shape
  * Service layer raises DocumentTooLargeError on oversized content
  * Service layer accepts under-cap content (regression guard so
    the gate doesn't accidentally fire on normal-size documents)
  * Service layer disabled (max_document_bytes=0) accepts anything
  * API layer returns HTTP 413 + structured JSON body
  * API layer returns 201 on under-cap content (happy path)

Service-layer tests use a stubbed Database; API-layer tests use the
TestServer/TestClient pattern matching test_ws_upgrade_errors.py
so the JSON body + status code are verified end-to-end.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from dragon_voice.api.documents import DocumentRoutes
from dragon_voice.config import MemoryConfig, VoiceConfig
from dragon_voice.memory import DocumentTooLargeError, MemoryService


# ───────────────────────── MemoryConfig schema


def test_max_document_bytes_default_is_10mb() -> None:
    """Pin the default — 10 MB matches the audit's recommended
    baseline.  A typical Markdown / PDF-extracted document is well
    under this; pathological 100 MB+ inputs fail fast."""
    cfg = MemoryConfig()
    assert cfg.max_document_bytes == 10 * 1024 * 1024


def test_max_document_bytes_in_voice_config() -> None:
    """Belt-and-suspenders against future config-shape refactors."""
    cfg = VoiceConfig()
    assert hasattr(cfg.memory, "max_document_bytes")


# ───────────────────────── MemoryService.ingest_document service-layer guard


def _make_service(max_bytes: int) -> MemoryService:
    """Build a MemoryService with a stubbed DB.  We never reach the
    DB write path in these tests because either the size check raises
    pre-chunk OR we want to verify the under-cap accept path right
    up to the chunking logic.  Stub _db.conn so the under-cap test
    doesn't blow up on the actual INSERT."""
    db = MagicMock()
    db.conn = MagicMock()
    db.conn.execute = AsyncMock()
    db.conn.commit = AsyncMock()
    svc = MemoryService(db, max_document_bytes=max_bytes)
    # _get_embedding gets called per chunk on the under-cap path —
    # stub it so we don't try to hit Ollama from the test.
    svc._get_embedding = AsyncMock(return_value=b"\x00" * 768)
    return svc


def test_ingest_raises_document_too_large_when_over_cap() -> None:
    """The headline service-layer contract: oversized content raises
    DocumentTooLargeError BEFORE any chunking / embedding work."""
    svc = _make_service(max_bytes=1024)  # 1 KB cap for fast test
    big_content = "x" * 2048  # 2 KB > 1 KB cap

    async def go():
        with __import__("pytest").raises(DocumentTooLargeError) as exc_info:
            await svc.ingest_document("title", big_content)
        # Message includes the actual size so ops can see how far
        # over the cap the request was.
        msg = str(exc_info.value)
        assert "2048" in msg, f"Expected actual size in message; got {msg!r}"
        assert "1024" in msg, f"Expected cap in message; got {msg!r}"

    asyncio.run(go())
    # Crucially: the embed mock was NEVER called — the size check
    # short-circuited before any chunking work happened.
    svc._get_embedding.assert_not_called()


def test_ingest_accepts_content_under_cap() -> None:
    """Regression guard: normal-size content must still ingest
    cleanly.  Without this an off-by-one in the size check could
    silently break the happy path."""
    svc = _make_service(max_bytes=10_000)
    content = "This is a normal document. " * 50  # ~1.4 KB, well under

    async def go():
        # Doesn't raise; reaches the embedding loop.
        await svc.ingest_document("title", content)

    asyncio.run(go())
    # _get_embedding was called at least once (chunking happened)
    assert svc._get_embedding.await_count >= 1


def test_ingest_with_cap_zero_accepts_any_size() -> None:
    """``max_document_bytes=0`` is the disable knob.  Pin so a
    future refactor that accidentally removes the guard would
    fail loudly.  This is the escape hatch for ops that want to
    ingest a giant codebase or knowledge dump."""
    svc = _make_service(max_bytes=0)
    huge_content = "x" * (100 * 1024 * 1024)  # 100 MB — would be rejected by default

    async def go():
        await svc.ingest_document("title", huge_content)

    asyncio.run(go())
    # Embed mock was called (at least once per chunk)
    assert svc._get_embedding.await_count >= 1


def test_ingest_size_check_uses_utf8_byte_count() -> None:
    """A 4 MB string of ASCII is 4 MB UTF-8.  A 4 MB string of
    emoji is ~16 MB UTF-8 (4× expansion).  The cap must apply to
    the UTF-8 byte count, not the str length, since that's what
    actually goes into SQLite + the embedding pipeline."""
    svc = _make_service(max_bytes=4 * 1024 * 1024)  # 4 MB cap
    # 2M emojis × 4 bytes = 8 MB UTF-8 → must reject even though
    # str length (2M) is well under any reasonable text-length cap
    emoji_content = "🎉" * (2 * 1024 * 1024)

    async def go():
        with __import__("pytest").raises(DocumentTooLargeError):
            await svc.ingest_document("title", emoji_content)

    asyncio.run(go())


# ───────────────────────── DocumentRoutes API-layer guard


class DocumentRoutesSizeCapTests(unittest.TestCase):
    """API-layer 413 + structured JSON body tests.  Matches the
    test_ws_upgrade_errors.py pattern (TestServer + TestClient)."""

    def _run(self, coro):
        return asyncio.run(coro)

    def _build_app(self, memory: MemoryService) -> web.Application:
        app = web.Application()
        routes = DocumentRoutes(memory)
        routes.register(app)
        return app

    def test_oversized_post_returns_413_with_structured_body(self) -> None:
        """Headline API contract: oversized POST → 413 + JSON body
        with code='document_too_large'."""
        # Real MemoryService instance with a tiny cap; raises on ingest.
        svc = _make_service(max_bytes=512)

        async def go():
            app = self._build_app(svc)
            async with TestServer(app) as srv, TestClient(srv) as client:
                resp = await client.post(
                    "/api/v1/documents",
                    json={"title": "big", "content": "x" * 1024},
                )
                self.assertEqual(resp.status, 413)
                self.assertEqual(resp.content_type, "application/json")
                body = await resp.json()
                self.assertEqual(body["code"], "document_too_large")
                self.assertIn("message", body)

        self._run(go())

    def test_under_cap_post_returns_201(self) -> None:
        """Happy path regression guard: under-cap content still
        accepts cleanly with HTTP 201."""
        svc = _make_service(max_bytes=10_000)

        async def go():
            app = self._build_app(svc)
            async with TestServer(app) as srv, TestClient(srv) as client:
                resp = await client.post(
                    "/api/v1/documents",
                    json={"title": "small", "content": "Hello world. " * 20},
                )
                self.assertEqual(resp.status, 201)

        self._run(go())

    def test_empty_content_still_returns_400_not_413(self) -> None:
        """Pin the boundary: empty content is a different error
        (400 'content required'), NOT a size-cap rejection.  This
        catches a future refactor that might collapse both paths."""
        svc = _make_service(max_bytes=10_000)

        async def go():
            app = self._build_app(svc)
            async with TestServer(app) as srv, TestClient(srv) as client:
                resp = await client.post(
                    "/api/v1/documents",
                    json={"title": "empty", "content": ""},
                )
                self.assertEqual(resp.status, 400)

        self._run(go())


if __name__ == "__main__":
    unittest.main()
