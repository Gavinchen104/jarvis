# Phase 1 — Hello-Voice Echo Loop (Retrospective)

> Detailed engineering plan + retrospective. Phase 1 was the **highest-risk**
> phase by deliberate design (DESIGN.md §8) — the first time real audio
> touches real hardware, with all four audio components in line. This doc
> records what was built, what surprised, what was tuned, and what got
> deferred. Closed 2026-05-15.

---

## 1. Goal & Acceptance Test (Closed)

**Goal:** end-to-end voice pipeline with **no intelligence** — JARVIS just
repeats what you said.

**Headline acceptance test:**

> "Hey Jarvis, testing one two three" → JARVIS says back "testing one two
> three."

**Status:** ✅ Passed 2026-05-15 on the target Mac.

---

## 2. Why This Was The Risk Phase

The interesting failure modes in v1 weren't ML — they were **OS plumbing**:

- macOS microphone permissions (CoreAudio + TCC).
- The Apple-Silicon Python audio stack (sounddevice → PortAudio → CoreAudio).
- ONNX runtime for openWakeWord (one of the few hardware-sensitive deps).
- faster-whisper / CTranslate2 picking the right CPU backend.

If any of these had been broken, every later phase would be blocked on a
problem that has nothing to do with AI. Front-loading them is the whole
point of DESIGN.md §8's "risk first" ordering — discover the worst part
when scope changes are cheap.

What was **not** in scope: an LLM (Phase 2), tools (Phase 3+), or any
decision-making at all. Echo loop = pure plumbing test.

---

## 3. The Four Components

### 3.1 Wake word — `audio/wakeword.py`

