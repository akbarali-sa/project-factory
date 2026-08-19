"""
Project Factory — full first-slice pipeline (LangGraph, Python).

  ingest(det) -> gap_detect(SONNET) -> [GATE A]
  -> clone_starter(det) -> provision_db(det) -> baseline(det) -> commit_specs(det)
  -> architect(OPUS) -> contract_lint(det) -> [GATE B]
  -> migrate(det)
  -> write_tests(SONNET: unit + e2e) -> red_first(det)
  -> implement_api(SONNET->OPUS) -> verify_api(det) --fail--> diagnose(OPUS) --+
  -> implement_web(SONNET->OPUS) -> verify_web(det) --fail--> diagnose(OPUS) --+
  -> launch_stack(det, docker) -> verify_e2e(det, playwright) --fail--> diagnose+
  -> teardown(det) -> commit -> open Draft PR -> [GATE C] -> END

Checkpointing: a `project_factory_state` database on the ONE shared Postgres
instance every local project uses (see defaults.json: db_host/db_port/...), so
every agent step is durable and a killed run resumes exactly where it stopped.
The thread_id is derived as <slug>:<slice_id>, so resume is automatic.

Paths are never guessed here — the CLI resolves them via config.discover().

Run:
  # ensure your shared Postgres (pgvector-based, see defaults.json) is running
  python -m project_factory run <slug>
"""

from __future__ import annotations

import json
import pathlib
from typing import Annotated, Any, Literal, TypedDict

import yaml
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from . import config, infra, livelog
from . import repo as repo_mod
from .harness import (
    check_contract,
    check_infra_contract,
    check_red_first,
    check_traceability,
    check_write_scope,
    digest_failures,
    run_tests,
    splice_schema,
)
from .models import BudgetExceeded, Usage, claude
from .prompting import render_scenarios

MAX_ATTEMPTS = 3

WRITE_API = ["apps/api/src/**"]
WRITE_WEB = ["apps/web/src/**", "packages/ui/src/**"]
READ_ONLY = ["**/__tests__/**", "**/e2e/**", "**/*.spec.ts", "**/*.test.ts", "specs/**"]


# -----------------------------------------------------------------------------
def _last(a: Any, b: Any) -> Any:
    return b if b is not None else a


def _merge(a: dict | None, b: dict | None) -> dict:
    return {**(a or {}), **(b or {})}


class S(TypedDict, total=False):
    # resolved by the CLI via config.discover() — the graph never guesses paths
    cfg: dict
    board_path: str
    scenarios_path: str
    repo_path: str
    project_dir: str
    slice_id: str

    ir: dict
    scenarios: dict
    gaps: list[dict]
    starter_sha: str
    stack: Any                       # infra.Stack
    contract: str

    phase_out: Annotated[dict, _merge]     # phase -> last test output
    attempts: Annotated[dict, _merge]      # phase -> count
    diagnosis: Annotated[dict, _merge]     # phase -> fix instruction
    status: Literal["pending", "green", "parked"]
    parked: Annotated[list[str], lambda a, b: (a or []) + (b or [])]

    usage: Annotated[Usage, _last]
    log: Annotated[list[str], lambda a, b: (a or []) + (b or [])]

    pr_approved: Annotated[bool, _last]    # Gate C verdict — merge_slice reads it
    infra_touched: Annotated[list[str], _last]  # infra files an agent edited


def _budget(state: S) -> float:
    return float(state["cfg"].get("budget_usd", 25.0))


def _gate_policy(state: S, gate: str) -> str:
    """
    'human' (interrupt and wait) or 'auto' (record a driver approval with the
    mechanical evidence and continue). Plain `run` never sets cfg["gates"], so
    every gate stays human unless run-project (or run.json) says otherwise.
    """
    gates = state.get("cfg", {}).get("gates") or {}
    return gates.get(gate, "human")


def _log_path(state: S):
    """Where the dashboard's live-CLI tail reads from — see livelog.py."""
    return livelog.path_for(state["project_dir"], state["slice_id"])


# =============================================================================
# 1. Ingest (deterministic)
# =============================================================================
def ingest(state: S) -> dict:
    # Paths come from discovery (config.py), not from run.json — the CLI resolves
    # them and passes them in, so nothing here needs to know the layout.
    # ingest only runs on a genuinely fresh thread (LangGraph skips completed
    # nodes on resume), so this is the right, and only, place to truncate the
    # live log — a resumed run keeps its history instead of losing it.
    livelog.reset(state["project_dir"], state["slice_id"])
    board = config.load_board(state["board_path"])
    scen = yaml.safe_load(pathlib.Path(state["scenarios_path"]).read_text())
    in_scope = [e for e in board["business_events"] if config.event_in_scope(e)]
    # Scope the decision log to this slice's neighbourhood: global entries
    # and those touching this slice's events stay, IN FULL; entries about
    # other slices' events live on the board. Relevance filter, not a cap.
    slice_ids = {t for g in ("scenarios", "web_scenarios", "e2e_scenarios")
                 for s in scen.get(g) or [] for t in s.get("traces_to") or []}
    decisions = [
        d
        for d in board.get("decision_log", [])
        if not d.get("related_node_id") or d["related_node_id"] in slice_ids
    ]
    return {
        "ir": {"project": board["name"], "events": in_scope,
               "processes": board.get("processes", []),
               "decisions": decisions},
        "scenarios": scen,
        "attempts": {}, "phase_out": {}, "diagnosis": {},
        "status": "pending", "usage": Usage(),
        "log": [f"ingest: {len(in_scope)} in-scope events, "
                f"{len(scen['scenarios'])} api + "
                f"{len(scen.get('web_scenarios', []))} web + "
                f"{len(scen.get('e2e_scenarios', []))} e2e scenarios"],
    }


