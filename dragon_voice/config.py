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
MAX_TOKENS_LOCAL = 1024  # Was 128 — bumped for thinking-mode models
                         # (MiniCPM-V-4.6, qwen3-thinking) which spend
                         # tokens in <think>...</think> before answering.
                         # Non-thinking models (ministral-3:3b) still
                         # stop naturally well under the cap.
MAX_TOKENS_HYBRID = 256  # Local LLM with cloud STT/TTS
MAX_TOKENS_CLOUD = 512   # Cloud LLM can handle more


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 3502
    # Wave 13 C2: Bearer token protecting all REST routes except the public
    # prefixes declared in server.py (_AUTH_PUBLIC_PREFIXES). Blank in the
    # committed config — populated from DRAGON_API_TOKEN at load time.
    api_token: str = ""


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
    # Dedicated backend for the REST `/api/v1/transcribe` endpoint
    # (long-form dictation).  When set, the transcribe endpoint uses
    # this instead of `backend` — keeps Moonshine's low-latency
    # streaming model on the voice WS pipeline while routing
    # batched WAV uploads to a long-form-friendly Whisper model.
    transcribe_backend: str = ""
    transcribe_model: str = ""
    # Where /api/v1/transcribe preserves raw WAV uploads so they can
    # be retranscribed with a stronger model later.  Empty = use the
    # default `/home/radxa/tinkerclaw/dictation_audio`.
    transcribe_audio_dir: str = ""


@dataclass
class TTSConfig:
    backend: str = "piper"
    piper_model: str = "en_US-lessac-medium"
    piper_data_dir: str = ""
    kokoro_model_path: str = ""
    # #338: kokoro-onnx 0.5.0+ needs the voices bin separately.
    kokoro_voices_path: str = ""
    kokoro_voice: str = "af_bella"
    # Pre-TTS text cleaner toggle (#338).  Strips markdown / bullets /
    # code fences / emojis / bare URLs before the backend renders the
    # text so the TTS doesn't read literal punctuation out loud.
    text_cleaner_enabled: bool = True
    edge_voice: str = "en-US-AriaNeural"
    sample_rate: int = 22050
    # NeuTTS Air voice-cloning backend (neuphonic/neutts-air-q4-gguf + neucodec)
    neutts_ref_audio: str = ""
    neutts_ref_text: str = ""
    # Supertonic-3 (Supertone, MIT) — 10 voices (F1-F5/M1-M5) + inline
    # expression tags (<laugh>/<sigh>/<breath>).  44.1 kHz output.
    supertonic_voice: str = "F1"
    supertonic_lang: str = "en"
    supertonic_speed: float = 1.0
    supertonic_steps: int = 8
    # KittenTTS (Apache-2.0) — 8 expression voices, 24 kHz English-only.
    kitten_voice: str = "expr-voice-2-f"
    kitten_speed: float = 1.0
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
    # How long Ollama keeps the model resident after the last request.
    # 30s avoids dual-model OOM on small Dragons (8 GB) by letting the
    # un-used model evict quickly.  Dual-model setups (#80) override
    # this to "5m" so both picker and responder stay warm across turns
    # — otherwise each turn pays a ~30 s disk-reload tax twice.
    ollama_keep_alive: str = "30s"
    openrouter_api_key: str = ""
    openrouter_model: str = "anthropic/claude-3-haiku"  # Default cloud model (user-selectable)
    openrouter_url: str = "https://openrouter.ai/api/v1"
    lmstudio_url: str = "http://localhost:1234/v1"
    lmstudio_model: str = "default"
    # Native tool-calling: pass tools=[...] + tool_choice="auto" to the
    # llama-server OpenAI API (requires --jinja) and consume structured
    # tool_calls, instead of prose-listing tools in the system prompt and
    # parsing markers out of the text. Off by default — the prose path
    # stays the fallback for backends/models without native tool support.
    # Live A/B on LFM2.5-VL-1.6B lifted the hard gauntlet 7/10 → 9/10.
    native_tools: bool = False
    genie_model_dir: str = "/home/radxa/qairt/models/llama32-1b"
    genie_config: str = "htp-model-config-llama32-1b-gqa.json"
    system_prompt: str = (
        "You are Tinker, a helpful AI assistant. Reply in 1-2 sentences maximum. "
        "Be concise. Never simulate user responses. Never generate text after "
        "your answer. Stop immediately after answering."
    )
    max_tokens: int = 128
    temperature: float = 0.7
    # TinkerClaw agent gateway (optional sidecar on localhost:18789)
    tinkerclaw_url: str = "http://localhost:18789"
    tinkerclaw_token: str = ""
    tinkerclaw_model: str = "minimax/MiniMax-M2.5"
    # Dual-model pipeline (backend = "dual"): a fast tool-picker + warm
    # responder. See dragon_voice/llm/dual.py and
    # docs/PLAN-dual-model-pipeline.md.  Sub-backends default to
    # "ollama" if blank — set explicitly to mix backends (e.g. picker
    # = ollama, responder = openrouter for hybrid setups).
    dual_picker_backend: str = ""
    dual_picker_model: str = "hf.co/Salesforce/xLAM-2-1b-fc-r-gguf:Q4_K_M"
    dual_responder_backend: str = ""
    dual_responder_model: str = "ministral-3:3b"
    # Multi-model router (#183) fleet — when `backend == "router"`,
    # this list defines the pool the router picks from per-turn.
    # Each entry mirrors `ModelSpec` (defined in dragon_voice.llm.router
    # to avoid an import cycle on this module).  Empty list + non-router
    # backend selection = legacy single-backend dispatch (no behavior
    # change).
    fleet: list[dict] = field(default_factory=list)


