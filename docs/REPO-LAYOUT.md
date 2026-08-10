<!-- factory:case-study -->
# Repo layout, seeding, Spec Kit decision, and the first-slice runbook

> **Historical case study — project #1 (barcode sorting).** The layout and
> seeding decisions here are factory-generic, but every worked example uses
> project #1's domain. Do not treat its fixtures, aggregates, or scenarios
> as templates for a new project — a new project's vocabulary comes from
> its own board, nowhere else.

Updated for **`akbarali-sa/turborepo-starter-kit @ starter-minimal`** as the default starter for every project.

---

## 0. What your fork changes (and it's a lot)

Reading the branch changed several assumptions in the factory code. Your fork already did work I had planned as factory tasks:

| Thing | Status in your fork | Impact on the factory |
|---|---|---|
| PostgreSQL + Prisma | **Done** — migrated off Mongo | Drop the entire Mongo→Postgres task from the plan |
| Demo domain | **Stripped** — `schema.prisma` has `User` only | Nothing to delete before generating; clean canvas |
| Auth | **Done** — Passport/JWT, httpOnly cookie on web, SecureStore on native, email-only | The single most expensive thing to rebuild; inherited free |
| Postgres tooling | **Done** — `db:up/migrate/reset/studio/init-db` + its own compose | **Deleted my custom app DB compose** — never reimplement the starter |
| Seeding | **Done** — `init-db` seeds 3 demo users | Layer 1 seeding solved; only domain fixtures remain |
| Conventions | **Documented** — cuid ids serialised as `_id`, join tables, cascade rules, `nullIfMissing()`, `$transaction` | This *is* the `constitution.md` layer — Claude Code reads it in-repo |
| CI | **Done** — Actions with a Postgres service, Codecov, SonarQube | Full verification gate is pre-wired |
| Dockerfiles for api/web | **Absent** | My app compose was wrong — corrected (see §3) |

One **blocking gap** the branch surfaced: the `User` model is `id, email, name, createdAt, updatedAt` — **no role field.** Scenarios SC-007 and E2E-004 need Admin vs Worker, so the Architect must *add* a Role. This is now flagged explicitly at Gate B as `extends_template_schema`, because it's the one place this slice modifies the template's auth model rather than adding alongside it.

---

## 1. Layout — the workspace lives OUTSIDE the factory

```
~/dev/
├── project-factory/                  the tool — its own git repo
│   ├── project_factory/              graph · config · infra · repo · harness · models
│   ├── docker/                       optional app-stack compose (not the DB)
│   ├── defaults.json                 true for EVERY project — incl. shared postgres
│   └── .cache/starter.git            bare mirror of your fork   (gitignored)
│
└── projects/                         SIBLING — never inside the factory
    └── acme-crm/
        ├── run.json                  overrides only  →  { "budget_usd": 25 }
        ├── specs/
        │   ├── acme-crm.board.json         presales IR
        │   └── *.scenarios.yaml           one file per slice
        ├── .factory/state.json       completed slices (factory-written)
        └── repo/                     GENERATED: git init, no starter history
            ├── apps/api · apps/web · apps/mobile · packages/*
            └── specs/                committed copy of the above
```

**Why the workspace is not nested inside the factory:**

1. **Nested git repos are hostile.** `repo/.git` inside the tool confuses IDEs, ripgrep, and — most importantly — coding agents that walk *up* the tree to find the repo root. An agent can end up resolving the wrong repo entirely.
2. **Blast radius.** One `git add -f` or one misbehaving tool away from committing a client codebase into your tool's history. Separation makes it impossible, not merely unlikely.
3. **Different lifecycles.** Client code may carry retention/encryption obligations — and some agreements would frown on co-location.
4. **Tooling noise.** Anything run at factory root would crawl generated `node_modules` and Turborepo caches.
5. **CI.** The workspace wants to be a mounted volume, not part of the checked-out tool.

**Why `repo/` nests inside the project dir rather than *being* it:** inputs (board, scenarios, `run.json`) must exist *before* the repo is created. Separating human-owned inputs from factory-owned output removes that ordering problem entirely.

Two invariants:

- **Squash on materialise** — `create_project_repo()` drops `.git` and re-inits, so a client repo carries no starter history and no upstream coupling. It is a new repo, not a fork.
- **Specs are copied into the repo** — source of truth is `projects/<slug>/specs/`, but the generated repo gets a committed copy so it stays self-contained and auditable in a PR.

Your fork's `sync-upstream.yml` keeps `main` mirroring john-data-chen weekly; `starter-minimal` is your work branch. The factory only reads `starter-minimal` — **pin it to a tag** once it stabilises.

---

## 1b. Discovery and config — how little you have to type

Everything is inferred from the layout, so `run.json` holds only genuine overrides:

