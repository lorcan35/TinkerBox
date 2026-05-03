"""Dual-model LLM backend: PICKER + RESPONDER.

Composes two `LLMBackend` instances behind the standard interface so the
ConversationEngine can stay unchanged.  See `docs/PLAN-dual-model-pipeline.md`
for the design.

Behavior summary
----------------
Initial turn (last message is `user`):
  1. Stream PICKER (e.g. xLAM-2-1b-fc-r) over the full context.
  2. If the picker emits tool-call markup → yield it verbatim and return.
     ConversationEngine will execute the tool, append the `tool` message,
     and call back in.  We end up in the responder phase below.
  3. If the picker emits a non-tool, conversational reply that looks
     useful → yield it and return.  Cheap chat path (~25 s).
  4. Otherwise (bracket-noise, empty, residual XML) → fall through to
     RESPONDER and yield its reply.

Follow-up turn (last message is `tool`):
  • Skip the picker entirely.  Stream RESPONDER (e.g. ministral-3:3b)
    over the full context (which now includes the tool result) so the
    user gets a warm conversational reply that incorporates what the
    tool returned.

The "useful text" heuristic is the same one server.py uses to decide
whether to invoke the template wrap (added in #79) — keeping the rule in
one shape across the codebase means a picker reply that wouldn't survive
the wrap test never reaches the user from the dual path either.
"""

from __future__ import annotations

import copy
import logging
from typing import AsyncIterator

from dragon_voice.config import LLMConfig
from dragon_voice.llm.base import LLMBackend, Modality

logger = logging.getLogger(__name__)


# SOLID-audit follow-up (PR #267 / SRP-9): dedupped against
# the canonical `tools.response_wrap.looks_like_useful_text`.
# The pre-extract comment claimed importing from server.py
# would cycle — but the canonical lives in `tools.response_wrap`
# which has zero internal deps (only `re` + `typing`), so the
# cycle worry doesn't apply.  Re-exported as
# `_looks_like_useful_text` to preserve the call site
# unchanged at line 238.
from dragon_voice.tools.response_wrap import (  # noqa: F401
    looks_like_useful_text as _looks_like_useful_text,
)


def _has_tool_marker(text: str) -> bool:
    """Lightweight pre-check for whether the picker emitted tool-call
    markup.  Mirrors `ToolRegistry.has_tool_call` so the dual backend
    doesn't have to import the registry (avoids a circular import via
    surfaces / pipeline)."""
    has_legacy = (
        ("<tool>" in text or "[tool>" in text or "[tool]" in text or "<tool]" in text)
        and "</tool>" in text
    )
    has_std = "<tool_call>" in text and "</tool_call>" in text
    return has_legacy or has_std


def _build_subconfig(parent: LLMConfig, backend: str, model: str) -> LLMConfig:
    """Make an LLMConfig for one of the sub-backends.  Reuses every
    other field (URLs, temperature, max_tokens, system prompt) from the
    parent so a single config block configures both halves of the pair."""
    sub = copy.deepcopy(parent)
    sub.backend = backend
    if backend == "ollama":
        sub.ollama_model = model
        # Dual mode keeps both models hot — the default 30 s eviction
        # would have either picker or responder reloading from disk on
        # almost every turn, paying ~30 s twice per round-trip.  5 min
        # comfortably spans a back-to-back gauntlet without giving up
        # so much RAM that other Dragon services suffer.
        sub.ollama_keep_alive = "5m"
    elif backend == "lmstudio":
        sub.lmstudio_model = model
    elif backend == "openrouter":
        sub.openrouter_model = model
    else:
        # Other backends (npu_genie, tinkerclaw) ignore model overrides
        # because their model identity is baked into other fields.
        # The caller is on their own to keep the parent config sane.
        pass
    return sub