@dataclass
class AudioConfig:
    input_sample_rate: int = 16000
    input_channels: int = 1
    output_sample_rate: int = 22050
    vad_enabled: bool = True
    vad_silence_ms: int = 1500


@dataclass
class ToolsConfig:
    enabled: bool = True
    max_tool_calls: int = 3
    web_search_engine: str = "duckduckgo"
    searxng_url: str = ""  # Set to http://your-searxng:8888 to use SearXNG


@dataclass
class BillingConfig:
    """W5-B (cross-stack audit 2026-05-11): server-side daily budget cap.

    When `daily_cap_cents > 0` and the running total of all
    `api_usage` events for today UTC exceeds the cap, Dragon emits a
    `cap_downgrade` frame to Tab5 — same shape the existing
    Tab5→Dragon `config_update` with `reason='cap_downgrade'` uses,
    just in the reverse direction.

    Tab5's `voice_billing.c` will (in a follow-up wave) handle the
    incoming frame by flipping NVS `voice_mode` to LOCAL and
    surfacing a toast.  For now Dragon-side emit is observable via
    journalctl + Tab5 obs ring.

    Set via env var `BUDGET_DAILY_CENTS` (overrides config file).
    `0` = disabled (default).  Per-connection state tracks whether
    the alert already fired this day so we don't spam every
    subsequent turn.
    """
    daily_cap_cents: int = 0


@dataclass
class DatabaseConfig:
    message_retention_days: int = 30  # Purge messages older than this (0 = never purge)
    # δ2 / H6 (issue #116): auto-end sessions in `paused` state whose
    # `last_active_at` is older than this many days.  Catches the
    # device-sat-idle-for-a-month-without-motion case that the
    # existing 30-min stale-session cleanup misses (because Tab5
    # motion-sensor wakeups every ~20 min refresh `last_active_at`
    # via the register→resume→touch_session path, even when no real
    # conversation happened).  Once the session is ended, its
    # messages purge normally via `purge_old_messages`.  Set to 0 to
    # disable.
    paused_session_retention_days: int = 30


@dataclass
class MemoryConfig:
    enabled: bool = True
    embed_model: str = "nomic-embed-text"
    auto_extract_facts: bool = True
    max_context_facts: int = 3
    max_context_chunks: int = 3
    # δ3 / D-docs (issue #118): cap document ingest size.  Above this
    # the embedding loop locks the HTTP handler for many minutes and
    # can OOM Dragon's 8 GB RAM during batch embedding.  10 MB is
    # the audit's recommended baseline — a typical Markdown / PDF-
    # extracted document is well under this.  Set to 0 to disable.
    max_document_bytes: int = 10 * 1024 * 1024  # 10 MB


