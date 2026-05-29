---
audience: developer
type: explanation
prerequisites: [docs/explanation/architecture.md](architecture.md), [docs/explanation/how-the-stack-fits-together.md](how-the-stack-fits-together.md)
last-verified: 2026-05-29
---
# Local-first inference on the Dragon — how it works and why

## The question

Voice mode 0 — Local — runs the entire LLM turn on the Dragon Q6A itself: no
OpenRouter, no workstation, no cloud. This page explains the principle behind
that choice, why the Local LLM path runs on
[llama-server](../../GLOSSARY.md#runtime--inference-terms) instead of Ollama,
how small models are made to call tools reliably, which models actually survived
the selection process, and the two hard physical limits — thermal throttling and
the Hexagon DSP's instruction set — that shape what "local" can be on this
hardware.

If you want to *change* the Local backend, read
[Swap the LLM backend](../how-to/swap-the-llm-backend.md). This page is the
*why* behind the defaults that guide does not re-explain.

## The Dragon-only principle

Local mode is Dragon-only. When a model is too big for the
[Dragon's](../../GLOSSARY.md#the-four-repos--the-hardware) ARM64 CPU + Ollama,
the answer is never "run it on a workstation on the LAN and point the Dragon at
it." The answer is: wait for upstream support, or have the user explicitly opt
into Cloud (voice mode 2) or Hybrid (voice mode 1).

This is a product decision, not a technical one. Two things break the moment a
workstation enters the Local path:

- **Privacy.** Local mode's whole promise is that the audio, the transcript, and
  the reply never leave the box in front of you. A LAN inference server is
  another machine that sees your data.
- **The product story.** The live product is "Tab5 + Dragon, nothing else." A
  silent dependency on a third always-on machine turns a two-box appliance into
  a homelab. That is a different product.

The router's `lan` tier (LM Studio on a workstation, see the
[router cookbook](../router-cookbook.md)) exists for Cloud-mode acceleration,
where the user has already opted out of the privacy guarantee. It is never the
answer to a Local-mode gap. (This is a captured user directive — see
`feedback_dragon_only_local.md` in the project memory.)

## Why llama-server, not Ollama

The Dragon serves its Local LLM through
[`llama-server`](../../GLOSSARY.md#runtime--inference-terms) — the
OpenAI-compatible binary from llama.cpp, built ARM64-native on the box (master
build `b8696` verified) — listening on `localhost:1234`. The Dragon backend that
talks to it is `dragon_voice/llm/lmstudio_llm.py` (LM Studio and llama-server
speak the same `/v1/chat/completions` dialect, so one backend covers both):

```yaml
llm:
  backend: "lmstudio"          # was "ollama"
  local_backend: "lmstudio"    # remembers the original for fallback
  lmstudio_url: "http://localhost:1234/v1"
  lmstudio_model: "default"    # llama-server reports its loaded GGUF
```

It runs persistently as `tinkerclaw-llama-server.service` (unit in
`deploy/systemd/`): survives reboot, restarts on failure, swaps model via the
unit's `--model` arg plus `systemctl restart`.

We swapped away from Ollama because, on identical hardware and the identical
`ministral-3:3b` model, the wrapper overhead was enormous:

| Path | Direct tool-call probe | End-to-end through Dragon |
|---|---|---|
| Ollama | ~78 s | ~140 s |
| llama-server | **~7.6 s** | **~191 s** |

The ~10× speedup on the direct probe is the load + sampler overhead Ollama's Go
wrapper adds on top of the same llama.cpp eval kernel. End-to-end the gap
narrows because most of the wall-clock is the eval itself plus tool execution
and the natural-language wrap — but the net product wins stack up: the MiniCPM-V
vision family loads and serves cleanly via llama-server (Ollama 500'd on V-4.6
projector blobs), the OpenAI-compatible API opens a richer ecosystem of tooling
and clients, and we stop paying for the wrapper.

### The fallback is one-shot, not per-request

`create_llm()` in `dragon_voice/llm/__init__.py` does a synchronous TCP probe to
`lmstudio_url` at process start when `backend="lmstudio"`. If the llama-server
socket is not reachable then, it transparently falls back to Ollama for the rest
of that process's life. Two consequences fall out of "one-shot at instance
creation":

- Restarting `tinkerclaw-llama-server` mid-session is **not** picked up
  automatically. After fixing llama-server, also
  `systemctl restart tinkerclaw-voice` so the next process re-runs the probe.
- Warm-path latency is unaffected — there is no per-request probe tax.

The order of preference in Local mode is: (1) `lmstudio` (llama-server) when
reachable on `127.0.0.1:1234`, then (2) `ollama` as automatic fallback. To force
pure Ollama without removing llama-server, set both `llm.backend: "ollama"` and
`llm.local_backend: "ollama"`.

Two timing knobs exist because Q6A Local turns are genuinely slow:

- `MAX_TOKENS_LOCAL = 1024` (was 128). Thinking-mode models (MiniCPM-V-4.6,
  qwen3-thinking) spend their budget inside `<think>…</think>` and emit zero
  visible content if capped low. Non-thinking models like `ministral-3:3b` stop
  naturally well under the cap, so the higher ceiling costs them nothing.
- `ClientTimeout(total=600, sock_read=300)` in `lmstudio_llm.py` — bumped from
  120/60 because Local turns on Q6A routinely hit 90–180 s.

## Making a 1–4 B model call tools

A frontier cloud model gets a structured `tools=[…]` array and a `tool_choice`
field and reliably emits a function call. A 1–4 B model running locally does not
have that much headroom, so the Dragon meets it where it is: it prose-lists the
tools in the system prompt and **parses the tool call back out of free text**.

The parser (`dragon_voice/tools/parser.py` and the registry in
`dragon_voice/tools/registry.py`) accepts several *dialects* because different
model families emit different markup regardless of what the system prompt asks
for:

1. **Legacy / Dragon-standard** — `<tool>NAME</tool><args>{json}</args>`. The
   TinkerBox house format; ministral and gemma3 emit this.
2. **Standard FC** — `<tool_call>{"name":"…","arguments":{…}}</tool_call>`.
   Industry-typical function-calling fine-tunes (Qwen-FC, Gemma-FC) emit this no
   matter the prompt.
3. **Bracketed-name** — `[NAME]{json}</NAME>`, gated on `NAME` being a
   registered tool so prose like `[note]` does not false-fire.
4. **Gemma sentinel** — `<|tool_call>call:NAME{}<tool_call|>` (added 2026-05-17).
5. **LFM sentinel** — `<|tool_call_start|>[name(arg="val")]<|tool_call_end|>`,
   parsed via `ast.literal_eval` so nested quotes and embedded commas survive
   (added 2026-05-17).

The parser is deliberately tolerant of small-model quirks — stray `>` after
`</args>`, missing closing tags, xLAM bracket noise — because that is the actual
output distribution of these models under prose pressure.

Two guards make this usable as a voice assistant rather than a demo:

- **Empty-reply wrap** (`dragon_voice/tools/response_wrap.py`). Some FC-trained
  models emit a tool call and stop, leaving the user-visible text empty once the
  markup is stripped. When that happens and at least one tool fired, the Dragon
  synthesizes a one-line natural-language acknowledgement from the tool result
  using a per-tool template — no extra LLM round-trip.
- **WS keepalive during inference** (`server.py: _ws_keepalive_during_inference`).
  Local turns routinely take 60–90 s; the Tab5's WebSocket library times out
  after ~30 s without a PONG and reconnects, which the server treats as a
  duplicate connection and evicts the in-flight stream — an empty reply on every
  slow turn. The keepalive fires `ws.ping()` every 5 s while the engine
  generates.

The deeper move — letting the inference server's own structured `tools=[…]` API
do the work instead of prose-listing — is what the glossary calls
[native tool-calling](../../GLOSSARY.md#runtime--inference-terms). That is the
direction of the parked local-mode capability rework (see "Where this is going"
below); it is not how the live Dragon Local path works today.

## The model gauntlets

Choosing the Local default is an empirical question, so it was settled by
running candidate models through fixed gauntlets on the Q6A — same system
prompt, same tool definitions, `temperature` pinned, no Dragon middleware in the
way for the direct probes. Two gauntlets matter: an **easy** 10-scenario
happy-path set, and a **hard** 20-scenario set that probes disambiguation,
complex arg extraction, red herrings, out-of-scope rejection, multi-step intent,
and chitchat. The hard gauntlet is the one that separates "looks like it works"
from "works on messy prose."

A model has to be good on **three independent axes**, and small models tend to
trade them against each other:

- **Latency** — sub-minute per turn is the floor for real-time voice.
- **Tool-routing accuracy** — picks the right tool *and* extracts the right args
  from prose, *and* knows when to call nothing.
- **Reply quality** — specific and actionable, not vague.

### LFM2.5-VL-1.6B — the real-time voice default

LiquidAI's 1.6 B vision-language model (696 MB Q4_0 weights + 583 MB Q8_0
mmproj) is the Local default for real-time voice (user decision, 2026-05-17). It
**aced the easy gauntlet 10/10** on tool name, tool choice, and arg extraction —
where ministral scored 7/10 — and it brings vision essentially for free (a
300×400 sunflower image described accurately in ~40 s wall-clock). Text turns run
~13–20 s.

The catch is the hard gauntlet: **7/20**. At 1.6 B the model has the capacity for
clean happy-path routing but not enough to robustly tell "use the word X" from
"invoke tool X." The documented failure modes are worth internalizing because
they are characteristic of *every* tiny tool-router, not a bug:

1. **Red-herring trigger** — any word matching a tool name fires that tool, even
   in chitchat ("calendar joke" → `calendar_today`). The model treats tool names
   as keywords, not intent.
2. **Verb confusion** — "ping the boss" became `timer_set`; "did Sarah email me"
   became `gmail_reply`. Action verbs near a duration or subject get misread.
3. **Few-shot contamination** — "buy bread" from a system-prompt exemplar leaked
   verbatim into an unrelated answer. Tiny models echo the examples.
4. **Arg-extraction brittleness** — `gmail_send(to, subject, body)` emitted the
   *parameter names* as positional placeholders instead of extracting "mom",
   "Happy Birthday", "love you…" from the prompt.

A crucial operational note: LFM-VL only routes well under a **directive** system
prompt ("you are a tool-router; you ALWAYS call exactly one tool; NEVER explain,
NEVER ask permission, NEVER refuse"). With a softer prompt it falls into
conversational refusals ("I can use `calendar_week()`. Would you like me to?")
and drops to 1/10 on the *easy* gauntlet. The prompt is load-bearing.

We accept the 7/20 hard-gauntlet brittleness as the cost of speed: sub-second
time-to-first-byte matters more for voice than tool-routing perfection, and the
easy gauntlet covers the common case. `ministral-3:3b` (7/10 easy, ~3–8 s/turn)
is kept at `/home/radxa/llama.cpp/models/ministral/model-q4_k_m.gguf` as a
one-line rollback.

### Qwen3.5-4B — the accuracy leader, parked for agentic

`lmstudio-community/Qwen3.5-4B-GGUF` Q4_K_M (2.7 GB) is the new accuracy leader:
**18/20 strict on the hard gauntlet — effectively 20/20, both misses scoring
artifacts**. It scored 4/4 on disambiguation, 3/3 on red herrings, 3/3 on
out-of-scope, and 3/3 on chitchat, the exact axes where LFM-VL and Gemma broke.
On "ping the boss I'll be late by 15 min" it drafted a complete, sane email body
in one shot where LFM-VL had chosen `timer_set(900)`.

Two non-obvious requirements: each request must pass
`chat_template_kwargs: {"enable_thinking": false}` (otherwise Qwen burns the
whole token budget inside `<think>` and emits empty content), and the recommended
non-thinking sampling is `temp 0.7, top_p 0.8, top_k 20, presence_penalty 1.5`.

It is **parked for real-time voice on latency**: ~96 s per turn (range 79–123 s)
on the Q6A, roughly 5× slower than LFM-VL for ~3× the accuracy. That makes it the
candidate for *agentic / background / browser-agent* workloads — where multiple
tool calls chain and quality beats latency — not for live conversation.

| Model | Hard-gauntlet score | Per-turn (mean) |
|---|---|---|
| ministral-3:3b | (easy: 7/10) | 3–8 s |
| LFM2.5-VL-1.6B Q4 | 7/20 | ~14–20 s |
| Gemma-4-E4B Q4 | (too slow to finish hard) | 30–75 s |
| Gemma-4-E2B Q4 | 0/10 args emitted | 7–8 s warm |
| **Qwen3.5-4B Q4_K_M** | **18/20 (≈20/20)** | **~96 s** |

The Gemma-4 siblings illustrate the two ways a candidate fails: E4B is *accurate*
but unusably slow (30–75 s per turn at 80-token headroom), and E2B is *fast*
(ministral-class) but defaults to `calendar_today` on any ambiguity and never
emits an `<args>` block (8/10 tool tokens, 0/10 args). Both are parked. Parser
Dialect 4 was added for Gemma and stays — it is harmless for other models and
earned its keep.

### Granite — the K144 onboard winner

[Granite](../../GLOSSARY.md#runtime--inference-terms) (IBM Granite 4.0 Nano-1B)
is the winner of a *different* gauntlet: native tool-calling on the
[K144 / TinkerON](../../GLOSSARY.md#the-four-repos--the-hardware) onboard module
(voice mode 4), where the model runs on the Tab5-side AX630C NPU with no Dragon
present. It scored **19/20** there. Do not confuse it with the Dragon's
llama-server default — Granite is the candidate to become the live *onboard*
Local default, and that work lives on the unpushed `feat/local-mode-capability`
branch, not on the Dragon's Local LLM path described above. See "Where this is
going."

## Thermal reality

The Q6A's Local latency is not a fixed hardware floor — a large part of it is
**thermal throttling**. On the K144 onboard work the same turn that took 436 s
dropped to 29 s after a prefix-stable cache-reuse fix, and the *residual*
slowness was traced to the SoC sitting at ~90 °C with no fan and half-clocking
itself. A $5 fan is the next 2× on that path, not a new chip.

The lesson generalizes to the Dragon. When you see a Local turn take three
minutes, the chain of suspects, in order, is:

1. **Prompt-cache misses** — a turn that cannot reuse the warm KV-cache prefix
   re-evaluates the full system + tools + memory + history prompt from scratch.
   Keeping the prefix stable is the single biggest warm-path win.
2. **Thermal half-clocking** — a hot, unfanned SoC runs at half its rated clock.
   Cooling is cheaper than any software optimization.
3. **The eval itself** — only after the first two are ruled out is the model
   genuinely too big for the silicon.

Treat a slow turn as a cache or thermal problem until proven otherwise. "The
model is too slow" is the last conclusion, not the first.

## Multimodal is parked, not skipped

Mainline llama.cpp does not yet support MiniCPM-V-4.6's `minicpmv4_6` projector
or MiniCPM-o's audio encoder, so the heaviest local multimodal paths are not
available on the Dragon today. This is a *wait-for-upstream* state, consistent
with the Dragon-only principle — not a "give up and add a workstation" state:

- **Vision via the MiniCPM-V family** — wait for projector support to land
  upstream, or rebuild llama.cpp from a feature branch that has it. (LFM2.5-VL
  already gives the Local default working vision.)
- **Audio via MiniCPM-o** — the realistic path is the openbmb maintainer's
  `tc-mb/llama.cpp-omni` fork (clean ~15 min build on Q6A), GGUF-ifying
  MiniCPM-o-4.5 from safetensors with the fork's converter. Tracked for a future
  session.

In the meantime, **Cloud mode (voice mode 2)** already handles audio and vision
via OpenRouter — the right answer precisely *because* the user has explicitly
opted in to cloud. Parking a local path is not the same as having no path.

## Where this is going

The native-tool-calling rework is the active investigation
(`feat/local-mode-capability`, unpushed). It moves the onboard
[K144 / TinkerON](../../GLOSSARY.md#the-four-repos--the-hardware) path off
prose-parsed XML dialects and onto the inference server's structured
[`native_tools`](../../GLOSSARY.md#runtime--inference-terms) API, with
**Granite 4.0 Nano-1B (19/20)** as the winning model and the prefix-stable
cache-reuse + cooling work as the latency story. The next steps are to make
Granite the live onboard Local default and to land the branch. None of this
changes the Dragon's llama-server Local path documented here — it is a parallel,
Tab5-side capability.

## See also

- [Swap the LLM backend](../how-to/swap-the-llm-backend.md) — the procedure for changing the Local (or any) LLM backend.
- [Configure the multi-model router](../how-to/configure-the-multi-model-router.md) · [Router cookbook](../router-cookbook.md) — the per-modality, per-tier fleet (and where the `lan` tier legitimately belongs).
- [Voice modes reference](../reference/voice-modes.md) — what Local / Hybrid / Cloud / TinkerClaw actually swap.
- [NPU setup](../npu-setup.md) — the QAIRT / Genie path and the HTP v68 size ceiling that blocks 3 B+ models on the Dragon.
- [TinkerBox architecture](architecture.md) — the system map this inference path sits inside.
- [`GLOSSARY.md`](../../GLOSSARY.md) — canonical terms for the stack.
