# JARVIS — Design Decisions

> A walkthrough of every non-trivial choice in this project, with the alternatives
> I considered and why I rejected them. This is the document I'd use to explain
> JARVIS in an interview. It is intentionally opinionated: the goal is to show
> engineering judgment, not exhaustive neutrality.

---

## 0. Executive Summary

**What it is.** A daily-use, voice-activated personal assistant ("Hey Jarvis…")
running entirely on an Apple Silicon Mac. Wake word, speech-to-text, LLM, tool
calls, memory, and text-to-speech all happen on-device.

**Why it exists.** Two answers, both true:
1. **Personal.** I wanted a private, hackable assistant that doesn't pipe my
   life to OpenAI or Google.
2. **Engineering.** A constrained environment (single 16GB Mac, local 7B
   model, single-user) is the most interesting place to make AI engineering
   tradeoffs. Cloud GPT-4 makes most decisions trivial.

**Core architectural bet.** A **local 7B model + reliable tool-use** is now
strong enough to feel useful for everyday tasks, *if* the system around it
(JSON validation, retry-on-error, tight prompts, voice-confirm safety) does
its job. If that bet is wrong, the system degrades to "fancy Siri" rather
than failing outright.

**What "done" means for v1.** Seven phases (audio → LLM → tools → macOS
control → calendar → Gmail → memory) shipped end-to-end on one machine, with
measured latency and tool-call accuracy.

---

## 1. Product Shape

### 1.1 Voice-first, single-user, local-only

| Decision | Choice | Rejected alternative | Why |
|---|---|---|---|
| Modality | Voice + wake word | Text-only chat | Chat already exists (ChatGPT, Claude). Voice is where the friction is, where Siri/Alexa fall short on capability, and where "JARVIS feel" lives. |
| Users | Single (me) | Multi-user / family | Auth, voice ID, per-user memory, and conflict resolution are large problems. Solving them before the core works is premature. |
| Network posture | Local-only by default | Cloud LLM fallback | Privacy is the user-visible differentiator. Adding cloud later is easy; ripping it out once users depend on it is not. |
| Deployment | Long-running process on my Mac | Daemon / launchd service | Foreground process is debuggable. Daemonization is a Phase 8 polish concern. |

**Tension acknowledged.** Local-only caps model quality and latency. I'm
betting the *whole system* feels good even if any single component is
mid-tier, because audio + tools + memory compound — and because privacy is a
real differentiator users will trade some capability for.

### 1.2 Non-goals (and why each was rejected for v1)

- **Mobile/phone client** — requires a server posture and a sync model. Solve
  the desktop loop first.
- **Real-time barge-in** (interrupt TTS by speaking) — significant audio
  engineering work for marginal product gain in v1.
- **Proactive notifications** ("your flight is delayed") — requires a
  scheduler, event sources, and a UX for non-disruptive interruption. v1 is
  reactive only; this is the single biggest deliberate scope cut.
- **Multi-agent orchestration** — see §3.3. Premature complexity until a
  single agent demonstrably fails.

---

## 2. The LLM Stack

### 2.1 Local vs cloud

**Choice: 100% local (Ollama).**

Cloud (Claude / GPT-4) would give better tool-use, lower latency on a fast
network, and remove model-management overhead. Rejected because:

1. **Product premise.** A "private assistant" that leaks every prompt to a
   third party is a contradiction.
2. **Engineering signal.** Cloud frontier models make tool-use trivial. The
   interesting work — schema-constrained generation, retry-on-error,
   prompt engineering for small models — only happens at the local tier.
3. **Cost predictability.** Continuous always-listening, multi-turn tool
   loops can rack up API spend fast. Local is a fixed cost.

**Escape hatch.** Hybrid mode (Claude API for the agent loop, local for
audio + memory) is on the table if the local model proves unreliable for
tool-use. Architected for: the LLM client is one module behind an
interface; swapping in the Anthropic SDK is a one-file change.

### 2.2 Which local model

**Choice: Qwen 2.5 7B Instruct (Q4_K_M quantization).**

Candidates evaluated:

