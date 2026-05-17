"""LLM backend factory."""

import logging
import socket
from urllib.parse import urlparse

from dragon_voice.config import LLMConfig
from dragon_voice.llm.base import LLMBackend

logger = logging.getLogger(__name__)

_BACKENDS = {
    "ollama": "dragon_voice.llm.ollama_llm.OllamaBackend",
    "openrouter": "dragon_voice.llm.openrouter_llm.OpenRouterBackend",
    "lmstudio": "dragon_voice.llm.lmstudio_llm.LMStudioBackend",
    "npu_genie": "dragon_voice.llm.npu_genie.NPUGenieBackend",
    "tinkerclaw": "dragon_voice.llm.tinkerclaw_llm.TinkerClawBackend",
    "dual": "dragon_voice.llm.dual.DualModelBackend",
    "router": "dragon_voice.llm.router.CapabilityAwareRouter",
}


def _quick_tcp_probe(url: str, timeout_s: float = 1.5) -> bool:
    """Synchronous TCP-connect probe so create_llm can short-circuit
    when the LM Studio backend isn't reachable.  HTTP-level health
    check would be more accurate but blocks the boot-time event loop;
    a TCP SYN/ACK is enough to detect "llama-server isn't listening"
    which is the actual fallback condition we care about."""
    try:
        parsed = urlparse(url if "://" in url else f"http://{url}")
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 80
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except (OSError, ValueError):
        return False


def create_llm(config: LLMConfig) -> LLMBackend:
    """Create an LLM backend instance from configuration.

    Args:
        config: LLM section of the voice config.

    Returns:
        An uninitialized LLMBackend — caller must await .initialize().

    Raises:
        ValueError: If the requested backend name is unknown.

    Behaviour for backend="lmstudio":
        User directive 2026-05-17: try LM Studio (llama-server) first
        as the Local-first inference path, fall back to Ollama if
        the llama-server socket isn't reachable.  Implemented as a
        startup-time TCP probe — the fallback is one-shot at instance
        creation, not per-call, to avoid latency on every generate.

        Mid-session llama-server crashes are NOT auto-recovered; if
        the service goes down after boot, Dragon needs a manual
        restart to flip back.  Future improvement: add per-request
        circuit-breaker inside lmstudio_llm.generate_stream.
    """
    backend_name = config.backend.lower()
    if backend_name not in _BACKENDS:
        available = ", ".join(sorted(_BACKENDS.keys()))
        raise ValueError(
            f"Unknown LLM backend '{backend_name}'. Available: {available}"
        )

    # LM Studio reachability gate.  When backend="lmstudio" and the
    # llama-server isn't responding on its configured URL, transparently
    # swap to Ollama so Dragon comes up serving SOMETHING instead of
    # blowing up at the first request.
    if backend_name == "lmstudio":
        if not _quick_tcp_probe(config.lmstudio_url):
            logger.warning(
                "LM Studio backend selected but %s unreachable — "
                "falling back to Ollama for this session.  Run "
                "`systemctl status tinkerclaw-llama-server` on Dragon "
                "and `systemctl restart tinkerclaw-voice` to switch back.",
                config.lmstudio_url,
            )
            backend_name = "ollama"

    module_path, class_name = _BACKENDS[backend_name].rsplit(".", 1)

    import importlib

    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)
    return cls(config)


__all__ = ["create_llm", "LLMBackend"]
