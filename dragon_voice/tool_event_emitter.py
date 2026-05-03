"""Per-connection tool-event emitter (β-arch + γ2-M1 + audit B4).

Wave 23 SOLID-audit follow-up — fifth (and biggest) sub-handler
extract from `_handle_register` (round 3, after stale_conn_eviction
#222, device_upsert #223, session_handshake #224, surface_register
#225).

Pre-extract, three async closures (`_on_tool_call`,
`_on_tool_result`, `_on_tool_error`) lived inline at server.py:1119-
1294 (~176 LOC) capturing per-connection state via Python closure
semantics.  They each combined β-arch pair-emit logic, per-turn
tracker bookkeeping, transport-close swallow, and (for tool_result)
the v4·D Phase 4c web_search auto-widget emission.

This module turns the three closures into a single stateful class
`ToolEventEmitter` that's constructed at register time with the
per-connection state, and exposes the three callbacks as methods.

## API

```python
emitter = ToolEventEmitter(
    ws=ws,
    conn_state=conn_state,
    session_id=session_id,
    safe_send_json=self._safe_send_json,
    emit_legacy=bool(getattr(conn_state.get("config"),
                              "progress_bus_emit_legacy", True)),
)
conn_state["on_tool_call"]   = emitter.on_tool_call
conn_state["on_tool_result"] = emitter.on_tool_result
conn_state["on_tool_error"]  = emitter.on_tool_error
```

After this PR, `_handle_register`'s tool-callback wiring shrinks
from ~176 LOC of inline closures to ~10 LOC of `if self._tool_registry:
emitter = ToolEventEmitter(...); conn_state["on_tool_*"] = emitter.on_tool_*`.

## Why a class, not three free functions

The three callbacks share state — the `ws`, `conn_state` ref,
`session_id`, `safe_send_json`, and `emit_legacy` flag.  Pre-extract
they were all closure variables.  Two equivalent options:

  1. **Three free functions** taking all state as keyword args.
     Verbose call sites; every call has to pass 5 positional args.
  2. **Class** capturing state in `__init__`; methods take only the
     event-specific arg.  Matches how the original closures
     captured state via Python's lexical scoping.

(2) is cleaner and matches existing patterns in
`dragon_voice/handlers/` where stateful HTTP handlers are class-based.

## DIP

`ws`, `conn_state`, `safe_send_json` are passed in.  No
VoiceServer reach-through.  The constructor records them; methods
operate on the captured refs.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiohttp import web

from dragon_voice.errors import Scope, Severity, error_event
from dragon_voice.progress import Phase, Stage
from dragon_voice.progress_emit import emit_progress_pair

logger = logging.getLogger(__name__)


SafeSendJson = Callable[[web.WebSocketResponse, dict], Awaitable[bool]]


class ToolEventEmitter:
    """Owns the per-connection tool-event lifecycle.

    Three async methods (`on_tool_call`, `on_tool_result`,
    `on_tool_error`) implement the contract that
    ConvEngine/ToolRegistry expect.  Each emits the β-arch
    pair-frame (legacy + progress.tool.*) via
    `emit_progress_pair` AND keeps the per-turn `tool_calls_this_turn`
    tracker in sync so the wrap synthesiser can see the
    call+args+result triple.

    `on_tool_result` additionally auto-emits a `widget_list`
    frame for `web_search` results so the Tab5 home live-slot
    surfaces the top hits without the LLM having to orchestrate
    a widget call itself (v4·D Phase 4c).
    """

    def __init__(
        self,
        *,
        ws: web.WebSocketResponse,
        conn_state: Any,                 # ConnState or dict
        session_id: str,
        safe_send_json: SafeSendJson,
        emit_legacy: bool = True,
    ) -> None:
        self._ws = ws
        self._conn_state = conn_state
        self._session_id = session_id
        self._safe_send_json = safe_send_json
        self._emit_legacy = emit_legacy

    # ── emit_progress_pair takes a Callable[[dict], Awaitable[None]]
    #    that returns None (it awaits but discards).  Our
    #    safe_send_json returns bool — wrap to match the signature.
    async def _emit_via_ws(self, ev: dict) -> None:
        await self._safe_send_json(self._ws, ev)

    async def on_tool_call(self, call: dict) -> None:
        """Called when ConvEngine fires a tool call.

        Pre-registers the call+args in `conn_state["tool_calls_this_turn"]`
        so `on_tool_result` can merge the result into the same record
        (the wrap synthesiser reads both sides — e.g. `remember`
        needs the `fact` from args to write "Got it — {fact}.").
        """
        # #75 phase 1b: pre-register the call + args in the per-turn
        # tracker.
        try:
            self._conn_state.setdefault("tool_calls_this_turn", []).append({
                "tool": call.get("tool"),
                "args": call.get("args") or {},
            })
        except Exception:
            logger.debug("tool_calls_this_turn pre-register suppressed", exc_info=True)

        # Wave 12 — agent_log recording happens at the
        # ToolRegistry.execute chokepoint (tools/registry.py) so it
        # captures every invocation regardless of caller (WS, REST,
        # dashboard).  No need to record here.
        if self._ws.closed:
            return

        # β-arch (issue #123): pair-emit — legacy `tool_call` for
        # unmodified Tab5 + new progress.tool.start with the same
        # payload nested.
        await emit_progress_pair(
            self._emit_via_ws,
            legacy={
                "type": "tool_call",
                "tool": call["tool"],
                "args": call["args"],
            },
            phase=Phase.TOOL,
            stage=Stage.START,
            payload={"tool": call["tool"], "args": call["args"]},
            emit_legacy=self._emit_legacy,
        )

    async def on_tool_result(self, result: dict) -> None:
        """Called when a tool finishes executing.

        Three things happen, in order:
          1. β-arch pair-emit of the result frame.
          2. Merge the result into the matching pre-registered call
             in the per-turn tracker (or append bare if no pre-reg).
          3. Auto-emit a `widget_list` frame for `web_search` hits
             (v4·D Phase 4c).
        """
        if self._ws.closed:
            return

        # β-arch (issue #123): pair-emit — legacy `tool_result`
        # (with all result fields spread at top level) + new
        # progress.tool.done with the same fields nested in payload
        # for the unified bus.
        await emit_progress_pair(
            self._emit_via_ws,
            legacy={"type": "tool_result", **result},
            phase=Phase.TOOL,
            stage=Stage.DONE,
            payload={
                "tool": result.get("tool"),
                "result": result.get("result"),
                "execution_ms": result.get("execution_ms"),
            },
            emit_legacy=self._emit_legacy,
        )

        # #75 phase 1b: merge result into the most-recent
        # pre-registered call for this tool name (fills the FIRST
        # pending slot so same-tool-twice-in-one-turn still maps
        # 1:1).  If no pre-register exists (some code paths emit
        # tool_result only), append the bare result so the wrap
        # still has something to describe.
        try:
            tracker = self._conn_state.setdefault("tool_calls_this_turn", [])
            merged = False
            for rec in tracker:
                if rec.get("tool") == result.get("tool") and "result" not in rec:
                    rec["result"] = result.get("result")
                    rec["execution_ms"] = result.get("execution_ms")
                    merged = True
                    break
            if not merged:
                tracker.append(result)
        except Exception:
            logger.debug("tool_calls_this_turn merge suppressed", exc_info=True)

        # Wave 12 — agent_log close happens at the
        # ToolRegistry.execute chokepoint, not here.

        # v4·D Phase 4c: auto-emit widget_list for web_search
        # results so the Tab5 home live-slot surfaces the top hits
        # without the LLM having to orchestrate a widget call itself.
        try:
            if result.get("tool") == "web_search":
                await self._emit_web_search_widget_list(result)
        except Exception:
            logger.debug("widget_list auto-emit failed", exc_info=True)

    async def _emit_web_search_widget_list(self, result: dict) -> None:
        """Convert a `web_search` tool result into a `widget_list`
        WS frame for the Tab5 home live-slot.

        Picks the top 5 hits, truncates titles to 79 chars, and
        emits via `ws.send_json` directly (NOT through
        safe_send_json) — matches pre-extract behaviour where
        this auto-emission was best-effort and any send failure
        was caught by the outer try/except.
        """
        payload = result.get("result") or {}
        hits = payload.get("results") or []
        query = payload.get("query", "")
        items = []
        for r in hits[:5]:
            t = str(r.get("title") or r.get("snippet") or "")[:79]
            if not t:
                continue
            items.append({"text": t, "value": ""})
        if not items:
            return
        await self._ws.send_json({
            "type": "widget_list",
            "skill_id": "web_search",
            "card_id": f"ws_{self._session_id[:8]}",
            "title": (query[:60] or "Web results"),
            "tone": "info",
            "priority": 70,
            "items": items,
        })

    async def on_tool_error(self, err: dict) -> None:
        """γ2-M1 (issue #104): emit a structured tool error frame
        when the parser swallows malformed JSON args.

        Pre-fix the failure was a silent `logger.warning` — the
        LLM continued without firing the tool and the user saw an
        empty/generic reply with zero signal that anything was
        attempted.  Now we surface a TRANSIENT error in the TOOL
        scope so Tab5 (γ2-H8) can render a non-blocking toast.

        Audit B4 (#137): the err dict's `code` and `message`
        fields are honoured so ConvEngine can signal e.g.
        `tool_call_limit_reached` distinct from the original
        `tool_args_invalid` parse failure.  Default codes preserve
        back-compat with callers that pre-date B4.

        The raw args are deliberately NOT included in the user-
        facing message — they may contain prompt-injection content
        from the LLM and Tab5's caption isn't a safe place to
        render arbitrary text.  Server log already carries the
        full failure for ops debugging.
        """
        if self._ws.closed:
            return

        tool_name = err.get("name") or "(unknown)"
        code = err.get("code") or "tool_args_invalid"
        message = err.get("message") or (
            f"Tool '{tool_name}' had invalid arguments — skipped."
        )

        # β-arch (issue #123): pair-emit — legacy γ1 error frame
        # (already structured per #102) + new progress.tool.error
        # frame for the unified bus.  Both carry the same
        # code/message/severity/scope so γ2-H8 routing applies
        # regardless of which frame Tab5 reads.
        await emit_progress_pair(
            self._emit_via_ws,
            legacy=error_event(
                code=code,
                message=message,
                severity=Severity.TRANSIENT,
                scope=Scope.TOOL,
            ),
            phase=Phase.TOOL,
            stage=Stage.ERROR,
            code=code,
            message=message,
            severity=Severity.TRANSIENT,
            scope=Scope.TOOL,
            emit_legacy=self._emit_legacy,
        )
