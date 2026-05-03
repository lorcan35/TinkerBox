"""Budget-cap-downgrade speak-system alert.

Wave 23 SOLID-audit follow-up (sibling of vision_capability extract):
splits the third sub-responsibility out of `_handle_config_update`.
After this PR, the bottom of `_handle_config_update` is a thin
coordinator that calls three focused functions:

  * config swap (still inline — biggest extract candidate next)
  * `vision_capability.emit_vision_capability` (PR #214)
  * `cap_downgrade.maybe_speak_cap_downgrade_alert` (this module)

## What this does

When Tab5 sends a `config_update` whose `reason` field is
`"cap_downgrade"` (i.e. the daily budget cap was hit and Tab5
auto-flipped voice mode back to LOCAL), Dragon speaks a short
TTS alert via the active pipeline so the user hears the change
even with the screen off.

The `pipeline.speak_system(...)` call returns an awaitable; we
spawn it as a tracked background task so it doesn't block the
config_update flow.  Tracking lives in `conn_state["bg_tasks"]`
(or `conn_state.bg_tasks`) so `_handle_disconnect` can cancel
the alert if the user closes the WS mid-utterance (Wave 14
W14-C06).

## Failure isolation

The whole call is best-effort.  If anything raises (pipeline
gone, speak_system not implemented on the active pipeline class,
TTS backend down, asyncio task creation fails), the failure is
logged and swallowed.  A failed alert must never tear down the
config_update flow itself.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

# The ack message Dragon speaks when Tab5 reports a cap_downgrade.
# Centralised here so the wording is in one place + can be A/B-tested
# or i18n'd in a follow-up without grepping the WS dispatcher.
_CAP_DOWNGRADE_ALERT_TEXT = "Daily budget cap reached. Switched back to local mode."


def maybe_speak_cap_downgrade_alert(cmd: dict, conn_state: Any) -> None:
    """If the config_update reports a cap_downgrade, spawn a tracked
    background TTS task that speaks the cap-hit alert.

    No-op when:
      * `cmd["reason"]` is not `"cap_downgrade"`
      * the connection has no pipeline yet (boot race)
      * the active pipeline class doesn't expose `speak_system`
        (e.g. tests with a stub pipeline)

    Args:
        cmd: The parsed `config_update` WS frame.
        conn_state: ConnState (or compat dict) for the connection.
            Must expose `.get("pipeline")` and `["bg_tasks"]` —
            both are satisfied by ConnState's dict-protocol shim
            and by plain dicts used in tests.

    Returns:
        None.  All work is fire-and-forget; the spawned task is
        discoverable via `conn_state["bg_tasks"]` for later
        cancellation by `_handle_disconnect`.
    """
    try:
        if cmd.get("reason") != "cap_downgrade":
            return
        pipeline = conn_state.get("pipeline")
        if not pipeline or not hasattr(pipeline, "speak_system"):
            return
        # Wave 14 W14-C06: track the task so _handle_disconnect can
        # cancel it if the user closes mid-utterance.
        bg = conn_state["bg_tasks"]
        t = asyncio.create_task(pipeline.speak_system(_CAP_DOWNGRADE_ALERT_TEXT))
        bg.add(t)
        t.add_done_callback(bg.discard)
    except Exception:
        logger.exception("cap_downgrade alert failed")
