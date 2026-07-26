#!/usr/bin/env python3
"""Re-open tasks that never got a fair attempt, so the A/B driver retries them.

Three abort variants seen on 2026-07-25, none of which are model failures but
all of which score as ordinary failures if left alone:

  1. five_hour 429            — token cap closed the window
  2. error_during_execution   — agent CLI killed, "[Request interrupted by user]"
  3. no terminal result event — stream stops mid-tool-call, rate limit 'allowed'

Variant 3 is the sneaky one: 10 tasks in the band 24-36 hit it in a 44s window,
6 scored as failures and dragged the skilled arm to a bogus 152/164.

Usage:
  python3 scripts/reopen_aborted.py           # report only
  python3 scripts/reopen_aborted.py --apply   # move them back to pending
"""
import glob
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(REPO)
STATE = "outputs/opus5_ab/state.json"


def abort_reason(ev_path):
    if os.path.getsize(ev_path) == 0:
        return "empty_stream"
    lines = open(ev_path, errors="ignore").readlines()
    if not any('"type":"result"' in l or '"type": "result"' in l for l in lines):
        return "no_result_event"
    for l in lines:
        if "error_during_execution" in l or "Request interrupted by user" in l:
            return "interrupted"
        if '"status":"rejected"' in l.replace(" ", "") and "five_hour" in l:
            return "rate_capped"
    return None


def scan_arm(arm):
    """Latest verdict per task across that arm's runs, newest run wins."""
    verdict = {}
    for d in sorted(glob.glob(f"outputs/opus5_ab/{arm}/agent_*/"),
                    key=os.path.getmtime):
        for ev in glob.glob(d + "workspaces/task_*/events.jsonl"):
            tid = int(ev.split("task_")[-1].split("/")[0])
            verdict[tid] = abort_reason(ev)
    return {t: r for t, r in verdict.items() if r}


def main():
    apply = "--apply" in sys.argv
    st = json.load(open(STATE))
    total = 0
    for arm in st["arms"]:
        bad = scan_arm(arm)
        if not bad:
            print(f"{arm}: clean — every task got a fair attempt")
            continue
        a = st["arms"][arm]
        # only re-open what is currently scored; leave already-pending alone
        scored = set(a["passed"]) | set(a["failed"])
        reopen = {t: r for t, r in bad.items() if t in scored}
        by_reason = {}
        for t, r in sorted(reopen.items()):
            by_reason.setdefault(r, []).append(t)
        print(f"{arm}: {len(reopen)} tasks to re-open")
        for r, ts in by_reason.items():
            hit = [t for t in ts if t in set(a["failed"])]
            print(f"   {r:16s} {ts}")
            print(f"   {'':16s} (of which scored as FAILURES: {hit})")
        total += len(reopen)
        if apply:
            a["passed"] = sorted(set(a["passed"]) - set(reopen))
            a["failed"] = sorted(set(a["failed"]) - set(reopen))
            a["pending"] = sorted(set(a["pending"]) | set(reopen))
    if apply:
        st["done"] = False
        json.dump(st, open(STATE, "w"), indent=2)
        print(f"\napplied — {total} tasks moved back to pending; "
              f"run `python3 scripts/opus5_ab.py` to retry them")
    else:
        print(f"\n{total} tasks affected (report only; pass --apply to re-open)")


if __name__ == "__main__":
    main()
