---
name: run-slice
description: Operational runbook and troubleshooting playbook for running project_factory slices end to end (python -m project_factory new/run/status/approve). Use this whenever running, resuming, or debugging a project_factory slice run in this repo — especially when a run appears to hang, errors mid-pipeline, resumes after a laptop sleep/crash, or you're unsure whether it's still working or genuinely stuck. Also consult it before approving Gate A/B/C, before touching prisma migrate/reset, before trusting a printed cost figure across a resume, or before assuming a slow step (or a failed e2e) is what it claims to be.
---

# Running a project_factory slice

This is the field guide for actually *operating* project_factory day to day —
not the pipeline's own logic (that's `project_factory/graph.py` and friends,
already correct). It exists because the first two full end-to-end slice runs
surfaced a specific, recurring set of failure modes that cost far more time
to diagnose than the actual pipeline work did. Read this before assuming
something is broken. Longer-form lessons live in the Obsidian vault
(`factory vault`, see the global CLAUDE.md) — search it for the topic before
re-deriving anything here from scratch.

## The standard runbook

0. **Always start and show the dashboard first.** Every slice run (new,
   resume, or debug session) begins by making sure the live fleet dashboard
   is up on **http://localhost:8420/** and visible to the user:
   - Check if it's already listening: `curl -sf http://localhost:8420/ >/dev/null`
     (or `lsof -iTCP:8420 -sTCP:LISTEN`).
   - If not, start it. In Claude Code, use the browser preview tools with the
     `dashboard` entry from `.claude/launch.json` (never a raw Bash background
     process); from a plain terminal it's:
     ```bash
     .venv/bin/uvicorn dashboard.app:app --port 8420
     ```
     run from the factory root.
   - Then open (or reload, if already open) http://localhost:8420/ in the
     browser pane so the user is looking at the **latest** state — after a
     resume or a code change to `dashboard/`, reload rather than trusting a
     stale tab.
   The dashboard is the primary "is it still running?" view during the long
   silent phases below — keep it open for the whole run.
   **Restart the dashboard server after pulling factory changes** — uvicorn
   runs without --reload, so a long-lived dashboard keeps old code in memory.
   Symptom of a stale server (not just a stale tab): the dashboard shows a
   step/cost pinned in the past while the live log clearly progresses — a
   pre-thread-generations dashboard silently watches the dead g1 thread while
   the real run happens on g2. Runs survive the restart
   (runner uses start_new_session=True).
1. `source .venv/bin/activate`, then `python -m project_factory doctor` —
   catches missing tools/wrong versions before you spend anything.
2. `python -m project_factory new <slug> --board <path> --slice-name <name>`
   scaffolds the project. It only writes a **placeholder** scenarios.yaml if
   one doesn't already exist — it will never overwrite a real one you already
   authored or copied in. On a TTY it also asks the human whether to record
   `db_reset_consent` now (the human can pass `--db-reset-consent` up front);
   collect it here, at the keyboard, or the first run will stop mid-pipeline
   at `provision_db` on Prisma's AI-agent gate (see gotchas). An agent driving
   `new` non-interactively must relay that question to the human — it must
   never pass the flag on its own.
3. If you're copying a hand-authored scenarios file into place, use a
   **single-line** `cp` command. A multi-line command with a trailing
   backslash continuation can get mis-pasted into some terminals (the
   continuation line executes as its own bogus command instead of continuing
   the `cp`), silently leaving the placeholder in place. Don't trust a clean
   prompt return — confirm the real file landed:
   ```bash
   head -20 <workspace>/<slug>/specs/<slice-name>.scenarios.yaml
   ```
   and check for the real `slice.id` you expect, not the placeholder's.
4. Always `python -m project_factory run <slug> --dry-run` first. It prints
   the resolved slice, scenario counts (api/web/e2e), and `thread_id` before
   spending a single token — a wrong scenario count here means step 3 failed.
