"""Phase 3 tool-call eval — the resume-grade deliverable (PHASE3.md §10).

Most portfolio projects stop at "it worked once." This is the repeatable
measurement that turns "I built a tool-using assistant" into "I built one
and measured N% tool-call accuracy at X ms p50, with a reproducible harness."

Run:  uv run python evals/toolcall.py

It drives the real tool_loop against the live MCP server and scores whether
the model decided to search when it should have (and not when it shouldn't),
plus latency percentiles split by class. Re-run after any prompt or
tool-description change — it's the regression suite.
"""

import statistics
import time

from jarvis.agent.prompt import SYSTEM_PROMPT
from jarvis.agent.tool_loop import run_tool_loop
from jarvis.tools.mcp_client import MCPClient
from jarvis.tools.registry import Registry
from jarvis.tools.web_search import register_web_search

# (utterance, expected_search). True = must search (current/real-time data),
# False = must NOT search (parametric knowledge — searching is over-calling).
GOLDEN: list[tuple[str, bool]] = [
    # --- should SEARCH: current / real-time / time-sensitive ---
    ("what's the weather in San Francisco today", True),
    ("what's the weather like in Tokyo right now", True),
    ("who won the game last night", True),
    ("what's the price of bitcoin", True),
    ("what's the latest news about SpaceX", True),
    ("what time does the Apple store in Palo Alto close today", True),
    ("what's the current stock price of Nvidia", True),
    ("are there any flight delays at SFO right now", True),
    ("what movies are playing in theaters this week", True),
    ("who is the current prime minister of Japan", True),
    ("what's trending on the news today", True),
    ("how did the stock market do today", True),
    ("what's the exchange rate from USD to euro right now", True),
    ("what are the reviews for the new Dune movie", True),
    # --- should NOT search: static / parametric / arithmetic ---
    ("what's the capital of France", False),
    ("what's seven times eight", False),
    ("what does HTTP stand for", False),
    ("how many days are in a leap year", False),
    ("what's the chemical symbol for gold", False),
    ("who wrote Romeo and Juliet", False),
    ("convert 10 kilometers to miles", False),
    ("what's the square root of 144", False),
    ("define the word ephemeral", False),
    ("how many continents are there", False),
    ("what's the boiling point of water in Celsius", False),
    ("spell the word necessary", False),
    ("what's 15 percent of 200", False),
    ("who painted the Mona Lisa", False),
]


def _searched(messages: list[dict]) -> bool:
    return any(m.get("role") == "tool" for m in messages)


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, round((p / 100) * (len(s) - 1))))
    return s[k]


def main() -> None:
    reg = Registry()
    mcp = MCPClient()
    mcp.start()
    register_web_search(reg, mcp)

    rows = []
    try:
        for i, (utt, expected) in enumerate(GOLDEN, 1):
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": utt},
            ]
            t = time.perf_counter()
            try:
                run_tool_loop(messages, reg)
                err = None
            except Exception as exc:  # noqa: BLE001 - record, don't abort the run
                err = repr(exc)
            dt = (time.perf_counter() - t) * 1000
            got = _searched(messages)
            correct = (got == expected) and err is None
            rows.append((utt, expected, got, correct, dt, err))
            flag = "ok " if correct else "ERR" if err else "MISS"
            print(
                f"[{i:2}/{len(GOLDEN)}] {flag}  "
                f"exp={'S' if expected else '.'} got={'S' if got else '.'} "
                f"{dt:6.0f}ms  {utt}"
            )
    finally:
        mcp.stop()

    n = len(rows)
    correct = sum(r[3] for r in rows)
    should = [r for r in rows if r[1]]
    shouldnt = [r for r in rows if not r[1]]
    under = [r for r in should if not r[2]]  # should search, didn't
    over = [r for r in shouldnt if r[2]]  # shouldn't search, did
    errs = [r for r in rows if r[5]]
    search_lat = [r[4] for r in rows if r[2]]
    nosearch_lat = [r[4] for r in rows if not r[2]]

    print("\n" + "=" * 60)
    print("TOOL-CALL SCORECARD")
    print("=" * 60)
    print(f"Overall accuracy ....... {correct}/{n}  ({100*correct/n:.0f}%)   target >=90%")
    print(f"  should-search recall . {len(should)-len(under)}/{len(should)}")
    print(f"  no-search specificity  {len(shouldnt)-len(over)}/{len(shouldnt)}")
    print(f"Under-calls (missed) ... {len(under)}")
    for r in under:
        print(f"    - {r[0]}")
    print(f"Over-calls (needless) .. {len(over)}")
    for r in over:
        print(f"    - {r[0]}")
    print(f"Errors ................. {len(errs)}")
    for r in errs:
        print(f"    - {r[0]}  ::  {r[5]}")
    print("Latency (ms):")
    print(
        f"  search turns ... p50={_pct(search_lat,50):.0f}  "
        f"p95={_pct(search_lat,95):.0f}  (n={len(search_lat)})"
    )
    print(
        f"  no-search ...... p50={_pct(nosearch_lat,50):.0f}  "
        f"p95={_pct(nosearch_lat,95):.0f}  (n={len(nosearch_lat)})"
    )
    if search_lat:
        print(f"  search mean .... {statistics.mean(search_lat):.0f}")
    print("=" * 60)


if __name__ == "__main__":
    main()
