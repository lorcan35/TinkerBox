---
audience: developer
type: how-to
prerequisites: [Agentic Pipeline in CLAUDE.md](../../CLAUDE.md#agentic-pipeline), [Adding a Tool — worked example](../adding-a-tool.md)
last-verified: 2026-05-29
est-time: 30 min
---
# Add a tool

Use this when you need to give the Dragon LLM a new capability it can invoke
mid-conversation — fetch data, do a calculation, change some state. A **tool**
is a Python class the model calls by emitting a marker like
`<tool>name</tool><args>{...}</args>`; Dragon parses the marker, runs your code,
and feeds the result back into the model's context so it can keep going. Assumes
you have a checkout of TinkerBox, can run `pytest` locally, and have read the
[Agentic Pipeline section in CLAUDE.md](../../CLAUDE.md#agentic-pipeline).

This page is the task checklist. For a full end-to-end worked example (a
`dice_roll` tool from issue to merge), follow the
[Adding a Tool worked example](../adding-a-tool.md).

## Steps

### 1. Subclass `Tool`

Every tool subclasses `dragon_voice.tools.base.Tool` and implements four members.
Create your file under [`dragon_voice/tools/`](../../dragon_voice/tools/), named
`<name>_tool.py`:

```python
from dragon_voice.tools.base import Tool


class WeatherTool(Tool):
    """Look up current weather for a city."""

    @property
    def name(self) -> str:
        return "weather"

    @property
    def description(self) -> str:
        # Injected into the LLM system prompt — this is what the model
        # reads to decide when to call you. Keep it under ~100 chars.
        return "Get the current weather for a city. Use when the user asks about weather."

    @property
    def parameters_schema(self) -> dict:
        # JSON Schema, same shape as OpenAI function-calling.
        return {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "City name, e.g. 'Paris'"},
            },
            "required": ["city"],
        }

    async def execute(self, args: dict) -> dict:
        # Coerce + validate args — the model may pass strings, omit
        # fields, or send junk. Return a clean error dict, never raise.
        city = args.get("city")
        if not isinstance(city, str) or not city.strip():
            return {"error": "city is required"}
        # ... do the work ...
        return {"city": city, "temp_c": 18, "conditions": "clear"}
```

The four members and their jobs:

| Member | Purpose |
|--------|---------|
| `name` | Snake_case identifier the model emits in `<tool>name</tool>`. Must be unique in the registry. |
| `description` | One line injected into the system prompt. The model uses it to decide *when* to call you. System-prompt context is precious — keep it under ~100 characters. |
| `parameters_schema` | JSON Schema for the args, OpenAI function-calling shape. |
| `execute` | Async. Receives the parsed args dict, returns a result dict that gets serialised to JSON and shown to the model as the tool result. |

`Tool.to_dict()` is provided by the base class and feeds the
`GET /api/v1/tools` listing — you do not implement it.

### 2. Register in the `ToolRegistry`

Tools are registered once at startup in
[`dragon_voice/lifecycle/startup.py`](../../dragon_voice/lifecycle/startup.py).
Find where the registry is populated (search for `tool_registry.register` or
`_tool_registry.register`) and add your tool alongside the existing ones:

```python
from dragon_voice.tools.weather_tool import WeatherTool

# ... existing registrations ...
server._tool_registry.register(WeatherTool())
logger.info("WeatherTool registered")
```

`ToolRegistry.register()` lives in
[`dragon_voice/tools/registry.py`](../../dragon_voice/tools/registry.py). The
registry owns parsing the model's markers, dispatching to your `execute`, and
catching exceptions — there is no separate tool-runner or scheduler to wire up.

> Register order matters in exactly one case: the **compact tool format** for
> local models hard-codes a top-priority list, `_PRIORITY_TOOLS`, in
> `registry.py`. See step 4.

### 3. Know the XML dialects the parser accepts

You write the tool; the registry's parser handles however the model phrases the
call. Three dialects are accepted (see the `registry.py` module docstring and
`LEARNINGS.md` #79):

1. **Legacy** — `<tool>NAME</tool><args>{json}</args>`. The TinkerBox
   system-prompt format; `ministral-3:3b` and `gemma3` emit this. The parser
   tolerates xLAM bracket quirks (`[tool>`, `<tool]`, `[tool]`).
2. **Standard** — `<tool_call>{"name": "...", "arguments": {...}}</tool_call>`.
   The industry-typical function-calling fine-tune format; Qwen-FC, Gemma-FC,
   and `distil-*` emit this regardless of system prompt.
3. **Bracketed-name** — `[NAME]{json}</NAME>` or `[NAME]UPPERCASE_IDENT()`. An
   xLAM quirk (issue #82). Gated on `NAME` being a registered tool name, so prose
   like `[note]` in chat does not false-fire.

The parser is also lenient about small-model noise — stray `>` after `</args>`,
missing closing tags, and similar formatting slips common with models like
`qwen3:1.7b`. You do not need to handle any of this in your tool; your `execute`
only ever sees a parsed args dict.

### 4. (Optional) Add to the compact format for local models

Local models run on a tight context budget, so Dragon sends them a **compact XML
tool format** to save tokens (`format_for_llm(compact=True)` in `registry.py`).
That compact view only includes a top-priority subset, `_PRIORITY_TOOLS`, in
`registry.py`. Cloud-mode models always get the full tool list.

- If your tool is high-traffic and you want local models to see it, add its
  `name` to `_PRIORITY_TOOLS`.
- If it is an opt-in or niche tool, leave it out — cloud-mode LLMs still see it
  and will call it.

### 5. Mind the runtime budget and the agent log

Two pipeline behaviors your tool runs inside:

- **Max 3 tool calls per turn.** `MAX_TOOL_CALLS = 3` caps the
  ConversationEngine's tool loop to prevent infinite chains. A single turn fires
  at most three tools before the model must produce a final reply. Do not write a
  tool that assumes it can be re-invoked many times in one turn.
- **`agent_log`.** Every `ToolRegistry.execute` call is recorded in a
  cross-session ring buffer (last 64 entries) exposed at
  `GET /api/v1/agent_log`. Your tool shows up there automatically — no
  instrumentation needed — which is the fastest way to confirm it actually fired
  during a live turn. Tool names observed here also surface in the merged
  `GET /api/v1/agent_skills` catalog the Tab5 Agents overlay displays.

Also relevant for tools that talk to the network: per-tool timeouts. `asyncio.timeout`
inside `execute` keeps one slow tool from eating the whole turn budget. See the
[long-running tools pattern](../adding-a-tool.md#long-running-tools).

### 6. Write a unit test

Mirror any existing `tests/test_*_tool.py`. Cover the happy path, default/missing
args, and the error branches:

```python
import pytest

from dragon_voice.tools.weather_tool import WeatherTool


@pytest.fixture
def tool():
    return WeatherTool()


@pytest.mark.asyncio
async def test_missing_city_errors(tool):
    result = await tool.execute({})
    assert "error" in result


def test_metadata(tool):
    assert tool.name == "weather"
    assert tool.parameters_schema["type"] == "object"
```

Add the new test file to the named test set in
[`.github/workflows/ci.yml`](../../.github/workflows/ci.yml) so CI runs it — CI
runs only the named unit tests, not the E2E suite.

### 7. Lint and test locally

```bash
ruff check --select F821,F722,F811,F823,B006,B904,E722,B007,RUF006 dragon_voice/ tests/
pytest -q tests/test_weather_tool.py
```

## Verify it worked

Start the server locally and check the tool surfaces, then exercise it directly:

```bash
python3 -m dragon_voice
```

```bash
# Tool is registered and listed
curl -s -H "Authorization: Bearer $DRAGON_API_TOKEN" \
    http://localhost:3502/api/v1/tools | jq '.tools[] | select(.name == "weather")'
# → {"name": "weather", "description": "...", "parameters": {...}}

# Execute it directly
curl -s -X POST -H "Authorization: Bearer $DRAGON_API_TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"city": "Paris"}' \
    http://localhost:3502/api/v1/tools/weather/execute
# → {"city": "Paris", "temp_c": 18, "conditions": "clear"}
```

For the full path, deploy to the Dragon (see
[Deploy on a Dragon](deploy-on-a-dragon.md)) and ask the question that should
trigger your tool. Confirm the model fired the marker and got a result by reading
the agent log:

```bash
curl -s -H "Authorization: Bearer $DRAGON_API_TOKEN" \
    http://localhost:3502/api/v1/agent_log | jq '.[0]'
# → most recent ToolRegistry.execute entry — should name your tool
```

## Troubleshooting

- **Tool never appears in `GET /api/v1/tools`** → it was not registered. Confirm
  your `register(WeatherTool())` line runs in
  [`startup.py`](../../dragon_voice/lifecycle/startup.py) and you restarted
  `tinkerclaw-voice`. After an `scp` deploy, clear stale bytecode first:
  `find /home/radxa/dragon_voice -name '__pycache__' -exec rm -rf {} +`.
- **Local model never calls the tool, but cloud mode does** → the tool is not in
  the compact format. Add its `name` to `_PRIORITY_TOOLS` in
  [`registry.py`](../../dragon_voice/tools/registry.py) (step 4). Local models only
  see the compact subset.
- **Model emits a marker but the reply comes back empty** → some
  function-calling models emit a tool call and stop, leaving no visible text after
  the parser strips the markup. The empty-reply guard in
  [`tools/response_wrap.py`](../../dragon_voice/tools/response_wrap.py) synthesizes a
  one-line acknowledgement from your tool result. If your tool returns an unusual
  shape and the ack reads wrong, add a per-tool wrap function there.
- **`execute` raises and the user sees "Tool execution failed"** → the registry
  caught your exception. Return `{"error": "message"}` instead of raising — a clean
  error dict surfaces to the user far better than a generic failure.
- **Tool fires twice on the same args (sends two emails, charges twice)** → the
  model can re-call within the 3-call budget. Make side-effecting tools idempotent,
  or use the confirmation gate from
  [`memory_tools.py: ForgetFactTool`](../../dragon_voice/tools/memory_tools.py): the
  first call returns `{"requires_confirmation": true, "preview": "..."}` and only a
  follow-up `{"confirmed": true}` executes.
- **Long network call stalls the whole turn** → wrap the network I/O in
  `asyncio.timeout` and return `{"error": "timeout after Ns"}` on expiry. The
  per-turn `MAX_TOOL_CALLS = 3` cap bounds total tool time, but one runaway can
  still consume the entire turn.

## Related

- [Adding a Tool — worked example](../adding-a-tool.md) — the full `dice_roll`
  walkthrough with patterns to follow and patterns to avoid.
- [Swap the LLM backend](swap-the-llm-backend.md) — choose which model runs the
  turn that calls your tool.
- [Deploy on a Dragon](deploy-on-a-dragon.md) — get your change onto the device.
- [Architecture](../ARCHITECTURE.md) — where the ToolRegistry sits in the pipeline.
