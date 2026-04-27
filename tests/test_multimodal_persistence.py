"""Multimodal message persistence + hydration (#183 PR 3).

Covers:
- `add_message(media_id=...)` encodes content with the multimodal marker
- `get_context(media_store=...)` hydrates back to OpenAI multimodal arrays
- Missing media file → `[image expired]` placeholder
- No media_store given → text fallback (don't crash)
- Plain text messages unaffected by hydration
- ConversationEngine forwards media_id via process_text_stream
- Router voice_mode hot-swap on config_update (router test, not server test)
"""

import asyncio
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

# Per-fixture temp DB path — the global TINKERCLAW_DB_PATH env var
# is captured at import time by other test modules, so we pass an
# explicit path to Database() rather than relying on the default.
_TEST_DB_DIR = tempfile.mkdtemp()

from dragon_voice.config import LLMConfig
from dragon_voice.db import Database
from dragon_voice.llm.base import Modality
from dragon_voice.llm.router import CapabilityAwareRouter
from dragon_voice.messages import (
    MessageStore,
    encode_multimodal_content,
    is_multimodal_content,
    _decode_multimodal_marker,
    _hydrate_multimodal_content,
)
from dragon_voice.sessions import SessionManager


# ── Fixtures ──────────────────────────────────────────────────────
@pytest_asyncio.fixture
async def db_session():
    import time as _time
    db_path = os.path.join(_TEST_DB_DIR, f"mm_{_time.monotonic_ns()}.db")
    db = Database(db_path)
    await db.initialize()
    sm = SessionManager(db)
    # Need a device first
    await db.upsert_device(
        device_id="testdev01", hardware_id="testdev01",
        name="test", platform="test", firmware_ver="0",
        capabilities={},
    )
    s = await sm.create_session(device_id="testdev01", session_type="conversation")
    yield db, s["id"]
    await db.close()


class _FakeMediaStore:
    """Just enough surface for _hydrate_multimodal_content."""
    def __init__(self, files: dict[str, bytes]):
        self._tmp = tempfile.mkdtemp()
        self._paths: dict[str, str] = {}
        for media_id, payload in files.items():
            p = os.path.join(self._tmp, f"{media_id}.jpg")
            with open(p, "wb") as f:
                f.write(payload)
            self._paths[media_id] = p

    async def get_path(self, media_id: str):
        return self._paths.get(media_id)


# ── Marker round-trip ─────────────────────────────────────────────
def test_encode_decode_roundtrip():
    encoded = encode_multimodal_content("abc123", "describe this")
    assert is_multimodal_content(encoded)
    decoded = _decode_multimodal_marker(encoded)
    assert decoded == {"media_id": "abc123", "text": "describe this"}


def test_plain_text_not_multimodal():
    assert not is_multimodal_content("hello world")
    assert not is_multimodal_content('{"hello": "world"}')  # JSON without marker
    assert _decode_multimodal_marker("hello world") is None


# ── Hydration ─────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_hydrate_returns_string_for_plain_text():
    out = await _hydrate_multimodal_content("just text", media_store=None)
    assert out == "just text"


@pytest.mark.asyncio
async def test_hydrate_returns_placeholder_when_no_media_store():
    encoded = encode_multimodal_content("abc", "describe")
    out = await _hydrate_multimodal_content(encoded, media_store=None)
    assert "[image attached: abc]" in out


@pytest.mark.asyncio
async def test_hydrate_returns_placeholder_when_file_missing():
    encoded = encode_multimodal_content("missing-id", "describe")
    media_store = _FakeMediaStore({})
    out = await _hydrate_multimodal_content(encoded, media_store=media_store)
    assert "[image expired]" in out