| Model | Size (Q4) | Tool use | Notes |
|---|---|---|---|
| **Qwen 2.5 7B Instruct** | ~4.7GB | Strong | Trained explicitly for function calling; current best-in-class at the 7B tier for tool use. **Winner.** |
| Llama 3.1 8B Instruct | ~5GB | Good | Capable generalist, weaker structured-output adherence than Qwen 2.5. |
| Mistral 7B Instruct v0.3 | ~4.4GB | Mixed | Older; tool use is bolt-on rather than trained-in. |
| Phi-3.5-mini (3.8B) | ~2.3GB | Decent | Tempting for latency, but reasoning trails noticeably; would hurt the agent loop. |
| Hermes-2-Pro Llama-3 8B | ~5GB | Strong | Fine-tuned for tool use; close second choice. Switch candidate if Qwen disappoints. |

**Why 7B and not bigger.** 16GB unified memory has to share between the OS,
Whisper (~500MB resident), embeddings (~300MB), the OS audio stack, and
whatever browser tabs I forgot to close. A Q4 7B model at ~5GB leaves
headroom; a Q4 13B at ~8GB does not.

**Why Q4_K_M specifically.** K-quants preserve perplexity better than legacy
Q4_0 at near-identical size. Q4_K_M is the standard sweet spot — Q5 gains
~1% perplexity for 25% more RAM, Q3 loses noticeable quality. Default to
the well-trodden path.

**Things I'd want to measure in production:**
- Tool-call accuracy on a 200-prompt golden set (target: ≥90%).
- Latency p50/p95 from end-of-speech to first audio out.
- Memory hit rate (how often retrieved facts actually appear in responses).

### 2.3 Why Ollama (vs llama.cpp / MLX)

**Choice: Ollama.**

