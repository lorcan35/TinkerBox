"""Configuration management for Dragon Voice Server.

Loads from config.yaml with environment variable overrides.
All config sections are dataclass-based for type safety and IDE support.
"""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

# Default config path: config.yaml next to this file
_DEFAULT_CONFIG_PATH = Path(__file__).parent / "config.yaml"

# ── Mode-aware system prompts ──────────────────────────────────────
# Local mode: small models (qwen3:1.7b) need tight constraints
SYSTEM_PROMPT_LOCAL = (
    "You are Tinker, a helpful AI assistant running locally. "
    "Reply in 1-2 sentences maximum. Be concise and direct. "
    "Never simulate user responses. Stop immediately after answering."
)

# Hybrid mode: cloud STT/TTS but local LLM — same constraints as local
SYSTEM_PROMPT_HYBRID = (
    "You are Tinker, a helpful AI assistant. "
    "Reply in 1-3 sentences. Be concise but helpful. "
    "Never simulate user responses. Stop immediately after answering."
)

# Cloud mode: full cloud LLM (Haiku/Sonnet/GPT-4o) — allow richer responses
SYSTEM_PROMPT_CLOUD = (
    "You are Tinker, a knowledgeable AI assistant. "
    "You can give detailed, helpful responses. Keep answers focused and practical. "
    "Use natural conversational tone. If the question is simple, keep the answer short. "
    "For complex topics, explain clearly in a few sentences."
)

# Mode-aware max tokens
MAX_TOKENS_LOCAL = 128   # Small model, keep fast
MAX_TOKENS_HYBRID = 256  # Local LLM with cloud STT/TTS
MAX_TOKENS_CLOUD = 512   # Cloud LLM can handle more


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 3502


@dataclass
class STTConfig:
    backend: str = "whisper_cpp"
    model: str = "tiny"
    language: str = "en"
    moonshine_model_path: str = ""
    whisper_model_path: str = ""
    vosk_model_path: str = ""
    # OpenRouter cloud STT (key auto-populated from llm.openrouter_api_key)
    openrouter_api_key: str = ""
    openrouter_url: str = "https://openrouter.ai/api/v1"


@dataclass
class TTSConfig:
    backend: str = "piper"
    piper_model: str = "en_US-lessac-medium"
    piper_data_dir: str = ""
    kokoro_model_path: str = ""
    kokoro_voice: str = "af_heart"
    edge_voice: str = "en-US-AriaNeural"
    sample_rate: int = 22050
    # OpenRouter cloud TTS (key auto-populated from llm.openrouter_api_key)
    openrouter_api_key: str = ""
    openrouter_url: str = "https://openrouter.ai/api/v1"
    openrouter_voice: str = "alloy"


@dataclass
class LLMConfig:
    backend: str = "ollama"
    local_backend: str = ""  # Stores the original local backend for fallback (set at load time)
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "gemma3:4b"
    openrouter_api_key: str = ""
    openrouter_model: str = "anthropic/claude-3-haiku"  # Default cloud model (user-selectable)
    openrouter_url: str = "https://openrouter.ai/api/v1"
    lmstudio_url: str = "http://localhost:1234/v1"
    lmstudio_model: str = "default"
    genie_model_dir: str = "/home/radxa/qairt/models/llama32-1b"
    genie_config: str = "htp-model-config-llama32-1b-gqa.json"
    system_prompt: str = (
        "You are Tinker, a helpful AI assistant. Reply in 1-2 sentences maximum. "
        "Be concise. Never simulate user responses. Never generate text after "
        "your answer. Stop immediately after answering."
    )
    max_tokens: int = 128
    temperature: float = 0.7


@dataclass
class AudioConfig:
    input_sample_rate: int = 16000
    input_channels: int = 1
    output_sample_rate: int = 22050
    vad_enabled: bool = True
    vad_silence_ms: int = 600


@dataclass
class ToolsConfig:
    enabled: bool = True
    max_tool_calls: int = 3
    web_search_engine: str = "duckduckgo"
    searxng_url: str = ""  # Set to http://your-searxng:8888 to use SearXNG


@dataclass
class MemoryConfig:
    enabled: bool = True
    embed_model: str = "nomic-embed-text"
    auto_extract_facts: bool = True
    max_context_facts: int = 3
    max_context_chunks: int = 3


@dataclass
class VoiceConfig:
    """Top-level configuration container."""

    server: ServerConfig = field(default_factory=ServerConfig)
    stt: STTConfig = field(default_factory=STTConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)

    def validate(self) -> list[str]:
        """Validate configuration values.

        Returns a list of error strings. An empty list means the config is valid.
        """
        errors: list[str] = []

        valid_stt = ("moonshine", "whisper_cpp", "vosk", "openrouter")
        if self.stt.backend not in valid_stt:
            errors.append(
                f"stt.backend must be one of {valid_stt}, got '{self.stt.backend}'"
            )

        valid_llm = ("ollama", "openrouter", "lmstudio", "npu_genie")
        if self.llm.backend not in valid_llm:
            errors.append(
                f"llm.backend must be one of {valid_llm}, got '{self.llm.backend}'"
            )

        valid_tts = ("piper", "kokoro", "edge_tts", "openrouter")
        if self.tts.backend not in valid_tts:
            errors.append(
                f"tts.backend must be one of {valid_tts}, got '{self.tts.backend}'"
            )

        if self.llm.backend == "openrouter" and not self.llm.openrouter_api_key:
            errors.append(
                "llm.openrouter_api_key must not be empty when using openrouter backend"
            )

        if not (1 <= self.llm.max_tokens <= 4096):
            errors.append(
                f"llm.max_tokens must be between 1 and 4096, got {self.llm.max_tokens}"
            )

        if not (0 <= self.llm.temperature <= 2):
            errors.append(
                f"llm.temperature must be between 0 and 2, got {self.llm.temperature}"
            )

        return errors


