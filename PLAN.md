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

Estimates assume focused work sessions; calendar time will be longer. Each
non-trivial phase has its own deep-dive doc (`PHASE3.md` … `PHASE7.md`) with
file-by-file build breakdowns, failure-mode tables, latency budgets, eval
designs, and escape hatches. The sections below are summaries — open the
phase file when you're about to actually build it.

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
Detailed retrospective: see [PHASE1.md](PHASE1.md).

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
Detailed plan + retrospective: see [PHASE2.md](PHASE2.md).

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
Detailed plan: see [PHASE3.md](PHASE3.md).

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

The "JARVIS feels real" moment, and where the safety gate comes online for
every `risk: write` / `destructive` tool. Detailed plan: see
[PHASE4.md](PHASE4.md).

Milestones:

- [ ] AppleScript tool (`risk: write`)
- [ ] Filesystem MCP server registered with per-op risk levels
- [ ] `agent/confirmation.py` — voice-confirm flow, ambiguous → no
- [ ] Risk gate enforced in `tool_loop.py` (every ≥`write` call routes through `confirm`)
- [ ] `evals/confirm.py` — decision-table test for yes/no classification

**Done when:** "Hey Jarvis, open Spotify and play Daft Punk" → confirms → plays.

**Estimate:** ~1–2 days. Confirmation UX is the fiddly bit.

---

### Phase 5 — Calendar + reminders

First phase with a third-party identity (Google OAuth) and with real
time-grounding requirements. Detailed plan: see [PHASE5.md](PHASE5.md).

Milestones:

- [ ] Google Calendar MCP server; tokens at `~/.jarvis/oauth/google.json` (0600)
- [ ] Apple Reminders via dedicated AppleScript tool (not generic `run_applescript`)
- [ ] Per-op risk: list=`read`, create=`write`, delete=`destructive`
- [ ] `tools/time_grounding.py` + `evals/time.py` (100% target — parsing is deterministic)
- [ ] System prompt prepends current date/time each turn

**Done when:**

- "What's on my calendar tomorrow?" → spoken summary
- "Add a reminder to email Sarah at 3pm" → creates reminder (confirmed)

**Estimate:** ~1 day. OAuth dance and time grounding are the main friction.

---

### Phase 6 — Gmail

Last because it has the largest schema surface and the highest stakes —
sending an email is the first thing the assistant can do that genuinely
cannot be undone. Detailed plan: see [PHASE6.md](PHASE6.md).

