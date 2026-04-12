-- TinkerBox Foundation Schema v1
-- SQLite with WAL mode, async via aiosqlite
-- refs #16, #17, #18, #19, #21
--
-- Design principles (from scaffolding research):
--   - Session != Connection. Sessions survive disconnects.
--   - Conversation items are append-only. Never mutate.
--   - Device is a first-class entity with capabilities.
--   - OpenAI message format as universal context representation.
--   - Notes are sessions tagged with type='recording'.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ── Devices ────────────────────────────────────────────────────────
-- Every Tab5/ESP32/client that connects gets registered here.
-- device_id comes from NVS UUID or MAC address on the device itself.

CREATE TABLE IF NOT EXISTS devices (
    id            TEXT PRIMARY KEY,              -- UUID from device NVS or server-assigned
    hardware_id   TEXT UNIQUE,                   -- MAC address or serial (immutable HW identity)
    name          TEXT NOT NULL DEFAULT '',       -- user-assigned friendly name
    firmware_ver  TEXT NOT NULL DEFAULT '',       -- e.g. "0.4.2"
    platform      TEXT NOT NULL DEFAULT '',       -- e.g. "esp32p4-tab5", "esp32c3", "web"
    capabilities  TEXT NOT NULL DEFAULT '{}',     -- JSON: {mic:true, speaker:true, screen:true, sd_card:true}
    config        TEXT NOT NULL DEFAULT '{}',     -- JSON: per-device config overrides
    is_online     INTEGER NOT NULL DEFAULT 0,     -- 1 if currently connected
    last_seen_at  REAL NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_devices_hardware ON devices(hardware_id);
CREATE INDEX IF NOT EXISTS idx_devices_online ON devices(is_online);


-- ── Sessions ───────────────────────────────────────────────────────
-- A session is a conversation. Created on connect, persists across reconnects.
-- type: 'conversation' (normal chat), 'recording' (notes/meeting mode), 'skill' (tool execution)
-- status: 'active' → 'paused' (disconnect/timeout) → 'active' (resume) → 'ended'

CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,              -- short uuid (12 hex chars)
    device_id     TEXT,                          -- NULL for API-only sessions
    type          TEXT NOT NULL DEFAULT 'conversation'
                  CHECK(type IN ('conversation', 'recording', 'skill')),
    status        TEXT NOT NULL DEFAULT 'active'
                  CHECK(status IN ('active', 'paused', 'ended')),
    title         TEXT NOT NULL DEFAULT '',       -- auto-generated or user-set
    system_prompt TEXT NOT NULL DEFAULT '',       -- session-level prompt override (empty = use global)
    config        TEXT NOT NULL DEFAULT '{}',     -- JSON: session-level config (llm model, temperature, etc.)
    metadata      TEXT NOT NULL DEFAULT '{}',     -- JSON: arbitrary session data (skill context, tags, etc.)
    message_count INTEGER NOT NULL DEFAULT 0,    -- denormalized for fast listing
    created_at    REAL NOT NULL,
    last_active_at REAL NOT NULL,
    ended_at      REAL,                          -- NULL until ended
    FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_device ON sessions(device_id, last_active_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status, last_active_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_type ON sessions(type);


-- ── Messages ───────────────────────────────────────────────────────
-- Every exchange in a session. Append-only — never mutate.
-- role: 'user', 'assistant', 'system', 'tool'
-- input_mode: how this message entered the system
-- content stored as text (plain text for chat, JSON for tool calls/results)

CREATE TABLE IF NOT EXISTS messages (
    id            TEXT PRIMARY KEY,              -- short uuid
    session_id    TEXT NOT NULL,
    role          TEXT NOT NULL
                  CHECK(role IN ('user', 'assistant', 'system', 'tool')),
    content       TEXT NOT NULL DEFAULT '',       -- the actual text content
    input_mode    TEXT NOT NULL DEFAULT 'text'
                  CHECK(input_mode IN ('voice', 'text', 'system')),
    interrupted   INTEGER NOT NULL DEFAULT 0,    -- 1 if user interrupted assistant
    audio_duration_s REAL,                       -- duration if voice input (NULL for text)
    token_count   INTEGER,                       -- LLM tokens used (NULL for user messages)
    model         TEXT,                          -- which LLM model generated this (NULL for user)
    latency_ms    REAL,                          -- e2e processing time in ms (NULL for user)
    created_at    REAL NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, created_at ASC);
CREATE INDEX IF NOT EXISTS idx_messages_role ON messages(session_id, role);


-- ── Notes ──────────────────────────────────────────────────────────
-- Notes = enriched session artifacts. A note CAN be linked to a session
-- (e.g., meeting recording → session type='recording') or standalone.
-- Embeddings stored as JSON-encoded float arrays in a BLOB column.

CREATE TABLE IF NOT EXISTS notes (
    id            TEXT PRIMARY KEY,              -- short uuid
    session_id    TEXT,                          -- NULL for standalone notes
    title         TEXT NOT NULL DEFAULT '',
    transcript    TEXT NOT NULL DEFAULT '',       -- raw STT output or user text
    summary       TEXT NOT NULL DEFAULT '',       -- LLM-generated summary
    tags          TEXT NOT NULL DEFAULT '[]',     -- JSON array of strings
    source        TEXT NOT NULL DEFAULT 'text'
                  CHECK(source IN ('audio', 'text', 'import')),
    duration_s    REAL NOT NULL DEFAULT 0.0,      -- audio duration if from recording
    word_count    INTEGER NOT NULL DEFAULT 0,
    embedding     BLOB,                          -- JSON-encoded float[] for semantic search
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_notes_session ON notes(session_id);
CREATE INDEX IF NOT EXISTS idx_notes_created ON notes(created_at DESC);


-- ── Events ─────────────────────────────────────────────────────────
-- System event log. Lightweight pub/sub persistence layer.
-- Consumers can poll by last_id or use in-memory event bus for real-time.
-- Useful for: dashboard live updates, audit trail, debugging.

CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    type          TEXT NOT NULL,                  -- e.g. 'session.created', 'device.connected', 'message.added'
    session_id    TEXT,                           -- context session (NULL for system events)
    device_id     TEXT,                           -- context device (NULL for API events)
    data          TEXT NOT NULL DEFAULT '{}',     -- JSON payload
    created_at    REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_type ON events(type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_device ON events(device_id, created_at DESC);


-- ── Config Store ───────────────────────────────────────────────────
-- Key-value config that supports scoping: global → device → session.
-- More specific scope wins. Runtime-mutable.

CREATE TABLE IF NOT EXISTS config (
    key           TEXT NOT NULL,                  -- e.g. 'llm.backend', 'llm.model', 'tts.voice'
    value         TEXT NOT NULL,                  -- JSON-encoded value
    scope         TEXT NOT NULL DEFAULT 'global'
                  CHECK(scope IN ('global', 'device', 'session')),
    scope_id      TEXT,                           -- device_id or session_id (NULL for global)
    updated_at    REAL NOT NULL,
    PRIMARY KEY (key, scope, scope_id)
);


-- ── Memory Facts ──────────────────────────────────────────────────
-- User facts and preferences, stored with embeddings for semantic search.
-- Populated via 'remember' tool or auto-extracted from conversations.

CREATE TABLE IF NOT EXISTS memory_facts (
    id            TEXT PRIMARY KEY,
    content       TEXT NOT NULL,
    source        TEXT NOT NULL DEFAULT 'conversation',  -- 'conversation', 'manual', 'tool'
    session_id    TEXT,
    embedding     BLOB,                                   -- packed float32 vector
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_facts_created ON memory_facts(created_at DESC);


-- ── Memory Documents ──────────────────────────────────────────────
-- Ingested documents (text, URLs). Split into chunks for RAG.

CREATE TABLE IF NOT EXISTS memory_documents (
    id            TEXT PRIMARY KEY,
    title         TEXT NOT NULL DEFAULT '',
    content       TEXT NOT NULL DEFAULT '',               -- first 500 chars as preview
    chunk_count   INTEGER NOT NULL DEFAULT 0,
    source        TEXT NOT NULL DEFAULT 'upload',         -- 'upload', 'url', 'text'
    metadata      TEXT NOT NULL DEFAULT '{}',             -- JSON: page count, author, etc.
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);


-- ── Memory Chunks ─────────────────────────────────────────────────
-- Document chunks with embeddings for vector search.

CREATE TABLE IF NOT EXISTS memory_chunks (
    id            TEXT PRIMARY KEY,
    document_id   TEXT NOT NULL,
    chunk_index   INTEGER NOT NULL,
    content       TEXT NOT NULL,
    embedding     BLOB,                                   -- packed float32 vector
    created_at    REAL NOT NULL,
    FOREIGN KEY (document_id) REFERENCES memory_documents(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_chunks_doc ON memory_chunks(document_id, chunk_index);
