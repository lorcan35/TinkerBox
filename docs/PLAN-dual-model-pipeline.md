# Plan: Dual-Model Local Pipeline

**Status:** PLAN ONLY (2026-04-25). No code yet. Reviewer-ready.

## Why

The 10-prompt gauntlet (post-#74/#76/#77, see CLAUDE.md "Local LLM
Benchmarks") splits the local-mode failure landscape into three classes:

| Class | Example models | What's broken |
|-------|----------------|---------------|
| Tool-pickers that don't talk | xLAM-2-1b-fc-r | Fires the right tool fast (24 s median) but emits no conversational text. Replies feel robotic — `Top hit: …` and that's it. |
| Talkers that don't pick tools | qwen3:1.7b, hermes3:3b, llama3.2:3b | Conversational fluency is fine; `<tool>` markers either never appear or appear in the wrong dialect. Math is wrong, "memories" are hallucinated. |
| Generalists that do both, slowly | ministral-3:3b, gemma3:4b | 7/10 correct tool fires + 5–7/10 visible replies, at 53–65 s median. Currently the only viable defaults. |

The thesis: **a generalist's job is two jobs**. Splitting the turn lets
each model do the thing it's actually good at and skip the thing it isn't.

## Sketch of the dual flow

```
user text
    │
    ▼
┌──────────────┐
│ PICKER turn  │  xLAM-2-1b-fc-r  (1.3 GB, ~24 s median)
│ tools = on   │  Inputs: system prompt + tool defs + user text + memory ctx
│ replies = no │  Output: <tool>X</tool><args>{…}</args>  OR  empty/short text
└──────┬───────┘
       │
       ├── if tool call(s) emitted → execute via existing ToolRegistry path
       │    (tool_call event → execute → tool_result event → append to ctx)
       │
       ▼
┌──────────────┐
│ RESPONDER    │  ministral-3:3b  (2.8 GB, ~65 s median)
│ tools = off  │  Inputs: system prompt + user text + tool_result block(s)
│ replies = on │  Output: warm conversational reply incorporating tool results
└──────┬───────┘
       │
       ▼
   user reply
```

## Decision: when to skip a phase

Two opportunities to cut latency, both opt-in via heuristic:

1. **Skip RESPONDER when no tool fired AND picker text is non-trivial.**
   If xLAM returns a short, useful, non-tool reply, hand it to the user
   directly. Saves 65 s on chitchat turns. Reuse `_looks_like_useful_text`
   from #79 — it already knows how to distinguish chat-text from
   bracket-noise residue.

2. **Skip PICKER when input is obviously chat-only.**
   *Optional second pass.* Heuristic could be a small classifier or just
   regex (no `?` + no imperative verb + < 4 tokens → chat). High risk of
   misclassification — defer this until phase 1 proves out.

Default: always run PICKER, run RESPONDER unless rule 1 above triggers.

## What lives where

### New files (proposed)

```
dragon_voice/llm/dual.py         — DualModelBackend(LLMBackend)
                                    Composes two LLMBackend instances.
                                    Implements generate_stream_with_messages
                                    by orchestrating PICKER → tool → RESPONDER.
docs/PLAN-dual-model-pipeline.md — this doc
```

### Touched files

```
dragon_voice/llm/__init__.py     — register "dual" in create_llm()
dragon_voice/config.py           — LLMConfig.dual block:
                                     picker_backend: str = "ollama"
                                     picker_model: str   = "hf.co/.../xLAM-2-1b-fc-r:Q4_K_M"
                                     responder_backend: str = "ollama"
                                     responder_model: str   = "ministral-3:3b"
                                     skip_responder_on_chat: bool = True
dragon_voice/conversation.py     — NO CHANGES if DualModelBackend
                                    transparently implements LLMBackend.
                                    Dual lives entirely behind the existing
                                    backend interface.
config.yaml                      — leave default as "ollama" + ministral-3:3b.
                                    Add commented dual-mode example block.
```

The "DualModelBackend implements LLMBackend" path is the cleanest. It
keeps ConversationEngine ignorant of the split and means the existing
tool-call loop, memory injection, and message storage all work unmodified.

The cost: DualModelBackend has to fake a single token stream for
ConversationEngine even though it's running two models in series. That's
fine — it can emit the picker's tool-call markup verbatim (so the existing
parser fires), then emit the responder's reply tokens, then the
end-of-stream sentinel.

