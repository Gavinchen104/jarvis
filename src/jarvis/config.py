import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(
        default_factory=lambda: Path(_env("JARVIS_DATA_DIR", str(Path.home() / ".jarvis"))).expanduser()
    )

    sample_rate: int = 16000
    wake_chunk_size: int = 1280  # 80ms @ 16kHz; openwakeword's expected frame size

    wake_word: str = field(default_factory=lambda: _env("JARVIS_WAKE_WORD", "hey_jarvis_v0.1"))
    wake_threshold: float = 0.5

    whisper_model: str = field(default_factory=lambda: _env("JARVIS_WHISPER_MODEL", "small.en"))
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"

    # Energy-based end-of-speech detection (Phase 1; swap to silero VAD later if needed).
    record_chunk_ms: int = 50
    silence_duration_ms: int = 900
    silence_rms_threshold: float = 0.01
    max_record_seconds: int = 30

    ollama_model: str = field(
        default_factory=lambda: _env("JARVIS_OLLAMA_MODEL", "qwen2.5:7b-instruct-q4_K_M")
    )
    ollama_host: str = field(
        default_factory=lambda: _env("JARVIS_OLLAMA_HOST", "http://localhost:11434")
    )
    # Session-only conversation memory (Phase 2). Long-term memory is Phase 7.
    max_history_turns: int = 6


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
