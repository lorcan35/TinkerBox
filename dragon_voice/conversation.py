"""Conversation engine for TinkerClaw.

Input-agnostic processing: receives text (from STT or keyboard),
loads context from MessageStore, sends to LLM, stores response.
Supports tool-calling and memory-augmented context.

refs #17, #18
"""

import logging
import re
import time
from typing import AsyncIterator, Optional

from dragon_voice.db import Database
from dragon_voice.messages import (
    MessageStore,
    CONTEXT_BUDGET_LOCAL,
    CONTEXT_BUDGET_CLOUD,
    trim_context_to_budget,
)
from dragon_voice.llm import create_llm, LLMBackend
from dragon_voice.llm.base import SupportsNativeTools
from dragon_voice.config import LLMConfig

logger = logging.getLogger(__name__)

MAX_TOOL_CALLS = 3  # Prevent infinite tool-call loops

# ── Native tool-calling recipe (TinkerBox spec 2026-05-27) ─────────────
# Lifts the 27-tool gauntlet from ~12/19 to 19/20 on Granite-4.0-Nano-1B.
# Three parts, applied only on the native (tools=[...]) path:
#   1) _NATIVE_TOOL_GUIDANCE — appended to the system prompt: when to call a
#      tool + read-vs-action / remember-vs-recall disambiguation + no-fire on
#      chit-chat.
#   2) _NATIVE_TOOL_DESC — intent-keyed descriptions for confusable tools
#      (the native router leans heavily on tool descriptions).
#   3) _NATIVE_FEWSHOT — message-level few-shot exemplars (the decisive lever;
#      rules alone didn't fix read-vs-action, real tool_call examples did).
_NATIVE_TOOL_GUIDANCE = (
    "\n\nTOOL USE: Always call the matching tool when the user asks about their "
    "calendar, email, tasks, the weather, the time, a calculation, or to "
    "remember/recall a fact — never answer those from your own knowledge. Match "
    "intent precisely: schedule/book/set up a NEW event = calendar_create (NOT "
    "calendar_today); cancel/delete an event = calendar_cancel; read/list "
    "existing events = calendar_today or calendar_week; find/search email or "
    "'did X email me' = gmail_search (NOT gmail_unread); new/unread email = "
    "gmail_unread; mark a task done/complete = tasks_complete (NOT tasks_list); "
    "save/'remember that' a fact = remember; 'what do you know'/recall = recall. "
    "For jokes, opinions, greetings, thanks, or chit-chat, do NOT call any tool "
    "— just reply briefly."
)
_NATIVE_TOOL_DESC = {
    "calendar_today": "List/read EXISTING calendar events for today. Read-only; does NOT create events.",
    "calendar_week": "List/read EXISTING calendar events for this week. Read-only.",
    "calendar_create": "Create/schedule/book a NEW calendar event or appointment.",
    "calendar_cancel": "Cancel, delete, or remove an existing calendar event.",
    "gmail_unread": "List the user's UNREAD/new emails ('any new mail', 'unread').",
    "gmail_search": "Search the inbox by keyword/sender/subject ('did X email me', 'find email about Y').",
    "gmail_send": "Compose and SEND a new email; extract recipient, subject, body.",
    "gmail_read": "Read the full body of one specific email.",
    "tasks_list": "List the user's existing to-do tasks. Read-only.",
    "tasks_add": "Add a NEW to-do task.",
    "tasks_complete": "Mark an existing to-do task as done/complete/finished.",
    "remember": "Store/save a NEW fact about the user for later.",
    "recall": "Retrieve previously stored facts about the user.",
}


def _native_fewshot() -> list[dict]:
    """Read-vs-action + no-tool-on-chitchat exemplars in OpenAI tool format."""
    import json as _j

    def _tc(name, args):
        return {"role": "assistant", "content": None, "tool_calls": [
            {"id": "fs", "type": "function",
             "function": {"name": name, "arguments": _j.dumps(args)}}]}

    def _tr():
        return {"role": "tool", "tool_call_id": "fs", "content": "{\"ok\":true}"}

    return [
        {"role": "user", "content": "book a haircut for Friday at 2pm"},
        _tc("calendar_create", {"summary": "Haircut", "start_iso": "Friday 14:00"}), _tr(),
        {"role": "user", "content": "cancel my 4pm meeting"},
        _tc("calendar_cancel", {"query": "4pm meeting"}), _tr(),
        {"role": "user", "content": "mark the dishes task as done"},
        _tc("tasks_complete", {"title": "dishes"}), _tr(),
        {"role": "user", "content": "tell me a joke"},
        {"role": "assistant",
         "content": "Why don't scientists trust atoms? Because they make up everything!"},
    ]