@dataclass
class ChannelGatewayConfig:
    """W7-F.2: OpenClaw gateway WS-RPC connector settings.

    The gateway runs as a sibling process on the Dragon machine (port
    18789, loopback-only).  When ``enabled`` is True, server startup
    swaps ``MockConnector`` for ``GatewayConnector`` so Tab5-originated
    ``channel_reply`` frames actually forward to Telegram/WhatsApp/etc.

    Default is OFF — keeping the existing mock-only behavior until an
    operator deliberately turns it on.  This lets the connector ship
    without disrupting any active session.
    """

    enabled: bool = False
    url: str = "ws://127.0.0.1:18789"
    # Reuses the same shared secret as ``llm.tinkerclaw_token`` by
    # default when blank — both auth surfaces target the same gateway
    # process.  Set explicitly only if the gateway is configured with
    # distinct tokens per client role.
    token: str = ""
    # Must match the OpenClaw `GATEWAY_CLIENT_IDS` enum
    # (see openclaw/src/gateway/protocol/client-info.ts).  "gateway-client"
    # is the generic backend id and matches the TypeScript reference client's
    # default for backend-mode connections.
    client_id: str = "gateway-client"


@dataclass
class CoredumpScraperTarget:
    """W4-D: one Tab5 to poll for coredumps."""

    device_id: str = ""              # Stable id (Tab5 NVS `device_id`); used as folder name + dedupe key
    host: str = ""                    # LAN IP or hostname (e.g. "192.168.1.90")
    port: int = 8080                  # Tab5 debug-server port
    token: str = ""                   # Bearer for `/coredump` (Tab5 NVS `auth_tok`)


@dataclass
class CoredumpScraperConfig:
    """W4-D (audit 2026-05-11): Dragon-side coredump scraper.

    Periodically polls each Tab5 in `targets` for a coredump on flash.
    When `coredump_present=true` per `/info`, pulls the body via
    `/coredump` and archives it under `save_dir/{device_id}/`.  Pre-W4-D
    coredumps only existed on Tab5 flash until a human manually curled
    them — operators saw a `SW` reset and had no on-disk dump to symbolicate.

    Default `enabled: false` so existing deploys stay unchanged.  Set
    `enabled: true` + a non-empty `targets` list to opt in.
    """

    enabled: bool = False
    poll_interval_s: float = 60.0     # one full target sweep per minute
    request_timeout_s: float = 10.0   # per-request HTTP timeout (info + coredump)
    # Default lives under /home/radxa/tinkerclaw/ because Dragon's systemd
    # hardening drop-in (W14-H14) sets `ProtectHome=read-only` + a
    # narrow `ReadWritePaths` whitelist; /home/radxa/tinkerclaw is on the
    # list, /home/radxa/tab5-coredumps is not.  Operators on un-hardened
    # deploys can override to anywhere.
    save_dir: str = "/home/radxa/tinkerclaw/tab5-coredumps"
    # Optional path to the deployed firmware ELF for auto-symbolicate.
    # If empty or non-readable, the scraper still archives the raw bin
    # — symbolication is a nice-to-have, not a gate.
    firmware_elf: str = ""
    targets: list[CoredumpScraperTarget] = field(default_factory=list)


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
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    billing: BillingConfig = field(default_factory=BillingConfig)
    channel_gateway: ChannelGatewayConfig = field(
        default_factory=ChannelGatewayConfig,
    )
    coredump_scraper: CoredumpScraperConfig = field(
        default_factory=CoredumpScraperConfig,
    )

    # β-arch (issue #123): protocol-level transition flag for the
    # progress event bus.  When True (default), migrated emitters
    # double-write — they send the legacy ad-hoc event AND the new
    # `progress` event so unmodified Tab5 firmware in production
    # keeps working unchanged.  Once Tab5 ships a `progress`-aware
    # build, set to False to drop the legacy frames in a follow-up
    # cleanup PR.  Not hot-reloaded; a flip requires a server
    # restart, which is fine for a transition flag.
    progress_bus_emit_legacy: bool = True

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
        if self.stt.transcribe_backend and self.stt.transcribe_backend not in valid_stt:
            errors.append(
                f"stt.transcribe_backend must be one of {valid_stt} (or empty), "
                f"got '{self.stt.transcribe_backend}'"
            )

        valid_llm = ("ollama", "openrouter", "lmstudio", "npu_genie", "tinkerclaw", "dual", "router")
        if self.llm.backend not in valid_llm:
            errors.append(
                f"llm.backend must be one of {valid_llm}, got '{self.llm.backend}'"
            )

        valid_tts = (
            "piper", "kokoro", "edge_tts", "openrouter", "neutts_air",
            "supertonic", "kitten",
        )
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


