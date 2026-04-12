"""Timer/Alarm tool: set countdown timers and alarms."""

import logging
import time
from datetime import datetime, timedelta

from dragon_voice.tools.base import Tool

logger = logging.getLogger(__name__)

# In-memory timer storage (persists for server lifetime)
_active_timers: list[dict] = []
_timer_counter = 0


class TimerTool(Tool):
    """Set a countdown timer or alarm. Returns when it will go off."""

    @property
    def name(self) -> str:
        return "timer"

    @property
    def description(self) -> str:
        return "Set a countdown timer or alarm. Returns when it will go off."

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "duration_seconds": {
                    "type": "integer",
                    "description": "Timer duration in seconds",
                },
                "minutes": {
                    "type": "number",
                    "description": "Timer duration in minutes (alternative to duration_seconds)",
                },
                "label": {
                    "type": "string",
                    "description": "Optional label for the timer (e.g., 'pasta')",
                },
            },
        }

    async def execute(self, args: dict) -> dict:
        global _timer_counter

        duration_seconds = args.get("duration_seconds")
        minutes = args.get("minutes")
        label = args.get("label", "")

        # Resolve duration
        if duration_seconds is not None:
            duration = int(duration_seconds)
        elif minutes is not None:
            duration = int(float(minutes) * 60)
        else:
            return {"error": "Provide either 'duration_seconds' or 'minutes'"}

        if duration <= 0:
            return {"error": "Duration must be positive"}
        if duration > 86400:
            return {"error": "Maximum timer duration is 24 hours"}

        now = datetime.now()
        fires_at = now + timedelta(seconds=duration)

        _timer_counter += 1
        timer_id = f"timer_{_timer_counter}"

        timer = {
            "id": timer_id,
            "label": label,
            "duration_seconds": duration,
            "created_at": now.isoformat(),
            "fires_at": fires_at.isoformat(),
            "fires_at_unix": time.time() + duration,
        }
        _active_timers.append(timer)

        # Format human-readable duration
        if duration >= 3600:
            h = duration // 3600
            m = (duration % 3600) // 60
            duration_str = f"{h}h {m}m" if m else f"{h}h"
        elif duration >= 60:
            m = duration // 60
            s = duration % 60
            duration_str = f"{m}m {s}s" if s else f"{m}m"
        else:
            duration_str = f"{duration}s"

        logger.info("Timer set: %s (%s) fires at %s", timer_id, label or "no label", fires_at)

        return {
            "id": timer_id,
            "label": label,
            "duration": duration_str,
            "fires_at": fires_at.strftime("%H:%M:%S"),
            "message": f"Timer set for {duration_str}" + (f" ({label})" if label else ""),
        }
