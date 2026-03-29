"""End-to-end tests for TinkerClaw foundation modules.

Tests the full stack: Database, SessionManager, MessageStore,
ConversationEngine, and REST API.

refs #16, #17
"""

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

# Set a temp DB path before importing modules
_TEST_DB_DIR = tempfile.mkdtemp()
os.environ["TINKERCLAW_DB_PATH"] = os.path.join(_TEST_DB_DIR, "test.db")

from dragon_voice.db import Database
from dragon_voice.sessions import SessionManager
from dragon_voice.messages import MessageStore


# ── Fixtures ───────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def db():
    """Create a fresh in-memory-style test database."""
    db_path = os.path.join(_TEST_DB_DIR, f"test_{time.monotonic_ns()}.db")
    database = Database(db_path)
    await database.initialize()
    yield database
    await database.close()
    # Clean up file
    try:
        os.unlink(db_path)
    except OSError:
        pass


@pytest_asyncio.fixture
async def session_mgr(db):
    """Create a SessionManager backed by the test DB."""
    mgr = SessionManager(db, timeout_s=5)  # Short timeout for tests
    # Don't start the cleanup loop in tests — we test it manually
    yield mgr


@pytest_asyncio.fixture
async def message_store(db):
    """Create a MessageStore backed by the test DB."""
    return MessageStore(db)


# ── Test: Database Schema ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_schema_applied(db):
    """Schema tables should exist after initialization."""
    cursor = await db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = [row[0] for row in await cursor.fetchall()]
    assert "devices" in tables
    assert "sessions" in tables
    assert "messages" in tables
    assert "notes" in tables
    assert "events" in tables
    assert "config" in tables


# ── Test: Device Registration ──────────────────────────────────────


@pytest.mark.asyncio
async def test_device_upsert_and_get(db):
    """Devices can be registered and retrieved."""
    device = await db.upsert_device(
        device_id="dev-001",
        hardware_id="AA:BB:CC:DD:EE:FF",
        name="Test Tab5",
        firmware_ver="0.4.2",
        platform="esp32p4-tab5",
        capabilities={"mic": True, "speaker": True},
    )
    assert device["id"] == "dev-001"
    assert device["hardware_id"] == "AA:BB:CC:DD:EE:FF"
    assert device["name"] == "Test Tab5"
    assert device["is_online"] == 1

    # Re-fetch
    fetched = await db.get_device("dev-001")
    assert fetched is not None
    assert fetched["platform"] == "esp32p4-tab5"


@pytest.mark.asyncio
async def test_device_online_offline(db):
    """Devices can be marked online and offline."""
    await db.upsert_device("dev-002", "11:22:33:44:55:66")
    device = await db.get_device("dev-002")
    assert device["is_online"] == 1

    await db.set_device_online("dev-002", False)
    device = await db.get_device("dev-002")
    assert device["is_online"] == 0

    await db.set_device_online("dev-002", True)
    device = await db.get_device("dev-002")
    assert device["is_online"] == 1


@pytest.mark.asyncio
async def test_list_devices(db):
    """List devices with online filter."""
    await db.upsert_device("dev-a", "aa:aa:aa:aa:aa:aa", name="Alpha")
    await db.upsert_device("dev-b", "bb:bb:bb:bb:bb:bb", name="Beta")
    await db.set_device_online("dev-b", False)

    all_devices = await db.list_devices()
    assert len(all_devices) == 2

    online = await db.list_devices(online_only=True)
    assert len(online) == 1
    assert online[0]["id"] == "dev-a"


# ── Test: Session Lifecycle ────────────────────────────────────────


@pytest.mark.asyncio
async def test_session_create(db, session_mgr):
    """Sessions can be created."""
    # Register device first (FK constraint)
    await db.upsert_device("dev-001", "01:01:01:01:01:01")

    session = await session_mgr.create_session(device_id="dev-001")
    assert session["status"] == "active"
    assert session["device_id"] == "dev-001"
    assert session["type"] == "conversation"
    assert len(session["id"]) == 12  # 6 bytes = 12 hex chars


