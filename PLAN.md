# JARVIS — Build Plan

Local voice-first personal assistant. Everything (wake word, STT, LLM, TTS, memory)
runs on-device. Cloud is opt-in only and currently scoped out.

---

## 1. Vision

A daily-use, voice-activated personal assistant for a single user (Gavin) running on
an Apple Silicon Mac. It should feel as immediate and natural as Siri, but be
private, hackable, and extensible — anything Gavin asks of it should be expressible
as a tool call against a local MCP server.

**Non-goals for v1:** multi-user support, mobile/phone access, remote-API
fallback, real-time interruption ("barge-in") of TTS, persistent voice biometrics.

---

## 2. Locked Decisions

| Decision | Choice | Rationale |
|---|---|---|
| LLM backend | **Local via Ollama** | Private posture; no prompts leave the machine |
| LLM model | **Qwen 2.5 7B Instruct (Q4_K_M)** | Best tool-use among local 7B models; ~5GB fits comfortably in 16GB RAM |
| Hardware target | Apple Silicon Mac, 16GB RAM | Sweet spot for 7-8B models with Metal acceleration |
| Primary interface | **Voice + wake word** ("Hey Jarvis") | The JARVIS feel; hands-free |
| Wake word engine | **openwakeword** (`hey_jarvis_v0.1`) | Free, open, ships a "Hey Jarvis" model out of the box |
| STT | **faster-whisper** `small.en` | Pip-installable, fast on Apple Silicon, English-only is plenty |
| TTS | **macOS `say`** (v1) → Piper later | Zero-setup MVP; swap to Piper when robot voice gets annoying |
| Orchestrator | Python 3.12, single long-running process | Best ecosystem for audio + ML glue |
| Memory | **SQLite + sqlite-vec**, local only | Tiny (~10-100MB/year); kept off cloud for privacy |
| Embeddings | `nomic-embed-text` via Ollama (274MB) | Local, integrates with existing Ollama runtime |
| Tool extensibility | **MCP servers** (not hand-rolled) | Inherits existing Gmail/Calendar/filesystem/search ecosystem |
| Agent architecture | **Single tool-use loop** | Multi-agent only if evidence warrants; resist premature complexity |
| Proactivity | **Reactive only** for v1 | Predictable; no scheduler to maintain |
| Safety posture | **Always confirm destructive actions by voice** | Tools declare `risk: read \| write \| destructive`; ≥ write requires yes/no |
| Source storage | Local git + private GitHub backup | Code is fine on GitHub; operational data is not |
| Operational data | Local only (`~/.jarvis/`) | Memory, OAuth tokens, logs — never sync to iCloud/Dropbox |

---

## 3. Architecture

### 3.1 Component diagram (text)

```
                ┌─────────────────────────────────────────────────────┐
                │                Long-running Python process          │
                │                                                     │
  mic ──► [Wake word] ──► [Recorder] ──► [Whisper STT] ──► [Agent loop]
                │              │              │                │      │
                │              │              │                │      │
                │           silence VAD    text in           tool use │
                │                                              │      │
                │                                  ┌───────────┴───┐  │
                │                                  ▼               ▼  │
                │                              [Ollama]      [MCP tools]
                │                              Qwen 2.5      Gmail/Cal/
                │                              7B            FS/search/
                │                                            AppleScript
                │                                  │               │  │
                │                                  └───────┬───────┘  │
                │                                          ▼          │
                │                              [Memory: SQLite +      │
                │                               sqlite-vec]           │
                │                                          │          │
                │                                          ▼          │
                │                                  [TTS: `say`] ──► speaker
                └─────────────────────────────────────────────────────┘
```

### 3.2 Data flow per turn

1. Mic stream → wake-word model on 80ms chunks (always-on, low CPU).
2. On wake → audible "yes" → start recording.
3. RMS-based silence detection ends the recording (>=900ms below threshold *after* hearing speech).
4. Buffer → Whisper → text.
5. Text + retrieved memories → Qwen via Ollama with tool schema.
6. If tool call: validate JSON → check risk gate → (confirm by voice if needed) → execute via MCP client → feed result back.
7. Final response → `say` → speaker.
8. Conversation + extracted facts → memory store.

