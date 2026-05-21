# Phase 2 — Add the Brain (LLM In The Loop)

> Detailed engineering plan + partial retrospective. Phase 2 looked like
> a "just swap the echo step for a real LLM" change. In practice the
> interesting work was around the seams: cold-start cost, session
> history correctness, system-prompt shape for *spoken* output, and
> failure-when-the-daemon-is-down. Code complete + headless-validated
> 2026-05-18; awaiting the voice test.

---

## 1. Goal & Acceptance Test

**Goal:** swap the echo step for a real Qwen response. Still no tools.

**Headline acceptance test (from PLAN.md):**

> "Hey Jarvis, what's the capital of France" → spoken answer.

**Status:**
- ✅ Code complete
- ✅ Headless-validated 2026-05-18 (import + history-trim unit + live
  LLM round-trip)
- ⏳ Voice test pending

---

## 2. Why This Looked Easy

On the surface: replace `speak(text)` with `speak(llm(text))`. The Phase
1 echo loop is unchanged except for that line.

What made it less trivial:

1. **Cold start is 29 seconds.** Loading 4.7GB of Qwen weights through
   the Ollama daemon on first call is unavoidable. The user's *first*
   query after launch can't take 29s and feel okay.
2. **Session history must be coherent across tool turns.** The history
   trim has to respect message-format invariants — naively keeping
   "last N messages" breaks Ollama's tool-call/tool-result pairing
   (which only matters in Phase 3+, but the trim has to be right *now*
   or it breaks then).
3. **The system prompt is read aloud.** Markdown, bullets, and
   "Certainly! I'd be happy to help." filler all sound terrible
   through TTS. The prompt isn't styled for accuracy — it's styled for
   speech.
4. **Daemon-down has to fail gracefully.** `ollama serve` is a separate
   process; the assistant has to *say* what's wrong, not just stack-trace.

---

## 3. Key Design Decisions

### 3.1 Ollama, not raw `llama.cpp` or MLX

Locked in DESIGN.md §2.3. Ollama is the "boring middleware" — stable
HTTP API, model management built in, embeddings included (free for
Phase 7). One process to keep alive (`ollama serve`), one HTTP client.

### 3.2 Qwen 2.5 7B Instruct, Q4_K_M

Locked in DESIGN.md §2.2. Picked specifically for tool-use accuracy at
the 7B tier. Phase 2 doesn't *use* tools yet, but choosing the model
here lets the same weights serve Phase 3.

Memory budget: ~4.7GB resident with Q8 KV cache, leaves headroom for
Whisper (~500MB), the OS, and Phase 7 embeddings (~300MB) on a 16GB Mac.

### 3.3 RAM-friendly Ollama flags

`OLLAMA_FLASH_ATTENTION=1 OLLAMA_KV_CACHE_TYPE=q8_0`:

- `FLASH_ATTENTION=1` — speeds attention computation on long-ish
  contexts (the multi-turn loop benefits).
- `KV_CACHE_TYPE=q8_0` — quantizes the KV cache to int8. ~halves cache
  memory at very little perplexity cost. Lets the model coexist with
  Whisper + Phase 7's embedding model.

### 3.4 Session-only history (6 user turns)

`max_history_turns = 6` keeps recent context without bloating prompts.
Persistent memory is Phase 7. The trim has to be on **user-turn
boundaries** (not raw message count), because by Phase 3 each user
turn may expand into `assistant (tool_calls)` + `tool` + `assistant
(text)` — three messages that must travel together.

The Phase 2 trim is implemented anyway (see `loops/chat.py:_trim_history`),
so Phase 3 inherited a correct trim instead of bolting one on.

### 3.5 Voice-tuned system prompt

The prompt is short, plain prose, and rules about *output shape*:

- One or two sentences unless asked for more.
- No markdown, no bullets, no emoji — these get read aloud literally.
- Low-affect: no "Great question!" filler.
- Ask one clarifying question on ambiguous input, don't guess.
- Acknowledge garbled STT briefly instead of answering the wrong question.

The last bullet is non-obvious: Whisper is wrong sometimes, and the
*correct* behavior when the input is garbled is to say so, not to
synthesize plausible nonsense.

---

## 4. File-by-File Build (As Shipped)

