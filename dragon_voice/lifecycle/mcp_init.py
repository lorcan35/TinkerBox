"""MCP server bridges init step for `run_startup`.

Wave 23 SOLID-audit follow-up — twenty-eighth sub-extract
(part of the SRP-6 finalisation trio with notes_init and
background_tasks_init).

Owns the boot wiring for Model Context Protocol (MCP) servers:
walks `server._config.mcp_servers` and bridges each one's tools
into the local `_tool_registry` so the LLM can call them
agentically alongside the built-in tools.

Pre-extract this 14-LOC chunk lived inline in `run_startup`
between Notes init and the purge/monitor wiring.  Now lives in
its own module.

## API

```python
await init_mcp_bridges(server)
```

For each MCP server entry in `server._config.mcp_servers` (a
list of dicts with `name`, `url`, optional `token` keys),
calls `bridge_mcp_server` to fetch the remote tool catalog and
register each as a local Tool.

## Failure isolation

The whole chain is wrapped in try/except — a malformed MCP
config, network failure, or missing dep logs at WARNING but
doesn't block boot.  Per-server failures (one MCP down out of
several) are NOT individually isolated in the pre-extract
behaviour; they tear down the whole MCP init.  Preserved
verbatim; a follow-up could narrow the try/except per server.

## getattr fallback

`server._config.mcp_servers` may not exist on older configs;
the `getattr(..., [])` fallback returns an empty list so the
loop is a no-op rather than crashing.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def init_mcp_bridges(server: Any) -> None:
    """Bridge configured MCP servers into the local tool registry.

    Run AFTER `init_agentic_modules` (the registry must exist).
    No-op when `_tool_registry` is None or `mcp_servers` is
    empty / missing from config.

    Failure isolation: any exception in the bridge chain logs
    at WARNING and returns.  Boot continues with whatever tools
    were already registered.
    """
    try:
        from dragon_voice.mcp.bridge import bridge_mcp_server
        mcp_servers = getattr(server._config, "mcp_servers", [])
        for mcp in mcp_servers:
            count = await bridge_mcp_server(
                server._tool_registry,
                name=mcp.get("name", "mcp"),
                url=mcp.get("url"),
                token=mcp.get("token"),
            )
            logger.info("MCP %s: %d tools bridged", mcp.get("name"), count)
    except Exception as e:
        logger.warning("MCP bridge not available: %s", e)
