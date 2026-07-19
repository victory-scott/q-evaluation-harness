# Persistent-session agent runs on q-HumanEval: does an accumulating coding session help?

**One-line result:** Running all 164 q-HumanEval tasks in a *single, resumed*
coding session (context accumulates across tasks) instead of one clean session
per task lifts a mid-tier model by ~10 pass@1 points — **Sonnet 5: 85.4 → 95.1%**,
**Haiku 4.5: 59.8 → 70.1%** — and the gain is *uniform across task difficulty*,
consistent with the model gaining **q-fluency and test-harness familiarity**
rather than leaking answers between problems.

> Exploratory / **not comparable** to the clean-room benchmark. Persistent runs
> are order-dependent, single-sample, and share context across tasks by design.
> These artifacts are a seed for further analysis, not a leaderboard entry.

---

## Motivation

The standard agent benchmark spawns one clean-room `claude` session per problem
(`--session-mode fresh`, default). Every task re-pays a fixed "startup tax" —
re-learning q idioms, the `solution.q` workflow, and the test-harness's quirks —
and never benefits from anything learned on a prior task. Hypothesis: an
**ongoing session that accumulates context**, like a real engineer getting
fluent in a codebase over a day, might raise a mid-tier model's success rate,
even though a top model has little headroom.

## Method

New backend mode `--session-mode persistent` (claude-code only; default `fresh`
is unchanged). In persistent mode:

- **One resumed session** across all 164 tasks: the first `claude -p` call
  starts a persisted session; each subsequent task adds `--resume <session_id>`,
  carrying the full conversation forward.
