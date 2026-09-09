import json
import os
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

d = r"C:\Users\hp\AgentAssistant\research_runs"
for r in ["503ffe745781", "aad230706c7c", "1adc4d2097da"]:
    mp = os.path.join(d, r, "meta.json")
    m = json.loads(open(mp, encoding="utf-8").read())
    print(f"=== {r} status={m.get('status')} error={m.get('error')}")
    # run_start event: goal head + any resume marker
    tp = os.path.join(d, r, "trace.jsonl")
    lines = open(tp, encoding="utf-8").read().splitlines()
    e0 = json.loads(lines[0])
    goal = (e0.get("args") or {}).get("goal", "")
    print("  goal[:80]:", goal[:80].replace("\n", " "))
    print("  events:", len(lines))
    # look for resume digest / timeout events
    for line in lines:
        e = json.loads(line)
        tool = e.get("tool", "")
        if tool in ("llm_retry", "report_retry", "report_fallback", "budget_notice"):
            print(f"  t{e.get('turn')} {tool}: {(e.get('summary') or '')[:80]}")
    # last event
    e_last = json.loads(lines[-1])
    print("  last:", e_last.get("tool"), "|", (e_last.get("summary") or "")[:90])
    print()