# =============================================================================
# 2. Spec Analyst (AGENT — Sonnet)
# =============================================================================
def gap_detect(state: S) -> dict:
    digest = [{
        "id": e["id"], "name": e["name"],
        "rules": len(e["implementation"].get("business_rules", [])),
        "ac": len(e["implementation"].get("acceptance_criteria", [])),
        "open_questions": [q["question"] for q in e.get("questions", [])
                           if q.get("status") != "answered"],
    } for e in state["ir"]["events"]]

    out = claude(
        "spec_analyst",
        "You audit a software specification for completeness. For each item, "
        "decide if it can be implemented without guessing. Flag anything with no "
        "business rules, unanswered questions, or untestable acceptance criteria."
        f"\n\n{json.dumps(digest, indent=1)}\n\n"
        'Return ONLY JSON: [{"id":...,"verdict":"ok"|"underspecified","missing":[...]}]',
        usage=state["usage"], budget_usd=_budget(state), log_path=_log_path(state),
    )
    try:
        gaps = json.loads(out[out.index("["):out.rindex("]") + 1])
    except (ValueError, json.JSONDecodeError):
        gaps = [{"id": "?", "verdict": "parse_error", "raw": out[:300]}]
    n = len([g for g in gaps if g.get("verdict") == "underspecified"])
    return {"gaps": gaps, "usage": state["usage"],
            "log": [f"gap_detect: {n} underspecified"]}


def gate_spec(state: S) -> dict:
    underspecified = [g for g in state["gaps"]
                      if g.get("verdict") == "underspecified"]
    provisional = [s["id"] for s in
                   state["scenarios"].get("provisional_scenarios", [])]
    if _gate_policy(state, "spec") == "auto":
        # The human accountability point moved up: the PROJECT PLAN was
        # human-approved, and Gate C still reviews the delivered slice. What
        # gets recorded here is the evidence a human reviewer would have seen.
        note = (f"auto-approved per gate policy: {len(underspecified)} "
                f"board-level gaps flagged (oracle authored against the "
                f"approved plan), provisional held back: {provisional or 'none'}")
        return {"log": [f"gate_A: {{'approved': True, 'by': 'gate-policy:auto', "
                        f"'note': {note!r}}}"]}
    d = interrupt({
        "gate": "A — spec & scenarios (you are approving the ORACLE)",
        "underspecified": underspecified,
        "provisional_held_back": provisional,
        "ask": "Approve to proceed.",
    })
    return {"log": [f"gate_A: {d}"]}


# =============================================================================
# 3. Clone starter (deterministic) — 1) in your list
# =============================================================================
def clone_starter(state: S) -> dict:
    cfg = state["cfg"]
    info = repo_mod.create_project_repo(
        factory_root=str(config.factory_root()),
        starter_url=cfg["starter_url"],
        starter_ref=cfg.get("starter_ref", "main"),
        repo_path=state["repo_path"],
        fresh=cfg.get("fresh_clone", True),
    )
    # Index HERE, not just at finish: every reader from `architect` onward is
    # told (_GRAFT_NUDGE) that the repo carries a graft/ card per source file.
    # Indexing only at finish made that promise false for the whole of slice 1
    # — agents followed it to a directory that did not exist yet and fell back
    # to reading whole starter sources, which is the cost the nudge exists to
    # avoid. Free, offline, and best-effort: it cannot fail the clone.
    graft_note = repo_mod.index_with_graft(state["repo_path"])
    return {"starter_sha": info.starter_sha,
            "log": [f"clone: {cfg['starter_url']}@{info.starter_ref} "
                    f"({info.starter_sha[:8]}) -> {info.path}",
                    f"graft: {graft_note}"]}


# =============================================================================
# 4. Provision DB (deterministic) — drives the STARTER'S own pnpm scripts
# =============================================================================
def provision_db(state: S) -> dict:
    """
    Postgres in Docker via the starter's `pnpm db:up`, then install, then
    `db:reset` (drop + re-migrate + re-seed 3 demo users).

    Two-layer seeding:
      Layer 1 (here)  the template's demo users -> identity for E2E login
      Layer 2 (tests) domain fixtures created per-test in its arrange step
    Global domain seeds are deliberately avoided: shared mutable state across
    tests creates order-dependence, and scenarios like SC-001 assert on an
    EMPTY starting state.
    """
    cfg, slug = state["cfg"], repo_mod.slugify(state["cfg"]["project_slug"])
    # Sticky per PROJECT, not per slice: the generated app keeps one address
    # across every wave, so the integrated product is always at the same URL.
    api_port, web_port = infra.sticky_ports(
        state["project_dir"], state["repo_path"],
        cfg.get("api_port", 3001), cfg.get("web_port", 3000))
    stack = infra.Stack(
        project_slug=slug,
        api_port=api_port,
        web_port=web_port,
        db_name=slug.replace("-", "_"),
        db_host=cfg.get("db_host", "localhost"),
        db_port=cfg.get("db_port", 5432),
        db_user=cfg.get("db_user", "postgres"),
        db_password=cfg.get("db_password", "postgres"),
    )
    infra.write_env(state["repo_path"], stack,
                    cfg.get("jwt_secret", "factory-local-dev-secret-not-for-production"))
    lp = _log_path(state)
    infra.db_up(state["repo_path"], stack, log_path=lp)
    infra.install(state["repo_path"], log_path=lp)
    infra.reset_db(state["repo_path"], stack, cfg.get("db_reset_consent"), log_path=lp)
    return {"stack": stack,
            "log": [f"db: database '{stack.db_name}' on shared postgres "
                    f"{stack.db_host}:{stack.db_port}, reset + template users "
                    f"seeded; api:{api_port} web:{web_port}"]}


