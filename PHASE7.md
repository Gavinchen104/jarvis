# Phase 7 — Long-Term Memory

> Detailed engineering plan. Memory is the only phase that's
> *cross-cutting* — it touches every turn the assistant takes from this
> point forward. It's also the one most likely to need iteration: the
> design decisions made here (episodes-vs-facts split, retrieval
> strategy, extraction prompt) all play out over weeks of real use, not
> one demo. The right Phase 7 ships a deliberately *simple* baseline plus
> the eval that tells you when to upgrade.

---

## 1. Goal & Acceptance Test

**Goal:** the assistant remembers things across conversations —
preferences, names, ongoing context — and uses them naturally without
being asked to.

**Headline acceptance test (from PLAN.md):**

> Tell it *"I prefer matcha over coffee"* Monday. Next week, ask *"what
> should I order at the café?"* → it remembers.

**Expanded done criteria:**

- [ ] Every turn writes an `episode` row with text + embedding to
      `~/.jarvis/memory.db`
- [ ] A periodic extraction job promotes durable statements to the
      `facts` table (off the critical path)
- [ ] Each turn's system prompt is augmented with all facts + top-K
      relevant episodes
- [ ] `jarvis memory list / forget <id> / export <path> / compact` works
- [ ] Retrieval p50 under 100ms (memory doesn't get to slow turns down)
- [ ] `evals/memory.py` plant-and-recall test passes (matcha-style)
- [ ] Storage size stays sub-100MB after a month of heavy use

---

## 2. Why Memory Is The Trickiest Phase

Earlier phases had clear success criteria — does the tool call work,
does the confirmation gate fire? Memory's failure mode is **silent**:
the model can answer fine *without* remembering, just less well. So
"is it working?" requires deliberate evaluation, not vibe-checking.

The three subtle failure modes:

1. **Retrieval pulls irrelevant context** → the model is distracted by
   stale facts, gets worse.
2. **Extraction is too noisy** → facts table fills with garbage ("the
   user said 'um'") and retrieval surfaces it.
3. **Memory leaks into wrong contexts** → "I prefer matcha" gets
   injected into a tool-use turn that doesn't care, slowing the turn and
   using context space.

These aren't crashes. They're a slow erosion of quality. The eval suite
is what catches them.

---

## 3. Key Design Decisions

### 3.1 Two tables: `episodes` + `facts`

DESIGN.md §5.1 commits to this split. The phase-level rationale:

- **Episodes** are the verbatim record. Append-only. The eventual source
  of truth for "what did we actually say." Used for recency-grounded
  retrieval.
- **Facts** are the curated layer. Extracted on a schedule, deduplicated,
  manually editable via CLI. Small enough to inject *whole* into the
  prompt — no retrieval logic needed.

The split is what makes the system **correctable**: when the assistant
remembers something wrong, the user can `jarvis memory forget <id>` on a
single fact instead of pruning a conversation transcript.

### 3.2 Embedding model: `nomic-embed-text` via Ollama

Locked in DESIGN.md §5.3. No new choice — the runtime is already loaded,
and Ollama's embedding endpoint is consistent with the chat endpoint.

### 3.3 Retrieval timing: at turn start, not per-LLM-round

The retrieved context is injected once, into the system prompt at the
start of each user turn. We don't re-retrieve per tool-use iteration.
Reasons:

- Cheaper (one query, not N).
- Stable context across iterations — the model doesn't see different
  facts on round 1 vs round 3.
- Tool calls don't typically benefit from re-grounding mid-loop.

### 3.4 Extraction cadence: every 10 user turns, in background

Per-turn extraction is wasteful (most turns don't contain durable facts)
and adds latency. Every-10-turns is a batched job that runs **off the
critical path**, in a background thread, after the user finishes a turn.
The user never waits for extraction to finish; the next turn proceeds
even if extraction is mid-run on the prior batch.

---

## 4. File-by-File Build Breakdown

```
src/jarvis/memory/
├── __init__.py
├── schema.sql       # NEW: episodes + facts tables; sqlite-vec virtual tables
├── store.py         # NEW: write_episode, insert_fact (dedup), DB helper
├── retrieve.py      # NEW: top-K episodes + all facts -> MemoryContext
├── extract.py       # NEW: periodic batched fact extraction (background thread)
├── embed.py         # NEW: nomic-embed-text wrapper, LRU-cached
└── manage.py        # NEW: CLI implementations (list/forget/export/compact)

src/jarvis/cli.py             # MODIFY: `jarvis memory ...` subcommands; `jarvis setup memory`
src/jarvis/agent/prompt.py    # MODIFY: build_system_prompt(memory_ctx) prepends memory block
src/jarvis/loops/chat.py      # MODIFY: retrieve at turn start; write episode after turn; trigger extract
evals/memory.py               # NEW: plant-and-recall harness
```

### 4.1 `src/jarvis/memory/schema.sql`

```sql
CREATE TABLE IF NOT EXISTS episodes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,         -- ISO 8601 with tz
    role        TEXT NOT NULL,         -- 'user' | 'assistant'
    content     TEXT NOT NULL,
    turn_id     INTEGER NOT NULL       -- groups a user+assistant pair
);
CREATE INDEX IF NOT EXISTS episodes_ts ON episodes(ts);
CREATE INDEX IF NOT EXISTS episodes_turn ON episodes(turn_id);

-- sqlite-vec virtual table for episode embeddings
CREATE VIRTUAL TABLE IF NOT EXISTS episode_vec USING vec0(
    embedding FLOAT[768]
);

CREATE TABLE IF NOT EXISTS facts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    statement   TEXT NOT NULL,            -- "Gavin prefers matcha over coffee"
    source_eps  TEXT,                     -- JSON list of episode ids that produced it
    created_ts  TEXT NOT NULL,
    last_used   TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS fact_vec USING vec0(
    embedding FLOAT[768]
);
```

A 768-dim embedding (nomic-embed-text default) is 3KB per row
uncompressed; 10k turns ≈ 30MB. The `episode_vec` / `fact_vec` ROWIDs
mirror the source tables' `id`s — single integer to find both rows.

### 4.2 `src/jarvis/memory/embed.py`

```python
from functools import lru_cache
from jarvis.config import settings

@lru_cache(maxsize=1)
def _client():
    from ollama import Client
    return Client(host=settings.ollama_host)

@lru_cache(maxsize=4096)
def embed(text: str) -> tuple[float, ...]:
    """nomic-embed-text via Ollama. LRU-cached on exact text."""
    resp = _client().embeddings(model="nomic-embed-text", prompt=text)
    return tuple(resp["embedding"])
```

LRU because retrieval will frequently re-embed near-identical queries
during testing and active conversation.

### 4.3 `src/jarvis/memory/store.py`

```python
def write_episode(role: str, content: str, turn_id: int) -> int:
    ts = now_iso()
    vec = embed(content)
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO episodes(ts, role, content, turn_id) VALUES (?,?,?,?)",
            (ts, role, content, turn_id),
        )
        eid = cur.lastrowid
        conn.execute(
            "INSERT INTO episode_vec(rowid, embedding) VALUES (?, ?)",
            (eid, _to_blob(vec)),
        )
        conn.commit()
        return eid

def insert_fact(statement: str, source_eps: list[int]) -> int | None:
    """Insert a fact unless a near-duplicate exists (cosine sim > 0.92).

    Returns the new fact id, or None if it was deduped.
    """
    vec = embed(statement)
    if _has_similar_fact(vec, threshold=0.92):
        return None
    ...
```

The 0.92 cosine threshold is the initial guess; tune from the eval. Too
strict → near-duplicates like "Gavin prefers matcha" / "Gavin likes
matcha" both land. Too loose → real distinct facts get collapsed.

### 4.4 `src/jarvis/memory/retrieve.py`

```python
from dataclasses import dataclass

@dataclass
class MemoryContext:
    facts: list[str]              # all of them (cap ~50)
    relevant_episodes: list[str]  # top-K, K=5

def retrieve(query_text: str, *, k: int = 5) -> MemoryContext:
    qvec = embed(query_text)
    facts = _all_facts()                            # cap 50 (cheap)
    eps = _top_k_episodes(qvec, k=k)                # sqlite-vec KNN
    return MemoryContext(
        facts=facts,
        relevant_episodes=[e.content for e in eps],
    )

def to_prompt_block(ctx: MemoryContext) -> str:
    if not (ctx.facts or ctx.relevant_episodes):
        return ""
    lines = ["What you know about Gavin:"]
    lines.extend(f"- {f}" for f in ctx.facts)
    if ctx.relevant_episodes:
        lines.append("\nRelevant prior conversation:")
        lines.extend(f"- {e}" for e in ctx.relevant_episodes)
    return "\n".join(lines)
```

`_top_k_episodes` uses sqlite-vec's `MATCH` syntax for KNN. **No
recency decay in v1** — start with the simplest retrieval; add decay (and
re-run the eval) only if precision drops.

### 4.5 `src/jarvis/memory/extract.py`

Runs in a background thread, triggered every 10 user turns. The
extraction *prompt* is the load-bearing piece:

```python
EXTRACTION_PROMPT = """You will be shown the last several turns of a \
conversation between Gavin and his assistant. Extract any *durable* \
facts about Gavin: preferences, ongoing projects, names of people in his \
life, recurring constraints, things he asked to be remembered.

Rules:
- One fact per line.
- Use third person: "Gavin prefers ...", "Gavin's sister is named ...".
- Skip transient state ("Gavin is asking about the weather today").
- Skip facts that are already obvious from the existing-facts list.
- If nothing durable, output nothing.

Existing facts you already know:
{existing_facts}

Recent conversation:
{recent_turns}

Facts:"""

def extract_now() -> list[str]:
    """Pull last 10 turns, call LLM, validate output, dedupe-insert."""
    recent = _last_n_turns(10)
    existing = _all_facts()
    raw = llm.chat([{"role": "system", "content": EXTRACTION_PROMPT.format(
        existing_facts="\n".join(existing) or "(none yet)",
        recent_turns=_format(recent),
    )}], tools=None)["content"]
    candidates = _split_lines(raw)
    accepted = []
    for line in candidates:
        if not _looks_like_a_fact(line):  # length / "Gavin" anchor / blacklist
            continue
        fid = insert_fact(line, source_eps=[e.id for e in recent])
        if fid is not None:
            accepted.append(line)
    return accepted

def _looks_like_a_fact(line: str) -> bool:
    if len(line) > 200:           # likely model drifted into prose
        return False
    if "gavin" not in line.lower():  # anchor heuristic — facts about Gavin
        return False
    if line.lower().startswith(("the user", "user is")):  # awkward phrasing
        return False
    return True
```

Validation: refuse lines >200 chars (likely drift into prose), refuse
lines without "Gavin" (anchor heuristic), refuse third-person-generic
phrasings ("the user is..."). All of these are tunable from the eval.

### 4.6 `src/jarvis/memory/manage.py`

CLI implementations behind `jarvis memory ...`:

```python
def cmd_list(query: str | None = None) -> None:
    """`jarvis memory list` (all facts) or with --search <query>."""

def cmd_forget(fact_id: int) -> None:
    """`jarvis memory forget 42` — deletes one fact + its vec row."""

def cmd_export(path: Path) -> None:
    """`jarvis memory export ~/jarvis-memory.json` — dump facts as JSON."""

def cmd_compact() -> None:
    """`jarvis memory compact` — VACUUM the DB; rebuild vec indices."""
```

A `cmd_remember` (`jarvis memory remember "<statement>"`) is the manual
escape hatch: when extraction misses something important, the user can
insert a fact directly.

### 4.7 `src/jarvis/agent/prompt.py` (modify)

```python
from jarvis.memory.retrieve import MemoryContext, to_prompt_block

def build_system_prompt(memory_ctx: MemoryContext | None = None) -> str:
    parts = [
        _BASE_PROMPT,
        f"\nCurrent date and time: {now_local():%A, %B %d, %Y at %I:%M %p %Z}.",
    ]
    if memory_ctx:
        block = to_prompt_block(memory_ctx)
        if block:
            parts.append("\n" + block)
    return "\n".join(parts)
```

### 4.8 `src/jarvis/loops/chat.py` (modify)

```python
turn_id = _next_turn_id()
ctx = retrieve(text)              # ~50-100ms
messages = [
    {"role": "system", "content": build_system_prompt(ctx)},
    *recent_history,
    {"role": "user", "content": text},
]
reply = run_tool_loop(messages, registry)

write_episode("user", text, turn_id)
write_episode("assistant", reply, turn_id)

# Off the critical path
maybe_trigger_extraction(turn_id)  # every 10 user turns, background thread
```

`maybe_trigger_extraction` is fire-and-forget onto a background thread
guarded by a single-flight lock — if extraction is still running from
the prior batch, skip and try again at the next 10th turn.

---

## 5. The Plant-and-Recall Eval

`evals/memory.py` — the regression suite for the whole phase.

Structure:

```
Phase A (plant):
  Inject N planted statements into the memory store as if they had been
  uttered in past conversations. Run extraction over those episodes.

Phase B (recall):
  For each planted statement, ask a related (not identical) question.
  Verify (a) retrieval surfaces the relevant fact and (b) the LLM's
  response uses it.
```

Scoring:

- **retrieval recall** — % of recall questions where the planted fact
  appeared in the top-K retrieved context.
- **use rate** — % of recall questions where the LLM's answer reflects
  the fact (substring or semantic match).
- **wrong-context injection** — on the *negative* rows (where memory
  shouldn't fire), did irrelevant facts get retrieved?

Example rows:

```
planted                                          | recall question                    | use signal
-------------------------------------------------|------------------------------------|----------------------------
"Gavin prefers matcha over coffee."              | "what should I order at the café?" | mentions matcha/tea, not coffee
"Gavin's sister is named Sarah."                 | "who's coming for dinner?"         | NEGATIVE: should NOT inject Sarah
"Gavin is allergic to shellfish."                | "is the lobster bisque safe?"      | mentions allergy
"Gavin is rebuilding his Mac next Tuesday."      | "any blockers on the calendar?"    | mentions Mac rebuild
"Gavin uses uv, not pip, for Python."            | "how do I install this package?"   | suggests uv, not pip
"Gavin's daughter is two years old."             | "what's the weather like?"         | NEGATIVE: should NOT inject daughter
```

The *negative* rows are critical. Memory shouldn't dump everything into
every context — irrelevant facts hurt as much as missing relevant ones.
The eval scores precision *and* recall.

Targets:

- Retrieval recall **≥80%** on positive rows
- Use rate **≥70%** on recalled facts
- Wrong-context injection **≤10%** on negative rows

---

## 6. Failure Modes & Mitigations

| Failure | Likelihood | Mitigation | Where |
|---|---|---|---|
| Extraction surfaces trivial facts ("Gavin said hello") | High at first | Anchor heuristic ("Gavin"); reject lines without it; tune prompt by re-reading raw output during eval | §4.5 |
| Extraction misses real facts | Medium | Eval finds it; tune the prompt or lower the cadence; add the manual `jarvis memory remember` trigger as escape | §4.5, §4.6 |
| Retrieval pulls noisy/old episodes | Medium | Start without recency decay; add it (and re-run the eval) if precision drops on the negative rows | §4.4 |
| Dedup threshold too strict | Medium | Lower from 0.92; or canonicalize statements pre-embed (lowercase, trim) | §4.3 |
| Dedup threshold too loose | Low | Inverse — facts duplicate; opposite tune | §4.3 |
| Memory DB grows unbounded | Low | `jarvis memory compact` weekly; deleted facts drop vec rows; PRAGMA `auto_vacuum=INCREMENTAL` | §4.6 |
| Extraction call blocks the loop | Low | Background thread + single-flight lock; `maybe_trigger_extraction` is fire-and-forget | §4.8 |
| Embedding model not pulled | Medium first run | `jarvis setup memory` subcommand pulls it; clear error otherwise | §4.6 |
| sqlite-vec extension not loaded | Medium first run | `db()` helper loads it on connection open; assert it's available with a friendly error | §4.1 |
| Facts contradict ("Gavin prefers matcha"; "Gavin prefers coffee") | Real over months | `jarvis memory list` surfaces both; user resolves manually via `forget`. **Do not** auto-reconcile — that's its own failure mode. | §4.6 |
| Privacy concern about logged content | Low | All in `~/.jarvis`, never synced; `jarvis memory export` for inspection; user can `forget` anything | §4.6 |
| Plant-and-recall passes but live use feels worse | Medium | Add `JARVIS_MEMORY_SHADOW=1`: retrieve and log but don't inject; diagnose offline against real turns | §11 |
| Embedding cache cold on every boot | Low | LRU is in-process; not persisted. Acceptable — embedding cost is ~50–100ms. | §4.2 |
| Extraction crashes mid-batch and corrupts state | Low | All writes in a transaction; partial extraction returns nothing visible until commit | §4.5 |
| User says "forget my address" → must purge content | Medium (privacy UX) | `jarvis memory forget` deletes one fact; `jarvis memory list --search "<term>"` finds candidates; deeper purge → `jarvis memory export` + manual edit + import (a v2 feature) | §4.6 |

---

## 7. System Prompt Changes

The base prompt doesn't change much — `build_system_prompt()` just
prepends the memory block when available. One added rule:

> - You may have a "What you know about Gavin" block above. Use those
>   facts naturally — don't quote them or announce that you remember.
>   If Gavin asks "what do you remember about me," summarize, don't read
>   the block verbatim.

---

## 8. Latency Budget

Memory must not slow turns. Targets:

| Stage | Budget | On critical path? |
|---|---|---|
| `retrieve()` (embed + sqlite-vec top-5) | <100ms | **Yes** — at turn start |
| `write_episode()` (embed + 2 inserts) | <300ms | No — after the reply is spoken |
| `extract_now()` (every 10 turns) | ~3–5s | No — background thread |

`retrieve` is the only one on the critical path. If it exceeds 200ms,
profile: usually it's the embedding call (~50–100ms), not the SQL. If
the embedding call is the bottleneck, raise the LRU cache size or
embed the query in a background thread *before* the user finishes
speaking (Phase 8 polish).

---

## 8.5 Observability

A turn with memory active:

```
[you] what should I order at the café
[memory] facts=12 retrieved_eps=3
[memory] injected: "Gavin prefers matcha over coffee."
[memory] injected: "Gavin is dairy-sensitive when stressed."
[memory] retrieved_eps[0]: "(Mon 2026-05-12) Gavin: I picked up matcha at Blue Bottle..."
[jarvis] If it's a matcha day, the iced matcha latte with oat — otherwise their pour-over.
[timing] stt=380ms agent=1820ms retrieve=87ms eos->audio=2290ms total=4100ms
[memory.write] ep_user=#481 ep_assistant=#482 turn=#241
```

Per-turn keys to look at:

| Key | Healthy range |
|---|---|
| `retrieve` | 50–120ms. If >200ms, profile `embed` separately. |
| `facts` count | should grow slowly (handful per month of use); jumps mean noisy extraction |
| `retrieved_eps` | always K=5; if fewer, the DB is too small to be useful yet |
| `memory.write` ep ids | should always increment; gaps mean a write failed |

A background extraction looks like (in a *different* log line, off the
critical path):

```
[memory.extract] turn=#250 last_n=10  candidates=4  accepted=2  rejected=2
[memory.extract] accepted: "Gavin's daughter is named Maya."
[memory.extract] accepted: "Gavin uses iA Writer for personal notes."
[memory.extract] rejected (too_short): "Yes"
[memory.extract] rejected (no_anchor): "It was raining."
```

The `accepted` lines are the audit trail for "where did this fact come
from" — paired with `source_eps` in the DB row, you can always trace a
remembered statement back to the originating conversation.

### Inspecting the DB by hand

When the eval misbehaves, drop to the SQLite CLI:

```
$ sqlite3 ~/.jarvis/memory.db
sqlite> .load <path-to-vec>/vec0
sqlite> SELECT id, statement FROM facts ORDER BY created_ts DESC LIMIT 20;
sqlite> SELECT id, role, substr(content,1,80) FROM episodes ORDER BY id DESC LIMIT 10;

-- KNN against a query (you'll need the embedding from outside):
sqlite> SELECT e.id, substr(e.content,1,80), v.distance
        FROM episodes e
        JOIN episode_vec v ON v.rowid = e.id
        WHERE v.embedding MATCH :qvec AND k = 5
        ORDER BY v.distance;
```

`jarvis memory list/forget/export` are the production paths; the SQLite
CLI is the *debugging* path when an eval row fails and you want to see
what was actually stored vs. what was retrieved.

---

## 9. Build Order (Checklist)

1. [ ] `ollama pull nomic-embed-text` (one-time)
2. [ ] `memory/schema.sql` + `memory/embed.py` + sqlite-vec loaded via
       `db()` helper
3. [ ] `memory/store.py` — `write_episode()` first; verify rows + vec
       rows show up via `sqlite3` inspection
4. [ ] `memory/retrieve.py` — top-K query against hand-planted episodes
5. [ ] `agent/prompt.py` — `build_system_prompt()` accepts `memory_ctx`
6. [ ] `loops/chat.py` — wire retrieval at turn start + write episode at
       turn end
7. [ ] **Headless smoke:** plant 3 facts via direct insert; ask 3 recall
       questions; verify retrieval surfaces them in `to_prompt_block`
       output
8. [ ] `memory/extract.py` — extraction job; verify it runs in
       background and doesn't block the turn (instrument a sleep in the
       extractor; the next user turn should still complete in normal
       time)
9. [ ] `memory/manage.py` + `cli.py` — list/forget/export/compact/remember
       CLI
10. [ ] `evals/memory.py` — the plant-and-recall harness
11. [ ] **Iterate the extraction prompt against the eval** until §5
        targets are met. Most prompt-tuning happens here.
12. [ ] **User voice test:** Monday "I prefer matcha over coffee" →
        following week, "what should I order at the café?"

Steps 7 and 11 are the iteration loops. Don't merge until 11 hits its
targets.

---

## 10. Scope Discipline — What Phase 7 Does NOT Do

- **No cross-device memory sync.** Single Mac. DESIGN.md §6.3 commits to
  local-only.
- **No memory encryption at rest.** FileVault is the threat model
  (DESIGN.md §6.4).
- **No automatic contradiction resolution.** When two facts conflict,
  the user resolves it via the CLI.
- **No continuous fine-tuning on memory.** Memory is *retrieved*, not
  baked into model weights.
- **No "memory dashboard" GUI.** CLI is enough.
- **No semantic search of arbitrary files.** That's Phase 8
  RAG-over-filesystem.
- **No conversation summarization for context-window compression.** A
  separate (legitimate) feature, but distinct from "remember this
  preference across weeks."

---

## 11. Risks & Escape Hatches

| Risk | Trigger | Escape hatch |
|---|---|---|
| Eval targets unreachable with the simple top-K + facts retrieval | Plant-and-recall plateaus below targets | Add recency decay; if still bad, switch to hybrid retrieval (semantic + keyword BM25 via SQLite FTS5) |
| Extraction prompt produces 20+ facts per batch (noise) | `jarvis memory list` reveals junk after a week | Tighten the rules; consider a second-pass "fact validator" prompt that filters each candidate |
| sqlite-vec extension is hard to ship | Build issues on Mac | Pin a known-good version in `pyproject.toml`; fall back to brute-force numpy cosine over the (small) vector set if it gets really stuck |
| The model self-quotes memory ("As I recall, you said you prefer matcha...") | Conversational annoyance | Add the §7 rule; if persistent, post-process responses to strip exact-substring matches against retrieved facts |
| Memory makes responses worse on a meaningful subset | Pre/post comparison shows regression | `JARVIS_MEMORY_SHADOW=1`: retrieve and log, but don't inject; diagnose offline |
| Privacy ask: user wants everything deleted | "Forget everything" | `jarvis memory export` (save first, in case of regret) → `jarvis memory wipe --force` |

---

## 12. Definition of Done

- [ ] Headline matcha test passes end-to-end
- [ ] `evals/memory.py` clears the §5 targets (recall ≥80%, use ≥70%,
      wrong-context ≤10%)
- [ ] `jarvis memory list / forget / export / compact / remember` all
      work
- [ ] `retrieve()` p50 <100ms, verified by instrumentation
- [ ] DB size <100MB after the eval run
- [ ] PLAN.md Phase 7 marked done with measured retrieval p50 + use rate

The memory phase ends not with "it works once" but with **the eval
framework that tells you when it's working**, because memory's failure
modes are silent. The eval is the deliverable.
