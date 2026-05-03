"""Pool-aware backend swap helper for `VoicePipeline.swap_backends`.

Wave 23 SOLID-audit follow-up — thirty-second sub-extract.
Fourth slice from `dragon_voice/pipeline.py` (audit SRP-4: the
1613-LOC `VoicePipeline` class still owns 4+ responsibilities;
this dedups the three near-identical STT/TTS/LLM swap blocks
inside `swap_backends`).

Pre-extract `swap_backends` had three almost-line-for-line
repetitions of the pool-aware swap pattern:

```python
if config_changed:
    if old_instance and not is_pooled:
        await old_instance.shutdown()
    new_key = _sig(new_config)
    if pool is not None and new_key in pool:
        new_instance = pool[new_key]
        is_pooled = True
    else:
        new_instance = factory(new_config)
        is_pooled = False
        tasks.append((new_instance, new_key, kind))
```

This module collapses that 11-line block into a single helper
call: `swap_one_backend(...)`.

## API

```python
new_instance, is_pooled, init_task = await swap_one_backend(
    kind="stt",
    config_changed=stt_config_changed,
    old_instance=self._stt,
    old_is_pooled=self._pooled_stt,
    new_signature=_stt_sig(config.stt),
    new_factory=lambda: create_stt(config.stt),
    pool=self._backend_pool,
)
```

Returns three values:
  * `new_instance` — the live backend (may be the old instance
    when `config_changed` is False; may be a pooled reuse;
    may be freshly created).
  * `is_pooled` — updated pooled flag for the caller to stash.
  * `init_task` — None when no init needed (no change OR pooled
    reuse); otherwise a `BackendInitTask` tuple
    `(instance, key, kind)` for the caller to batch with
    `asyncio.gather` + register in the pool post-init.

## Why a function not a class

The swap is a one-shot operation per backend.  No state needs
to persist across calls.  Three free args + a returned
namedtuple is the simplest shape.

## W15-C01 closure preserved

The pool-aware reuse avoids tearing down a backend that's
still serving another connection.  `is_pooled` tracks whether
the current instance came from the pool (in which case
`shutdown` is the pool's job, not the pipeline's).
"""
from __future__ import annotations

import logging
from typing import Any, Callable, NamedTuple, Optional

logger = logging.getLogger(__name__)


class BackendInitTask(NamedTuple):
    """A freshly-created backend that needs `initialize()` called
    + registration in the pool post-init.  Returned from
    `swap_one_backend` when a new instance was created
    (vs. pooled reuse OR no-change).
    """

    instance: Any
    key: Any
    kind: str  # "stt" / "tts" / "llm" — for logging only


class SwapResult(NamedTuple):
    """The three values `swap_one_backend` returns to the caller."""

    new_instance: Any
    is_pooled: bool
    init_task: Optional[BackendInitTask]


async def swap_one_backend(
    *,
    kind: str,                       # "stt" / "tts" / "llm"
    config_changed: bool,
    old_instance: Any,
    old_is_pooled: bool,
    new_signature: Any,              # backend-pool key (typically a tuple)
    new_factory: Callable[[], Any],  # zero-arg constructor
    pool: Optional[dict],
) -> SwapResult:
    """Swap one backend (STT / TTS / LLM) according to the
    pool-aware reuse policy.

    Behaviour matrix:

      * `config_changed=False` → returns the old instance
        unchanged (no shutdown, no pool lookup, no init task).
      * `config_changed=True` AND old instance not pooled →
        old instance shut down first.
      * `config_changed=True` AND `pool` has the new signature →
        reuses the pooled instance, marks `is_pooled=True`,
        no init task (the pool guarantees the instance is
        already initialised).
      * `config_changed=True` AND new signature not in pool →
        creates via `new_factory`, marks `is_pooled=False`,
        returns an init task for the caller to batch.

    Args:
        kind: "stt" / "tts" / "llm" — used for the swap-log
            line only.
        config_changed: True iff the new config differs from the
            old one in a way that requires a swap.
        old_instance: The currently-installed backend (or None).
        old_is_pooled: True iff `old_instance` came from the
            backend pool (skips shutdown on swap).
        new_signature: Pool key for the new config (caller
            computes via `_stt_sig` / `_tts_sig` / `_llm_sig`).
        new_factory: Zero-arg callable returning a freshly-
            constructed backend (caller wraps `create_stt(cfg)`).
        pool: Backend pool (dict) or None (pool disabled).

    Returns:
        SwapResult(new_instance, is_pooled, init_task).
    """
    if not config_changed:
        return SwapResult(
            new_instance=old_instance,
            is_pooled=old_is_pooled,
            init_task=None,
        )

    # Shutdown the old instance ONLY if we own it (not pooled).
    # Pooled instances are the pool's responsibility — tearing
    # them down here would break other connections sharing the
    # same backend.
    if old_instance is not None and not old_is_pooled:
        await old_instance.shutdown()

    # Try the pool first.
    if pool is not None and new_signature in pool:
        return SwapResult(
            new_instance=pool[new_signature],
            is_pooled=True,
            init_task=None,
        )

    # Cold path: construct + return an init task for the caller
    # to batch.  The caller registers the new instance in the
    # pool AFTER initialize() succeeds (so a failed init doesn't
    # leave a half-constructed backend in the pool).
    new_instance = new_factory()
    return SwapResult(
        new_instance=new_instance,
        is_pooled=False,
        init_task=BackendInitTask(
            instance=new_instance,
            key=new_signature,
            kind=kind,
        ),
    )
