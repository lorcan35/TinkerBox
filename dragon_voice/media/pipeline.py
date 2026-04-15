"""MediaPipeline — detect renderable content in LLM responses and produce media events.

Detects three types of content (in priority order):
1. Code blocks  — triple-backtick fenced blocks → syntax-highlighted JPEG via Pygments
2. Markdown tables — pipe-delimited rows → styled table image via Pillow
3. Image URLs  — http(s) URLs ending in image extensions → download, resize, proxy

All rendered images are saved via MediaStore and referenced by /api/media/{id} URL.
Max 3 media items per response (MAX_MEDIA_PER_RESPONSE = 3).
"""

import io
import re
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

MAX_MEDIA_PER_RESPONSE = 3
TARGET_WIDTH = 660          # matches Tab5 bubble max width (px)

# Dark theme palette
BG_COLOR    = (15, 15, 26)    # #0f0f1a
TEXT_COLOR  = (224, 224, 232) # #e0e0e8
ACCENT_COLOR = (255, 107, 53) # #ff6b35
CODE_BG     = (22, 33, 62)    # #16213e
GRID_COLOR  = (50, 50, 70)    # subtle grid lines

# Regex patterns
_RE_CODE_BLOCK = re.compile(
    r"```(?P<lang>[a-zA-Z0-9_+-]*)[ \t]*\n(?P<code>.*?)```",
    re.DOTALL,
)
_RE_TABLE_ROW = re.compile(r"^\|(.+\|)+\s*$", re.MULTILINE)
_RE_IMAGE_URL = re.compile(
    r"https?://[^\s\"'<>]+\.(?:jpg|jpeg|png|gif|webp)(?:\?[^\s\"'<>]*)?",
    re.IGNORECASE,
)

# Max download size for proxied images (10 MB)
_MAX_IMAGE_BYTES = 10 * 1024 * 1024


class MediaPipeline:
    """Analyses a complete LLM response and returns a list of media events.

    Args:
        store: A ``MediaStore`` instance used to persist rendered images.
    """

    def __init__(self, store) -> None:
        self._store = store
        self._session: Optional[object] = None  # aiohttp.ClientSession, lazy-init

    # ── Public API ───────────────────────────────────────────────────────────

    async def process_response(self, text: str, session_id: str = "") -> list[dict]:
        """Detect renderable content in *text* and return media event dicts.

        Detection priority: code blocks → tables → image URLs.
        At most ``MAX_MEDIA_PER_RESPONSE`` events are returned.

        Args:
            text:       Full LLM response text.
            session_id: Session identifier forwarded to the media store.

        Returns:
            List of event dicts ready for ``ws.send_json()``.
        """
        events: list[dict] = []

        # 1. Code blocks
        for m in _RE_CODE_BLOCK.finditer(text):
            if len(events) >= MAX_MEDIA_PER_RESPONSE:
                break
            lang = m.group("lang").strip() or "text"
            code = m.group("code")
            try:
                media_id = await self.render_code_block(code, lang, session_id)
                events.append(_media_event(media_id, f"Code: {lang}"))
            except Exception as exc:
                logger.warning("MediaPipeline: code block render failed: %s", exc)

        # 2. Markdown tables
        if len(events) < MAX_MEDIA_PER_RESPONSE:
            table_text = _extract_table(text)
            if table_text:
                try:
                    media_id = await self.render_table(table_text, session_id)
                    events.append(_media_event(media_id, "Table"))
                except Exception as exc:
                    logger.warning("MediaPipeline: table render failed: %s", exc)

        # 3. Image URLs
        for m in _RE_IMAGE_URL.finditer(text):
            if len(events) >= MAX_MEDIA_PER_RESPONSE:
                break
            url = m.group(0)
            try:
                media_id = await self.proxy_image(url, session_id)
                events.append(_media_event(media_id, "Image"))
            except Exception as exc:
                logger.warning("MediaPipeline: image proxy failed for %s: %s", url, exc)

        return events

    # ── Renderers ────────────────────────────────────────────────────────────

    async def render_code_block(
        self, code: str, language: str = "text", session_id: str = ""
    ) -> str:
        """Syntax-highlight *code* and save as a JPEG; return media_id.

        Uses Pygments (monokai style, DejaVu Sans Mono 14pt) when available,
        falls back to plain-text Pillow rendering if Pygments is absent.
        """
        try:
            img_bytes = _render_code_pygments(code, language)
        except ImportError:
            logger.warning("MediaPipeline: Pygments not installed, using plain text fallback")
            img_bytes = _render_code_plain(code)

        img_bytes = _resize_jpeg(img_bytes, TARGET_WIDTH)
        return await self._store.store(img_bytes, "jpg", session_id)

    async def render_table(self, table_md: str, session_id: str = "") -> str:
        """Render a markdown table as a styled JPEG; return media_id."""
        img_bytes = _render_table_pillow(table_md)
        img_bytes = _resize_jpeg(img_bytes, TARGET_WIDTH)
        return await self._store.store(img_bytes, "jpg", session_id)

    async def proxy_image(self, url: str, session_id: str = "") -> str:
        """Download *url*, resize to ≤660 px wide, save as JPEG; return media_id."""
        import aiohttp

        session = await self._get_http_session()
        timeout = aiohttp.ClientTimeout(total=15)
        async with session.get(url, timeout=timeout) as resp:
            resp.raise_for_status()
            data = await resp.read()

        if len(data) > _MAX_IMAGE_BYTES:
            raise ValueError(
                f"Image too large: {len(data)} bytes (max {_MAX_IMAGE_BYTES})"
            )

        img_bytes = _resize_jpeg(data, TARGET_WIDTH)
        return await self._store.store(img_bytes, "jpg", session_id)

    async def fetch_link_preview(self, url: str, session_id: str = "") -> Optional[dict]:
        """Fetch *url*, parse OG tags, return a card event dict or None."""
        import aiohttp

        try:
            session = await self._get_http_session()
            timeout = aiohttp.ClientTimeout(total=10)
            async with session.get(url, timeout=timeout) as resp:
                if resp.status != 200:
                    return None
                html = await resp.text(errors="replace")
        except Exception as exc:
            logger.warning("MediaPipeline.fetch_link_preview: GET failed for %s: %s", url, exc)
            return None

        title = _og_meta(html, "og:title") or _og_meta(html, "twitter:title")
        description = (
            _og_meta(html, "og:description") or _og_meta(html, "twitter:description")
        )
        og_image = _og_meta(html, "og:image") or _og_meta(html, "twitter:image")

        if not title and not description:
            return None

        card: dict = {"type": "card", "title": title or "", "subtitle": description or ""}

        if og_image:
            try:
                thumb_id = await self.proxy_image(og_image, session_id)
                card["image_url"] = f"/api/media/{thumb_id}"
            except Exception as exc:
                logger.warning(
                    "MediaPipeline.fetch_link_preview: thumbnail proxy failed: %s", exc
                )

        return card

    # ── Internal helpers ─────────────────────────────────────────────────────

    async def _get_http_session(self):
        """Lazy-init a cached aiohttp.ClientSession."""
        import aiohttp

        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        """Close the underlying HTTP session if open."""
        if self._session and not self._session.closed:
            await self._session.close()


