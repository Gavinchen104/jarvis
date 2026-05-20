import re
import subprocess

# The system prompt asks the model for plain spoken prose, but a local 7B
# still leaks markdown on long tool-synthesis turns (observed in the Phase 3
# voice test). The TTS layer owns the "must be speakable" invariant and
# enforces it in code so `say` never reads "asterisk asterisk" aloud,
# regardless of what the model emits. Same philosophy as the risk gate:
# don't trust the model, enforce the property at the boundary.

_CODE_FENCE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`]*)`")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")  # [text](url) -> text
_URL = re.compile(r"https?://\S+")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_BULLET = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+", re.MULTILINE)
_EMPHASIS = re.compile(r"(\*{1,3}|_{1,3})(\S.*?\S|\S)\1")
_LEFTOVER_MARKS = re.compile(r"[*_#`>]")
_WS = re.compile(r"[ \t]{2,}")
_NEWLINES = re.compile(r"\s*\n\s*")


def clean_for_speech(text: str) -> str:
    """Strip markdown / URLs so `say` reads natural prose, not punctuation."""
    if not text:
        return ""
    text = _CODE_FENCE.sub(" ", text)
    text = _INLINE_CODE.sub(r"\1", text)
    text = _MD_LINK.sub(r"\1", text)
    text = _URL.sub("", text)
    text = _HEADING.sub("", text)
    text = _BULLET.sub("", text)
    text = _EMPHASIS.sub(r"\2", text)
    text = _LEFTOVER_MARKS.sub("", text)
    # Bullets/headings became separate lines; join into flowing sentences.
    text = _NEWLINES.sub(". ", text)
    text = re.sub(r"\.\s*\.\s*", ". ", text)  # collapse ".. " from the join
    text = _WS.sub(" ", text)
    return text.strip()


def speak(text: str) -> None:
    """Speak text via the macOS `say` command. Blocks until done."""
    text = clean_for_speech(text)
    if not text:
        return
    subprocess.run(["say", text], check=False)
