"""
Project-level planning: board → wave plan → drafted oracles.

Two agents live here, both OUTSIDE the per-slice graph because their output is
what makes a slice thread possible at all:

  plan_slices     Partitions the board's in-scope events into wave-ordered
                  vertical slices → specs/project-plan.json. Runs once per
                  project. The plan is the PROJECT-LEVEL GATE: run-project
                  refuses to build anything until a human has approved it
                  (approve-plan), because the plan decides what gets built,
                  in what order, and what is explicitly not built.

  author_oracle   Drafts one slice's scenarios.yaml from the board + plan,
                  holding it to the bundled exemplar's standard. The draft is
                  mechanically validated here (structure, traceability); its
                  CONTENT is reviewed per the project's gate policy — with
                  gates.spec=auto the human accountability point moves up to
                  the plan gate and Gate C, which is the run-project default.

The oracle philosophy shift is deliberate and explicit: hand-authored oracles
(the run-slice flow) remain the gold standard; run-project trades that for
agent-drafted + plan-gated oracles so a whole project can run from a board.
"""

from __future__ import annotations

import datetime
import json
import pathlib
import re

import yaml

from . import config as cfgmod
from .models import Usage, claude

PLAN_FILENAME = "project-plan.json"

_EXEMPLAR = pathlib.Path(__file__).parent / "exemplars" / "scenarios-exemplar.yaml"


class PlanError(RuntimeError):
    """A plan or drafted oracle failed validation. Message names every failure."""


# -----------------------------------------------------------------------------
# Plan file access
# -----------------------------------------------------------------------------
def plan_path(project: cfgmod.Project) -> pathlib.Path:
    return project.dir / "specs" / PLAN_FILENAME


def load_plan(project: cfgmod.Project) -> dict | None:
    p = plan_path(project)
    if not p.exists():
        return None
    return json.loads(p.read_text())


def save_plan(project: cfgmod.Project, plan: dict) -> pathlib.Path:
    p = plan_path(project)
    p.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n")
    return p