### 3.3 Process boundaries

- **Single Python process** owns the mic, the LLM client, the MCP clients, and the memory DB.
- **Ollama** runs as its own background daemon (`ollama serve`), reachable on `localhost:11434`.
- **MCP servers** run as subprocesses spawned by the Python process (stdio transport).

---

## 4. Repository Layout

```
~/jarvis/
├── pyproject.toml              # uv project, Python 3.12, deps
├── README.md                   # Setup + roadmap
├── PLAN.md                     # This file
├── LICENSE
├── .gitignore
├── .env.example
├── src/jarvis/
│   ├── __init__.py             # __version__
│   ├── __main__.py             # python -m jarvis
│   ├── cli.py                  # argparse: echo | run | setup
│   ├── config.py               # Settings dataclass, env-driven
│   ├── audio/
│   │   ├── wakeword.py         # openwakeword listener
│   │   ├── recorder.py         # mic + energy-based end-of-speech
│   │   ├── stt.py              # faster-whisper wrapper
│   │   └── tts.py              # `say` subprocess
│   ├── loops/
│   │   └── echo.py             # Phase 1 loop (no LLM)
│   ├── agent/                  # [Phase 2+] tool-use loop, Ollama client
│   ├── tools/                  # [Phase 3+] MCP client, tool registry, risk gates
│   └── memory/                 # [Phase 7] sqlite-vec, fact extraction, retrieval
└── ~/.jarvis/                  # Operational data dir (outside repo)
    ├── memory.db               # SQLite + sqlite-vec
    ├── oauth/                  # OAuth tokens (Gmail, Calendar)
    └── logs/
```

---

## 5. Build Phases

Estimates assume focused work sessions; calendar time will be longer.

### Phase 0 — Foundations ✅ DONE

- [x] `uv` Python 3.12 project at `/Users/gavin/jarvis`
- [x] Repo layout (`src/jarvis/{audio,agent,tools,memory,loops}`)
- [x] `pyproject.toml` with Darwin-only resolver scope (workaround for `tflite-runtime`)
- [x] `.gitignore`, `.env.example`
- [x] `jarvis --version` and `jarvis --help` working

**Done when:** ✅ `uv run jarvis --version` prints `jarvis 0.1.0`.

---

### Phase 1 — Hello-voice echo loop ✅ DONE (hardware-validated 2026-05-15)

End-to-end voice pipeline with **no intelligence** — JARVIS just repeats what you said.
Highest-risk phase because it's the first time real audio touches real hardware.

- [x] `audio/wakeword.py` — openwakeword listener, blocks until "Hey Jarvis"
- [x] `audio/recorder.py` — mic capture + RMS-energy end-of-speech detection
- [x] `audio/stt.py` — faster-whisper `small.en`, lazy-loaded
- [x] `audio/tts.py` — macOS `say` subprocess
- [x] `loops/echo.py` — wires the four together
- [x] `jarvis setup` pre-downloads wake-word + Whisper models
- [x] All deps installed (`uv sync` succeeded — 43 packages in `.venv`)
- [x] Import smoke-test passes
- [x] **User test:** ran `uv run jarvis echo`, granted mic permission, loop validated end-to-end (2026-05-15)

**Done when:** "Hey Jarvis, testing one two three" → JARVIS says back "testing one two three".

**Known knobs to tune after first test (config.py):**
- `wake_threshold` (default 0.5) — raise to reduce false wakes
- `silence_rms_threshold` (default 0.01) — raise in noisy rooms, lower in quiet ones
- `silence_duration_ms` (default 900) — how long of a pause ends a sentence

#### How to measure each number

The echo loop now prints what you need — you just read it off the terminal.

| Metric | How to get it |
|---|---|
| **Tuning values** | Whatever is in `config.py` after you stopped fiddling. If you never changed them, they're the defaults above — write "default". |
| **Latency** | The `[timing]` line prints it: `eos->audio` is end-of-speech → first audio out (the headline number). Do ~5 turns, take the rough median. |
| **STT accuracy** | Eyeball the `[stt]` line vs. what you said over ~10 utterances. Note the miss rate and any *consistent* misses (names, jargon). |
| **False wakes** | Leave `jarvis echo` running while you work/talk normally. Each wake prints `[wake #N @ HH:MM:SS]`. Count the ones you didn't trigger; note the window length. |

