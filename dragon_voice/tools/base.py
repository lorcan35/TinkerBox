"""Abstract base class for tools."""

from abc import ABC, abstractmethod


class Tool(ABC):
    """Base class for all tools in the registry."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique tool name (used in LLM output parsing)."""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable description (injected into LLM system prompt)."""
        ...

    @property
    @abstractmethod
    def parameters_schema(self) -> dict:
        """JSON Schema for the tool's arguments."""
        ...

    @abstractmethod
    async def execute(self, args: dict) -> dict:
        """Execute the tool with given arguments. Returns a result dict."""
        ...

    def to_dict(self) -> dict:
        """Serialize tool metadata for API responses."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters_schema,
        }