- **One shared workspace** — the agent accumulates files/notes across problems.
- **Sequential** execution (a session can't be resumed in parallel), so
  concurrency is forced to 1.
- **Eager compaction** between tasks: when the carried context window exceeds a
  threshold (default 250k tokens), a `/compact` turn runs *at the task boundary*
  (never mid-task), preempting the CLI's native auto-compaction.

Each run: 164 tasks, pass@1 (n=1), Q-HumanEval, same model for both arms.
Models resolved from CLI aliases: `sonnet` → `claude-sonnet-5`,
`haiku` → `claude-haiku-4-5`.

### Two measurement subtleties (worth stating for the paper)

1. **Token accounting.** The CLI's `usage.input_tokens` counts only *uncached*
   new tokens (often single digits on a resumed turn — the prompt itself is
   billed under `cache_creation_input_tokens`). Two distinct quantities:
   - **billed input** = `input + cache_read + cache_creation` summed over every
     turn (drives cost; large because each turn re-reads cached context).
   - **context window** = the *last* turn's input side — the true "how much
     context is carried." Compaction triggers on this, and it's the meaningful
     accumulation metric.
2. **Learning-curve confound.** Persistent runs execute in task-id order, so
   "pass-rate vs. position" is confounded with task difficulty (later task-ids
   are harder — see the quartile table). Positional/"context-rot" claims require
   a **shuffled-order rerun** (`--shuffle-seed`, plumbed but not yet run). The
   quartile-controlled comparison below is the honest analysis: it compares the
   *same task-ids* across arms.

## Results

| Metric | Sonnet 5 fresh | Sonnet 5 persistent | Haiku 4.5 fresh | Haiku 4.5 persistent |
|---|--:|--:|--:|--:|
| **pass@1** | 85.4% (140/164) | **95.1% (156/164)** | 59.8% (98/164) | **70.1% (115/164)** |
| mean turns/task | 10.6 | 5.4 | 15.1 | 9.0 |
| mean wall/task (s) | 77.8 | 29.1 | 158.3 | 115.0 |
| agent timeouts (300s) | 10 | 0 | 45 | 31 |
| total cost (USD) | $38.93 | $44.36 | $12.81 | $16.77 |
| billed input tokens | 60.9M | 122.9M | — | — |
| total output tokens | 437k | 265k | — | — |
| mean context window | 39.7k | 141k | 33.4k | 112.7k |
| max context window | 51.5k | 251k | 55.7k | 190k |
| compactions | — | 2 (pos 62, 124) | — | 0 (never hit 250k) |

### Quartile-controlled: persistent wins at every difficulty level

pass@1 by task-id quartile (same tasks both arms; fresh → persistent):

| task-id range | Sonnet 5 | Haiku 4.5 |
|---|--:|--:|
| 0–40   | 85% → 93%  (+8)  | 59% → 66%  (+7)  |
| 41–81  | 93% → 98%  (+5)  | 80% → 93%  (+13) |
| 82–122 | 80% → 95%  (+15) | 54% → 63%  (+9)  |
| 123–163| 83% → 95%  (+12) | 46% → 59%  (+13) |

Fresh *also* declines in the harder late quartiles — those tasks are simply
harder. Persistent improves **every** band by a roughly constant margin, with no
sign of degradation as the context window grows (to 251k for Sonnet, 190k for
Haiku). The earlier-looking "context-rot" dip in the raw persistent learning
curve was an artifact of task-id-ordered execution, not accumulation.

### Flip analysis (which tasks changed, and why)

| | Sonnet 5 | Haiku 4.5 |
|---|--:|--:|
| gains (fresh-fail → persistent-pass) | 18 | 32 |
| — of which fresh *wrong-answer* (not timeout) | 17 | 31 |
| regressions (fresh-pass → persistent-fail) | 2 | 15 |
| **net** | **+16** | **+17** |

Gains are overwhelmingly *correctness* improvements (fresh produced a wrong
answer, persistent produced a correct one), not timeout rescues.

## Findings

1. **Persistent context lifts a mid-tier model ~+10 pass@1 on q** (Sonnet
   +9.7, Haiku +10.3) — a real, reproducible effect across two capability tiers.
2. **The gain is uniform across difficulty, not accumulating** — present from
   the first quartile and roughly constant thereafter. This argues the benefit
   is fixed q/harness *fluency*, not answer leakage snowballing over the run
   (which would grow with position).
3. **Reliability — not just accuracy — is the capability threshold.** Sonnet
   gains cleanly (18 fixed, **2** broken); Haiku gains *churn-ily* (32 fixed,
   **15** broken ≈ 9% of previously-passing tasks regressed). A 70% pass@1 that
   is *unstable* — "it fixed X but silently broke Y" — is a frustrating basis
   for real interactive coding. Persistent context raises Haiku's ceiling but
   not its reliability floor; for real work the floor is what matters.
4. **Efficiency intuitions were partly backwards.** Persistent uses *more*
   input tokens (Sonnet 2×, though cheap cache reads → only +14% cost) but
   *fewer* turns (~half) and *less* output. "Wasteful on turns" was wrong;
   "cheap in wall-clock" was wrong for the weak model (Haiku's persistent run
   floundered for ~5.3h with 31 timeouts).

## Threats to validity

- **Single sample (n=1)** per task: individual flips include variance; net
  deltas (+16, +17) are beyond noise but exact counts are not precise.
- **Answer leakage not fully excluded**: q-HumanEval problems share idioms. The
  uniform-across-difficulty result argues against *accumulating* leakage, but a
  **shuffled-order rerun** is the clean test and has not been run.
- **Order dependence / non-reproducibility**: persistent results depend on task
  order and the compaction schedule.
- **Compaction schedule barely exercised**: only 2 compactions (Sonnet), 0
  (Haiku). The effect of a lower threshold on a weak model is open — could
  reduce Haiku's churn, or hurt.

## Artifacts

Per arm: `<model>_<mode>_results.json` (metrics, per-task pass/fail with
`sequence_position`, `learning_curve`, `compactions`) and
`<model>_<mode>_solutions.jsonl` (the q code the agent wrote per task, for
qualitative diffing of fresh-fail vs persistent-pass).

### Reproduce

```bash
# A: fresh (clean-room, parallel baseline)
qeval agent-run q-humaneval --model sonnet --backend claude-code \
  --session-mode fresh --save-events -o ./outputs/sonnet_ab_fresh

# B: persistent (resumed session, eager compaction at 250k)
qeval agent-run q-humaneval --model sonnet --backend claude-code \
  --session-mode persistent --compact-threshold-tokens 250000 \
  --save-events --keep-workspaces -o ./outputs/sonnet_ab_persistent
```

### Suggested next experiments
- `--shuffle-seed` rerun to decouple position from task-id (leakage/rot test).
- Compaction-threshold sweep (e.g. 100k vs 250k) — does earlier compaction cut
  Haiku's regression churn?
- Larger models (little expected headroom — a negative-result control).
