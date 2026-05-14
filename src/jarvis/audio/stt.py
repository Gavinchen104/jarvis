from __future__ import annotations

import numpy as np

from jarvis.config import settings

_model = None


def get_model():
    """Lazy-load the Whisper model; first call downloads weights (~500MB for small.en)."""
    global _model
    if _model is None:
        from faster_whisper import WhisperModel

        _model = WhisperModel(
            settings.whisper_model,
            device=settings.whisper_device,
            compute_type=settings.whisper_compute_type,
        )
    return _model


def transcribe(audio: np.ndarray) -> str:
    """Transcribe a 16kHz mono float32 audio buffer to text."""
    if audio.size == 0:
        return ""
    model = get_model()
    segments, _ = model.transcribe(audio, language="en", beam_size=1, vad_filter=True)
    return " ".join(seg.text.strip() for seg in segments).strip()
