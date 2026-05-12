"""Tab5 `start` WS command handler.

Wave 6-A of the cross-stack cohesion audit (2026-05-11).  Extracts
the inline `start` branch from `_handle_ws_voice` into a focused
module so the dispatch table stays a flat one-liner per command —
matching the existing pattern (`stop_handler`, `cancel_handler`,
`clear_handler`, `widget_action_handler`).

## What this owns

When Tab5 sends `{"type":"start", "mode":"ask"|"dictate", "turn_id":"..."}`:

  1. Read `mode` (default `"ask"`).
  2. Stash on `conn_state["mode"]` so segment/stop branches dispatch
     correctly.
  3. Stash `turn_id` (W4-B) on `conn_state["turn_id"]` so emit echoes
     (W4-C) + log lines tag the turn.
  4. Clear the pipeline's audio buffer so the new utterance doesn't
     blend with whatever was queued from a prior turn.
  5. Flip `_dictation_mode` flag on the pipeline.
  6. In dictate mode, also reset the segment buffers.
  7. Log a single info line summarising the action.

The pipeline reference is read off `conn_state["pipeline"]`.  When
the pipeline is missing (register hasn't completed yet, or a
race during teardown), the whole handler is a no-op — same as the
inline behaviour before extraction.

## Failure isolation

Everything inside is straight attribute access + dict writes; no
network, no DB.  If pipeline state shape changes in a refactor, the
existing `getattr`-style soft access keeps the handler from
exploding on missing fields.

## API

```python
await handle_start_command(ws_id, conn_state, cmd)
```

No return value.  Logs the outcome.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def handle_start_command(
    ws_id: str,
    conn_state: Any,        # ConnState or dict
    cmd: dict,
) -> None:
    """See module docstring."""
    pipeline = conn_state.get("pipeline")
    if not pipeline:
        return

    mode = cmd.get("mode", "ask")
    conn_state["mode"] = mode

    # W4-B (cross-stack audit 2026-05-11): Tab5 stamps a 12-hex
    # turn_id on every start/text frame so a turn's Tab5 obs events
    # correlate with Dragon's log lines.  Store on conn_state for
    # downstream emit echo + log alongside session_id.
    turn_id = cmd.get("turn_id") or "-"
    conn_state["turn_id"] = turn_id

    pipeline._audio_buffer.clear()
    pipeline._dictation_mode = (mode == "dictate")
    if mode == "dictate":
        pipeline._segment_buffer.clear()
        pipeline._dictation_segments.clear()

    logger.info(
        "Connection %s: start (mode=%s, turn_id=%s, audio buffer cleared)",
        ws_id, mode, turn_id,
    )
