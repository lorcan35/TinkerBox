"""Notes module + NoteTool init step for `run_startup`.

Wave 23 SOLID-audit follow-up — twenty-eighth sub-extract.
Third slice from `dragon_voice/lifecycle/startup.py` (audit
SRP-6 finalisation, after PR #252 + #253).

Owns the boot wiring for:
  * `NotesDB` + `NotesService` (Notes module persistence layer
    + service interface)
  * Notes API routes (registered on the aiohttp `app`)
  * `NoteTool` (registered on the existing `_tool_registry` so
    the LLM can call it agentically)

Pre-extract this 22-LOC chunk lived inline in `run_startup`
between MCP bridges and the purge/monitor wiring.  Now lives
in its own module with tests pinning the failure-isolation
invariant (Notes-not-available doesn't block boot).

## API

```python
await init_notes_module(server, app)
```

Mutates ``server._notes_svc`` in place; registers Notes API
routes on `app`; registers `NoteTool` on `server._tool_registry`
(when both registry + notes service are available).

## Failure isolation

Whole chain wrapped in try/except — Notes is an optional module
(it depends on the notes_db schema being present, the
NotesService being configured, etc.).  A missing optional dep
logs at WARNING but doesn't block boot.  Voice/text paths still
work; just no notes API surface or NoteTool calls.
"""
from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

logger = logging.getLogger(__name__)


async def init_notes_module(
    server: Any,
    app: web.Application,
) -> None:
    """Initialise NotesDB + NotesService, register Notes API
    routes on `app`, and register NoteTool on the tool registry.

    Run AFTER `init_agentic_modules` (depends on the registry)
    AND AFTER REST API setup (the Notes routes register on the
    same `app`).

    Side effects:
      * `server._notes_svc` ← live NotesService (or unset on
        init failure)
      * Notes API routes added to `app.router`
      * NoteTool registered on `server._tool_registry` (when
        both registry + notes service are available)

    Failure isolation: any exception in the chain logs at
    WARNING and returns silently.  Boot continues.
    """
    try:
        from dragon_voice.notes.api import setup_routes as setup_notes_routes
        from dragon_voice.notes.db import NotesDB
        from dragon_voice.notes.service import NotesService

        notes_db = NotesDB()
        # Wave 14 W14-C05: NotesDB.initialize is async now.
        # NotesService awaits it internally so we don't call
        # it here.
        notes_svc = NotesService(server._config, notes_db)
        await notes_svc.initialize()
        server._notes_svc = notes_svc
        setup_notes_routes(app, notes_svc)
        logger.info("Notes API routes registered")

        # Register NoteTool now that NotesService is available.
        if server._tool_registry and server._notes_svc:
            from dragon_voice.tools.note_tool import NoteTool
            server._tool_registry.register(NoteTool(server._notes_svc))
            logger.info("Note tool registered (notes service available)")
    except Exception as e:
        logger.warning("Notes API not available: %s", e)
