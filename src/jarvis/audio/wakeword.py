import numpy as np
import sounddevice as sd

from jarvis.config import settings


def ensure_wake_models() -> None:
    """Pre-download openwakeword's default models (idempotent)."""
    import openwakeword.utils as ow_utils

    print("Ensuring openwakeword models are downloaded...")
    ow_utils.download_models()


def wait_for_wake_word() -> None:
    """Block until the configured wake word is detected from the default mic."""
    from openwakeword.model import Model

    ensure_wake_models()
    model = Model(wakeword_models=[settings.wake_word], inference_framework="onnx")

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
