import numpy as np
import sounddevice as sd

from jarvis.config import settings

# Calibrated against ambient noise at startup. None = use the static fallback,
# which is studio-quiet and breaks in any normal room (HVAC, fans, breathing
# all clear 0.01 RMS), causing recording to run to max_record_seconds instead
# of stopping at end-of-speech.
_calibrated_threshold: float | None = None


def calibrate_silence(duration_s: float = 1.5) -> float:
    """Sample the mic for `duration_s` and set the silence threshold above
    the room's noise floor. Returns the threshold so the caller can log it.
    """
    global _calibrated_threshold
    chunk_samples = int(settings.sample_rate * settings.record_chunk_ms / 1000)
    n_chunks = max(1, int(duration_s * 1000 / settings.record_chunk_ms))
    rms: list[float] = []
    with sd.InputStream(
        channels=1,
        samplerate=settings.sample_rate,
        dtype="float32",
        blocksize=chunk_samples,
    ) as stream:
        for _ in range(n_chunks):
            chunk, _ = stream.read(chunk_samples)
            rms.append(float(np.sqrt(np.mean(chunk[:, 0] ** 2))))
    rms.sort()
    p95 = rms[int(len(rms) * 0.95)]
    # 2.5x the noisy-end of ambient gives headroom while staying well below
    # normal speech (typically 0.1-0.3 RMS). The config default acts as a
    # floor for genuinely silent rooms.
    _calibrated_threshold = max(settings.silence_rms_threshold, p95 * 2.5)
    return _calibrated_threshold


def record_until_silence() -> np.ndarray:
    """Record mono 16kHz audio until sustained silence is detected.

    Returns a float32 numpy array suitable for faster-whisper.
    """
    threshold = (
        _calibrated_threshold
        if _calibrated_threshold is not None
        else settings.silence_rms_threshold
    )
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
            if rms < threshold:
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
