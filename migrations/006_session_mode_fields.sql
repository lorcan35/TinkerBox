-- 006_session_mode_fields.sql — Chat v4·C mode-per-session (refs #27)
--
-- Adds explicit voice_mode + llm_model columns to the sessions table so
-- each conversation carries its own mode fingerprint. Lossless for
-- existing rows (defaults to Local mode, empty model → session inherits
-- the device's active mode at runtime).
--
-- Both fields are also surfaceable via sessions.config JSON, but explicit
-- columns let the REST drawer query sort/filter fast without JSON parsing.

ALTER TABLE sessions ADD COLUMN voice_mode INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sessions ADD COLUMN llm_model  TEXT    NOT NULL DEFAULT '';
