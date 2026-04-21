"""Wave 14 W14-H01 regression: `config_to_dict(redact_secrets=True)` must
cover every secret-ish field shape, not just `api_key`.

Prior bug: `server.api_token` (DRAGON_API_TOKEN — the REST bearer) and
`llm.tinkerclaw_token` (gateway bearer) passed through `GET /api/config`
in cleartext because the redaction predicate matched only strings
containing `"api_key"`.  Any authenticated caller could exfiltrate the
TinkerClaw gateway token and pivot to the agent runner.

This test asserts the widened predicate (`api_key|token|password|secret`)
masks every field that exposes a secret.
"""

import os

from dragon_voice.config import VoiceConfig, config_to_dict


def _load_config_with_secrets() -> VoiceConfig:
    os.environ["DRAGON_API_TOKEN"] = "w14-h01-probe-token"
    os.environ["TINKERCLAW_TOKEN"] = "w14-h01-probe-tctoken"
    from dragon_voice.config import load_config
    cfg = load_config()
    # Inject a few more for the test; the committed yaml may have these blank.
    cfg.llm.openrouter_api_key = "sk-or-probe"
    cfg.stt.openrouter_api_key = "sk-or-stt-probe"
    cfg.tts.openrouter_api_key = "sk-or-tts-probe"
    return cfg


def test_api_token_redacted():
    cfg = _load_config_with_secrets()
    d = config_to_dict(cfg, redact_secrets=True)
    assert d["server"]["api_token"] == "***redacted***"


def test_tinkerclaw_token_redacted():
    cfg = _load_config_with_secrets()
    d = config_to_dict(cfg, redact_secrets=True)
    assert d["llm"]["tinkerclaw_token"] == "***redacted***"


def test_api_keys_still_redacted():
    """Regression: the original `api_key` predicate must still hit."""
    cfg = _load_config_with_secrets()
    d = config_to_dict(cfg, redact_secrets=True)
    assert d["llm"]["openrouter_api_key"] == "***redacted***"
    assert d["stt"]["openrouter_api_key"] == "***redacted***"
    assert d["tts"]["openrouter_api_key"] == "***redacted***"


def test_non_secret_fields_preserved():
    """Redaction must not clobber operational data like urls / models."""
    cfg = _load_config_with_secrets()
    d = config_to_dict(cfg, redact_secrets=True)
    assert d["llm"]["openrouter_url"].startswith("http")
    assert d["llm"]["ollama_model"]
    assert d["server"]["port"]


def test_unredacted_path_is_verbatim():
    """redact_secrets=False returns the real values (internal use only)."""
    cfg = _load_config_with_secrets()
    d = config_to_dict(cfg, redact_secrets=False)
    assert d["server"]["api_token"] == "w14-h01-probe-token"
    assert d["llm"]["tinkerclaw_token"] == "w14-h01-probe-tctoken"