class DualModelBackend(LLMBackend):
    """LLMBackend that orchestrates a fast tool-picker and a warm responder.

    Both halves are themselves `LLMBackend` instances built via
    `create_llm`, so any combination supported elsewhere (ollama +
    ollama, ollama + openrouter, …) works out of the box.
    """

    def __init__(self, config: LLMConfig) -> None:
        # Late import: `from dragon_voice.llm import create_llm` would
        # cycle through this module via the registry.  Importing the
        # factory function directly from the package's __init__ at call
        # time (not module load time) avoids it.
        from dragon_voice.llm import create_llm

        self._config = config

        picker_cfg = _build_subconfig(
            config,
            backend=config.dual_picker_backend or "ollama",
            model=config.dual_picker_model,
        )
        responder_cfg = _build_subconfig(
            config,
            backend=config.dual_responder_backend or "ollama",
            model=config.dual_responder_model,
        )
        self._picker: LLMBackend = create_llm(picker_cfg)
        self._responder: LLMBackend = create_llm(responder_cfg)

    @property
    def name(self) -> str:
        return f"Dual(picker={self._picker.name} | responder={self._responder.name})"

    @property
    def capabilities(self) -> frozenset[Modality]:
        """Forward to the responder.

        The responder is the model whose tokens reach the user; if it
        can't see images, the dual pipeline can't see images. Picker is
        text-only by design (xLAM-style FC head) and doesn't constrain
        the user-visible modality set.
        """
        return self._responder.capabilities

    # ── Optional-feature forwarders (Wave 21b, #204) ─────────────
    # The responder owns the user-visible state; we delegate the four
    # optional Protocols to it.  Each forwarder is a no-op if the
    # responder doesn't implement the matching method — mirrors the
    # callsite-level isinstance() guard in pipeline.py / server.py.

    def get_last_usage(self) -> dict:
        """Forward to responder if it tracks usage; else empty dict."""
        method = getattr(self._responder, "get_last_usage", None)
        return method() if callable(method) else {}

    def trim_history(self, max_turns: int = 10) -> None:
        """Forward to responder if it maintains client-side history."""
        method = getattr(self._responder, "trim_history", None)
        if callable(method):
            method(max_turns)

    def clear_history(self) -> None:
        """Forward to responder if it maintains client-side history."""
        method = getattr(self._responder, "clear_history", None)
        if callable(method):
            method()

    async def initialize(self) -> None:
        await self._picker.initialize()
        await self._responder.initialize()
        logger.info("DualModelBackend ready: %s", self.name)

    async def shutdown(self) -> None:
        # Run both shutdowns even if the first one raises so we don't
        # leak the responder's HTTP session when the picker goes wrong.
        first_err: Exception | None = None
        for sub in (self._picker, self._responder):
            try:
                await sub.shutdown()
            except Exception as e:
                first_err = first_err or e
                logger.warning("Sub-backend shutdown error (%s): %s", sub.name, e)
        if first_err:
            raise first_err

    async def generate_stream(
        self, prompt: str, system_prompt: str = ""
    ) -> AsyncIterator[str]:
        """Single-prompt entrypoint — synthesize a 1-message context and
        delegate to the message-list path so the dual logic lives in one
        place."""
        messages: list[dict] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        async for tok in self.generate_stream_with_messages(messages):
            yield tok

    async def generate_stream_with_messages(
        self, messages: list[dict]
    ) -> AsyncIterator[str]:
        # Responder phase: ConversationEngine has appended a tool result
        # to the context after firing whatever the picker chose.  Skip
        # the picker entirely — the responder gets the full context
        # (incl. the tool_result block) and writes the user-facing reply.
        if messages and messages[-1].get("role") == "tool":
            logger.debug("Dual: tool-result detected → responder only")
            async for tok in self._responder.generate_stream_with_messages(messages):
                yield tok
            return

        # Picker phase: stream the picker into a buffer so we can
        # inspect the full output before deciding which path to take.
        # ConversationEngine already buffers LLM output before yielding
        # to the WebSocket when a tool registry is present (see
        # conversation.py:258-266), so the buffer here adds no
        # user-visible latency over single-model behavior.
        logger.debug("Dual: running picker (%s)", self._picker.name)
        picker_buf: list[str] = []
        async for tok in self._picker.generate_stream_with_messages(messages):
            picker_buf.append(tok)
        picker_text = "".join(picker_buf)

        if _has_tool_marker(picker_text):
            logger.debug("Dual: picker emitted tool marker → yield verbatim")
            for tok in picker_buf:
                yield tok
            return

        if _looks_like_useful_text(picker_text):
            logger.debug("Dual: picker text is useful → cheap chat path")
            for tok in picker_buf:
                yield tok
            return

        logger.debug(
            "Dual: picker text junk/empty (%r) → fall through to responder",
            picker_text[:60],
        )
        async for tok in self._responder.generate_stream_with_messages(messages):
            yield tok
