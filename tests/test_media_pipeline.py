"""Tests for dragon_voice.media.pipeline.MediaPipeline.

Covers:
- Plain text (no media) → empty list
- Code block detection + render → media event with Code: alt
- Markdown table detection + render → media event with Table alt
- Image URL detection + proxy → media event with Image alt
- Multiple code blocks capped at MAX_MEDIA_PER_RESPONSE (3)
- Mixed content priority: code > table > image URLs
- Event structure validation (all required fields present)
- _parse_table helper (separator rows skipped, header preserved)
- _extract_table helper (finds first table, returns None on no table)
- _og_meta helper (both attribute orderings)
"""

import asyncio
import io
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from dragon_voice.media.pipeline import (
    MAX_MEDIA_PER_RESPONSE,
    MediaPipeline,
    _extract_table,
    _og_meta,
    _parse_table,
    _render_code_plain,
    _render_table_pillow,
    _resize_jpeg,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────


def make_mock_store(counter: list | None = None) -> MagicMock:
    """Return a mock MediaStore whose store() returns 'fake_<n>.jpg' IDs."""
    if counter is None:
        counter = [0]

    store = MagicMock()

    async def _store(data: bytes, ext: str, session_id: str = "") -> str:
        counter[0] += 1
        return f"fake_{counter[0]:04d}.{ext}"

    store.store = _store
    return store


def make_pipeline(counter: list | None = None) -> MediaPipeline:
    return MediaPipeline(store=make_mock_store(counter))


# ── process_response: plain text ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_plain_text_no_events():
    p = make_pipeline()
    events = await p.process_response("Hello, world! No code or images here.", "sess1")
    assert events == []


@pytest.mark.asyncio
async def test_empty_string_no_events():
    p = make_pipeline()
    events = await p.process_response("", "sess1")
    assert events == []


# ── process_response: code blocks ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_code_block_produces_media_event():
    p = make_pipeline()
    text = "Here is some code:\n```python\nprint('hello')\n```"
    events = await p.process_response(text, "sess1")

    assert len(events) == 1
    ev = events[0]
    assert ev["type"] == "media"
    assert ev["media_type"] == "image"
    assert ev["url"].startswith("/api/media/")
    assert ev["alt"] == "Code: python"
    assert ev["width"] == 660
    assert ev["height"] == 0


@pytest.mark.asyncio
async def test_code_block_no_language_tag():
    p = make_pipeline()
    text = "```\nsome code\n```"
    events = await p.process_response(text, "sess1")
    assert len(events) == 1
    assert events[0]["alt"] == "Code: text"


@pytest.mark.asyncio
async def test_multiple_code_blocks_capped():
    p = make_pipeline()
    # Four code blocks — only 3 should produce events
    text = "\n".join(
        f"```lang{i}\ncode block {i}\n```" for i in range(4)
    )
    events = await p.process_response(text, "sess1")
    assert len(events) == MAX_MEDIA_PER_RESPONSE


@pytest.mark.asyncio
async def test_code_block_render_failure_is_skipped(caplog):
    """A render failure logs a warning and doesn't crash."""
    import logging

    store = make_mock_store()
    p = MediaPipeline(store=store)

    # Patch render_code_block to raise
    async def _boom(*args, **kwargs):
        raise RuntimeError("render exploded")

    p.render_code_block = _boom

    text = "```python\nprint('hi')\n```"
    with caplog.at_level(logging.WARNING):
        events = await p.process_response(text, "sess1")

    assert events == []
    assert any("code block render failed" in r.message for r in caplog.records)


# ── process_response: markdown tables ────────────────────────────────────────


@pytest.mark.asyncio
async def test_table_produces_media_event():
    p = make_pipeline()
    text = (
        "Results:\n"
        "| Name  | Score |\n"
        "|-------|-------|\n"
        "| Alice | 95    |\n"
        "| Bob   | 87    |\n"
    )
    events = await p.process_response(text, "sess1")

    assert len(events) == 1
    assert events[0]["alt"] == "Table"
    assert events[0]["type"] == "media"


@pytest.mark.asyncio
async def test_table_not_detected_without_pipe_syntax():
    p = make_pipeline()
    text = "Name  Score\nAlice 95\nBob   87"
    events = await p.process_response(text, "sess1")
    assert events == []


# ── process_response: image URLs ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_image_url_produces_media_event():
    p = make_pipeline()

    # Make proxy_image a mock that returns a fake id
    async def _proxy(url, session_id=""):
        return "proxied_abc.jpg"

    p.proxy_image = _proxy

    text = "Check this out: https://example.com/photo.jpg"
    events = await p.process_response(text, "sess1")

    assert len(events) == 1
    assert events[0]["alt"] == "Image"
    assert "/api/media/proxied_abc.jpg" == events[0]["url"]


@pytest.mark.asyncio
async def test_image_url_various_extensions():
    p = make_pipeline()
    collected = []

    async def _proxy(url, session_id=""):
        collected.append(url)
        return "x.jpg"

    p.proxy_image = _proxy

    text = (
        "https://a.com/img.png "
        "https://b.com/img.jpeg "
        "https://c.com/img.gif "
        "https://d.com/img.webp"
    )
    events = await p.process_response(text, "sess1")
    assert len(events) == MAX_MEDIA_PER_RESPONSE  # capped at 3
    assert len(collected) == MAX_MEDIA_PER_RESPONSE


# ── process_response: priority and mixed content ──────────────────────────────


@pytest.mark.asyncio
async def test_code_fills_cap_before_image_urls():
    """Three code blocks fill the cap; image URLs after are ignored."""
    p = make_pipeline()
    proxy_calls = []

    async def _proxy(url, session_id=""):
        proxy_calls.append(url)
        return "img.jpg"

    p.proxy_image = _proxy

    code_blocks = "\n".join(
        f"```lang{i}\ncode {i}\n```" for i in range(3)
    )
    text = code_blocks + "\nhttps://example.com/photo.jpg\n"
    events = await p.process_response(text, "sess1")

    assert len(events) == MAX_MEDIA_PER_RESPONSE
    assert all(ev["alt"].startswith("Code:") for ev in events)
    assert proxy_calls == []


@pytest.mark.asyncio
async def test_mixed_code_and_table():
    """Two code blocks + one table = 3 events total."""
    p = make_pipeline()
    text = (
        "```python\nprint('a')\n```\n"
        "```js\nconsole.log('b');\n```\n"
        "| Col A | Col B |\n"
        "|-------|-------|\n"
        "| 1     | 2     |\n"
    )
    events = await p.process_response(text, "sess1")
    assert len(events) == 3
    alts = [ev["alt"] for ev in events]
    assert alts.count("Table") == 1
    assert sum(1 for a in alts if a.startswith("Code:")) == 2


# ── _parse_table ─────────────────────────────────────────────────────────────


def test_parse_table_basic():
    md = "| A | B |\n|---|---|\n| 1 | 2 |"
    rows = _parse_table(md)
    assert len(rows) == 2
    assert rows[0] == ["A", "B"]
    assert rows[1] == ["1", "2"]


def test_parse_table_skips_separator():
    md = "| X | Y | Z |\n|:--|:-:|--:|\n| a | b | c |"
    rows = _parse_table(md)
    assert len(rows) == 2
    assert rows[0] == ["X", "Y", "Z"]


def test_parse_table_empty():
    rows = _parse_table("no table here")
    assert rows == []


def test_parse_table_multiple_data_rows():
    md = "| Name | Val |\n|------|-----|\n| foo | 1 |\n| bar | 2 |\n| baz | 3 |"
    rows = _parse_table(md)
    assert len(rows) == 4  # header + 3 data rows


# ── _extract_table ────────────────────────────────────────────────────────────


def test_extract_table_finds_table():
    text = "Some text.\n| A | B |\n|---|---|\n| 1 | 2 |\nMore text."
    result = _extract_table(text)
    assert result is not None
    assert "| A | B |" in result


def test_extract_table_returns_none_on_no_table():
    assert _extract_table("Just plain text.") is None
    assert _extract_table("") is None


def test_extract_table_stops_at_non_pipe_line():
    text = "| A | B |\n| 1 | 2 |\nSome other line\n| X | Y |"
    result = _extract_table(text)
    # Should only get the first table block
    assert "| X | Y |" not in result
    assert "| A | B |" in result


# ── _og_meta ─────────────────────────────────────────────────────────────────


def test_og_meta_property_before_content():
    html = '<meta property="og:title" content="My Title">'
    assert _og_meta(html, "og:title") == "My Title"


def test_og_meta_content_before_property():
    html = '<meta content="My Title" property="og:title">'
    assert _og_meta(html, "og:title") == "My Title"


def test_og_meta_not_found():
    html = '<meta property="og:description" content="desc">'
    assert _og_meta(html, "og:title") is None


def test_og_meta_twitter_card():
    html = '<meta name="twitter:title" content="Tweet Title">'
    assert _og_meta(html, "twitter:title") == "Tweet Title"


# ── render_code_block (unit, no I/O) ────────────────────────────────────────


@pytest.mark.asyncio
async def test_render_code_block_returns_media_id():
    p = make_pipeline()
    media_id = await p.render_code_block("x = 1 + 2", "python", "sess1")
    assert isinstance(media_id, str)
    assert media_id.endswith(".jpg")


@pytest.mark.asyncio
async def test_render_code_block_unknown_language():
    """Unknown language should fall back to TextLexer without raising."""
    p = make_pipeline()
    media_id = await p.render_code_block("weird stuff", "notareallanguage", "sess1")
    assert media_id.endswith(".jpg")


# ── render_table (unit) ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_render_table_returns_media_id():
    p = make_pipeline()
    table_md = "| Col A | Col B |\n|-------|-------|\n| val1  | val2  |"
    media_id = await p.render_table(table_md, "sess1")
    assert isinstance(media_id, str)
    assert media_id.endswith(".jpg")


@pytest.mark.asyncio
async def test_render_table_empty_raises():
    p = make_pipeline()
    with pytest.raises(Exception):
        await p.render_table("no table here", "sess1")


# ── _render_code_plain (sync helper) ─────────────────────────────────────────


def test_render_code_plain_returns_bytes():
    result = _render_code_plain("def foo():\n    return 42")
    assert isinstance(result, bytes)
    assert len(result) > 0


# ── _resize_jpeg ─────────────────────────────────────────────────────────────


def test_resize_jpeg_shrinks_wide_image():
    from PIL import Image

    img = Image.new("RGB", (1200, 600), color=(100, 100, 100))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    data = buf.getvalue()

    resized = _resize_jpeg(data, max_width=660)
    result_img = Image.open(io.BytesIO(resized))
    assert result_img.width == 660
    assert result_img.height == 330  # aspect ratio preserved


def test_resize_jpeg_leaves_narrow_image_alone():
    from PIL import Image

    img = Image.new("RGB", (400, 200), color=(50, 50, 50))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    data = buf.getvalue()

    resized = _resize_jpeg(data, max_width=660)
    result_img = Image.open(io.BytesIO(resized))
    assert result_img.width == 400


# ── proxy_image (mocked aiohttp) ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_proxy_image_downloads_and_stores():
    from PIL import Image

    # Create a tiny valid JPEG
    img = Image.new("RGB", (100, 50), color=(200, 100, 50))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    fake_img_bytes = buf.getvalue()

    store = make_mock_store()
    p = MediaPipeline(store=store)

    # Mock aiohttp session
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.read = AsyncMock(return_value=fake_img_bytes)
    mock_resp.raise_for_status = MagicMock()
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.closed = False
    mock_session.get = MagicMock(return_value=mock_resp)

    p._session = mock_session

    media_id = await p.proxy_image("https://example.com/img.jpg", "sess1")
    assert isinstance(media_id, str)
    assert media_id.endswith(".jpg")


@pytest.mark.asyncio
async def test_proxy_image_rejects_oversized():
    from PIL import Image

    large_data = b"x" * (11 * 1024 * 1024)  # 11 MB

    store = make_mock_store()
    p = MediaPipeline(store=store)

    mock_resp = AsyncMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.read = AsyncMock(return_value=large_data)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.closed = False
    mock_session.get = MagicMock(return_value=mock_resp)

    p._session = mock_session

    with pytest.raises(ValueError, match="too large"):
        await p.proxy_image("https://example.com/huge.jpg", "sess1")
