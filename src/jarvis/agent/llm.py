"""Thin wrapper around Ollama's chat API.

Phase 3: supports native tool-calling. We use Ollama's structured `tools=`
path (not prompt-based JSON extraction) because Qwen 2.5 is trained for it
and parsing tool calls out of prose is the fragile path — see PHASE3.md §3.
Returns a normalized dict so the tool-use loop never touches the ollama
client's object types.
"""

from functools import lru_cache
from typing import Any

from jarvis.config import settings


@lru_cache(maxsize=1)
def _client():
    from ollama import Client

    return Client(host=settings.ollama_host)


def chat(
    messages: list[dict], tools: list[dict] | None = None
) -> dict[str, Any]:
    """Send messages (+ optional tool schemas) to the model.

    Returns ``{"content": str, "tool_calls": [{"name", "arguments"}, ...]}``.
    `tool_calls` is empty when the model answered with plain text.
    """
    try:
        resp = _client().chat(
            model=settings.ollama_model, messages=messages, tools=tools or None
        )
    except Exception as exc:  # noqa: BLE001 - surface a usable hint to the user
        raise RuntimeError(
            f"Ollama call failed ({exc}). Is `ollama serve` running and "
            f"`{settings.ollama_model}` pulled? Try: ollama list"
        ) from exc

    msg = resp.message
    tool_calls = [
        {
            "name": tc.function.name,
            "arguments": dict(tc.function.arguments or {}),
        }
        for tc in (msg.tool_calls or [])
    ]
    return {"content": (msg.content or "").strip(), "tool_calls": tool_calls}
