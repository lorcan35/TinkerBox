"""Memory service: facts, documents, and RAG for the agentic pipeline.

Three tiers:
  1. Facts — short user preferences/info with embeddings
  2. Conversations — existing messages table (searched via embeddings)
  3. Documents — ingested text, chunked and embedded

All stored in SQLite with cosine similarity search.
"""

import json
import logging
import math
import secrets
import struct
import time
from typing import Optional

import aiohttp

from dragon_voice.db import Database

logger = logging.getLogger(__name__)

# Chunk size for document ingestion
CHUNK_SIZE = 512  # tokens (approx chars/4)
CHUNK_OVERLAP = 50


class MemoryService:
    """Manages facts, documents, and semantic search."""

    def __init__(self, db: Database, ollama_url: str = "http://localhost:11434",
                 embed_model: str = "nomic-embed-text") -> None:
        self._db = db
        self._ollama_url = ollama_url
        self._embed_model = embed_model
        self._embed_dim: int = 0  # set after first embedding call

    async def initialize(self) -> None:
        """Create memory tables if they don't exist."""
        await self._db.conn.executescript("""
            CREATE TABLE IF NOT EXISTS memory_facts (
                id          TEXT PRIMARY KEY,
                content     TEXT NOT NULL,
                source      TEXT NOT NULL DEFAULT 'conversation',
                session_id  TEXT,
                embedding   BLOB,
                created_at  REAL NOT NULL,
                updated_at  REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_facts_created ON memory_facts(created_at DESC);

            CREATE TABLE IF NOT EXISTS memory_documents (
                id          TEXT PRIMARY KEY,
                title       TEXT NOT NULL DEFAULT '',
                content     TEXT NOT NULL DEFAULT '',
                chunk_count INTEGER NOT NULL DEFAULT 0,
                source      TEXT NOT NULL DEFAULT 'upload',
                metadata    TEXT NOT NULL DEFAULT '{}',
                created_at  REAL NOT NULL,
                updated_at  REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS memory_chunks (
                id          TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                content     TEXT NOT NULL,
                embedding   BLOB,
                created_at  REAL NOT NULL,
                FOREIGN KEY (document_id) REFERENCES memory_documents(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_chunks_doc ON memory_chunks(document_id, chunk_index);
        """)
        await self._db.conn.commit()
        logger.info("MemoryService initialized (embed_model=%s)", self._embed_model)

    # ── Embeddings ──

    async def _get_embedding(self, text: str) -> Optional[bytes]:
        """Get embedding vector from Ollama. Returns packed floats as bytes."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self._ollama_url}/api/embed",
                    json={"model": self._embed_model, "input": text},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status != 200:
                        logger.warning("Embedding request failed: %d", resp.status)
                        return None
                    data = await resp.json()
                    embeddings = data.get("embeddings", [])
                    if not embeddings:
                        return None
                    vec = embeddings[0]
                    self._embed_dim = len(vec)
                    return struct.pack(f"{len(vec)}f", *vec)
        except Exception as e:
            logger.warning("Embedding failed (Ollama may not be running): %s", e)
            return None

    def _cosine_similarity(self, a: bytes, b: bytes) -> float:
        """Compute cosine similarity between two packed float vectors."""
        if not a or not b or len(a) != len(b):
            return 0.0
        n = len(a) // 4
        va = struct.unpack(f"{n}f", a)
        vb = struct.unpack(f"{n}f", b)
        dot = sum(x * y for x, y in zip(va, vb))
        norm_a = math.sqrt(sum(x * x for x in va))
        norm_b = math.sqrt(sum(x * x for x in vb))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    # ── Facts ──

    async def store_fact(self, content: str, source: str = "conversation",
                         session_id: Optional[str] = None) -> dict:
        """Store a fact with embedding."""
        fact_id = secrets.token_hex(6)
        now = time.time()
        embedding = await self._get_embedding(content)

        await self._db.conn.execute(
            """INSERT INTO memory_facts (id, content, source, session_id, embedding, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (fact_id, content, source, session_id, embedding, now, now),
        )
        await self._db.conn.commit()
        logger.info("Fact stored: %s (%s)", fact_id, content[:50])
        return {"id": fact_id, "content": content, "source": source, "created_at": now}

    async def list_facts(self, limit: int = 50, offset: int = 0) -> list[dict]:
        """List all facts, ordered by creation time."""
        cursor = await self._db.conn.execute(
            "SELECT id, content, source, session_id, created_at, updated_at FROM memory_facts ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def delete_fact(self, fact_id: str) -> bool:
        """Delete a fact by ID."""
        cursor = await self._db.conn.execute(
            "DELETE FROM memory_facts WHERE id = ?", (fact_id,)
        )
        await self._db.conn.commit()
        return cursor.rowcount > 0

    async def search_facts(self, query: str, limit: int = 5) -> list[dict]:
        """Search facts by semantic similarity."""
        query_emb = await self._get_embedding(query)

        cursor = await self._db.conn.execute(
            "SELECT id, content, source, session_id, embedding, created_at FROM memory_facts"
        )
        rows = await cursor.fetchall()

        scored = []
        for row in rows:
            row_dict = dict(row)
            if query_emb and row_dict.get("embedding"):
                score = self._cosine_similarity(query_emb, row_dict["embedding"])
            else:
                # Fallback: simple keyword match
                score = 0.1 if query.lower() in row_dict["content"].lower() else 0.0
            if score > 0.0:
                scored.append({
                    "id": row_dict["id"],
                    "content": row_dict["content"],
                    "source": row_dict["source"],
                    "score": round(score, 3),
                    "created_at": row_dict["created_at"],
                })

        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:limit]

    # ── Documents ──

    def _chunk_text(self, text: str) -> list[str]:
        """Split text into overlapping chunks (~512 tokens each)."""
        # Approximate: 1 token ≈ 4 chars
        char_size = CHUNK_SIZE * 4
        overlap_chars = CHUNK_OVERLAP * 4
        chunks = []
        start = 0
        while start < len(text):
            end = start + char_size
            chunk = text[start:end]
            if chunk.strip():
                chunks.append(chunk.strip())
            start = end - overlap_chars
        return chunks

    async def ingest_document(self, title: str, content: str,
                              metadata: Optional[dict] = None) -> dict:
        """Ingest a document: chunk, embed, store."""
        doc_id = secrets.token_hex(6)
        now = time.time()
        chunks = self._chunk_text(content)

        await self._db.conn.execute(
            """INSERT INTO memory_documents (id, title, content, chunk_count, source, metadata, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'upload', ?, ?, ?)""",
            (doc_id, title, content[:500], len(chunks), json.dumps(metadata or {}), now, now),
        )

        for i, chunk_text in enumerate(chunks):
            chunk_id = secrets.token_hex(6)
            embedding = await self._get_embedding(chunk_text)
            await self._db.conn.execute(
                """INSERT INTO memory_chunks (id, document_id, chunk_index, content, embedding, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (chunk_id, doc_id, i, chunk_text, embedding, now),
            )

        await self._db.conn.commit()
        logger.info("Document ingested: %s (%d chunks)", doc_id, len(chunks))
        return {
            "id": doc_id, "title": title, "chunk_count": len(chunks),
            "content_preview": content[:200], "created_at": now,
        }

    async def list_documents(self, limit: int = 50, offset: int = 0) -> list[dict]:
        """List documents (without chunk detail)."""
        cursor = await self._db.conn.execute(
            "SELECT id, title, chunk_count, source, metadata, created_at, updated_at FROM memory_documents ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def delete_document(self, doc_id: str) -> bool:
        """Delete a document and all its chunks (CASCADE)."""
        cursor = await self._db.conn.execute(
            "DELETE FROM memory_documents WHERE id = ?", (doc_id,)
        )
        await self._db.conn.commit()
        return cursor.rowcount > 0

    async def search_documents(self, query: str, limit: int = 5) -> list[dict]:
        """Search across document chunks, return best matches with parent doc info."""
        query_emb = await self._get_embedding(query)

        cursor = await self._db.conn.execute(
            """SELECT c.id, c.document_id, c.chunk_index, c.content, c.embedding,
                      d.title as doc_title, d.source as doc_source
               FROM memory_chunks c
               JOIN memory_documents d ON c.document_id = d.id"""
        )
        rows = await cursor.fetchall()

        scored = []
        for row in rows:
            row_dict = dict(row)
            if query_emb and row_dict.get("embedding"):
                score = self._cosine_similarity(query_emb, row_dict["embedding"])
            else:
                score = 0.1 if query.lower() in row_dict["content"].lower() else 0.0
            if score > 0.0:
                scored.append({
                    "document_id": row_dict["document_id"],
                    "document_title": row_dict["doc_title"],
                    "chunk_index": row_dict["chunk_index"],
                    "content": row_dict["content"][:300],
                    "score": round(score, 3),
                })

        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:limit]

    async def get_relevant_context(self, query: str, max_facts: int = 3,
                                    max_chunks: int = 3) -> str:
        """Get relevant facts + document chunks as formatted context string."""
        facts = await self.search_facts(query, limit=max_facts)
        chunks = await self.search_documents(query, limit=max_chunks)

        if not facts and not chunks:
            return ""

        lines = ["[MEMORY CONTEXT]"]
        for f in facts:
            lines.append(f"- {f['content']}")
        for c in chunks:
            lines.append(f"- [From {c['document_title']}]: {c['content'][:200]}")
        lines.append("[END MEMORY CONTEXT]")
        return "\n".join(lines)