Milestones (a layered rollout — don't compress the dwell times):

- [ ] Gmail MCP server; tokens at `~/.jarvis/oauth/gmail.json` (0600)
- [ ] **Read layer** shipped + used ≥3 days
- [ ] **Draft layer** (`risk: write`) shipped + used ≥2 days
- [ ] **Send layer** (`risk: destructive`) unlocked only after `evals/gmail.py` ≥90% verb-pick
- [ ] Send confirmation reads recipient + subject + body preview aloud (`alex dot chen at gmail dot com`)
- [ ] `tools/address_book.py` (manual JSON map) — model asks rather than guessing addresses

**Done when:**

- "Summarize my unread email" works reliably
- "Reply to Alex's email saying I'm in" drafts → confirms → sends

**Estimate:** ~1–2 days of code, plus the layered dwell time.

---

### Phase 7 — Long-term memory

Cross-cutting — touches every turn. Memory's failure modes are silent, so
the deliverable is the *eval framework* as much as the storage layer.
Detailed plan: see [PHASE7.md](PHASE7.md).

Milestones:

- [ ] `ollama pull nomic-embed-text`; sqlite-vec extension wired in `db()` helper
- [ ] `memory/store.py` + `memory/retrieve.py` (episodes + facts split)
- [ ] `memory/extract.py` — periodic, off the critical path
- [ ] `memory/manage.py` + CLI (`jarvis memory list / forget / export / compact / remember`)
- [ ] `evals/memory.py` — plant-and-recall harness with positive *and* negative rows
- [ ] Retrieval p50 <100ms (memory may not slow turns down)

**Done when:** Tell it "I prefer matcha over coffee" Monday; next week,
ask "what should I order at the café?" — it remembers.

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

---

## 10. Command Reference

Single source of truth for every `jarvis ...` subcommand across phases.
Updated each time a new command lands.

| Command | Phase | What it does |
|---|---|---|
| `jarvis --version` | 0 | Print version. |
| `jarvis --help` | 0 | Subcommand help. |
| `jarvis setup` | 1 | Pre-download wake-word + Whisper models. |
| `jarvis setup memory` | 7 | `ollama pull nomic-embed-text`; create empty `memory.db`; load sqlite-vec. |
| `jarvis echo` | 1 | Voice loop with no intelligence (echo). |
| `jarvis run` | 2+ | The real assistant — wake → STT → agent → TTS. |
| `jarvis auth gcal` | 5 | One-time OAuth browser flow for Google Calendar. |
| `jarvis auth gmail` | 6 | Same for Gmail (separate scopes / token). |
| `jarvis auth status` | 5 | List known OAuth tokens + expiry + file perms. |
| `jarvis gmail enable <read\|draft\|send>` | 6 | Set the Gmail capability layer (writes to `~/.jarvis/env`). `send` requires `--force`. |
| `jarvis memory list [--search Q]` | 7 | Dump all facts (or top-K matching Q). |
| `jarvis memory remember "<statement>"` | 7 | Manual fact insertion (escape hatch when extraction missed something). |
| `jarvis memory forget <id>` | 7 | Delete one fact + its vec row. |
| `jarvis memory export <path>` | 7 | JSON dump of all facts. |
| `jarvis memory compact` | 7 | VACUUM the DB; rebuild vec indices. |
| `jarvis memory wipe --force` | 7 | Delete every episode and fact. Saves an export first. |

---

## 11. First-Run Setup (one-time)

The order is deliberate — each step proves the prior one works.

1. **System deps**
   - `brew install ollama` (≥0.3 for native tool-calling)
   - `OLLAMA_FLASH_ATTENTION=1 OLLAMA_KV_CACHE_TYPE=q8_0 ollama serve` (run as a launchd service in practice)
   - `ollama pull qwen2.5:7b-instruct-q4_K_M`
2. **Python env** — `cd ~/jarvis && uv sync` (Python 3.12, Darwin resolver scope).
3. **Microphone permission** — `uv run jarvis echo` once. macOS prompts; grant.
4. **Wake-word + Whisper models** — `uv run jarvis setup`. Pre-downloads so the first wake isn't a 30s freeze.
5. **Smoke** — `uv run jarvis echo`, say "Hey Jarvis, testing one two three." JARVIS repeats it. Phase 1 green.
6. **LLM smoke** — `uv run jarvis run`, ask "what's the capital of France." Spoken answer in <2.5s warm. Phase 2 green.
7. **Tool smoke** — same command, ask "what's the weather in San Francisco." Returns a real, current answer (Phase 3).
8. **Phase 4+ unlocks** — at each later phase, the corresponding `jarvis auth <service>` is the one-time per-service setup.

The setup data lives in two places:

- Code + plans: `~/jarvis/` (this repo).
- Operational data: `~/.jarvis/` (memory.db, oauth/*.json, logs/, env, address_book.json). Never goes into git; backed up via `restic` if at all (Phase 7+).

---

## 12. Observability

Every phase emits a `[timing]` line per turn with stage-level millisecond
breakdowns. The keys grow as phases land — this table is the contract.

| Key | Introduced | What it measures |
|---|---|---|
| `stt` | 1 | mic buffer → text |
| `eos->audio` | 1 | end-of-speech → first TTS audio (headline UX number) |
| `total` | 1 | end-of-speech → silence after TTS |
| `agent` | 2 | LLM + tool calls (sum of all rounds) |
| `tool_n` | 3 | Per-tool round-trip (`tool_web_search`, `tool_calendar_list`, …) |
| `llm_round_n` | 3 | Per LLM round-trip inside the tool loop |
| `confirm` | 4 | Phase 4 confirmation overhead (TTS + record + STT + classify) |
| `oauth_refresh` | 5 | Whenever a token refresh fires (should be rare) |
| `retrieve` | 7 | Memory retrieval at turn start |
| `embed` | 7 | Per embedding call (`retrieve` decomposes into `embed + sql`) |

Logs and timings go to `~/.jarvis/logs/jarvis-YYYY-MM-DD.log` (line-per-turn
JSON when running with `--log-json`, otherwise human-readable). No
audio buffers, STT transcripts, or LLM content are logged by default —
the timing line is the operational signal; content lives only in
memory.db once Phase 7 ships.
