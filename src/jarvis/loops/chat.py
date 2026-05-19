import time
from datetime import datetime

from jarvis.agent.llm import chat
from jarvis.agent.prompt import SYSTEM_PROMPT
from jarvis.audio.recorder import record_until_silence
from jarvis.audio.stt import get_model, transcribe
from jarvis.audio.tts import speak
from jarvis.audio.wakeword import wait_for_wake_word
from jarvis.config import settings


def run_chat() -> None:
    """Phase 2 loop: wake -> record -> Whisper -> Qwen -> macOS say.

    Session-only conversation history (no persistence yet; that's Phase 7).
    Prints a [timing] line each turn so end-to-end latency with the LLM in
    the loop is measured, not guessed.
    """
    print("Warming up Whisper...")
    get_model()
    print(f"Ready ({settings.ollama_model}). Say 'Hey Jarvis' to wake. Ctrl-C to quit.")

    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    wake_count = 0

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
        try:
            reply = chat(messages)
        except RuntimeError as exc:
            print(f"[llm error] {exc}")
            speak("My language model isn't responding. Check that Ollama is running.")
            messages.pop()  # don't keep the unanswered turn in history
            continue
        t_llm = time.perf_counter()

        messages.append({"role": "assistant", "content": reply})
        _trim_history(messages)
        print(f"[jarvis] {reply}")

        speak(reply)
        t_done = time.perf_counter()

        stt_ms = (t_stt - t_eos) * 1000
        llm_ms = (t_llm - t_stt) * 1000
        eos_to_audio_ms = (t_llm - t_eos) * 1000  # end-of-speech -> first audio
        total_ms = (t_done - t_eos) * 1000
        print(
            f"[timing] stt={stt_ms:.0f}ms  llm={llm_ms:.0f}ms  "
            f"eos->audio={eos_to_audio_ms:.0f}ms  total={total_ms:.0f}ms"
        )


def _trim_history(messages: list[dict]) -> None:
    """Keep the system prompt + the last `max_history_turns` user/assistant pairs."""
    keep = settings.max_history_turns * 2
    if len(messages) > keep + 1:
        del messages[1 : len(messages) - keep]
