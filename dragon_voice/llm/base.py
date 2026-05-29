"""Abstract base class for LLM backends, plus optional-feature Protocol mixins.

The `LLMBackend` ABC defines the *required* interface every backend must
implement (initialize, generate_stream, shutdown, name, capabilities).

Optional features that only some backends support — usage tracking,
history trim/clear, session-key injection — are declared as
`@runtime_checkable` Protocol mixins below (closes #204, Wave 21b).
Callsites use `isinstance(backend, SupportsX)` instead of
`hasattr(backend, "method_name")`, which gives the type checker
visibility into what's optional and prevents the silent "this backend
doesn't actually support that method" failure mode.

The concrete backends don't need to inherit the Protocols — Python's
structural Protocol matching means `isinstance(obj, SupportsUsage)`
returns True iff `obj.get_last_usage` exists with a callable signature.
The Protocols document *what* a method must do; the existence + name
of the method is the runtime contract.
"""

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import AsyncIterator, Protocol, runtime_checkable


class Modality(StrEnum):
    """Capabilities a backend can declare for the multi-model router (#183).

    A backend's `capabilities` property returns a frozenset of these.
    The router infers the *required* set from the inbound message
    (image_url content -> +VISION, etc.) and picks the lowest-priority
    backend whose capabilities are a superset.
    """

    TEXT = "text"
    VISION = "vision"
    VIDEO = "video"
    AUDIO_IN = "audio_in"
    AUDIO_OUT = "audio_out"
    TOOL_CALLING = "tool_calling"