Procedure: run `uv run jarvis echo`, do ~5 clean test phrases for latency/STT, then leave it idle-running ~30–60 min for the false-wake count. Copy the numbers into the block below.

#### Baseline measurements (fill in — this is the data that makes Phase 1 resume-grade)

```
Date / room ............ 2026-05-15, <quiet apartment | noisy | etc.>
Hardware ............... <Mac model>, 16GB RAM

Tuning (config.py):
  wake_threshold ....... <default 0.5 | changed to X because ...>
  silence_rms_threshold  <default 0.01 | changed to X because ...>
  silence_duration_ms .. <default 900 | changed to X because ...>

Latency (median of ~5 turns, from [timing] line):
  stt .................. <___ ms>
  eos -> audio ......... <___ ms>   <- headline Phase 1 latency

STT accuracy:
  ~<__>/10 utterances correct
  consistent misses .... <none | "Gavin"->"Galvin" | etc.>

False wakes:
  <__> false wakes over <__> min of normal use  (target: <1/day)

Verdict: <ship as-is | needs Silero VAD | needs medium.en | etc.>
```

---

### Phase 2 — Add the brain (LLM in the loop) ✅ CODE DONE / ⏳ VOICE TEST PENDING

Swap the echo step for a real Qwen response. Still no tools.

- [x] `brew install ollama` (0.24.0) + `ollama serve` running with
  `OLLAMA_FLASH_ATTENTION=1 OLLAMA_KV_CACHE_TYPE=q8_0` (RAM-friendly)
- [x] `ollama pull qwen2.5:7b-instruct-q4_K_M` (4.7GB, verified in `ollama list`)
- [x] Added `ollama>=0.3.0` dep (resolved to 0.6.2), `uv sync` OK
- [x] `agent/llm.py` — thin Ollama chat wrapper, clear error if daemon down
- [x] `agent/prompt.py` — voice-tuned system prompt (brief, no markdown — it gets read aloud)
- [x] `loops/chat.py` — wake → STT → LLM → TTS, with `[timing]` instrumentation
- [x] Session history: last `max_history_turns` (6) pairs, system prompt pinned
- [x] `jarvis run` wired in `cli.py` (removed now-unused `sys` import)
- [x] Headless validation: import + history-trim unit check + live LLM round-trip
- [ ] **User test:** `uv run jarvis run`, say "Hey Jarvis, what's the capital of France"

**Done when:** "Hey Jarvis, what's the capital of France" → spoken answer.

**Headless measurements (2026-05-18):**
- LLM cold start (first call, loads 4.7GB into RAM): **~29s** — the first
  query after launch is always slow. Mitigation idea: a warm-up call at
  startup so the *user's* first turn is fast (deferred — note it).
- LLM warm latency, short answers: **~0.6–1.1s** (well under the 2.5s budget).
- System prompt working: replies are short, plain prose, no markdown.

**Estimate:** ~½ day. (Actual: code complete; voice test outstanding.)

---

### Phase 3 — First MCP tool: web search ✅ CODE DONE + MEASURED / ⏳ VOICE TEST + TUNING

Validates the MCP plumbing on a read-only tool (no confirmation flow needed yet).
Detailed plan: see `PHASE3.md`.

- [x] Added `mcp` Python SDK dep
- [x] `tools/mcp_client.py` — MCP server subprocess + stdio session, list/call
- [x] Web-search server: **DuckDuckGo MCP** (`uvx duckduckgo-mcp-server`,
  keyless — debug the loop without an external dependency; swap to Tavily
  later, see PHASE3.md §4)
- [x] `agent/tool_loop.py` — **native** Ollama tool-calling (not prompt-based
  JSON, PHASE3.md §3) + schema validation + retry-with-error + risk gate +
  iteration cap + graceful fallbacks