```
src/jarvis/
├── agent/
│   ├── __init__.py
│   ├── llm.py          # Thin Ollama chat wrapper (Phase 2)
│   └── prompt.py       # The voice-tuned system prompt
├── loops/
│   └── chat.py         # wake -> STT -> LLM -> TTS, with [timing]
└── cli.py              # `jarvis run` subcommand
```

### 4.1 `agent/llm.py`

The shape that Phase 3 builds on top of:

```python
def chat(messages: list[dict], tools: list[dict] | None = None) -> dict[str, Any]:
    """Returns {"content": str, "tool_calls": [...]}."""
```

Phase 2 only uses the `content` path. The `tools=` parameter is
present from day one — Phase 3 just starts passing schemas. Designing
the function signature *once*, with the optional argument in place,
meant Phase 3's LLM-side change was zero. (The tool-use *loop* was new
work, but the LLM client wasn't.)

The error handling is also Phase-2-shaped: a daemon-down `Exception`
gets caught and re-raised as `RuntimeError` with a usable hint
(`"Is ``ollama serve`` running and ``qwen2.5:7b-instruct-q4_K_M``
pulled? Try: ollama list"`). The chat loop catches the `RuntimeError`
and speaks a friendly message — no stack trace to the user.

### 4.2 `agent/prompt.py`

Single string constant `SYSTEM_PROMPT`. Two halves:

- **Output shape rules** — brevity, plain prose, low-affect (read aloud).
- **Failure-mode rules** — ask on ambiguous input; acknowledge garbled STT.

Phase 3 added a `Tools:` section to this same string. Phase 5 will
replace the static constant with `build_system_prompt()` to inject the
time anchor per turn.

### 4.3 `loops/chat.py`

The Phase 2 loop is recognizably a sibling of Phase 1's echo loop:

```
wait_for_wake_word()
record_until_silence()
text = transcribe(audio)
messages.append({"role": "user", "content": text})
reply = chat(messages)
messages.append({"role": "assistant", "content": reply})
_trim_history(messages)
speak(reply)
```

Phase 3 added `run_tool_loop(messages, registry)` in place of
`chat(messages)`. Same caller-side shape — the loop is a strict
extension, not a rewrite.

---

## 5. Cold Start: The Real Surprise

Headless measurement from 2026-05-18:

| Call | Latency |
|---|---|
| First `chat()` after `ollama serve` boot | **~29s** |
| Warm follow-ups, short answers | 0.6–1.1s |
| Warm follow-ups, longer (paragraph) | 1.5–2.5s |

The 29s is **loading 4.7GB of weights into RAM**, not generation. It
happens *once* per Ollama daemon lifetime, but it's the user's first
interaction.

Two mitigations on the table, neither yet shipped:

1. **Startup warmup call.** `agent/llm.py` does a 1-token `chat()` at
   process start. Hides the cold start behind the
   "Calibrating mic / Warming up Whisper / Loading model" startup
   phase. **Recommended.**
2. **Keep-alive the model in Ollama.** `OLLAMA_KEEP_ALIVE=24h` env
   var. Avoids the cold start across multiple `jarvis run` invocations.

Either alone is enough; together is belt + suspenders. Tracked as Phase
2.5 (post-voice-test).

---

## 6. Failure Modes & Mitigations

| Failure | Likelihood | Mitigation | Where |
|---|---|---|---|
| `ollama serve` not running | Real (forget to start daemon) | `chat()` raises `RuntimeError` with actionable hint; chat loop catches and speaks "My language model isn't responding. Check that Ollama is running." | §4.1, loops/chat.py |
| `qwen2.5:7b-instruct-q4_K_M` not pulled | Real on a new machine | Same error path; the hint mentions `ollama list` | §4.1 |
| First turn takes 29s (cold start) | Certain | Warmup + KEEP_ALIVE (§5) — deferred but documented | §5 |
| Model emits markdown despite prompt | Real | Phase 1's `clean_for_speech` strips it at the TTS boundary | tts.py |
| Model writes "Sure! Great question..." filler | Real | Prompt rule (§3.5); model mostly complies but slips on long answers | prompt.py |
| Session history grows unbounded | Real | `_trim_history` cuts to `max_history_turns` user turns, on boundaries | loops/chat.py |
| Trim orphans a `tool` message from its assistant turn | Will be real in Phase 3 | Trim on user-turn boundaries, not raw message count | loops/chat.py |
| Daemon dies mid-turn | Rare | `RuntimeError` path; user sees the friendly message and retries | §4.1 |
| OOM (model eviction by OS) | Rare on 16GB | KV cache quantized (q8_0); Whisper int8; embeddings deferred to Phase 7 with separate budget | config.py |
| Model hallucinates a confident wrong answer ("San Francisco is 72°F") | **High** for current-data questions | This is the explicit motivation for Phase 3 (tools). Phase 2 ships with the failure mode; Phase 3 closes it. | PHASE3.md §1 |

