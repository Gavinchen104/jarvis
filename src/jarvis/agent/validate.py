"""JSON-Schema validation for tool-call arguments.

Native tool-calling (PHASE3.md §3) does not mean trusted: a local 7B still
emits missing or wrong-typed arguments. The tool-use loop validates every
call against the tool's schema and feeds failures back to the model to
self-correct (retry-with-error). `jsonschema` is already available as an
MCP transitive dependency.
"""

from typing import Any

from jsonschema import Draft202012Validator


def validate_arguments(
    arguments: dict[str, Any], schema: dict[str, Any]
) -> tuple[bool, str]:
    """Return (ok, error_message). error_message is '' when ok."""
    if not schema:
        return True, ""
    errors = sorted(
        Draft202012Validator(schema).iter_errors(arguments),
        key=lambda e: list(e.path),
    )
    if not errors:
        return True, ""
    msg = "; ".join(
        f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in errors
    )
    return False, msg
