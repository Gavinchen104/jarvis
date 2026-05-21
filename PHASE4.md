# Phase 4 — macOS Control + Voice-Confirmation Flow

> Detailed engineering plan. Phase 4 is where the assistant stops being
> read-only. The confirmation flow is the load-bearing safety primitive for
> the rest of the project — once it's built right on a contained surface
> (AppleScript / filesystem write), Phases 5–6 inherit it for calendar
> writes and outgoing email. Get it wrong here and every later phase
> inherits the same hole.

---

## 1. Goal & Acceptance Test

**Goal:** the assistant can take a real *action* on the Mac — but every
action with `risk_level >= write` flows through a voice-confirmation gate,
and the gate's default on ambiguous input is **no**.

**Headline acceptance test (from PLAN.md):**

> "Hey Jarvis, open Spotify and play Daft Punk" → confirms → plays.

**Expanded done criteria:**

- [ ] AppleScript tool registered, `risk: write`
- [ ] Filesystem MCP wired in, with read paths open and write paths gated
- [ ] Every tool with `risk_level != "read"` routes through `agent/confirmation.py`
- [ ] A "yes" response (`yes|yeah|sure|do it|go ahead|confirm|okay|ok`) proceeds
- [ ] A "no" response (`no|nope|cancel|stop|don't|abort|never mind`) aborts cleanly and the model gets back a `"user cancelled"` tool-result so it can speak the cancellation
- [ ] Ambiguous or empty STT defaults to **no**, prints why, and re-asks at most once
- [ ] No `tool.run(...)` callsite in the codebase bypasses the gate (greppable invariant)
- [ ] Headless eval (`evals/confirm.py`) passes the full decision table

---

## 2. Why This Phase Matters

The risk model for the whole project hinges on the confirmation flow:

- Web search (Phase 3) was `risk: read` — no human in the loop, by design.
- Send email (Phase 6) will be `risk: destructive` — a wrong confirmation
  here ships the wrong email to a real person.

If the confirmation gate is sloppy now, it will be sloppy when the stakes
are real. **Phase 4 is the dress rehearsal where mistakes are reversible.**
AppleScript and filesystem writes are the right surface to debug it on:
blast radius is contained to Gavin's own Mac.

DESIGN.md §6.1 commits to enforcing the gate in the *loop*, not in each
tool — so this phase is also where that property gets implemented for the
first time. Phase 3 already has a stub in `tool_loop.py` that rejects
non-`read` calls (`error: confirmation required (not yet supported)`); this
phase replaces that stub with the real gate.

---

## 3. Key Design Decision: Where the Gate Lives

| Option | How | Pros | Cons |
|---|---|---|---|
| **A. In the tool-use loop** | `tool_loop.py` inspects `risk_level` before `tool.run()`; if `>= write`, calls `confirmation.confirm()` first | One enforcement site; tool authors can't forget; matches DESIGN.md §6.1 | Coupling between the loop and audio |
| B. In each tool | Each `write`/`destructive` tool calls `confirm()` itself | Tools self-contained | Easy to forget — one missed `confirm()` is a footgun |
| C. A wrapping decorator | `@confirms_with(summary_fn)` on each tool | Visible per-tool | Same forgetfulness risk as B |

**Decision: A.** The whole point of the risk-tag primitive is that the
*gate* is the loop's job, not the tool's. The loop already knows
`risk_level`; pushing the check into tools recreates the problem the
registry was built to prevent.

A side benefit: it makes the safety property a 1-grep invariant. *No tool
can run without consent* is true iff `tool.run(...)` is only called inside
`tool_loop.py`, and `tool_loop.py` gates on `risk_level`.

---

## 4. The Confirmation Flow

```python
confirm(summary: str, *, destructive: bool = False) -> bool
```

Concrete sequence:

1. Build a one-line summary from the pending tool call (e.g.
   *"Open Spotify and play Daft Punk"*).
2. For `destructive`, also read the *full payload* aloud (recipient,
   subject, first ~150 chars of body — this is Phase 6's full use; Phase 4
   only needs the hook).
3. `say(summary + ". Confirm?")`.
4. `record_short()` — capped at 3s, with a shorter (600ms) silence window
   than the main loop; we expect "yes"/"no", not a paragraph.
5. STT the buffer.
6. Match against the YES/NO dictionaries (§5.3).
7. If exactly one match → return the match.
8. If both or neither → say *"I didn't catch that — should I proceed?"*,
   re-record once.