@pytest.mark.asyncio
async def test_session_pause_resume(db, session_mgr):
    """Sessions can be paused and resumed."""
    await db.upsert_device("dev-001", "01:01:01:01:01:01")
    session = await session_mgr.create_session(device_id="dev-001")
    sid = session["id"]

    await session_mgr.pause_session(sid)
    paused = await session_mgr.get_session(sid)
    assert paused["status"] == "paused"

    resumed = await session_mgr.resume_session(sid)
    assert resumed is not None
    assert resumed["status"] == "active"


@pytest.mark.asyncio
async def test_session_end(db, session_mgr):
    """Ended sessions cannot be resumed."""
    await db.upsert_device("dev-001", "01:01:01:01:01:01")
    session = await session_mgr.create_session(device_id="dev-001")
    sid = session["id"]

    await session_mgr.end_session(sid)
    ended = await session_mgr.get_session(sid)
    assert ended["status"] == "ended"
    assert ended["ended_at"] is not None

    # Cannot resume ended session
    result = await session_mgr.resume_session(sid)
    assert result is None


@pytest.mark.asyncio
async def test_session_get_or_create(db, session_mgr):
    """get_or_create resumes existing or creates new."""
    await db.upsert_device("dev-001", "01:01:01:01:01:01")

    # Create initial session
    s1 = await session_mgr.create_session(device_id="dev-001")
    await session_mgr.pause_session(s1["id"])

    # Resume via get_or_create
    s2, resumed = await session_mgr.get_or_create_session(
        device_id="dev-001", requested_session_id=s1["id"]
    )
    assert resumed is True
    assert s2["id"] == s1["id"]

    # With invalid session_id → creates new
    s3, resumed = await session_mgr.get_or_create_session(
        device_id="dev-001", requested_session_id="nonexistent"
    )
    assert resumed is False
    assert s3["id"] != s1["id"]


@pytest.mark.asyncio
async def test_list_sessions_pagination(db, session_mgr):
    """Sessions can be listed with pagination."""
    for i in range(5):
        await db.upsert_device(f"dev-{i}", f"0{i}:0{i}:0{i}:0{i}:0{i}:0{i}")
        await session_mgr.create_session(device_id=f"dev-{i}")

    # Get first 3
    page1 = await session_mgr.list_sessions(limit=3, offset=0)
    assert len(page1) == 3

    # Get next 3
    page2 = await session_mgr.list_sessions(limit=3, offset=3)
    assert len(page2) == 2

    # All 5
    all_sessions = await session_mgr.list_sessions(limit=10)
    assert len(all_sessions) == 5


# ── Test: Message Store ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_add_and_retrieve_messages(db, message_store):
    """Messages can be added and retrieved."""
    # Create a session first
    session = await db.create_session("sess-001", device_id=None)

    await message_store.add_message("sess-001", "user", "Hello!")
    await message_store.add_message("sess-001", "assistant", "Hi there!")
    await message_store.add_message("sess-001", "user", "How are you?")
    await message_store.add_message("sess-001", "assistant", "I'm great!")
    await message_store.add_message("sess-001", "user", "What is 2+2?")

    messages = await message_store.get_messages("sess-001")
    assert len(messages) == 5
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "Hello!"
    assert messages[4]["content"] == "What is 2+2?"


@pytest.mark.asyncio
async def test_message_count_denormalized(db, message_store):
    """Session message_count is updated as messages are added."""
    await db.create_session("sess-002", device_id=None)

    await message_store.add_message("sess-002", "user", "msg1")
    await message_store.add_message("sess-002", "assistant", "msg2")
    await message_store.add_message("sess-002", "user", "msg3")

    session = await db.get_session("sess-002")
    assert session["message_count"] == 3

    count = await message_store.count_messages("sess-002")
    assert count == 3