5. Climb the cheap-first ladder rather than jumping to a full run:
   ```
   --until gap_detect     (~free, Haiku)
   [approve Gate A]
   --until baseline       ($0 — starter clones + builds green)
   --until provision_db   ($0 — db up, migrated, seeded)
   --until migrate        (~$1 — contract applied)
   [approve Gate B]
   --until write_tests    (~$2 — THE KEYSTONE: tests exist and fail red)
   ```
   Only after `write_tests` succeeds does a plain `run <slug>` (no `--until`)
   make sense — that's what carries you through the implement/verify/diagnose
   loops to Gate C.
6. **After every `approve`, immediately re-run `status`.** Don't assume the
   graph advanced just because `approve` didn't print an error — an
   interactive prompt or a slow step further down can leave you sitting
   exactly where you started (see below).

## "Still running" vs. actually stuck

This distinction was the single biggest time-sink in the first full run, so
slow down and check before concluding anything is broken.

**Long silence is normal, not a bug.** `baseline` (a real `pnpm build`),
`architect`, `write_tests`, and `implement_*` are all genuine Opus/Sonnet
calls over large prompts — several minutes with zero terminal output is
expected, not evidence of a hang.

First glance at the dashboard (http://localhost:8420/ — step 0 of the
runbook should already have it open; reload it to get the latest state).
Then, before you conclude a run is stuck, check both of these:

**1. Is there actually a live process?**
```bash
ps aux | grep -E "claude -p|pnpm|prisma|turbo" | grep -v grep
```
A `claude -p ...` (or `pnpm`/`prisma`/`turbo`) process that's alive and
consuming some CPU is working, not stuck — regardless of terminal silence.

**2. Inspect the checkpoint directly** — don't rely only on the CLI's
`status`, which only prints a payload for a *real* gate `interrupt()`. A
`--until` stop looks identical to "nothing pending" until you check further:
```python
from project_factory import config as cfgmod, infra
from langgraph.checkpoint.postgres import PostgresSaver
from project_factory.graph import build_graph

project = cfgmod.discover('<slug>')
conn = infra.ensure_state_db(project.cfg['checkpoint_db_url'])
with PostgresSaver.from_conn_string(conn) as saver:
    saver.setup()
    app = build_graph(saver)
    thread = {'configurable': {'thread_id': project.thread_id('<slice_id>')}}
    snap = app.get_state(thread)
    print('next:', snap.next)
    print('log tail:', (snap.values.get('log') or [])[-5:])
    for t in (snap.tasks or ()):
        print('task:', t.name, 'error:', getattr(t, 'error', None))
```
`snap.next` is the node genuinely pending. `log tail` shows the last steps
that actually completed. `task.error` tells you a node crashed rather than
merely being slow.

**The real stuck pattern to recognize:** a subprocess silently waiting on an
interactive confirmation prompt it can never see or answer, because
`infra.py`/`harness.py` capture subprocess output (`capture_output=True`)
instead of streaming it live. A process that's been alive a long time at
near-zero CPU, making no progress, is the tell. Known instances:
`prisma migrate reset` asking `Are you sure? (y/N)`, and `prisma migrate dev`
asking `Enter a name for the new migration` when no `--name` is given. The
starter template (`turborepo-starter-kit@starter-minimal`) already carries
non-interactive fixes for both (`--force`, `--name auto`) — if you hit this
pattern on a *different* script, that's the fix to apply there too.

**If you kill a hung subprocess, kill the whole tree.** Killing the
immediate child (e.g. `pnpm`) does not kill its own children — the actual
`prisma migrate dev` process, or its schema-engine grandchild, become
orphans still holding the prompt open and can interfere with a retry.
```bash
ps aux | grep -E "prisma|schema-engine" | grep -v grep   # find every PID
kill -9 <all of them>
```

## Resuming after a laptop sleep or crash

A run interrupted by sleep produces **three different artifacts** — triage
them separately instead of retrying blindly:

1. **`API Error: Connection closed mid-response`** on an agent call —
   harmless. `run <slug>` resumes from the checkpoint; only the in-flight
   node re-runs, and its partial file edits survive on disk so the retry is
   faster.
2. **A FAIL from a timer that expired during suspend** — Playwright's 120s
   `config.webServer` wait fires "instantly" on wake because wall-clock
   jumped hours. That failure is *not real*. Re-verify with a healthy stack
   before believing it, and **never feed it to `diagnose_*`** — a $5 Opus
   diagnosis of a stale timeout produces a plausible-but-wrong fix plan.
