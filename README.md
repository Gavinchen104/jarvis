# jarvis

Local voice-first personal assistant. Runs entirely on-device: wake-word
detection, speech-to-text, LLM, and text-to-speech all stay on the Mac.

## Stack

| Layer | Choice |
|---|---|
| Wake word | [openwakeword](https://github.com/dscripka/openWakeWord) — `hey_jarvis_v0.1` |
| STT | [faster-whisper](https://github.com/SYSTRAN/faster-whisper) `small.en` |
| LLM | [Ollama](https://ollama.com) running `qwen2.5:7b-instruct-q4_K_M` |
| TTS | macOS `say` (will upgrade to Piper later) |
| Memory | SQLite + sqlite-vec (Phase 7) |
| Tools | MCP servers (Phase 3+) |

## Setup

Requires Apple Silicon, Python 3.12+, [uv](https://docs.astral.sh/uv/), and Ollama.

```bash
brew install ollama
ollama serve &
ollama pull qwen2.5:7b-instruct-q4_K_M

uv sync
uv run jarvis setup      # pre-download wake-word + Whisper models
uv run jarvis echo       # Phase 1: voice echo loop
```

## Roadmap

- **Phase 0** — scaffolding, Ollama, `jarvis --version`
- **Phase 1** — wake word -> STT -> TTS echo loop *(current)*
- **Phase 2** — add Qwen 2.5 in the loop (conversational)
- **Phase 3** — first MCP tool: web search
- **Phase 4** — macOS control + voice-confirmation flow for destructive actions
- **Phase 5** — Calendar + reminders
- **Phase 6** — Gmail
- **Phase 7** — long-term memory (SQLite + sqlite-vec, fact extraction)
