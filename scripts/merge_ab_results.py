#!/usr/bin/env python3
"""Collapse an arm's run fragments into one canonical results file.

A cap- or abort-resumed arm ends up spread over several run dirs (the skilled
Opus 5 arm took three: initial + interrupt retry + abort repair). This merges
them into the repo's committed `outputs/results_agent_<model>_<variant>.json`
shape, keeping each task's LATEST attempt — which is the fair one, since aborted
tasks are exactly the ones that got re-run.

Cross-checks the merged pass count against the driver ledger and refuses to
write on mismatch, so a fragment can't be silently dropped.

Usage: python3 scripts/merge_ab_results.py
"""
import glob
import json
import os
import statistics
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(REPO)

STATE = json.load(open("outputs/opus5_ab/state.json"))
MODEL = STATE.get("model", "claude-opus-5")
ARMS = {"skilled": "skill", "noskill": "noskill"}


def merge_arm(arm):
    """Latest attempt per task across the arm's run dirs, oldest first."""
    sols, res, metrics_src = {}, {}, []
    run_dirs = sorted(glob.glob(f"outputs/opus5_ab/{arm}/agent_*/"),
                      key=os.path.getmtime)
    for d in run_dirs:
        for line in open(os.path.join(d, "solutions.jsonl")):
            s = json.loads(line)
            sols[s["task_id"]] = s
        blob = json.load(open(os.path.join(d, "results.json")))
        for r in blob["results"]:
            res[r["task_id"]] = r
        metrics_src.append(blob)
    return sols, res, run_dirs, metrics_src


def build(arm):
    sols, res, run_dirs, blobs = merge_arm(arm)
    ledger = STATE["arms"][arm]
    expected = set(ledger["passed"])

    tasks = sorted(res)
    results = []
    for t in tasks:
        r = res[t]
        results.append({"task_id": t, "sample_index": 0,
                        "passed": bool(r["passed"]),
                        "info": r.get("info", "passed" if r["passed"] else "failed"),
                        "errored": bool(r.get("errored", False))})
    got = {r["task_id"] for r in results if r["passed"]}

    if got != expected:
        sys.exit(f"REFUSING to write {arm}: merged passes {len(got)} != ledger "
                 f"{len(expected)}; only-in-merge={sorted(got-expected)} "
                 f"only-in-ledger={sorted(expected-got)}")

    wall = [s.get("agent_wall_time") or 0 for s in sols.values()]
    turns = [s.get("agent_num_turns") for s in sols.values() if s.get("agent_num_turns")]
    passed_wall = [sols[t].get("agent_wall_time") or 0 for t in tasks if res[t]["passed"]]
    failed_wall = [sols[t].get("agent_wall_time") or 0 for t in tasks if not res[t]["passed"]]

    return {
        "total_solutions": len(results),
        "passed_solutions": len(got),
        "errored_solutions": sum(1 for r in results if r["errored"]),
        "scored_solutions": len(results),
        "pass_rate": len(got) / len(results),
        "total_problems": len(results),
        "pass_at_1": len(got) / len(results),
        "per_problem_results": [
            {"task_id": t, "num_samples": 1, "num_correct": int(res[t]["passed"])}
            for t in tasks],
        "evaluation_type": "agent",
        "agent_backend": "claude-code",
        "agent_model": MODEL,
        "agent_max_turns": 20,
        "dataset": STATE.get("dataset", "q-humaneval"),
        # NB: no extra provenance keys here — committed results files match the
        # existing schema exactly. Run provenance lives in docs/OPUS-5-NOTES.md.
        "agent_metrics": {
            "total_wall_time_seconds": sum(wall),
            "mean_wall_time_seconds": statistics.mean(wall) if wall else None,
            "median_wall_time_seconds": statistics.median(wall) if wall else None,
            "mean_wall_time_passed": statistics.mean(passed_wall) if passed_wall else None,
            "mean_wall_time_failed": statistics.mean(failed_wall) if failed_wall else None,
            "total_cost_usd": ledger["cost"],
            "mean_cost_usd": ledger["cost"] / len(results),
            "mean_turns": statistics.mean(turns) if turns else None,
            "median_turns": statistics.median(turns) if turns else None,
            "no_solution_count": sum(
                1 for s in sols.values()
                if not (s.get("completion") or "").strip()
                or "/ function body" in (s.get("completion") or "")),
            "errored_count": sum(1 for r in results if r["errored"]),
        },
        "results": results,
    }


def main():
    for arm, tag in ARMS.items():
        blob = build(arm)
        out = f"outputs/results_agent_{MODEL}_{tag}.json"
        json.dump(blob, open(out, "w"), indent=2)
        print(f"{out}: {blob['passed_solutions']}/{blob['total_solutions']} "
              f"= {blob['pass_rate']*100:.1f}%  "
              f"(merged runs)")


if __name__ == "__main__":
    main()
