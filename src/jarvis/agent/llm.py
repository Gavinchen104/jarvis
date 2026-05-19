"""Thin wrapper around Ollama's chat API. Phase 2: no tools yet."""

from functools import lru_cache

from jarvis.config import settings


@lru_cache(maxsize=1)
def _client():
    from ollama import Client

    return Client(host=settings.ollama_host)


def chat(messages: list[dict]) -> str:
    """Send a message list to the local model, return the reply text.

    `messages` is the OpenAI-style [{role, content}, ...] list, system
    prompt included by the caller.
    """
    try:
        resp = _client().chat(model=settings.ollama_model, messages=messages)
    except Exception as exc:  # noqa: BLE001 - surface a usable hint to the user
        raise RuntimeError(
            f"Ollama call failed ({exc}). Is `ollama serve` running and "
            f"`{settings.ollama_model}` pulled? Try: ollama list"
        ) from exc

    return resp["message"]["content"].strip()
