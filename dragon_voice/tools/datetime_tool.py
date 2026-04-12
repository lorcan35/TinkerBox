"""DateTime tool: provides current date/time to the LLM."""

from datetime import datetime, timezone

from dragon_voice.tools.base import Tool


class DateTimeTool(Tool):
    """Returns current date, time, and timezone."""

    @property
    def name(self) -> str:
        return "datetime"

    @property
    def description(self) -> str:
        return "Get the current date, time, and day of the week"

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {},
        }

    async def execute(self, args: dict) -> dict:
        now = datetime.now()
        utc = datetime.now(timezone.utc)
        return {
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M:%S"),
            "day": now.strftime("%A"),
            "timezone": str(now.astimezone().tzinfo),
            "utc": utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "unix": int(utc.timestamp()),
        }
