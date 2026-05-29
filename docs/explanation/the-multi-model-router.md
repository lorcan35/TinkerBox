---
audience: developer
type: explanation
prerequisites: [architecture.md](architecture.md), [the-voice-pipeline.md](the-voice-pipeline.md)
last-verified: 2026-05-29
---
# The capability-aware multi-model router — how it works and why

## The question

This page answers: *when Dragon has a whole fleet of language models
to choose from, how does it decide which one runs your turn — and why
is that decision shaped the way it is?*

For most of TinkerBox's life the rule was simple: one voice_mode picked
one LLM backend. Local mode meant ollama; Cloud mode meant whatever
single model OpenRouter was pointed at. That rule could not survive
contact with reality. A photo turn needs a vision model; a plain "what's
on my calendar?" turn does not, and paying frontier-vision prices to
answer it is waste. A video frame from a call needs a model that
actually ingests video. The model that is best at extracting email
arguments from messy prose is not the model that answers fastest.

The multi-model router (PRs #183–#188) replaces "voice_mode picks a
backend" with "voice_mode picks a *tier*, the message's modalities pick
the *requirements*, and the cheapest model that satisfies both wins."
This page is the mental model behind that sentence. If you want to
*configure* a fleet, read the
[configure the multi-model router how-to](../how-to/configure-the-multi-model-router.md)
or the [router cookbook](../router-cookbook.md). If you want to *look
up* what a voice mode does, read the
[voice modes reference](../reference/voice-modes.md). This page exists
to explain the design, not to give you a recipe.

## The model

The router is opt-in. Set `llm.backend: "router"` in
`dragon_voice/config.yaml` and populate `llm.fleet`, and the
`CapabilityAwareRouter` in `dragon_voice/llm/router.py` takes over model
selection. Leave `backend` set to any single value (`ollama`,
`lmstudio`, `openrouter`, …) and the router code never runs — behavior
is byte-identical to the pre-router days. There is no flag day; the
fleet is dormant until you ask for it.

When it is active, every turn flows through four decisions.

```
Incoming message (OpenAI-format content array)
        |
        v
[1] infer_required_caps(messages)  ──►  required_caps : frozenset(Modality)
        |                                e.g. {TEXT}  or  {TEXT, VISION}
        v
[2] TIER_FOR_MODE[voice_mode]      ──►  tier_filter   : {"local"} / {"cloud","lan"} / None
        |
        v
[3] candidates = [ m in fleet
                   if required_caps  <=  m.capabilities    # m can do everything the turn needs
                   and m.tier in tier_filter ]             # m is allowed in this mode
        |
        v
[4] winner = min(candidates, key=lambda m: m.priority)     # lowest priority number wins
        |
        v
   chosen backend generates the turn
```

### Modality capabilities

Every model in the fleet declares what it can *do* as a frozenset of
`Modality` values. The enum lives in `dragon_voice/llm/base.py`, and
each `LLMBackend` subclass exposes its set through a `capabilities`
property.

| Modality | The turn requires it when |
|----------|---------------------------|
| `TEXT` | Always — every turn requires `TEXT` |
| `VISION` | The message content contains `{"type":"image_url"}` (a photo) |
| `VIDEO` | The message content contains `{"type":"video_url"}` (a call frame) |
| `AUDIO_IN` | The message content contains `{"type":"input_audio"}` |
| `AUDIO_OUT` | The backend can emit audio responses |
| `TOOL_CALLING` | The backend is trained for function-calling |

Backends declare their capabilities differently because they know
different things about themselves:

- **ollama** scans the model id as a substring. Vision is implied by
  `llava`, `minicpm-v`, `minicpm-o`, `moondream`, `qwen2-vl`, `pixtral`,
  or `internvl`; audio by `minicpm-o`; tools by the known
  function-calling families.
- **openrouter** uses a static registry, `_OPENROUTER_CAPS` in
  `openrouter_llm.py` (35 models tracked as of the 2026-04-27 sync in
  #187). An unknown model id defaults to `{TEXT, TOOL_CALLING}`.
- **lmstudio** uses the same name-substring heuristic as ollama, because
  LM Studio serves arbitrary GGUFs and the file name is all it has.
- **npu_genie** is text-only — QAIRT has no vision path on the QCS6490
  Hexagon DSP.
- **tinkerclaw** declares `TEXT + TOOL_CALLING`, plus `VISION` when the
  gateway's configured model is `anthropic/`, `openai/gpt-4o`,
  `google/gemini`, or `minimax/`.
- **dual** forwards to its responder's capabilities — the responder is
  the sub-backend whose tokens actually reach the user.

### `infer_required_caps` — reading the message, not the prompt

Step [1] is `infer_required_caps(messages)`. It walks the OpenAI-format
content arrays of the incoming messages and accumulates requirements
from what is *present in the payload*:

- an `image_url` part adds `VISION`
- a `video_url` part adds `VIDEO`
- an `input_audio` part adds `AUDIO_IN`
- `TEXT` is always in the set

The function is deliberately mechanical. It looks at structured content
parts, not at the words the user said. A turn that carries a JPEG
requires `VISION` whether the user said "look at this" or said nothing
at all. This is what makes routing predictable: the requirement set is a
function of the wire payload, and you can reproduce any routing decision
by hand from the message and the fleet.

### `TIER_FOR_MODE` — voice_mode chooses the lane, not the model

The router never reads `voice_mode` to pick a model. It reads it to pick
a *tier filter* — the set of tiers a candidate is allowed to live in.

```python
TIER_FOR_MODE = {
    0: {"local"},          # Local
    1: {"local"},          # Hybrid — LLM stays local; only STT/TTS go cloud
    2: {"cloud", "lan"},   # Full Cloud — LAN tier eligible (e.g. LM Studio on a workstation)
    3: None,               # TinkerClaw — bypass the router, the gateway picks
}
```

Three things fall out of this table:

- **Hybrid mode (1) keeps the LLM local.** Hybrid sends STT and TTS to
  the cloud for speed and quality, but the language model stays on
  Dragon. So Hybrid's tier filter is `{"local"}`, exactly like Local
  mode. The cloud part of Hybrid lives entirely in the pipeline's STT/TTS
  swap, never in the router.
- **Cloud mode (2) admits the LAN tier.** When you run a heavy
  multimodal model on a workstation via LM Studio's OpenAI-compatible
  API, you declare it `tier: lan`, and Cloud mode can route to it. That
  is the one sanctioned use of off-Dragon inference, and it is opt-in per
  fleet entry. (Note the project's standing rule: *Local mode is
  Dragon-only*. The LAN tier is a Cloud-mode affordance, never a
  Local-first path.)
- **TinkerClaw mode (3) returns `None`.** When the tier filter is `None`,
  `choose()` returns `None` immediately and the router gets out of the
  way — the TinkerClaw gateway runs its own agent loop and picks its own
  model. See [run the TinkerClaw sidecar](../how-to/run-the-tinkerclaw-sidecar.md).

### Lowest-priority-wins

Step [3] filters the fleet down to candidates that can satisfy the turn
*and* are allowed in the active tier. Step [4] breaks the tie by
`priority`, an integer you assign per fleet entry. **Lower wins.**

```python
def choose(required_caps, voice_mode):
    tier_filter = TIER_FOR_MODE[voice_mode]
    if tier_filter is None:
        return None  # tinkerclaw mode — router not used
    candidates = [m for m in fleet
                  if required_caps <= m.capabilities
                  and m.tier in tier_filter]
    return min(candidates, key=lambda m: m.priority) if candidates else None
```

Priority is "default preference within the set of models that can do the
job," not "quality rank." You give your cheapest acceptable model
priority 0 and reserve the higher numbers for models you only want to
reach when the cheaper ones are filtered out by a capability they lack.
Because filtering happens *before* the priority sort, a vision turn never
even considers a text-only model no matter how low its priority — it is
simply not a candidate.

Walk the canonical fleet through this:

| Mode | Turn carries | Candidates after filter | Winner (min priority) |
|------|--------------|--------------------------|-----------------------|
| Local (0) | text only | all local-tier models | `ministral-3:3b` (priority 0) |
| Local (0) | a photo | only `minicpm_v4` (the one local vision model) | `minicpm_v4` |
| Cloud (2) | text only | all cloud-tier models | `deepseek-v4-flash` (priority 0) |
| Cloud (2) | a photo | cloud models with `VISION` | `qwen3.6-flash` (priority 5) |
| Cloud (2) | a video frame | cloud models with `VIDEO` | `gemini-3-flash-preview` (priority 8) |

The full fleet that produces this table is in
[CLAUDE.md](../../CLAUDE.md)'s Multi-Model Router section and in the
[router cookbook](../router-cookbook.md). A voice_mode swap — say Local
→ Cloud — calls `set_voice_mode(2)` on the router; it only flips the
tier filter. The instantiated sub-backends survive the switch, so there
is no model reload cost to changing modes.

### Why `TOOL_CALLING` is declared but never gates

`TOOL_CALLING` appears in the capability registry, and most fleet entries
list it. But `infer_required_caps` *never* adds it to the required set,
so it never narrows the candidate list. It is informational — a label,
not a gate.

This is a deliberate, load-bearing decision, and it is the most
counterintuitive part of the design. Here is the trap it avoids:

> Imagine a vision turn. The local vision model is MiniCPM-V, which is
> excellent at describing a photo but is *not* a tool-calling model. If
> `infer_required_caps` added `TOOL_CALLING` to the requirements of
> every turn — reasoning "Dragon is agentic, so every turn might need a
> tool" — then MiniCPM-V would be filtered out of its own vision turn,
> and the router would have *no* local candidate that can see. The photo
> turn would fail or fall through to a worse model.

So tool capability is excluded from inference on purpose. Tool detection
is a *runtime* event, not a routing input: the chosen model generates,
and if it emits a tool marker (`<tool>name</tool><args>{…}</args>` or one
of the other accepted dialects), the `ToolRegistry` parses and executes
it then. A model that lacks tool training simply never emits a marker,
and you get a plain answer. Routing on a capability the turn doesn't
strictly *require* would amputate models that are the only option for the
capability the turn *does* require. The
[router cookbook troubleshooting](../router-cookbook.md) makes the same
point from the operator's chair: "TOOL_CALLING is intentionally NOT
inferred from system-prompt content; it's a bonus, not a gate."

### Cross-modal continuity

A router that picks the right model for *this* turn is only half the
problem. The other half is making a *later* text turn remember an
*earlier* image turn. Before the router landed, the multimodal handler
(`_handle_user_media`) bypassed the conversation engine entirely, so a
photo never entered conversation history. Send a photo, then ask "what
color was the chair?" — and the text follow-up routed to a text-only
model that had never seen the chair.

The router fixes this end to end:

- `_handle_user_media` no longer bypasses `ConversationEngine`.
  Multimodal user messages persist through
  `MessageStore.add_message(media_id=…)` using a `__mm__:` JSON marker in
  the message content — a compact pointer, not the raw image bytes, in
  the row.
- On context build, `get_context(media_store=…)` *hydrates* those markers
  back into OpenAI `image_url` content arrays, pulling the image from the
  media store. So the follow-up turn's message array carries the photo
  again, `infer_required_caps` re-derives `VISION`, and the router again
  picks a vision-capable model — which now answers "the chair was blue"
  because it can see the original photo in context.
- `OllamaBackend.generate_stream_with_messages` translates the OpenAI
  multimodal content array into Ollama's flat `content + images` shape.
  Without that translation step, router-fed vision turns failed with
  `json: cannot unmarshal array into Go struct field`.

The result is the property you actually want from a "smart" assistant:
modality is sticky across the conversation, not just within one turn.

### `fleet_summary` — telling Tab5 what it can do

When the router is active, `session_start.config` and every
`config_update` ACK carry a `fleet_summary` object — a per-modality map
of which model would handle each kind of turn:

```json
{
  "type": "session_start",
  "config": {
    "fleet_summary": {
      "text":         "ministral-3:3b",
      "vision":       "hf.co/openbmb/MiniCPM-V-4-gguf:Q4_K_M",
      "video":        "hf.co/openbmb/MiniCPM-V-4-gguf:Q4_K_M",
      "audio_in":     null,
      "audio_out":    null,
      "tool_calling": "ministral-3:3b"
    }
  }
}
```

This lets Tab5 light up its vision/video/audio capability chips
dynamically from the *actual* fleet, rather than guessing. A `null` entry
means no model in the active tier can serve that modality, so Tab5 knows
to grey out the camera affordance. Tab5 firmware is free to ignore
`fleet_summary` — the legacy `vision_capability` event keeps firing for
backward compatibility — but a fleet-aware client gets a precise picture
of what the current mode can handle. The summary is produced by the
router's `summarize()`; the wire shape is in the
[WebSocket protocol reference](../reference/websocket-protocol.md).

## Why it's built this way

**Why separate "what the turn needs" from "what the mode allows"?**
Because they answer to different stakeholders. The required-caps set is a
fact about the *message* — the user sent a photo, or they didn't. The
tier filter is a *policy* about privacy and cost — the user chose Local
to keep data on the box, or Cloud to get frontier quality. Folding them
together is what created the original one-mode-one-model rigidity. Keeping
them orthogonal means you can change the privacy policy (swap modes)
without re-deciding modality, and you can change modality (send a photo)
without re-deciding privacy. The intersection is computed fresh each turn,
so the two axes never fight.

**Why lowest-priority-wins instead of a scoring function?** A scoring
function (weigh cost, latency, quality, …) would be more "intelligent"
and far less predictable. With a single integer per model and a hard
capability filter, every routing decision is reproducible by hand from
the fleet config — which is exactly what you want when you are debugging
"why did *that* model fire?" at 2 a.m. The cost of the simplicity is that
you, the fleet author, encode your preferences as priority numbers
up front. That is a feature: the policy lives in `config.yaml` where you
can read it, not in a weighting heuristic you have to reverse-engineer.
Use gaps (0, 5, 10, 15, …) so you can slot a new model in without
renumbering.

**Why is the router opt-in?** Because the single-backend path is simpler,
and most installs don't need a fleet. A factory `router` with one model
would be a one-model fleet pretending to be a router — exactly the kind of
speculative abstraction the project's anti-slop rules forbid. So the
router only exists when you have a reason to declare more than one model,
and the dormant fleet costs nothing.

**Why does TinkerClaw mode bypass the router entirely?** Because in mode
3 the language-model decision belongs to the gateway's agent loop, not to
Dragon. Dragon is an audio pipe in that mode — `ConversationEngine`,
`ToolRegistry`, and the router are all out of the path. Returning `None`
from `TIER_FOR_MODE[3]` is how the router declines to have an opinion.

**The trade-off we accepted.** A capability filter is a *hard* filter: if
no model in the active tier can satisfy the required modalities, `choose`
returns `None` and the turn has nowhere to go (you'll see `router: no
fleet model satisfies …` in the logs). The cookbook's "Local with Cloud
Vision Fallback" pattern — let Local mode borrow a cloud vision model when
the local one isn't loaded — is *not* supported, precisely because the
tier filter is hard by design. Softening it would reintroduce the
cross-tier surprises the tier split was meant to eliminate. The workaround
is the honest one: stay in Local mode for text, flip to Cloud when you tap
the camera. If you want the soft-fallback behavior, that is a feature
request, not a bug.

## See also

- [Configure the multi-model router](../how-to/configure-the-multi-model-router.md) · [Swap the LLM backend](../how-to/swap-the-llm-backend.md) · [Run the TinkerClaw sidecar](../how-to/run-the-tinkerclaw-sidecar.md)
- [Voice modes reference](../reference/voice-modes.md) · [Config reference](../reference/config-reference.md) · [WebSocket protocol](../reference/websocket-protocol.md) · [Package & file map](../reference/package-file-map.md)
- [Router cookbook](../router-cookbook.md) — copy-paste fleets for common patterns
- [Architecture](architecture.md) · [The voice pipeline](the-voice-pipeline.md)