9. If second attempt also ambiguous → return **False** and say
   *"Cancelled."*

**Asymmetric default.** Making the user repeat themselves is annoying;
running the wrong action is the disaster. DESIGN.md §6.2 already commits to
this asymmetry — Phase 4 implements it.

---

## 5. File-by-File Build Breakdown

```
src/jarvis/
├── tools/
│   ├── applescript.py    # NEW: run_applescript tool, risk: write
│   ├── filesystem.py     # NEW: register MCP filesystem server, mixed risk
│   └── ...
├── agent/
│   ├── confirmation.py   # NEW: confirm(summary) -> bool
│   ├── summarize.py      # NEW: pending tool call -> human-readable summary
│   └── tool_loop.py      # MODIFY: replace Phase 3 stub with confirm() routing
└── audio/
    └── recorder.py        # MODIFY: record_short() — same algorithm, tighter caps
```

### 5.1 `src/jarvis/tools/applescript.py`

```python
# Phase 4: ad-hoc AppleScript execution, risk: write.

import subprocess
from typing import Any
from jarvis.tools.registry import Registry, Tool

_DESCRIPTION = (
    "Run a short AppleScript on the user's Mac. Use this for opening apps, "
    "controlling Music, setting Do Not Disturb, scripting Reminders, or "
    "typing into the focused window. Do NOT use it for tasks that have a "
    "dedicated tool (web_search, calendar, gmail)."
)
_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "script": {
            "type": "string",
            "description": "A single AppleScript snippet, ideally one tell block.",
        }
    },
    "required": ["script"],
}

def _run(args: dict[str, Any]) -> str:
    res = subprocess.run(
        ["osascript", "-e", args["script"]],
        capture_output=True, text=True, timeout=10,
    )
    if res.returncode != 0:
        return f"[applescript error] {res.stderr.strip() or 'non-zero exit'}"
    return res.stdout.strip() or "(ok)"

def register_applescript(registry: Registry) -> None:
    registry.register(Tool(
        name="run_applescript",
        description=_DESCRIPTION,
        schema=_SCHEMA,
        risk_level="write",
        run=_run,
    ))
```

Choices worth calling out:

- **10s subprocess timeout.** A runaway `tell` shouldn't hang the assistant.
- **`risk: write`, not `destructive`.** The *common* use is reversible
  (open app, set DND). The user can refuse a destructive script at the
  confirmation prompt; we don't need to over-tag.
- **No pre-validation of the script.** Validating AppleScript would mean
  partially reimplementing it. We trust the model + the confirmation gate
  to do their jobs and `osascript` to fail safely on bad input.

### 5.2 `src/jarvis/tools/filesystem.py`

Wraps the upstream MCP `filesystem` server. Register tools with
**different risk levels** so the loop's gate fires per operation:

| Registered name | Underlying MCP tool | Risk |
|---|---|---|
| `fs_read_file` | `read_file` | `read` |
| `fs_list_directory` | `list_directory` | `read` |
| `fs_write_file` | `write_file` | `write` |
| `fs_create_directory` | `create_directory` | `write` |
| `fs_delete` | `delete` (if exposed) | `destructive` |

```python
ALLOWED_ROOTS = ["/Users/gavin/Documents", "/Users/gavin/Desktop", "/tmp"]

def register_filesystem(registry: Registry, client: MCPClient) -> None:
    """Register a curated subset of the upstream fs MCP server's tools.

    The MCP server itself is spawned with --root flags for each ALLOWED_ROOT,
    so the server-side gate is the primary safety. The risk_level mapping
    is the loop-side gate — defense in depth.
    """
```

The upstream server gets `--root` arguments for each `ALLOWED_ROOTS` entry
(server-side enforcement). The risk-level mapping is the loop-side gate.
Defense in depth: a misconfigured server alone wouldn't write anywhere
sensitive, and a missing risk tag wouldn't either.

### 5.3 `src/jarvis/agent/confirmation.py`