def approve_plan(project: cfgmod.Project, by: str) -> dict:
    """Record human approval on the plan. Mirrors record_db_reset_consent's
    contract: call ONLY on an explicit human action."""
    plan = load_plan(project)
    if plan is None:
        raise PlanError("no project plan to approve — run run-project first")
    plan["approved_by"] = by
    plan["approved_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    save_plan(project, plan)
    return plan


def plan_is_approved(plan: dict | None) -> bool:
    return bool(plan and plan.get("approved_by"))


# -----------------------------------------------------------------------------
# Board digest — what the planner sees
# -----------------------------------------------------------------------------
def _board_events(board: dict) -> tuple[list[dict], list[dict]]:
    """(in_scope, out_of_scope) events, same rule as graph.ingest."""
    in_scope, out = [], []
    for e in board.get("business_events", []):
        if "Out-of-Scope" in (e.get("bounded_context") or ""):
            out.append(e)
        else:
            in_scope.append(e)
    return in_scope, out


def _digest_event(e: dict) -> dict:
    impl = e.get("implementation", {}) or {}
    return {
        "id": e["id"],
        "name": e["name"],
        "bounded_context": e.get("bounded_context"),
        "flow_id": e.get("flow_id"),
        "actors": [a.get("label") for a in e.get("actors", [])],
        "acceptance_criteria": impl.get("acceptance_criteria", []),
        "business_rules": impl.get("business_rules", []),
        "open_questions": [q["question"][:200] for q in e.get("questions", [])
                           if q.get("status") != "answered"],
        "nfrs": [n.get("requirement", "")[:200] for n in e.get("nfrs", [])],
    }


# -----------------------------------------------------------------------------
# Agent 1: the planner
# -----------------------------------------------------------------------------
def plan_slices(project: cfgmod.Project, *, usage: Usage | None = None,
                budget_usd: float | None = None,
                log_path: pathlib.Path | None = None) -> dict:
    """
    Partition the board into wave-ordered slices and persist the plan
    (unapproved). Raises PlanError when the agent's plan fails validation.
    """
    board = json.loads(project.board_path.read_text())
    in_scope, out_of_scope = _board_events(board)
    digest = [_digest_event(e) for e in in_scope]

    out = claude(
        "planner",
        "Partition a project's business events into VERTICAL SLICES for "
        "incremental delivery on a NestJS + Prisma + Next.js starter.\n\n"
        "Rules:\n"
        "  * A slice is independently shippable: it owns its aggregates, its "
        "API surface, and the screens that exercise them end to end.\n"
        "  * Group by bounded context first, then by dependency: a slice may "
        "only depend on earlier waves.\n"
        "  * 3-6 events per slice is the sweet spot; never split one "
        "command/event pair across slices.\n"
        "  * Move an event to out_of_scope (with the reason) when it is "
        "genuinely unbuildable now: hard distributed-systems edges (offline "
        "sync, conflict resolution), events whose open questions block "
        "specification, or pure off-system context. Do not park something "
        "merely because it is late in the flow.\n"
        "  * Slice ids: snake_case, prefixed 'slice_'.\n\n"
        f"Project: {board.get('name')}\n\n"
        f"In-scope business events:\n{json.dumps(digest, indent=1)}\n\n"
        "Return ONLY JSON:\n"
        '{"slices": [{"id": "slice_...", "name": "...", "wave": 1, '
        '"bounded_context": "...", "event_ids": ["be_..."], '
        '"rationale": "..."}], '
        '"out_of_scope": [{"id": "be_...", "reason": "..."}]}',
        usage=usage, budget_usd=budget_usd, log_path=log_path,
    )
    try:
        plan = json.loads(out[out.index("{"):out.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError) as e:
        raise PlanError(f"planner returned unparseable JSON: {e}\n{out[:500]}")

    errors = validate_plan(plan, in_scope)
    if errors:
        raise PlanError("plan failed validation:\n  " + "\n  ".join(errors))

    plan["board"] = project.board_path.name
    plan["approved_by"] = None
    plan["approved_at"] = None
    save_plan(project, plan)
    return plan


def validate_plan(plan: dict, in_scope_events: list[dict]) -> list[str]:
    """Every in-scope event lands in exactly one slice or is explicitly parked
    with a reason. Returns a list of human-readable failures (empty = valid)."""
    errors: list[str] = []
    slices = plan.get("slices") or []
    if not slices:
        return ["plan has no slices"]

    ids = [s.get("id", "") for s in slices]
    if len(ids) != len(set(ids)):
        errors.append(f"duplicate slice ids: {ids}")
    for s in slices:
        sid = s.get("id", "")
        if not re.fullmatch(r"slice_[a-z0-9_]+", sid):
            errors.append(f"bad slice id '{sid}' (want slice_<snake_case>)")
        if not isinstance(s.get("wave"), int) or s["wave"] < 1:
            errors.append(f"{sid}: wave must be an int >= 1")
        if not s.get("event_ids"):
            errors.append(f"{sid}: no event_ids")

    board_ids = {e["id"] for e in in_scope_events}
    assigned: dict[str, str] = {}
    for s in slices:
        for eid in s.get("event_ids", []):
            if eid not in board_ids:
                errors.append(f"{s.get('id')}: unknown event '{eid}'")
            elif eid in assigned:
                errors.append(f"event '{eid}' in both {assigned[eid]} and {s.get('id')}")
            else:
                assigned[eid] = s.get("id", "?")

    parked = {o.get("id"): o.get("reason") for o in plan.get("out_of_scope", [])}
    for oid, reason in parked.items():
        if not reason:
            errors.append(f"out_of_scope '{oid}' has no reason")
    unaccounted = board_ids - set(assigned) - set(parked)
    if unaccounted:
        errors.append(f"events neither sliced nor parked: {sorted(unaccounted)}")
    return errors


# -----------------------------------------------------------------------------
# Agent 2: the oracle author
# -----------------------------------------------------------------------------
def scenarios_path_for(project: cfgmod.Project, slice_id: str) -> pathlib.Path:
    stem = slice_id.removeprefix("slice_").replace("_", "-")
    return project.dir / "specs" / f"{stem}.scenarios.yaml"


def author_oracle(project: cfgmod.Project, planned: dict, *,
                  usage: Usage | None = None, budget_usd: float | None = None,
                  log_path: pathlib.Path | None = None) -> pathlib.Path:
    """
    Draft specs/<slice>.scenarios.yaml for one planned slice. Validates the
    draft mechanically; retries once with the validation errors appended, then
    raises PlanError. Never overwrites an existing scenarios file.
    """
    target = scenarios_path_for(project, planned["id"])
    if target.exists():
        return target

    board = json.loads(project.board_path.read_text())
    events = [e for e in board.get("business_events", [])
              if e["id"] in set(planned["event_ids"])]
    exemplar = _EXEMPLAR.read_text()

    prompt = (
        "Author the ORACLE for one vertical slice: the scenarios file the Test "
        "Author will convert into executable tests, and the single source of "
        "truth for correctness. Implementers never see or edit it.\n\n"
        "Hold yourself to the standard of this exemplar (structure, precision, "
        "explicit rejection paths, test-data strategy). Copy its FORM, never "
        "its domain content:\n\n"
        f"```yaml\n{exemplar}\n```\n\n"
        f"Slice to author (from the approved project plan):\n"
        f"{json.dumps({k: planned[k] for k in ('id', 'name', 'wave', 'bounded_context', 'event_ids')}, indent=1)}\n\n"
        f"Full board events for this slice:\n{json.dumps(events, indent=1)[:20000]}\n\n"
        "Requirements:\n"
        "  * slice.id / wave / bounded_context must match the plan exactly; "
        "approved_by/approved_at stay null.\n"
        "  * Keep the exemplar's starter_constraints block VERBATIM — it is "
        "starter-kit truth, not domain content.\n"
        "  * Every scenario: unique id (SC-/WEB-/E2E-), title, traces_to, "
        "given/when/then with concrete values and explicit HTTP status codes.\n"
        "  * Cover the happy path AND every rejection/validation path the "
        "board's acceptance criteria imply.\n"
        "  * Scenarios may trace only to this slice's events (or to SC-/WEB- "
        "ids for e2e). An event whose open questions block specification goes "
        "under provisional_scenarios with blocked_by, NOT under scenarios.\n"
        "  * Behaviour needing data from earlier waves belongs to those waves "
        "— reference it in given, do not respecify it.\n\n"
        "Return ONLY the YAML document (no fences, no commentary)."
    )

    errors: list[str] = []
    for attempt in range(2):
        out = claude(
            "oracle_author",
            prompt if not errors
            else prompt + "\n\nYour previous draft failed validation — fix "
                          "exactly these and return the full YAML again:\n  "
                          + "\n  ".join(errors),
            attempt=attempt, usage=usage, budget_usd=budget_usd, log_path=log_path,
        )
        text = _strip_fences(out)
        errors = validate_oracle(text, planned)
        if not errors:
            header = (
                "# =============================================================================\n"
                "# ORACLE ARTIFACT — drafted by the factory's oracle_author agent from the\n"
                "# approved project plan; reviewed per this project's gate policy.\n"
                "# The Test Author converts these scenarios into executable tests. The\n"
                "# Implementer is NEVER allowed to edit this file or the tests derived from it.\n"
                "# =============================================================================\n\n"
            )
            target.write_text(header + text.strip() + "\n")
            return target

    raise PlanError(
        f"oracle for {planned['id']} failed validation after 2 attempts:\n  "
        + "\n  ".join(errors))


def _strip_fences(out: str) -> str:
    m = re.search(r"```(?:yaml)?\s*\n(.*?)```", out, re.S)
    return m.group(1) if m else out


def validate_oracle(text: str, planned: dict) -> list[str]:
    """Mechanical validation of a drafted oracle. Content review is the gate's
    job; this catches everything a schema can catch."""
    errors: list[str] = []
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        return [f"not valid YAML: {e}"]
    if not isinstance(data, dict):
        return ["not a YAML mapping"]

    sl = data.get("slice") or {}
    if sl.get("id") != planned["id"]:
        errors.append(f"slice.id '{sl.get('id')}' != planned '{planned['id']}'")
    if sl.get("wave") != planned["wave"]:
        errors.append(f"slice.wave {sl.get('wave')} != planned {planned['wave']}")
    if sl.get("approved_by") is not None:
        errors.append("slice.approved_by must be null in a draft")

    scenarios = data.get("scenarios") or []
    if len(scenarios) < 3:
        errors.append(f"only {len(scenarios)} scenarios — a slice needs >= 3 "
                      "(happy path + rejection paths)")
    if not data.get("e2e_scenarios"):
        errors.append("no e2e_scenarios — every slice must prove itself "
                      "against the real stack")

    seen: set[str] = set()
    allowed = set(planned["event_ids"])
    for group in ("scenarios", "web_scenarios", "e2e_scenarios"):
        for s in data.get(group) or []:
            sid = s.get("id", "")
            if not sid:
                errors.append(f"{group}: scenario without id")
                continue
            if sid in seen:
                errors.append(f"duplicate scenario id {sid}")
            seen.add(sid)
            for k in ("title", "when", "then") if group != "web_scenarios" else ("title",):
                if not s.get(k):
                    errors.append(f"{sid}: missing {k}")
            for t in s.get("traces_to", []) or []:
                if t in allowed or t in seen or re.fullmatch(r"(FR|UC|SC|WEB|E2E)-\w+", t):
                    continue
                errors.append(f"{sid}: traces_to '{t}' is neither a slice "
                              "event nor a scenario/requirement id")
    return errors
