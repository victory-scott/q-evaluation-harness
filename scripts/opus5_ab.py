#!/usr/bin/env python3
"""Opus 5 A/B on q-humaneval: skilled (q-kdb) vs clean-room (--no-skills).

Survives the 5-hour token cap across sessions. State of record is
outputs/opus5_ab/state.json — re-running this script picks up exactly where it
left off, so a fresh Claude session (or a human) can resume with no context.

Per cycle: for each arm with pending ids, run them. Tasks that come back as
cap stubs (instant return, no real attempt) stay pending; everything else is
scored and retired. When both arms are fully capped, sleep until the window
reopens and go again.
"""
import glob
import json
import os
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(REPO)

SKILL = os.path.expanduser("~/.q-eval/skills/q-kdb")
MODEL = "claude-opus-5"
BASE = "outputs/opus5_ab"
STATE = os.path.join(BASE, "state.json")
ALL_IDS = [json.loads(l)["task_id"] for l in open("datasets/q_humaneval.jsonl")]
if os.environ.get("AB_IDS"):  # smoke-test subset
    ALL_IDS = [int(x) for x in os.environ["AB_IDS"].split(",")]

ARMS = {
    "skilled": ["--skill-dirs", SKILL],
    "noskill": ["--no-skills"],
}
ARM_ORDER = ["skilled", "noskill"]  # A fully, then B


def load_state():
    if os.path.exists(STATE):
        return json.load(open(STATE))
    return {
        "model": MODEL,
        "dataset": "q-humaneval",
        "total": len(ALL_IDS),
        "skill_provenance": "KxSystems/kx-skills @ f8deb26, plugins/q-knowledge/skills/q (verified identical 2026-07-25)",
        "arms": {
            arm: {"pending": list(ALL_IDS), "passed": [], "failed": [],
                  "runs": [], "cost": 0.0}
            for arm in ARMS
        },
        "next_eligible_epoch": 0,
        "cycles": 0,
    }


def save_state(st):
    os.makedirs(BASE, exist_ok=True)
    tmp = STATE + ".tmp"
    json.dump(st, open(tmp, "w"), indent=2)
    os.replace(tmp, STATE)


def scan_aborted(run_dir):
    """Task ids that never got a fair attempt, + max resetsAt.

    Two distinct causes, both retryable rather than scoreable:
      * five_hour 429  — token cap closed the window mid-run
      * error_during_execution / empty stream — the agent CLI was killed by an
        external signal ("[Request interrupted by user]"). Cost 13 tasks on
        2026-07-25; scoring these as failures silently understates a model.
    """
    capped, reset = set(), 0
    for ev in glob.glob(os.path.join(run_dir, "workspaces", "task_*", "events.jsonl")):
        tid = int(ev.split("task_")[-1].split("/")[0])
        if os.path.getsize(ev) == 0:
            capped.add(tid)
            continue
        # Third abort variant (2026-07-25 evening): the stream simply stops
        # mid-tool-call with NO terminal result event and no interrupt message —
        # 10 tasks in the contiguous band 24-36 died inside a 44s window while
        # rate-limit status was 'allowed'. Without this check they score as
        # ordinary failures, which is what dragged skilled to a bogus 152.
        if not any('"type":"result"' in l or '"type": "result"' in l
                   for l in open(ev, errors="ignore")):
            capped.add(tid)
            continue
        for line in open(ev, errors="ignore"):
            if '"error_during_execution"' in line or "Request interrupted by user" in line:
                capped.add(tid)
            if "rate_limit" not in line and "429" not in line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("type") == "result" and e.get("is_error") and e.get("api_error_status") == 429:
                capped.add(tid)
            rli = e.get("rate_limit_info") or {}
            if rli.get("rateLimitType") == "five_hour" and rli.get("status") == "rejected":
                capped.add(tid)
                if rli.get("resetsAt"):
                    reset = max(reset, int(rli["resetsAt"]))
    return capped, reset


def is_stub(sol):
    c = sol.get("completion", "")
    return ("/ function body" in c) or (not c.strip())


def run_arm(arm, ids):
    """Run one arm over `ids`. Returns (passed, failed, still_pending, reset_epoch)."""
    out = os.path.join(BASE, arm)
    before = set(glob.glob(os.path.join(out, "agent_*")))
    cmd = ["poetry", "run", "qeval", "agent-run", "q-humaneval",
           "--backend", "claude-code", "--model", MODEL,
           *ARMS[arm],
           "--problem-ids", *map(str, ids),
           "--timeout", "600", "--concurrency", "4",
           "--save-events", "--keep-workspaces", "-o", out]
    print(f"[{time.strftime('%H:%M:%S')}] {arm}: launching {len(ids)} tasks", flush=True)
    # start_new_session: put qeval (and the claude CLIs it spawns) in their own
    # process group. The 2026-07-25 run lost 13 tasks to simultaneous
    # "[Request interrupted by user]" aborts — millisecond-aligned across every
    # in-flight task, i.e. a process-group signal reaching the children rather
    # than any model or rate-limit failure. This isolates them from that.
    subprocess.run(cmd, check=False, start_new_session=True)
    new = set(glob.glob(os.path.join(out, "agent_*"))) - before
    if not new:
        print(f"{arm}: no run dir produced — aborting cycle", flush=True)
        return set(), set(), set(ids), 0, "", 0.0
    run_dir = max(new, key=os.path.getmtime)

    sols = {json.loads(l)["task_id"]: json.loads(l)
            for l in open(os.path.join(run_dir, "solutions.jsonl"))}
    res_blob = json.load(open(os.path.join(run_dir, "results.json")))
    res = {r["task_id"]: r["passed"] for r in res_blob["results"]}

    capped, reset = scan_aborted(run_dir)
    passed, failed, pending = set(), set(), set()
    for t in ids:
        s = sols.get(t, {})
        # a cap casualty: 429 in the stream, or an instant empty return
        stubbed = is_stub(s) and s.get("agent_wall_time", 99) <= 5 \
            and (s.get("agent_num_turns") or 0) <= 1
        if t in capped or stubbed:
            pending.add(t)
        elif res.get(t):
            passed.add(t)
        else:
            failed.add(t)
    cost = (res_blob.get("agent_metrics") or {}).get("total_cost_usd") or 0.0
    print(f"[{time.strftime('%H:%M:%S')}] {arm}: +{len(passed)} pass, "
          f"{len(failed)} fail, {len(pending)} capped  (run {os.path.basename(run_dir)})",
          flush=True)
    return passed, failed, pending, reset, run_dir, cost


