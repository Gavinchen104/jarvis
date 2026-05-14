from jarvis.audio.recorder import record_until_silence
from jarvis.audio.stt import get_model, transcribe
from jarvis.audio.tts import speak
from jarvis.audio.wakeword import wait_for_wake_word


def run_echo() -> None:
    """Phase 1 loop: wake word -> record -> Whisper -> macOS say. No LLM."""
    print("Warming up Whisper...")
    get_model()
    print("Ready. Say 'Hey Jarvis' to wake. Ctrl-C to quit.")

    while True:
        wait_for_wake_word()
        print("\n[wake] listening...")
        speak("yes")
        audio = record_until_silence()
        print(f"[recorded] {audio.size / 16000:.1f}s")
        text = transcribe(audio)
        if not text:
            print("[stt] (empty)")
            speak("I didn't catch that")
            continue
        print(f"[stt] {text}")
        speak(text)
