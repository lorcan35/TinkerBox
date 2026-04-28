# Adding a Tool — Worked Example

> Build a real agentic tool from scratch.  By the end of this doc
> you'll have a working `dice_roll` tool that the LLM can call when
> a user says "roll a d20" or "give me three random numbers between
> 1 and 100" — registered, tested, and merged.
>
> Estimated time: 30-45 minutes (most of it reading + understanding,
> not typing).

## What is a tool?

A **tool** is a Python class that the LLM can invoke during a
conversation by emitting a marker like `<tool>NAME</tool><args>{...}</args>`.
Dragon parses the marker, calls your tool with the JSON args, and
injects the result back into the LLM's context so it can keep going.

This is how the assistant goes from "knowing only what the LLM was
trained on" to "able to actually do things" — fetch weather, search
the web, save memory facts, look up the time, do math, etc.

15 tools ship with Dragon today.  Look in
[`dragon_voice/tools/`](../dragon_voice/tools/) for examples.

## The Tool contract

Every tool subclasses `dragon_voice.tools.base.Tool`:

```python
from dragon_voice.tools.base import Tool


class MyTool(Tool):
    @property
    def name(self) -> str: ...

    @property
    def description(self) -> str: ...

    @property
    def parameters_schema(self) -> dict: ...

    async def execute(self, args: dict) -> dict: ...
```

Four pieces:

| Method | Purpose |
|--------|---------|
| `name` | Unique identifier the LLM emits.  Snake_case, matches the `<tool>name</tool>` marker. |
| `description` | One-line human-readable description.  Injected into the LLM system prompt; this is what the model uses to decide when to call your tool. |
| `parameters_schema` | JSON Schema describing the args.  Same format as OpenAI function-calling. |
| `execute` | Async method.  Receives the parsed args dict, returns a result dict.  Whatever you return gets serialised to JSON and shown to the LLM as the tool result. |

That's the whole API.  No middleware, no tool-runner, no scheduling.
The registry handles all of that.

---

## Walkthrough — build `dice_roll`

### 1. Open an issue

```bash
gh issue create --title "feat(tools): dice_roll tool for random number generation" --body "$(cat <<'EOF'
## Why
Users frequently ask "roll a d20" or "pick a random number between
X and Y" in casual conversation.  The local LLM has to fake this
without entropy, which gives bad results (it'll often pick "7"
because that's a common answer in training data).

## What
New tool `dice_roll` with two args:
- `count`: int (default 1) — number of dice
- `sides`: int (default 6) — sides per die

Returns:
- `rolls`: list of ints
- `total`: sum of rolls
- `count` + `sides`: echo of inputs

## Acceptance
- LLM correctly fires the tool on natural-language prompts
- Edge cases handled: count > 100 capped, sides between 2-1000
- Unit tests cover happy path + arg validation
EOF
)"
```

Suppose this becomes issue **#999**.

### 2. Branch

```bash
git checkout main && git pull --ff-only origin main
git checkout -b feat/dice-roll-tool
```

### 3. Write the tool

Create `dragon_voice/tools/dice_roll_tool.py`:

```python
"""Dice-roll tool: roll N dice with M sides for the LLM."""

import random

from dragon_voice.tools.base import Tool


class DiceRollTool(Tool):
    """Rolls dice with configurable count + sides."""

    # Caps protect against runaway agents; LLMs occasionally
    # decide to "roll a billion dice" if you let them.
    MAX_COUNT = 100
    MAX_SIDES = 1000

    @property
    def name(self) -> str:
        return "dice_roll"

    @property
    def description(self) -> str:
        return (
            "Roll one or more fair dice. Use when the user asks for a "
            "random number, dice roll, or to pick randomly from a range. "
            "Returns the individual rolls and their sum."
        )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "count": {
                    "type": "integer",
                    "description": "Number of dice to roll (1-100). Default 1.",
                    "default": 1,
                    "minimum": 1,
                    "maximum": self.MAX_COUNT,
                },
                "sides": {
                    "type": "integer",
                    "description": "Sides per die (2-1000). Default 6.",
                    "default": 6,
                    "minimum": 2,
                    "maximum": self.MAX_SIDES,
                },
            },
        }

    async def execute(self, args: dict) -> dict:
        # Defensive arg parsing — the LLM might pass strings instead
        # of ints, omit fields, or pass bonkers values. Validate here
        # rather than crash; surface a clean error.
        try:
            count = int(args.get("count", 1))
            sides = int(args.get("sides", 6))
        except (TypeError, ValueError):
            return {"error": "count and sides must be integers"}

        if count < 1 or count > self.MAX_COUNT:
            return {"error": f"count must be between 1 and {self.MAX_COUNT}"}
        if sides < 2 or sides > self.MAX_SIDES:
            return {"error": f"sides must be between 2 and {self.MAX_SIDES}"}

        rolls = [random.randint(1, sides) for _ in range(count)]
        return {
            "rolls": rolls,
            "total": sum(rolls),
            "count": count,
            "sides": sides,
        }
```