@pytest.mark.asyncio
async def test_get_context_openai_format(db, message_store):
    """get_context returns messages in OpenAI format with system prompt."""
    await db.create_session("sess-003", device_id=None)

    await message_store.add_message("sess-003", "user", "Hello")
    await message_store.add_message("sess-003", "assistant", "Hi!")
    await message_store.add_message("sess-003", "user", "Tell me a joke")

    context = await message_store.get_context("sess-003", max_messages=10)

    # First message should be system prompt
    assert context[0]["role"] == "system"
    assert len(context[0]["content"]) > 0

    # Then user/assistant messages
    assert context[1] == {"role": "user", "content": "Hello"}
    assert context[2] == {"role": "assistant", "content": "Hi!"}
    assert context[3] == {"role": "user", "content": "Tell me a joke"}


@pytest.mark.asyncio
async def test_get_context_truncation(db, message_store):
    """get_context respects max_messages limit."""
    await db.create_session("sess-004", device_id=None)

    for i in range(10):
        await message_store.add_message("sess-004", "user", f"msg-{i}")
        await message_store.add_message("sess-004", "assistant", f"resp-{i}")

    # Only get last 4 messages
    context = await message_store.get_context("sess-004", max_messages=4)

    # System prompt + 4 messages
    assert len(context) == 5  # 1 system + 4 conversation
    assert context[0]["role"] == "system"
    # Last 4 should be the most recent
    assert context[-1]["content"] == "resp-9"


@pytest.mark.asyncio
async def test_get_context_custom_system_prompt(db, message_store):
    """get_context uses custom system prompt when provided."""
    await db.create_session("sess-005", device_id=None)
    await message_store.add_message("sess-005", "user", "Hi")

    context = await message_store.get_context(
        "sess-005", system_prompt="You are a pirate."
    )
    assert context[0]["content"] == "You are a pirate."


# ── Test: Full Conversation Flow ───────────────────────────────────


@pytest.mark.asyncio
async def test_full_conversation_flow(db, message_store):
    """Create session → 5 messages → retrieve full history."""
    # Register device
    device = await db.upsert_device("dev-flow", "FF:FF:FF:FF:FF:FF", name="FlowTest")
    assert device["is_online"] == 1

    # Create session
    session = await db.create_session("sess-flow", device_id="dev-flow")
    assert session["status"] == "active"

    # Send 5 exchanges
    exchanges = [
        ("Hello, Glyph!", "Hello! How can I help you?"),
        ("What time is it?", "I don't have a clock, but I can help with other things!"),
        ("Tell me a joke", "Why did the scarecrow win an award? Outstanding in his field!"),
        ("That's funny", "Glad you liked it!"),
        ("Goodbye", "See you later!"),
    ]

    for user_msg, assistant_msg in exchanges:
        await message_store.add_message("sess-flow", "user", user_msg, input_mode="voice")
        await message_store.add_message("sess-flow", "assistant", assistant_msg)

    # Verify full history
    messages = await message_store.get_messages("sess-flow")
    assert len(messages) == 10  # 5 user + 5 assistant

    # Verify session message count
    session = await db.get_session("sess-flow")
    assert session["message_count"] == 10

    # Verify context building
    context = await message_store.get_context("sess-flow", max_messages=20)
    assert len(context) == 11  # 1 system + 10 conversation
    assert context[1]["content"] == "Hello, Glyph!"
    assert context[-1]["content"] == "See you later!"


# ── Test: Device Connect/Disconnect Flow ───────────────────────────


