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

    logger.info("API routes registered (modular package)")
