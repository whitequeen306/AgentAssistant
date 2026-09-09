"""Tail the newest research run trace; print one compact line per event."""
import json
import os
import sys
import io
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

d = r"C:\Users\hp\AgentAssistant\research_runs"
run_filter = sys.argv[1] if len(sys.argv) > 1 else None


def newest_running():
    runs = []
    for r in os.listdir(d):
        mp = os.path.join(d, r, "meta.json")
        try:
            m = json.loads(open(mp, encoding="utf-8").read())
        except Exception:
            continue
        runs.append((os.path.getmtime(mp), r, m))
    runs.sort(reverse=True)
    for _, r, m in runs:
        if m.get("status") == "running" and (not run_filter or r.startswith(run_filter)):
            return r
    return runs[0][1] if runs else None


run = newest_running()
print("WATCHING", run, flush=True)
p = os.path.join(d, run, "trace.jsonl")
seen = 0
while True:
    # re-check status each loop; stop 3s after run leaves "running"
    try:
        meta = json.loads(open(os.path.join(d, run, "meta.json"), encoding="utf-8").read())
    except Exception:
        meta = {}
    try:
        lines = open(p, encoding="utf-8").read().splitlines()
    except FileNotFoundError:
        lines = []
    for line in lines[seen:]:
        seen += 1
        try:
            e = json.loads(line)
        except Exception:
            continue
        t = e.get("ts", "")[11:19]
        tool = e.get("tool", "?")
        ok = e.get("ok")
        turn = e.get("turn")
        args = json.dumps(e.get("args", {}), ensure_ascii=False)[:110]
        flag = "OK " if ok else "FAIL" if ok is False else "... "
        print(f"{t} t{turn!s:>2} {flag} {tool:<14} {args}", flush=True)
        s = (e.get("summary") or "").replace("\n", " / ")[:110]
        if s:
            print(f"           | {s}", flush=True)
    status = meta.get("status")
    if status and status != "running":
        print(f"RUN FINISHED status={status} error={meta.get('error')}", flush=True)
        break
    time.sleep(3)