# Audit D5 fix: the ToolRegistry's tolerant parser accepts `<tool>`/`</tool>`
# + `<args>`/`</args>` with minor whitespace + stray closing chars. When we
# fall through to "no tool call (or max reached)" and yield the buffered
# response to the client, any raw tool XML in that text lands in the chat
# bubble. Strip both blocks before yielding so the user never sees markup.
_TOOL_MARKUP_RE = re.compile(
    r"<tool>[\s\S]*?</tool>\s*<args>[\s\S]*?</args>\s*>?", re.IGNORECASE
)


def _strip_tool_markup(text: str) -> str:
    """Remove `<tool>...</tool><args>...</args>` blocks from user-visible text."""
    return _TOOL_MARKUP_RE.sub("", text)


# Phase 2 H1 (issue #94): tool-call marker openers we recognize.  When
# any of these appears in the streaming buffer we hold back tokens from
# that position onward so we don't yield half-formed tool markup to the
# user.  See `_split_at_marker_boundary` for the rolling-detection
# semantics.
#
# Dialect coverage (must stay in sync with `tools/registry.py`):
#   1. Legacy + xLAM bracket quirks: `<tool>`, `[tool>`, `<tool]`, `[tool]`
#   2. Standard FC: `<tool_call>`
#   3. Bracketed-name (xLAM dialect 3): `[NAME]` — added at runtime
#      from the tool registry by `_split_at_marker_boundary`
_TOOL_MARKER_OPENERS_STATIC: tuple[str, ...] = (
    "<tool>",
    "[tool>",
    "<tool]",
    "[tool]",
    "<tool_call>",
)


def _split_at_marker_boundary(
    accumulated: str,
    registered_tool_names: set[str] | None = None,
) -> tuple[str, str]:
    """Split `accumulated` into (flushable, held) at the earliest tool-marker
    boundary so the caller can stream the flushable prefix while waiting
    for the held suffix to either complete into a parseable tool call or
    turn out to be benign prose.

    The boundary is the leftmost position of:
      * any complete known opener anywhere in the string, OR
      * a strict-prefix match of any known opener at the END of the string
        (i.e. an in-flight opener that hasn't completed yet)

    If neither condition fires, returns `(accumulated, "")` — everything
    is safe to flush right now.

    Notes
    -----
    * Stateless: callers re-invoke after each token append, passing the
      full accumulated buffer.  Cheap because the registered-tool set is
      small (~15 entries) and the openers are short.
    * Conservative on registered-tool dialect-3 partials: matches only on
      `[NAME]` (complete close-bracket) — we don't try to match a `[NAM`
      partial because false positives on prose like "[New York]" would
      stutter the stream visibly.  The complete-marker check still catches
      `[NAME]` once the close-bracket arrives.
    """
    # Two opener sets — they have different partial-matching semantics:
    #   * static openers participate in BOTH complete + tail-partial matching
    #   * dialect-3 `[NAME]` openers participate ONLY in complete matching
    #     because partial-matching them (e.g. holding back `[recal` on the
    #     way to `[recall]`) would also stutter on benign prose like
    #     `[Star Trek` or `[Mr. Smith` and visibly break streaming.
    static_openers: list[str] = list(_TOOL_MARKER_OPENERS_STATIC)
    dialect3_openers: list[str] = (
        [f"[{n}]" for n in registered_tool_names] if registered_tool_names else []
    )

    hold_at = len(accumulated)

    # 1) Earliest complete opener anywhere (both sets).
    for op in static_openers + dialect3_openers:
        idx = accumulated.find(op)
        if 0 <= idx < hold_at:
            hold_at = idx

    # 2) Tail partial — strict prefix of any STATIC opener at the end
    #    of the string.  Dialect-3 openers deliberately excluded; see
    #    note above.
    max_op_len = max((len(o) for o in static_openers), default=0)
    tail_window = accumulated[-max_op_len:] if max_op_len else ""
    for op in static_openers:
        for partial_len in range(1, len(op)):
            if tail_window.endswith(op[:partial_len]):
                tail_start = len(accumulated) - partial_len
                if tail_start < hold_at:
                    hold_at = tail_start
                break  # found longest partial for this opener; move on

    return accumulated[:hold_at], accumulated[hold_at:]