def baseline(state: S) -> dict:
    """Starter must build green BEFORE we generate — makes failures attributable."""
    r, lp = state["repo_path"], _log_path(state)
    for cmd in (["pnpm", "check-types"], ["pnpm", "build"]):
        p = livelog.tee_subprocess(cmd, cwd=r, timeout=2400, check=False, log_path=lp)
        if p.returncode != 0:
            raise RuntimeError(f"starter not green ({' '.join(cmd)}):\n{p.stdout[-2000:]}")
    return {"log": ["baseline: starter type-checks and builds green"]}


def commit_specs(state: S) -> dict:
    """
    Copy the project's specs INTO the generated repo.

    Source of truth is <workspace>/<slug>/specs/; the repo gets a committed copy
    so it is self-contained — visible in the PR diff and auditable a year later
    without the factory present.
    """
    repo_mod.commit_spec_artifacts(
        state["repo_path"], state["board_path"], state["scenarios_path"])
    repo_mod.create_branch(state["repo_path"], f"feat/{state['slice_id']}")
    return {"log": [f"specs copied into repo; branch feat/{state['slice_id']}"]}


# =============================================================================
# 5. Architect (AGENT — Opus)
# =============================================================================
def architect(state: S) -> dict:
    sc = state["scenarios"]
    ids = {t for s in sc["scenarios"] for t in s["traces_to"]}
    relevant = [e for e in state["ir"]["events"] if e["id"] in ids]
    # Read at node runtime, not carried in state: keeps old checkpoints
    # resumable, and slice-mode projects without a backbone simply skip it.
    backbone_file = (pathlib.Path(state["project_dir"]) / "specs"
                     / "data-backbone.yaml")
    backbone = (
        "PROJECT DATA BACKBONE — the project-wide entity contract (entities, "
        "business keys, relations, delete semantics, owning slice), decided "
        "once after plan approval. BINDING for names, keys and relations; "
        "columns, enums, indexes and constraints are yours to design. Declare "
        "only what this slice owns, plus redeclarations needed to add a "
        "relation the backbone already lists:\n"
        f"```yaml\n{backbone_file.read_text()}\n```\n\n"
    ) if backbone_file.exists() else ""
    uiux_map_file = (pathlib.Path(state["project_dir"]) / "specs"
                     / "uiux-map.yaml")
    uiux_map = (
        "APPROVED UI/UX MAP — the human-gated route/screen contract from the "
        "project's UI/UX preview. BINDING for route paths, screen inventory "
        "and per-screen states; the full visual reference is "
        "specs/uiux-preview.html in this repo, read it for any screen this "
        "slice serves:\n"
        f"```yaml\n{uiux_map_file.read_text()}\n```\n\n"
    ) if uiux_map_file.exists() else ""
    out = claude(
        "architect",
        "Design the data + API contract for ONE vertical slice of an existing "
        "NestJS + Prisma + Next.js monorepo. Follow the repo's conventions and "
        "match the existing reference module exactly. Invent no new structural "
        "patterns.\n\n"
        + _GRAFT_NUDGE +
        "REDECLARE-TO-MODIFY: your prisma block is spliced into the repo's "
        "existing schema.prisma — new models/enums are appended, and a model "
        "you redeclare REPLACES the existing one wholesale. To change an "
        "existing model (e.g. add a field or back-relation to `User`), "
        "redeclare the ENTIRE model with your change applied. Never describe "
        "a change in comments or diff notation: the splice is literal, "
        "`prisma validate` runs on the result, and a commented-out change "
        "leaves every relation that depends on it dangling. A redeclaration "
        "must reproduce every existing field of the model — dropping one is "
        "a silent destructive migration and fails validation.\n\n"
        + backbone + uiux_map +
        f"Aggregates: {sc['aggregates']}\n\n"
        f"Domain events:\n{json.dumps(relevant, indent=1)}\n\n"
        f"API scenarios the contract must satisfy:\n"
        f"{render_scenarios(sc['scenarios'])}\n\n"
        f"Web scenarios (screens that will consume it):\n"
        f"{render_scenarios(sc.get('web_scenarios', []))}\n\n"
        "Output exactly two fenced blocks, nothing else:\n"
        "```prisma\n<models>\n```\n```yaml\n<OpenAPI paths + schemas>\n```",
        cwd=state["repo_path"], usage=state["usage"], budget_usd=_budget(state), log_path=_log_path(state),
    )
    return {"contract": out, "usage": state["usage"],
            "log": ["architect: contract drafted"]}


