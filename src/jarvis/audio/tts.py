import subprocess


def speak(text: str) -> None:
    """Speak text via the macOS `say` command. Blocks until done."""
    if not text:
        return
    subprocess.run(["say", text], check=False)
