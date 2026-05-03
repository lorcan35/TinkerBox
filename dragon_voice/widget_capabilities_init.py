"""Widget-capability initialisation for the register flow.

Wave 23 SOLID-audit follow-up — sixteenth sub-extract from
the WS-handler family in server.py (round 4 spillover, after
the fifteen prior extracts #227-#241).

Tab5's `register` frame includes a `capabilities.widgets` block
declaring what the device's screen + memory budget can render
(types, list-item caps, chart-point caps, prompt-choice caps).
Skills query this via `SurfaceManager` to downgrade their
emissions for low-end clients (smaller lists, lower-res media).

When Tab5 omits the block (legacy firmware) we fall back to
conservative defaults that any TinkerTab firmware build can
render without overrun.

Pre-extract this 14-LOC chunk lived inline in `_handle_register`
between the device upsert and the session lookup.  Now lives in
its own dedicated module.

## API

```python
init_widget_capabilities(conn_state, capabilities, device_id)
```

Mutates `conn_state["widget_capabilities"]` to either the
client-supplied block or the default fallback.

## Why a separate module

The widget-capability surface is its own axis of change — Tab5
firmware adds new widget types and the default fallback list
needs to grow accordingly.  Keeping it in `server.py` meant
every widget-cap update touched the WS-handler family.

The default-fallback shape is referenced by both `register`
flow and `surfaces/manager.py` — having it as a module-level
constant means the two stay in sync mechanically.
"""
from __future__ import annotations

import copy
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


# Conservative widget-capability defaults — what any TinkerTab
# firmware build can render without overrun.  Used when the
# `register` frame omits `capabilities.widgets` (legacy clients).
#
# Adding a new widget type / cap here propagates to every code
# path that consults `conn_state["widget_capabilities"]` —
# SurfaceManager, skill renderers, the widget event emitters.
_DEFAULT_WIDGET_CAPABILITIES: dict[str, Any] = {
    "types": ["live", "card"],
    "list_max_items": 3,
    "chart_max_points": 8,
    "prompt_max_choices": 2,
}


def init_widget_capabilities(
    conn_state: dict,
    *,
    capabilities: Optional[dict],
    device_id: str,
) -> None:
    """Pluck `capabilities.widgets` from the register frame and
    stash it on conn_state for skill queries.  Falls back to
    `_DEFAULT_WIDGET_CAPABILITIES` when the field is missing
    (legacy clients).

    The defaults are conservative: every TinkerTab firmware
    build (including pre-Wave-12) renders them without overrun.

    Logged at INFO so register-time triage can see what the
    device claimed it could render — useful when a skill
    misbehaves in production and we're trying to figure out
    whether the device caps were the limiter.
    """
    widget_caps = (
        capabilities.get("widgets")
        if isinstance(capabilities, dict)
        else None
    )
    # Deep-copy the default so two connections sharing the
    # fallback can't mutate each other's caps via the shared
    # `types` list reference.
    conn_state["widget_capabilities"] = (
        widget_caps or copy.deepcopy(_DEFAULT_WIDGET_CAPABILITIES)
    )
    logger.info(
        "widget_capabilities for %s: %s",
        device_id, conn_state["widget_capabilities"],
    )
