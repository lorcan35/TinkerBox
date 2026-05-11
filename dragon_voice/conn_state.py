"""Per-WS-connection state container.

2026-05-03 SOLID audit (DIP-1, "ConnState dataclass"): server.py held
22 ``conn_state["..."]`` untyped dict accesses scattered across the
WS handler family.  Adding a field meant grepping for the new key;
typos returned ``None`` silently; the IDE could neither autocomplete
nor type-check anything.

This module introduces a typed :class:`ConnState` dataclass that
collects the full surface area in one place with explicit field
types + sensible defaults.

## Backward-compat dict protocol

ConnState implements the read+write subset of the mutable mapping
protocol (``__getitem__`` / ``__setitem__`` / ``__contains__`` /
``.get`` / ``.setdefault``).  This lets the existing code keep its
``state.get("pipeline")`` / ``state["session_id"] = ...`` patterns
unchanged during migration — the dict-style accesses route to the
underlying dataclass fields transparently.

External callers (``handlers/``, ``lifecycle/``, tests with plain-
dict mocks) continue to work without any code change.  Internal
server.py call sites can migrate to ``state.pipeline`` /
``state.session_id`` etc. for the typed surface incrementally
without forcing a big-bang refactor.

## Lifecycle

Instantiation happens at the top of :meth:`VoiceServer._handle_ws_voice`
right after the per-connection lock + config-deepcopy are minted.
Teardown is implicit — the ``_active_connections.pop(ws_id)`` call
in :meth:`_handle_disconnect` drops the only reference and Python
garbage-collects the instance.

## Design notes

* Field types use ``Any`` for ``ws`` / ``pipeline`` / ``config`` /
  ``conversation`` to dodge circular imports — ConnState lives at
  the dependency leaf, importing from VoiceServer would cycle.
* The ``_on_audio`` / ``_on_event`` underscored fields preserve the
  exact key names the existing ``conn_state["_on_audio"] = ...``
  writes used.  No semantic change.
* Future type-tightening: replace ``Any`` with ``"VoicePipeline" | None``
  etc. once the type-only imports can be threaded via
  ``TYPE_CHECKING``.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, fields
from typing import Any, Callable, Optional


@dataclass
class ConnState:
    """Per-WebSocket connection state — typed, with dict-protocol
    backward compat.  See module docstring for the migration story.
    """

    # ── Identity (set at instantiation) ───────────────────────────
    ws_id: str
    ws: Any                    # aiohttp.web.WebSocketResponse
    conn_lock: asyncio.Lock    # A06: serializes config_update vs text_handler
    config: Any                # VoiceConfig (per-connection deep copy)

    # ── Set after WS register ─────────────────────────────────────
    pipeline: Optional[Any] = None         # VoicePipeline
    session_id: Optional[str] = None
    device_id: Optional[str] = None
    registered: bool = False
    mode: str = "ask"                       # "ask" or "dictate"
    response_mode: str = "always_speak"     # "always_speak" | "text_only"
    voice_mode: int = 0                     # 0..4 — see VoiceMode
    widget_capabilities: dict = field(default_factory=dict)

    # ── Callbacks (stored for pipeline re-init under A04 monitor) ─
    # Underscored names match the historical dict-key spellings so
    # the dict-compat shim doesn't need a translation table.
    _on_audio: Optional[Callable] = None
    _on_event: Optional[Callable] = None
    on_tool_call: Optional[Callable] = None
    on_tool_result: Optional[Callable] = None
    on_tool_error: Optional[Callable] = None

    # ── Optional per-connection ConversationEngine override ───────
    # Most connections share self._conversation; some test scenarios
    # inject a per-conn instance via this field.
    conversation: Optional[Any] = None

    # ── Per-turn ephemeral state ──────────────────────────────────
    tool_calls_this_turn: list = field(default_factory=list)
    # W4-B (cross-stack audit 2026-05-11): Tab5 stamps a 12-hex
    # turn_id on each `start` / `text` frame.  Stored here so
    # downstream emits + log lines can echo back, enabling cross-
    # system trace correlation with Tab5 obs events.  Default "-"
    # before the first turn (and for pre-W4-A firmwares).
    turn_id: str = "-"

    # ── Background work tracking ──────────────────────────────────
    bg_tasks: set = field(default_factory=set)
    handler_tasks: dict = field(default_factory=dict)

    # ── Rate-limit cursor (epoch seconds) ─────────────────────────
    _last_config_update_ts: float = 0.0

    # ── Dict-compat shim ──────────────────────────────────────────
    # Allows the existing `state.get("key")` / `state["key"]` /
    # `state["key"] = val` / `state.setdefault("key", default)` call
    # sites to keep working unchanged during the incremental
    # migration to typed `.field` access.

    def __getitem__(self, key: str) -> Any:
        try:
            return getattr(self, key)
        except AttributeError as e:
            raise KeyError(key) from e

    def __setitem__(self, key: str, value: Any) -> None:
        if not hasattr(self, key):
            raise KeyError(
                f"ConnState has no field '{key}' — add it to the dataclass "
                f"before assigning.  Catches typos that the dict-era code "
                f"would have silently accepted."
            )
        setattr(self, key, value)

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and hasattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def setdefault(self, key: str, default: Any) -> Any:
        """Match dict.setdefault — return existing value, else set
        to default and return that."""
        cur = getattr(self, key, None)
        if cur is None:
            self[key] = default
            return default
        return cur

    def keys(self) -> list[str]:
        """Field names — for `dict(state)` round-trip in tests."""
        return [f.name for f in fields(self)]