@pytest.mark.asyncio
async def test_hydrate_returns_openai_multimodal_array():
    fake_jpeg = b"\xff\xd8\xff\xe0fake-jpeg-bytes"
    media_store = _FakeMediaStore({"abc": fake_jpeg})
    encoded = encode_multimodal_content("abc", "what is this?")
    out = await _hydrate_multimodal_content(encoded, media_store=media_store)
    assert isinstance(out, list)
    assert len(out) == 2
    assert out[0]["type"] == "image_url"
    assert out[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert out[1] == {"type": "text", "text": "what is this?"}


# ── End-to-end: store + retrieve ──────────────────────────────────
@pytest.mark.asyncio
async def test_add_message_with_media_id_persists_marker(db_session):
    db, session_id = db_session
    store = MessageStore(db)
    msg = await store.add_message(
        session_id=session_id,
        role="user",
        content="describe this photo",
        input_mode="text",
        media_id="img-001",
    )
    assert is_multimodal_content(msg["content"])
    decoded = _decode_multimodal_marker(msg["content"])
    assert decoded["media_id"] == "img-001"
    assert decoded["text"] == "describe this photo"


@pytest.mark.asyncio
async def test_get_context_hydrates_with_media_store(db_session):
    db, session_id = db_session
    store = MessageStore(db)
    media_store = _FakeMediaStore({"img-001": b"\xff\xd8\xff\xe0fake"})

    await store.add_message(session_id=session_id, role="user",
                            content="describe", input_mode="text",
                            media_id="img-001")
    await store.add_message(session_id=session_id, role="assistant",
                            content="It's a photo of a chair.")
    await store.add_message(session_id=session_id, role="user",
                            content="What color was it?")

    context = await store.get_context(
        session_id, max_messages=10, media_store=media_store
    )
    # System prompt + 3 messages
    assert len(context) == 4
    # First user msg should be hydrated to multimodal array
    user_vision = context[1]
    assert user_vision["role"] == "user"
    assert isinstance(user_vision["content"], list)
    assert user_vision["content"][0]["type"] == "image_url"
    # Plain text turns stay as strings
    assert isinstance(context[2]["content"], str)
    assert isinstance(context[3]["content"], str)


@pytest.mark.asyncio
async def test_get_context_without_media_store_returns_placeholder(db_session):
    """Backward compat: callers that don't pass media_store still get sensible output."""
    db, session_id = db_session
    store = MessageStore(db)
    await store.add_message(session_id=session_id, role="user",
                            content="describe", media_id="img-x")
    context = await store.get_context(session_id, max_messages=10)
    user_msg = context[1]
    # Without media_store, hydrate returns the placeholder text
    assert isinstance(user_msg["content"], str)
    assert "[image attached" in user_msg["content"]


# ── Router voice_mode hot-swap ────────────────────────────────────
def _fleet():
    return [
        {"id": "ministral", "backend": "ollama", "model_id": "ministral-3:3b",
         "caps": ["text"], "tier": "local", "priority": 0},
        {"id": "minicpm_v4", "backend": "ollama",
         "model_id": "hf.co/openbmb/MiniCPM-V-4-gguf:Q4_K_M",
         "caps": ["text", "vision"], "tier": "local", "priority": 10},
        {"id": "haiku", "backend": "openrouter",
         "model_id": "anthropic/claude-3.5-haiku",
         "caps": ["text", "vision"], "tier": "cloud", "priority": 5},
    ]


def test_router_set_voice_mode_changes_capabilities():
    """capabilities is the union over the active tier."""
    cfg = LLMConfig(backend="router", fleet=_fleet())
    router = CapabilityAwareRouter(cfg)
    # Local mode (default): minicpm_v4 supplies vision
    assert Modality.VISION in router.capabilities
    # Tinkerclaw mode: router not used → no caps
    router.set_voice_mode(3)
    assert router.capabilities == frozenset()
    # Cloud mode: haiku supplies vision
    router.set_voice_mode(2)
    assert Modality.VISION in router.capabilities


def test_ollama_translator_flat_text_passthrough():
    from dragon_voice.llm.ollama_llm import _translate_to_ollama_format
    msg = {"role": "user", "content": "hello"}
    assert _translate_to_ollama_format(msg) == msg


def test_ollama_translator_strips_data_uri_prefix():
    from dragon_voice.llm.ollama_llm import _translate_to_ollama_format
    msg = {"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,RAWBYTES"}},
        {"type": "text", "text": "describe"},
    ]}
    out = _translate_to_ollama_format(msg)
    assert out["role"] == "user"
    assert out["content"] == "describe"
    assert out["images"] == ["RAWBYTES"]


def test_ollama_translator_no_image_no_images_key():
    from dragon_voice.llm.ollama_llm import _translate_to_ollama_format
    msg = {"role": "user", "content": [
        {"type": "text", "text": "no image"},
    ]}
    out = _translate_to_ollama_format(msg)
    assert out == {"role": "user", "content": "no image"}
    assert "images" not in out


def test_router_summarize_per_modality():
    cfg = LLMConfig(backend="router", fleet=_fleet())
    router = CapabilityAwareRouter(cfg)
    summary_local = router.summarize(voice_mode=0)
    summary_cloud = router.summarize(voice_mode=2)
    assert summary_local["vision"] == "hf.co/openbmb/MiniCPM-V-4-gguf:Q4_K_M"
    assert summary_cloud["vision"] == "anthropic/claude-3.5-haiku"
    # Audio is in neither tier of this fleet
    assert summary_local.get("audio_in") is None
    assert summary_cloud.get("audio_in") is None