| Resolved | How |
|---|---|
| workspace root | `--workspace` > `$PROJECT_FACTORY_WORKSPACE` > `defaults.json` > `../projects` |
| board | single `specs/*.board.json` (errors clearly on 0 or 2+) |
| slices | every `specs/*.scenarios.yaml`, ordered by `slice.wave` then filename |
| repo | `<project>/repo/` |
| `thread_id` | derived `<slug>:<slice_id>` — so **resume is automatic**, never dependent on remembering an id |
| next slice | first not listed in `.factory/state.json` |
| everything else | `defaults.json`, overridden per project |

```bash
python -m project_factory list                          # what exists, what's done
python -m project_factory new acme --board ~/x.board.json
python -m project_factory run acme --dry-run            # resolve + print, spend nothing
python -m project_factory run acme                      # next unstarted slice
python -m project_factory run acme --slice orders       # or a specific one
python -m project_factory doctor                        # toolchain preflight
```

`--dry-run` prints the resolved plan (workspace, board, slices with scenario counts, starter ref, budget, thread id) and exits. Always run it first — it costs nothing and catches a wrong board file before tokens.

Completing a slice records it in `.factory/state.json` and prints the next one, so `run <slug>` repeatedly walks the waves in order.

---

## 2. Seeding — yes, every project, in two layers

**Layer 1 — template identity (already built).** `pnpm db:reset` = drop + re-migrate + re-seed, giving three sign-in-able users every run:

```
john.doe@example.com · jane.doe@example.com · mark.s@example.com
```

Email-only login, no passwords — so E2E can authenticate with no secret management. Run `db:reset` **before every slice** and you get an identical starting database each time. This is the single most valuable determinism lever in the whole pipeline: a failing test is then about the code, never about leftover rows.

**Layer 2 — domain fixtures, owned by tests, not by a global seed.** This is a deliberate choice, and worth understanding:

- Scenario SC-001 asserts *"no container exists with C-1001."* A global domain seed would make that assertion false before the test runs.
- Shared mutable seed data creates order-dependent suites — the classic "passes alone, fails in CI" bug.

So each test creates the rows it needs in its own arrange step. The `.xlsx` fixtures (valid, duplicate-barcode, missing-column, malformed-quantity, empty) are generated as **real files** under `apps/api/__tests__/fixtures/` — not mocked, because parsing is exactly what's under test.

**One addition you will need:** once Role is added for SC-007, the seed must produce at least one Admin and one Worker. That's a small edit to the starter's `apps/api/database/seed.ts`, and it should go into the *fork* (so every future project inherits it), not into a single generated project.

---

## 3. Docker: Postgres yes, apps no (for slice 1)

You asked to run backend and frontend in Docker. Here's the honest tradeoff, and why I only half-did it.

**Postgres runs in Docker** — via the starter's own `apps/api/database/docker-compose.yml` and `pnpm db:up`, namespaced per project with `COMPOSE_PROJECT_NAME=pf-<slug>`.

**api + web run as local processes**, because the starter has **no Dockerfiles** for them. Adding them means solving pnpm-workspace container builds, and the traps are real:

- you need `turbo prune --docker --scope=@repo/api` or image builds are huge and cache-hostile
- `lightningcss`, `oxlint` and the Prisma query engines are **compiled per-architecture** — bind-mounting host `node_modules` into a container breaks confusingly on arm64-vs-amd64
- Next.js needs `output: 'standalone'`; Prisma needs `generate` in-image and `migrate deploy` before boot

None of that de-risks the *factory*, and the starter already ships `start-server-and-test` for exactly this. **E2E oracle strength is identical** — Playwright still drives a real browser against real HTTP against real Postgres. The only thing you lose is container parity with production, which matters at deploy time, not at slice 1.

`docker/docker-compose.app.yml` now holds a documented sketch plus the trap list, clearly marked unused, for when you do want it.

### How deterministic is this?

Deterministic *enough*; not bit-for-bit. Controlled: pinned Postgres image, `pnpm install --frozen-lockfile`, `TZ`/`PGTZ=UTC` everywhere, `db:reset` before every slice, healthcheck polling instead of `sleep`, auto-incrementing ports. Not controlled: image pulls need network (pin by digest to fix retag drift), arm64-vs-amd64 native deps, and Playwright browser versions — pin those in `package.json`.

Also pinned by `preflight()`: **pnpm 11.x and Node ≥ 24**, matching the fork's `engines`. A different pnpm major resolves the lockfile differently, which silently destroys reproducibility.

---

## 4. GitHub Spec Kit — revised: take the vocabulary, skip the tool

I recommended adopting Spec Kit earlier. Having seen both what it does and what your fork already provides, I'd change that.