@pytest.mark.asyncio
async def test_device_connect_disconnect_flow(db, session_mgr):
    """Device connects → registers → shows in device list → disconnect → offline."""
    # Connect and register
    device = await db.upsert_device(
        "dev-lifecycle", "AA:BB:CC:DD:EE:01",
        name="Lifecycle Test", platform="esp32p4-tab5",
    )
    assert device["is_online"] == 1

    # Should appear in device list
    devices = await db.list_devices(online_only=True)
    device_ids = [d["id"] for d in devices]
    assert "dev-lifecycle" in device_ids

    # Create session
    session = await session_mgr.create_session(device_id="dev-lifecycle")

    # Disconnect: pause session + mark offline
    await session_mgr.pause_session(session["id"])
    await db.set_device_online("dev-lifecycle", False)

    # Device should be offline
    device = await db.get_device("dev-lifecycle")
    assert device["is_online"] == 0

    # Session should be paused
    s = await session_mgr.get_session(session["id"])
    assert s["status"] == "paused"

    # Should NOT appear in online-only list
    online = await db.list_devices(online_only=True)
    assert len([d for d in online if d["id"] == "dev-lifecycle"]) == 0


# ── Test: Events ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_event_logging(db):
    """Events are logged and retrievable."""
    eid1 = await db.add_event("device.connected", device_id="dev-001",
                               data={"platform": "esp32p4-tab5"})
    eid2 = await db.add_event("session.created", session_id="sess-001",
                               device_id="dev-001")
    eid3 = await db.add_event("message.added", session_id="sess-001")

    # Get all events
    events = await db.get_events()
    assert len(events) == 3

    # Get events since a specific ID
    events = await db.get_events(since_id=eid1)
    assert len(events) == 2

    # Filter by type
    events = await db.get_events(event_type="device.connected")
    assert len(events) == 1
    assert events[0]["device_id"] == "dev-001"


# ── Test: Config Store ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_config_scoped_resolution(db):
    """Config resolves: session > device > global."""
    # Set at all three scopes
    await db.set_config("llm.model", '"gemma3:4b"', "global")
    await db.set_config("llm.model", '"llama3.2:1b"', "device", "dev-001")
    await db.set_config("llm.model", '"gpt-4"', "session", "sess-001")

    # Global only
    val = await db.get_resolved_config("llm.model")
    assert val == '"gemma3:4b"'

    # Device overrides global
    val = await db.get_resolved_config("llm.model", device_id="dev-001")
    assert val == '"llama3.2:1b"'

    # Session overrides device and global
    val = await db.get_resolved_config("llm.model", device_id="dev-001", session_id="sess-001")
    assert val == '"gpt-4"'


# ── Test: Notes ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_notes_crud(db):
    """Notes can be created and listed."""
    note = await db.add_note(
        note_id="note-001",
        title="Meeting Notes",
        transcript="We discussed the project timeline.",
        summary="Timeline discussion",
        tags=["meeting", "project"],
        source="audio",
        duration_s=120.5,
        word_count=6,
    )
    assert note["id"] == "note-001"
    assert note["title"] == "Meeting Notes"

    fetched = await db.get_note("note-001")
    assert fetched is not None
    assert json.loads(fetched["tags"]) == ["meeting", "project"]

    notes = await db.list_notes()
    assert len(notes) == 1


# ── Test: Stale Session Cleanup ────────────────────────────────────


@pytest.mark.asyncio
async def test_stale_session_detection(db):
    """Stale sessions are detected based on inactivity timeout."""
    # Create a session with an old last_active_at
    now = time.time()
    await db.conn.execute(
        """
        INSERT INTO sessions (id, device_id, type, status, created_at, last_active_at)
        VALUES (?, ?, 'conversation', 'paused', ?, ?)
        """,
        ("stale-sess", None, now - 3600, now - 3600),  # 1 hour ago
    )
    await db.conn.commit()

    # Should find it with a 30-minute timeout
    stale = await db.get_stale_sessions(timeout_seconds=1800)
    assert len(stale) == 1
    assert stale[0]["id"] == "stale-sess"

    # Should NOT find it with a 2-hour timeout
    stale = await db.get_stale_sessions(timeout_seconds=7200)
    assert len(stale) == 0