## Failure modes to design around

| Risk | Mitigation |
|------|------------|
| xLAM emits a 4th XML dialect that #74's parser misses | Test all 10 gauntlet prompts against xLAM in isolation first. Update parser if needed. **Do not ship dual until parser is verified against the picker model.** |
| RAM ceiling: picker (1.3 GB) + responder (2.8 GB) + Moonshine (300 MB) + Piper (50 MB) + embeddings (200 MB) ≈ 4.7 GB resident. Dragon has 12 GB. | Headroom is fine. Watch for Ollama keeping both warm — may need `OLLAMA_KEEP_ALIVE=5m` and explicit unload on idle. |
| Sequential latency = 24 s + 65 s = ~89 s worst case (vs 65 s for ministral alone) | Acceptable for tool-required turns (the alternative is the empty-reply class of bugs that #77 exists to paper over). For chat-only turns rule 1 above keeps latency at 24 s. |
| Memory context: which model gets it? | Both. PICKER needs it so `recall_facts` works; RESPONDER needs it so it can reference recalled facts in the reply. Same `_build_context` injection point applies to both. |
| Tool result must reach RESPONDER, not PICKER | Trivial: PICKER finishes before tools execute. RESPONDER's context = user text + tool_result blocks (already how `_messages.get_context` reconstructs it). |
| Streaming UX: user sees nothing for 24 s while PICKER runs silent | Send a synthetic `{"type":"llm","text":""}` thinking-indicator at PICKER start, same pattern used in TC bypass at server.py:1518. |
| Test client / WS keepalive | Already handled by #76's `_ws_keepalive_during_inference` — covers any inference duration. No new work. |
| What if PICKER mis-fires a tool the user didn't want? | Same risk profile as today's single-model setup. MAX_TOOL_CALLS=3 still bounds the loop. |
| What if RESPONDER ignores the tool result and hallucinates? | Possible. Mitigation: prepend `Use this tool result to answer the user:` to the responder's system prompt when a tool fired. Prompt-engineering, not architecture. |

## Validation plan (before shipping anything)

1. **Parser audit:** run xLAM against all 10 gauntlet prompts in isolation,
   capture raw output, verify every tool-call dialect parses. If any miss,
   widen `tools/registry.py` parser or open a follow-up issue.
2. **Bench dual config in sandbox:** add `gauntlet_sandbox.sh` invocation
   for `dual:xlam+ministral`. Same 10 prompts. Compare:
     - correct tool fires (target: ≥ 7/10, parity with ministral solo)
     - visible replies (target: ≥ 7/10, parity with gemma3 solo)
     - median latency (target: < 90 s tool turns, < 30 s chat turns)
     - G2 math correct (target: 359,784)
3. **A/B variant:** also bench `dual:xlam+gemma3`. Pick whichever
   responder pairs best with xLAM's tool-call coverage.
4. **No code merges to main until the bench shows ≥ ministral-solo on both
   correct-tool-fires and visible replies.** Worst-case, the experiment
   stays in sandbox and we keep ministral-3:3b as the documented default.

## Out of scope (for this plan)

- Running PICKER and RESPONDER in parallel (speculative execution). Adds
  meaningful complexity for a marginal latency win on the chat-only path
  that rule 1 already handles. Revisit only if the sequential numbers
  prove unacceptable in real Tab5 sessions.
- Letting RESPONDER call tools too. Doubles the tool-call surface area
  and re-introduces the failure modes ministral solo already has. The
  whole point of this plan is to give each model one job.
- Cloud-mode parallel (mode 2 with two OpenRouter models). Not worth the
  cost; cloud single-model already exceeds local dual on every metric.
- NPU pairing. The Genie Llama 3.2 1B path is fast enough that pairing
  it with anything probably hurts latency more than it helps quality.
  Re-evaluate after the QAIRT NPU path lands (`docs/npu-setup.md`).

## What this plan does NOT commit to

- Implementing it. This is a design only. After the gauntlet round
  finishes (Step 4), the user reviews this plan, picks an option, and we
  open a GitHub issue + branch + PR to do the work.
- Changing the default model. `ministral-3:3b` stays the documented
  default until the dual benchmark proves otherwise.
- Picker-model commitment. xLAM-2-1b-fc-r is the strongest candidate from
  the 11-model bench, but `qwen2.5:3b-fc` or any other future
  function-calling-fine-tuned small model could replace it without
  changing the architecture.
