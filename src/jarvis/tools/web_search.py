"""The web_search tool — Phase 3's first and only tool.

We expose a deliberately minimal schema (just `query`) and our own
prompt-engineered description rather than passing the MCP server's raw tool
through. Fewer arguments = fewer ways a local 7B can produce a malformed
call; the description is the main lever on *when* the model decides to
search (PHASE3.md §5.2). The underlying DDG MCP tool is named `search`.
"""

from typing import Any

from jarvis.tools.mcp_client import MCPClient
from jarvis.tools.registry import Registry, Tool

_DESCRIPTION = (
    "Search the web for current, real-time, or factual information the "
    "assistant cannot know from training: weather, news, prices, sports "
    "scores, recent events, or anything time-sensitive. Returns ranked "
    "result snippets. Do not use it for things you already know (basic "
    "facts, arithmetic, definitions) — searching is slower and only helps "
    "when the answer depends on current data."
)

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "A specific, descriptive search query.",
        }
    },
    "required": ["query"],
}


def register_web_search(registry: Registry, client: MCPClient) -> None:
    """Register `web_search` (risk: read) backed by the DDG MCP `search` tool."""

    def _run(args: dict[str, Any]) -> str:
        return client.call_tool("search", {"query": args["query"]})

    registry.register(
        Tool(
            name="web_search",
            description=_DESCRIPTION,
            schema=_SCHEMA,
            risk_level="read",
            run=_run,
        )
    )