def acquire_lock():
    """Refuse to run if another driver is already working this ledger.

    Two concurrent drivers race on state.json (last writer wins) and on the
    arm output dirs, which silently corrupts the tally. This makes that loud.
    """
    os.makedirs(BASE, exist_ok=True)
    lock = os.path.join(BASE, "driver.lock")
    if os.path.exists(lock):
        pid = open(lock).read().strip()
        alive = pid.isdigit() and os.path.exists(f"/proc/{pid}")
        if not alive:  # macOS has no /proc — probe the pid directly
            try:
                os.kill(int(pid), 0)
                alive = True
            except (OSError, ValueError):
                alive = False
        if alive:
            sys.exit(f"another driver is running (pid {pid}); refusing to race. "
                     f"Kill it or remove {lock} if that pid is stale.")
        print(f"clearing stale lock from dead pid {pid}", flush=True)
    open(lock, "w").write(str(os.getpid()))
    return lock


def main():
    lock = acquire_lock()
    st = load_state()
    max_cycles = int(os.environ.get("AB_MAX_CYCLES", "24"))

    # Arms run strictly sequentially: finish A (skilled) — including every cap
    # and interrupt retry — before B (noskill) starts. Interleaving them was
    # what made the 2026-07-25 ledger impossible to untangle after the fact.
    cycles = 0
    for arm in ARM_ORDER:
        while st["arms"][arm]["pending"] and cycles < max_cycles:
            wait = st.get("next_eligible_epoch", 0) - time.time()
            if wait > 0:
                print(f"[{time.strftime('%H:%M:%S')}] token window closed — sleeping "
                      f"{wait/60:.0f} min (resume "
                      f"{time.strftime('%H:%M', time.localtime(st['next_eligible_epoch']))})",
                      flush=True)
                while time.time() < st["next_eligible_epoch"]:
                    time.sleep(min(300, st["next_eligible_epoch"] - time.time() + 1))
                    print(f"  … waiting, "
                          f"{(st['next_eligible_epoch']-time.time())/60:.0f} min left", flush=True)

            cycles += 1
            st["cycles"] = st.get("cycles", 0) + 1
            pend = st["arms"][arm]["pending"]
            passed, failed, still, reset, run_dir, cost = run_arm(arm, pend)
            progressed = bool(passed or failed)   # did anything get a real verdict?
            a = st["arms"][arm]
            a["passed"] = sorted(set(a["passed"]) | passed)
            a["failed"] = sorted(set(a["failed"]) | failed)
            a["pending"] = sorted(still)
            if run_dir:
                a["runs"].append(run_dir)
            a["cost"] = round(a["cost"] + (cost or 0.0), 2)
            save_state(st)
            print(f"  {arm}: {len(a['passed'])} passed / {len(a['failed'])} failed "
                  f"/ {len(a['pending'])} pending  (${a['cost']})", flush=True)

            if a["pending"]:
                # Only back off for an actual token cap. Interrupt / no-result
                # aborts have nothing to do with the 5-hour window and can be
                # retried at once — the blanket hour-long sleep here burned a
                # full idle hour on 2026-07-25 to re-run 2 interrupted tasks.
                if reset:
                    st["next_eligible_epoch"] = reset + 60
                elif progressed:
                    st["next_eligible_epoch"] = time.time() + 30   # transient; retry now
                else:
                    # a whole cycle achieved nothing and no resetsAt surfaced —
                    # something systemic; back off properly rather than spin
                    st["next_eligible_epoch"] = time.time() + 3600
                save_state(st)
        if st["arms"][arm]["pending"]:
            print(f"!! {arm} still has {len(st['arms'][arm]['pending'])} pending after "
                  f"{cycles} cycles — stopping before starting the next arm", flush=True)
            break
        print(f"== {arm} COMPLETE: {len(st['arms'][arm]['passed'])}/{len(ALL_IDS)} ==", flush=True)

    st["done"] = not any(st["arms"][a]["pending"] for a in ARMS)
    save_state(st)
    try:
        os.remove(lock)
    except OSError:
        pass
    print("\n=== Opus 5 A/B ===")
    for arm in ARMS:
        a = st["arms"][arm]
        n = len(a["passed"]) + len(a["failed"])
        rate = len(a["passed"]) / len(ALL_IDS) * 100
        print(f"{arm:9s} {len(a['passed'])}/{len(ALL_IDS)} = {rate:.1f}%  "
              f"(scored {n}, pending {len(a['pending'])}, ${a['cost']})")
    return 0 if st["done"] else 1


if __name__ == "__main__":
    sys.exit(main())