| Runtime | Pros | Cons |
|---|---|---|
| **Ollama** | OpenAI-compatible API, model management built in, embeddings included, runs as a daemon | A wrapper around llama.cpp — slight overhead, less control |
| llama.cpp direct | Maximum control, latest features first | I'd be writing my own model-pull and HTTP layer |
| MLX (Apple's framework) | Best Apple Silicon perf on paper | Smaller model ecosystem, no built-in chat server, tool-use story less mature |
| LM Studio | Nice GUI | GUI-first, not meant to be embedded |

Ollama is "boring middleware" — it does one thing (serve a model) over a
stable HTTP API. That makes the rest of the system simpler. If
performance becomes the bottleneck I can swap to MLX or raw llama.cpp
behind the same interface.

---

## 3. Agent Architecture

### 3.1 Tool-use loop, not a chain

**Choice: ReAct-style tool-use loop.** Model emits a tool call, system
executes, result feeds back, repeat until the model emits plain text.

Rejected: hardcoded chains ("if user mentions weather, call weather tool").
That doesn't generalize and isn't really an agent.

### 3.2 Defensive JSON handling

Local 7B models hallucinate tool calls more often than frontier models.
The loop assumes failure:

1. **Strict JSON schema validation** on every tool call.
2. **Retry once with error feedback** — feed the validation error back as a
   tool-result-style message and let the model self-correct. This single
   technique recovers a large fraction of malformed calls in practice.
3. **Bail to plain text on second failure** — better to apologize than to
   spiral.

This is the single most important reliability technique in the system. A
plain "call the tool" loop with a local model is ~60-80% reliable; with
schema validation + retry it climbs much higher.

### 3.3 Single-agent vs multi-agent

**Choice: Single tool-use loop.** No router, no specialist sub-agents, no
planner/executor split.

Multi-agent is fashionable in 2025–26 but adds latency, debugging surface,
and prompt-engineering work. The principle: **don't reach for multi-agent
until evidence proves the single agent has hit a ceiling.** Evidence would
look like: tool-call accuracy plateaus below target, context window
overflows on common requests, or tasks genuinely require parallel sub-tasks.

This is a deliberate "show judgment" choice. The temptation to over-architect
is the trap I'm avoiding.

### 3.4 MCP for tool extensibility

**Choice: Model Context Protocol (MCP) servers for all tools.**

| Approach | Pros | Cons |
|---|---|---|
| **MCP servers** | Inherits a growing ecosystem (Gmail, Calendar, FS, search already exist); transport-agnostic; works across LLM clients | Subprocess management, stdio quirks |
| Hand-rolled Python tools | Direct, no IPC overhead | I rebuild what others have built; no portability |
| LangChain tools | Familiar | Heavy dep, opinionated abstractions, churn |

MCP is the right bet for 2026 — it's becoming the de facto interop layer
for LLM tools. Building on it means JARVIS gets new tools "for free" as
the ecosystem grows.

---

## 4. Audio Pipeline

### 4.1 Wake word

**Choice: openWakeWord with the shipped `hey_jarvis_v0.1` model.**

| Engine | Quality | Cost | Customizable | Why not |
|---|---|---|---|---|
| **openWakeWord** | Good | Free / OSS | Yes (train your own) | **Winner.** Ships a "Hey Jarvis" model — perfect fit. |
| Picovoice Porcupine | Excellent | Free tier for personal | Yes (paid) | Better accuracy but paid for any production use; OSS is the cleaner story. |
| Snowboy | Decent | Free but abandoned | — | Project is dormant — avoid. |
| Always-on Whisper | Best accuracy | Heavy on CPU | — | Burns the laptop; defeats the point of a wake word. |

**Always-on vs push-to-talk.** Always-on (wake word) is the JARVIS feel.
Push-to-talk would be more reliable but worse product.

### 4.2 STT

**Choice: faster-whisper `small.en`.**

- **faster-whisper** over `whisper.cpp`: pip-installable, comparable
  performance on Apple Silicon via CTranslate2, easier to keep in-process.
- **`small.en`** (244M params) over `tiny`/`base`/`medium`: tiny/base have
  noticeable word-error rate jumps on real-world audio; medium adds latency
  and memory for marginal gain on conversational queries. English-only
  variant is meaningfully better than the multilingual one at the same
  size — I only speak English to it.
- **Lazy loading.** Whisper loads on first wake, not at startup, so cold
  start to "listening" is fast.

**Upgrade path.** If domain vocabulary (names, technical terms) gets missed,
options in order: (1) prompt-conditioning with recent context, (2) upgrade
to `medium.en` (~1.5GB), (3) fine-tune on personal corpus.

### 4.3 End-of-speech detection

**Choice: RMS energy threshold + duration window.** Recording ends after
≥900ms below an RMS threshold *once speech has been detected*.

Rejected for v1: **Silero VAD** (neural voice-activity detection). It's
better — handles noise and non-speech sounds. Listed as the fallback if
RMS-based VAD proves annoying. I'm starting with the simpler thing because
it's debuggable and the bar is "works in a quiet apartment."

This is a deliberately *replaceable* component. The interface is "give me a
recorded buffer when the user stops talking" — implementation can swap.

### 4.4 TTS

**Choice: macOS `say` for v1; Piper for v2.**

`say` is zero-setup and ships with the OS. The voice is robotic and will
get annoying. Piper (open-source neural TTS) is the upgrade — better
voices, still local, but adds setup. Shipping `say` first means I can use
JARVIS for a week before deciding which Piper voice I actually prefer —
real usage > armchair selection.

---

## 5. Memory

### 5.1 Layered model: episodes + facts

**Choice: two tables.**
- **Episodes** — raw conversation turns with embeddings. The verbatim record.
- **Facts** — extracted, deduplicated, durable statements about the user
  ("prefers matcha over coffee", "sister's name is Sarah"). Smaller, more
  precise, injected directly into the system prompt.

Why both: pure episode retrieval is noisy (the model gets paragraphs when
it needs one sentence). Pure facts loses context and is brittle to bad
extraction. Layering them gives the model both the *gist* (facts) and the
*evidence* (relevant episodes if needed).

### 5.2 Storage

**Choice: SQLite + `sqlite-vec` extension.**

| Option | Pros | Cons |
|---|---|---|
| **SQLite + sqlite-vec** | One file, no daemon, ACID, runs in-process, fast enough for 10k–100k vectors | Not for billions of vectors — but I'll never have those |
| Chroma | Easy API | Separate process, more moving parts |
| Qdrant / Weaviate | Production-grade | Massive overkill for one user |
| Pinecone / cloud vector DB | Managed | Defeats the privacy posture |
| Flat numpy file | Trivial | No filtering, no metadata joins |

The total dataset size after a year of heavy use is maybe 100MB. A vector
DB is the wrong tool. SQLite + sqlite-vec is the right tool *because* it
matches the data size — not because it's fancier.

### 5.3 Embeddings

**Choice: `nomic-embed-text` (274MB), served via Ollama.**

- Local — same privacy posture as the rest of the stack.
- Runs in the runtime I already have (Ollama), so no second model server.
- Strong on retrieval benchmarks for its size; competitive with OpenAI's
  ada-002 on MTEB at zero cost and zero data egress.

Rejected: `bge-small` (slightly worse quality), OpenAI embeddings
(privacy), sentence-transformers in-process (yet another model loaded into
RAM).

### 5.4 Retrieval strategy

Top-K cosine similarity over episode embeddings at the *start* of each
turn, with K ~5, plus *all* extracted facts (they're small). Retrieved
context is injected into the system prompt, not into the user turn — this
avoids confusing the model about who said what.

**Decay / recency:** not implemented in v1. Open question whether to weight
recent episodes higher. The Phase 7 evaluation will tell me whether the
naive top-K is good enough.

### 5.5 Fact extraction

A periodic job (every N turns, batched) asks the LLM to extract durable
facts from recent episodes. Output is JSON-schema-validated and
deduplicated against existing facts via embedding similarity before
insertion.

This is the part most likely to need iteration. The extraction prompt is
load-bearing: too aggressive and the facts table fills with noise; too
conservative and nothing useful is ever extracted.

---

## 6. Safety & Privacy

### 6.1 Risk-tagged tools

Every tool declares `risk: read | write | destructive`. The agent loop
enforces the gate, not the tool — so a tool author can't accidentally skip
confirmation.

| Risk | Examples | Default behavior |
|---|---|---|
| `read` | Web search, list calendar, query memory | Execute silently |
| `write` | Create event, draft email, open app | Voice-confirm with one-line summary |
| `destructive` | Send email, delete file, run shell | Voice-confirm + read full payload aloud |

### 6.2 Ambiguous-confirmation default

If the user's voice response to a confirmation doesn't clearly match
yes/no patterns (or STT failed), the default is **no**. Asymmetric cost:
making the user repeat themselves is annoying; sending the wrong email is
a disaster.

### 6.3 What leaves the machine

Documented in PLAN.md §6. The short version: audio, transcripts, LLM
prompts, memory, OAuth tokens — all local. Web search queries and
Gmail/Calendar API calls necessarily leave (you can't search Gmail without
talking to Google). The user-visible boundary is explicit.

### 6.4 What's deliberately *not* secured

- No on-disk encryption of `~/.jarvis/memory.db`. Mac FileVault handles
  the threat model (lost laptop). Re-encrypting at the app layer is
  security theater.
- No voice biometrics. Single-user assumption. Adding this for v2 if I
  ever share the machine.

---

## 7. Operational Decisions

### 7.1 Python 3.12 + a single long-running process

| Language | Why considered | Why not |
|---|---|---|
| **Python 3.12** | Best ecosystem for audio (`sounddevice`), ML glue, fast iteration | **Winner.** GIL doesn't matter much — I/O-bound, not CPU-bound. |
| Rust | Performance, single binary | I'd rebuild every audio/ML wrapper. Premature optimization. |
| Swift | Native macOS APIs, AVAudioEngine | Ecosystem for LLM + MCP tooling is thinner; harder to keep code portable. |
| Go | Good concurrency | Audio + ML libraries are weak. |

**Single process** owns the mic, LLM client, MCP clients, and DB. No IPC.
Easier to reason about, easier to debug. Ollama and MCP servers run as
their own processes — that's the right boundary.

### 7.2 Dependency management: `uv`

Faster than pip, lockfile-first, handles Python version installation. The
2025–26 default for new Python projects.

### 7.3 Repo vs operational data

- **Repo (`~/jarvis/`)** — code, plans, docs. Goes to private GitHub.
- **Operational data (`~/.jarvis/`)** — memory DB, OAuth tokens, logs.
  Never leaves the machine.

Keeping these in two directories prevents an accidental `git add .` from
committing tokens or memory to GitHub. Cheap, important.

### 7.4 Configuration

A single `Settings` dataclass driven by env vars (with `.env` support).
No YAML/TOML config file. Avoids the YAML-config-as-DSL trap and means
defaults live in code where they're discoverable.

---

## 8. Build Order Logic

The phase ordering isn't arbitrary — it's risk-front-loaded.

| Phase | What | Why this order |
|---|---|---|
| 0 | Foundations | Trivial, but blocks everything else |
| 1 | Voice echo loop (no LLM) | **Highest-risk piece.** Audio + permissions + hardware. Fail-fast on the part most likely to be miserable. |
| 2 | Add LLM | Now that audio works, the LLM swap is a one-component change |
| 3 | First MCP tool (web search, read-only) | Validates MCP plumbing on a tool that needs no confirmation |
| 4 | macOS control + voice-confirm | Confirmation flow is fiddly; build it once on a safe surface |
| 5 | Calendar | OAuth + real stakes, but Apple Reminders side is easy |
| 6 | Gmail | Largest schema; highest blast radius; build last when patterns are settled |
| 7 | Memory | Cross-cutting; meaningful only once there's a corpus of conversations to remember |

**Principle:** front-load technical risk, back-load product risk. If
Phase 1 fails (audio is a disaster), stop and reconsider. If Phase 7
underwhelms, the system is still useful without it.

---

## 9. Things I Explicitly Considered and Rejected

These are the "why didn't you do X" questions a sharp interviewer would
ask. Pre-empting them:

| Idea | Why rejected |
|---|---|
| **Fine-tune Qwen on personal data** | Premature. Need a baseline first. Re-evaluate after Phase 7 when there's training data. |
| **RAG over my filesystem** | Phase 8 territory. Solve general assistant first; add semantic file search once core works. |
| **Continuous learning from corrections** | Hard to do correctly; easy to do badly. Memory + manual fact editing is the v1 substitute. |
| **Web UI / desktop GUI** | Voice-first means the GUI is a distraction. CLI is enough for me. |
| **Speculative decoding for lower latency** | Real win possible, but adds complexity. Measure first, optimize second. |
| **Streaming TTS** (TTS starts before LLM finishes) | Big UX win, real engineering work. v2. |
| **Self-hosted on a server, accessed from anywhere** | Defeats the local-only posture. Inverts the whole design. |
| **Switch to a Cloud LLM for "hard" queries only** | Tempting hybrid mode, kept as an escape hatch. But routing logic + privacy boundary is its own complex feature. Don't build it until the local model proves insufficient. |

---

## 10. How I'd Evaluate This System

**The single biggest gap in most portfolio projects is no evaluation.**
What I'd build alongside the code:

### 10.1 Tool-call accuracy benchmark

- A golden set of ~200 user utterances, each labeled with the expected
  tool call (or "no tool — just answer").
- Run on every model under consideration. Report accuracy, precision per
  tool, common failure modes.
- Re-run after every prompt change. This is the regression suite.

### 10.2 Latency budget

A target like: **<2.5s from end-of-speech to first audio out (p50).**
Instrument every stage:

| Stage | Budget |
|---|---|
| End-of-speech detection | 0.9s (the silence window) |
| STT (Whisper small.en, ~5s utterance) | ~0.5s |
| Retrieval (top-K from sqlite-vec) | <0.1s |
| LLM first token | 0.5–1.0s |
| LLM full response | 0.5–1.5s |
| TTS first audio | <0.3s |

If actual numbers diverge, the data points to the right optimization
(streaming, smaller model, faster STT, etc.) rather than guessing.

### 10.3 Wake-word false-positive rate

Leave it running for 24 hours of typical room noise. Count false wakes.
Target: <1/day. This is the metric that decides whether the product is
actually usable.

### 10.4 Memory recall test

Plant facts on Monday ("I prefer matcha"). Ask related questions a week
later ("what should I order at the café?"). Was the fact retrieved? Was
it used? Manually scored, small sample, repeat after retrieval-strategy
changes.

### 10.5 Observability

What gets logged on every turn, deliberately chosen:

- **Latency line** — per-stage milliseconds (`stt`, `agent`, `tool_*`,
  `eos->audio`, `total`). This is the operational signal; it makes
  regressions visible without inspecting any user content.
- **Wake counter** — `[wake #N @ HH:MM:SS]`. Idle-time false-wake rate
  is computed from this stream, not from a heuristic on STT.
- **Tool calls** — name + arguments + 120-char prefix of the result.
  120 chars is the line-length compromise: enough to debug a wrong
  call, short enough that an accidental `tail -f` over the user's
  shoulder doesn't leak the world.
- **Confirmation verdicts** — `[confirm] attempt N: heard 'yes' -> yes`.
  Critical for auditing the safety property over real use.

What deliberately does **not** get logged:

- Whisper transcripts (the user's words).
- LLM messages content.
- Tool call results beyond the 120-char prefix.
- Email bodies, calendar event details, address book lookups.

The asymmetry is intentional: the *operational* layer is observable; the
*content* layer is not, except inside `memory.db` which the user can
inspect, export, or wipe with `jarvis memory ...`.

### 10.6 What I'd build into CI if this were a team project

Not in v1 (single user, no CI), but the design is shaped to permit it:

- `evals/toolcall.py` runs against a fixture LLM (recorded responses) for
  fast determinism, against the live Ollama server in a slower nightly job.
- `evals/confirm.py` and `evals/time.py` are pure-function tests — fast.
- `evals/memory.py` runs against a fresh `memory.db` per invocation;
  the harness owns the seed.

The eval suite *is* the regression suite. There's no separate "unit
test" tier for the agent loop — the failure modes the loop guards
against are exactly what `evals/toolcall.py` covers.

---

## 11. Interview Talking Points

If asked **"what did you learn?"** — these are the answers worth giving:

1. **Local-first changes which problems are interesting.** With frontier
   models, prompt engineering is the entire game. With a local 7B, the
   game is *system design around* the model: JSON validation, retry
   loops, fact extraction, latency budgets, fallback paths.

2. **"Boring middleware" is a feature.** Ollama, SQLite, MCP — every
   choice I made trends toward unsexy infrastructure with stable
   interfaces. The interesting part of an AI system isn't the AI; it's
   the seams.

3. **Risk-tagging tools was the right safety primitive.** Centralizing
   the gate in the agent loop (not in the tool) means the safety
   property holds even when a tool author forgets. This is a small
   design pattern with outsized payoff.

4. **Replaceable components matter more than perfect ones.** I picked
   `macOS say` over Piper, RMS VAD over Silero, energy threshold over
   ML-based end-of-speech — all things I expect to swap. The interfaces
   make the swap a 1-hour change. Optimize for *changeability*, not for
   getting it right on day one.

5. **Phase ordering is engineering.** Front-loading audio risk meant I'd
   discover the worst problems first, when scope changes are cheap.
   Front-loading the LLM would have been more fun but riskier — I might
   have built half the system on top of an audio pipeline that didn't
   work.

If asked **"what's still wrong with it?"** — be honest:

1. **No public eval suite yet.** All my claims about tool-call accuracy
   are anecdotal until Phase 8 (evaluation + measurement) is done.
2. **Single-user, single-machine.** No story for scale, multi-tenancy,
   or collaboration.
3. **Tool ecosystem is rented, not built.** I'm composing existing MCP
   servers — that's pragmatic but not differentiating on its own.
4. **No novel ML contribution.** No fine-tune, no custom architecture.
   This is systems work, not research work.

---

## 12. Quick Reference: Decisions at a Glance

```
LLM backend ............ Ollama (local HTTP API)
LLM model .............. Qwen 2.5 7B Instruct, Q4_K_M
Wake word .............. openWakeWord, "hey_jarvis_v0.1"
STT .................... faster-whisper, small.en
TTS .................... macOS `say` (v1) → Piper (v2)
Agent .................. Single tool-use loop, ReAct-style
Tool layer ............. MCP servers via stdio
Memory store ........... SQLite + sqlite-vec, local file
Embeddings ............. nomic-embed-text via Ollama
Memory model ........... Episodes (raw) + Facts (extracted)
Retrieval .............. Top-K cosine, K≈5, plus all facts
Safety ................. Risk tags + voice-confirm for write/destructive
Confirmation default ... No (on ambiguous STT)
Runtime ................ Python 3.12, single long-running process
Dep manager ............ uv
Config ................. Env-driven Settings dataclass
Repo layout ............ Code in ~/jarvis, ops data in ~/.jarvis
```