Spec Kit ships a `specify` CLI writing `constitution.md`, `spec.md`, `plan.md`, `tasks.md`, wiring `/speckit.specify → /plan → /tasks → /implement` into a coding agent. Two problems here:

- **`/specify` is redundant.** Its job is turning a vague request into a structured spec. Your presales architect already produces something *better*: a machine-readable board with typed view parameters, a 22-entry decision log with named stakeholders, and use cases traced to board event IDs. Running `/specify` would regenerate a worse spec from a better one.
- **`/implement` fights your orchestrator.** It drives the agent itself, bypassing your gates, Postgres checkpointing, per-phase retry caps, budget breaker and test write-protection — i.e. bypassing the part that *is* the value.

| Spec Kit artifact | Your equivalent | Verdict |
|---|---|---|
| `constitution.md` | your fork's `AGENTS.md` + `.agents/skills/` + `ai-docs/api-context.md` + schema convention comments | **already have it, in-repo** |
| `spec.md` | `board.json` + `scenarios.yaml` | already have, and better |
| `plan.md` | Architect's OpenAPI + Prisma output (Gate B) | already have |
| `tasks.md` | deterministic wave/slice plan (topo sort) | already have; deterministic beats generated |

**Call: skip the CLI entirely for now.** Your fork's `AGENTS.md` + `ai-docs/` already do what `constitution.md` would, and Claude Code reads them natively. Revisit only if you catch yourself hand-rolling something it solves.

---

## 5. Runbook — first slice

```bash
# 0. one-time factory setup
git init project-factory && cd project-factory
# copy in project_factory/  docker/  defaults.json
printf '.cache/\n__pycache__/\n.venv/\n' > .gitignore     # note: no workspace/
python -m venv .venv && source .venv/bin/activate
pip install langgraph langgraph-checkpoint-postgres pyyaml "psycopg[binary]"

# Claude Code on subscription login — keep ANTHROPIC_API_KEY UNSET
claude setup-token

# 1. ONE shared Postgres for every local project (pgvector-based, :5432,
#    postgres/postgres by default — see defaults.json) must already be running

# 2. toolchain + env check (Node >= 24, pnpm 11.x, docker, claude, no API key)
python -m project_factory doctor

# 3. scaffold — creates ../projects/acme-crm/{run.json,specs/}
python -m project_factory new acme-crm \
    --board ~/Downloads/acme-crm.board.json \
    --slice-name customer-onboarding

# 4. author the scenarios — THE ORACLE, the highest-value work in the pipeline
$EDITOR ../projects/acme-crm/specs/customer-onboarding.scenarios.yaml

# 5. free checks before spending a token
python -m project_factory.harness                    # prompt-leak -> must be 0
python -m project_factory run acme-crm --dry-run  # resolve + print, no spend

# 6. run (next unstarted slice; repeat to walk the waves)
python -m project_factory run acme-crm
```

### Five steps, not one big bang

| Step | Scope | Proves | Cost |
|---|---|---|---|
| **1** | `ingest` → `gate_spec` | IR parses, gaps found, gate pauses | ~free (Haiku) |
| **2** | + `clone_starter` → `baseline` | Fork clones at pinned ref, `check-types` + `build` green **before** generation, so later red is attributable | $0 |
| **3** | + `provision_db` | `db:up` + install + `db:reset`, 3 users seeded | $0 |
| **4** | + `architect` → Gate B → `migrate` | Contract validates, models **append** to schema (User survives), migration applies | ~$1 |
| **5** | + `write_tests` → `red_first` | **THE KEYSTONE** — generated tests must fail red on an empty implementation | ~$2 |
| **6** | + api → web → e2e loops | Full slice, app running at `localhost:3000` | measured |

Steps 2–4 are free or near-free and de-risk the boring-but-fatal infrastructure. **Step 5 is where you learn whether the whole approach is sound.** If tests pass with no implementation, stop and fix the Test Author before spending anything on step 6.

---

## 6. What to watch on the first run

- **`ORACLE VIOLATION`** — the Implementer touched a test file. Investigate; never suppress. Belt-and-braces: the CLI deny-flags *and* a `git diff` audit, because a silently renamed flag must not silently disable oracle protection.
- **`red-first failed`** — tests pass with no implementation, so they assert nothing. Regenerate with stricter instructions.
- **Schema didn't append** — if `User` vanished from `schema.prisma`, the append logic failed and auth is broken. The `migrate` node skips any model the Architect re-declares, so the starter always wins.
- **Cost by agent** at Gate C — the number that replaces the indefensible "$15/project".
- **Attempts per phase** — if api/web routinely hit 3 attempts, your conventions layer is too thin. That's a spec problem, not a model problem.
- **`parked` phases** — escalation, not failure. A stuck phase must never halt the run.