def contract_lint(state: S) -> dict:
    res = check_contract(state["contract"], state["scenarios"], state["repo_path"])
    if not res.ok:
        raise RuntimeError("contract invalid:\n" + "\n".join(res.errors))
    return {"log": [f"contract_lint: ok ({res.summary})"]}


def gate_contract(state: S) -> dict:
    sc = state["scenarios"]
    needs_schema_change = [
        s["id"] for group in ("scenarios", "web_scenarios", "e2e_scenarios")
        for s in sc.get(group, []) if s.get("depends_on_schema_change")
    ]
    if _gate_policy(state, "contract") == "auto":
        # contract_lint already passed (it is the node before this one and
        # raises on failure) — that mechanical validation is the evidence.
        note = ("auto-approved per gate policy: contract_lint ok"
                + (f"; schema-extending scenarios: {needs_schema_change}"
                   if needs_schema_change else "; purely additive schema"))
        return {"log": [f"gate_B: {{'approved': True, 'by': 'gate-policy:auto', "
                        f"'note': {note!r}}}"
                        + (f" (schema-extending: {needs_schema_change})"
                           if needs_schema_change else "")]}
    d = interrupt({
        "gate": "B — contract freeze",
        "contract": state["contract"][:4000],
        # Surfaced explicitly: these scenarios extend the STARTER's auth model
        # (its User has no role field), so they are not a pure additive slice.
        "extends_template_schema": needs_schema_change,
        "starter_constraints": sc.get("starter_constraints", {}),
        "ask": "Approve to freeze. Check the schema additions against the "
               "starter's conventions before approving.",
    })
    return {"log": [f"gate_B: {d}"
                    + (f" (schema-extending: {needs_schema_change})"
                       if needs_schema_change else "")]}


def migrate(state: S) -> dict:
    """
    Append the approved models to the starter's schema, then migrate.

    New models are appended; a model the Architect RE-DECLARES (e.g. `User`,
    to add `role`) REPLACES the existing block rather than being discarded.
    Gate B exists specifically to review and approve exactly that kind of
    change — silently keeping the starter's old version instead would defeat
    the gate and leave the schema missing whatever field Gate B approved
    (and any relation elsewhere that references it, e.g. a back-relation on
    `User` for a new model's foreign key).
    """
    import re
    m = re.search(r"```prisma\n(.*?)```", state["contract"], re.S)
    schema = pathlib.Path(state["repo_path"]) / "apps/api/prisma/schema.prisma"
    if m:
        # Same splice check_contract validated — see harness.splice_schema.
        schema.write_text(splice_schema(schema.read_text(), m.group(1)))

    lp = _log_path(state)
    infra.migrate(state["repo_path"], state["stack"], state["cfg"].get("db_reset_consent"), log_path=lp)
    infra.seed_template_users(state["repo_path"], state["stack"], log_path=lp)
    repo_mod.commit(state["repo_path"],
                    "feat(db): slice models + migration from approved contract")
    return {"log": ["migrate: models appended, migration applied, users re-seeded"]}


