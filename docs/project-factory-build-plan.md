# Project Factory — Realistic Build Plan (v1: Walking Skeleton)

**Author:** Akbar Ali
**Date:** 23 July 2026
**Audience:** CTO
**Scope of this plan:** Prove the pipeline end-to-end with the *thinnest possible* working version, then iterate. This is deliberately **not** a plan to build the full factory in one shot.

**Core principle: assemble, don't build.** We do not build a code generator. We orchestrate proven open-source pieces — **Claude Code** (the code-generation muscle) and **GitHub Spec Kit** (spec → plan → tasks) — behind our own gated LangGraph spine. Our IP is the presales-to-contract pipeline and the human gates, not the codegen. This is both cheaper and more defensible than building from scratch.

---

## 1. The honest headline

I can have a **working end-to-end walking skeleton in ~6 weeks** with one dedicated senior engineer (me), including the time to come up to speed on the orchestration stack. That skeleton will take one real presales artifact set and produce a Draft PR containing generated backend code for one bounded context, behind one human approval gate.

What that 6 weeks does **not** deliver is the full "$15, 10-days-per-project" factory from the original vision. A *trustworthy* factory that actually collapses delivery time is a quarter-plus of iteration beyond the skeleton. I want to be upfront about that gap now rather than discover it in week 8. The skeleton's job is to **de-risk the concept cheaply and prove the spine works** — then we decide, with evidence, how far to push it.

---

## 2. Key technical decisions (already made, with rationale)

| Decision | Choice | Why |
|---|---|---|
| Orchestration framework | **LangGraph** (single orchestrator) | Native durable checkpointing + human-in-the-loop pause/resume — the backbone of our two approval gates. We drop CrewAI: it's a second overlapping orchestration layer with no benefit for our controlled, gated flow. |
| Orchestration **language** | **TypeScript (LangGraph.js)** | I'm a .NET/C# architect with no Python. LangGraph.js reached feature parity with Python (Postgres checkpointing, HITL interrupts) and is production-stable. C#→TS is a far smaller jump than C#→Python, and TS is the *same language as the monorepo we generate into* — one language across the whole stack. |
| State / checkpoint store | **PostgreSQL** (`PostgresSaver`) | Durable resume across restarts and multi-worker scaling. Also our app-DB default (below). |
| Baseline code template | **Fork of `turborepo-starter-kit`**, hardened | NestJS + Next.js + Expo, AI-native (`AGENTS.md`/skills), QA harness pre-wired. Matches our stack exactly. |
| App database | **PostgreSQL + Prisma** (swap out Mongo) | Our pilot domains (e.g. inventory) are transactional/relational, not document-shaped. The template tool doesn't dictate the DB. |
| Contract model | **OpenAPI + DB schema, human-approved at Gate 1** | Language-agnostic contract frozen before any parallel generation — prevents integration drift. |
| Code-generation muscle | **Claude Code (via Agent SDK), headless** | Already edits files, runs tests, works in a repo, and *natively respects `AGENTS.md`/`CLAUDE.md` + skills*. Our template ships those, so Claude Code arrives pre-briefed on the house style. We invoke it per bounded context instead of building a generator. |
| Spec → plan → tasks | **GitHub Spec Kit** (open source, MIT) | Provides the spec-driven methodology and templates, and already integrates Claude Code. We adopt it for the front half rather than inventing our own task decomposition. |

---

## 3. Why the language ramp is smaller than it looks

Coming from senior .NET, the genuinely new material is narrow:

- **TypeScript syntax** — days, not weeks. If you know C# generics, async/await, and DI, TS is familiar.
- **The LangGraph state-machine model** — this is the part that *maps directly onto skills you already have*. A LangGraph graph is a state machine with typed state, nodes, conditional edges, and checkpoints. You already think in workflows and state machines; you're learning a new API for an old mental model, not a new way of thinking.
- **LLM prompt / tool-use engineering** — this is the real new skill, and it's **language-independent**. You'd face it identically in Python or C#. Better to learn it in the language closest to your codebase.

Net: the "no Python" gap is not a real blocker once we choose TypeScript. It removes weeks of ramp, not adds them.

---

## 4. What the walking skeleton does (and deliberately doesn't)

**In scope for v1:**

