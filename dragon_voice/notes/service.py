"""Notes service — transcription, summarization, embedding, and search."""

import asyncio
import json
import logging
import math
import os
import time
from pathlib import Path
from typing import Optional

import aiohttp
import numpy as np

from dragon_voice.config import VoiceConfig
from dragon_voice.notes.db import Note, NotesDB

logger = logging.getLogger(__name__)

# W4: bounded background retry for note embeddings.  An embedding failure (e.g.
# "Server disconnected") must never block or fail the note insert — the note is
# saved + usable the moment the transcript exists; only the semantic index of
# that note is deferred until a retry succeeds.  Delays in seconds (backoff).
_EMBED_RETRY_DELAYS = [2, 10, 30]


def _placeholder_title(text: str, max_words: int = 8) -> str:
    """First N non-empty words from the transcript as a placeholder title.

    Used as the initial title at note-create time so the HTTP response
    can return immediately; the real LLM-generated title overwrites it
    when the background task finishes.
    """
    if not text:
        return "Untitled note"
    words = text.strip().split()
    if not words:
        return "Untitled note"
    head = " ".join(words[:max_words])
    if len(words) > max_words:
        head += "…"
    return head[:80]


def _placeholder_summary(text: str, max_chars: int = 200) -> str:
    """First N chars of the transcript as a placeholder summary."""
    s = (text or "").strip()
    if len(s) <= max_chars:
        return s
    return s[:max_chars].rstrip() + "…"


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
        # Wave 14 W14-C06 / W14-M16 RUF006: track fire-and-forget embed tasks
        # so shutdown can cancel/await them instead of hitting
        # "Task was destroyed but it is pending" on systemctl restart.
        self._bg_tasks: set[asyncio.Task] = set()

    def _spawn_bg(self, coro) -> asyncio.Task:
        """Create a tracked background task.

        The returned task is retained in ``self._bg_tasks`` until it finishes,
        then auto-discarded via ``add_done_callback``. Shutdown cancels any
        stragglers.
        """
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    async def initialize(self) -> None:
        # Wave 14 W14-C05: NotesDB.initialize is now async (aiosqlite).
        await self._db.initialize()
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None)
        )
        logger.info("Notes service initialized")

    async def shutdown(self) -> None:
        # Wave 14 W14-C06: cancel and await in-flight background embed tasks
        # before closing the http session they depend on, otherwise a racing
        # task would hit a closed session and log a spurious ConnectionError.
        if self._bg_tasks:
            for t in list(self._bg_tasks):
                t.cancel()
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)
        if self._session and not self._session.closed:
            await self._session.close()
        # Wave 14 W14-C05: close is now async.
        await self._db.close()

    # ── Note CRUD (async wrappers for DB) ───────────────────────────────
    # Wave 14 W14-C05: every method below now awaits the async NotesDB.
    # Callers in notes/api.py also had to gain await.

    async def create_note(self, note: Note) -> Note:
        return await self._db.create(note)

    async def get_note(self, note_id: str) -> Optional[Note]:
        return await self._db.get(note_id)

    async def list_notes(self, limit: int = 50, offset: int = 0) -> tuple[list[Note], int]:
        return await self._db.list_all(limit, offset)

    async def update_note(self, note_id: str, updates: dict) -> Optional[Note]:
        return await self._db.update(note_id, updates)

    async def delete_note(self, note_id: str) -> bool:
        return await self._db.delete(note_id)

    # ── Audio → Note pipeline ───────────────────────────────────────────

    async def create_from_audio(
        self, pcm_data: bytes, sample_rate: int = 16000
    ) -> Note:
        """Full pipeline: audio → STT → store → (background) summarize + embed.

        Title/summary generation is offloaded so the HTTP response
        comes back in <1 s instead of blocking on a ~2 min Genie/Ollama
        turn.  The note lands with a quick-and-dirty placeholder title
        (first words of the transcript); the real title arrives via
        ``update_note`` once the background task completes.
        """
        duration_s = len(pcm_data) / (sample_rate * 2)  # 16-bit mono
        logger.info(
            "Processing audio note: %.1fs, %d bytes", duration_s, len(pcm_data)
        )

        transcript = await self._transcribe(pcm_data, sample_rate)
        if not transcript or transcript.strip() == "":
            transcript = "(empty recording)"

        placeholder_title = _placeholder_title(transcript)
        placeholder_summary = _placeholder_summary(transcript)
        note = Note(
            title=placeholder_title,
            transcript=transcript,
            summary=placeholder_summary,
            source="audio",
            duration_s=duration_s,
        )
        note = await self._db.create(note)

        # Background: real title/summary via LLM + semantic embedding.
        # Neither blocks the HTTP response — the network-error chip on
        # Tab5 was Dragon holding the POST for the full LLM duration.
        self._spawn_bg(self._fill_title_summary(note.id, transcript))
        self._spawn_bg(self._embed_note(note.id, transcript))

        return note

    async def create_from_text(self, text: str, title: str = "") -> Note:
        """Create a note from text input (no audio).

        Same async pattern as ``create_from_audio`` — the LLM title +
        summary fill in later; the HTTP response comes back fast.
        """
        initial_title = title or _placeholder_title(text)
        initial_summary = _placeholder_summary(text)
        note = Note(
            title=initial_title,
            transcript=text,
            summary=initial_summary,
            source="text",
        )
        note = await self._db.create(note)
        # Only fire title/summary in the background when the caller
        # didn't already supply a title — they may be authoring it
        # explicitly (e.g. typed Notes flow).
        if not title:
            self._spawn_bg(self._fill_title_summary(note.id, text))
        else:
            self._spawn_bg(self._fill_summary_only(note.id, text))
        self._spawn_bg(self._embed_note(note.id, text))
        return note

    async def _fill_title_summary(self, note_id: str, text: str) -> None:
        """Background task: generate real title+summary and update note."""
        try:
            title, summary = await self._summarize(text)
        except Exception:  # noqa: BLE001
            logger.warning("Background title+summary failed for %s", note_id, exc_info=True)
            return
        await self._db.update(note_id, {"title": title, "summary": summary})

    async def _fill_summary_only(self, note_id: str, text: str) -> None:
        """Background task: leave the user-supplied title alone, just
        fill in the summary."""
        try:
            _, summary = await self._summarize(text)
        except Exception:  # noqa: BLE001
            logger.warning("Background summary failed for %s", note_id, exc_info=True)
            return
        await self._db.update(note_id, {"summary": summary})

    # ── Semantic search ─────────────────────────────────────────────────

    async def search(self, query: str, limit: int = 10) -> list[dict]:
        """Semantic search using cosine similarity on embeddings."""
        query_emb = await self._get_embedding(query)
        if not query_emb:
            return []

        notes = await self._db.get_all_with_embeddings()
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
                    "keep_alive": "30s",
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
        """Generate + store the note embedding, retrying transient failures.

        Never raises — embedding is best-effort background work (W4).  On every
        attempt failure we wait a backoff and retry; after the last delay we give
        up quietly (the note stays usable, just unindexed until a later edit
        re-embeds it).  This makes a transient "Server disconnected" self-heal
        instead of leaving the note permanently unsearchable.
        """
        for delay in [0, *_EMBED_RETRY_DELAYS]:
            if delay:
                await asyncio.sleep(delay)
            try:
                embedding = await self._get_embedding(text[:8000])
            except Exception:
                logger.warning("Embedding attempt failed for %s", note_id, exc_info=True)
                continue
            if embedding:
                await self._db.update(note_id, {"embedding": embedding})
                logger.info("Embedded note %s (%d dims)", note_id, len(embedding))
                return
        logger.warning("Embedding gave up for %s after %d retries", note_id, len(_EMBED_RETRY_DELAYS))

    async def _get_embedding(self, text: str) -> list[float]:
        """Get embedding vector from Ollama."""
        try:
            async with self._session.post(
                f"{self._ollama_url}/api/embed",
                json={"model": self._embedding_model, "input": text, "keep_alive": "30s"},
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