# =============================================================================
# 6. Test Author (AGENT — Sonnet). Unit + E2E, committed FIRST, red.
# =============================================================================
def write_tests(state: S) -> dict:
    sc, slug = state["scenarios"], state["scenarios"]["slice"]["id"]
    # commit_specs copies the oracle INTO the repo, and this agent runs with
    # cwd=repo — so it can read the whole file, including the scenarios the
    # oracle deliberately parked behind unanswered questions. Naming them as
    # forbidden is cheaper than letting check_traceability catch it afterwards:
    # on barcode-v2's scanning slice a single implemented provisional scenario
    # threw away a completed $12 Test Author run.
    provisional = [s.get("id") for s in sc.get("provisional_scenarios") or []
                   if s.get("id")]
    blocked = (
        "BLOCKED SCENARIOS — the oracle parked these behind open questions "
        f"nobody has answered: {provisional}. They are in the scenarios file "
        "you can read in specs/, and they are NOT yours to write. Do not "
        "write a test for any of them, not even a skipped or placeholder one; "
        "a test asserting a behaviour no source has decided invents the "
        "answer. Write tests ONLY for the scenarios listed below.\n\n"
    ) if provisional else ""
    claude(
        "test_author",
        "Write executable tests from approved scenarios. Two suites:\n"
        f"  1. Vitest API integration tests -> apps/api/__tests__/{slug}/\n"
        f"  2. Playwright E2E tests -> apps/web/__tests__/e2e/{slug}/\n\n"
        + _GRAFT_NUDGE
        + blocked
        + "Rules: one test per scenario; put the scenario id in the test name; "
        "assert exactly what the scenario states. Write NO implementation code. "
        "Do NOT weaken assertions — these SHOULD fail now, because the feature "
        "does not exist yet. Never use it.skip.\n\n"
        "You are running headless and UNATTENDED: nobody can answer questions, "
        "so never stop to ask one — the run fails if the test files do not "
        "exist on disk when you finish. Where a detail is not pinned down by "
        "the frozen contract, make the most conventional choice consistent "
        "with the starter's existing patterns, record that decision as a "
        "comment next to the affected test, and keep going.\n\n"

        # Every rule below was paid for: across three slices of the first full
        # project run, essentially EVERY non-infrastructure e2e failure was one
        # of these five, and each one burned a full 3-attempt diagnose/fix loop
        # on a product that was behaving correctly the whole time.
        # Paid for on barcode-v2's scanning slice: a fixture that threw on its
        # second call sent the Implementer into the test files to fix it, which
        # check_write_scope correctly refused — costing the node.
        # Two slices in a row lost their whole e2e/API run to this exact
        # character sequence, written by two different Test Author calls.
        "COMMENT MECHANICS — never write the two characters `*/` inside a "
        "block comment: it TERMINATES the comment and the rest becomes code. "
        "Glob paths (**/items/*/detail) and scenario-id patterns "
        "(WEB-*/E2E-*) are exactly how it happens, and the file then fails "
        "to PARSE, so the suite collects zero tests and every attempt burns "
        "on a syntax error. Put globs and id patterns in `//` line comments, "
        "or write them without the slash-star adjacency.\n\n"
        "IDEMPOTENT FIXTURES — a seed helper must survive being called twice "
        "with the same business key. Scenarios in one file share constants, "
        "the suite re-runs after every implement attempt, and unique columns "
        "do not forgive a second insert. Delete-then-create inside the "
        "transaction, or derive a per-scenario key. A fixture that throws on "
        "its second call reads as a product failure and sends the Implementer "
        "into YOUR files to fix it — which the oracle guard rejects, losing "
        "the whole attempt.\n\n"
        "SHARED HELPERS — put every selector helper (live region, form fields, "
        "sign-in, seeding) in ONE module at the suite root, e.g. "
        "apps/web/__tests__/e2e/helpers/, and import it from each spec. Do NOT "
        "redefine a helper per spec file. A duplicated helper means a flawed "
        "selector must be rediscovered and refixed once per copy, and each "
        "discovery costs a full suite run.\n\n"

        "SELECTOR AND WAIT RULES — these are mechanics, not assertions; "
        "getting them wrong produces failures that look exactly like product "
        "bugs:\n"
        "  1. SCOPE live-region selectors to the page's own main region "
        "(e.g. page.locator('main [role=\"alert\"]')). An unscoped "
        "[role=\"alert\"]/[aria-live] query also matches the toast region and "
        "the framework's route announcer — a strict-mode violation on 3 "
        "elements while the app was rendering the right message all along.\n"
        "  2. NEVER snapshot innerText (or textContent) of body/main straight "
        "after click()/reload(). There is no auto-wait, so you capture the "
        "in-flight 'Loading…' state. Anchor first with an expect(...) that "
        "waits for the rendered value, THEN read.\n"
        "  3. NEVER anchor a wait on a bare number, or on any value the page "
        "chrome may also render. If a record's reference is 'R-1001' and you "
        "wait for the quantity '100', a breadcrumb rendering 'R 1001' "
        "satisfies the wait instantly — you then read the page mid-load and "
        "the failure looks like a product bug. Anchor on a token unique to "
        "the thing you are waiting for: a business key from your fixture, or "
        "a test id you put on the row.\n"
        "  4. Do NOT assert focus/click on a control that is correctly "
        "disabled until a precondition is met — a disabled control is not "
        "focusable by design, in HTML. Satisfy the precondition first, then "
        "assert reachability.\n"
        "  5. Prefer role+name or a test id over free text that another screen "
        "may repeat. Navigation, index rows and detail screens routinely "
        "render the SAME action labels; a bare getByText is ambiguous the "
        "moment a later slice adds a nav link.\n\n"
        f"Frozen contract:\n{state['contract']}\n\n"
        f"API scenarios:\n{render_scenarios(sc['scenarios'])}\n\n"
        f"Web scenarios:\n{render_scenarios(sc.get('web_scenarios', []))}\n\n"
        f"E2E scenarios:\n{render_scenarios(sc.get('e2e_scenarios', []))}",
        cwd=state["repo_path"],
        write_scope=["apps/api/__tests__/**", "apps/web/__tests__/**"],
        usage=state["usage"], budget_usd=_budget(state), log_path=_log_path(state),
    )
    tr = check_traceability(state["repo_path"], sc)
    if not tr.ok:
        raise RuntimeError("traceability failed:\n" + "\n".join(tr.errors))
    rf = check_red_first(state["repo_path"], slug)
    if not rf.ok:
        raise RuntimeError("red-first failed:\n" + "\n".join(rf.errors))
    # --no-verify: red-first tests reference not-yet-existing implementation
    # modules, so the starter's pre-commit typecheck can never pass here.
    repo_mod.commit(state["repo_path"], f"test({slug}): scenarios as executable tests",
                    verify=False)
    return {"usage": state["usage"],
            "log": [f"write_tests: {tr.summary}; red-first ok"]}


# =============================================================================
# 7. Implement / verify / diagnose — one helper, three phases
# =============================================================================
# Navigation economics for sandboxed agents: they cannot run commands, so
# exploration happens by Reading files — and reading whole sources to FIND
# things is the expensive pattern. The repo ships an AST-derived index
# (graft/) precisely so a Read of one small card replaces several full-file
# reads. One shared nudge, injected into the readers that explore most.
_GRAFT_NUDGE = (
    "NAVIGATION — the repo contains graft/: INDEX.md plus one markdown card "
    "per source file (same path, .md suffix) listing every symbol's purpose "
    "and exact file:line span. To find or understand code, Read the card "
    "first and open the source only at the named span; do not read whole "
    "source files to search for things.\n\n"
)