The last row is the most important: Phase 2 is the *demo* that motivates
Phase 3. The "what's the weather" failure here is the before/after slide
for the rest of the project.

---

## 7. Observability

A Phase 2 turn:

```
[wake #3 @ 14:32:08] listening...
[recorded] 2.1s
[stt] what's the capital of france
[jarvis] Paris.
[timing] stt=400ms agent=820ms eos->audio=1220ms total=2510ms
```

| Key | Healthy range |
|---|---|
| `stt` | 300–500ms warm |
| `agent` | 600–1200ms warm; ~29000ms cold (first turn) |
| `eos->audio` | 1.0–1.7s warm (the headline UX number) |
| `total` | varies with answer length / TTS time |

The Phase 1 → Phase 2 latency delta is the cost of intelligence. ~1.2s
warm is comfortably under the 2.5s budget DESIGN.md §10.2 set.

---

## 8. Voice Test (Pending)

Procedure:

1. `OLLAMA_FLASH_ATTENTION=1 OLLAMA_KV_CACHE_TYPE=q8_0 ollama serve` (in a separate terminal or as a service).
2. Confirm `ollama list` shows `qwen2.5:7b-instruct-q4_K_M`.
3. `uv run jarvis run`. Wait for "Ready ({model}). Say 'Hey Jarvis'..."
4. Test phrases:
   - "Hey Jarvis, what's the capital of France." → expect "Paris."
   - "Hey Jarvis, what's seven times eight." → expect "Fifty-six."
   - "Hey Jarvis, summarize what you just said." → expect short recap (history works).
   - "Hey Jarvis, what's the weather in San Francisco." → expect a *plausible-sounding wrong answer* (the Phase 3 motivator).
5. Record `[timing]` numbers from the first 5 turns. Note cold-start.
6. Leave it idle 30 min; count false wakes.

What "pass" means: spoken answers arrive, sound natural, history works
across 2–3 turns, daemon-down message is friendly, false-wake rate
matches Phase 1's.

---

## 9. Scope Discipline — What Phase 2 Did NOT Do

- **No tools.** Phase 3.
- **No streaming.** TTS waits for the full LLM response. Streaming
  would shave perceived latency but adds real engineering (Phase 8+).
- **No persistent memory.** Session-only history; restart wipes
  context. Phase 7.
- **No fallback model.** If Qwen is down, there's no Llama-3 backup —
  the assistant fails loudly. Hybrid mode (Claude API for the agent
  loop) is the escape hatch in DESIGN.md §2.1 if Phase 3 reveals the
  local model isn't reliable enough.
- **No prompt-conditioning on user identity.** Single-user; the prompt
  hardcodes "Gavin." Multi-user is out of v1 (PLAN.md §8).

---

## 10. Risks Carried Forward

These are Phase 2 limitations that Phase 3 (or later) must address:

| Risk | First addressed in |
|---|---|
| Hallucinated current-data answers | Phase 3 (web search) |
| 29s cold start | Phase 2.5 warmup + KEEP_ALIVE |
| No memory beyond session | Phase 7 |
| No fallback if Qwen disappoints on tool use | Hybrid mode (DESIGN.md §2.1, PHASE3.md §13) |
| Long answers strain TTS speakability | `clean_for_speech` partial fix; structural fix needs streaming |

---

## 11. Definition of Done

Closed for code; gated on voice test for green.

- [x] `agent/llm.py` — Ollama wrapper with daemon-down hint
- [x] `agent/prompt.py` — voice-tuned system prompt
- [x] `loops/chat.py` — wake → STT → LLM → TTS, with `[timing]` and history trim
- [x] `jarvis run` CLI subcommand wired
- [x] Headless validation: import + history-trim unit + live LLM round-trip (2026-05-18)
- [x] Cold/warm latency measured (~29s / ~0.6–1.1s)
- [ ] Voice test: "Hey Jarvis, what's the capital of France" → spoken answer
- [ ] PLAN.md Phase 2 marked done with measured `eos->audio` p50 from the voice test
