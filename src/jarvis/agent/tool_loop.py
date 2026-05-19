"""The defensive tool-use loop — the engineering core of Phase 3.

A naive "pass tools, hope for the best" loop with a local 7B is ~60-80%
reliable. The guards here (iteration cap, unknown-tool check, schema
validation, retry-with-error, execution try/except, graceful fallback) are
what push it higher. Each guard maps to a documented failure mode — see
PHASE3.md §2 and §6. The risk gate is enforced *here*, not in the tool, so
the safety property holds even if a tool forgets (DESIGN.md §6.1).
"""

from typing import Any

from jarvis.agent.llm import chat
from jarvis.agent.validate import validate_arguments
from jarvis.config import settings
from jarvis.tools.registry import Registry


def _assistant_msg(content: str, tool_calls: list[dict]) -> dict:
    msg: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = [
            {"function": {"name": c["name"], "arguments": c["arguments"]}}
            for c in tool_calls
        ]
    return msg


def _tool_msg(name: str, content: str) -> dict:
    return {"role": "tool", "tool_name": name, "content": content}


def run_tool_loop(messages: list[dict], registry: Registry) -> str:
    """Drive the model through any tool calls until it produces a text answer.

    `messages` is mutated in place (assistant + tool turns appended) so the
    caller's session history stays coherent. Returns the final spoken text.
    """
    tools = registry.ollama_schemas()

    for _ in range(settings.max_tool_iters):
        resp = chat(messages, tools=tools)
        content, tool_calls = resp["content"], resp["tool_calls"]

        # Plain-text answer → done.
        if not tool_calls:
            messages.append(_assistant_msg(content, []))
            return content or "I'm not sure how to answer that."

        # Record the assistant's tool-call turn before resolving it.
        messages.append(_assistant_msg(content, tool_calls))

        for call in tool_calls:
            name = call["name"]
            args = call["arguments"]

            tool = registry.get(name)
            if tool is None:  # hallucinated a tool that doesn't exist
                messages.append(
                    _tool_msg(name, f"error: no such tool '{name}'")
                )
                continue

            # Risk gate. Phase 3 only ships read tools; write/destructive
            # would route to voice-confirm here (Phase 4). Fail safe.
            if tool.risk_level != "read":
                messages.append(
                    _tool_msg(name, "error: confirmation required (not yet supported)")
                )
                continue

            ok, err = validate_arguments(args, tool.schema)
            if not ok:  # retry-with-error: let the model self-correct next iter
                messages.append(
                    _tool_msg(name, f"error: invalid arguments: {err}")
                )
                continue

            try:
                result = tool.run(args)
            except Exception as exc:  # noqa: BLE001 - MCP crash / timeout / API down
                messages.append(_tool_msg(name, f"error: tool failed: {exc}"))
                continue

            messages.append(_tool_msg(name, result))

    # Ran out of iterations without a text answer.
    return "I wasn't able to finish that — the tool calls didn't resolve."
