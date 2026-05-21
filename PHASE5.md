# Phase 5 — Calendar + Reminders

> Detailed engineering plan. Phase 5 is the first phase that depends on a
> third-party identity (Google OAuth) and on *time grounding* — turning
> "tomorrow at 3" into an ISO datetime correctly enough to put on the
> user's real calendar. Both are bugs-hide-here territory. Phase 5 is
> small in surface area but tall in foot-gun potential; the answer is to
> debug time grounding *before* you touch any tool that consumes it.

---

## 1. Goal & Acceptance Tests

**Goal:** Gavin can ask about his schedule and create events / reminders
by voice.

**Headline acceptance tests (from PLAN.md):**

> "What's on my calendar tomorrow?" → spoken summary.
> "Add a reminder to email Sarah at 3pm" → creates reminder (confirmed).

**Expanded done criteria:**

- [ ] Google Calendar MCP server connects with OAuth tokens persisted to
      `~/.jarvis/oauth/google.json`
- [ ] Read tools (list events, search) are `risk: read` — no confirmation
- [ ] Create/update events are `risk: write` — voice-confirmed via the
      Phase 4 flow
- [ ] Delete event is `risk: destructive` — the confirmation summary reads
      the event title + start time aloud
- [ ] Reminders (Apple) tool created via a dedicated AppleScript helper
      (not the generic `run_applescript`)
- [ ] Date/time grounding: "tomorrow at 3pm" → correct local ISO datetime,
      with timezone awareness
- [ ] Token refresh is automatic; an expired token does not force the user
      through the browser flow on every restart
- [ ] OAuth tokens stored with `0600` permissions
- [ ] `evals/time.py` passes 100% of the resolution table

---

## 2. Why This Phase Matters

Phase 5 has the smallest *new* engineering content per phase, but it
introduces three concerns that recur in Phase 6:

1. **OAuth on a voice-only device.** No browser pop-up flow in the wake
   loop — auth happens once via `jarvis auth gcal` in the CLI, tokens
   persist to disk, refresh happens silently. This becomes the template
   for Gmail.
2. **Time grounding.** Voice → datetime is the single biggest accuracy
   risk for a local model. PLAN.md Phase 3 already showed Qwen 2.5 is
   *conservative* (it under-calls); that habit *hurts* here when "today"
   / "tomorrow" need a confident concrete commitment.
3. **Apple Reminders via AppleScript.** Sets the pattern for "no
   third-party API needed, just a local OS verb."

If Phase 5 lands clean, Phase 6 is mostly *the same thing again but with
email semantics.*

---

## 3. Key Design Decisions

### 3.1 Google Calendar via MCP, not direct API

| Option | Pros | Cons |
|---|---|---|
| **MCP Calendar server** | Inherits Phase 3 plumbing; one less SDK to manage; risk-tag mapping in our registry; replaceable | Subprocess overhead; depends on someone else's server |
| Direct `google-api-python-client` | Total control over scopes, pagination, retry | Reimplements OAuth, scopes, retry, batching |

**Choice: MCP.** The MCP plumbing already works (Phase 3). Adding a
second MCP server *is the point* — it pressure-tests the registry as a
multi-tool surface and proves the pattern generalizes. Escape hatch: if
the chosen server is buggy or unmaintained, fork it into the repo (small
codebase) before reaching for the direct SDK.

### 3.2 Reminders via dedicated AppleScript, not the generic tool

There is no canonical Reminders MCP server. AppleScript ships in macOS,
needs no OAuth, and is already trusted (Phase 4). But routing through
`run_applescript` would let the model emit raw AppleScript when a
constrained schema would be safer. Decision: a *dedicated* `reminders_*`
tool with a tight schema (`title`, `due`, `notes`), generating the
AppleScript internally.

### 3.3 Time grounding in code, not in the prompt

| Option | Pros | Cons |
|---|---|---|
| **Hybrid: prompt-injected "now" + server-side parser** | Robust; model has the anchor without needing to do date arithmetic | Two places to keep correct |
| Model resolves dates from training | Simple | Wrong, often — local 7Bs guess years confidently |
| Tool-side parsing only | Simple to ship | Model can still send "next Tuesday" and we'd reject it without context |

**Choice: hybrid.** Inject
`Current date and time: 2026-05-19 14:32 PDT (America/Los_Angeles)` into
the system prompt at every turn (cheap, ~30 tokens). Tools that take
datetimes accept *either* a plain ISO string *or* a relative phrase, with
`parsedatetime` doing the conversion server-side. The prompt anchor lets
the model emit *either* `"2026-05-20T15:00"` *or* `"tomorrow at 3pm"`
without needing to know which is preferred — both work.