```python
from typing import Literal
from jarvis.audio.recorder import record_short
from jarvis.audio.stt import transcribe
from jarvis.audio.tts import speak

YES = {"yes","yeah","yep","sure","do it","go ahead","confirm","okay","ok"}
NO  = {"no","nope","cancel","stop","don't","abort","never mind"}

def _classify(text: str) -> Literal["yes","no","ambiguous"]:
    """Exact-match-or-prefix classifier. Deliberately NOT substring —
    'no problem' must not match 'no'."""
    norm = text.lower().strip().rstrip(".!?")
    if not norm:
        return "ambiguous"
    if norm in YES: return "yes"
    if norm in NO:  return "no"
    for kw in YES:
        if norm.startswith(kw + " "): return "yes"
    for kw in NO:
        if norm.startswith(kw + " "): return "no"
    return "ambiguous"

def confirm(summary: str, *, destructive: bool = False) -> bool:
    """Voice-confirm a pending action. Default on ambiguous: False."""
    speak(summary + ". Confirm?")
    for attempt in (1, 2):
        audio = record_short()
        text = transcribe(audio)
        verdict = _classify(text)
        print(f"[confirm] attempt {attempt}: heard {text!r} -> {verdict}")
        if verdict in ("yes","no"):
            return verdict == "yes"
        if attempt == 1:
            speak("I didn't catch that. Should I proceed?")
    speak("Cancelled.")
    return False
```

The exact-match-or-prefix rule (not substring) is deliberate — "no
problem" must not classify as "no", and "yes I am" must not classify as
"yes I am ready for a long answer."

### 5.4 `src/jarvis/agent/summarize.py`

A small map from tool name → summary template. Keeps the spoken summary
deterministic, concise, and free of LLM-time round-trips.

```python
from typing import Any

def summarize_call(name: str, args: dict[str, Any]) -> str:
    if name == "run_applescript":
        return f"Run an AppleScript that {_describe_script(args['script'])}"
    if name == "fs_write_file":
        return f"Write to {args['path']}"
    if name == "fs_create_directory":
        return f"Create directory {args['path']}"
    if name == "fs_delete":
        return f"Delete {args['path']}"
    return f"Run the {name} tool"

def _describe_script(script: str) -> str:
    """Heuristic: grab the first tell <app> block; fall back to a snippet."""
    ...
```

Phases 5 and 6 extend this with calendar/gmail templates.

### 5.5 `src/jarvis/agent/tool_loop.py` (modify)

Replace the current Phase 3 stub:

```python
# Phase 3 stub
if tool.risk_level != "read":
    messages.append(_tool_msg(name, "error: confirmation required (not yet supported)"))
    continue
```

with:

```python
# Phase 4: real gate
if tool.risk_level != "read":
    summary = summarize_call(name, args)
    ok = confirm(summary, destructive=tool.risk_level == "destructive")
    if not ok:
        messages.append(_tool_msg(name, "user cancelled the action"))
        continue
```

Returning a `"user cancelled"` tool-result message (rather than aborting
the loop entirely) lets the model say *"Got it, cancelled."* in plain
prose instead of silently failing or spiraling into a retry.

### 5.6 `src/jarvis/audio/recorder.py` (modify)

Add a short-utterance variant:

```python
def record_short(max_seconds: int = 3, silence_ms: int = 600):
    """Same algorithm as record_until_silence but tighter caps,
    sized for yes/no confirmations."""
```

Re-use the silence threshold from `calibrate_silence()` (already tuned to
the room at startup).

---

## 6. System Prompt Changes

Append two rules to the `Tools:` section of `agent/prompt.py`:

> - For tasks that modify the user's state (opening apps, writing files,
>   calendar or email actions), call the appropriate tool — Gavin will be
>   asked to confirm before anything happens. If he says no, acknowledge
>   briefly and move on; do not try a different tool unless he asks.
> - Don't bundle multiple destructive actions in a single tool call
>   ("delete X and send Y"). Issue them separately so each gets its own
>   confirmation.

These two lines target the two Phase 4 failure modes the *model* can
cause: (a) acting frustrated at being denied (annoying), (b) sneaking
actions past the confirmation by bundling.

---

## 7. Failure Modes & Mitigations

