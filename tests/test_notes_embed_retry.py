"""W4: embedding must never fail/roll back the note insert, and a transient
embedding error must retry in the background until it succeeds."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from dragon_voice.notes.service import NotesService


def _make_service() -> NotesService:
    svc = NotesService.__new__(NotesService)  # bypass __init__ / config
    svc._db = MagicMock()
    svc._db.update = AsyncMock()
    svc._bg_tasks = set()
    return svc


@pytest.mark.asyncio
async def test_embed_retries_then_succeeds(monkeypatch):
    """First two _get_embedding calls fail (return []), the third succeeds.
    _embed_note must keep retrying until it stores a non-empty vector, and must
    NOT raise (so the note insert is never affected)."""
    svc = _make_service()
    calls = {"n": 0}

    async def fake_get_embedding(text):
        calls["n"] += 1
        if calls["n"] < 3:
            return []  # simulate "Server disconnected"
        return [0.1, 0.2, 0.3]

    monkeypatch.setattr(svc, "_get_embedding", fake_get_embedding)
    monkeypatch.setattr("dragon_voice.notes.service._EMBED_RETRY_DELAYS", [0, 0, 0])

    await svc._embed_note("note-1", "transcript")

    assert calls["n"] == 3
    svc._db.update.assert_awaited_once_with("note-1", {"embedding": [0.1, 0.2, 0.3]})


@pytest.mark.asyncio
async def test_embed_gives_up_without_raising(monkeypatch):
    """If every attempt fails, _embed_note gives up quietly — never raises (so
    the note insert is unaffected) and never stores an empty vector."""
    svc = _make_service()

    async def always_fail(text):
        return []

    monkeypatch.setattr(svc, "_get_embedding", always_fail)
    monkeypatch.setattr("dragon_voice.notes.service._EMBED_RETRY_DELAYS", [0, 0, 0])

    await svc._embed_note("note-2", "transcript")  # must not raise
    svc._db.update.assert_not_awaited()
