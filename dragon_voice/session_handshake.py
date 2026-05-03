"""Session-handshake WS messages — `session_start` + `session_messages`.

Wave 23 SOLID-audit follow-up — third sub-handler extract from
`_handle_register` (round 3, after stale_conn_eviction #222 and
device_upsert #223).

Owns the two WS frames Tab5 receives RIGHT AFTER the device row is
upserted but BEFORE the slow voice pipeline initializes:

  * `session_start` — confirm the session id, the resume state,
    the message count, and the active backend triple + (when
    router-active) the per-modality fleet summary.
  * `session_messages` — on resume, replay the tail of the
    message history so Tab5 chat can rehydrate its local store
    (Audit C8/K15, 2026-04-20).

## API

Two functions in one module — same shape as `config_finalize.py`
which paired the DB-persist + conn-config-mutate steps.  Keeps
the natural call-site one logical block.

```python
ok = await emit_session_start(
    ws,
    *,
    session_id, device_id, resumed, message_count, ws_id,
    conn_config, conversation, voice_mode,
    safe_send_json,
) -> bool
```

Returns ``True`` iff the send succeeded.  Returns ``False`` if
`safe_send_json` reported a transport drop — the caller MUST
short-circuit out of `_handle_register` (Tab5 will reconnect and
we'll replay session_start fresh on the next handshake).

```python
await replay_session_message_tail(
    ws,
    *,
    session_id, ws_id,
    message_store,
    safe_send_json,
    limit=20,
) -> None
```

No-op when `message_store` is None or no messages exist.
Failures are logged at WARNING level and swallowed — replay is a
nice-to-have UX feature, not a session correctness invariant.

## DIP

`safe_send_json`, `conversation`, `message_store` are all passed
in.  The module compiles without importing `VoiceServer`.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

from aiohttp import web

logger = logging.getLogger(__name__)


SafeSendJson = Callable[[web.WebSocketResponse, dict], Awaitable[bool]]

# Cap on the message tail we replay on resume.  20 is small enough
# to keep the WS frame under any practical buffer threshold while
# giving Tab5 enough context to rehydrate the visible chat scroll.
# Larger histories can be fetched via /api/v1/sessions/{id}/messages.
_REPLAY_TAIL_LIMIT = 20


async def emit_session_start(
    ws: web.WebSocketResponse,
    *,
    session_id: str,
    device_id: str,
    resumed: bool,
    message_count: int,
    ws_id: str,
    conn_config: Any,                  # VoiceConfig
    conversation: Optional[Any],       # ConversationEngine
    voice_mode: int,
    safe_send_json: SafeSendJson,
) -> bool:
    """Send the `session_start` WS frame to Tab5.

    Pre-extract the build was inline in `_handle_register` at
    server.py:1326-1349 with the `fleet_summary` inclusion logic
    duplicated from `config_update_ack.emit_config_update_ack`
    (PR #220).  Sharing the fleet-summary lookup behind
    `ConversationEngine.fleet_summary` (Wave 22b, PR #207) keeps
    both call sites in sync.

    Returns ``True`` iff `safe_send_json` reported success.
    Returns ``False`` if the send was dropped (transport
    half-closed mid-handshake) — caller MUST short-circuit out
    of `_handle_register`; Tab5 will reconnect and replay
    `session_start` fresh on the next handshake.
    """
    config_payload: dict = {
        "stt": conn_config.stt.backend,
        "tts": conn_config.tts.backend,
        "llm": conn_config.llm.backend,
        "tts_sample_rate": conn_config.audio.input_sample_rate,
        "response_mode": "match_input",
        "system_prompt": conn_config.llm.system_prompt,
    }

    # Wave 22b (#202): use ConversationEngine.fleet_summary() instead
    # of reaching into _conversation._llm with isinstance().  Same
    # pattern as config_update_ack.emit_config_update_ack (PR #220).
    if conversation is not None:
        summary = conversation.fleet_summary(voice_mode)
        if summary is not None:
            config_payload["fleet_summary"] = summary

    sent = await safe_send_json(ws, {
        "type": "session_start",
        "session_id": session_id,
        "device_id": device_id,
        "resumed": resumed,
        "message_count": message_count,
        "config": config_payload,
    })

    if not sent:
        logger.info("session_start send dropped on %s — client likely reconnecting", ws_id)

    return sent


async def replay_session_message_tail(
    ws: web.WebSocketResponse,
    *,
    session_id: str,
    ws_id: str,
    message_store: Optional[Any],     # MessageStore
    safe_send_json: SafeSendJson,
    limit: int = _REPLAY_TAIL_LIMIT,
) -> None:
    """Audit C8/K15 (2026-04-20): on resume, replay the tail of
    the message history so Tab5 chat can rehydrate its local
    store.  Pre-fix `session_start` carried only `message_count`
    and Tab5 had to fetch via REST (which it never did) — so a
    reconnect lost the conversation from the user's view even
    though it was on disk.

    No-op when:
      * `message_store` is None (test paths)
      * no messages exist for `session_id`
      * every message has empty role/content (defensive — pre-
        extract code skipped these inline)

    Failures are caught + logged at WARNING and swallowed — replay
    is a UX nice-to-have, not a session correctness invariant.
    Tab5 can still fetch full history via REST.

    `limit` defaults to 20 to keep the WS frame small.
    """
    if message_store is None:
        return

    try:
        msgs = await message_store.get_messages(session_id, limit=limit, offset=0)
        # Return the LAST `limit` (get_messages returns ascending,
        # so slice the tail).
        tail = msgs[-limit:] if len(msgs) > limit else msgs

        items = []
        for m in tail:
            role = m.get("role")
            content = m.get("content")
            if not role or not content:
                continue
            items.append({
                "role": role,
                "content": content,
                "timestamp": m.get("created_at"),
            })

        if not items:
            return  # nothing useful to replay

        sent = await safe_send_json(ws, {
            "type": "session_messages",
            "session_id": session_id,
            "items": items,
        })
        if not sent:
            logger.info("session_messages replay dropped on %s", ws_id)
        else:
            logger.info(
                "Replayed %d messages for session %s",
                len(items), session_id,
            )
    except Exception as e:
        logger.warning("session_messages replay failed: %s", e)
