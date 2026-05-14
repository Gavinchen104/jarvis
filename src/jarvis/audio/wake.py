from __future__ import annotations

import logging

import numpy as np

from jarvis.config import WAKE_THRESHOLD

log = logging.getLogger(__name__)


class WakeDetector:
    """Wraps openWakeWord's prebuilt 'hey_jarvis' model."""

    def __init__(self) -> None:
        try:
            from openwakeword.utils import download_models
            download_models(["hey_jarvis"])
        except Exception as exc:
            log.debug("download_models skipped: %s", exc)

        from openwakeword.model import Model

        self.model = Model(
            wakeword_models=["hey_jarvis"],
            inference_framework="onnx",
        )
        log.info("Wake-word ready (hey_jarvis, threshold=%.2f)", WAKE_THRESHOLD)

    def detect(self, frame_int16: np.ndarray) -> bool:
        scores = self.model.predict(frame_int16)
        return any(score >= WAKE_THRESHOLD for score in scores.values())

    def reset(self) -> None:
        self.model.reset()
