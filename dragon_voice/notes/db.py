"""SQLite storage for notes with full-text search and embeddings."""

import json
import logging
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

NOTES_DIR = Path(os.environ.get("TINKERCLAW_NOTES_DIR", "/home/radxa/tinkerclaw/notes"))
DB_PATH = NOTES_DIR / "notes.db"


@dataclass
class Note:
    id: str = ""
    title: str = ""
    transcript: str = ""
    summary: str = ""
    tags: list[str] = field(default_factory=list)
    source: str = "audio"  # "audio" or "text"
    duration_s: float = 0.0
    word_count: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0
    embedding: list[float] = field(default_factory=list)

    def to_dict(self, include_embedding: bool = False) -> dict:
        d = asdict(self)
        if not include_embedding:
            d.pop("embedding", None)
        return d


class NotesDB:
    """SQLite-backed notes store with embedding support."""

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self._db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None

    def initialize(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._create_tables()
        logger.info("Notes DB initialized at %s", self._db_path)

    def _create_tables(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS notes (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '',
                transcript TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '[]',
                source TEXT NOT NULL DEFAULT 'audio',
                duration_s REAL NOT NULL DEFAULT 0.0,
                word_count INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                embedding BLOB
            );
            CREATE INDEX IF NOT EXISTS idx_notes_created ON notes(created_at DESC);
        """)
        self._conn.commit()

    def create(self, note: Note) -> Note:
        now = time.time()
        if not note.id:
            note.id = uuid.uuid4().hex[:12]
        if not note.created_at:
            note.created_at = now
        note.updated_at = now
        note.word_count = len(note.transcript.split()) if note.transcript else 0

        emb_blob = (
            json.dumps(note.embedding).encode() if note.embedding else None
        )

        self._conn.execute(
            """INSERT INTO notes
               (id, title, transcript, summary, tags, source,
                duration_s, word_count, created_at, updated_at, embedding)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                note.id, note.title, note.transcript, note.summary,
                json.dumps(note.tags), note.source, note.duration_s,
                note.word_count, note.created_at, note.updated_at, emb_blob,
            ),
        )
        self._conn.commit()
        logger.info("Created note %s: '%s'", note.id, note.title[:50])
        return note

    def get(self, note_id: str) -> Optional[Note]:
        row = self._conn.execute(
            "SELECT * FROM notes WHERE id = ?", (note_id,)
        ).fetchone()
        return self._row_to_note(row) if row else None

    def list_all(
        self, limit: int = 50, offset: int = 0
    ) -> tuple[list[Note], int]:
        total = self._conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
        rows = self._conn.execute(
            "SELECT * FROM notes ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [self._row_to_note(r) for r in rows], total

    def update(self, note_id: str, updates: dict) -> Optional[Note]:
        note = self.get(note_id)
        if not note:
            return None

        if "title" in updates:
            note.title = updates["title"]
        if "transcript" in updates:
            note.transcript = updates["transcript"]
            note.word_count = len(note.transcript.split())
        if "summary" in updates:
            note.summary = updates["summary"]
        if "tags" in updates:
            note.tags = updates["tags"]
        if "embedding" in updates:
            note.embedding = updates["embedding"]

        note.updated_at = time.time()
        emb_blob = (
            json.dumps(note.embedding).encode() if note.embedding else None
        )

        self._conn.execute(
            """UPDATE notes SET title=?, transcript=?, summary=?, tags=?,
               word_count=?, updated_at=?, embedding=? WHERE id=?""",
            (
                note.title, note.transcript, note.summary,
                json.dumps(note.tags), note.word_count, note.updated_at,
                emb_blob, note_id,
            ),
        )
        self._conn.commit()
        return note

    def delete(self, note_id: str) -> bool:
        cursor = self._conn.execute(
            "DELETE FROM notes WHERE id = ?", (note_id,)
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def get_all_with_embeddings(self) -> list[Note]:
        rows = self._conn.execute(
            "SELECT * FROM notes WHERE embedding IS NOT NULL"
        ).fetchall()
        return [self._row_to_note(r, load_embedding=True) for r in rows]

    def _row_to_note(self, row: sqlite3.Row, load_embedding: bool = False) -> Note:
        embedding = []
        if load_embedding and row["embedding"]:
            embedding = json.loads(row["embedding"])
        return Note(
            id=row["id"],
            title=row["title"],
            transcript=row["transcript"],
            summary=row["summary"],
            tags=json.loads(row["tags"]),
            source=row["source"],
            duration_s=row["duration_s"],
            word_count=row["word_count"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            embedding=embedding,
        )

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