- [x] `tools/registry.py` — central registry; `name/description/schema/risk_level`
- [x] Web search registered with `risk: read`
- [x] `evals/toolcall.py` — 28-case golden set + reproducible scorecard
- [ ] **Voice test:** `uv run jarvis run` → "Hey Jarvis, what's the weather…"
- [ ] **Tuning (Phase 3.1):** close the 4 under-calls (see below)
- [ ] **Latency (Phase 3.1):** search-turn p50 ~15s is over the ~5s budget

**Done when:** "Hey Jarvis, what's the weather in San Francisco" returns a real, current answer. ✅ (verified headless; voice pending)

#### Scorecard — `uv run python evals/toolcall.py` (2026-05-18, DuckDuckGo)

```
Overall tool-call accuracy ... 24/28  (86%)   [target >=90%]
  should-search recall ....... 10/14
  no-search specificity ...... 14/14   (zero over-calls)
Under-calls (missed) ......... 4
  - who won the game last night
  - what time does the Apple store in Palo Alto close today
  - who is the current prime minister of Japan
  - how did the stock market do today
Over-calls ................... 0
Errors ....................... 0
Latency: search p50=14.9s p95=24.3s | no-search p50=2.0s p95=3.8s
```

**Honest read (the interview story):** the model is *conservative* — zero
false searches (perfect specificity), but it under-calls when its stale
parametric knowledge *feels* sufficient (current officeholder, "who won",
market direction). That's a **prompt/description tuning** problem, not an
architecture failure, and 86% is above the PHASE3.md §13 escape-hatch
threshold (~85%) — no model swap warranted; sharpen the §7 rules in Phase
3.1. Latency (~15s p50 for search turns) is the bigger gap: root cause is
the 2nd LLM round synthesizing from ~3KB of raw DDG snippets on a local 7B.
Fixes: trim results before synthesis, tighter synthesis prompt, or move to
Tavily (returns a pre-synthesized answer). Recording the real numbers
rather than a flattering one is the point of having an eval.

**Estimate:** ~1 day. (Actual: core + eval done; Phase 3.1 tuning outstanding.)

---

### Phase 4 — macOS control + voice-confirmation flow

The "JARVIS feels real" moment. Also where safety gates come online because AppleScript can do anything.

- [ ] `tools/applescript.py` — `run_applescript(script: str)` MCP tool, `risk: write`
- [ ] Filesystem MCP server (read paths whitelisted, write paths confirmed)
- [ ] `agent/confirmation.py` — voice-confirm flow:
  - Build human-readable summary of the pending action
  - `say` the summary + "Confirm?"
  - Record a short response (≤3s)
  - STT → match against yes/no patterns (`yes|yeah|sure|do it` vs `no|nope|cancel`)
  - If ambiguous: re-ask once, then default to no
- [ ] Risk-level enforcement in `tool_loop.py`: anything ≥ `write` routes through confirmation
- [ ] Common scripts as built-in helpers: open app, control Music, set Do Not Disturb, type text into focused app

**Done when:** "Hey Jarvis, open Spotify and play Daft Punk" → confirms → plays.

**Estimate:** ~1–2 days. Confirmation UX is the fiddly bit.

---

### Phase 5 — Calendar + reminders

- [ ] Google Calendar MCP server (open-source one; one-time OAuth in browser)
- [ ] Store OAuth tokens in `~/.jarvis/oauth/` with `0600` perms
- [ ] Apple Reminders via AppleScript (no OAuth, easier)
- [ ] Tool risk levels: read-list = `read`, create-event = `write`, delete-event = `destructive`

**Done when:**
- "What's on my calendar tomorrow?" → spoken summary
- "Add a reminder to email Sarah at 3pm" → creates reminder (confirmed)

**Estimate:** ~1 day. OAuth dance is the main friction.

---

### Phase 6 — Gmail

Saved for last because it has the largest schema surface and the highest stakes (sending email).

- [ ] Open-source Gmail MCP server + OAuth
- [ ] Read/search tools first; ship and use them for a day
- [ ] Then draft tools (create draft, save to Drafts folder — still safe)
- [ ] Finally send tool: `risk: destructive`, always confirms with full preview of recipient + subject + first 100 chars of body

**Done when:**
- "Summarize my unread email" works reliably
- "Reply to Alex's email saying I'm in" drafts → confirms → sends