**Engine:** [openWakeWord](https://github.com/dscripka/openWakeWord) with
the shipped `hey_jarvis_v0.1` ONNX model.

Key implementation choices (visible in the file):

- **Model cached for process lifetime** (`@lru_cache(maxsize=1)`).
  Re-instantiating per wake reloaded ONNX every turn — slow + leaked
  the "Ensuring openwakeword models are downloaded…" print every wake.
  Caching kills both.
- **`reset()` between activations.** The model holds an internal score
  buffer; without `reset()`, the *tail* of the previous activation
  re-triggers the wake immediately when the loop comes back around.
  This is the "wakes twice in a row" bug — fixed by the `reset()` call.
- **80ms chunks at 16kHz** (`wake_chunk_size=1280`). That's openWakeWord's
  native frame size; anything else either drops samples or buffers
  awkwardly.
- **No threading.** The wake listener blocks the loop. Phase 1 is
  single-threaded by choice — debuggable trumps "responsive while
  waiting" for a tool we'll only iterate at startup.

### 3.2 Recorder — `audio/recorder.py`

**Algorithm:** RMS energy threshold + sustained-silence-window, with
**runtime calibration**.

The piece that *almost* didn't ship correctly was the threshold. The
static default (`silence_rms_threshold=0.01`) is studio-quiet — any
normal room (HVAC, laptop fan, breathing) clears it on every chunk, so
recording **never stops** until `max_record_seconds=30`. That's
indistinguishable from "the recorder is broken."

The fix: `calibrate_silence()` runs for 1.5s at startup, takes the 95th
percentile of ambient RMS, multiplies by **2.5** for headroom, and uses
that as the floor (`max(static_floor, 2.5 * p95)`). Normal speech is
0.1–0.3 RMS — well above the calibrated threshold; ambient noise is
below it.

Other choices visible in the code:

- **`heard_voice` latch.** Recording only ends if silence is detected
  *after* speech started. Without it, the recorder would return a
  zero-length buffer immediately if the user paused before speaking.
- **50ms chunks for recording** (vs 80ms for wake). Different units
  because the wake model wants 80ms exactly; recording cares more about
  silence-detection granularity, where 50ms gives finer end-of-speech
  timing.

### 3.3 STT — `audio/stt.py`

**Model:** `faster-whisper small.en`, CPU, int8 compute type.

- **Lazy-loaded.** First `transcribe()` call downloads ~500MB; subsequent
  calls are warm. The module-level `_model = None` + `get_model()`
  pattern is intentional: the wake/recorder modules import cleanly
  without paying STT's cost.
- **Beam size = 1.** Greedy decoding is fast enough; beam search adds
  latency for ~no quality on conversational speech.
- **`vad_filter=True`.** Whisper's built-in VAD strips silences inside
  the buffer. Without it, the model occasionally hallucinates words in
  trailing silence (the classic "thank you" / "thanks for watching"
  failure mode from YouTube training data).
- **English-only (`language="en"`).** Faster than auto-detect, and
  `small.en` is meaningfully better than `small` multilingual at the
  same size.

### 3.4 TTS — `audio/tts.py`

**Engine:** macOS `say` subprocess.

Two non-obvious pieces:

- **`clean_for_speech()` markdown stripper.** Even with a "no markdown"
  system prompt (Phase 2), a local 7B leaks asterisks, bullet points,
  and URLs on long synthesis turns. The TTS layer **enforces the
  speakable-text invariant** in code, the same way the tool loop
  enforces the risk gate — don't trust the model; enforce the property
  at the boundary. The regex set was tuned against real Phase 3
  outputs.
- **`cue_heard()` for perceptual responsiveness.** A `/System/Library/Sounds/Pop.aiff`
  plays via `afplay` the instant end-of-speech fires. STT + LLM still
  take 5–15s, but the user *knows* they were heard. Without it, the
  loop feels broken even when it's working.

### 3.5 Loop — `loops/echo.py`

Glue. Orders the four steps, emits the `[timing]` line, numbers wakes
for false-wake counting. Single thread. ~50 lines.

---

## 4. The Tuning Journey

What was tuned after first hardware contact (config.py defaults reflect
the post-tune values):

| Knob | Default | First-pass value | Reason for tune |
|---|---|---|---|
| `wake_threshold` | 0.5 | 0.5 (kept) | False-wake rate inside acceptable range with stock value |
| `silence_rms_threshold` | 0.01 | 0.02 (raised) | Static default too sensitive; calibration overrides it but the floor still mattered for genuinely silent rooms |
| `silence_duration_ms` | 900 | 700 (cut) | 900ms felt sluggish in turn-taking; 700 still survived natural pauses |
| `record_chunk_ms` | 50 | 50 (kept) | Trade-off between latency granularity and CPU |
| `whisper_compute_type` | int8 | int8 (kept) | float16 was marginally more accurate but added latency on M-series CPU |

The takeaway: **defaults that look reasonable in code can be wrong in
the room.** The fix wasn't tuning until perfect — it was adding
*runtime calibration* (`calibrate_silence`) so the system tunes itself
to wherever it's used.

---

## 5. Measurement Methodology

The Phase 1 deliverable wasn't "it works once" — it was the `[timing]`
line and the procedure for reading it.

Per-turn output:

```
[wake #4 @ 14:32:08] listening...
[recorded] 2.3s
[stt] testing one two three
[timing] stt=410ms eos->audio=410ms recspeak_total=2730ms
```

| Metric | How to read it |
|---|---|
| **Wake count + timestamp** | `[wake #N @ HH:MM:SS]`. Idle-time false-wake rate is computed from this stream; leave the loop idle for 30–60min and count the wakes you didn't trigger. |
| **`stt`** | End-of-speech → text. Whisper warm latency on a 2-3s utterance. |
| **`eos->audio`** | The headline Phase 1 latency. In echo it's the same as `stt` because there's no LLM. |
| **`recspeak_total`** | Start of recording through end of `say`. Useful for catching a regression where TTS itself slows down. |
| **STT accuracy** | Eyeball `[stt]` vs what you said. Note miss rate over ~10 utterances and any consistent misses (names, jargon). |

A useful diagnostic: run `jarvis echo`, do 5 clean test phrases for
latency/STT, then leave it idle 30–60min for the false-wake count.
Read the numbers off the terminal scroll.

---

## 6. What Surprised

| Surprise | What we learned |
|---|---|
| Recorder ran for 30s every time on the first hardware run | The static `silence_rms_threshold` was studio-quiet. Lesson: hardware-sensitive thresholds need *runtime calibration*, not careful defaults. |
| Wake fired twice in a row | openWakeWord retains internal scores between calls — `reset()` between activations is required. Discoverable only by reading the model's source. |
| First wake took ~10s to react | ONNX model warm-up on first `predict()`. Fixed by calling `warm_wake_model()` at startup. |
| `say` reading "asterisk asterisk" aloud | Not actually Phase 1's problem — the bug only appeared once Phase 2 added an LLM. But the *fix* (`clean_for_speech`) belongs in the TTS layer, so it lives in Phase 1's file. |
| ONNX install was the only deps fight | `tflite-runtime` doesn't have a Darwin wheel; `pyproject.toml` had to be scoped to the Darwin resolver. Worth ~½ day. |

---

## 7. Failure Modes & Mitigations

| Failure | What happened | Mitigation |
|---|---|---|
| Mic permission denied silently | sounddevice's `InputStream` returns zeros, no exception | The first `jarvis echo` run triggers the macOS permission prompt; document in PLAN.md §11 |
| Recording never ends | Static threshold below ambient noise floor | Runtime `calibrate_silence` (§3.2) |
| Whisper hallucinates trailing "Thank you" | YouTube training-data quirk on silence | `vad_filter=True` strips silences before decode |
| Loop *feels* broken (dead silence after speaking) | STT+TTS latency, no acknowledgment | `cue_heard()` audio cue at end-of-speech |
| Wake re-fires immediately | openWakeWord internal state retention | `model.reset()` between activations |
| First wake takes 10s | ONNX cold-start | `warm_wake_model()` at startup |
| Whisper accuracy poor on jargon ("Gavin" → "Galvin") | `small.en` is genuinely weaker on names | Documented; upgrade path is `medium.en` (1.5GB, ~2× slower) |
| `tflite-runtime` install fails | No Darwin wheel | Scope the `uv` resolver to Darwin in `pyproject.toml` |

---

## 8. Observability

What a Phase 1 turn looks like in the log (already covered in §5;
repeated here as the contract):

```
[wake #N @ HH:MM:SS] listening...
[recorded] D.Ds
[stt] <transcribed text or "(empty)">
[timing] stt=___ms eos->audio=___ms recspeak_total=___ms
```

No transcripts persisted to disk by Phase 1; the terminal scroll is the
log. Phase 7's memory layer is the first time transcripts get stored.

---

## 9. Scope Discipline — What Phase 1 Did NOT Do

- **No LLM.** Echo only. Adding intelligence would have hidden which
  problems were audio and which were model.
- **No interruption / barge-in.** Out of v1 scope (PLAN.md §8).
- **No streaming TTS.** `say` blocks until done; that's fine for short
  echo strings.
- **No noise suppression.** RMS thresholding plus runtime calibration
  was good enough; deeper denoising is Phase 1.5+ via Silero VAD.
- **No multi-microphone selection.** Default input device only.

---

## 10. Deferred to Phase 1.5+

Two upgrade paths kept warm but not built:

| Component | Default | Upgrade trigger | Effort |
|---|---|---|---|
| TTS | macOS `say` | Voice gets unbearable | ~1hr swap to Piper (open-source neural TTS, same interface) |
| VAD | RMS energy | Noisy environment cuts off mid-sentence | Swap `recorder.py` to Silero VAD (already bundled by openWakeWord). Same `record_until_silence() -> np.ndarray` interface — the rest of the loop is unchanged. |

DESIGN.md §11 calls this out as "replaceable components" — the
interfaces are designed for swap, not the implementations for permanence.

---

## 11. What I'd Do Differently

- **Calibration earlier.** Spent a frustrating ~30min debugging
  recording-never-ends before realizing the threshold was the problem.
  If `calibrate_silence` had been in the first commit, the bug
  wouldn't have existed.
- **`[timing]` line from turn one.** The procedure for "how to read
  the numbers" was bolted on after the first user test, when it should
  have been part of the contract from the start. Phase 3 didn't repeat
  this mistake.
- **`clean_for_speech` written in Phase 1, not Phase 2.** The TTS-must-be-speakable
  invariant exists regardless of who's producing the text. Placing the
  guard at the boundary in Phase 1 would have meant Phase 2 inherited
  it for free.

---

## 12. Definition of Done (Closed)

- [x] All four components landed and importable
- [x] `uv run jarvis echo` runs end-to-end on hardware
- [x] `[timing]` line emits per turn
- [x] False-wake count tractable (<1/day in normal use)
- [x] STT accuracy acceptable on conversational speech
- [x] Mic permission flow works on first run
- [x] PLAN.md Phase 1 marked done 2026-05-15 with the measured numbers

The phase ended not when echo worked once, but when the **measurement
procedure** was documented and the knobs were findable in config. That
made Phase 2's swap (`speak(text)` → `speak(llm(text))`) a one-line
change.