3. **Servers that LISTEN but never answer** — the api/web processes from
   `launch_stack` outlived their parent or wedged during suspend.
   `verify_e2e` now self-heals this (`infra.ensure_stack` health-checks and
   relaunches, killing only listeners owned by the project repo), and server
   output goes to `$TMPDIR/project-factory-{api,web}.log` — `tail` those,
   not the process. If a server is LISTENing, `curl` hangs forever, and the
   process sits at ~0% CPU, that's the wedge signature.

Also on every resume: the checkpoint holds `cfg` **as of thread start**; the
CLI refreshes it from `run.json` on resume, but anything you script by hand
against the checkpoint must do the same or silently ignore edits made while
paused.

## Re-running a finished slice — thread generations

A finished thread can never be cleanly restarted in place: the graph state's
merge/append reducers mean `ingest`'s reset of `attempts`/`diagnosis`/
`phase_out` is a no-op, and `parked`/`log` are never reset — so a naive
rerun parks phases early (stale attempt counts vs `MAX_ATTEMPTS`) and
injects the *previous* run's diagnosis into fresh implementer prompts.

The CLI handles this with **thread generations**: `run` on a slice whose
thread already finished automatically starts a new thread
(`slug:slice_id:g2`, counter in `.factory/state.json` under
`thread_generations`). Nothing is ever deleted — earlier generations stay
in the checkpoint DB, so you can compare cost/attempts across reruns to
measure factory maturity. `status`, `approve`, and the dashboard all follow
the *latest* generation automatically.

`run --fresh` forces a new generation explicitly — the only way to abandon
a paused/in-flight thread (e.g. one poisoned by a sleep artifact) without
checkpoint surgery. To point back at an abandoned generation, decrement the
counter in `.factory/state.json` by hand.

## Recovering by hand — checkpoint surgery

When you (the driver) have already done a pending node's work, don't pay for
an agent re-run. `update_state(..., as_node=...)` writes a node's output as
if it ran, and the graph routes onward:

```python
# continuing from the inspection snippet above
app.update_state(thread, {"log": ["<what you did and why>"]}, as_node="fix_e2e")
# next becomes verify_e2e — the stale pending diagnose_e2e never runs
```

Three proven patterns from real runs:

* **Salvage a crashed node's good output** — e.g. the Test Author wrote all
  tests but died at the commit step: fix the mechanical issue by hand, run
  the node's own checks (`harness.check_traceability`, `check_red_first`),
  commit, then `as_node="write_tests"` with the node's normal return shape.
* **Skip a poisoned diagnosis** — pending `diagnose_*` whose `phase_out` is
  a sleep artifact: `as_node="fix_<phase>"` reroutes straight to re-verify.
* **Record an externally-verified result** — after proving the suite green
  by hand, write `{"status": "green", "phase_out": {...}, "log": [...]}`
  `as_node="verify_e2e"`.

Corollary: when an *agent artifact* (not code) is wrong — e.g. the Architect
described a schema change in comments instead of redeclaring the model —
patch the artifact in state (`update_state(thread, {"contract": fixed})`),
validate with the harness function the pending lint node uses, and resume.
Re-running the validator is free; re-running the generator is not.

## Known gotchas and their fixes

**Docker port collisions.** If other local Postgres containers already run
on 5432, project_factory's shared-instance model (one Postgres, project-
scoped databases, configured via `defaults.json`'s `db_host`/`db_port`/
`db_user`/`db_password`) avoids per-project container sprawl — but confirm
the shared instance is actually reachable via `doctor` before running, and
don't spin up a second dedicated container as a workaround; that's exactly
the fragmentation this design avoids.