class LLMBackend(ABC):
    """Interface that all LLM backends must implement."""

    @abstractmethod
    async def initialize(self) -> None:
        """Verify connectivity and prepare for inference. Called once at startup."""
        ...

    @abstractmethod
    async def generate_stream(
        self, prompt: str, system_prompt: str = ""
    ) -> AsyncIterator[str]:
        """Stream text tokens from the LLM.

        Args:
            prompt: The user's message / transcribed speech.
            system_prompt: System prompt for the conversation. If empty,
                          the backend should use its configured default.

        Yields:
            Text tokens (strings) as they arrive from the model.
        """
        ...

    async def generate_stream_with_messages(
        self, messages: list[dict]
    ) -> AsyncIterator[str]:
        """Stream tokens using a full OpenAI-format message list as context.

        Default implementation formats messages into a single prompt and
        delegates to generate_stream(). Backends that support native multi-turn
        (e.g. Ollama /api/chat) should override this.

        Args:
            messages: List of {role: "system"|"user"|"assistant", content: "..."}.
                     The last message is the current user input.
        """
        # Extract system prompt and current user message
        system_prompt = ""
        current_prompt = ""
        history = []

        for msg in messages:
            if msg["role"] == "system":
                system_prompt = msg["content"]
            elif msg == messages[-1] and msg["role"] == "user":
                current_prompt = msg["content"]
            else:
                history.append(msg)

        if not current_prompt:
            # Fallback: use last message content regardless of role
            current_prompt = messages[-1]["content"] if messages else ""

        # Build a formatted prompt with history context
        parts = []
        if system_prompt:
            parts.append(f"System: {system_prompt}\n")
        for msg in history[-6:]:  # Last 3 turns
            role = "User" if msg["role"] == "user" else "Assistant"
            parts.append(f"{role}: {msg['content']}")
        parts.append(f"User: {current_prompt}")
        parts.append("Assistant:")

        formatted = "\n".join(parts)

        async for token in self.generate_stream(formatted, ""):
            yield token

    @abstractmethod
    async def shutdown(self) -> None:
        """Release resources. Called once at server shutdown."""
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable backend name for logging and status pages."""
        ...

    @property
    def capabilities(self) -> frozenset[Modality]:
        """Modalities this backend can handle.

        Default is text-only. Concrete backends override via
        `dragon_voice.llm.capability_registry` (#200, Wave 21a).
        Used by the multi-model router (#183) to pick a backend per turn.
        """
        return frozenset({Modality.TEXT})

    @property
    def synthesize_summary_locally(self) -> bool:
        """True for slow CPU-local backends where the dictation
        title+summary should be synthesized from the transcript
        directly instead of paying a 60-90 s `generate_stream` round
        trip.

        This is a *capability*, not an identity check.  It replaces the
        old `type(llm).__name__ == 'OllamaBackend'` gate in
        `dictation_post.run_dictation_post_process`, which silently
        broke when the live Local backend moved from Ollama to
        llama-server (`LMStudioBackend`) — reintroducing the very hang
        the gate was written to kill (the slow summary races Tab5's 45 s
        grace timer / the ngrok idle-close window).

        Slow CPU-local backends (Ollama, llama-server, NPU Genie)
        override to True.  Fast remote backends (OpenRouter, TinkerClaw)
        keep the default False so they go through the LLM, which gives a
        nicer title+summary and is fast enough not to race any timeout.

        Wrapper backends (router/dual) inherit the default False today;
        forwarding the property to the chosen sub-backend is a tracked
        follow-up (dictation redesign W3/W4).  The live default
        deployment is a direct backend, so the property resolves
        correctly there.
        """
        return False

    async def health_check(self, timeout_s: float = 2.0) -> tuple[bool, str]:
        """W4-B: cheap reachability probe surfaced by `GET /health`.

        Default returns `(True, "no probe")` so local/in-process backends
        (e.g. NPU Genie) don't need to override — they're considered
        healthy as long as `initialize()` returned without raising.
        Network-backed backends (Ollama, OpenRouter, TinkerClaw) override
        to actually round-trip a cheap request within `timeout_s`.

        Returns
        -------
        (ok, detail)
            ok=True  → probe succeeded or none implemented
            ok=False → reachability failed; `detail` is the error string
                       (≤ ~120 chars; truncated by the caller for display)

        Must never raise — wrap exceptions and report as (False, str(e)).
        """
        return True, "no probe"


# ─────────────────────────────────────────────────────────────────────
# Optional-feature Protocol mixins (#204, Wave 21b).
#
# Each Protocol below describes a method that *some* backends provide.
# Callsites in pipeline.py / server.py check `isinstance(backend, SupportsX)`
# instead of `hasattr(backend, "method_name")`.
#
# Why isinstance over hasattr:
#   - mypy + IDEs can see what's optional and warn at the type level.
#   - The Protocol's docstring documents the contract once, not at every
#     callsite that copy-pasted "if backend has this method...".
#   - A `dual` / wrapper backend that *forwards* an optional feature to
#     its inner responder can cleanly implement the Protocol once.
#
# All Protocols are `@runtime_checkable` so isinstance() works without
# explicit inheritance — the concrete backend just needs the method.
# ─────────────────────────────────────────────────────────────────────


@runtime_checkable
class SupportsNativeTools(Protocol):
    """Backends that support the OpenAI-native `tools=[...]` API.

    Instead of prose-listing tools in the system prompt and parsing
    `<tool>NAME</tool><args>{}</args>` markers out of the streamed text,
    these backends accept structured tool schemas + `tool_choice="auto"`
    and return structured tool calls. This gives the model a schema to
    fill (clean arg extraction) and a native "don't call a tool" path
    (no spurious fires on chit-chat).

    `generate_with_tools` is non-streaming: the tool-decision turn emits
    a tool call, not user-facing prose, so streaming buys nothing. The
    final natural-language turn (after the tool result) is returned as
    `content` in one block.

    Returns a dict:
        {"content": str, "tool_calls": [{"name": str, "args": dict}, ...]}
    `tool_calls` is empty when the model chose to answer directly.
    """

    async def generate_with_tools(
        self, messages: list[dict], tools: list[dict]
    ) -> dict: ...


@runtime_checkable
class SupportsSessionKey(Protocol):
    """Backends that own their own conversation context per-session.

    The session key is an opaque string Dragon's MessageStore uses to
    namespace per-conversation state; backends that maintain server-side
    history (TinkerClaw gateway) need it to associate inbound requests
    with the right ongoing thread.  Backends without this method maintain
    history client-side or are stateless; the call is a no-op for them.
    """

    def set_session_key(self, key: str) -> None: ...


@runtime_checkable
class SupportsUsage(Protocol):
    """Backends that report token usage + model id after each turn.

    Returns a dict with at minimum: `model` (str), `prompt_tokens` (int),
    `completion_tokens` (int), `total_tokens` (int).  Empty dict means
    no usage was captured (yet) — the receipt code skips cost rendering.

    Concrete bug this Protocol prevents (audit finding F5): without it,
    `dual` and `tinkerclaw` backends fall through the hasattr check and
    receipts emit `model="llm"` (the literal string from a fallback path)
    instead of the actual upstream model id.
    """

    def get_last_usage(self) -> dict: ...


@runtime_checkable
class SupportsHistoryTrim(Protocol):
    """Backends that maintain client-side conversation history.

    Drops oldest turns when the in-memory list exceeds `max_turns`.
    Backends that delegate context to MessageStore (the new path via
    ConvEngine) don't need this — it's the legacy single-backend path
    that maintains its own list.
    """

    def trim_history(self, max_turns: int = 10) -> None: ...


@runtime_checkable
class SupportsClearHistory(Protocol):
    """Backends that maintain client-side conversation history.

    Wipes the in-memory turn list — used by the "New Chat" UI flow when
    Tab5 sends `{"type": "clear"}`.  Same caveat as SupportsHistoryTrim:
    only the legacy single-backend path needs it.
    """

    def clear_history(self) -> None: ...
