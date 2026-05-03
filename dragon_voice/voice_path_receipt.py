"""Per-turn `receipt` emission for the voice-path STT → LLM → TTS chain.

Wave 23 SOLID-audit follow-up — fortieth sub-extract.  Closes
audit SRP-10 (receipt emission was scattered across 3 inline
sites in `pipeline.py` plus the text-path equivalent
`text_path_receipt.py` from PR #229).

Three receipt frames fire over a voice-path turn:

  * **STT receipt** (audit F4, 2026-04-20) — after `stt`
    transcript event lands.  Stamps which STT backend ran +
    how long it took.  cost_mils=0 today (local Moonshine
    + cloud STT-cost-per-second TBD).
  * **LLM receipt** (Phase 3 per-turn) — after `llm_done`.
    Pulls token counts + cost from `SupportsUsage` backends
    (OpenRouter); falls back to a minimal receipt with
    cost_mils=0 when the usage path fails.  Includes the
    Gauntlet G2 retry surface fields.
  * **TTS receipt** (audit F5, 2026-04-20) — after `tts_end`.
    Stamps speech backend + accumulated tts_ms.

This module owns all three so future receipt-shape changes
(adding new fields, switching to cents-based pricing, etc.)
happen in one place.

## API

```python
await emit_voice_path_stt_receipt(
    on_event,
    stt_backend=conn_config.stt.backend,
    stt_ms=stt_ms,
)

await emit_voice_path_llm_receipt(
    on_event,
    llm=self._llm,
    llm_ms=llm_ms,
)

await emit_voice_path_tts_receipt(
    on_event,
    tts_backend=conn_config.tts.backend,
    tts_total_ms=self._tts_total_ms,
)
```

All three are failure-isolated: emit exceptions logged at
DEBUG (the receipt is informational, not session-correctness)
and never propagate.

## v4·D audit P0 fallback (LLM receipt)

The LLM receipt has a fallback path: if the usage-based emit
fails (corrupt usage dict, pricing-table miss), emit a MINIMAL
receipt with cost_mils=0 so the chat bubble still gets a
stamp + the day-budget accumulator increments by 0
(harmless but consistent).  Pre-extract this fallback was
the difference between "missing receipt → no bubble stamp"
and "stamped 0 → bubble shows".

## SupportsUsage gate (Wave 21b #204)

`isinstance(SupportsUsage)` over `hasattr` — backends like
`dual` or `tinkerclaw` that don't implement the protocol but
expose `get_last_usage` via `__getattr__` accidentally would
have silently emitted `model='llm'` and skewed cost
attribution.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


OnEvent = Callable[[dict], Awaitable[None]]


async def emit_voice_path_stt_receipt(
    on_event: OnEvent,
    *,
    stt_backend: str,
    stt_ms: float,
) -> None:
    """Emit the per-turn STT receipt.  Audit F4 (2026-04-20):
    Tab5's per-turn transparency + budget tracker can see which
    STT backend ran and how long it took.

    cost_mils=0 today.  Local Moonshine is free; OpenRouter STT
    cost would need per-audio-second pricing which the STT
    class doesn't currently expose — stub at 0 and let the
    cloud-STT path surface its own charge later.

    Failure isolation: emit exceptions logged at DEBUG.
    """
    try:
        await on_event({
            "type": "receipt",
            "stage": "stt",
            "model": stt_backend or "stt",
            "stt_ms": round(stt_ms),
            "cost_mils": 0,
        })
    except Exception as e:
        logger.debug("STT receipt emit failed: %s", e)


async def emit_voice_path_llm_receipt(
    on_event: OnEvent,
    *,
    llm: Any,
    llm_ms: float,
) -> None:
    """Emit the per-turn LLM receipt for the voice path.

    Two-path emit:

      1. **Usage-based** (Wave 21b #204 — only fires when the
         backend implements ``SupportsUsage`` AND has a non-
         zero ``total_tokens`` in last_usage).  Computes cost
         via ``price_for_model`` and surfaces token counts +
         retry-status fields (Gauntlet G2).

      2. **Fallback** (v4·D audit P0) — fires when the usage-
         based path fails.  Minimal receipt with cost_mils=0
         + zero token counts so the chat bubble still gets a
         stamp.  retry_reason field encodes the fallback
         reason as ``"receipt-fallback: <ExcClass>"`` for ops
         triage.

    Both paths swallow exceptions at the outer level so a
    receipt failure can't break the turn.
    """
    try:
        # Wave 21b (#204): isinstance(SupportsUsage) over hasattr.
        from dragon_voice.llm.base import SupportsUsage
        if not isinstance(llm, SupportsUsage):
            return

        usage = llm.get_last_usage()
        if not usage or not usage.get("total_tokens"):
            return

        from dragon_voice.llm.openrouter_llm import price_for_model
        cost_mils = price_for_model(
            usage["model"],
            usage.get("prompt_tokens", 0),
            usage.get("completion_tokens", 0),
        )
        await on_event({
            "type": "receipt",
            "stage": "llm",
            "model": usage["model"],
            "prompt_tokens":     usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "total_tokens":      usage.get("total_tokens", 0),
            "cost_mils":         cost_mils,
            "llm_ms":            round(llm_ms),
            # v4·D Gauntlet G2: surface retries so the chat
            # bubble can stamp a "retried" chip instead of
            # silently presenting a possibly-degraded reply.
            "retried":           bool(usage.get("retried", False)),
            "retry_reason":      usage.get("retry_reason", ""),
        })
    except Exception as e:
        # v4·D audit P0 fallback: emit a minimal receipt so the
        # chat bubble still gets a stamp + the day-budget
        # accumulator stays consistent (increments by 0).
        logger.warning("Receipt emit failed: %s -- emitting fallback", e)
        try:
            fallback_model = getattr(llm, "name", "") or "llm"
            await on_event({
                "type": "receipt",
                "stage": "llm",
                "model": fallback_model,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "cost_mils": 0,
                "llm_ms": round(llm_ms) if isinstance(llm_ms, (int, float)) else 0,
                "retried": False,
                "retry_reason": "receipt-fallback: " + type(e).__name__,
            })
        except Exception:
            logger.debug("fallback receipt also failed", exc_info=True)


async def emit_voice_path_tts_receipt(
    on_event: OnEvent,
    *,
    tts_backend: str,
    tts_total_ms: float,
) -> None:
    """Emit the per-turn TTS receipt.  Audit F5 (2026-04-20):
    per-turn chat bubbles stamp the speech backend + time.

    No-op when ``tts_total_ms <= 0`` (no audio actually
    synthesised this turn — silence reply or cancelled).

    cost_mils=0 today.  Local Piper is free; OpenRouter TTS
    cost left at 0 (same rationale as the STT receipt).
    """
    if tts_total_ms <= 0:
        return
    try:
        await on_event({
            "type": "receipt",
            "stage": "tts",
            "model": tts_backend or "tts",
            "tts_ms": round(tts_total_ms),
            "cost_mils": 0,
        })
    except Exception as e:
        logger.debug("TTS receipt emit failed: %s", e)
