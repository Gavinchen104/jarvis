import numpy as np
import sounddevice as sd

from jarvis.config import settings


def record_until_silence() -> np.ndarray:
    """Record mono 16kHz audio until sustained silence is detected.

    Returns a float32 numpy array suitable for faster-whisper.
    """
    chunk_samples = int(settings.sample_rate * settings.record_chunk_ms / 1000)
    silence_chunks_needed = settings.silence_duration_ms // settings.record_chunk_ms
    max_chunks = settings.max_record_seconds * 1000 // settings.record_chunk_ms

    chunks: list[np.ndarray] = []
    silent_run = 0
    heard_voice = False

    with sd.InputStream(
        channels=1,
        samplerate=settings.sample_rate,
        dtype="float32",
        blocksize=chunk_samples,
    ) as stream:
        for _ in range(max_chunks):
            chunk, _ = stream.read(chunk_samples)
            audio = chunk[:, 0]
            chunks.append(audio.copy())

            rms = float(np.sqrt(np.mean(audio**2)))
            if rms < settings.silence_rms_threshold:
                silent_run += 1
                # Only stop if we've heard at least some voice first.
                if heard_voice and silent_run >= silence_chunks_needed:
                    break
            else:
                heard_voice = True
                silent_run = 0

    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks)