| Failure | Likelihood | Mitigation | Where |
|---|---|---|---|
| Confirmation STT mishears "no problem" as "no" | Medium | Exact-match-or-prefix dictionary, not substring; "no problem" never matches | §5.3 |
| User says "yeah um actually no" | Low/Medium | Re-ask once on ambiguous; default no on second ambiguous | §5.3 |
| Background noise wakes the confirmation recorder before the user replies | Medium | `record_short` uses the calibrated silence threshold from the main loop; 3s hard cap | §5.6 |
| AppleScript hangs on a modal dialog | Low | 10s subprocess timeout returns `[applescript error]` to the loop | §5.1 |
| Filesystem MCP allows writes outside `ALLOWED_ROOTS` | Low | Server spawned with explicit `--root` args; never `--write-everywhere` | §5.2 |
| Model issues two `write` calls in one tool-loop iteration | Medium | Loop confirms each call individually; user can refuse mid-sequence | §5.5 |
| Model loops trying a different tool after a "no" | Low/Medium | Prompt rule (§6) + the existing iteration cap from Phase 3 | §6 |
| User can't remember the confirmation words | Low | Generous synonym set in YES/NO (§5.3); includes "okay", "go ahead", "abort" | §5.3 |
| A tool author adds a new write/destructive tool but forgets `risk_level` | Low | `Tool` dataclass requires `risk_level`; default-less | registry.py |
| Some callsite calls `tool.run()` directly, bypassing the loop | Low | Grep for `\.run(` in `src/jarvis/tools/` and `src/jarvis/agent/` as part of the §13 done check | §13 |
| User changes mind after "yes" but before action completes | Medium (UX) | v1 does not support cancel-mid-action; surface this in the summary ("This is your last chance.") for destructive | §5.3 |
| Confirmation summary is so long the user stops listening | Medium (after weeks of use) | `summarize_call` keeps summaries ≤1 sentence for write; destructive read of payload is unavoidable | §5.4 |

---

## 8. Latency Budget (Confirmation Path)

The confirmation flow adds a serialized round trip *on top of* the normal
tool-turn budget. Keep it tight.

| Stage | Budget |
|---|---|
| TTS "Confirm?" | ~0.4s |
| Silence window before the user speaks | ~0.5s typical |
| Recording the yes/no utterance | ~1.0s |
| STT (short utterance, Whisper warm) | ~0.2s |
| Match + branch | <10ms |
| **eos → action (confirmation overhead)** | **~2.0s on top of the normal tool turn** |

A confirmed `write` turn lands around 7s total. Slow but acceptable for an
action that requires informed consent. The `read` path keeps the common
case at the Phase 3 ~5s — confirmation is opt-in to risk, not a tax on
every turn.

---

## 8.5 Observability

A confirmed `write` turn should look like this in the log:

```
[wake #12 @ 14:32:08] listening...
[you] open Spotify and play Daft Punk
[tool] -> run_applescript({"script": "tell application \"Spotify\" to play..."})
[confirm] summary: "Run an AppleScript that tells Spotify to play"
[confirm] attempt 1: heard 'yes' -> yes
[tool] <- run_applescript: '(ok)'
[jarvis] Done.
[timing] stt=380ms agent=2010ms confirm=1840ms eos->audio=4230ms total=5700ms
```

The `[confirm]` lines are the audit trail for the safety property. They
are deliberately verbose: `summary` shows what was spoken aloud,
`attempt N` shows what was heard *and* how it classified. A grep across
`~/.jarvis/logs/` for `\[confirm\]` reconstructs every prompt the user
ever saw.

What is **not** logged: the AppleScript body beyond what the summary
includes, file contents from `fs_write_file`, anything from
`destructive` payload reads. Phase 4's confirmation prompt is the place
that *speaks* sensitive content aloud — re-logging it would defeat the
"audio is never persisted" posture.

---

## 9. Evaluation: Confirmation Decision Table

Build `evals/confirm.py` — **no audio**, just feeds simulated transcripts
into `_classify` and verifies the branch.

```
transcript                       | expected verdict   | note
---------------------------------|--------------------|------------------------
"yes"                            | yes                |
"yeah do it"                     | yes                | prefix match
"go ahead"                       | yes                |
"okay"                           | yes                |
""                               | ambiguous          | default cascades to no
"hmm"                            | ambiguous          |
"no thanks"                      | no                 | prefix match
"actually never mind"            | ambiguous*         | starts with "actually", not in dict
"sure but only the first one"    | yes                | caveat is the model's problem
"no problem"                     | ambiguous          | MUST NOT be "no" — that would block agreement
"yes no wait"                    | yes                | first-token wins; suboptimal but predictable
"don't do that"                  | no                 | prefix match
```

The "no problem" row is the regression test — that mistake would block a
user who said "no problem" while *agreeing* to something. The "actually
never mind" row is the under-classification regression test — should
ideally be "no" but the dictionary doesn't catch it; document the gap and
add "actually" / "never mind" if it bites in practice.

Target: **100% on the table.** This isn't an ML metric — it's a
deterministic decision and should be exact.

The eval also covers the orchestrator (`confirm()`) by mocking
`record_short`/`transcribe` to return a scripted sequence:

