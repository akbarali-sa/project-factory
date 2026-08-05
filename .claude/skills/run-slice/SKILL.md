---
name: run-slice
description: Operational runbook and troubleshooting playbook for running project_factory slices end to end (python -m project_factory new/run/status/approve). Use this whenever running, resuming, or debugging a project_factory slice run in this repo — especially when a run appears to hang, errors mid-pipeline, or you're unsure whether it's still working or genuinely stuck. Also consult it before approving Gate A/B/C, before touching prisma migrate/reset, or before assuming a slow step is broken.
---

# Running a project_factory slice

This is the field guide for actually *operating* project_factory day to day —
not the pipeline's own logic (that's `project_factory/graph.py` and friends,
already correct). It exists because a full first end-to-end slice run
surfaced a specific, recurring set of failure modes that cost far more time
to diagnose than the actual pipeline work did. Read this before assuming
something is broken.

## The standard runbook

1. `source .venv/bin/activate`, then `python -m project_factory doctor` —
   catches missing tools/wrong versions before you spend anything.
2. `python -m project_factory new <slug> --board <path> --slice-name <name>`
   scaffolds the project. It only writes a **placeholder** scenarios.yaml if
   one doesn't already exist — it will never overwrite a real one you already
   authored or copied in.
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

Before you conclude a run is stuck, check both of these:

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