def _implement(state: S, phase: str, scope: list[str], instruction: str) -> dict:
    n = state["attempts"].get(phase, 0)
    fix = ""
    if state["diagnosis"].get(phase):
        # digest_failures, not a raw tail-slice: dedup is noise control over
        # unbounded runtime logs (one root cause arrives once per browser
        # project), while a [-3000:] tail was TRUNCATION — it kept whatever
        # happened to fail last and hid the first, often causal, failure.
        fix = (f"\n\nPrevious attempt failed. Diagnosis:\n{state['diagnosis'][phase]}\n\n"
               f"Authoritative failure output (deduplicated):\n"
               f"{digest_failures(state['phase_out'].get(phase, ''))}")
    claude(
        "implementer",
        f"{instruction}\n\n"
        "The tests are the specification and are READ-ONLY. Never edit, delete "
        "or skip a test. Follow AGENTS.md and the repo skills. Match the "
        "existing reference module's structure.\n\n"
        + _GRAFT_NUDGE +
        # Paid for on barcode-v2: the Implementer wrote the single most
        # idiomatic TS Result type, the pre-commit typecheck rejected it, and
        # two Opus diagnosticians ($10.52) reasoned about "type errors" that
        # were a compiler-config artifact, not a defect in the code.
        "TYPESCRIPT — apps/api/tsconfig.json sets `strictNullChecks: false`, "
        "under which a BOOLEAN-literal discriminated union does not narrow: "
        "with `{ ok: true; value: T } | { ok: false; error: E }`, code inside "
        "`if (!r.ok)` still sees the `ok: true` branch and every `r.error` "
        "access fails TS2339. Use a STRING discriminant — "
        '`{ status: "ok"; ... } | { status: "error"; error: E }` tested with '
        '`if (r.status === "error")`. The pre-commit hook type-checks, so '
        "getting this wrong loses the whole node's work at its final step.\n\n"
        "Do NOT change how the app is RUN — the `dev`/`start` scripts, the "
        "Playwright webServer config, or any .env. The harness allocates ports "
        "and starts the stack; pinning a port or altering the boot sequence "
        "breaks it silently and resurfaces as failures that look like product "
        "bugs. Behaviour changes belong in src/.\n\n"
        f"Frozen contract:\n{state['contract']}{fix}",
        cwd=state["repo_path"], attempt=n, write_scope=scope, read_only=READ_ONLY,
        usage=state["usage"], budget_usd=_budget(state), log_path=_log_path(state),
    )
    scope_check = check_write_scope(state["repo_path"], READ_ONLY)
    if not scope_check.ok:
        raise RuntimeError("ORACLE VIOLATION — tests modified:\n"
                           + "\n".join(scope_check.errors))
    # Caught HERE, not at the gate: a pinned port or an altered boot sequence
    # makes every later verify fail against the wrong app, and the diagnostician
    # then reasons about product code that was never the problem.
    infra = check_infra_contract(state["repo_path"])
    if not infra.ok:
        raise RuntimeError("HARNESS CONTRACT VIOLATION:\n" + "\n".join(infra.errors))
    notable = infra.data.get("notable", [])
    return {"attempts": {phase: n + 1}, "usage": state["usage"],
            "infra_touched": notable,
            "log": [f"{phase}: attempt {n + 1}"
                    + (f" (infrastructure files touched: {notable})" if notable else "")]}


def implement_api(state: S) -> dict:
    return _implement(state, "api", WRITE_API,
                      "Implement the NestJS module (controller, service, "
                      "repository, DTOs) so the API integration tests pass.")


def implement_web(state: S) -> dict:
    return _implement(state, "web", WRITE_WEB,
                      "Implement the React/Next.js screen and its API client so "
                      "the web unit tests pass. Use the repo's design system and "
                      "existing UI components; keep it accessible. If "
                      "specs/uiux-map.yaml and specs/uiux-preview.html exist in "
                      "this repo they are the APPROVED layout reference — "
                      "BINDING for route paths, screen structure and the states "
                      "each screen handles; the design system stays the visual "
                      "layer.")


def fix_e2e(state: S) -> dict:
    return _implement(state, "e2e", WRITE_API + WRITE_WEB,
                      "The full stack is running in Docker but E2E tests fail. "
                      "Fix the integration between web and api.")


def verify_api(state: S) -> dict:
    # turbo filter, not a path — the starter's script is `turbo run test`
    r = run_tests(state["repo_path"], workspace="@repo/api")
    return {"phase_out": {"api": r.output},
            "status": "green" if r.ok else "pending",
            "log": [f"verify_api: {'PASS' if r.ok else 'FAIL'} ({r.summary})"]}


def verify_web(state: S) -> dict:
    r = run_tests(state["repo_path"], workspace="@repo/web")
    return {"phase_out": {"web": r.output},
            "status": "green" if r.ok else "pending",
            "log": [f"verify_web: {'PASS' if r.ok else 'FAIL'} ({r.summary})"]}


# =============================================================================
# 8. Launch stack in Docker + E2E — 5) in your list
# =============================================================================
def launch_stack(state: S) -> dict:
    summary = infra.launch_stack(state["repo_path"], state["stack"])
    return {"log": [f"launch: {summary}"]}


