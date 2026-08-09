---
name: run-project
description: Operational runbook for running a COMPLETE project_factory project from a single board JSON (python -m project_factory run-project) — planning, agent-drafted oracles, project-level gates, cross-slice merges, and the $100+ project budget. Use when running, resuming, or debugging a full-project run, approving the project plan, or when a slice-level question arises inside a project run (then defer to the run-slice skill for that layer).
---

# Running a complete project from one board

`run-project` drives every slice of a project from a single `*.board.json`:
plan → draft oracle → build slice → verify → merge → next slice. This skill
covers the PROJECT layer. Everything about a single slice's internals —
stuck-vs-slow diagnosis, checkpoint surgery, sleep artifacts, prisma gotchas —
is the `run-slice` skill and applies unchanged to every slice a project run
drives; read it first if you haven't.

## Before you debug anything: query the graphs, don't rediscover

The first full project run lost roughly **8 of 18 hours** to failures already
described in the vault, because the notes were written after each incident
and never read before the next one. Do these three things FIRST — they cost
seconds:

1. **Vault** — search `factory vault` for the symptom before reasoning about
   it. The whole class of "every e2e test fails" is already solved there:
   `Lessons/When every e2e test fails, suspect the stack not the code`,
   `Lessons/Agent-authored e2e tests fail in a few recurring mechanical ways`,
   `Lessons/Hydration races are a real product risk in generated UIs`.
2. **graft in the GENERATED repo** — `finish` now runs `graft build` there
   after every slice, so `graft ask "where is X" --source`, `graft grep`,
   `graft callers` all work inside `<workspace>/<slug>/repo`. Use them
   instead of grepping; the debugging happens in that repo, not the factory.
3. **graft in the factory repo** — same, for pipeline questions.

> [!warning] The specific trap
> An e2e failure list that includes PREVIOUSLY DELIVERED slices' tests is
> almost never a regression — it means the app under test is not running, or
> is not the app you think. Check the stack (`lsof -iTCP -sTCP:LISTEN -P |
> grep :300`, then curl the web URL and confirm it returns HTML, not a JSON
> `statusCode`) before reading a single assertion. Three slices' entire
> diagnose budgets went to this.

## What changes vs. slice mode — read this first

* **The oracle is agent-drafted, not hand-authored.** An Opus `oracle_author`
  drafts each slice's scenarios.yaml from the board + plan, validated
  mechanically (structure, traceability, ≥3 scenarios, ≥1 e2e). The human
  accountability points are the PLAN (explicitly approved, attributable) and
  GATE C per slice. This is a deliberate trade; for a slice where correctness
  is subtle, hand-author its scenarios file BEFORE run-project reaches it —
  an existing file is never overwritten.
* **Gate policy defaults to auto A+B, human C**
  (`config.PROJECT_GATE_POLICY`). Override per run with
  `--gates spec=human,contract=human` or persistently via `"gates"` in
  run.json. Plain `run` keeps all gates human regardless.
* **Approved slices merge into main** (`merge_slice` node, --no-ff) so slice
  N+1 builds on delivered code. The per-slice branch survives for archaeology.
* **One project budget** (`project_budget_usd`, default $100) across
  planning, oracle drafting, and every slice. Each slice thread gets the
  REMAINING balance as its own breaker ceiling. Per-slice actuals land in
  `.factory/state.json` `slice_costs` from the LIVE LOG (ground truth), not
  the undercounting checkpoint counter.

## The standard flow

```bash
python -m project_factory new <slug> --board <file>.board.json --project \
    --db-reset-consent        # human decision — see run-slice skill
python -m project_factory run-project <slug>          # plans, then stops
# review/edit specs/project-plan.json — THE project-level gate
python -m project_factory approve-plan <slug> --by "<your name>"
python -m project_factory run-project <slug>          # drafts + builds
```

Dashboard equivalents: New project → tick **Full project (run-project)**;
the project card then grows a banner with plan state, budget bar, and
**Approve plan** / **Run project** buttons. Keep the dashboard open
(http://localhost:8420/) — step 0 of run-slice applies here doubly, since a
project run is hours long.

`run-project` is **re-entrant**: it advances to the first decision a human
owns (plan approval, Gate C, a park, budget exhaustion) and exits. Approving
Gate C — CLI `approve` or dashboard — CHAINS automatically into the next
slice (`_cmd_approve` → `_cmd_run_project`), so after the plan is approved
the only human actions in the happy path are one Gate C review per slice.

## Reviewing the plan (the gate that matters most)

The plan decides what gets built, in what order, and what is explicitly NOT
built. Check before approving:

1. **Every hard-10% event is parked with an honest reason** — offline sync,
   conflict resolution, dropout recovery belong in `out_of_scope`, not wave 3.
2. **Wave order respects data dependencies** — a slice may consume only
   earlier waves' aggregates.
3. **Events with unanswered blocking questions** should not be in a slice
   (the oracle author will otherwise have to guess); expect them parked or
   provisional.
4. Edit the JSON directly if wrong — it's a plain file; `approve-plan`
   records whatever is on disk at that moment.

## Budget expectations (calibration from real runs)

The barcode slice cost $25.74 solo (api 1 attempt, web 1, e2e 3). Planning
~$1, an oracle draft ~$1–3. A 3–4 slice project therefore fits a $100 budget
only if most slices pass early; raise `project_budget_usd` in run.json
before starting if the board is bigger, not mid-run. When the breaker trips
mid-slice, the run-slice skill's budget-recovery pattern applies (verify by
hand, surgery, resume with the guard) — and remember the recorded project
spend is `max(live log, checkpoint)` per slice, so post-crash accounting
stays honest.

## Failure modes specific to project runs

* **Oracle draft fails validation twice** → run-project exits with the
  errors. Either fix the plan (the slice's events genuinely underspecify
  behaviour) or hand-author that scenarios file and re-run.
* **A slice parks** → run-project stops (Gate C shows parked phases; auto-pr
  policy refuses non-clean slices by design). Fix by hand at the slice layer
  (run-slice skill), get it green, approve, and run-project continues.
* **Slice N+1 baseline fails after N merged** → the merge itself broke the
  build (should be impossible with green-only merges; suspect uncommitted
  generated files). `git -C repo log --oneline` and build by hand.
* **Never run two run-projects concurrently** — the 'project' pidfile guards
  the button, but the real protection is per-slice run locks; a second
  driver stealing a slice mid-flight is the dashboard-resume race from
  2026-08-07 at project scale (see vault:
  [[Deleted project dirs orphan the thread-generation counter (2026-08-07)]]
  §also-learned).
* **Ghost threads after deleting a project dir** — same trap as slice mode
  (generation counter lives in the deleted dir); drop the checkpoint DB or
  bump generations before rerunning a same-slug project.

## Watching a run

* Project layer logs to `.factory/live/project.log` (planner + oracle
  author); each slice logs to its own `<slice_id>.log` as usual.
* Dashboard overview: planned-but-undrafted slices show as ghost "Planned"
  rows; the banner's budget bar turns red past 85%.
* `python -m project_factory run-project <slug> --dry-run` prints the plan,
  budget, and gate policy without spending.
* `--max-slices N` checkpoints a long project into sessions.
