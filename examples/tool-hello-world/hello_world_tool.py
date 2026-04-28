"""Hello-world tool — smallest possible agentic tool.

What it does
------------
The LLM can call this tool to greet someone by name.  Returns a
JSON object with the greeting and a timestamp.  Useful as a
template; not useful in production.

How to use it
-------------
1. Copy this file into ``dragon_voice/tools/hello_world_tool.py``.
2. Register it in ``dragon_voice/lifecycle/startup.py`` next to the
   other ``server._tool_registry.register(...)`` calls::

       from dragon_voice.tools.hello_world_tool import HelloWorldTool
       server._tool_registry.register(HelloWorldTool())

3. Restart ``tinkerclaw-voice`` (or your local ``python3 -m
   dragon_voice``).  The LLM will see the new tool in its system
   prompt and can invoke it on prompts like "say hi to Alice".

Test it
-------
    curl -s -X POST -H "Authorization: Bearer $DRAGON_API_TOKEN" \\
         -H "Content-Type: application/json" \\
         -d '{"name": "World"}' \\
         http://localhost:3502/api/v1/tools/hello_world/execute

Should return::

    {"greeting": "Hello, World!", "at": "..."}

For a fuller worked example with tests + caps + arg validation,
see ``docs/adding-a-tool.md``.
"""

from datetime import datetime, timezone

from dragon_voice.tools.base import Tool


class HelloWorldTool(Tool):
    """Greet someone by name."""

    @property
    def name(self) -> str:
        return "hello_world"

    @property
    def description(self) -> str:
        # Keep this under ~100 chars — it's injected into every
        # LLM system prompt and pushes out memory facts + history.
        return (
            "Say hello to someone by name. Use when the user asks "
            "to greet a person."
        )

    @property
    def parameters_schema(self) -> dict:
        # JSON Schema — same shape as OpenAI function-calling.
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Person to greet.",
                    "default": "World",
                },
            },
        }

    async def execute(self, args: dict) -> dict:
        # Defensive arg parsing — the LLM occasionally returns weird
        # values.  Coerce-to-string + sane default beats raising.
        name = str(args.get("name") or "World")
        return {
            "greeting": f"Hello, {name}!",
            "at": datetime.now(timezone.utc).isoformat(),
        }
