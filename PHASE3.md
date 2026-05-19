# Phase 3 — First MCP Tool: Web Search

> Detailed engineering plan. Phase 3 is the **load-bearing risk** of the whole
> project: it's where the core architectural bet — *a local 7B model can do
> tool use reliably enough to be useful* — gets tested with real data. If this
> works, Phases 4–6 are variations on the same machinery. If it doesn't, the
> escape hatch (hybrid: Claude API for the agent loop) gets pulled.

---

## 1. Goal & Acceptance Test

**Goal:** the model can decide to call a tool, call it correctly, read the
result, and answer from it — instead of hallucinating.

**Headline acceptance test (from PLAN.md):**

> "Hey Jarvis, what's the weather in San Francisco" → a real, current answer.

This is the exact query that failed in Phase 2 (the model made up a forecast
because it had no data). Phase 3's definition of done is: **that same query
now returns the truth.** The before/after is the project's best demo.

**Expanded done criteria** (not just the one happy-path query):

- [ ] Queries that *need* current data trigger a search (weather, news, prices, "who won…")
- [ ] Queries that *don't* (capital of France, arithmetic, general knowledge) do **not** trigger a search — no over-calling
- [ ] A malformed tool call is recovered via retry-with-error, not a crash
- [ ] MCP server down / search API failure degrades to a spoken apology, not a stack trace
- [ ] The tool-use loop cannot spin forever (hard iteration cap)
- [ ] `[timing]` line reports per-stage latency for tool-augmented turns
- [ ] A tool-call eval set exists and passes at the target accuracy (see §10)

---

## 2. Why This Phase Is The Risk

DESIGN.md §2.2 chose Qwen 2.5 7B *specifically* because it's best-in-class
for tool use at the 7B tier. Phase 3 is the bill coming due on that decision.

Local 7B models fail at tool use in characteristic ways:

1. **Under-calling** — they answer from parametric memory instead of calling
   the tool (the Phase 2 weather hallucination, but now with a tool available).
2. **Over-calling** — they search for things they already know ("capital of
   France"), adding latency and flakiness.
3. **Malformed arguments** — right tool, broken JSON, or missing required
   fields.
4. **Result-ignoring** — they call the tool, get the answer, then ignore it
   and answer from training anyway.

The loop must be **defensive about all four**. A naive "pass tools, hope for
the best" loop with a 7B is ~60–80% reliable. The techniques in §6 are what
push it higher. **Building that robustness layer is the actual engineering
content of this phase** — and the strongest interview story in the project.

---

## 3. Key Design Decision: Native Tool-Calling vs Prompt-Based JSON

PLAN.md's original sketch said "model emits tool call (JSON in response) →
parse it." That describes the **prompt-based** approach. There's a better one
available now, and choosing correctly here is itself an interview-grade
decision.

| Approach | How | Pros | Cons |
|---|---|---|---|
| **A. Native Ollama tool-calling** | Pass `tools=[…]` to `client.chat()`; Ollama + Qwen 2.5 return a structured `message.tool_calls` array | Model was *trained* for this exact format; far fewer parse failures; no regex-ing JSON out of prose | Slightly less control over the protocol; tied to Ollama's tool API |
| **B. Prompt-based JSON extraction** | Instruct the model in the system prompt to emit a JSON blob; parse it out of free text | Model-agnostic; total control | Fragile (models wrap JSON in prose, add markdown fences); reinvents what the model was trained to do |

**Decision: A (native), with B's defensive validation kept as a safety net.**

Rationale: Qwen 2.5 is a function-calling-trained model and Ollama ≥0.3
exposes structured tool calls. Fighting that with prompt-hacking would be
choosing the fragile path on purpose. But "native" does **not** mean
"trusted" — a 7B still emits wrong/missing arguments. So we keep:

- JSON-schema validation of every tool call's arguments
- Retry-once-with-error feedback on validation failure
- Hard iteration cap

This is the nuanced answer to *"did you use native tool calling or parse it
yourself?"* — "Native, because the model was trained for it and parsing prose
is the fragile path; but I kept schema validation and a retry loop on top
because a 7B's arguments still can't be trusted."

---

## 4. Web Search MCP Server Selection

This is the first time data leaves the machine. PLAN.md §6 already documents
this boundary: *search queries → search-provider API; treat as semi-public.*
The privacy posture is "local-first," not "never touches the network" — this
crossing is deliberate and disclosed, not a regression.

| Server | API key? | Output quality for LLMs | Notes |
|---|---|---|---|
| **Tavily MCP** | Yes (free 1000/mo) | **Best** — returns a synthesized answer + sources, designed for LLM consumption | Recommended primary. Cleanest path to the weather acceptance test. |
| **Brave Search MCP** | Yes (free 2000/mo) | Good — web result snippets, model must synthesize | Solid alternative; more raw, more general. |
| **DuckDuckGo MCP** (community) | **No key** | Mixed — instant-answers are thin, full search is scrape-fragile | Zero-friction fallback for "just get the loop working today" before signing up for a key. |

**Recommendation:** start on **DuckDuckGo (no key)** to build and debug the
tool-use loop without an external dependency, then switch to **Tavily** for
quality once the loop is proven. The MCP client makes this a config change,
not a rewrite — consistent with DESIGN.md's "replaceable components"
principle.

**API key handling (when moving to Tavily/Brave):**
- Key goes in `.env` (already gitignored), read via the `config.py`
  env-driven `Settings` pattern (e.g. `JARVIS_TAVILY_API_KEY`).
- Add the var name (not the value) to `.env.example`.
- Never commit the key. The repo/ops-data split (DESIGN.md §7.3) already
  guards this.

---

## 5. File-by-File Build Breakdown

```
src/jarvis/
├── tools/
│   ├── __init__.py
│   ├── mcp_client.py      # NEW: spawn MCP server subprocess, list tools, call tools
│   └── registry.py        # NEW: central tool registry; name/desc/schema/risk_level
├── agent/
│   ├── llm.py             # MODIFY: add tools= param + return tool_calls
│   ├── prompt.py          # MODIFY: add "use the search tool for current info" rules
│   └── tool_loop.py       # NEW: the defensive tool-use loop
└── loops/
    └── chat.py            # MODIFY: route through tool_loop instead of bare chat()
```

Plus: `mcp` Python SDK added to `pyproject.toml` deps; `uv sync`.

### 5.1 `tools/mcp_client.py`

Owns the MCP server lifecycle.

- Spawn the chosen MCP server as a subprocess over **stdio transport**
  (per DESIGN.md §3.3 process model).
- `list_tools()` → fetch the server's tool list (name, description, JSON
  input schema).