---

## 4. File-by-File Build Breakdown

```
src/jarvis/
├── tools/
│   ├── calendar.py         # NEW: GCal MCP server, register list/create/update/delete
│   ├── reminders.py        # NEW: AppleScript-backed reminders tool
│   └── time_grounding.py   # NEW: relative-phrase -> ISO datetime
├── agent/
│   ├── prompt.py           # MODIFY: build_system_prompt() prepends time anchor
│   ├── summarize.py        # EXTEND: calendar/reminder templates for Phase 4 confirms
│   └── confirmation.py     # USED AS-IS from Phase 4
├── cli.py                  # MODIFY: add `jarvis auth gcal`, `jarvis auth status`
└── oauth/                   # NEW package
    ├── __init__.py
    └── google.py           # token load/refresh/save, 0600 perms
```

### 4.1 `src/jarvis/tools/calendar.py`

Register four tools, each with its own risk level. We expose *our* names
(`calendar_list`, `calendar_create_event`, …) — the MCP server's native
names are mapped inside each `_run` closure. This keeps the model-facing
surface stable across server swaps.

```python
def register_calendar(registry: Registry, client: MCPClient) -> None:
    registry.register(Tool(
        name="calendar_list",
        description=(
            "List the user's Google Calendar events in a date range. "
            "Use this whenever the user asks what's on their schedule, what's "
            "next, or about a specific day's events. Returns one line per event."
        ),
        schema={
            "type": "object",
            "properties": {
                "start": {"type": "string",
                          "description": "ISO datetime or relative phrase ('today', 'tomorrow', 'this week')"},
                "end":   {"type": "string",
                          "description": "ISO datetime or relative phrase ('tomorrow', 'end of week')"},
            },
            "required": ["start", "end"],
        },
        risk_level="read",
        run=_run_list(client),
    ))
    registry.register(Tool(
        name="calendar_create_event",
        description=...,
        schema={
            "type": "object",
            "properties": {
                "title":       {"type": "string"},
                "start":       {"type": "string"},
                "end":         {"type": "string"},
                "description": {"type": "string"},
                "location":    {"type": "string"},
            },
            "required": ["title", "start", "end"],
        },
        risk_level="write",
        run=_run_create(client),
    ))
    registry.register(Tool(
        name="calendar_delete_event",
        description=...,
        schema={
            "type": "object",
            "properties": {"event_id": {"type": "string"}},
            "required": ["event_id"],
        },
        risk_level="destructive",
        run=_run_delete(client),
    ))
```

Each `_run_*` closure does:

