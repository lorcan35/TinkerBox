"""Capability-aware multi-model router (#183).

Holds a fleet of LLMBackend instances declared in `LLMConfig.fleet`,
picks one per request based on the modalities present in the inbound
messages and the active voice_mode tier policy.

Lifecycle:
- `__init__(config)` parses the fleet into `ModelSpec` records, stores
  voice_mode (default 0 = local).
- `initialize()` is a no-op — sub-backends are *lazily instantiated*
  on first use to keep startup fast and avoid loading models that may
  never be needed.
- `generate_stream_with_messages(messages)` infers required caps from
  the message content array, picks a spec via `choose()`, lazily
  instantiates the sub-backend, and delegates token streaming.
- `set_voice_mode(mode)` flips the tier policy without rebuilding the
  fleet — instantiated backends survive the switch.
- `shutdown()` closes every instantiated sub-backend.

Threading: the router itself is single-task (each WS connection has
its own deep-copied config and its own router instance), so per-call
contention is not a concern.  Lazy instantiation uses an `asyncio.Lock`
to keep parallel requests for the same uninstantiated backend from
double-creating it.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from dataclasses import dataclass, field
from typing import AsyncIterator, Iterable

from dragon_voice.config import LLMConfig
from dragon_voice.llm.base import LLMBackend, Modality

logger = logging.getLogger(__name__)


# ── Tier policy from voice_mode ────────────────────────────────────
# voice_mode is the Tab5-side three-tier picker:
#   0 = Local        — local-tier LLM (NPU/ollama/lmstudio if LAN)
#   1 = Hybrid       — local LLM, cloud STT/TTS (LLM tier unchanged)
#   2 = Full Cloud   — cloud + LAN tier eligible for routing
#   3 = TinkerClaw   — gateway picks; router is not used
TIER_FOR_MODE: dict[int, frozenset[str] | None] = {
    0: frozenset({"local"}),
    1: frozenset({"local"}),
    2: frozenset({"cloud", "lan"}),
    3: None,
}


@dataclass
class ModelSpec:
    """One entry in the router's fleet.

    Mirrors the YAML schema in `LLMConfig.fleet`. Fields beyond the
    core router contract (e.g. `lmstudio_url`) get folded into the
    sub-backend's `LLMConfig` at instantiation time.
    """

    id: str                                # human-readable, e.g. "minicpm_v4"
    backend: str                           # ollama/openrouter/lmstudio/npu_genie/tinkerclaw
    model_id: str                          # the actual model identifier
    capabilities: frozenset[Modality]      # declared modalities
    tier: str                              # local | cloud | lan
    priority: int = 0                      # lower = preferred
    keep_alive_s: int = 120                # ollama keep_alive override
    overrides: dict = field(default_factory=dict)  # extra cfg fields (lmstudio_url, etc.)


def _spec_from_dict(entry: dict) -> ModelSpec:
    """Parse a YAML-loaded dict into a ModelSpec, validating fields."""
    if "id" not in entry or "backend" not in entry or "model_id" not in entry:
        raise ValueError(
            f"fleet entry missing required field (id/backend/model_id): {entry}"
        )
    raw_caps = entry.get("caps", entry.get("capabilities", ["text"]))
    caps = frozenset(Modality(c) for c in raw_caps)
    tier = entry.get("tier", "local")
    if tier not in ("local", "cloud", "lan"):
        raise ValueError(f"fleet entry tier must be local/cloud/lan: {entry}")
    # Pull known core fields out; everything else becomes an override
    # to splat into the sub-backend's LLMConfig.
    KNOWN = {"id", "backend", "model_id", "caps", "capabilities",
             "tier", "priority", "keep_alive_s"}
    overrides = {k: v for k, v in entry.items() if k not in KNOWN}
    return ModelSpec(
        id=entry["id"],
        backend=entry["backend"],
        model_id=entry["model_id"],
        capabilities=caps,
        tier=tier,
        priority=int(entry.get("priority", 0)),
        keep_alive_s=int(entry.get("keep_alive_s", 120)),
        overrides=overrides,
    )


def infer_required_caps(messages: list[dict]) -> frozenset[Modality]:
    """Inspect OpenAI-format messages to infer the modality requirements.

    - Image content (`type: image_url`) → +VISION
    - Video content (`type: video_url`) → +VIDEO
    - Audio content (`type: input_audio` or audio attachment) → +AUDIO_IN
    - Tool definitions in the system prompt → +TOOL_CALLING (light heuristic;
      the parser is tolerant so we set this conservatively whenever any
      message mentions a tool call marker).
    Always includes TEXT.
    """
    caps: set[Modality] = {Modality.TEXT}
    for msg in messages or []:
        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                ptype = part.get("type", "")
                if ptype == "image_url":
                    caps.add(Modality.VISION)
                elif ptype == "video_url":
                    caps.add(Modality.VIDEO)
                elif ptype == "input_audio" or ptype == "audio":
                    caps.add(Modality.AUDIO_IN)
        elif isinstance(content, str):
            # Cheap heuristic: tool-call markers in system prompt mean
            # the caller wants tool support.
            if msg.get("role") == "system" and (
                "<tool>" in content or "<tool_call>" in content
            ):
                caps.add(Modality.TOOL_CALLING)
    return frozenset(caps)


class CapabilityAwareRouter(LLMBackend):
    """Pick one of N sub-backends per request based on capabilities + tier."""

    def __init__(self, config: LLMConfig) -> None:
        self._config = config
        self._fleet: list[ModelSpec] = [
            _spec_from_dict(e) for e in (config.fleet or [])
        ]
        if not self._fleet:
            raise ValueError(
                "CapabilityAwareRouter requires a non-empty `llm.fleet` — "
                "either populate fleet[] or set backend to a single-model "
                "value (ollama/openrouter/lmstudio/...)."
            )
        self._instances: dict[str, LLMBackend] = {}
        self._instance_lock = asyncio.Lock()
        self._voice_mode: int = 0
        logger.info(
            "Router initialized with %d models: %s",
            len(self._fleet),
            ", ".join(f"{s.id}({s.tier})" for s in self._fleet),
        )

    # ── LLMBackend abstract methods ──────────────────────────────
    async def initialize(self) -> None:
        """No-op — sub-backends initialise lazily on first use."""
        return None

    async def generate_stream(
        self, prompt: str, system_prompt: str = ""
    ) -> AsyncIterator[str]:
        """Text-only fallback — wraps the single prompt and delegates."""
        messages: list[dict] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        async for tok in self.generate_stream_with_messages(messages):
            yield tok

    async def generate_stream_with_messages(
        self, messages: list[dict]
    ) -> AsyncIterator[str]:
        required = infer_required_caps(messages)
        spec = self.choose(required, self._voice_mode)
        if spec is None:
            tier = TIER_FOR_MODE.get(self._voice_mode)
            tier_str = ",".join(sorted(tier)) if tier else "tinkerclaw"
            logger.error(
                "router: no fleet model satisfies %s in tier=%s",
                sorted(required), tier_str,
            )
            # Yield nothing; caller (server.py) will detect empty + emit
            # the modality_unsupported error event.
            return
        backend = await self._ensure_instance(spec)
        logger.info(
            "router: chose %s for %s tier=%s",
            spec.id, sorted(required), spec.tier,
        )
        async for tok in backend.generate_stream_with_messages(messages):
            yield tok

    async def shutdown(self) -> None:
        for spec_id, backend in list(self._instances.items()):
            try:
                await backend.shutdown()
            except Exception as e:  # noqa: BLE001
                logger.warning("router: %s shutdown error: %s", spec_id, e)
        self._instances.clear()

    @property
    def name(self) -> str:
        return f"Router({len(self._fleet)} models, mode={self._voice_mode})"

    @property
    def capabilities(self) -> frozenset[Modality]:
        """Union of the entire fleet's caps within the active tier.

        This is what the rest of the server queries (e.g.
        `_handle_user_media` checks `if Modality.VISION in llm.capabilities`).
        Restricting to the current tier means a Local-mode connection
        with no local vision model in fleet correctly reports VISION
        unavailable, even if a cloud vision model exists in fleet.
        """
        tier = TIER_FOR_MODE.get(self._voice_mode)
        if tier is None:
            return frozenset()
        union: set[Modality] = set()
        for spec in self._fleet:
            if spec.tier in tier:
                union |= spec.capabilities
        return frozenset(union)

    # ── Router-specific surface ──────────────────────────────────
    def choose(
        self, required: Iterable[Modality], voice_mode: int
    ) -> ModelSpec | None:
        """Pick the lowest-priority spec satisfying caps + tier."""
        tier = TIER_FOR_MODE.get(voice_mode)
        if tier is None:
            return None
        req_set = frozenset(required)
        candidates = [
            s for s in self._fleet
            if s.tier in tier and req_set <= s.capabilities
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda s: s.priority)

    def set_voice_mode(self, mode: int) -> None:
        """Flip tier policy. Instantiated sub-backends survive."""
        if mode != self._voice_mode:
            logger.info(
                "router: voice_mode %d -> %d (instantiated=%s)",
                self._voice_mode, mode, sorted(self._instances.keys()),
            )
            self._voice_mode = mode

    def summarize(self, voice_mode: int | None = None) -> dict[str, str | None]:
        """For each modality, what model_id would the router pick?

        Used by `_handle_register` and `_handle_config_update` to push
        a `fleet_summary` to Tab5 so the firmware can light up the
        right capability chips dynamically.
        """
        mode = self._voice_mode if voice_mode is None else voice_mode
        out: dict[str, str | None] = {}
        for modality in Modality:
            spec = self.choose({Modality.TEXT, modality}
                               if modality != Modality.TEXT else {Modality.TEXT}, mode)
            out[modality.value] = spec.model_id if spec else None
        return out

    # ── Lazy instantiation ───────────────────────────────────────
    async def _ensure_instance(self, spec: ModelSpec) -> LLMBackend:
        if spec.id in self._instances:
            return self._instances[spec.id]
        async with self._instance_lock:
            # Re-check after acquiring (another coroutine may have just
            # created it).
            if spec.id in self._instances:
                return self._instances[spec.id]
            logger.info("router: instantiating %s (backend=%s, model=%s)",
                        spec.id, spec.backend, spec.model_id)
            sub_config = self._build_sub_config(spec)
            # Late import to avoid factory cycle
            from dragon_voice.llm import create_llm
            backend = create_llm(sub_config)
            await backend.initialize()
            self._instances[spec.id] = backend
            return backend

    def _build_sub_config(self, spec: ModelSpec) -> LLMConfig:
        """Deep-copy parent config + overlay this spec's fields."""
        sub = copy.deepcopy(self._config)
        sub.backend = spec.backend
        sub.fleet = []  # sub-backends are single-model, not routers
        if spec.backend == "ollama":
            sub.ollama_model = spec.model_id
            sub.ollama_keep_alive = f"{spec.keep_alive_s}s"
        elif spec.backend == "openrouter":
            sub.openrouter_model = spec.model_id
        elif spec.backend == "lmstudio":
            sub.lmstudio_model = spec.model_id
        elif spec.backend == "tinkerclaw":
            sub.tinkerclaw_model = spec.model_id
        elif spec.backend == "npu_genie":
            # NPU model identity is baked into the dir/config files;
            # treating spec.model_id as a label only.
            pass
        # Apply per-spec overrides (e.g. lmstudio_url for a LAN backend)
        for k, v in spec.overrides.items():
            if hasattr(sub, k):
                setattr(sub, k, v)
        return sub