class ConversationEngine:
    """Processes text input through the LLM with persistent context.

    Input-agnostic: works for both voice (post-STT) and text (keyboard/API).
    Stores all messages in the MessageStore for history and resume.
    Supports tool-calling (ToolRegistry) and memory-augmented context (MemoryService).
    """

    def __init__(
        self,
        db: Database,
        message_store: MessageStore,
        llm_config: LLMConfig,
        tool_registry=None,
        memory_service=None,
        media_store=None,
    ) -> None:
        self._db = db
        self._messages = message_store
        self._llm_config = llm_config
        self._llm: Optional[LLMBackend] = None
        self._tool_registry = tool_registry
        self._memory_service = memory_service
        # #183 PR 3: when set, multimodal user messages stored via
        # add_message(media_id=...) get hydrated back to OpenAI
        # image_url content arrays at context-build time, enabling
        # cross-modal continuity (photo turn -> text follow-up that
        # still sees the photo).
        self._media_store = media_store

    async def initialize(self) -> None:
        """Create and initialize the LLM backend."""
        self._llm = create_llm(self._llm_config)
        await self._llm.initialize()
        logger.info("ConversationEngine initialized with LLM: %s", self._llm.name)

    async def shutdown(self) -> None:
        """Shut down the LLM backend."""
        if self._llm:
            await self._llm.shutdown()
            self._llm = None

    @property
    def llm(self) -> Optional[LLMBackend]:
        """Access the underlying LLM backend (for pipeline integration)."""
        return self._llm

    async def swap_llm(
        self,
        new_config: LLMConfig,
        *,
        pool: dict[str, LLMBackend],
        voice_mode: Optional[int] = None,
    ) -> tuple[LLMBackend, bool]:
        """Hot-swap the LLM backend with pool reuse (#202, Wave 22b).

        Single canonical swap path used by both the WS `config_update`
        handler and the HTTP `/api/v1/config` PUT handler.  Pre-Wave-22b
        the WS path was 40 lines of inline `_conversation._llm = ...`
        assignment that bypassed encapsulation; the HTTP path had its
        own variant in `pipeline.swap_backends`.  Drift between the two
        was a real source of "config_update worked from one but not
        the other" bugs.

        Behavior:
          * If the active backend is a CapabilityAwareRouter, no swap
            happens — `set_voice_mode(voice_mode)` is called (when
            provided) and `_llm_config` is updated.  The fleet stays
            warm.  Returns `(self._llm, False)` ("not pooled" since we
            kept the same instance).
          * Otherwise, look up `_llm_sig(new_config)` in `pool`.  If
            present, reuse it.  If absent, `create_llm(new_config)` +
            `initialize()` + insert into pool.  Shut down the old
            backend iff nothing else in the pool references it.
            Replace `self._llm`.  Update `self._llm_config` so the
            tool-prompt selection in `_augment_context_with_tools`
            picks the right format for the new backend.

        Args:
            new_config: The desired LLM config (one slice of VoiceConfig.llm).
            pool: The shared backend instance pool — the caller (server.py)
                  owns this dict; we mutate it.
            voice_mode: Required for router mode; ignored for non-router.
                        Pass the active voice_mode (0..4) so the router
                        re-keys its tier filter.

        Returns:
            (new_llm_instance, was_pooled) for diagnostic logging.

        Raises:
            Whatever `create_llm` or `initialize` raises — the caller
            is expected to wrap in try/except for graceful degrade.
        """
        # Lazy import to avoid pulling the router into module init.
        from dragon_voice.llm.router import CapabilityAwareRouter

        if isinstance(self._llm, CapabilityAwareRouter):
            if voice_mode is not None:
                self._llm.set_voice_mode(voice_mode)
            self._llm_config = new_config
            logger.info(
                "router: voice_mode set to %s (no backend swap)",
                voice_mode,
            )
            return self._llm, False

        # Lazy imports — pipeline imports conversation, so importing
        # pipeline at module top would cycle.
        from dragon_voice.llm import create_llm
        from dragon_voice.pipeline import _llm_sig

        new_key = _llm_sig(new_config)
        pooled = pool.get(new_key)
        old_llm = self._llm

        if pooled is not None:
            new_llm = pooled
            was_pooled = True
        else:
            new_llm = create_llm(new_config)
            await new_llm.initialize()
            pool[new_key] = new_llm
            was_pooled = False

        # Shut down the OLD backend only if nothing else in the pool
        # holds a reference to it (i.e. it wasn't pooled or it was the
        # last reference).  This preserves warm-loaded models across
        # rapid mode toggles.
        if old_llm is not None and old_llm is not new_llm and old_llm not in pool.values():
            try:
                await old_llm.shutdown()
            except Exception as e:  # noqa: BLE001 — best-effort cleanup
                logger.warning("Old LLM shutdown failed during swap: %s", e)

        self._llm = new_llm
        # 2026-04-23 (#58): also swap _llm_config so the compact-vs-full
        # tool prompt logic in _augment_context_with_tools picks the right
        # format for the active backend.
        self._llm_config = new_config

        logger.info(
            "ConversationEngine LLM swapped to %s%s (backend=%s)",
            new_llm.name,
            " (pooled)" if was_pooled else "",
            new_config.backend,
        )
        return new_llm, was_pooled

    def fleet_summary(self, voice_mode: int) -> Optional[dict]:
        """Per-modality fleet summary for protocol advertisement (#202, Wave 22b).

        Returns the router's `summarize(voice_mode)` dict when the active
        backend is a CapabilityAwareRouter; returns None otherwise.

        Used by the WS `session_start` and `config_update` ACK paths to
        advertise per-modality model selection to Tab5 — replaces 3
        sites of direct `isinstance(self._conversation._llm, CapabilityAwareRouter)`
        in `server.py`.
        """
        from dragon_voice.llm.router import CapabilityAwareRouter

        if isinstance(self._llm, CapabilityAwareRouter):
            return self._llm.summarize(voice_mode)
        return None

    def choose_vision_model(self, voice_mode: int):
        """Return the `ModelSpec` for a vision turn at this voice_mode.

        Closes audit ENC-1 (2026-05-03): pre-extract `server.py` reached
        directly into `self._conversation._llm` and called `.choose(...)`
        on the router instance from three different sites.  That violated
        encapsulation (private attribute access) AND DIP (high-level
        WS handler depending on a concrete `CapabilityAwareRouter` type
        rather than a stable public surface).

        Returns:
            * The chosen `ModelSpec` (with `.model_id`, `.tier`, etc.) when
              the active backend is a `CapabilityAwareRouter` AND a
              vision-capable model exists in its fleet for the given tier.
            * `None` when the active backend isn't a router (single-backend
              configurations), or when the router has no vision-capable
              candidate for this tier.

        Callers (`server.py`'s `_handle_config_update` vision-capability
        emit) interpret `None` as "no router-managed vision model
        available — fall back to the substring-based capability gate".

        :returns: `ModelSpec | None`
        """
        from dragon_voice.llm.base import Modality
        from dragon_voice.llm.router import CapabilityAwareRouter

        if not isinstance(self._llm, CapabilityAwareRouter):
            return None
        return self._llm.choose({Modality.TEXT, Modality.VISION}, voice_mode)

    async def process_text(
        self,
        session_id: str,
        text: str,
        input_mode: str = "text",
        audio_duration_s: Optional[float] = None,
    ) -> str:
        """Process text input with tool-calling and memory support (non-streaming).

        Returns the full response text after any tool calls are resolved.
        """
        if not self._llm:
            raise RuntimeError("ConversationEngine not initialized")

        # Store user message
        await self._messages.add_message(
            session_id=session_id,
            role="user",
            content=text,
            input_mode=input_mode,
            audio_duration_s=audio_duration_s,
        )

        await self._db.touch_session(session_id)

        # Build context with memory + tool descriptions
        context = await self._build_context(session_id, text)

        t0 = time.monotonic()
        tool_calls_made = 0

        while True:
            full_response = []
            async for token in self._llm.generate_stream_with_messages(context):
                full_response.append(token)

            response_text = "".join(full_response)

            # Check for tool calls
            if (self._tool_registry
                    and self._tool_registry.has_tool_call(response_text)
                    and tool_calls_made < MAX_TOOL_CALLS):

                tool_calls = self._tool_registry.parse_tool_calls(response_text)
                if tool_calls:
                    tool_call = tool_calls[0]
                    tool_calls_made += 1
                    logger.info("Tool call (sync): %s(%s)", tool_call["tool"], tool_call["args"])

                    # Wave 12 skill SDK: inject session_id so skills can
                    # resolve the per-Tab5 surface via
                    # SurfaceManager.surface_for(session_id, skill_id).
                    # Without this the skill pulls "unknown" and no
                    # widget ever reaches the user.  Existing tools that
                    # don't need session continue to ignore the key.
                    _args = dict(tool_call["args"] or {})
                    _args.setdefault("session_id", session_id)
                    result = await self._tool_registry.execute(tool_call["tool"], _args)

                    import json as _json
                    await self._messages.add_message(
                        session_id=session_id, role="assistant",
                        content=response_text, input_mode="system",
                        model=self._llm.name,
                    )
                    await self._messages.add_message(
                        session_id=session_id, role="tool",
                        content=f"<tool_result>{_json.dumps(result.get('result', result))}</tool_result>",
                        input_mode="system",
                    )
                    context = await self._messages.get_context(session_id)
                    continue

            break

        latency_ms = (time.monotonic() - t0) * 1000

        await self._messages.add_message(
            session_id=session_id,
            role="assistant",
            content=response_text,
            input_mode="system",
            model=self._llm.name,
            latency_ms=latency_ms,
        )

        logger.info(
            "Conversation turn (session=%s, latency=%.0fms, tools=%d): '%s' → '%s'",
            session_id, latency_ms, tool_calls_made, text[:50], response_text[:50],
        )
        return response_text

    async def _build_context(
        self, session_id: str, user_text: str, inject_tool_prose: bool = True
    ) -> list[dict]:
        """Build LLM context with optional memory augmentation and tool descriptions.

        After assembling the full context (system prompt + memory + tools + history),
        trims oldest messages to fit within the model's token budget (US-P16).

        `inject_tool_prose=False` skips the prose [TOOLS] block — used by the
        native tool-calling path, which sends tool schemas via the API
        `tools=[...]` parameter instead of describing them in the prompt.
        """
        # Mode-aware context depth: local models have tiny context windows,
        # cloud models (128K+) can use much more conversation history.
        is_local = self._llm_config.backend in ("ollama", "npu_genie", "lmstudio")
        max_msgs = 10 if is_local else 30
        context = await self._messages.get_context(
            session_id, max_messages=max_msgs, media_store=self._media_store
        )

        # Inject memory context before the user's message
        if self._memory_service:
            try:
                memory_ctx = await self._memory_service.get_relevant_context(user_text)
                if memory_ctx:
                    # Augment system prompt with memory context
                    if context and context[0]["role"] == "system":
                        context[0]["content"] += "\n\n" + memory_ctx
            except Exception as e:
                logger.warning("Memory context retrieval failed: %s", e)

        # Inject tool descriptions into system prompt
        # Use compact format for local models to save tokens
        if self._tool_registry and inject_tool_prose:
            is_local = self._llm_config.backend in ("ollama", "npu_genie", "lmstudio")
            tool_desc = self._tool_registry.format_for_llm(compact=is_local)
            if tool_desc and context and context[0]["role"] == "system":
                context[0]["content"] += "\n" + tool_desc

        # ── Token budget enforcement (US-P16) ─────────────────────────
        # After all augmentation (memory, tools), trim oldest messages so
        # total context fits within the model's context window.
        budget = CONTEXT_BUDGET_LOCAL if is_local else CONTEXT_BUDGET_CLOUD
        context = trim_context_to_budget(context, budget)

        return context

    async def process_text_stream(
        self,
        session_id: str,
        text: str,
        input_mode: str = "text",
        audio_duration_s: Optional[float] = None,
        on_tool_call=None,
        on_tool_result=None,
        on_tool_error=None,
        media_id: Optional[str] = None,
    ) -> AsyncIterator[str]:
        """Process text input with streaming response and tool-calling support.

        Args:
            session_id: Active session to converse in.
            text: The user's message.
            input_mode: 'voice' or 'text'.
            audio_duration_s: Duration of voice input (None for text).
            on_tool_call: Optional async callback(call_dict) for tool call events.
            on_tool_result: Optional async callback(result_dict) for tool result events.
            on_tool_error: Optional async callback(error_dict) — fires once
                per failed tool-call parse (γ2-M1, issue #104).  Pre-fix
                these failures were silent `logger.warning` lines; the
                WS handler wires this to a `tool_args_invalid` error
                frame so the user sees a transient toast instead of an
                empty/generic LLM reply.

        Yields:
            Text tokens as they arrive from the LLM.
        """
        if not self._llm:
            raise RuntimeError("ConversationEngine not initialized")

        # Native tool-calling path (llm.native_tools + a backend that
        # supports the OpenAI tools=[...] API). Isolated method so the
        # audit-hardened prose/marker loop below is untouched and stays
        # the fallback for every other backend/model.
        _nt = getattr(self._llm_config, "native_tools", False)
        logger.info(
            "TT-ROUTE native_tools=%s backend=%s llm=%s supports_native=%s reg=%s",
            _nt, getattr(self._llm_config, "backend", None),
            type(self._llm).__name__,
            isinstance(self._llm, SupportsNativeTools),
            bool(self._tool_registry),
        )
        if (
            _nt
            and self._tool_registry
            and isinstance(self._llm, SupportsNativeTools)
        ):
            async for token in self._process_text_stream_native(
                session_id, text, input_mode, audio_duration_s,
                on_tool_call, on_tool_result, on_tool_error, media_id,
            ):
                yield token
            return

        # Store user message — multimodal turns pass media_id to encode
        # the content with the multimodal marker (#183 PR 3). Uses
        # input_mode="text" because the schema's CHECK constraint is
        # ('voice','text','system') only; the multimodal nature is
        # captured in the marker-prefixed content. Follow-up: add a
        # DB migration adding 'vision' to the allowed set.
        await self._messages.add_message(
            session_id=session_id,
            role="user",
            content=text,
            input_mode="text" if media_id else input_mode,
            audio_duration_s=audio_duration_s,
            media_id=media_id,
        )

        # Touch session activity
        await self._db.touch_session(session_id)

        # Build context with memory + tool descriptions
        context = await self._build_context(session_id, text)

        t0 = time.monotonic()
        tool_calls_made = 0

        # Phase 2 H1 (issue #94): registered-tool name set is needed at
        # streaming time for dialect-3 ([NAME]{json}) marker detection.
        # Captured once per turn; live registry mutations between turns
        # are fine because we re-fetch each iteration's loop.
        tool_names: set[str] = (
            set(self._tool_registry._tools.keys())
            if self._tool_registry else set()
        )

        while True:
            # Stream LLM response
            full_response = []
            # Phase 2 H1 (issue #94): rolling buffer for marker-aware
            # streaming.  Pre-fix this loop suppressed every yield when
            # `tool_registry` was set, accumulated the entire LLM output
            # in `full_response`, and emitted a single yield at the
            # bottom — Tab5 stared at a silent caption for 60-90 s on
            # every tool-calling local turn.
            #
            # New behaviour: stream tokens immediately UNTIL a known
            # tool-marker opener appears in the rolling buffer.  Hold
            # back from the marker-start onward; once the LLM finishes
            # we either parse + execute a tool call (existing path) or
            # treat the held buffer as benign prose and flush it (with
            # `_strip_tool_markup` for safety).
            #
            # `_split_at_marker_boundary` is the rolling-detection
            # primitive — see its docstring for boundary rules.
            held = ""
            async for token in self._llm.generate_stream_with_messages(context):
                full_response.append(token)
                if not self._tool_registry:
                    # No registry → no tool detection needed; raw stream.
                    yield token
                    continue
                held += token
                flushable, held = _split_at_marker_boundary(held, tool_names)
                if flushable:
                    yield flushable

            response_text = "".join(full_response)

            # Audit B4 (#137): if the LLM emitted ANOTHER tool-call after
            # MAX_TOOL_CALLS was already hit, surface that to the user
            # instead of silently stripping the markup.  Pre-fix the
            # loop just fell through to the strip path and the user saw
            # a reply that read as if it had been cut off mid-thought
            # ("Let me check the calendar… <silence>").  Now we emit a
            # γ-arch TRANSIENT/TOOL error so Tab5 can render a toast
            # like "Reached the 3-tool limit for this turn — try again."
            if (self._tool_registry
                    and self._tool_registry.has_tool_call(response_text)
                    and tool_calls_made >= MAX_TOOL_CALLS):
                logger.warning(
                    "MAX_TOOL_CALLS=%d reached on session %s — emitting "
                    "tool_call_limit error and stopping the chain",
                    MAX_TOOL_CALLS, session_id,
                )
                if on_tool_error is not None:
                    try:
                        await on_tool_error({
                            "name": "(chain)",
                            "code": "tool_call_limit_reached",
                            "message": (
                                f"Reached the {MAX_TOOL_CALLS}-tool limit for "
                                "this turn — please ask again to continue."
                            ),
                            "limit": MAX_TOOL_CALLS,
                        })
                    except Exception as e:
                        logger.debug("on_tool_error (limit) callback error: %s", e)

            # Check for tool calls
            if (self._tool_registry
                    and self._tool_registry.has_tool_call(response_text)
                    and tool_calls_made < MAX_TOOL_CALLS):

                # γ2-M1 (issue #104): use the error-surfacing variant so
                # malformed tool-call attempts emit a `tool_args_invalid`
                # WS frame instead of silently disappearing into a
                # logger.warning.  Errors are reported even when there's
                # at least one successful call in the same response —
                # the user wants to know "tool A worked, tool B was
                # skipped" rather than just seeing the partial result.
                tool_calls, tool_errors = (
                    self._tool_registry.parse_tool_calls_with_errors(response_text)
                )
                if tool_errors and on_tool_error:
                    for err in tool_errors:
                        try:
                            await on_tool_error(err)
                        except Exception as e:
                            logger.debug("on_tool_error callback error: %s", e)
                if tool_calls:
                    tool_call = tool_calls[0]  # Execute one at a time
                    tool_calls_made += 1
                    logger.info("Tool call detected: %s(%s)", tool_call["tool"], tool_call["args"])

                    # Notify caller (per-connection, not shared)
                    if on_tool_call:
                        try:
                            await on_tool_call(tool_call)
                        except Exception as e:
                            logger.debug("Callback error: %s", e)

                    # Execute the tool
                    # Wave 12 skill SDK: inject session_id so skills can
                    # resolve the per-Tab5 surface via
                    # SurfaceManager.surface_for(session_id, skill_id).
                    # Without this the skill pulls "unknown" and no
                    # widget ever reaches the user.  Existing tools that
                    # don't need session continue to ignore the key.
                    _args = dict(tool_call["args"] or {})
                    _args.setdefault("session_id", session_id)
                    result = await self._tool_registry.execute(tool_call["tool"], _args)

                    if on_tool_result:
                        try:
                            await on_tool_result(result)
                        except Exception as e:
                            logger.debug("Callback error: %s", e)

                    # Store tool interaction as messages
                    import json as _json
                    await self._messages.add_message(
                        session_id=session_id, role="assistant",
                        content=response_text, input_mode="system",
                        model=self._llm.name,
                    )
                    await self._messages.add_message(
                        session_id=session_id, role="tool",
                        content=f"<tool_result>{_json.dumps(result.get('result', result))}</tool_result>",
                        input_mode="system",
                    )

                    # Rebuild context with tool result and re-query LLM
                    context = await self._messages.get_context(session_id)
                    continue  # Loop back for next LLM call

            # No tool call (or max reached) — this is the final response.
            # Phase 2 H1 (issue #94): the streaming loop above has already
            # yielded everything up to the last marker-opener boundary.
            # `held` is the residual tail from the marker onward — usually
            # empty (no in-flight markers), or contains text that LOOKED
            # like a tool start but never completed (benign prose with
            # `<tool` literal, or malformed markup the parser rejected).
            #
            # Audit D5: still run `_strip_tool_markup` on the held tail so
            # if it DID contain a complete-but-unparseable
            # `<tool>X</tool><args>{}</args>` block (happens when the LLM
            # emitted markup after MAX_TOOL_CALLS was hit) the user
            # doesn't see raw XML.  Most turns this is a no-op.
            if self._tool_registry and held:
                cleaned = _strip_tool_markup(held)
                if cleaned:
                    yield cleaned

            break

        latency_ms = (time.monotonic() - t0) * 1000

        # Store final assistant response (only the LAST response, not duplicates)
        await self._messages.add_message(
            session_id=session_id,
            role="assistant",
            content=response_text,
            input_mode="system",
            model=self._llm.name,
            latency_ms=latency_ms,
        )

        logger.info(
            "Conversation streamed (session=%s, latency=%.0fms, tools=%d): '%s' → '%s'",
            session_id, latency_ms, tool_calls_made, text[:50], response_text[:50],
        )

    async def _process_text_stream_native(
        self,
        session_id: str,
        text: str,
        input_mode: str = "text",
        audio_duration_s: Optional[float] = None,
        on_tool_call=None,
        on_tool_result=None,
        on_tool_error=None,
        media_id: Optional[str] = None,
    ) -> AsyncIterator[str]:
        """Native tool-calling turn via the OpenAI tools=[...] API.

        Mirrors process_text_stream's contract but uses
        `SupportsNativeTools.generate_with_tools` instead of prose-listed
        tools + the marker parser. Each loop iteration asks the model for
        a structured decision: a tool call (execute → feed result back →
        loop) or final content (yield + stop). `tool_choice="auto"` means
        the model returns no tool call for chit-chat / out-of-scope, so
        the synthetic none() escape hatch isn't needed.

        Non-streaming: the final answer is yielded in one chunk. The
        WS keepalive (server.py) covers the silent inference window.
        """
        import json as _json

        await self._messages.add_message(
            session_id=session_id,
            role="user",
            content=text,
            input_mode="text" if media_id else input_mode,
            audio_duration_s=audio_duration_s,
            media_id=media_id,
        )
        await self._db.touch_session(session_id)

        tools = self._tool_registry.openai_tools()
        for _t in tools:  # sharpen confusable tool descriptions (recipe part 2)
            _nm = _t["function"]["name"]
            if _nm in _NATIVE_TOOL_DESC:
                _t["function"]["description"] = _NATIVE_TOOL_DESC[_nm]
        fewshot = _native_fewshot()
        t0 = time.monotonic()
        tool_calls_made = 0
        response_text = ""

        while True:
            context = await self._build_context(
                session_id, text, inject_tool_prose=False
            )
            # Recipe parts 1+3: append tool-routing guidance to the system
            # prompt and splice the read-vs-action / no-chitchat few-shot in
            # right after it (native path only).
            if context and context[0].get("role") == "system":
                context[0] = {**context[0],
                              "content": context[0]["content"] + _NATIVE_TOOL_GUIDANCE}
                context = [context[0]] + fewshot + context[1:]
            else:
                context = (
                    [{"role": "system", "content": _NATIVE_TOOL_GUIDANCE.strip()}]
                    + fewshot + context
                )
            result = await self._llm.generate_with_tools(context, tools)
            calls = result.get("tool_calls") or []
            response_text = result.get("content") or ""

            if calls and tool_calls_made < MAX_TOOL_CALLS:
                call = calls[0]  # one at a time, same as the prose path
                tool_calls_made += 1
                tool_call = {"tool": call["name"], "args": call.get("args") or {}}
                logger.info(
                    "Native tool call: %s(%s)", tool_call["tool"], tool_call["args"]
                )

                if on_tool_call:
                    try:
                        await on_tool_call(tool_call)
                    except Exception as e:
                        logger.debug("on_tool_call callback error: %s", e)

                if not self._tool_registry.get(tool_call["tool"]):
                    # Model invented a tool name — surface it, don't loop.
                    if on_tool_error:
                        try:
                            await on_tool_error({
                                "name": tool_call["tool"],
                                "code": "unknown_tool",
                                "message": f"No such tool: {tool_call['tool']}",
                            })
                        except Exception as e:
                            logger.debug("on_tool_error callback error: %s", e)
                    break

                _args = dict(tool_call["args"])
                _args.setdefault("session_id", session_id)
                tool_result = await self._tool_registry.execute(
                    tool_call["tool"], _args
                )

                if on_tool_result:
                    try:
                        await on_tool_result(tool_result)
                    except Exception as e:
                        logger.debug("on_tool_result callback error: %s", e)

                await self._messages.add_message(
                    session_id=session_id, role="assistant",
                    content=f"<tool>{tool_call['tool']}</tool>"
                            f"<args>{_json.dumps(tool_call['args'])}</args>",
                    input_mode="system", model=self._llm.name,
                )
                await self._messages.add_message(
                    session_id=session_id, role="tool",
                    content=f"<tool_result>"
                            f"{_json.dumps(tool_result.get('result', tool_result))}"
                            f"</tool_result>",
                    input_mode="system",
                )
                continue  # re-query with the tool result in context

            # No tool call (or limit hit) — final answer.
            if calls and tool_calls_made >= MAX_TOOL_CALLS and on_tool_error:
                try:
                    await on_tool_error({
                        "name": "(chain)",
                        "code": "tool_call_limit_reached",
                        "message": (
                            f"Reached the {MAX_TOOL_CALLS}-tool limit for "
                            "this turn — please ask again to continue."
                        ),
                        "limit": MAX_TOOL_CALLS,
                    })
                except Exception as e:
                    logger.debug("on_tool_error (limit) callback error: %s", e)

            if response_text:
                yield response_text
            break

        latency_ms = (time.monotonic() - t0) * 1000
        await self._messages.add_message(
            session_id=session_id,
            role="assistant",
            content=response_text,
            input_mode="system",
            model=self._llm.name,
            latency_ms=latency_ms,
        )
        logger.info(
            "Native turn (session=%s, latency=%.0fms, tools=%d): '%s' → '%s'",
            session_id, latency_ms, tool_calls_made, text[:50], response_text[:50],
        )