# Mapping of env vars to config paths — allows overriding any setting
# Format: DRAGON_VOICE_{SECTION}_{KEY} e.g. DRAGON_VOICE_STT_BACKEND
_ENV_PREFIX = "DRAGON_VOICE_"


def _apply_env_overrides(raw: dict) -> dict:
    """Override config values from environment variables.

    Environment variables follow the pattern DRAGON_VOICE_SECTION_KEY,
    e.g. DRAGON_VOICE_STT_BACKEND=vosk overrides stt.backend.
    """
    for key, value in os.environ.items():
        if not key.startswith(_ENV_PREFIX):
            continue
        parts = key[len(_ENV_PREFIX) :].lower().split("_", 1)
        if len(parts) != 2:
            continue
        section, field_name = parts
        if section in raw and isinstance(raw[section], dict):
            # Attempt type coercion based on existing value
            existing = raw[section].get(field_name)
            if isinstance(existing, bool):
                raw[section][field_name] = value.lower() in ("true", "1", "yes")
            elif isinstance(existing, int):
                try:
                    raw[section][field_name] = int(value)
                except ValueError:
                    logger.warning("Cannot convert env %s=%s to int", key, value)
            elif isinstance(existing, float):
                try:
                    raw[section][field_name] = float(value)
                except ValueError:
                    logger.warning("Cannot convert env %s=%s to float", key, value)
            else:
                raw[section][field_name] = value
            logger.debug("Env override: %s.%s = %s", section, field_name, value)
    return raw


def _dict_to_dataclass(section_cls, data: dict):
    """Create a dataclass instance from a dict, ignoring unknown keys."""
    known_fields = {f.name for f in section_cls.__dataclass_fields__.values()}
    filtered = {k: v for k, v in data.items() if k in known_fields}
    return section_cls(**filtered)


def load_config(path: Optional[str] = None) -> VoiceConfig:
    """Load configuration from YAML file with environment variable overrides.

    Args:
        path: Path to config.yaml. Falls back to DRAGON_VOICE_CONFIG env var,
              then to the default config.yaml bundled with the package.

    Returns:
        Fully populated VoiceConfig instance.
    """
    config_path = Path(
        path
        or os.environ.get("DRAGON_VOICE_CONFIG", "")
        or str(_DEFAULT_CONFIG_PATH)
    )

    raw: dict = {}
    if config_path.exists():
        logger.info("Loading config from %s", config_path)
        with open(config_path, "r") as f:
            raw = yaml.safe_load(f) or {}
    else:
        logger.warning(
            "Config file not found at %s, using defaults", config_path
        )

    # Ensure all sections exist
    for section in ("server", "stt", "tts", "llm", "audio", "tools", "memory"):
        raw.setdefault(section, {})

    # Apply environment variable overrides
    raw = _apply_env_overrides(raw)

    # Build typed config
    config = VoiceConfig(
        server=_dict_to_dataclass(ServerConfig, raw["server"]),
        stt=_dict_to_dataclass(STTConfig, raw["stt"]),
        tts=_dict_to_dataclass(TTSConfig, raw["tts"]),
        llm=_dict_to_dataclass(LLMConfig, raw["llm"]),
        audio=_dict_to_dataclass(AudioConfig, raw["audio"]),
        tools=_dict_to_dataclass(ToolsConfig, raw["tools"]),
        memory=_dict_to_dataclass(MemoryConfig, raw["memory"]),
    )

    # Remember original local LLM backend for fallback from cloud mode
    if not config.llm.local_backend:
        config.llm.local_backend = config.llm.backend

    # Auto-propagate OpenRouter API key to STT/TTS when using cloud backends
    if config.stt.backend == "openrouter" and not config.stt.openrouter_api_key:
        config.stt.openrouter_api_key = config.llm.openrouter_api_key
        config.stt.openrouter_url = config.llm.openrouter_url
    if config.tts.backend == "openrouter" and not config.tts.openrouter_api_key:
        config.tts.openrouter_api_key = config.llm.openrouter_api_key
        config.tts.openrouter_url = config.llm.openrouter_url

    return config


def config_to_dict(config: VoiceConfig, redact_secrets: bool = False) -> dict:
    """Serialize config back to a plain dict, optionally redacting secrets."""
    from dataclasses import asdict

    d = asdict(config)
    if redact_secrets:
        # Redact anything that looks like an API key
        for section in d.values():
            if isinstance(section, dict):
                for key in section:
                    if "api_key" in key and section[key]:
                        section[key] = "***redacted***"
    return d