- `call_tool(name, arguments)` → invoke, return the result content.
- Translate MCP tool schemas → the format Ollama's `tools=` param expects.
- Clean shutdown (terminate subprocess on exit / Ctrl-C).

### 5.2 `tools/registry.py`

Central registry. Each tool entry declares:

```
name        : str   # what the model calls
description : str    # what the model sees (prompt-engineered — this drives calling accuracy)
schema      : dict   # JSON schema for arguments (used for validation)
risk_level  : "read" | "write" | "destructive"
```

Web search registers as **`risk: read`** → executes silently, no
confirmation. This is the phase where the registry + risk field are
introduced, deliberately on a safe (read-only) tool, so the *confirmation*
machinery (Phase 4) is built later on top of a registry that already exists.
The `description` field is not boilerplate — it's the single biggest lever on
whether the model calls the tool at the right times. Treat it as prompt
engineering.

### 5.3 `agent/llm.py` (modify)

- Add optional `tools: list[dict] | None` param to `chat()`.
- When passed, forward to `client.chat(model=…, messages=…, tools=tools)`.
- Return the full message object (not just `.content`) so the caller can see
  `tool_calls`.
- Keep the existing daemon-down RuntimeError.

### 5.4 `agent/tool_loop.py` (the heart of the phase)

See §6 for the algorithm. This is the module that earns the resume bullet.

### 5.5 `loops/chat.py` (modify)

Replace the single `chat(messages)` call with `run_tool_loop(messages,
registry)`. Keep all the existing `[timing]` instrumentation; extend it with
per-stage tool timing (§9).

---

## 6. The Tool-Use Loop Algorithm

```
run_tool_loop(messages, registry):
    tools = registry.as_ollama_schemas()
    for iteration in range(MAX_TOOL_ITERS):          # hard cap, e.g. 5
        msg = llm.chat(messages, tools=tools)

        if not msg.tool_calls:                        # plain text → done
            return msg.content

        messages.append(msg)                          # record the assistant turn

        for call in msg.tool_calls:
            name, args = call.function.name, call.function.arguments

            # --- defensive layer (the engineering content) ---
            tool = registry.get(name)
            if tool is None:
                messages.append(tool_error(call, f"no such tool: {name}"))
                continue

            ok, err = validate_against_schema(args, tool.schema)
            if not ok:
                # retry-with-error: feed the validation failure back so the
                # model can self-correct on the next iteration
                messages.append(tool_error(call, f"invalid arguments: {err}"))
                continue

            try:
                result = mcp_client.call_tool(name, args)
            except Exception as e:
                messages.append(tool_error(call, f"tool failed: {e}"))
                continue

            messages.append(tool_result(call, result))

    # ran out of iterations without a text answer
    return "I wasn't able to complete that — the tool calls didn't resolve."
```

