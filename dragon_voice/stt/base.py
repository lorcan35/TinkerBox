"""Abstract base class for Speech-to-Text backends."""

from abc import ABC, abstractmethod


class STTBackend(ABC):
    """Interface that all STT backends must implement."""

    @abstractmethod
    async def initialize(self) -> None:
        """Load models and prepare for inference. Called once at startup."""
        ...

    @abstractmethod
    async def transcribe(self, audio_bytes: bytes, sample_rate: int = 16000) -> str:
        """Transcribe PCM 16-bit signed integer audio to text.

        Args:
            audio_bytes: Raw PCM int16 audio data, mono channel.
            sample_rate: Sample rate of the input audio (default 16kHz).

        Returns:
            Transcribed text string, stripped of leading/trailing whitespace.
        """
        ...

    @abstractmethod
    async def shutdown(self) -> None:
        """Release resources. Called once at server shutdown."""
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable backend name for logging and status pages."""
        ...

    async def health_check(self, timeout_s: float = 2.0) -> tuple[bool, str]:
        """W4-B: cheap reachability probe surfaced by `GET /health`.

        Default returns `(True, "no probe")` so local/in-process backends
        (Moonshine, Whisper.cpp, Vosk) don't need to override — they're
        considered healthy as long as `initialize()` returned without
        raising.  Network-backed backends (OpenRouter STT) override to
        actually round-trip a cheap request within `timeout_s`.

        Must never raise — wrap exceptions and report as (False, str(e)).
        """
        return True, "no probe"
