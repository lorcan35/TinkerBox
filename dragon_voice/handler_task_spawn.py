"""Per-connection command-handler task spawning.

Wave 23 SOLID-audit follow-up — nineteenth sub-extract from
the WS-handler family in server.py (round 4 spillover, after
the eighteen prior extracts #227-#244).

Phase 1 of the UX-gap remediation (issue #91, see
docs/UX-GAPS.md): the text/media/config handlers used to be
awaited inline in the WS read loop, blocking it from receiving
cancel/ping/voice frames for the full duration of the handler.

This module provides the helper that detaches each handler as
a tracked asyncio.Task in
`conn_state["handler_tasks"][slot]` so:
  * The cancel handler can selectively kill any of them
    (cancel_handler.py from PR #237).
  * `_handle_disconnect` can clean them up on WS close
    (disconnect_handler.py from PR #244).

Pre-extract this 80-LOC helper lived inline as
`VoiceServer._spawn_handler_task`.  Now lives in its own
dedicated module.

## API

```python
await spawn_handler_task(
    conn_state,
    slot,
    coro_func, *args,
    conn_lock=None,
    coalesce=False,
)
```

Spawns `coro_func(*args)` as a tracked task in
`conn_state["handler_tasks"][slot]`.

## Coalesce vs. queue semantics

  * `coalesce=False` (default): wait for any in-flight task in
    the same slot to finish before spawning the new one.
    Matches Tab5's "+1 QUEUED" stash semantics for text input —
    a second text frame while the first is mid-LLM-stream
    queues behind it.
  * `coalesce=True`: cancel the in-flight task and replace.
    Matches user intent for `config_update` mode toggles —
    rapid-fire mode swaps should land the LAST one, not stack
    them all up.

## Why we swallow exceptions in `_run`

A handler that raises wouldn't be caught by the outer
`async for msg in ws` loop — that catch is for transport-level
errors, not handler-level ones.  Propagating would tear down
the WS read loop.  The handler's own try/except already
logged the failure; we re-log here at EXCEPTION for
discoverability and let the task transition to FINISHED
naturally.

`CancelledError` is re-raised so the task transitions to
CANCELLED (rather than FINISHED) — this matters for the
cancel_handler's done() check, which uses cancelled() vs.
done() distinctions.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


CoroFunc = Callable[..., Awaitable[Any]]


async def spawn_handler_task(
    conn_state: dict,
    slot: str,
    coro_func: CoroFunc,
    *args: Any,
    conn_lock: Optional[asyncio.Lock] = None,
    coalesce: bool = False,
) -> None:
    """Spawn `coro_func(*args)` as a tracked per-connection task
    in `conn_state["handler_tasks"][slot]`.

    Coalesce semantics:
      * `coalesce=False` (default) — wait for any in-flight
        task in the same slot to finish before spawning the
        new one.  Preserves text-turn ordering.
      * `coalesce=True` — cancel the in-flight task and replace.
        Used for config_update mode toggles where last-write
        wins.

    Conn lock:
      * When `conn_lock` is provided, the spawned task acquires
        it before invoking the handler — preserves US-P10
        serialisation with the voice path so two text turns
        can't interleave LLM-state mutations.

    Failure isolation in the spawned task:
      * `CancelledError` re-raised so the task transitions to
        CANCELLED (cancel_handler distinguishes cancelled() from
        done()).
      * Other exceptions logged at EXCEPTION but NOT re-raised
        — propagating would tear down the WS read loop.
    """
    handler_tasks = conn_state.setdefault("handler_tasks", {})
    prev = handler_tasks.get(slot)
    if prev and not prev.done():
        if coalesce:
            prev.cancel()
            try:
                await prev
            except (asyncio.CancelledError, Exception):
                # Cancellation may surface as the underlying
                # handler's exception; suppressed because we're
                # about to replace it anyway.
                pass
        else:
            # Wait for the previous handler to finish before
            # spawning the new one (preserves ordering for
            # text turns).
            try:
                await prev
            except (asyncio.CancelledError, Exception):
                pass

    async def _run() -> None:
        try:
            if conn_lock is not None:
                async with conn_lock:
                    await coro_func(*args)
            else:
                await coro_func(*args)
        except asyncio.CancelledError:
            # Re-raise so the task transitions to CANCELLED state.
            # Resource cleanup is the handler's own responsibility;
            # the cancel cmd path already killed any pipeline TTS
            # subproc.
            raise
        except Exception:
            # Handler raised — already logged inside the
            # handler's own try/except.  Don't propagate to the
            # WS loop or the loop dies on us; the WS catch at
            # the outer `async for msg in ws` is for transport-
            # level errors, not handler-level ones.
            logger.exception(
                "handler_tasks[%s] raised — task will exit",
                slot,
            )

    task = asyncio.create_task(_run(), name=f"ws_handler:{slot}")
    handler_tasks[slot] = task
