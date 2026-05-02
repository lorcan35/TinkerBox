"""Centralized capability detector registry (closes #200).

Each backend registers a pure detector function that maps a model_id
(string) to a `frozenset[Modality]`.  Backends delegate their
`capabilities` property to `detect(backend_type, model_id)` instead of
embedding heuristics inline.

Why: pre-registry, each backend had its own inline `capabilities`
property — substring scans for ollama / lmstudio, a static dict for
openrouter, a prefix-check for tinkerclaw, hard-coded text-only for
npu_genie.  The multi-model router (`router.py`) trusts these
declarations; a misclassification routes a vision turn away from a
vision-capable model (false negative) or *toward* a non-vision one
(false positive that crashes mid-stream).

Centralizing solves three things:

  1. Detectors are pure functions, individually unit-testable.
  2. Adding a new backend is one `register(...)` call.
  3. The registry can be probed for arbitrary model_ids (useful for the
     REST diagnostic surfaces / dashboard model picker).

The OpenRouter static registry (`_OPENROUTER_CAPS` in `openrouter_llm.py`)
stays where it lives — the dict is large + OR-specific knowledge, and
the OR detector simply delegates back to it.  Same source of truth,
accessed through the unified registry interface.

Audit reference: cross-stack 2026-05-01 finding F1 (P0).
"""

from collections.abc import Callable

from .base import Modality

CapabilityDetector = Callable[[str], frozenset[Modality]]

_DETECTORS: dict[str, CapabilityDetector] = {}


def register(backend_type: str, detector: CapabilityDetector) -> None:
    """Register a detector for a backend type.

    Idempotent: last registration wins. Backends that bundle their own
    detector logic should call this at module-import time.
    """
    _DETECTORS[backend_type] = detector


def detect(backend_type: str, model_id: str) -> frozenset[Modality]:
    """Return declared modalities for a (backend, model) pair.

    Unknown backend types default to text-only — the same conservative
    baseline as `LLMBackend.capabilities` itself. An empty model_id is
    forwarded to the detector verbatim (most detectors handle "" by
    returning their default cap set).
    """
    detector = _DETECTORS.get(backend_type)
    if detector is None:
        return frozenset({Modality.TEXT})
    return detector(model_id or "")


def registered_backends() -> list[str]:
    """Return the sorted list of registered backend types."""
    return sorted(_DETECTORS.keys())


# ─────────────────────────────────────────────────────────────────────
# Built-in detectors — extracted from each backend's prior inline logic.
# ─────────────────────────────────────────────────────────────────────

# Shared vision-hint substrings. Matches the ollama / lmstudio convention
# where the served model name typically embeds the upstream HF repo
# (`hf.co/openbmb/MiniCPM-V-4-gguf:Q4_K_M`, etc).  Adding a hint here
# enables vision detection on BOTH backends simultaneously.
_VISION_HINTS: tuple[str, ...] = (
    "llava",
    "bakllava",
    "minicpm-v",
    "minicpm-o",
    "moondream",
    "vision",
    "qwen2-vl",
    "qwen2.5-vl",
    "pixtral",
    "internvl",
)

# Curated list of known function-calling-trained ollama families.
# Conservative — the parser is tolerant enough to wring tool calls out
# of others, but the router should only declare TOOL_CALLING for models
# actually trained for it.
_OLLAMA_TOOLCALL_HINTS: tuple[str, ...] = (
    "ministral",
    "gemma3",
    "xlam",
    "llama3.1",
    "llama3.2",
    "qwen2.5",
    "hermes",
    "functiongemma",
    "lfm2",
    "smollm3",
)

# Known multimodal-upstream prefixes the TinkerClaw gateway can route to.
_TINKERCLAW_VISION_PREFIXES: tuple[str, ...] = (
    "minimax/",
    "anthropic/",
    "openai/gpt-4o",
    "google/gemini",
)


def detect_ollama(model_id: str) -> frozenset[Modality]:
    """Ollama: vision via name substring, audio via minicpm-o, tools via curated list."""
    model_lc = (model_id or "").lower()
    caps = {Modality.TEXT}
    if any(hint in model_lc for hint in _VISION_HINTS):
        caps.add(Modality.VISION)
        caps.add(Modality.VIDEO)  # multi-image input doubles as frame-sampled video
    if "minicpm-o" in model_lc:
        caps.add(Modality.AUDIO_IN)
        caps.add(Modality.AUDIO_OUT)
    if any(hint in model_lc for hint in _OLLAMA_TOOLCALL_HINTS):
        caps.add(Modality.TOOL_CALLING)
    return frozenset(caps)


def detect_lmstudio(model_id: str) -> frozenset[Modality]:
    """LM Studio: same vision hints as ollama, but always declare TOOL_CALLING.

    LM Studio's `/chat/completions` endpoint supports OpenAI-format
    tools across the board regardless of whether the loaded GGUF was
    trained for them — the parser is tolerant; the router can pick
    LM Studio for tool turns even if the model itself is iffy.
    """
    model_lc = (model_id or "").lower()
    caps = {Modality.TEXT, Modality.TOOL_CALLING}
    if any(hint in model_lc for hint in _VISION_HINTS):
        caps.add(Modality.VISION)
        caps.add(Modality.VIDEO)
    if "minicpm-o" in model_lc:
        caps.add(Modality.AUDIO_IN)
        caps.add(Modality.AUDIO_OUT)
    return frozenset(caps)


def detect_npu_genie(_model_id: str) -> frozenset[Modality]:
    """QAIRT / Genie has no vision support — text only."""
    return frozenset({Modality.TEXT})


def detect_tinkerclaw(model_id: str) -> frozenset[Modality]:
    """TinkerClaw gateway: text + tools always; +VISION for known multimodal upstreams."""
    model_lc = (model_id or "").lower()
    caps = {Modality.TEXT, Modality.TOOL_CALLING}
    if any(prefix in model_lc for prefix in _TINKERCLAW_VISION_PREFIXES):
        caps.add(Modality.VISION)
    return frozenset(caps)


def _detect_openrouter(model_id: str) -> frozenset[Modality]:
    """Delegate to the static registry in openrouter_llm.py (35+ entries)."""
    # Lazy import to avoid circular deps at module init.
    from .openrouter_llm import _openrouter_capabilities

    return _openrouter_capabilities(model_id)


# Register built-ins at import time. Backends that prefer to register
# themselves (e.g. plugin backends) can still call register() later.
register("ollama", detect_ollama)
register("lmstudio", detect_lmstudio)
register("npu_genie", detect_npu_genie)
register("tinkerclaw", detect_tinkerclaw)
register("openrouter", _detect_openrouter)