1. Resolve `start`/`end` via `time_grounding.resolve()` (no-op if already ISO).
2. Translate to whatever the MCP server expects.
3. Call `client.call_tool(...)`.
4. Compress the JSON response to one-event-per-line text the model can
   summarize aloud (don't return the raw JSON — it bloats context).

### 4.2 `src/jarvis/tools/time_grounding.py`

```python
from datetime import datetime
from zoneinfo import ZoneInfo
import parsedatetime

LOCAL_TZ = ZoneInfo("America/Los_Angeles")

def now_local() -> datetime:
    return datetime.now(LOCAL_TZ)

def resolve(phrase: str, anchor: datetime | None = None) -> str:
    """Resolve a relative phrase to ISO 8601 with local tz offset.

    Pass-through for strings that already look like ISO 8601.
    Raises ValueError on unparseable input — the tool-use loop catches and
    feeds the error back to the model so it can try again.
    """
    if _looks_iso(phrase):
        return phrase
    cal = parsedatetime.Calendar()
    base = anchor or now_local()
    struct, flag = cal.parseDT(phrase, sourceTime=base, tzinfo=base.tzinfo)
    if flag == 0:
        raise ValueError(f"could not parse time: {phrase!r}")
    return struct.isoformat()
```

`parsedatetime` over `dateparser`: smaller dep tree, well-maintained,
handles "tomorrow at 3pm" / "next Thursday morning" / "in 20 minutes"
cleanly. If `evals/time.py` rows fail, the swap to `dateparser` is a
one-import change.

### 4.3 `src/jarvis/tools/reminders.py`

```python
def _osascript(script: str) -> str:
    res = subprocess.run(["osascript", "-e", script],
                         capture_output=True, text=True, timeout=10)
    if res.returncode != 0:
        return f"[reminders error] {res.stderr.strip() or 'non-zero exit'}"
    return res.stdout.strip() or "(ok)"

def _run_create(args: dict) -> str:
    title = args["title"].replace('"', '\\"')
    due_iso = time_grounding.resolve(args["due"]) if args.get("due") else None
    due_clause = (f'set due date of newRem to date "{due_iso}"'
                  if due_iso else "")
    script = f'''
        tell application "Reminders"
            set newRem to make new reminder with properties {{name:"{title}"}}
            {due_clause}
        end tell
    '''
    return _osascript(script)

def register_reminders(registry: Registry) -> None:
    registry.register(Tool(
        name="reminders_create",
        description="Create an Apple Reminders item. Use for to-dos, "
                    "deadlines, and explicit 'remind me to ...' requests.",
        schema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "due":   {"type": "string",
                          "description": "ISO datetime or relative phrase; omit for no due date"},
            },
            "required": ["title"],
        },
        risk_level="write",
        run=_run_create,
    ))
```

We bypass the generic `run_applescript` tool *deliberately* — a dedicated
reminders tool gets a tighter schema (model can't accidentally compose a
multi-line AppleScript), a clearer confirmation summary, and removes one
"hallucinate the AppleScript" failure mode.

### 4.4 `src/jarvis/oauth/google.py`

```python
TOKEN_PATH = settings.data_dir / "oauth" / "google.json"  # 0600

def load() -> Credentials | None:
    if not TOKEN_PATH.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        save(creds)
    return creds

def save(creds: Credentials) -> None:
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(creds.to_json())
    TOKEN_PATH.chmod(0o600)
    assert TOKEN_PATH.stat().st_mode & 0o077 == 0  # paranoid recheck

def authenticate_interactive() -> Credentials:
    """Triggered by `jarvis auth gcal`. Spawns the OAuth browser flow on
    localhost:8765 and saves the resulting token. Idempotent — if a valid
    token already exists, just refreshes it."""
    ...
```

### 4.5 `src/jarvis/cli.py` (modify)

Add subcommands:

```
jarvis auth gcal       # one-time browser flow; writes ~/.jarvis/oauth/google.json
jarvis auth status     # lists tokens + expiry, with file permissions
```

### 4.6 `src/jarvis/agent/prompt.py` (modify)

Replace the static `SYSTEM_PROMPT` constant with `build_system_prompt() -> str`
that prepends the time anchor:

```python
def build_system_prompt() -> str:
    now = now_local()
    return (f"Current date and time: {now:%A, %B %d, %Y at %I:%M %p %Z}.\n\n"
            + _BASE_PROMPT)
```

Called per-turn in `loops/chat.py` so the anchor is fresh.

---

## 5. The Time-Grounding Test Plan

Time grounding is the failure mode most likely to bite the user. Build
`evals/time.py` **before** writing any tool that consumes datetimes.

```
input phrase          | anchor (now)               | expected resolution
----------------------|----------------------------|----------------------------
"tomorrow at 3pm"     | 2026-05-19 09:00 PDT       | 2026-05-20T15:00-07:00
"tomorrow"            | 2026-05-19 09:00 PDT       | 2026-05-20T09:00-07:00 (no time -> carry)
"next Thursday"       | 2026-05-19 (Mon) PDT       | 2026-05-21T...
"in 30 minutes"       | 2026-05-19 09:00 PDT       | 2026-05-19T09:30-07:00
"3pm"                 | 2026-05-19 09:00 PDT       | 2026-05-19T15:00-07:00 (today, future)
"3pm"                 | 2026-05-19 16:00 PDT       | 2026-05-20T15:00-07:00 (today is past -> next day)
"end of week"         | 2026-05-19 (Mon) PDT       | 2026-05-23T... (Friday)
"in two hours"        | 2026-05-19 09:00 PDT       | 2026-05-19T11:00
"yesterday"           | 2026-05-19                 | ValueError or 2026-05-18 — pick one and document
```

Target: **100% on the table.** Parsing is deterministic — a failure means
fix the resolver (or document the choice), not retry the data.

The "yesterday" row is a documented-choice row: do we resolve to a past
datetime (and let calendar reject it) or refuse upfront? Pick now, lock
it in.

---

## 6. Failure Modes & Mitigations

| Failure | Likelihood | Mitigation | Where |
|---|---|---|---|
| OAuth token expires; refresh fails silently | Medium | Catch refresh error → speak "Your Google auth expired. Run `jarvis auth gcal` to re-authorize." Don't try to mask it. | §4.4 |
| Tokens leak via `git add .` | Low | Stored in `~/.jarvis/` (outside repo) per DESIGN.md §7.3 | §4.4 |
| Token file group/other readable | Low | `chmod 0o600` after every write; assert at load time | §4.4 |
| Model guesses wrong year (training cutoff) | High pre-fix | Inject current date in system prompt each turn | §4.6 |
| "3pm" with no date is ambiguous about today vs tomorrow | Medium | Default to *future* (today if still future, else tomorrow); confirmation reads back the resolved date so the user catches it | §4.2 |
| Reminders AppleScript races with the Reminders.app sync | Low (UX) | Accept eventual consistency; the post-create acknowledgment says "should show up on your phone shortly" | §4.3 |
| Calendar delete confirmation says only "delete event" with no details | Medium | Confirmation summary fetches the event first and reads title + start time aloud before proceeding | §4.1, summarize.py |
| MCP server rate-limits us | Low | Phase 3 retry-with-error catches it; model can apologize | tool_loop.py |
| GCal MCP returns >50 events in a "this month" query | Medium | `_run_list` truncates to top-20 by start time and notes truncation in the returned text | §4.1 |
| `time_grounding.resolve()` ValueError reaches the user as a stack trace | Low | The tool's `_run` catches and returns the error text; tool_loop feeds it back as a `tool_msg` so the model can apologize or ask | §4.1, §4.2 |
| User says "remind me to X on Saturday" but the model picks the *past* Saturday | Medium | `parsedatetime` defaults to future for ambiguous days; verify in `evals/time.py` | §5 |
| GCal scopes too broad (full read/write) | Real | Document in PLAN.md §6; scope is necessary for create/delete; user accepts at OAuth time | §4.4 |
| MCP server crashes mid-session | Low | Same fallback as Phase 3 (`mcp.stop()` + restart on next turn? — for v1, just degrade and log) | §4.1 |

---

## 7. System Prompt Changes

Append to the `Tools:` section:

> - When acting on calendar or reminders, use specific times. If Gavin
>   says "soon" or "later," ask one short clarifying question with a
>   concrete suggestion ("3pm okay?") instead of guessing.
> - If asked "what's on my calendar," call `calendar_list` rather than
>   guessing — your training data does not know Gavin's schedule.
> - Times can be ISO 8601 or relative phrases ("tomorrow at 3pm"); both
>   are accepted by the tools.

Plus the dynamic time prepend from §4.6.

---

## 8. Latency Budget

Calendar list:

| Stage | Budget |
|---|---|
| LLM round 1 (decide + emit call) | ~1.0s |
| Tool call (network to GCal) | ~0.5–1.0s |
| LLM round 2 (synthesize event list) | ~1.5s (longer payload than search) |
| TTS | ~0.3s |
| **eos → audio: ~5–6s** |

Reminder create (confirmed):

| Stage | Budget |
|---|---|
| LLM round 1 (decide + emit call) | ~1.0s |
| Phase 4 confirmation overhead | ~2.0s |
| AppleScript exec | ~0.2s |
| LLM final ack | ~0.8s |
| TTS | ~0.3s |
| **eos → audio: ~6–7s** |

---

## 8.5 Observability

A calendar list turn:

```
[you] what's on my calendar tomorrow
[tool] -> calendar_list({"start": "tomorrow", "end": "tomorrow"})
[time] resolved 'tomorrow' -> '2026-05-20T00:00:00-07:00'
[tool] <- calendar_list: '09:00 Team standup\n14:00 Lunch with Alex\n16:00 1:1 with Sam'
[jarvis] Tomorrow you've got the team standup at nine, lunch with Alex at two, and a one-on-one at four.
[timing] stt=400ms agent=4810ms eos->audio=5210ms total=6900ms
```

A create-event turn (confirmed):

```
[you] put dentist on the calendar for Friday at 10am
[tool] -> calendar_create_event({"title": "Dentist", "start": "Friday at 10am", "end": "Friday at 11am"})
[time] resolved 'Friday at 10am' -> '2026-05-23T10:00:00-07:00'
[confirm] summary: "Create event Dentist on Friday May 23 at 10am"
[confirm] attempt 1: heard 'yes' -> yes
[tool] <- calendar_create_event: 'created event eid=abc123'
[jarvis] Added.
[timing] stt=370ms agent=2400ms confirm=1900ms eos->audio=4670ms total=6100ms
```

The `[time]` line is the explicit grounding receipt — it's how a user
debugging "the assistant put it on the wrong day" can see *what
phrase* was parsed and *what ISO datetime* came out. Without it, time
errors are silent.

### OAuth-failure recovery (the recipe)

If `jarvis run` startup prints `[oauth] google: token refresh failed`:

1. Run `jarvis auth status` — confirms the token file's last-modified
   time and expiry.
2. If expired: `jarvis auth gcal` → browser flow → done.
3. If revoked (refresh-token gone): same — `jarvis auth gcal` handles
   either case; it's idempotent.
4. If the browser flow loops, check that `~/.jarvis/oauth/` is
   writable and that `localhost:8765` isn't taken by another process.

The fix is never "ignore OAuth and proceed offline" — calendar tools
can't function without it.

---

## 9. Build Order (Checklist)

1. [ ] `evals/time.py` — write the table first, **before** any code that
       depends on parsing.
2. [ ] `tools/time_grounding.py` — make the table pass.
3. [ ] `oauth/google.py` — token persistence + 0600 + refresh.
4. [ ] `cli.py` — `jarvis auth gcal` + `jarvis auth status` subcommands.
5. [ ] Pick a GCal MCP server; manual `client.list_tools()` smoke without
       LLM.
6. [ ] `tools/calendar.py` — register **list-only** first (`risk: read`).
7. [ ] Headless: query "what's on my calendar tomorrow" against the live
       API. Verify the returned text is one-event-per-line and tractable
       for the synthesis LLM round.
8. [ ] `agent/summarize.py` — add calendar/reminder summary templates so
       Phase 4 confirmations are specific.
9. [ ] `tools/calendar.py` — extend to `calendar_create_event`
       (`risk: write`); confirms via the Phase 4 flow.
10. [ ] `tools/reminders.py` — AppleScript create.
11. [ ] `agent/prompt.py` — dynamic time prepend + system-prompt rules.
12. [ ] Extend `evals/toolcall.py` with calendar/reminder triggers
       (over-call / under-call regressions).
13. [ ] `tools/calendar.py` — `calendar_delete_event` (`risk: destructive`,
       summary reads event details aloud).
14. [ ] **User voice test:** the two PLAN.md headline queries
       end-to-end, plus one delete-event test.

---

## 10. Scope Discipline — What Phase 5 Does NOT Do

- **No multi-calendar selection.** Primary calendar only. Picking among
  multiple calendars is a multi-step UX problem; deferred.
- **No recurring events.** Single events with single start/end. Recurring
  rules are a schema rabbit-hole that doesn't pay off until there's
  evidence it's needed daily.
- **No attendee invites.** Solo events only. Inviting people = email
  semantics = Phase 6.
- **No timezone math beyond local.** If Gavin travels with the Mac, GCal
  handles tz on its side; we don't synthesize timezone changes here.
- **No fuzzy calendar search** ("find that meeting about the budget").
  List by date range is enough for v1.
- **No reminders edit/delete tools.** Create-only. Editing reminders by
  voice is a rare ask; do it in the Reminders app for v1.

---

## 11. Risks & Escape Hatches

| Risk | Trigger | Escape hatch |
|---|---|---|
| GCal MCP server is buggy or unmaintained | Errors in `list_tools()` / `call_tool()` | Fork the server into our repo (it's small); or fall back to `google-api-python-client` direct — the registered tool surface stays the same |
| Time grounding fails on "next Tuesday at noon Pacific"-type phrases | `evals/time.py` rows fail | Switch from `parsedatetime` to `dateparser` (broader, heavier). One-import change. |
| OAuth refresh-token revoked by Google | `jarvis run` startup errors | Surface plain instruction: "Run `jarvis auth gcal` to re-authorize." Don't paper over. |
| Apple Reminders sync lag confuses the user | Reminder doesn't appear instantly on phone | Set expectations in the post-create acknowledgment ("Added — should show up on your phone in a moment.") |
| Model creates events at the wrong year (2024 instead of 2026) | Real if time anchor not injected | The §4.6 prompt injection is the fix; if it still happens, add `time_grounding.resolve` to *also* clamp dates that look more than a year off |
| Calendar delete fires on the wrong event | Real | Phase 4 destructive confirmation reads title + start aloud (§4.1) — the gate is the safety net |

---

## 12. Definition of Done

- [ ] Both headline acceptance queries pass end-to-end voice tests
- [ ] `evals/time.py` 100% pass
- [ ] Tokens stored 0600, verified by `ls -la ~/.jarvis/oauth/`
- [ ] Delete-event confirmation reads the event title + start time aloud
      before proceeding (verified by listening)
- [ ] PLAN.md Phase 5 marked done with measured tool-turn p50 for list and
      for create-with-confirm