**Why each guard exists** (maps to the four failure modes in §2):

| Guard | Defends against |
|---|---|
| `MAX_TOOL_ITERS` cap | Infinite loop / result-ignoring spin |
| Unknown-tool check | Model hallucinating a tool that doesn't exist |
| Schema validation | Malformed / missing arguments |
| Retry-with-error message | Recovering a bad call instead of crashing (single biggest reliability win) |
| try/except around `call_tool` | MCP server crash, API timeout, rate limit |
| Final fallback string | Graceful spoken apology instead of a stack trace |

Under-calling and over-calling aren't fixed here — they're fixed by the
**system prompt** (§7) and the tool **description** (§5.2). The loop handles
*mechanical* robustness; the prompt handles *decision* quality.

---

## 7. System Prompt Changes

The Phase 2 prompt told the model to be brief and speak plainly. Phase 3 must
add tool-use policy. Add rules like:

- "You have a `web_search` tool. Use it for anything you cannot know from
  training: current weather, news, prices, sports results, recent events,
  or anything time-sensitive. Do not guess at these."
- "Do **not** search for things you already know (basic facts, arithmetic,
  definitions). Searching is slower — only use it when it actually helps."
- "After a search, answer from the results. If the results don't contain the
  answer, say so briefly — don't fall back to guessing."

These three lines target under-calling (rule 1), over-calling (rule 2), and
result-ignoring (rule 3) respectively. They are the *decision-quality* half
of reliability; the loop in §6 is the *mechanical* half.

---

## 8. Failure Modes & Mitigations (Summary)

| Failure | Likelihood (local 7B) | Mitigation | Where |
|---|---|---|---|
| Under-calls (hallucinates instead of searching) | Medium | System prompt rule 1; sharp tool description | §7, §5.2 |
| Over-calls (searches known facts) | Medium | System prompt rule 2; eval set catches regressions | §7, §10 |
| Malformed arguments | Medium-high | Schema validation + retry-with-error | §6 |
| Hallucinated tool name | Low-medium | Unknown-tool guard + retry | §6 |
| Ignores tool result | Medium | System prompt rule 3; eval set scores answer-uses-result | §7, §10 |
| MCP server crash | Low | try/except → spoken apology | §6 |
| Search API rate-limited / down | Low-medium | Same; consider 1 retry w/ backoff | §6 |
| Infinite tool loop | Low | `MAX_TOOL_ITERS` cap | §6 |

---

## 9. Latency Budget (Tool-Augmented Turn)

Phase 2 warm latency was ~0.6–1.1s for a no-tool answer. A tool turn is
**multi-round** and will be noticeably slower. Instrument each stage so the
`[timing]` line tells the truth instead of guessing.

| Stage | Rough budget |
|---|---|
| STT | ~0.5s |
| LLM round 1 (decide to call + emit tool call) | ~1.0s |
| Web search MCP round-trip | ~1.0–2.0s (network — the variable cost) |
| LLM round 2 (synthesize answer from result) | ~1.0–1.5s |
| TTS first audio | ~0.3s |
| **eos → audio (tool turn)** | **~4–5s realistic** |

This number is a headline Phase 3 metric. ~4–5s for a spoken web answer is
acceptable; if it's much worse, the data points to where (slow search
provider? second LLM round too long?). Don't optimize before measuring.

---

## 10. Evaluation: The Tool-Call Golden Set

**This is the resume-grade deliverable of Phase 3.** Most portfolio projects
stop at "it worked once." The differentiator is a repeatable eval.

Build a small labeled set (~30–50 utterances is enough to start) of the form:

```
utterance                                  | expected behavior
-------------------------------------------|----------------------------
"what's the weather in San Francisco"      | SEARCH  (current data)
"who won the game last night"              | SEARCH  (recent event)
"what's the price of bitcoin"              | SEARCH  (live data)
"what's the capital of France"             | NO SEARCH (parametric)
"what's seven times eight"                 | NO SEARCH (arithmetic)
"what does HTTP stand for"                 | NO SEARCH (static fact)
"summarize the latest news on <topic>"     | SEARCH
...
```

Run them through `run_tool_loop` (with the MCP server live) and score:

1. **Tool-call accuracy** — did SEARCH cases search, and NO-SEARCH cases not?
   This is the headline number. Target: **≥90%**.
2. **Per-class breakdown** — precision on "should search" vs "shouldn't".
   Over-calling and under-calling have different fixes (§7).
3. **Answer-uses-result** — on SEARCH cases, did the final answer actually
   reflect the search result (not ignore it)? Manual or LLM-judged.
4. **Latency** — p50 / p95 of eos→audio for tool turns.

