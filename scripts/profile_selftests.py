#!/usr/bin/env python3
"""How aggressively does each model self-test its q solution at codegen time?

Motivated by the 2026-07-25 root cause: the agent probes its own solution by
running q, and a pathological probe input (factorize of the 10-digit prime
1000000007) spins a q process at 100% CPU, starving other in-flight agents.
Question: is Opus 5 unusually aggressive here, or is this long-standing?

Counts, per run: `q solution.q` invocations issued by the agent, and how many
carry a >=6-digit numeric literal (a proxy for a heavy probe input).
"""
import glob
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(REPO)

BIG = re.compile(r"\b\d{6,}\b")


def profile(run_dir):
    tasks = qcalls = big = 0
    examples = []
    for ev in glob.glob(os.path.join(run_dir, "workspaces", "task_*", "events.jsonl")):
        try:
            if os.path.getsize(ev) == 0:
                continue
        except OSError:
            continue
        tasks += 1
        for line in open(ev, errors="ignore"):
            # cheap prefilter before the expensive json parse
            if "tool_use" not in line or "solution.q" not in line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            for c in (e.get("message") or {}).get("content") or []:
                if c.get("type") != "tool_use":
                    continue
                cmd = (c.get("input") or {}).get("command") or ""
                if "q solution.q" not in cmd:
                    continue
                qcalls += 1
                hits = BIG.findall(cmd)
                if hits:
                    big += 1
                    if len(examples) < 5:
                        examples.append(max(hits, key=len))
    return tasks, qcalls, big, examples


def main():
    runs = sys.argv[1:] or sorted(glob.glob("outputs/agent_claude-code_*"))
    print(f"{'run':<46} {'tasks':>5} {'q_tests':>8} {'per_task':>9} {'heavy':>6}  examples")
    for r in runs:
        if not os.path.isdir(os.path.join(r, "workspaces")):
            continue
        t, q, b, ex = profile(r)
        if not t:
            continue
        label = os.path.basename(r).replace("agent_claude-code_", "").replace("agent_codex_", "")
        print(f"{label:<46} {t:>5} {q:>8} {q/t:>9.2f} {b:>6}  {ex}", flush=True)


if __name__ == "__main__":
    main()