**Estimate:** ~1–2 days.

---

### Phase 7 — Long-term memory

- [ ] `ollama pull nomic-embed-text` (274MB embedding model)
- [ ] Add `sqlite-vec` extension; create `memory.db` with `episodes` and `facts` tables
- [ ] `memory/store.py` — write episodes + embeddings each turn
- [ ] `memory/retrieve.py` — top-K relevant retrieval at turn start, injected into system prompt
- [ ] `memory/extract.py` — periodic job (every 10 turns?) that asks the LLM to extract durable facts from recent episodes into the `facts` table
- [ ] `memory/manage.py` — CLI to inspect, delete, or export memories (`jarvis memory list`, `jarvis memory forget <id>`)

**Done when:** Tell it "I prefer matcha over coffee" Monday; next week, ask "what should I order at the cafe" — it remembers.

**Estimate:** ~1–2 days. Extraction prompt tuning is the variable cost.

---

## 6. Privacy & Safety

### What stays local, always

- Microphone audio (never persisted by default; transient buffer only)
- Whisper transcripts
- LLM prompts and responses
- Memory database (`~/.jarvis/memory.db`)
- OAuth tokens (`~/.jarvis/oauth/`)
- Conversation logs

### What leaves the machine (and when)

- Web search queries → search-provider API (Phase 3+). Treat search queries as semi-public.
- Gmail / Calendar API calls → Google. Necessary for those tools to work; OAuth-scoped.

### Confirmation matrix (Phase 4 onward)

| Risk | Examples | Behavior |
|---|---|---|
| `read` | search the web, read calendar, list files, query memory | execute silently |
| `write` | create event, create draft, open app, run AppleScript | voice-confirm with summary |
| `destructive` | send email, delete file, run arbitrary shell | voice-confirm + read full payload aloud |

Confirmation default on ambiguous STT: **no**. Better to make Gavin repeat himself than to send the wrong email.

---

## 7. Open Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Qwen 2.5 7B tool-use is too unreliable | Medium | JSON schema validation + retry-with-error. Escape hatch: hybrid mode (Claude API for agent loop, keep local audio) |
| openwakeword false wakes are annoying | Medium-low | Tune threshold; consider Picovoice Porcupine (paid, more accurate) if intolerable |
| Whisper `small.en` misses domain words | Low | Upgrade to `medium.en` (1.5GB) or use prompt-conditioning |
| macOS `say` voice gets unbearable | Certain | Phase 1.5 swap to Piper — drop-in replacement, ~1hr work |
| Energy-based VAD cuts off in noisy rooms | Medium | Swap to silero-vad (already bundled by openwakeword) |
| OAuth tokens leak | Low | `0600` perms, document that `~/.jarvis/` is sensitive |
| Mac dies, memory gone | Real but tolerable for v1 | Phase 7+: `restic` encrypted backups to B2/S3 |

---

## 8. Out of Scope for v1 (Maybe Later)

- Mobile / phone client
- Multi-user
- Real-time barge-in (interrupt TTS by speaking)
- Voice biometrics / wake-word personalization
- Streaming responses (TTS starts before LLM finishes)
- Proactive notifications ("flight delayed", "meeting in 5")
- Multi-agent orchestration (specialist sub-agents)
- Smart-home control (HomeKit / HA)
- Cloud sync of memory (still rejected by current privacy posture)
- A GUI

---

## 9. Current Status (2026-05-18)

- **Phase 0:** ✅ Complete
- **Phase 1:** ✅ Complete — hardware-validated 2026-05-15
- **Phase 2:** ✅ Code complete + headless-validated 2026-05-18; ⏳ awaiting voice test
- **Phase 3:** ✅ Core + eval done 2026-05-18 (86% tool-call accuracy, 0 over-calls);
  ⏳ voice test + Phase 3.1 tuning (4 under-calls, ~15s search latency)
- **Phase 4–7:** Not started

**Next concrete action:** Voice test — `uv run jarvis run`, say "Hey Jarvis,
what's the weather in San Francisco", confirm a real spoken answer. Then
Phase 3.1 (sharpen tool-use prompt to close the 4 under-calls; cut search
latency) before moving to Phase 4.
