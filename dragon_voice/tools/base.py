"""Abstract base class for tools."""

from abc import ABC, abstractmethod


class Tool(ABC):
    """Base class for all tools in the registry."""

    # Compact-format priority — lower = higher priority for the
    # small-model system prompt slot.  Default 50 means "not in
    # the compact prompt"; set explicitly < 50 to include in the
    # small-model block (e.g. priority=10 for must-have tools
    # like web_search).
    #
    # Wave 23 audit OCP-3 closure: lets new tools opt into the
    # compact slot without editing
    # `dragon_voice.tools.formatter.COMPACT_PRIORITY_TOOLS`.
    # Backward compat: tools registered via the legacy name list
    # (COMPACT_PRIORITY_TOOLS) ALSO appear in compact regardless
    # of their priority value, so existing subclasses don't need
    # to set this attribute to keep working.
    priority: int = 50

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