### 4. Register the tool

Tools are registered at startup in
[`dragon_voice/lifecycle/startup.py`](../dragon_voice/lifecycle/startup.py).
Find the section that constructs the `ToolRegistry` (search for `tool_registry.register`) and add:

```python
from dragon_voice.tools.dice_roll_tool import DiceRollTool

# ... existing registrations ...

server._tool_registry.register(DiceRollTool())
logger.info("DiceRollTool registered")
```

> **Note:** the *order* of registration matters for one specific case
> — the small-LLM compact-tool format hard-codes a top-5 priority
> list in [`tools/registry.py: _PRIORITY_TOOLS`](../dragon_voice/tools/registry.py).
> If you want your tool included in the compact format that local
> models see, add the name there too.  For an opt-in tool like
> `dice_roll` that's not high-traffic, leaving it out is fine —
> cloud-mode LLMs see the full tool list and will use it.

### 5. Write tests

Create `tests/test_dice_roll_tool.py`:

```python
"""Unit tests for the dice_roll tool (closes #999)."""

import pytest

from dragon_voice.tools.dice_roll_tool import DiceRollTool


@pytest.fixture
def tool():
    return DiceRollTool()


@pytest.mark.asyncio
async def test_default_args(tool):
    """Default: 1 die, 6 sides."""
    result = await tool.execute({})
    assert "rolls" in result
    assert len(result["rolls"]) == 1
    assert 1 <= result["rolls"][0] <= 6
    assert result["count"] == 1
    assert result["sides"] == 6
    assert result["total"] == result["rolls"][0]


@pytest.mark.asyncio
async def test_multiple_dice(tool):
    """3d20 — three twenty-sided dice."""
    result = await tool.execute({"count": 3, "sides": 20})
    assert len(result["rolls"]) == 3
    for r in result["rolls"]:
        assert 1 <= r <= 20
    assert result["total"] == sum(result["rolls"])


@pytest.mark.asyncio
async def test_string_args_coerced(tool):
    """LLM might pass '5' instead of 5 — coerce."""
    result = await tool.execute({"count": "5", "sides": "10"})
    assert len(result["rolls"]) == 5


@pytest.mark.asyncio
async def test_count_too_high_rejected(tool):
    result = await tool.execute({"count": 1000})
    assert "error" in result
    assert "count must be between 1 and 100" in result["error"]


@pytest.mark.asyncio
async def test_sides_too_low_rejected(tool):
    result = await tool.execute({"sides": 1})
    assert "error" in result


@pytest.mark.asyncio
async def test_invalid_arg_type(tool):
    result = await tool.execute({"count": "blue"})
    assert "error" in result


def test_metadata(tool):
    """Tool metadata is well-formed."""
    assert tool.name == "dice_roll"
    assert "random" in tool.description.lower()

    schema = tool.parameters_schema
    assert schema["type"] == "object"
    assert "count" in schema["properties"]
    assert schema["properties"]["count"]["maximum"] == 100
    assert "sides" in schema["properties"]
    assert schema["properties"]["sides"]["minimum"] == 2


def test_to_dict(tool):
    """Serialised form for API responses."""
    d = tool.to_dict()
    assert d["name"] == "dice_roll"
    assert "description" in d
    assert "parameters" in d
```

Add the new test file to the CI named-set in `.github/workflows/ci.yml`:

```yaml
# in ci.yml's pytest invocation
- tests/test_dice_roll_tool.py
```

### 6. Run tests locally

```bash
pytest -q tests/test_dice_roll_tool.py
# Should pass 8/8.

# Full suite check
pytest tests/ --ignore=tests/audit -q
# Should pass 564/564 (was 556 before your tests).
```

### 7. Lint

```bash
ruff check --select F821,F722,F811,F823,B006,B904,E722,B007,RUF006 dragon_voice/ tests/
# Should pass.
```

### 8. Test live (optional but recommended)

If you have Dragon running locally:

```bash
python3 -m dragon_voice
```

In another terminal:

```bash
# Hit the tools listing endpoint
curl -s -H "Authorization: Bearer $DRAGON_API_TOKEN" \
    http://localhost:3502/api/v1/tools | jq '.tools[] | select(.name == "dice_roll")'

# Should show your tool's metadata.

# Execute the tool directly
curl -s -X POST -H "Authorization: Bearer $DRAGON_API_TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"count": 3, "sides": 20}' \
    http://localhost:3502/api/v1/tools/dice_roll/execute

# Should return {"rolls": [...], "total": ..., "count": 3, "sides": 20}
```

For full LLM integration, deploy to Dragon and ask "roll three d20" via Tab5.  The LLM should fire `<tool>dice_roll</tool><args>{"count":3,"sides":20}</args>`, get back the result, and announce the rolls.

### 9. Commit + PR

