import time
from datetime import datetime

from jarvis.audio.recorder import record_until_silence
from jarvis.audio.stt import get_model, transcribe
from jarvis.audio.tts import speak
from jarvis.audio.wakeword import wait_for_wake_word


def run_echo() -> None:
    """Phase 1 loop: wake word -> record -> Whisper -> macOS say. No LLM.

    Prints a [timing] line each turn so Phase 1 latency can be recorded
    without guessing. Wakes are numbered + timestamped so false wakes
    are countable: leave it running, count [wake #N] lines you didn't
    trigger.
    """
    print("Warming up Whisper...")
    get_model()
    print("Ready. Say 'Hey Jarvis' to wake. Ctrl-C to quit.")

    wake_count = 0
    while True:
        wait_for_wake_word()
        wake_count += 1
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"\n[wake #{wake_count} @ {ts}] listening...")
        speak("yes")

        t_record_start = time.perf_counter()
        audio = record_until_silence()
        t_eos = time.perf_counter()  # end-of-speech: recording returned
        dur = audio.size / 16000
        print(f"[recorded] {dur:.1f}s")

        text = transcribe(audio)
        t_stt = time.perf_counter()
        if not text:
            print("[stt] (empty)")
            speak("I didn't catch that")
            continue
        print(f"[stt] {text}")

        speak(text)
        t_done = time.perf_counter()

        stt_ms = (t_stt - t_eos) * 1000
        # end-of-speech -> first audio out (Phase 1's headline latency number)
        eos_to_audio_ms = stt_ms
        total_ms = (t_done - t_record_start) * 1000
        print(
            f"[timing] stt={stt_ms:.0f}ms  "
            f"eos->audio={eos_to_audio_ms:.0f}ms  "
            f"recspeak_total={total_ms:.0f}ms"
        )
