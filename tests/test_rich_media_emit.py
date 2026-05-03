"""Tests for ``dragon_voice.rich_media_emit.emit_rich_media_for_text_turn``.

Pin every branch of the dedup'd rich-media emission helper.  Two
groups of tests:

  * No-op branches: empty response_text, missing media_pipeline,
    failure isolation.
  * Happy-path emission: media_rendering progress signal, text_update
    BEFORE media events (Audit D6 ordering invariant), every
    media event sent.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from dragon_voice.rich_media_emit import emit_rich_media_for_text_turn


def _make_ws(*, closed: bool = False) -> MagicMock:
    ws = MagicMock()
    ws.closed = closed
    ws.send_json = AsyncMock()
    return ws


def _make_safe_send_json() -> AsyncMock:
    return AsyncMock(return_value=True)


def _make_pipeline(
    *,
    has_renderable: bool = True,
    media_events: list | None = None,
    cleaned_text: str = "",
    process_raises: Exception | None = None,
) -> MagicMock:
    p = MagicMock()
    p.has_renderable_content = MagicMock(return_value=has_renderable)
    if process_raises is not None:
        p.process_response = AsyncMock(side_effect=process_raises)
    else:
        p.process_response = AsyncMock(return_value=media_events or [])
    p.strip_rendered_content = MagicMock(return_value=cleaned_text)
    return p


# ─── No-op branches ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_response_text_is_noop():
    ws = _make_ws()
    pipe = _make_pipeline()
    send = _make_safe_send_json()

    await emit_rich_media_for_text_turn(
        ws,
        response_text="",
        media_pipeline=pipe,
        session_id="sess-X",
        safe_send_json=send,
    )

    pipe.has_renderable_content.assert_not_called()
    pipe.process_response.assert_not_called()
    send.assert_not_awaited()
    ws.send_json.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_media_pipeline_is_noop():
    """Embedded usage / test paths may not have a MediaPipeline."""
    ws = _make_ws()
    send = _make_safe_send_json()

    await emit_rich_media_for_text_turn(
        ws,
        response_text="Some response with code blocks",
        media_pipeline=None,
        session_id="sess-X",
        safe_send_json=send,
    )

    send.assert_not_awaited()
    ws.send_json.assert_not_awaited()


# ─── media_rendering progress signal ─────────────────────────────


@pytest.mark.asyncio
async def test_renderable_content_emits_media_rendering_start():
    """When content is renderable, send media_rendering:start
    BEFORE the actual render so Tab5 shows a hint."""
    ws = _make_ws()
    pipe = _make_pipeline(has_renderable=True, media_events=[])
    send = _make_safe_send_json()

    await emit_rich_media_for_text_turn(
        ws,
        response_text="Here's some code: ```python\nprint('x')\n```",
        media_pipeline=pipe,
        session_id="sess-X",
        safe_send_json=send,
    )

    # The progress signal goes via safe_send_json
    send.assert_awaited_once()
    payload = send.await_args.args[1]
    assert payload == {"type": "media_rendering", "stage": "start"}


@pytest.mark.asyncio
async def test_non_renderable_content_skips_media_rendering():
    """Plain text response → no media_rendering signal."""
    ws = _make_ws()
    pipe = _make_pipeline(has_renderable=False, media_events=[])
    send = _make_safe_send_json()

    await emit_rich_media_for_text_turn(
        ws,
        response_text="Just plain text",
        media_pipeline=pipe,
        session_id="sess-X",
        safe_send_json=send,
    )
    send.assert_not_awaited()  # no media_rendering signal


@pytest.mark.asyncio
async def test_ws_closed_skips_media_rendering_signal():
    ws = _make_ws(closed=True)
    pipe = _make_pipeline(has_renderable=True, media_events=[])
    send = _make_safe_send_json()

    await emit_rich_media_for_text_turn(
        ws,
        response_text="```python\n```",
        media_pipeline=pipe,
        session_id="sess-X",
        safe_send_json=send,
    )
    send.assert_not_awaited()


# ─── Audit D6 ordering invariant ─────────────────────────────────


@pytest.mark.asyncio
async def test_text_update_sent_BEFORE_media_events():
    """Audit D6 invariant: when there are media events, the
    text_update (clearing the streamed markdown bubble) MUST be
    sent BEFORE the media events (the rendered JPEGs).  Pin the
    ordering with a captured-call list so a future refactor can't
    silently flip it (which would cause Tab5 to delete the wrong
    chat row)."""
    ws = _make_ws()
    pipe = _make_pipeline(
        has_renderable=True,
        media_events=[
            {"type": "media", "url": "/api/media/abc.jpg"},
            {"type": "media", "url": "/api/media/def.jpg"},
        ],
        cleaned_text="(cleaned)",
    )

    await emit_rich_media_for_text_turn(
        ws,
        response_text="```python\nprint(1)\n```\n```python\nprint(2)\n```",
        media_pipeline=pipe,
        session_id="sess-X",
        safe_send_json=_make_safe_send_json(),
    )

    # Sequence of ws.send_json calls — the FIRST must be text_update,
    # the rest must be the media events in order.
    types = [c.args[0]["type"] for c in ws.send_json.await_args_list]
    assert types == ["text_update", "media", "media"]
    # And the cleaned text payload landed.
    text_update = ws.send_json.await_args_list[0].args[0]
    assert text_update["text"] == "(cleaned)"


@pytest.mark.asyncio
async def test_no_media_events_skips_text_update_too():
    """If process_response returns no events, don't emit a
    text_update — the original streamed text is fine as-is."""
    ws = _make_ws()
    pipe = _make_pipeline(media_events=[])

    await emit_rich_media_for_text_turn(
        ws,
        response_text="No code blocks here",
        media_pipeline=pipe,
        session_id="sess-X",
        safe_send_json=_make_safe_send_json(),
    )

    # No ws.send_json calls (only the safe_send_json may have fired).
    ws.send_json.assert_not_awaited()


# ─── Failure isolation ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_process_response_failure_is_logged_not_raised():
    """A pipeline.process_response exception must NOT propagate —
    rich media is UX nice-to-have."""
    ws = _make_ws()
    pipe = _make_pipeline(process_raises=RuntimeError("Pillow blew up"))

    # Must NOT raise.
    await emit_rich_media_for_text_turn(
        ws,
        response_text="```py\n```",
        media_pipeline=pipe,
        session_id="sess-X",
        safe_send_json=_make_safe_send_json(),
    )

    # No text_update or media events sent (the try/except caught
    # before we could iterate).
    ws.send_json.assert_not_awaited()


# ─── log_label provenance ───────────────────────────────────────


@pytest.mark.asyncio
async def test_log_label_is_used_for_log_lines(caplog):
    """The log_label parameter discriminates TC vs local in logs.
    Caplog should see 'tc' when we pass log_label="tc"."""
    import logging
    caplog.set_level(logging.INFO, logger="dragon_voice.rich_media_emit")
    ws = _make_ws()
    pipe = _make_pipeline(
        has_renderable=True,
        media_events=[{"type": "media", "url": "/x"}],
        cleaned_text="x",
    )

    await emit_rich_media_for_text_turn(
        ws,
        response_text="```\n```",
        media_pipeline=pipe,
        session_id="sess-X",
        safe_send_json=_make_safe_send_json(),
        log_label="tc",
    )

    # At least one log line must mention 'tc'
    matched = [r for r in caplog.records if "(tc)" in r.getMessage()]
    assert matched, f"no log lines mentioned (tc): {[r.getMessage() for r in caplog.records]}"
