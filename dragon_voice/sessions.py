"""Session lifecycle manager for TinkerClaw.

Manages session create/resume/pause/end with persistence via db.py.
Sessions survive WebSocket disconnects — reconnecting devices resume
their previous session if it hasn't timed out.

refs #16
"""

import asyncio
import logging
import secrets
import time
from typing import Optional

from dragon_voice.db import Database

logger = logging.getLogger(__name__)

# Default inactivity timeout before auto-ending a paused session
SESSION_TIMEOUT_S = 1800  # 30 minutes


def _generate_session_id() -> str:
    """Generate a short hex session ID (12 hex chars = 6 bytes)."""
    return secrets.token_hex(6)


class SessionManager:
    """Manages session lifecycle: create, resume, pause, end.

    Backed by the sessions table via Database. Runs a background task
    to auto-end stale sessions.
    """

    def __init__(self, db: Database, timeout_s: float = SESSION_TIMEOUT_S) -> None:
        self._db = db
        self._timeout_s = timeout_s
        self._cleanup_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Start the background cleanup task."""
        # Clean up stale sessions left over from a previous run
        await self._cleanup_stale_on_startup()

        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("SessionManager started (timeout=%ds)", self._timeout_s)

    async def _cleanup_stale_on_startup(self) -> None:
        """End sessions left active/paused from a previous server run.

        Any session with status 'active' or 'paused' whose last_active_at
        is older than 30 minutes is presumed orphaned and set to 'ended'.
        """
        stale = await self._db.get_stale_sessions(self._timeout_s)
        for session in stale:
            await self._db.update_session_status(session["id"], "ended")
        if stale:
            logger.info(
                "Startup cleanup: ended %d stale session(s)", len(stale)
            )
        else:
            logger.debug("Startup cleanup: no stale sessions found")

    async def stop(self) -> None:
        """Stop the background cleanup task."""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None
        logger.info("SessionManager stopped")

    async def create_session(
        self,
        device_id: Optional[str] = None,
        session_type: str = "conversation",
        system_prompt: str = "",
        config: Optional[dict] = None,
    ) -> dict:
        """Create a new session and log the event."""
        session_id = _generate_session_id()
        session = await self._db.create_session(
            session_id=session_id,
            device_id=device_id,
            session_type=session_type,
            system_prompt=system_prompt,
            config=config,
        )
        await self._db.add_event(
            "session.created",
            session_id=session_id,
            device_id=device_id,
            data={"type": session_type},
        )
        logger.info("Session created: %s (device=%s, type=%s)", session_id, device_id, session_type)
        return session

    async def get_session(self, session_id: str) -> Optional[dict]:
        """Fetch a session by ID."""
        return await self._db.get_session(session_id)

    async def resume_session(self, session_id: str) -> Optional[dict]:
        """Resume a paused session. Returns the session or None if not resumable."""
        session = await self._db.get_session(session_id)
        if not session:
            logger.warning("Cannot resume session %s — not found", session_id)
            return None

        if session["status"] == "ended":
            logger.warning("Cannot resume session %s — already ended", session_id)
            return None

        if session["status"] == "active":
            # Already active — just touch it
            await self._db.touch_session(session_id)
            return await self._db.get_session(session_id)

        # Status is 'paused' — resume it
        await self._db.update_session_status(session_id, "active")
        await self._db.add_event(
            "session.resumed",
            session_id=session_id,
            device_id=session.get("device_id"),
        )
        logger.info("Session resumed: %s", session_id)
        return await self._db.get_session(session_id)

    async def pause_session(self, session_id: str) -> None:
        """Pause a session (e.g., on WebSocket disconnect)."""
        session = await self._db.get_session(session_id)
        if not session or session["status"] != "active":
            return

        await self._db.update_session_status(session_id, "paused")
        await self._db.add_event(
            "session.paused",
            session_id=session_id,
            device_id=session.get("device_id"),
        )
        logger.info("Session paused: %s", session_id)

    async def end_session(self, session_id: str) -> None:
        """End a session permanently."""
        session = await self._db.get_session(session_id)
        if not session or session["status"] == "ended":
            return

        await self._db.update_session_status(session_id, "ended")
        await self._db.add_event(
            "session.ended",
            session_id=session_id,
            device_id=session.get("device_id"),
        )
        logger.info("Session ended: %s", session_id)

    async def list_sessions(
        self,
        device_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """List sessions with optional filters and pagination."""
        return await self._db.list_sessions(
            device_id=device_id, status=status, limit=limit, offset=offset
        )

    async def get_or_create_session(
        self,
        device_id: str,
        requested_session_id: Optional[str] = None,
        session_type: str = "conversation",
        system_prompt: str = "",
        config: Optional[dict] = None,
    ) -> tuple[dict, bool]:
        """Get an existing session or create a new one.

        If requested_session_id is provided and the session exists and is
        resumable, resume it. Otherwise create a new session.

        Returns:
            (session_dict, resumed: bool)
        """
        if requested_session_id:
            session = await self.resume_session(requested_session_id)
            if session:
                return session, True

        # Create new session
        session = await self.create_session(
            device_id=device_id,
            session_type=session_type,
            system_prompt=system_prompt,
            config=config,
        )
        return session, False

    async def _cleanup_loop(self) -> None:
        """Background task: auto-end sessions that have been inactive too long."""
        while True:
            try:
                await asyncio.sleep(60)  # Check every minute
                stale = await self._db.get_stale_sessions(self._timeout_s)
                for session in stale:
                    await self.end_session(session["id"])
                    logger.info(
                        "Auto-ended stale session: %s (inactive for %.0fs)",
                        session["id"],
                        time.time() - session["last_active_at"],
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error in session cleanup loop")