def verify_e2e(state: S) -> dict:
    # Self-heal a stack whose processes died (or wedged) with the run that
    # launched them — the checkpoint remembers launch_stack, the OS doesn't.
    relaunched = infra.ensure_stack(state["repo_path"], state["stack"])
    # Before spending 20 minutes and a diagnostician on the results, confirm the
    # thing answering the web URL is actually the web app.
    infra.assert_web_serves_app(state["stack"])
    r = infra.run_e2e(state["repo_path"], state["stack"],
                      consent=state["cfg"].get("db_reset_consent"),
                      log_path=_log_path(state))
    log = ([f"verify_e2e: stack relaunched ({relaunched})"] if relaunched else []) \
        + [f"verify_e2e: {'PASS' if r.ok else 'FAIL'} ({r.summary})"]
    return {"phase_out": {"e2e": r.output},
            "status": "green" if r.ok else "pending",
            "log": log}


def _diagnose(state: S, phase: str) -> dict:
    out = claude(
        "diagnostician",
        "A generated slice fails its tests. Identify the root cause and give a "
        "specific, minimal fix naming files and changes. Do not restate the "
        "error. Never suggest changing the tests.\n\n"
        + _GRAFT_NUDGE +
        # A digest, not the raw log: the suite runs every spec across four
        # browser projects, so one root cause arrives four times verbatim.
        # Slice 3's diagnostician read 80 copies of a single dead web server
        # and charged $12.89 to conclude nothing.
        # The failure DIGEST stays deduplicated (that is noise control over
        # unbounded runtime logs, not spec truncation), but its tail-slice is
        # gone and the contract goes in whole: a diagnostician reasoning about
        # a schema mismatch with 2,500 chars of a 10KB contract sees the
        # models but not the endpoints they feed.
        f"Phase: {phase}\n\nFailures (deduplicated):\n"
        f"{digest_failures(state['phase_out'].get(phase, ''))}\n\n"
        f"Contract:\n{state['contract']}",
        cwd=state["repo_path"], usage=state["usage"], budget_usd=_budget(state), log_path=_log_path(state),
    )
    return {"diagnosis": {phase: out}, "usage": state["usage"],
            "log": [f"diagnose[{phase}]: produced"]}


def diagnose_api(state: S) -> dict:
    return _diagnose(state, "api")


def diagnose_web(state: S) -> dict:
    return _diagnose(state, "web")


def diagnose_e2e(state: S) -> dict:
    return _diagnose(state, "e2e")


def _router(phase: str, on_green: str):
    def route(state: S) -> Literal["green", "retry", "park"]:
        if state["status"] == "green":
            return "green"
        return "retry" if state["attempts"].get(phase, 0) < MAX_ATTEMPTS else "park"
    route.__name__ = f"route_{phase}"
    return route


def park_api(state: S) -> dict:
    return {"parked": ["api"], "log": ["PARKED api — escalate to human"]}


def park_web(state: S) -> dict:
    return {"parked": ["web"], "log": ["PARKED web — escalate to human"]}


def park_e2e(state: S) -> dict:
    return {"parked": ["e2e"], "log": ["PARKED e2e — escalate to human"]}


# =============================================================================
# 9. Finish
# =============================================================================
def teardown(state: S) -> dict:
    if state["cfg"].get("keep_stack_running"):
        # No hard-coded login hint: which seeded user holds which role is a
        # PROJECT fact (barcode-v2's own seed inverted the starter default),
        # and a stale email here misdirects whoever opens the app.
        return {"log": [f"stack left running for inspection: {state['stack'].web_url} "
                        f"(seeded logins: apps/api/src/constants/demoData.ts)"]}
    infra.teardown(state["repo_path"], state["stack"],
                   keep_db=state["cfg"].get("keep_db", False))
    return {"log": ["stack stopped, postgres down"]}


def finish(state: S) -> dict:
    slug = state["scenarios"]["slice"]["id"]
    parked = state.get("parked", [])
    repo_mod.commit(state["repo_path"], f"feat({slug}): generated slice")
    pr = repo_mod.open_draft_pr(
        state["repo_path"], f"feat/{slug}",
        title=f"feat({slug}): generated slice",
        body=(f"Generated by project-factory.\n\n"
              f"- starter: {state['starter_sha'][:8]}\n"
              f"- attempts: {state['attempts']}\n"
              f"- parked: {parked or 'none'}\n"
              f"- cost: ${state['usage'].cost_usd:.2f}\n"
              f"- by agent: {json.dumps({k: round(v,3) for k,v in state['usage'].by_agent.items()})}\n"),
        remote=state["cfg"].get("git_remote"),
    )
    # Index the delivered code so the NEXT slice (and any human or agent
    # debugging this one at its gate) can query the graph instead of grepping.
    # Best effort: never fails the slice.
    graft_note = repo_mod.index_with_graft(state["repo_path"])
    return {"status": "parked" if parked else "green",
            "log": [f"pr: {pr}", f"graft: {graft_note}"]}


