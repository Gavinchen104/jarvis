import argparse

from jarvis import __version__


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="jarvis",
        description="Local voice-first personal assistant",
    )
    parser.add_argument("--version", action="version", version=f"jarvis {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("echo", help="Phase 1 echo loop: wake → STT → say")
    subparsers.add_parser("run", help="Phase 2 chat loop: wake → STT → LLM → say")
    subparsers.add_parser(
        "setup", help="Pre-download wake-word and Whisper models"
    )

    args = parser.parse_args()

    if args.command == "echo":
        from jarvis.loops.echo import run_echo

        run_echo()
        return 0

    if args.command == "setup":
        from jarvis.audio.wakeword import ensure_wake_models
        from jarvis.audio.stt import get_model

        ensure_wake_models()
        print("Loading Whisper model (first run downloads ~500MB)...")
        get_model()
        print("Setup complete.")
        return 0

    if args.command == "run":
        from jarvis.loops.chat import run_chat

        run_chat()
        return 0

    parser.print_help()
    return 0
