"""Wave 14 W14-C05 regression: NotesDB is fully async (aiosqlite).

Prior impl was synchronous sqlite3 inside an aiohttp handler, blocking
the event loop and starving the Tab5 WS keepalive during a voice turn.
This test proves the critical CRUD + search-read paths are:
  (a) real async (no blocking sqlite3.Connection under the hood),
  (b) survive concurrent writes without losing rows, and
  (c) handle the update-unknown-key case without raising.

Run:
    python3 -m pytest tests/test_notes_db_async.py -v
"""

import asyncio
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio

from dragon_voice.notes.db import Note, NotesDB


@pytest_asyncio.fixture
async def db(tmp_path):
    """Fresh aiosqlite-backed NotesDB per test."""
    path = tmp_path / "notes.db"
    db = NotesDB(path)
    await db.initialize()
    try:
        yield db
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_create_get_round_trip(db):
    n = await db.create(Note(title="Hi", transcript="hello world"))
    assert n.id
    assert n.created_at > 0
    fetched = await db.get(n.id)
    assert fetched is not None
    assert fetched.title == "Hi"
    assert fetched.word_count == 2  # "hello world"


@pytest.mark.asyncio
async def test_list_all_pagination(db):
    for i in range(5):
        await db.create(Note(title=f"Note {i}", transcript=f"body {i}"))
    notes, total = await db.list_all(limit=3, offset=0)
    assert total == 5
    assert len(notes) == 3


@pytest.mark.asyncio
async def test_update_mutates_and_preserves_unmentioned_fields(db):
    n = await db.create(Note(title="Orig", transcript="longer text here"))
    updated = await db.update(n.id, {"title": "New"})
    assert updated.title == "New"
    # transcript must survive an update that only touches the title
    assert updated.transcript == "longer text here"


@pytest.mark.asyncio
async def test_update_missing_returns_none(db):
    result = await db.update("no-such-id", {"title": "X"})
    assert result is None


@pytest.mark.asyncio
async def test_delete_returns_bool(db):
    n = await db.create(Note(title="T", transcript=""))
    assert await db.delete(n.id) is True
    assert await db.delete(n.id) is False
    assert await db.get(n.id) is None


@pytest.mark.asyncio
async def test_embedding_round_trip(db):
    n = await db.create(Note(title="Emb", transcript="x"))
    await db.update(n.id, {"embedding": [0.1, 0.2, 0.3]})
    with_emb = await db.get_all_with_embeddings()
    assert len(with_emb) == 1
    assert with_emb[0].embedding == [0.1, 0.2, 0.3]


@pytest.mark.asyncio
async def test_concurrent_creates_do_not_lose_rows(db):
    """20 parallel writes; asyncio.Lock inside NotesDB serializes them."""
    async def _spawn(i):
        return await db.create(Note(title=f"T{i}", transcript=f"body {i}"))

    notes = await asyncio.gather(*(_spawn(i) for i in range(20)))
    assert len({n.id for n in notes}) == 20
    _, total = await db.list_all()
    assert total == 20


@pytest.mark.asyncio
async def test_non_blocking_vs_sync_sqlite():
    """Proves the new path doesn't import the blocking sqlite3 Connection.

    A cheap structural check — regressing to sync sqlite3 would bring back
    the original W14-C05 bug.
    """
    from dragon_voice.notes import db as notes_db_mod
    # The module should import aiosqlite; sqlite3 may still be referenced
    # in other modules but not as the NotesDB connection type.
    assert hasattr(notes_db_mod, "aiosqlite")
    import aiosqlite
    # Instantiating shouldn't create any sqlite3.Connection either.
    inst = NotesDB(Path(tempfile.mkdtemp()) / "probe.db")
    assert inst._conn is None  # lazy — only created in initialize()
    await inst.initialize()
    assert isinstance(inst._conn, aiosqlite.Connection)
    await inst.close()


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
