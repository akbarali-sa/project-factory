# Project Factory

Turns vetted presales artifacts (EventStorming board + WBS) into generated,
test-passing vertical slices in a NestJS + Next.js + Expo monorepo — behind
human approval gates.

**We don't build a code generator.** We orchestrate a proven one (Claude Code)
behind a gated LangGraph spine. The IP is the presales-to-contract pipeline, the
independent test oracle, and the gates.

```
ingest(det) → gap_detect(HAIKU) → [GATE A: spec + scenarios]
→ clone_starter(det) → provision_db(det) → baseline(det) → commit_specs(det)
→ architect(OPUS) → contract_lint(det) → [GATE B: contract freeze]
→ migrate(det) → write_tests(SONNET) → red_first(det)
→ implement_api → verify_api(det) ⟲ diagnose(OPUS)   [≤3, then park]
→ implement_web → verify_web(det) ⟲ diagnose(OPUS)   [≤3, then park]
→ launch_stack(det) → verify_e2e(det) ⟲ diagnose(OPUS)
→ teardown(det) → Draft PR → [GATE C: review] → END
```

Only **five** nodes use a model. Everything else — retrieval, scheduling, git,
docker, migrations, test execution — is deterministic code.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

claude setup-token          # subscription login; keep ANTHROPIC_API_KEY UNSET
docker compose -f docker/docker-compose.state.yml up -d    # checkpoint DB :5433

python -m project_factory doctor
```

## First slice

```bash
python -m project_factory new barcode-mvp \
    --board ~/Downloads/barcode-sorting-inventory-mvp.board.json \
    --slice-name container-packing-list

# author the scenarios — THE ORACLE. Highest-value work in the pipeline.
$EDITOR ../projects/barcode-mvp/specs/container-packing-list.scenarios.yaml

python -m project_factory run barcode-mvp --dry-run
```

### Cheap-first ladder — don't run the whole graph on attempt one

| Command | Proves | Cost |
|---|---|---|
| `run <slug> --until gap_detect` | IR parses, gaps found | ~free |
| `run <slug> --until baseline` | starter clones at pinned ref, builds green | $0 |
| `run <slug> --until provision_db` | postgres up, migrated, users seeded | $0 |
| `run <slug> --until migrate` | contract validated, schema applied | ~$1 |
| `run <slug> --until write_tests` | **KEYSTONE: tests exist and fail red** | ~$2 |
| `run <slug>` | full slice, app on `localhost:3000` | measured |

If tests *pass* against an empty implementation, stop — they assert nothing and
the oracle is a placebo. Fix the Test Author before spending more.

## Gates

Runs pause at Gate A/B/C. State is in Postgres, so a pause can last hours or
survive a reboot.

```bash
python -m project_factory status  barcode-mvp     # where is it paused, and why
python -m project_factory approve barcode-mvp
python -m project_factory approve barcode-mvp --reject --note "SC-003 unclear"
```

`thread_id` is derived as `<slug>:<slice_id>`, so `run` on an interrupted slice
resumes from the checkpoint instead of starting over.

## Layout

The workspace lives **outside** this repo — nested git repos confuse coding
agents that walk up the tree to find the repo root, and separation makes it
impossible to commit client code into this repo.

```
~/dev/
├── project-factory/            this repo
│   ├── project_factory/        config · graph · models · infra · repo · harness
│   ├── docker/                 checkpoint postgres
│   ├── defaults.json           true for EVERY project
│   └── .cache/starter.git      bare mirror of the starter (gitignored)
└── projects/                   sibling
    └── barcode-mvp/
        ├── run.json            overrides only → { "budget_usd": 25 }
        ├── specs/              *.board.json + *.scenarios.yaml
        ├── .factory/state.json completed slices
        └── repo/               GENERATED: git init, no starter history
```

Override the workspace with `--workspace` or `$PROJECT_FACTORY_WORKSPACE`.

## Starter

`akbarali-sa/turborepo-starter-kit @ starter-minimal` — Postgres/Prisma, auth,
24 UI components, `AGENTS.md` + skills, CI. Set once in `defaults.json`.
**Pin `starter_ref` to a tag** once it stabilises; a moving branch means two runs
a week apart aren't the same experiment.

## Guardrails

- **Independent oracle** — scenarios are human-approved, tests are committed
  before implementation, and the Implementer has `src/` write access with
  tests read-only (deny-flags **plus** a `git diff` audit, because a renamed CLI
  flag must never silently disable the protection).
- **Budget breaker** — per-run ceiling from measured `total_cost_usd`; aborts
  rather than overrunning. Cost by agent is reported at Gate C.
- **Portability** — `harness.check_prompt_leak()` fails CI if domain nouns
  appear in factory code. Domain facts live in the IR; prompts hold only
  conventions. That's what makes project #2 a config swap.
- **Park, don't halt** — a phase that fails 3× is parked and escalated; the run
  continues.

## Docs

- `docs/REPO-LAYOUT.md` — layout, seeding strategy, Spec Kit decision, runbook
- `docs/project-factory-build-plan.md` — phased plan, agent/test spec, risks
