"""Note tool: take quick notes via the Dragon Notes service."""

import logging

from dragon_voice.tools.base import Tool

logger = logging.getLogger(__name__)


class NoteTool(Tool):
    """Take a quick note or create a reminder via the Notes service."""

    def __init__(self, notes_service) -> None:
        self._notes_svc = notes_service

    @property
    def name(self) -> str:
        return "note"

    @property
    def description(self) -> str:
        return "Take a quick note or create a reminder"

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The note content (e.g., 'Buy groceries tomorrow')",
                },
            },
            "required": ["text"],
        }

    async def execute(self, args: dict) -> dict:
        text = args.get("text", "").strip()
        if not text:
            return {"error": "text is required"}

        try:
            note = await self._notes_svc.create_from_text(text)
            logger.info("Note created: %s (id=%s)", text[:60], note.id)
            return {
                "id": note.id,
                "title": note.title,
                "text": text,
                "created": True,
            }
        except Exception as e:
            logger.exception("Failed to create note")
            return {"error": f"Failed to create note: {e}"}
