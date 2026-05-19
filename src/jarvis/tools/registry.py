"""Central tool registry.

Every tool the agent can call is registered here with a `risk_level`. The
tool-use loop (not the tool) enforces the risk gate, so the safety property
holds even if a tool author forgets — see DESIGN.md §6.1. Phase 3 introduces
the registry on a read-only tool; the voice-confirm machinery for write/
destructive tools is built on top of it in Phase 4.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

RiskLevel = Literal["read", "write", "destructive"]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str  # what the model sees — the biggest lever on call accuracy
    schema: dict[str, Any]  # JSON Schema for arguments (used for validation)
    risk_level: RiskLevel
    run: Callable[[dict[str, Any]], str]  # executes the tool, returns text

    def to_ollama(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.schema or {"type": "object", "properties": {}},
            },
        }


@dataclass
class Registry:
    _tools: dict[str, Tool] = field(default_factory=dict)

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def ollama_schemas(self) -> list[dict[str, Any]]:
        return [t.to_ollama() for t in self._tools.values()]
