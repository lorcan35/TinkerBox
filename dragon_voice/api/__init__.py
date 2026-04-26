"""API route package — modular REST endpoints for Dragon Voice Server.

All routes are registered via setup_all_routes() called from server.py.
"""

import logging

from aiohttp import web

from dragon_voice.api.sessions import SessionRoutes
from dragon_voice.api.messages import MessageRoutes
from dragon_voice.api.devices import DeviceRoutes
from dragon_voice.api.config_routes import ConfigRoutes
from dragon_voice.api.events import EventRoutes
from dragon_voice.api.media_routes import MediaRoutes
from dragon_voice.api.synthesize import SynthesizeRoutes
from dragon_voice.api.completions import CompletionRoutes
from dragon_voice.api.system import SystemRoutes

logger = logging.getLogger(__name__)


def setup_all_routes(
    app: web.Application,
    db,
    session_mgr,
    message_store,
    conversation=None,
    voice_config=None,
    start_time: float = 0,
    get_active_connections=None,
    tool_registry=None,
    memory_service=None,
    media_store=None,
    media_url_signer=None,
    scheduler_mgr=None,
) -> None:
    """Register all API route modules on the aiohttp app.

    Single entry point called from VoiceServer._on_startup().
    """
    # Core CRUD routes
    SessionRoutes(db, session_mgr, message_store, conversation).register(app)
    MessageRoutes(db, session_mgr, message_store, conversation).register(app)
    DeviceRoutes(db).register(app)
    ConfigRoutes(db).register(app)
    EventRoutes(db).register(app)

    # Media endpoints (TTS synthesis, STT transcription, OTA)
    if voice_config:
        SynthesizeRoutes(voice_config).register(app)

    # Media file serving + camera upload (Task 2)
    # Wave 14 W14-H04: pass the URL signer so /api/media/* requires a
    # valid HMAC signature + expiry when DRAGON_API_TOKEN is configured.
    if media_store:
        MediaRoutes(media_store, url_signer=media_url_signer).register(app)

    # Direct LLM completion
    CompletionRoutes(conversation).register(app)

    # System info + backend listing
    if voice_config and get_active_connections:
        SystemRoutes(voice_config, start_time, get_active_connections, get_db=db).register(app)

    # Agentic routes (Sprint 2 — registered when available)
    if tool_registry:
        try:
            from dragon_voice.api.tools import ToolRoutes
            ToolRoutes(tool_registry).register(app)
        except ImportError:
            logger.debug("Tool routes not available yet")

    if memory_service:
        try:
            from dragon_voice.api.memory_routes import MemoryRoutes
            MemoryRoutes(memory_service).register(app)
            from dragon_voice.api.documents import DocumentRoutes
            DocumentRoutes(memory_service).register(app)
        except ImportError:
            logger.debug("Memory/document routes not available yet")

    # Phase 5 ε1b (issue #126): scheduler REST endpoints.  Guarded so
    # a SchedulerManager init failure (logged in startup.py) doesn't
    # take the API package down with it.
    if scheduler_mgr is not None:
        try:
            from dragon_voice.api.scheduler import SchedulerRoutes
            SchedulerRoutes(scheduler_mgr).register(app)
            logger.info("Scheduler REST routes registered (5 endpoints)")
        except ImportError:
            logger.debug("Scheduler routes not available yet")

    logger.info("API routes registered (modular package)")