# ── Pure rendering helpers (sync, testable in isolation) ─────────────────────


def _render_code_pygments(code: str, language: str) -> bytes:
    """Return raw PNG bytes of syntax-highlighted *code* using Pygments."""
    from pygments import highlight
    from pygments.lexers import get_lexer_by_name, TextLexer
    from pygments.formatters import ImageFormatter
    from pygments.styles import get_style_by_name

    try:
        lexer = get_lexer_by_name(language, stripall=True)
    except Exception:
        lexer = TextLexer(stripall=True)

    formatter = ImageFormatter(
        style="monokai",
        font_name="DejaVu Sans Mono",
        font_size=14,
        line_numbers=False,
        image_pad=12,
    )
    result = highlight(code, lexer, formatter)
    return result  # PNG bytes


def _render_code_plain(code: str) -> bytes:
    """Fallback: render *code* as plain monospace text with Pillow."""
    from PIL import Image, ImageDraw, ImageFont

    lines = code.splitlines() or [""]
    font_size = 14
    line_height = font_size + 4
    padding = 12

    try:
        font = ImageFont.truetype("DejaVuSansMono.ttf", font_size)
    except (IOError, OSError):
        font = ImageFont.load_default()

    # Measure widest line
    dummy = Image.new("RGB", (1, 1))
    draw = ImageDraw.Draw(dummy)
    max_w = max(
        (draw.textlength(line, font=font) if hasattr(draw, "textlength") else len(line) * 8)
        for line in lines
    )

    img_w = int(max_w) + padding * 2
    img_h = len(lines) * line_height + padding * 2
    img = Image.new("RGB", (img_w, img_h), color=CODE_BG)
    draw = ImageDraw.Draw(img)

    y = padding
    for line in lines:
        draw.text((padding, y), line, fill=TEXT_COLOR, font=font)
        y += line_height

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _render_table_pillow(table_md: str) -> bytes:
    """Render a markdown table string as a styled JPEG with Pillow."""
    from PIL import Image, ImageDraw, ImageFont

    rows = _parse_table(table_md)
    if not rows:
        raise ValueError("No table rows found")

    font_size = 14
    padding = 10
    line_height = font_size + padding

    try:
        font = ImageFont.truetype("DejaVuSans.ttf", font_size)
        header_font = ImageFont.truetype("DejaVuSans-Bold.ttf", font_size)
    except (IOError, OSError):
        font = ImageFont.load_default()
        header_font = font

    # Determine column widths
    num_cols = max(len(row) for row in rows)
    col_widths = [0] * num_cols
    dummy = Image.new("RGB", (1, 1))
    draw = ImageDraw.Draw(dummy)

    for row_idx, row in enumerate(rows):
        for col_idx, cell in enumerate(row):
            f = header_font if row_idx == 0 else font
            if hasattr(draw, "textlength"):
                w = int(draw.textlength(cell, font=f))
            else:
                w = len(cell) * 8
            col_widths[col_idx] = max(col_widths[col_idx], w + padding * 2)

    total_w = sum(col_widths) + 1  # +1 for trailing grid line
    total_h = len(rows) * line_height + 1

    img = Image.new("RGB", (total_w, total_h), color=BG_COLOR)
    draw = ImageDraw.Draw(img)

    # Draw rows
    for row_idx, row in enumerate(rows):
        y = row_idx * line_height
        is_header = row_idx == 0
        text_color = ACCENT_COLOR if is_header else TEXT_COLOR
        f = header_font if is_header else font

        x = 0
        for col_idx, cell in enumerate(row):
            # Cell background fill for header
            if is_header:
                draw.rectangle([x, y, x + col_widths[col_idx], y + line_height - 1], fill=CODE_BG)
            draw.text((x + padding, y + padding // 2), cell, fill=text_color, font=f)
            x += col_widths[col_idx]

        # Horizontal grid line at bottom of row
        draw.line([(0, y + line_height - 1), (total_w - 1, y + line_height - 1)], fill=GRID_COLOR)

    # Vertical grid lines
    x = 0
    for w in col_widths:
        draw.line([(x, 0), (x, total_h - 1)], fill=GRID_COLOR)
        x += w
    draw.line([(total_w - 1, 0), (total_w - 1, total_h - 1)], fill=GRID_COLOR)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _parse_table(table_md: str) -> list[list[str]]:
    """Parse markdown table rows into a list of string cell lists.

    Skips separator rows like |---|---| and empty lines.
    Returns only data rows (header first).
    """
    rows: list[list[str]] = []
    for line in table_md.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        # Skip separator rows — cells contain only dashes, colons, spaces
        cells = [c.strip() for c in line.strip("|").split("|")]
        if all(re.match(r"^[-: ]+$", c) for c in cells if c):
            continue
        if any(c for c in cells):
            rows.append([c for c in cells])
    return rows


def _extract_table(text: str) -> Optional[str]:
    """Return the first markdown table block found in *text*, or None."""
    lines = text.splitlines()
    table_lines: list[str] = []
    in_table = False

    for line in lines:
        if _RE_TABLE_ROW.match(line):
            in_table = True
            table_lines.append(line)
        elif in_table:
            # Allow blank lines within a table block (between header and rows)
            if line.strip() == "":
                continue
            break  # Non-pipe, non-blank line ends the table

    return "\n".join(table_lines) if table_lines else None


def _resize_jpeg(img_bytes: bytes, max_width: int) -> bytes:
    """Resize image data so its width is at most *max_width*; return JPEG bytes."""
    from PIL import Image

    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    w, h = img.size
    if w > max_width:
        new_h = int(h * max_width / w)
        img = img.resize((max_width, new_h), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


def _og_meta(html: str, prop: str) -> Optional[str]:
    """Extract the content of an OG/Twitter meta tag from raw HTML."""
    pattern = re.compile(
        r'<meta\s[^>]*(?:property|name)=["\']' + re.escape(prop) + r'["\'][^>]*content=["\']([^"\']+)["\']',
        re.IGNORECASE | re.DOTALL,
    )
    m = pattern.search(html)
    if m:
        return m.group(1).strip()

    # Also match content-first ordering: <meta content="..." property="og:title">
    pattern2 = re.compile(
        r'<meta\s[^>]*content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\']' + re.escape(prop) + r'["\']',
        re.IGNORECASE | re.DOTALL,
    )
    m2 = pattern2.search(html)
    return m2.group(1).strip() if m2 else None


def _media_event(media_id: str, alt: str) -> dict:
    return {
        "type": "media",
        "media_type": "image",
        "url": f"/api/media/{media_id}",
        "width": TARGET_WIDTH,
        "height": 0,
        "alt": alt,
    }
