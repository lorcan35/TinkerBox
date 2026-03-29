"""Notes service — transcription, summarization, embedding, and search."""

import asyncio
import json
import logging
import math
import os
import tempfile
import time
from pathlib import Path
from typing import Optional

import aiohttp
import numpy as np

from dragon_voice.config import VoiceConfig
from dragon_voice.notes.db import Note, NotesDB

logger = logging.getLogger(__name__)


class NotesService:
    """Orchestrates note creation from audio or text, with STT + LLM + embeddings."""

    def __init__(self, config: VoiceConfig, db: NotesDB) -> None:
        self._config = config
        self._db = db
        self._ollama_url = config.llm.ollama_url.rstrip("/")
        self._genie_model_dir = Path(config.llm.genie_model_dir)
        self._genie_config = config.llm.genie_config
        self._embedding_model = "qwen3-embedding:0.6b"
        self._session: Optional[aiohttp.ClientSession] = None

    async def initialize(self) -> None:
        self._db.initialize()
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None)
        )
        logger.info("Notes service initialized")

    async def shutdown(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._db.close()

    # ── Note CRUD (sync wrappers for DB) ────────────────────────────────

    def create_note(self, note: Note) -> Note:
        return self._db.create(note)

    def get_note(self, note_id: str) -> Optional[Note]:
        return self._db.get(note_id)

    def list_notes(self, limit: int = 50, offset: int = 0) -> tuple[list[Note], int]:
        return self._db.list_all(limit, offset)

    def update_note(self, note_id: str, updates: dict) -> Optional[Note]:
        return self._db.update(note_id, updates)

    def delete_note(self, note_id: str) -> bool:
        return self._db.delete(note_id)

    # ── Audio → Note pipeline ───────────────────────────────────────────

    async def create_from_audio(
        self, pcm_data: bytes, sample_rate: int = 16000
    ) -> Note:
        """Full pipeline: audio → STT → summarize → embed → store."""
        duration_s = len(pcm_data) / (sample_rate * 2)  # 16-bit mono
        logger.info(
            "Processing audio note: %.1fs, %d bytes", duration_s, len(pcm_data)
        )

        # Step 1: Transcribe
        transcript = await self._transcribe(pcm_data, sample_rate)
        if not transcript or transcript.strip() == "":
            transcript = "(empty recording)"

        # Step 2: Generate title + summary with NPU LLM
        title, summary = await self._summarize(transcript)

        # Step 3: Create note
        note = Note(
            title=title,
            transcript=transcript,
            summary=summary,
            source="audio",
            duration_s=duration_s,
        )
        note = self._db.create(note)

        # Step 4: Generate embedding (background — don't block response)
        asyncio.create_task(self._embed_note(note.id, transcript))

        return note

    async def create_from_text(self, text: str, title: str = "") -> Note:
        """Create a note from text input (no audio)."""
        if not title:
            title, _ = await self._summarize(text)
        _, summary = await self._summarize(text)

        note = Note(
            title=title,
            transcript=text,
            summary=summary,
            source="text",
        )
        note = self._db.create(note)
        asyncio.create_task(self._embed_note(note.id, text))
        return note

    # ── Semantic search ─────────────────────────────────────────────────

    async def search(self, query: str, limit: int = 10) -> list[dict]:
        """Semantic search using cosine similarity on embeddings."""
        query_emb = await self._get_embedding(query)
        if not query_emb:
            return []

        notes = self._db.get_all_with_embeddings()
        scored = []
        for note in notes:
            if note.embedding:
                sim = self._cosine_similarity(query_emb, note.embedding)
                scored.append((sim, note))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            {**n.to_dict(), "score": round(s, 4)}
            for s, n in scored[:limit]
        ]

    # ── Internal: STT ───────────────────────────────────────────────────

    async def _transcribe(self, pcm_data: bytes, sample_rate: int) -> str:
        """Transcribe PCM audio using the pipeline's STT backend."""
        from dragon_voice.stt import create_stt

        stt = create_stt(self._config.stt)
        await stt.initialize()
        try:
            audio = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32) / 32768.0
            result = await stt.transcribe(audio, sample_rate)
            return result
        finally:
            await stt.shutdown()

    # ── Internal: LLM summarization ─────────────────────────────────────

    async def _summarize(self, transcript: str) -> tuple[str, str]:
        """Generate title and summary using NPU Genie LLM."""
        prompt = (
            f"Given this transcript, provide:\n"
            f"1. A short title (max 8 words)\n"
            f"2. A 1-2 sentence summary\n\n"
            f"Transcript: {transcript[:2000]}\n\n"
            f"Respond in this exact format:\n"
            f"TITLE: <title>\n"
            f"SUMMARY: <summary>"
        )

        response = await self._run_genie(prompt)

        # Parse response
        title = "Untitled Note"
        summary = transcript[:200] + "..." if len(transcript) > 200 else transcript

        for line in response.split("\n"):
            line = line.strip()
            if line.upper().startswith("TITLE:"):
                title = line[6:].strip().strip('"')
            elif line.upper().startswith("SUMMARY:"):
                summary = line[8:].strip().strip('"')

        return title, summary

    async def _run_genie(self, prompt: str) -> str:
        """Run genie-t2t-run for NPU inference."""
        genie_bin = self._genie_model_dir / "genie-t2t-run"
        config_path = self._genie_model_dir / self._genie_config

        if not genie_bin.exists():
            logger.warning("genie-t2t-run not found, falling back to Ollama")
            return await self._run_ollama(prompt)

        env = os.environ.copy()
        lib_dirs = [str(self._genie_model_dir), "/home/radxa/qairt/lib"]
        env["LD_LIBRARY_PATH"] = ":".join(lib_dirs) + ":" + env.get("LD_LIBRARY_PATH", "")
        env.setdefault("ADSP_LIBRARY_PATH", str(self._genie_model_dir))

        try:
            proc = await asyncio.create_subprocess_exec(
                str(genie_bin), "-c", str(config_path), "-p", prompt,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._genie_model_dir),
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            output = stdout.decode("utf-8", errors="replace")

            # Extract between [BEGIN]: and [END]
            if "[BEGIN]:" in output:
                text = output.split("[BEGIN]:", 1)[1]
                if "[END]" in text:
                    text = text.split("[END]", 1)[0]
                return text.strip()
            return output.strip()
        except Exception as e:
            logger.error("Genie failed: %s, falling back to Ollama", e)
            return await self._run_ollama(prompt)

    async def _run_ollama(self, prompt: str) -> str:
        """Fallback: use Ollama for summarization."""
        try:
            async with self._session.post(
                f"{self._ollama_url}/api/generate",
                json={
                    "model": self._config.llm.ollama_model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"num_predict": 128, "temperature": 0.3},
                },
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("response", "")
        except Exception as e:
            logger.error("Ollama fallback failed: %s", e)
        return ""

    # ── Internal: Embeddings ────────────────────────────────────────────

    async def _embed_note(self, note_id: str, text: str) -> None:
        """Generate and store embedding for a note."""
        embedding = await self._get_embedding(text[:8000])
        if embedding:
            self._db.update(note_id, {"embedding": embedding})
            logger.info("Embedded note %s (%d dims)", note_id, len(embedding))

    async def _get_embedding(self, text: str) -> list[float]:
        """Get embedding vector from Ollama."""
        try:
            async with self._session.post(
                f"{self._ollama_url}/api/embed",
                json={"model": self._embedding_model, "input": text},
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    embeddings = data.get("embeddings", [])
                    if embeddings:
                        return embeddings[0]
        except Exception as e:
            logger.warning("Embedding failed: %s", e)
        return []

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        if len(a) != len(b) or not a:
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)