def _build_coredump_scraper_cfg(data: dict) -> CoredumpScraperConfig:
    """W4-D: build `CoredumpScraperConfig` with a typed `targets` list.

    `_dict_to_dataclass` would leave `targets` as a list of plain dicts;
    we need each entry to be a `CoredumpScraperTarget`.  Unknown
    per-target keys are silently dropped (forward-compat).
    """
    targets_raw = data.get("targets") or []
    targets: list[CoredumpScraperTarget] = []
    if isinstance(targets_raw, list):
        for t in targets_raw:
            if isinstance(t, dict):
                targets.append(_dict_to_dataclass(CoredumpScraperTarget, t))
    base = {k: v for k, v in data.items() if k != "targets"}
    cfg = _dict_to_dataclass(CoredumpScraperConfig, base)
    cfg.targets = targets
    return cfg


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
    for section in (
        "server", "stt", "tts", "llm", "audio", "tools", "memory",
        "database", "channel_gateway", "coredump_scraper",
    ):
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
        database=_dict_to_dataclass(DatabaseConfig, raw["database"]),
        channel_gateway=_dict_to_dataclass(
            ChannelGatewayConfig, raw["channel_gateway"],
        ),
        coredump_scraper=_build_coredump_scraper_cfg(raw["coredump_scraper"]),
    )

    # Wave 13 C2: DRAGON_API_TOKEN env var sets the REST bearer token without
    # requiring the DRAGON_VOICE_SERVER_API_TOKEN naming (shorter, lines up with
    # how Tab5 and deploy scripts refer to it). Explicit env wins over yaml.
    _dragon_api_token = os.environ.get("DRAGON_API_TOKEN", "").strip()
    if _dragon_api_token:
        config.server.api_token = _dragon_api_token

    # W5-B (cross-stack audit 2026-05-11): BUDGET_DAILY_CENTS env
    # overrides the on-disk billing.daily_cap_cents (0 = disabled).
    # Treating the env path as authoritative so an oncall can flip
    # the cap without redeploying config.yaml.
    _budget_cents_raw = os.environ.get("BUDGET_DAILY_CENTS", "").strip()
    if _budget_cents_raw:
        try:
            _budget_cents = int(_budget_cents_raw)
            if _budget_cents >= 0:
                config.billing.daily_cap_cents = _budget_cents
        except ValueError:
            # Malformed env — ignore, leave config-file value in place.
            pass

    # Wave 13 H7: same pattern for the TinkerClaw gateway token. Used to live
    # in config.yaml in the committed tree, which is a leak vector — now the
    # yaml keeps a blank placeholder and real deploys inject via env/.env.
    _tc_token = os.environ.get("TINKERCLAW_TOKEN", "").strip()
    if _tc_token:
        config.llm.tinkerclaw_token = _tc_token

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

    # Wave 14 W14-M11: validate at the end of load_config so a bad
    # backend name fails fast at startup with an actionable message,
    # instead of deferring the failure until first use (which
    # produced cryptic cascading init failures under
    # `VoicePipeline.initialize`).  Errors are logged + raised so the
    # systemd unit restart-loops with a clear cause rather than
    # silently running a half-configured pipeline.
    errors = config.validate()
    if errors:
        for err in errors:
            logger.error("config validation: %s", err)
        raise ValueError(
            "Invalid config:\n  - " + "\n  - ".join(errors)
        )

    return config


_SECRET_FIELD_MARKERS = ("api_key", "token", "password", "secret")


def config_to_dict(config: VoiceConfig, redact_secrets: bool = False) -> dict:
    """Serialize config back to a plain dict, optionally redacting secrets.

    Wave 14 W14-H01: the original predicate only matched ``api_key``.  The
    audit flagged that ``server.api_token`` (DRAGON_API_TOKEN) and
    ``llm.tinkerclaw_token`` passed through ``GET /api/config`` in
    cleartext, letting any authenticated caller exfiltrate the gateway
    token.  Broader predicate covers every secret-ish field shape.
    """
    from dataclasses import asdict

    d = asdict(config)
    if redact_secrets:
        for section in d.values():
            if isinstance(section, dict):
                for key in section:
                    if any(m in key for m in _SECRET_FIELD_MARKERS) and section[key]:
                        section[key] = "***redacted***"
    return d
