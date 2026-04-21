# Authoring a TinkerClaw Widget Skill

*This is the SDK reference for adding a new skill to Dragon that emits
widgets to a connected Tab5.*

A **skill** is a subclass of `dragon_voice.tools.base.Tool` that the LLM
can invoke via the XML tool-call protocol. Skills can be pure-text (like
`CalculatorTool`) or **widget-emitting** — pushing structured UI state to
Tab5's six widget slots (`live`, `card`, `list`, `chart`, `media`, `prompt`).

This doc walks through the minimal viable widget skill end-to-end. The
reference implementation is `dragon_voice/tools/quick_poll_tool.py` —
~80 LOC including docstrings. Read alongside the code.

---

## 1. The contract

Dragon's skill author contract is four obligations plus one constraint:

| # | Obligation | Why |
|---|---|---|
| 1 | Subclass `Tool` and implement `name`, `description`, `parameters_schema`, `execute(args)` | Lets the tool registry + LLM discover and invoke you |
| 2 | Read `session_id` from `args.get("session_id")` — don't expect a kwarg | Conversation engine injects it before calling execute (wave 12) |
| 3 | Resolve the per-session surface via `surface_mgr.surface_for(session_id, skill_id)` | Returns a `Tab5Surface` scoped to *your* skill id, or `None` if the device is disconnected |
| 4 | Use **declarative** `surface.prompt(on_action=handler, ...)` instead of imperative `register_action` | Handler is wired before the emit, so fast taps can't race past it (wave 8 fix) |

**Constraint:** Never block the turn forever. Always give `execute()` a
timeout escape hatch (wait_for, `timeout_s` arg, etc.) so the LLM
doesn't stall if the user walks away.

## 2. Minimum viable skill — QuickPollTool

`dragon_voice/tools/quick_poll_tool.py`:

```python
class QuickPollTool(Tool):
    def __init__(self, surface_manager):
        self._mgr = surface_manager

    @property
    def name(self): return "quick_poll"

    @property
    def description(self):
        return ("Ask the user a quick poll on the Tab5 screen and "
                "wait for the tap. Use when a conversational choice "
                "would benefit from a 2-3 button prompt.")

    @property
    def parameters_schema(self):
        return {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "choices":  {"type": "array", "items": {"type": "string"},
                             "minItems": 2, "maxItems": 3},
                "timeout_s": {"type": "number"},
            },
            "required": ["question", "choices"],
        }

    async def execute(self, args: dict) -> dict:
        session_id = str(args.get("session_id", "unknown"))
        surface = self._mgr.surface_for(session_id, "quick_poll")
        if surface is None:
            return {"error": "no Tab5 surface"}

        picked = {}
        done = asyncio.Event()

        async def _on_tap(event: str, payload: dict):
            picked["answer"] = event
            done.set()

        card_id = await surface.prompt(
            title=args["question"][:60],
            choices=[(label, f"poll.{label.lower()}")
                     for label in args["choices"][:3]],
            priority=75,
            on_action=_on_tap,
        )

        try:
            await asyncio.wait_for(done.wait(),
                                    timeout=float(args.get("timeout_s", 60)))
            return {"answer": picked["answer"].replace("poll.", ""),
                    "timed_out": False}
        except asyncio.TimeoutError:
            return {"answer": args["choices"][0], "timed_out": True}
        finally:
            try: await surface.dismiss(card_id)
            except Exception: pass
```

## 3. Registration

Add one line to `server.py` `_on_startup` after `SurfaceManager` is instantiated:

```python
from dragon_voice.tools.quick_poll_tool import QuickPollTool
self._tool_registry.register(QuickPollTool(self._surface_mgr))
```

Verify it's live:

```bash
curl -s http://dragon:3502/api/v1/tools | jq '.tools[] | select(.name=="quick_poll")'
```

## 4. Why declarative > imperative

Before wave 8, widget-prompt skills had to call:

```python
card_id = await surface.prompt(title=q, choices=...)
surface_mgr.register_action(session_id, card_id, handler)  # ← easy to forget
```

If the user tapped between the two lines (rare but possible under load), the
handler wasn't registered yet — and wave 8 default-dismiss would swallow
the tap. Worse, most half-broken drive-by skills just forgot the second
call entirely.

Now the manager binds `_manager` + `_session_id` onto the surface at
session registration (wave 10 B6/K3). `surface.prompt(on_action=h)`
registers before emit so the race window is gone. If you still pass no
`on_action`, the default-dismiss guard fires when the user taps — cleaner
than a silent no-op.

## 5. Available widget types

| Helper | WS type | Slot |
|---|---|---|
| `surface.live(...)` | `widget_live` | home live slot (one at a time) |
| `surface.card(...)` | `widget_card` | chat bubble |
| `surface.list(...)` | `widget_list` | home live slot |
| `surface.chart(...)` | `widget_chart` | home live slot |
| `surface.media(url=...)` | `widget_media` | home live slot (JPEG) |
| `surface.prompt(...)` | `widget_prompt` | home live slot (button choices) |
| `surface.dismiss(cid)` | `widget_dismiss` | clears a specific card |
| `surface.clear_all()` | `widget_clear_all` | clear your skill's cards |

Full protocol shapes: see `docs/protocol.md` §17 and `TinkerTab/docs/WIDGETS.md`.

## 6. Respect device caps

`surface_mgr.register_session(caps=...)` is called with the `widget_capabilities`
payload Tab5 sends in its register frame. The surface reads `caps.list_max_items`,
`caps.chart_max_points`, `caps.prompt_max_choices`, `caps.media_max_w/h` and
truncates/resizes before emit (wave 8). Your skill can trust the helper to
honor the device's declared limits — pass more items than the device accepts
and the tail is silently dropped, no error.

## 7. Testing your skill

The 8-story user E2E in `tests/audit/test_user_stories.py` has a
`widget_prompt` story that exercises `/debug/widget_prompt`. For your own
skill add a similar asyncio story:

```python
async def story_my_skill():
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(DRAGON_WS) as ws:
            await _register(ws, voice_mode=2)
            await ws.send_json({"type": "text",
                "content": "poll me with Yes or No"})
            # Wait for the widget_prompt event, emulate a tap via
            # widget_action, assert the skill returns the tapped value
            # in the LLM final response.
```

## 8. Naming + co-existing with existing tools

Name your skill with a unique string (the `name` property). The LLM
picks based on `description`, so be specific about when to invoke
vs. not. Example: TimerTool and TimesenseTool *used to* coexist both
claiming "set a timer" — the LLM picked TimerTool (first registered,
text-only) and TimesenseTool's widget-emitting path went dead.
Wave 7 resolved the duplication by removing TimerTool's registration.

If you're adding a poll-like skill, pick a unique trigger (`quick_poll`)
so the LLM doesn't confuse it with TimerTool/TimesenseTool conventions.

## 9. Error patterns

| Condition | Return |
|---|---|
| Missing required arg | `{"error": "question is required"}` |
| `surface_for` returns None (Tab5 offline) | `{"error": "no Tab5 surface"}` |
| Timeout waiting for tap | `{"answer": <default>, "timed_out": True}` — give the LLM something to continue with |
| Unexpected exception | Let it propagate; the registry wraps with `{"tool": name, "error": str(e)}` |

## 10. Changelog

- **Wave 6** — rich-media chat (`surface.card`, `surface.media`)
- **Wave 7** — TimerTool dedup in favor of TimesenseTool
- **Wave 8** — declarative `on_action=handler` on `surface.prompt()`
- **Wave 10** — conversation engine injects `session_id` into tool args
- **Wave 12** — this guide + `QuickPollTool` reference
