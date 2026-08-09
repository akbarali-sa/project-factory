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
        "  * Slice ids: snake_case, prefixed 'slice_'.\n"
        # Learned the expensive way: four correct vertical slices are not a
        # product. On the first full run every screen shipped and worked, and
        # four of them were reachable ONLY by typing a URL, because no slice
        # owned navigation. The gap had to be closed by hand after the run.
        "  * FINALLY, always add ONE horizontal slice as the LAST wave, with "
        '"kind": "horizontal" and an empty event_ids list. Vertical slices '
        "deliver screens; nothing makes them one application. This slice owns "
        "the hub/index screen for the primary aggregate, the global navigation "
        "covering every top-level workflow, the cross-links between screens "
        "that belong to one user journey, and whatever list endpoint the index "
        "needs. Without it the delivered app has screens no user can reach.\n\n"
        f"Project: {board.get('name')}\n\n"
        f"In-scope business events:\n{json.dumps(digest, indent=1)}\n\n"
        "Return ONLY JSON:\n"
        '{"slices": [{"id": "slice_...", "name": "...", "wave": 1, '
        '"bounded_context": "...", "event_ids": ["be_..."], '
        '"rationale": "..."}, '
        '{"id": "slice_navigation_and_hub", "name": "...", "wave": <last>, '
        '"kind": "horizontal", "bounded_context": "...", "event_ids": [], '
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
        # A horizontal slice OWNS no events — it composes what earlier waves
        # already shipped into one navigable product. Demanding event_ids of it
        # would force it to claim events another slice owns, which the
        # "event in two slices" check below would then (correctly) reject.
        if not s.get("event_ids") and s.get("kind") != "horizontal":
            errors.append(f"{sid}: no event_ids")

    # Exactly one horizontal slice, and it must come last: it links screens, so
    # every screen has to exist before it runs. A horizontal slice in wave 2 of
    # 4 would wire up half a product and call the project done.
    horizontals = [s for s in slices if s.get("kind") == "horizontal"]
    if len(horizontals) > 1:
        errors.append(f"more than one horizontal slice: "
                      f"{[s.get('id') for s in horizontals]}")
    if horizontals:
        h = horizontals[0]
        top = max((s.get("wave") or 0) for s in slices)
        if h.get("wave") != top:
            errors.append(f"{h.get('id')}: horizontal slice must be the last "
                          f"wave (is {h.get('wave')}, last is {top})")
        if not h.get("rationale"):
            errors.append(f"{h.get('id')}: horizontal slice needs a rationale")

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
    horizontal = planned.get("kind") == "horizontal"
    if horizontal:
        # It owns no events, so "its" events would be an empty list and the
        # agent would author scenarios for nothing. Give it every event the
        # earlier waves DID ship: those are the screens it has to connect.
        owned = {eid for s in (load_plan(project) or {}).get("slices", [])
                 for eid in (s.get("event_ids") or [])}
        events = [e for e in board.get("business_events", []) if e["id"] in owned]
    else:
        events = [e for e in board.get("business_events", [])
                  if e["id"] in set(planned["event_ids"])]
    exemplar = _EXEMPLAR.read_text()

    kind_brief = (
        "Author the ORACLE for the project's HORIZONTAL slice — the one that "
        "turns already-delivered screens into a usable application. It owns no "
        "business events of its own; the events below are what EARLIER waves "
        "shipped, and they are the screens this slice must connect.\n\n"
        "Specify, as scenarios:\n"
        "  * a hub/index screen listing the primary aggregate, including its "
        "empty, loading and error states;\n"
        "  * global navigation reaching every top-level workflow, with the "
        "correct entry marked current for a given URL;\n"
        "  * cross-links between screens in one user journey (from a list row "
        "to its detail, from a detail to each workflow scoped to it);\n"
        "  * whatever list endpoint the index needs, with its auth rules.\n\n"
        "The binding requirement: after this slice, EVERY screen the project "
        "delivered is reachable by clicking from the landing page. No user "
        "should ever have to type a URL. Write at least one e2e scenario that "
        "walks that path click by click.\n\n"
    ) if horizontal else (
        "Author the ORACLE for one vertical slice: the scenarios file the Test "
        "Author will convert into executable tests, and the single source of "
        "truth for correctness. Implementers never see or edit it.\n\n"
    )

    prompt = (
        kind_brief +
        "This file is the single source of truth for correctness; "
        "implementers never see or edit it.\n\n"
        "Hold yourself to the standard of this exemplar (structure, precision, "
        "explicit rejection paths, test-data strategy). Copy its FORM, never "
        "its domain content:\n\n"
        f"```yaml\n{exemplar}\n```\n\n"
        f"Slice to author (from the approved project plan):\n"
        f"{json.dumps({k: planned[k] for k in ('id', 'name', 'wave', 'bounded_context', 'event_ids')}, indent=1)}\n\n"
        + (f"Events shipped by earlier waves — the screens to connect, NOT "
           f"behaviour to respecify:\n{json.dumps(events, indent=1)[:20000]}\n\n"
           if horizontal else
           f"Full board events for this slice:\n{json.dumps(events, indent=1)[:20000]}\n\n")
        + "Requirements:\n"
        "  * slice.id / wave / bounded_context must match the plan exactly; "
        "approved_by/approved_at stay null.\n"
        "  * Keep the exemplar's starter_constraints block VERBATIM — it is "
        "starter-kit truth, not domain content.\n"
        "  * Every scenario: unique id (SC-/WEB-/E2E-), title, traces_to, "
        "given/when/then with concrete values and explicit HTTP status codes.\n"
        "  * Cover the happy path AND every rejection/validation path the "
        "board's acceptance criteria imply.\n"
        + ("  * This slice owns no events, so traces_to carries its own "
           "SC-/WEB-/E2E- ids. Never restate an earlier wave's behaviour as a "
           "scenario here: assume its data exists, set it up in given, and "
           "assert only reachability, listing, navigation and linking.\n\n"
           if horizontal else
           "  * Scenarios may trace only to this slice's events (or to SC-/WEB- "
           "ids for e2e). An event whose open questions block specification "
           "goes under provisional_scenarios with blocked_by, NOT under "
           "scenarios.\n"
           "  * Behaviour needing data from earlier waves belongs to those "
           "waves — reference it in given, do not respecify it.\n\n")
        + "Return ONLY the YAML document (no fences, no commentary)."
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