- Ingest one presales artifact set (EventStorming JSON + WBS) → canonical IR.
- Deterministically bootstrap a fresh project repo from the hardened template (green baseline).
- **Architect Agent** generates OpenAPI + Prisma schema for **one** bounded context (using Spec Kit's spec→plan→tasks flow).
- **HITL Gate 1:** graph pauses, I approve the contract, graph resumes.
- **Generation:** the graph invokes **Claude Code headless** with the approved contract + the template's skills to generate that context's NestJS module into the repo. *We wire and drive an existing agent — we do not build a generator.*
- Run the template's existing build + tests; open a **Draft PR**.
- Basic token-cost + step telemetry logged.

**Deliberately deferred (the roadmap, not the skeleton):**

- Web and mobile crews (backend-only for v1).
- Multiple bounded contexts / parallel dispatch.
- The automated QA "3-retry" fix loop (v1 just reports pass/fail).
- Gate 2 escalation / IDE handoff automation.
- IaC / deploy / staging / E2E smoke.
- Brownfield mode (adding to existing client repos).

Deferring these is the whole point: the skeleton proves the *spine* is sound before we invest in the muscles.

### Per-node engine map (important)

LangGraph.js is the spine for **every** node. The choice of *what runs inside a node* is made per node — we do not use one engine everywhere:

| Node | Engine | Why this engine |
|---|---|---|
| 1. Ingestion (JSON → IR) | **Deterministic code** (no LLM) | Pure parsing of a known schema. No model needed. |
| 2. Bootstrap (repo from template) | **Deterministic code** (no LLM) | Git + install + green-build. Must be 100% reproducible. |
| 3. Architect (IR → OpenAPI + schema) | **Raw Claude API** | Structured-output reasoning, no file editing. We want tight control, low cost, near-deterministic output. |
| — | **HITL Gate 1** | Graph pauses on a Postgres checkpoint; human approves the contract; graph resumes. |
| 4. Generation (contract → code in repo) | **Wrapped Claude Code (Agent SDK)** | Genuinely needs the file-edit / test-run agent loop and reads the template's `AGENTS.md`/skills. Rebuilding this loop from the raw API is the weeks of work we're avoiding. |
| 5. Verify + PR | **Deterministic code** (no LLM) | Run build/tests, open Draft PR via git. |

Principle: **raw Claude API where we need control and there's no file I/O; wrapped Claude Code where we'd otherwise reinvent a coding agent.** Because the node boundary is clean, the generation node can drop to raw API later if we ever need finer control or cheaper runs. The Week-1 spike tests the generation node both ways and lets the data decide.

### Pipeline flow (arrow diagram)

```text
                       Presales JSON + WBS
                               │
                               ▼
                 ┌───────────────────────────────┐
                 │  1. INGESTION NODE            │   deterministic · no LLM
                 │     JSON  →  Canonical IR     │
                 └───────────────┬───────────────┘
                                 ▼
                 ┌───────────────────────────────┐
                 │  2. BOOTSTRAP NODE            │   deterministic · no LLM
                 │     generate repo from        │
                 │     template → green baseline │
                 └───────────────┬───────────────┘
                                 ▼
                 ┌───────────────────────────────┐
                 │  3. ARCHITECT NODE            │ ◀── RAW CLAUDE API
                 │     IR → OpenAPI + Prisma     │     (structured output,
                 │     schema  (via Spec Kit)    │      tight control, cheap)
                 └───────────────┬───────────────┘
                                 ▼
                        ◇  HITL GATE 1  ◇          pause (checkpoint) →
                                 │                 human approves → resume
                                 ▼
                 ┌───────────────────────────────┐
                 │  4. GENERATION NODE           │ ◀── WRAPPED CLAUDE CODE
                 │     contract → NestJS module  │     (Agent SDK: edits files,
                 │     in the repo               │      runs tests, reads skills)
                 └───────────────┬───────────────┘
                                 ▼
                 ┌───────────────────────────────┐
                 │  5. VERIFY + PR NODE          │   deterministic · no LLM
                 │     build + test → Draft PR   │
                 └───────────────┬───────────────┘
                                 ▼
                     Draft PR  +  token/time telemetry

   ── LangGraph.js spine wraps all nodes · Postgres checkpointer · resume-safe ──
```

---

## 5. Phased timeline (~6 weeks, one senior engineer)

| Phase | Duration | Outcome | Go/No-Go |
|---|---|---|---|
| **0. Ramp + spike** | Week 1 | Two spikes in parallel: (a) toy 3-node LangGraph.js graph with a HITL interrupt that pauses, persists to Postgres, and resumes; (b) drive **Claude Code headless** from a script to generate a trivial change into the template repo, and run **Spec Kit** on a sample spec. | **Gate:** if pause/resume *and* headless Claude Code invocation both work in a week, we proceed. If either is a wall, we reassess before spending more. |
| **1. Harden the template** | Week 2 | Fork template, swap Mongo→Postgres/Prisma in `apps/api`, pin versions, confirm green build/test, mark as GitHub template repo. | Baseline builds green from a fresh `generate`. |
| **2. Spine: ingestion + bootstrap** | Week 3 | Parser node (JSON→IR, deterministic) + Bootstrap node (generate repo from template, rename scopes, install, green build) + Postgres checkpointing wired. | A run produces a clean, green, checkpointed repo with zero LLM involvement. |
| **3. Architect Agent + Gate 1** | Week 4 | Architect node runs Spec Kit's spec→plan→tasks over the IR + repo conventions, emits OpenAPI + Prisma schema for one context; output validated; HITL interrupt for human approval. | Human can review and approve/reject a contract; graph resumes correctly. |
| **4. Claude Code generation node + Draft PR** | Weeks 5–6 | Node invokes **Claude Code headless** with the approved contract + template skills to generate the NestJS module; run build+tests; open a Draft PR via git. Effort is *prompt/context engineering and wiring*, not writing a generator. | **DEMO:** one presales artifact → Draft PR with compiling, test-passing backend for one context. |

*Assumes a mostly-dedicated senior engineer. Part-time or interruptions push this out proportionally. Because we invoke Claude Code rather than build a generator, Phase 4 shrinks from "build codegen" to "engineer the context + prompts and wire the git handoff" — still the hardest stretch (getting reliably compiling output takes iteration), but a smaller build. The time freed goes into integration robustness, not net-new code.*

---

## 6. What "done" looks like for the demo

A CTO-facing demo where I:

1. Drop in a real presales JSON + WBS.
2. Show the graph run: ingest → bootstrap (green repo) → architect proposes a contract → **it pauses**.
3. I review and approve the OpenAPI/schema in the gate.
4. The graph resumes, generates the backend module, runs tests, and opens a **Draft PR** in the client repo.
5. Show the token cost and wall-clock time for the run.

That proves every load-bearing concept — ingestion, deterministic scaffolding, contract-first generation, durable HITL pause/resume, code generation against conventions, and git handoff — on the smallest possible surface.

---

## 7. Risks and how the skeleton addresses them

| Risk | Reality | Mitigation in this plan |
|---|---|---|
| **Generated code doesn't compile / integrate** | The hardest, most likely failure. | Rigid template conventions + `AGENTS.md`/skills give the LLM a narrow pattern to fill. Green baseline means any red is *our* generated code, so failures are cleanly attributable. |
| **Self-referential QA (tests prove nothing)** | If the same spec writes both code and tests, green ≠ correct. | Out of scope for v1 (we only report pass/fail), but flagged now: the real fix is scenario-level acceptance criteria authored independently of the generator. This is a *known* v2 problem, not a surprise. |
| **Cost/time claims oversold** | "$15 / 10 days per project" is not realistic; true token cost is likely 1–2 orders of magnitude higher, and senior review time is the real cost. | v1 **measures** actual token cost and time per run so we replace guesses with data before making any external commitments. |
| **Retry loop that loops forever** | Re-running a stochastic agent with unchanged context just re-rolls the dice. | Deferred to v2, but the design principle is set: every retry must inject the specific failure signal into the next prompt. |
| **My ramp on a new stack** | Real, but bounded. | Week 1 is an explicit spike with a go/no-go gate. We find out cheaply if the stack fits. |
| **Adopted tools resist our shape** | Spec Kit / Claude Code are opinionated; bending them to our artifact format and gates can occasionally cost more than a thin bespoke node. | Week-1 spike evaluates the actual integration effort before we commit. If Claude Code headless is a poor fit, OpenHands is the fallback muscle — the spine (LangGraph) is unaffected either way. |

---

## 7b. Implementation & test spec (the five agents)

### Model tiering

Cost control is per-agent, not global. All agents run through the Claude Code CLI with `--model`, so **no API key is needed** — your subscription login covers it, exactly as the spike proved.

| Agent | Model | Rationale |
|---|---|---|
| Spec Analyst | **Haiku 4.5** | Classification over the IR. If recall on obvious gaps is <90%, fix the prompt, not the model. |
| Architect | **Opus 5** | Hard reasoning, small output, highest blast radius. |
| Test Author | **Sonnet 5** | Translation of approved scenarios → test files. |
| Implementer | **Sonnet 5 → Opus 5** | Bulk of the work; escalates to Opus only on attempt 3. Most slices pass on attempt 1, so this is a real cost lever. |
| Diagnostician | **Opus 5** | Hard but rare. |

*(Note: the original proposal referenced "Claude 3.5 Sonnet/Opus" — current generation is Opus 5 / Sonnet 5 / Haiku 4.5.)*

Later, the three pure-reasoning agents can migrate to the raw Anthropic SDK for structured outputs and finer control; that needs an API key and a billing decision. CLI tiering is free until then.

### How the factory itself is tested

Two levels. Level 2 alone tells you "the run failed" but not *which agent regressed*.

**Level 1 — per agent:**

| Agent | Test mechanism |
|---|---|
| Spec Analyst | Golden fixture (hand-label the 21 board events) + **mutation**: delete a known business rule, assert it's flagged. |
| Architect | `prisma validate`, OpenAPI lint, aggregate→model completeness, scenario status-code coverage, and **stability** (same IR twice → semantically equal contract). |
| Test Author | **Red-first** (tests must FAIL with no implementation — kills vacuous tests), **mutation score** (inject bugs into working code; <0.6 means green is decorative, >0.8 means it's meaningful), **traceability** (every scenario → ≥1 test; no provisional scenario leaks in). |
| Implementer | The tests are the oracle. Plus a **git-diff write-scope audit** — if it touched `tests/`, hard fail. |
| Diagnostician | **A/B against naive retry.** If diagnosis doesn't beat re-sending raw failure output, delete the agent and save the Opus tokens. |

**Level 2 — pipeline:** fixture-project smoke test; kill-and-resume test (proves the checkpointer); gate test (assert the graph *cannot* pass Gate B unapproved); budget-breaker test.

**Portability guard:** `check_prompt_leak()` fails CI if domain nouns appear in factory code. Domain facts live in the IR and the run config; prompts hold only conventions. This is what makes project #2 a config swap rather than a rewrite — and it already caught a real leak on first run (hard-coded paths in `__main__`, now externalised to `run.json`).



Rough order, each a genuine multi-week effort, prioritised by value and de-risking:

1. **Independent test oracle** — scenario-level acceptance criteria feeding the QA node. *This is the difference between a demo and a trustworthy factory; it should come first.*
2. **QA auto-retry loop** with failure-signal feedback (Gate 2 escalation to IDE).
3. **Web crew** (second generator, still one context) — proves multi-crew coherence against the frozen contract.
4. **Multi-context parallel dispatch.**
5. **Mobile crew**, then **IaC/deploy**.
6. **Brownfield mode** (clone-and-extend existing repos).

A defensible statement to make externally: *"first useful internal pilots in a quarter; broad reliable use is a multi-quarter investment."*

---

## 9. One-paragraph summary for the CTO

We don't build a code generator — we orchestrate proven ones (**Claude Code** + **GitHub Spec Kit**) behind our own gated LangGraph.js spine. Our IP is the presales-to-contract pipeline and the human gates. Using TypeScript (which sidesteps my lack of Python and keeps us in one language across the stack), I can stand up a working, end-to-end walking skeleton in about six weeks, with a week-one go/no-go spike to de-risk both the orchestration and the Claude Code integration early. It will turn a real presales artifact into a Draft PR of generated, test-passing backend code for one bounded context, behind one human approval gate — proving every core mechanism on the smallest surface. The full factory, and especially the correctness-assurance and cost claims in the original vision, is a multi-quarter effort that this skeleton lets us scope with real data instead of optimism.