**Prisma's own AI-agent safety gate.** `prisma migrate reset` (and
occasionally `migrate dev`) detects it's being invoked by an AI agent and
refuses to run without `PRISMA_USER_CONSENT_FOR_DANGEROUS_AI_ACTION` set to
the human's *literal, verbatim* consent text. This is a deliberate guardrail
against an agent irreversibly wiping a database unsupervised — never
bypass it with an invented or default value. It requires a real human who
has reviewed and approved it for *that specific project's* dev database,
recorded explicitly in that project's own `run.json` as `db_reset_consent`
(never in the shared `defaults.json`, which would silently apply to every
future project without per-project review). If a run errors on this
message, stop and ask the human for explicit consent before setting it.
Both front doors collect this at project creation so runs never stall on it:
CLI `new` prompts on a TTY (or takes `--db-reset-consent` from the human),
and the dashboard's New Project dialog has the equivalent checkbox — both
write the same canned text via `config.record_db_reset_consent()`. The
mid-run stall should now only happen for projects scaffolded before this
existed, or when the human declined at scaffold time.

**A `contract_lint` failure with a suspiciously *empty* generator/datasource
block** (e.g. "provider is missing" on a schema that clearly has one) means
the logic that splices the starter's existing `schema.prisma` with the
Architect's new models mis-extracted the header — not that the Architect's
output is wrong. Treat "well-formed input, malformed extracted output" as a
sign to check the extraction/splicing logic in `harness.py`, not the input
itself.

**The msgpack "Deserializing unregistered type ..." warning** on every run
(for the `Usage`/`Stack` dataclasses) is cosmetic day to day, but is a known
correctness risk specifically for the budget-breaker: custom dataclass state
can silently reset to defaults across a checkpoint resume. Flag it rather
than ignore it if accurate cumulative cost tracking across gate-pauses
matters for a real budget-limited run — don't assume the printed
"cost so far" is trustworthy across a resume without checking.

**The cost counter undercounts across crashed nodes — by design of the
checkpoint, not a bug you can fix at read time.** A node that raises loses
its state update, *including* usage from an agent call that completed inside
it. The first run's checkpoint said $21.57 when the breaker had tripped at
$27.01 and the per-agent log lines summed to ~$35. Consequences: after any
crash/resume the printed figure is a floor; the breaker can trip, then on
resume the checkpointed counter is back under the cap and spends again.
Ground truth is the live log:
```bash
grep -oE '\$[0-9]+\.[0-9]+\)' <project>/.factory/live/<slice>.log \
  | tr -d '$)' | awk '{s+=$1} END {print s}'
```

**The red-first test commit uses `--no-verify` deliberately.** Red tests
import modules that do not exist yet — that is the whole point — so the
starter's pre-commit typecheck can never pass them. `write_tests` commits
with `verify=False`; implementation commits keep the hook. Don't "fix" this
by making the hook pass, and don't extend `--no-verify` to other commits.

**Headless agents cannot run commands** (current limitation): every
`pnpm`/`vitest`/server invocation inside a `claude -p` agent is denied by
the sandbox, so implementers/diagnosticians work from static analysis only.
Expect blind first attempts and budget accordingly until the allowed-tools
fix lands in `models.py`'s `claude()`.

**Starter-kit traps that break e2e in every generated project**
(`turborepo-starter-kit@starter-minimal`, reference fixes in the barcode
repo, commit `cfe3935`):
1. `Login.test` compares the raw `{appName}` i18n template against the
   rendered heading — always fails; compare the resolved string.
2. The login form's demo prefill (`mark.s@example.com`, a WORKER) wins over
   any `fill()` that lands before React hydrates — the test "signs in fine"
   but *as the wrong user*, then admin-gated pages 404. WebKit/Mobile Safari
   hit it every time. Sign-in helpers must verify the issued token's
   identity and retry, not just wait for the post-login redirect.
3. `pnpm db:reset` does not reliably seed — always chain
   `pnpm init-db:force` or every login 401s afterwards.
4. The API's JwtStrategy prefers the `jwt` cookie over the `Authorization`
   Bearer header — client-side role checks must fetch with
   `credentials: "omit"` or a stale cookie silently wins.

## Reviewing at a gate

Gate A and Gate B payloads get truncated in the terminal (long values are
cut at ~1500 chars with a `(+N chars)` note) — don't approve off the
truncated view when the ASK explicitly says to check something (e.g. Gate
B's "check the schema additions against the starter's conventions"). Pull
the full value from the checkpoint first:
```python
# continuing from the snippet above
print(snap.values['contract'])   # or 'gaps', 'scenarios', etc.
```