Make it a script (`evals/toolcall.py` or similar) that prints a scorecard.
Re-run it after every system-prompt or tool-description change — it's the
regression suite. **This script is what turns "I built a tool-using
assistant" into "I built one and measured 92% tool-call accuracy at 4.3s
p50, with a reproducible eval harness."** That sentence is the Phase 3 resume
bullet.

---

## 11. Build Order (Checklist)

Front-load the risky/uncertain parts; defer polish.

1. [ ] Add `mcp` SDK to `pyproject.toml`; `uv sync`
2. [ ] `tools/mcp_client.py` — get a **DuckDuckGo** (no-key) MCP server
       spawning and `list_tools()` returning something. *Highest-uncertainty
       step — do it first.*
3. [ ] Manual smoke: call the search tool directly from a Python REPL, no LLM.
       Confirms the MCP plumbing independent of model behavior.
4. [ ] `tools/registry.py` — register web search, `risk: read`.
5. [ ] `agent/llm.py` — add `tools=` passthrough, return full message.
6. [ ] `agent/tool_loop.py` — implement §6 with all guards.
7. [ ] Headless test: drive `run_tool_loop` with text input (no mic) on the
       weather query. Iterate the system prompt + tool description until it
       calls correctly. *Most prompt-tuning happens here.*
8. [ ] `agent/prompt.py` — finalize the §7 rules.
9. [ ] `loops/chat.py` — route through the tool loop; extend `[timing]`.
10. [ ] `evals/toolcall.py` — the §10 golden set + scorecard.
11. [ ] Run the eval; record the scorecard in PLAN.md / a results file.
12. [ ] (Optional) Switch DuckDuckGo → Tavily for answer quality; re-run eval.
13. [ ] **User voice test:** `uv run jarvis run` → "Hey Jarvis, what's the
        weather in San Francisco" → real spoken answer.

Steps 2 and 7 are the load-bearing ones. If step 2 (MCP plumbing) fights
back, that's a contained problem. If step 7 (model won't call the tool
reliably) fights back, that's the architectural-bet risk — escalate to §13.

---

## 12. Scope Discipline — What Phase 3 Does NOT Do

Resisting scope creep is itself an engineering decision (DESIGN.md §3.3):

- **No confirmation flow.** Web search is `risk: read`. The voice-confirm
  machinery is Phase 4, built deliberately on a *write*-risk tool. Building
  it now would be premature.
- **No multiple tools.** One tool (web search). Multi-tool selection
  pressure-tests the model differently — that emerges naturally in Phases 5–6.
- **No parallel tool calls.** Sequential only. Parallelism is complexity
  without evidence it's needed yet.
- **No streaming.** TTS still waits for the full response (PLAN.md out-of-scope).
- **No caching of search results.** Premature optimization; measure first.

---

## 13. Risks & Escape Hatches

| Risk | Trigger | Escape hatch |
|---|---|---|
| Qwen 2.5 7B tool-use unreliable | Eval tool-call accuracy stuck <~85% after prompt tuning | (a) Try **Hermes-2-Pro-Llama-3 8B** (tool-tuned) — one env-var swap, DESIGN.md §2.2. (b) If still bad: **hybrid mode** — Claude API for the agent loop only, keep audio/memory local. This is the documented architectural fallback (DESIGN.md §2.1). |
| MCP ecosystem friction | stdio transport / subprocess pain | Direct Python tool as a temporary bridge to unblock, but treat as debt — MCP is the strategic bet (DESIGN.md §3.4). |
| Search provider quality poor | Answers wrong despite correct tool calls | Switch provider (DDG → Tavily → Brave); the eval set quantifies the difference objectively. |
| Latency unacceptable (>~7s) | `[timing]` p95 too high | Profile per-stage; likely the second LLM round — shorten with a tighter "synthesize" prompt before reaching for bigger guns. |

**Decision rule:** if after honest prompt + description tuning the eval
tool-call accuracy can't clear ~85%, that is *evidence* (DESIGN.md's standard)
— pull the Hermes swap first, then hybrid mode. Don't grind indefinitely on a
7B that the data says won't do it.

---

## 14. Definition of Done

Phase 3 is done when **all** of these hold:

- [ ] "Hey Jarvis, what's the weather in San Francisco" → correct spoken answer
- [ ] Expanded done criteria (§1) all pass
- [ ] Tool-call eval scorecard exists and clears the §10 target (≥90% accuracy)
- [ ] `[timing]` reports realistic per-stage tool-turn latency
- [ ] Failure modes (§8) verified to degrade gracefully (kill the MCP server
      mid-run; confirm a spoken apology, not a crash)
- [ ] PLAN.md Phase 3 marked done with the measured numbers recorded

The numbers — tool-call accuracy %, tool-turn p50 latency — are the point.
"It answered the weather once" is the demo; the scorecard is the credential.
