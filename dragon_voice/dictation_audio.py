"""Persistent WAV storage for /api/v1/transcribe uploads.

Content is paramount: every WAV that hits the transcribe endpoint is
kept on disk so the user can re-transcribe later with a stronger
model, audit Whisper hallucinations against the original audio, or
download the raw recording.

Storage layout
--------------
    <root>/<audio_id>.wav

`audio_id` = first 16 hex chars of the sha256 of the WAV bytes.
Content-addressed — re-uploading the same WAV is a no-op.

Why content-address instead of UUID
-----------------------------------
Tab5 can retry a failed POST without coordinating with Dragon — the
second upload writes to the same file and returns the same id.  No
duplicate audio on disk, no "did this already get saved?" guesswork.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class DictationAudioStore:
    """Disk-backed store keyed by content hash."""

    def __init__(self, root: str | os.PathLike) -> None:
        self._root = Path(root)
        self._ready = False
        logger.info("DictationAudioStore: root=%s (lazy mkdir)", self._root)

    def _ensure_dir(self) -> None:
        if self._ready:
            return
        self._root.mkdir(parents=True, exist_ok=True)
        self._ready = True

    def _path(self, audio_id: str) -> Path:
        # Defense in depth — block path traversal via crafted ids
        if not audio_id.isalnum() or len(audio_id) not in (16, 64):
            raise ValueError(f"invalid audio_id: {audio_id!r}")
        return self._root / f"{audio_id}.wav"

    async def save(self, wav_bytes: bytes) -> str:
        """Persist a WAV blob; return its audio_id."""
        self._ensure_dir()
        audio_id = hashlib.sha256(wav_bytes).hexdigest()[:16]
        path = self._path(audio_id)
        if path.exists():
            return audio_id

        def _write() -> None:
            tmp = path.with_suffix(".wav.tmp")
            tmp.write_bytes(wav_bytes)
            tmp.rename(path)

        await asyncio.get_running_loop().run_in_executor(None, _write)
        logger.info(
            "dictation_audio saved: id=%s bytes=%d", audio_id, len(wav_bytes)
        )
        return audio_id

    async def load(self, audio_id: str) -> Optional[bytes]:
        path = self._path(audio_id)
        if not path.exists():
            return None
        return await asyncio.get_running_loop().run_in_executor(
            None, path.read_bytes
        )

    async def delete(self, audio_id: str) -> bool:
        path = self._path(audio_id)
        if not path.exists():
            return False
        await asyncio.get_running_loop().run_in_executor(None, path.unlink)
        logger.info("dictation_audio deleted: id=%s", audio_id)
        return True

    def list_ids(self) -> list[str]:
        return [p.stem for p in self._root.glob("*.wav")]