def gate_pr(state: S) -> dict:
    payload = {
        "gate": "C — PR review",
        "status": state["status"], "parked": state.get("parked", []),
        "attempts": state["attempts"],
        "cost_usd": round(state["usage"].cost_usd, 2),
        "by_agent": {k: round(v, 3) for k, v in state["usage"].by_agent.items()},
        # Not a failure — but an agent editing build/test config is worth a
        # human glance before it merges, since it changes how everything after
        # this slice is built and run.
        "infrastructure_files_touched": state.get("infra_touched", []),
        "ask": "Review the Draft PR; merge or push fixes.",
    }
    if _gate_policy(state, "pr") == "auto":
        # Supported for completeness ("fully autonomous"), never a default:
        # only a green, nothing-parked slice may pass unreviewed.
        if state["status"] == "green" and not state.get("parked"):
            return {"pr_approved": True,
                    "log": ["gate_C: {'approved': True, 'by': 'gate-policy:auto', "
                            "'note': 'green, nothing parked'}"]}
        payload["ask"] = ("Slice is not clean (parked phases or non-green) — "
                          "auto policy refuses it. Review and approve/reject.")
    d = interrupt(payload)
    return {"pr_approved": bool(d.get("approved")), "log": [f"gate_C: {d}"]}


def merge_slice(state: S) -> dict:
    """
    Fold the approved slice back into main so the NEXT slice builds on
    delivered code — run-project's cross-slice contract. Plain single-slice
    runs keep today's behaviour (branch left for manual review) unless
    merge_on_approval is set.
    """
    if not state["cfg"].get("merge_on_approval"):
        return {"log": ["merge: skipped (merge_on_approval not set)"]}
    if not state.get("pr_approved"):
        return {"log": ["merge: skipped (Gate C did not approve)"]}
    sha = repo_mod.merge_to_main(state["repo_path"], f"feat/{state['slice_id']}")
    return {"log": [f"merge: feat/{state['slice_id']} -> main ({sha[:8]})"]}


# =============================================================================
# Graph
# =============================================================================
def build_graph(checkpointer=None, interrupt_after: list[str] | None = None):
    """
    interrupt_after powers `run --until <node>`: the graph pauses after that node
    and the checkpoint holds everything, so a later `run` (or `approve`) resumes
    from exactly there. This is what makes the cheap-first ladder possible —
    prove clone+build for $0 before spending anything on generation.
    """
    g = StateGraph(S)
    nodes = [
        ("ingest", ingest), ("gap_detect", gap_detect), ("gate_spec", gate_spec),
        ("clone_starter", clone_starter), ("provision_db", provision_db),
        ("baseline", baseline), ("commit_specs", commit_specs),
        ("architect", architect), ("contract_lint", contract_lint),
        ("gate_contract", gate_contract), ("migrate", migrate),
        ("write_tests", write_tests),
        ("implement_api", implement_api), ("verify_api", verify_api),
        ("diagnose_api", diagnose_api), ("park_api", park_api),
        ("implement_web", implement_web), ("verify_web", verify_web),
        ("diagnose_web", diagnose_web), ("park_web", park_web),
        ("launch_stack", launch_stack), ("verify_e2e", verify_e2e),
        ("fix_e2e", fix_e2e), ("diagnose_e2e", diagnose_e2e), ("park_e2e", park_e2e),
        ("teardown", teardown), ("finish", finish), ("gate_pr", gate_pr),
        ("merge_slice", merge_slice),
    ]
    for name, fn in nodes:
        g.add_node(name, fn)

    chain = ["ingest", "gap_detect", "gate_spec", "clone_starter", "provision_db",
             "baseline", "commit_specs", "architect", "contract_lint",
             "gate_contract", "migrate", "write_tests", "implement_api"]
    g.add_edge(START, chain[0])
    for a, b in zip(chain, chain[1:]):
        g.add_edge(a, b)

    # api loop
    g.add_edge("implement_api", "verify_api")
    g.add_conditional_edges("verify_api", _router("api", "implement_web"),
                            {"green": "implement_web", "retry": "diagnose_api",
                             "park": "park_api"})
    g.add_edge("diagnose_api", "implement_api")
    g.add_edge("park_api", "implement_web")

    # web loop
    g.add_edge("implement_web", "verify_web")
    g.add_conditional_edges("verify_web", _router("web", "launch_stack"),
                            {"green": "launch_stack", "retry": "diagnose_web",
                             "park": "park_web"})
    g.add_edge("diagnose_web", "implement_web")
    g.add_edge("park_web", "launch_stack")

    # e2e loop against the live docker stack
    g.add_edge("launch_stack", "verify_e2e")
    g.add_conditional_edges("verify_e2e", _router("e2e", "teardown"),
                            {"green": "teardown", "retry": "diagnose_e2e",
                             "park": "park_e2e"})
    g.add_edge("diagnose_e2e", "fix_e2e")
    g.add_edge("fix_e2e", "verify_e2e")
    g.add_edge("park_e2e", "teardown")

    g.add_edge("teardown", "finish")
    g.add_edge("finish", "gate_pr")
    g.add_edge("gate_pr", "merge_slice")
    g.add_edge("merge_slice", END)
    return g.compile(checkpointer=checkpointer,
                     interrupt_after=interrupt_after or [])


# NODE_ORDER and LADDER live in config.py, which does NOT import langgraph — so
# `run --until <bad node>` and `--dry-run` stay usable on a checkout that only
# has pyyaml installed. Re-exported here for convenience.
from .config import LADDER, NODE_ORDER  # noqa: E402,F401


# Entry point lives in __main__.py so paths are resolved by discovery:
#   python -m project_factory run <slug>
