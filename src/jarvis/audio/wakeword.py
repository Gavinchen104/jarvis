from functools import lru_cache

import numpy as np
import sounddevice as sd

from jarvis.config import settings


def ensure_wake_models() -> None:
    """Pre-download openwakeword's default models (idempotent)."""
    import openwakeword.utils as ow_utils

    print("Ensuring openwakeword models are downloaded...")
    ow_utils.download_models()


@lru_cache(maxsize=1)
def _get_model():
    """Build the wake-word model once and reuse it for the process lifetime.

    Previously a fresh Model() (and an `ensure_wake_models()` re-check) ran
    on *every* wake call from the chat loop — reloading the ONNX model and
    printing the "Ensuring…" line each turn. Caching removes that per-turn
    cost; state is cleared per activation via reset() below.
    """
    from openwakeword.model import Model

    ensure_wake_models()
    return Model(wakeword_models=[settings.wake_word], inference_framework="onnx")


def warm_wake_model() -> None:
    """Build the cached model ahead of the first wake (called at startup)."""
    _get_model()


def wait_for_wake_word() -> None:
    """Block until the configured wake word is detected from the default mic."""
    model = _get_model()
    # Clear buffered scores so the tail of the previous activation can't
    # immediately re-trigger when we reuse the cached model.
    if hasattr(model, "reset"):
        model.reset()

    with sd.InputStream(
        channels=1,
        samplerate=settings.sample_rate,
        dtype="int16",
        blocksize=settings.wake_chunk_size,
    ) as stream:
        while True:
            chunk, _ = stream.read(settings.wake_chunk_size)
            audio = np.ascontiguousarray(chunk[:, 0])
            scores = model.predict(audio)
            top = max(scores.values()) if scores else 0.0
            if top >= settings.wake_threshold:
                return
