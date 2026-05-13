"""Abstract base class for Text-to-Speech backends."""

from abc import ABC, abstractmethod


class TTSBackend(ABC):
    """Interface that all TTS backends must implement."""

    @abstractmethod
    async def initialize(self) -> None:
        """Load models and prepare for synthesis. Called once at startup."""
        ...

    @abstractmethod
    async def synthesize(self, text: str) -> bytes:
        """Synthesize text to audio.

        Args:
            text: Text string to convert to speech.

        Returns:
            Raw PCM 16-bit signed integer audio bytes at the backend's
            native sample rate (see .sample_rate property).
        """
        ...

    @abstractmethod
    async def shutdown(self) -> None:
        """Release resources. Called once at server shutdown."""
        ...

    @property
    @abstractmethod
    def sample_rate(self) -> int:
        """Native output sample rate of this backend in Hz."""
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable backend name for logging and status pages."""
        ...

    async def health_check(self, timeout_s: float = 2.0) -> tuple[bool, str]:
        """W4-B: cheap reachability probe surfaced by `GET /health`.

        Default returns `(True, "no probe")` so local/in-process backends
        (Piper, Kokoro) don't need to override — they're considered
        healthy as long as `initialize()` returned without raising.
        Network-backed backends (OpenRouter TTS, Edge TTS) override to
        actually round-trip a cheap request within `timeout_s`.

        Must never raise — wrap exceptions and report as (False, str(e)).
        """
        return True, "no probe"