```
sequence              | final return | note
----------------------|--------------|------------------------
["yes"]               | True         |
["no"]                | False        |
["", "yes"]           | True         | re-ask succeeds
["hmm", "no"]         | False        | re-ask succeeds
["hmm", "hmm"]        | False        | two ambiguous → default no
```

---

## 10. Build Order (Checklist)

1. [ ] `audio/recorder.py` — add `record_short()` variant.
2. [ ] `agent/confirmation.py` — implement `confirm()` and `_classify()`.
3. [ ] `evals/confirm.py` — table-driven test for `_classify()` + scripted
       sequence test for `confirm()`. Pass before going further.
4. [ ] `agent/summarize.py` — minimal templates for `run_applescript` +
       filesystem ops.
5. [ ] `tools/applescript.py` — register with `risk: write`.
6. [ ] `agent/tool_loop.py` — replace the Phase 3 stub with `confirm()`
       routing; preserve the `"user cancelled"` tool-result on no.
7. [ ] Headless smoke: drive the loop with a fake LLM that always emits
       `run_applescript("display dialog \"hi\"")`. Confirm it gates
       correctly on simulated yes/no via the eval harness.
8. [ ] `tools/filesystem.py` — register the MCP filesystem server tools
       with the right per-op risk levels and `ALLOWED_ROOTS`.
9. [ ] `agent/prompt.py` — append the §6 rules.
10. [ ] **User voice test:** `uv run jarvis run` → "open Spotify and play
        Daft Punk" → "yes" → music plays. Repeat with "no" → "Cancelled."

Step 6 is the load-bearing change — every later phase assumes the loop
enforces the gate. Make it the single commit, not part of a larger one.

---

## 11. Scope Discipline — What Phase 4 Does NOT Do

- **No undo.** AppleScript runs are fire-and-forget; the assistant doesn't
  track *what* it did so it can't roll back. An undo log is Phase 8 polish.
- **No "always allow" / trust caching.** Every confirmation is independent.
  Building "remember this for the next 30s" risks silently approving things
  the user assumed required consent; deferred until there's evidence the
  per-call cost is intolerable.
- **No GUI confirmation fallback.** Voice-only, even though a macOS
  notification with yes/no buttons would be more reliable. Inverting that
  is a v2 product call.
- **No multi-step planning.** One tool call per loop iteration still. The
  "open Spotify *and* play Daft Punk" headline test is a single
  AppleScript that does both — not two confirmed tool calls.
- **No partial-action commits.** If a tool emits multiple side effects
  internally, we don't track those.

---

## 12. Risks & Escape Hatches

| Risk | Trigger | Escape hatch |
|---|---|---|
| Confirmation feels intrusive ("everything asks me") | After a week of use, Gavin starts ignoring the prompt | Add a 30-second "remember this" trust window for *repeat-of-identical-call* (with explicit voice opt-in: "always do that for the next minute?"). Do NOT default to it. |
| Ambiguity rate too high | >10% of confirmations re-ask | Tune the YES/NO sets first; then consider a Silero VAD swap for the short utterance to cut spurious mishears |
| AppleScript surface too sharp | Real footgun encountered (e.g. script deleted something unexpected) | Promote AppleScript to `destructive` (always read the script aloud) until a built-in helper for the specific verb ships |
| Filesystem writes to wrong path | MCP server misconfigured | Add a fail-safe in `_run`: refuse paths that don't `startswith` an `ALLOWED_ROOTS` entry, **in addition to** the MCP server's own gate |
| `confirm()` blocks the wake loop for too long | User starts a sentence, walks away | 3s hard cap on `record_short`; on empty → ambiguous → default no |
| Two write tools in one iteration confuses the user (two confirms in a row) | Real if model bundles | Loop already handles it: confirms one, executes or cancels, then the next. Make sure the second `confirm()` re-says the summary in full (no "and also") |

---

## 13. Definition of Done

Phase 4 is done when **all** of these hold:

- [ ] "open Spotify and play Daft Punk" works end-to-end with a "yes"
      confirmation; the same query with "no" cancels and JARVIS says so
- [ ] `evals/confirm.py` passes the decision table (100%) and the scripted
      sequence test
- [ ] `grep -R 'tool\.run' src/jarvis/` shows no callsite outside
      `tool_loop.py`
- [ ] No `write`/`destructive` tool can execute without confirmation —
      verified by inspecting `tool_loop.py` and the grep above
- [ ] PLAN.md Phase 4 marked done with the measured confirmation-turn p50
      latency recorded
