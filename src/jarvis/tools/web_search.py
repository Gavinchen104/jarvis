"""The web_search tool — Phase 3's first and only tool.

We expose a deliberately minimal schema (just `query`) and our own
prompt-engineered description rather than passing the MCP server's raw tool
through. Fewer arguments = fewer ways a local 7B can produce a malformed
call; the description is the main lever on *when* the model decides to
search (PHASE3.md §5.2).

The underlying tool name is *discovered* from the MCP server at
registration time, not hardcoded — DDG calls it `search`, Tavily calls it
`tavily-search`, Brave uses `brave_web_search`. Discovery makes the
provider a config-only change.
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

# Don't pick these even if "search" appears in the name — they're auxiliary
# tools Tavily/Brave/etc. ship alongside the main search endpoint.
_AUX_KEYWORDS = ("extract", "crawl", "map", "fetch", "scrape", "summari")


def _pick_search_tool(client: MCPClient) -> str:
    """Find the MCP server's primary search tool by name; raise if absent."""
    tools = client.list_tools()
    matches = [t for t in tools if "search" in t.name.lower()]
    if not matches:
        names = [t.name for t in tools]
        raise RuntimeError(
            f"no search tool advertised by MCP server (saw: {names})"
        )
    primary = [
        t for t in matches if not any(k in t.name.lower() for k in _AUX_KEYWORDS)
    ]
    return (primary or matches)[0].name


def register_web_search(registry: Registry, client: MCPClient) -> None:
    """Register `web_search` (risk: read) backed by whatever search tool the
    connected MCP server advertises (DDG `search`, Tavily `tavily-search`, …).
    """
    underlying = _pick_search_tool(client)

    def _run(args: dict[str, Any]) -> str:
        return client.call_tool(underlying, {"query": args["query"]})

    registry.register(
        Tool(
            name="web_search",
            description=_DESCRIPTION,
            schema=_SCHEMA,
            risk_level="read",
            run=_run,
        )
    )
