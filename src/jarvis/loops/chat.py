import time
from datetime import datetime

from jarvis.agent.prompt import SYSTEM_PROMPT
from jarvis.agent.tool_loop import run_tool_loop
from jarvis.audio.recorder import record_until_silence
from jarvis.audio.stt import get_model, transcribe
from jarvis.audio.tts import speak
from jarvis.audio.wakeword import wait_for_wake_word, warm_wake_model
from jarvis.config import settings
from jarvis.tools.mcp_client import MCPClient
from jarvis.tools.registry import Registry
from jarvis.tools.web_search import register_web_search


def run_chat() -> None:
    """Phase 3 loop: wake -> Whisper -> agent (LLM + tools) -> macOS say.

    Session-only history (persistence is Phase 7). The MCP server starts
    once and stays alive for the process. A [timing] line per turn keeps
    tool-augmented latency measured, not guessed.
    """
    print("Warming up Whisper...")
    get_model()
    warm_wake_model()  # build wake model now, not on the first "Hey Jarvis"

    registry = Registry()
    mcp = MCPClient()
    try:
        print(f"Starting search tool ({settings.search_provider})...")
        mcp.start()
        register_web_search(registry, mcp)
        print(f"Tools ready: {[t.name for t in registry.all()]}")
    except Exception as exc:  # noqa: BLE001 - degrade to no-tools, don't crash
        print(f"[warn] search tool unavailable ({exc}); running without tools.")

    print(f"Ready ({settings.ollama_model}). Say 'Hey Jarvis' to wake. Ctrl-C to quit.")
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    wake_count = 0

    try:
        while True:
            wait_for_wake_word()
            wake_count += 1
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"\n[wake #{wake_count} @ {ts}] listening...")
            speak("yes")

            audio = record_until_silence()
            t_eos = time.perf_counter()
            print(f"[recorded] {audio.size / 16000:.1f}s")

            text = transcribe(audio)
            t_stt = time.perf_counter()
            if not text:
                print("[stt] (empty)")
                speak("I didn't catch that")
                continue
            print(f"[you] {text}")

            messages.append({"role": "user", "content": text})
            mark = len(messages)
            try:
                reply = run_tool_loop(messages, registry)
            except RuntimeError as exc:
                print(f"[llm error] {exc}")
                speak("My language model isn't responding. Check that Ollama is running.")
                del messages[mark - 1 :]  # drop the unanswered turn
                continue
            t_agent = time.perf_counter()

            for m in messages[mark:]:
                if m.get("role") == "assistant" and m.get("tool_calls"):
                    for c in m["tool_calls"]:
                        print(f"[tool] -> {c['function']['name']}({c['function']['arguments']})")
                elif m.get("role") == "tool":
                    print(f"[tool] <- {m['tool_name']}: {m['content'][:120]!r}")

            _trim_history(messages)
            print(f"[jarvis] {reply}")

            speak(reply)
            t_done = time.perf_counter()

            stt_ms = (t_stt - t_eos) * 1000
            agent_ms = (t_agent - t_stt) * 1000  # LLM rounds + tool calls
            eos_to_audio_ms = (t_agent - t_eos) * 1000
            total_ms = (t_done - t_eos) * 1000
            print(
                f"[timing] stt={stt_ms:.0f}ms  agent={agent_ms:.0f}ms  "
                f"eos->audio={eos_to_audio_ms:.0f}ms  total={total_ms:.0f}ms"
            )
    finally:
        mcp.stop()


def _trim_history(messages: list[dict]) -> None:
    """Keep system + the last `max_history_turns` user turns and everything after.

    Trimming on user-turn boundaries (not raw message count) avoids orphaning
    a `tool` message from its assistant tool-call turn, which would break the
    format Ollama expects on the next round.
    """
    user_idxs = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    if len(user_idxs) <= settings.max_history_turns:
        return
    cut = user_idxs[-settings.max_history_turns]
    del messages[1:cut]