```bash
git add dragon_voice/tools/dice_roll_tool.py \
        tests/test_dice_roll_tool.py \
        dragon_voice/lifecycle/startup.py \
        .github/workflows/ci.yml

git commit -m "$(cat <<'EOF'
feat(tools): dice_roll tool for random number generation (closes #999)

LLMs without an RNG fake "random" by picking common-in-training
numbers (often "7" for d20).  This adds a real entropy-backed
tool the LLM can call when a user asks for a random number or
dice roll.

Tool surface
------------
- name: dice_roll
- args: count (1-100, default 1), sides (2-1000, default 6)
- returns: rolls (list[int]), total (int), count, sides

Caps protect against runaway agents asking for a billion dice.
Defensive arg parsing returns {"error": "..."} on bad input
rather than raising.

Tests
-----
8 assertions: default args, multiple dice, string-coerced args,
upper/lower-bound rejection, invalid type, metadata correctness,
to_dict serialisation.  Full suite: 564 passed (+8), 0 regressions.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"

git push -u origin feat/dice-roll-tool

gh pr create --title "feat(tools): dice_roll tool for random number generation (closes #999)" --body "..."
```

### 10. Squash-merge

After CI green and review:

```bash
gh pr merge --squash --delete-branch
```

---

## Patterns to follow

### Defensive arg parsing

LLMs sometimes return weird args.  Coerce + validate:

```python
async def execute(self, args: dict) -> dict:
    try:
        count = int(args.get("count", 1))
    except (TypeError, ValueError):
        return {"error": "count must be an integer"}
    # ...
```

### Returning errors

Return `{"error": "message"}` instead of raising.  The registry
catches exceptions but a clean error dict gets surfaced to the user
better than "Tool execution failed."

### Long-running tools

If your tool talks to the network, don't hold up the LLM:

```python
async def execute(self, args: dict) -> dict:
    timeout = args.get("timeout_s", 5)
    try:
        async with asyncio.timeout(timeout):
            result = await self._fetch(args["url"])
    except asyncio.TimeoutError:
        return {"error": f"timeout after {timeout}s"}
    # ...
```

`MAX_TOOL_CALLS=3` per turn already caps total tool time, but per-tool timeouts prevent one runaway from eating the whole budget.

### Side-effect tools (state changes)

For tools that change state (memory, files, external APIs), think about idempotency.  An LLM might call your tool twice on the same args.  If that's bad (sends two emails, charges twice), debounce inside the tool or accept an idempotency key.

The `remember` tool ([`memory_tools.py`](../dragon_voice/tools/memory_tools.py)) handles this by deduplicating embeddings — same fact text → no second insert.

### Confirmation-gated tools

For tools with irreversible consequences (delete, send, charge), use the confirmation pattern from
[`tools/memory_tools.py: ForgetFactTool`](../dragon_voice/tools/memory_tools.py).  The first call returns `{"requires_confirmation": true, "preview": "..."}`; the LLM's follow-up call with `{"confirmed": true}` actually executes.

### WebSocket events from inside a tool

Tools sometimes need to surface progress to Tab5.  The registry passes `on_tool_call` / `on_tool_result` callbacks; tools that need richer events should use the unified `progress` event bus (β-arch, issue #123) rather than inventing per-tool event types.

---

## Patterns to avoid

- **Don't query Dragon's own DB from a tool.**  Use the existing services (MemoryService, MessageStore).  Direct DB access from a tool bypasses the deep-copy-per-connection isolation.
- **Don't call the LLM from inside a tool.**  Tools are leaf operations.  Recursive LLM calls happen at the ConversationEngine level (capped at MAX_TOOL_CALLS=3).
- **Don't make the description more than ~100 characters.**  The system prompt is precious context; long descriptions push out memory facts and history.
- **Don't use `print` or `logging.info` for tool I/O.**  Return the result.  Logging is for diagnostics, not for surfacing data to the LLM.

---

## Reference

- Tool ABC: [`dragon_voice/tools/base.py`](../dragon_voice/tools/base.py)
- Registry: [`dragon_voice/tools/registry.py`](../dragon_voice/tools/registry.py)
- Existing tools (15+): [`dragon_voice/tools/`](../dragon_voice/tools/)
- Tool-calling dialect spec: registry.py module docstring
- Empty-reply guard for FC-only models: [`tools/response_wrap.py`](../dragon_voice/tools/response_wrap.py)
- Test patterns: any `tests/test_*_tool.py` file
- Conventions: [`../CONTRIBUTING.md`](../CONTRIBUTING.md)

---

## Where to next

You've shipped a tool.  Next ideas:
- **Skill that uses your tool** — [`SKILL_AUTHORING.md`](SKILL_AUTHORING.md)
- **MCP-bridged tool** — point at an external Model Context Protocol server in [`dragon_voice/mcp/`](../dragon_voice/mcp/)
- **Tool that emits widgets to Tab5** — see `timesense_tool.py` for the reference (Pomodoro emits `widget_live` state)
- **Channel-specific tool** — e.g., a Slack-message tool that only fires when invoked from a Slack adapter
