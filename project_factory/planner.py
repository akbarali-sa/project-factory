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
from . import harness
from . import livelog
from .models import Usage, claude

PLAN_FILENAME = "project-plan.json"

_EXEMPLAR = pathlib.Path(__file__).parent / "exemplars" / "scenarios-exemplar.yaml"
_STARTER_CONSTRAINTS = (pathlib.Path(__file__).parent / "exemplars"
                        / "starter-constraints.yaml")


def starter_constraints() -> dict:
    """Canonical starter-kit truth block, parsed. Single source for both the
    prompt injection and the validator's verbatim check — the exemplar no
    longer carries it, so exemplar style and starter truth can evolve
    independently."""
    return yaml.safe_load(_STARTER_CONSTRAINTS.read_text())["starter_constraints"]


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


PER_SLICE_BUDGET_USD = 50.0
# Bounds the planning phase itself, before any slices exist to scale from.
PRE_PLAN_BUDGET_USD = 100.0


def project_budget_usd(project: cfgmod.Project, plan: dict | None = None) -> float:
    """
    The project-wide ceiling. An explicit number in run.json (or defaults.json)
    wins; otherwise the budget scales with the plan — $50 per planned slice —
    so a 3-slice project isn't handed an 8-slice wallet. Until the planner has
    produced slices there is nothing to scale from, so a flat $100 bounds the
    planning phase.
    """
    explicit = project.cfg.get("project_budget_usd")
    if isinstance(explicit, (int, float)) and not isinstance(explicit, bool):
        return float(explicit)
    if plan is None:
        plan = load_plan(project)
    n = len((plan or {}).get("slices") or [])
    return PER_SLICE_BUDGET_USD * n if n else PRE_PLAN_BUDGET_USD


# -----------------------------------------------------------------------------
# Board digest — what the planner sees
# -----------------------------------------------------------------------------
def _board_events(board: dict) -> tuple[list[dict], list[dict]]:
    """(in_scope, out_of_scope) events, same rule as graph.ingest
    (config.event_in_scope: out_of_scope flag, Deferred card, or legacy
    bounded-context marker)."""
    in_scope, out = [], []
    for e in board.get("business_events", []):
        if cfgmod.event_in_scope(e):
            in_scope.append(e)
        else:
            out.append(e)
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
        "open_questions": [q["question"] for q in e.get("questions", [])
                           if q.get("status") != "answered"],
        "nfrs": [n.get("requirement", "") for n in e.get("nfrs", [])],
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

    With a reviewed backlog present (specs/backlog.json) the plan is CONVERTED
    deterministically instead — a co-architect already made the decomposition
    decisions, and an agent re-deriving them from raw events is how reviewed
    judgement gets silently replaced by plausible guesses.
    """
    board = cfgmod.load_board(project.board_path)
    if project.backlog_path:
        return plan_from_backlog(project, board)
    in_scope, out_of_scope = _board_events(board)
    digest = [_digest_event(e) for e in in_scope]

    # cwd = the PROJECT dir, deliberately: without it the agent inherits the
    # factory root as its working directory and explores the factory's own
    # source instead of the engagement's specs — truev's oracle_author read
    # planner.py/harness.py to reverse-engineer the validator and listed
    # OTHER projects' boards (a confidentiality leak between engagements).
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
        cwd=str(project.dir),
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


# -----------------------------------------------------------------------------
# Deterministic converter: reviewed backlog -> plan (no agent)
# -----------------------------------------------------------------------------
def _reconcile_node_ref(ref: str, event_ids: set[str]) -> str | None:
    """Map a backlog board_node reference onto an actual event id. Handles the
    drift a hand-written backlog accumulates: trailing '(annotation)' text,
    suffixed ids (be_count_synced_to_server -> be_count_synced), and small
    renames — but only when the match is UNIQUE. Returns None when unresolved:
    the caller fails loudly, never silently drops scope."""
    base = re.sub(r"\s*\(.*\)\s*$", "", ref).strip()
    if base in event_ids:
        return base
    prefixed = [i for i in event_ids if base.startswith(i + "_")]
    if len(prefixed) == 1:
        return prefixed[0]
    import difflib
    close = difflib.get_close_matches(base, sorted(event_ids), n=2, cutoff=0.7)
    if len(close) == 1:
        return close[0]
    if len(close) == 2:  # accept a clear winner, reject a coin flip
        r0 = difflib.SequenceMatcher(None, base, close[0]).ratio()
        r1 = difflib.SequenceMatcher(None, base, close[1]).ratio()
        if r0 - r1 > 0.1:
            return close[0]
    return None


def _story_digest(story: dict) -> dict:
    """What a slice carries forward from a backlog story: enough for the
    oracle author to honour the reviewed decomposition without re-reading
    the backlog."""
    return {k: story[k] for k in
            ("id", "title", "wave", "status", "actor", "aggregate", "emits",
             "depends_on", "tasks", "caveats", "blocking_questions", "nfrs")
            if story.get(k) is not None}


def plan_from_backlog(project: cfgmod.Project, board: dict) -> dict:
    """
    Convert specs/backlog.json (a co-architect's reviewed story decomposition)
    into the project plan — deterministically, no agent call. Slicing follows
    the backlog's epics: one vertical slice per domain epic (a bounded
    context), stories embedded, plus the standard horizontal navigation slice
    last. Foundation stories and spikes are recorded on the plan (the starter
    kit covers most Foundation scaffolding; spikes deliver decisions, not
    code) rather than becoming slices.

    Scope guard: a story referencing an event the board marks out_of_scope or
    Deferred is an error, and every board_node reference must reconcile onto a
    real event id — unresolved references fail the plan, never vanish.
    """
    backlog = json.loads(project.backlog_path.read_text())
    in_scope, excluded = _board_events(board)
    ids = {e["id"] for e in in_scope}
    excluded_ids = {e["id"] for e in excluded}

    errors: list[str] = []
    aliases: dict[str, str] = {}

    def resolve(story: dict) -> list[str]:
        out = []
        for ref in story.get("board_nodes") or []:
            got = _reconcile_node_ref(ref, ids)
            if got is None:
                if _reconcile_node_ref(ref, excluded_ids):
                    errors.append(
                        f"{story['id']}: board_node '{ref}' is out of scope / "
                        f"Deferred on the board — a story may not price it")
                else:
                    errors.append(f"{story['id']}: board_node '{ref}' matches "
                                  f"no event on the board")
                continue
            if got != ref:
                aliases[ref] = got
            out.append(got)
        return out

    epics = {e["id"]: e for e in backlog.get("epics", [])}
    stories = backlog.get("stories", [])
    domain_epics = sorted(
        (e for e in epics.values() if e.get("bounded_context")),
        key=lambda e: min((s["wave"] for s in stories if s["epic"] == e["id"]),
                          default=99))
    foundation = [s for s in stories
                  if not epics.get(s["epic"], {}).get("bounded_context")]

    # An epic carrying no stories is a heading, not deliverable work — it
    # would otherwise become a slice with no events and fail validation with
    # a message about the plan rather than about the backlog.
    domain_epics = [e for e in domain_epics
                    if any(s["epic"] == e["id"] for s in stories)]

    slices, epic_slice_id = [], {}
    for wave, epic in enumerate(domain_epics, start=1):
        bc = epic["bounded_context"]
        sid = "slice_" + re.sub(r"[^a-z0-9]+", "_", bc.lower()).strip("_")
        epic_slice_id[epic["id"]] = sid
        epic_stories = [s for s in stories if s["epic"] == epic["id"]]
        event_ids: list[str] = []
        for s in epic_stories:
            event_ids.extend(eid for eid in resolve(s)
                             if eid not in event_ids)
        slices.append({
            "id": sid, "name": bc, "wave": wave, "bounded_context": bc,
            "event_ids": event_ids,
            "stories": [_story_digest(s) for s in
                        sorted(epic_stories, key=lambda s: s["wave"])],
            "depends_on": [epic_slice_id[d] for d in epic.get("depends_on", [])
                           if d in epic_slice_id],
            "rationale": epic.get("note") or f"backlog epic {epic['id']}",
        })

    slices.append({
        "id": "slice_navigation_and_hub",
        "name": "Navigation & hub — one navigable application",
        "wave": len(slices) + 1, "kind": "horizontal",
        "bounded_context": slices[0]["bounded_context"] if slices else "",
        "event_ids": [],
        "rationale": "Vertical slices deliver screens; nothing makes them one "
                     "application. Hub/index, global navigation, journey "
                     "cross-links — every delivered screen reachable by "
                     "clicking from the landing page.",
    })

    # Wave-order sanity from the board's own edges + policy triggers: a
    # producer event may never sit in a LATER wave than its consumer. This is
    # the check whose absence hid a consumer scheduled three waves before its
    # producer (BL-007) in the artifact this converter replaces.
    wave_of = {eid: s["wave"] for s in slices for eid in s.get("event_ids", [])}
    dep_edges = [(e.get("from"), e.get("to")) for e in board.get("edges", [])]
    dep_edges += [(e.get("policy", {}).get("trigger_event_id"), e["id"])
                  for e in in_scope if (e.get("policy") or {}).get("trigger_event_id")]
    for src, dst in dep_edges:
        if src in wave_of and dst in wave_of and wave_of[src] > wave_of[dst]:
            errors.append(f"wave inversion: '{dst}' (wave {wave_of[dst]}) "
                          f"consumes '{src}' scheduled later (wave {wave_of[src]})")

    # An epic is an ESTIMATION unit; a slice is a DELIVERY unit, and they are
    # not the same size. barcode-v2's ingestion epic became one 7-event slice
    # whose oracle ran to 33 scenarios: the Test Author cost $12.26 and
    # finished two seconds inside its ceiling, and the Implementer was killed
    # by it. Flagged rather than auto-split — splitting an epic mid-dependency
    # can produce a slice nobody can demo (parsing with no upload), which is a
    # judgement call for whoever approves the plan, not a rule to apply blind.
    for s in slices:
        if len(s.get("event_ids", [])) > 6:
            s["size_warning"] = (
                f"{len(s['event_ids'])} events from {len(s.get('stories', []))} "
                "stories — larger than the 3-6 event slice that runs "
                "comfortably. Expect a long, expensive Test Author and "
                "Implementer; consider splitting it along story boundaries "
                "before approving.")

    unsliced = ids - set(wave_of)
    plan = {
        "slices": slices,
        "out_of_scope": [{"id": eid, "reason": "in scope on the board but "
                          "owned by no backlog story — flagged for review"}
                         for eid in sorted(unsliced)],
        "source": f"backlog.json (deterministic converter; "
                  f"{len(stories)} stories, {len(domain_epics)} domain epics)",
        "aliases": aliases,
        "hard_constraints": backlog.get("hard_constraints", []),
        "not_priced": backlog.get("not_priced", []),
        "foundation": [{k: s.get(k) for k in ("id", "title", "wave", "tasks")}
                       for s in foundation],
        "spikes": backlog.get("spikes", []),
    }

    errors += validate_plan(plan, in_scope)
    if errors:
        raise PlanError("backlog conversion failed:\n  " + "\n  ".join(errors))

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
# Agent 1.5: the assumptions register
# -----------------------------------------------------------------------------
def assumptions_path(project: cfgmod.Project) -> pathlib.Path:
    return project.dir / "specs" / "assumptions.yaml"


def load_assumptions(project: cfgmod.Project) -> dict | None:
    p = assumptions_path(project)
    if not p.exists():
        return None
    return yaml.safe_load(p.read_text())


def draft_assumptions(project: cfgmod.Project, *, usage: Usage | None = None,
                      budget_usd: float | None = None,
                      log_path: pathlib.Path | None = None) -> pathlib.Path:
    """
    Turn the board's PENDING questions into specs/assumptions.yaml: one
    recorded working assumption per question that blocks an in-scope event.
    Questions the board already answers are imported verbatim as `resolved`
    (deterministically — an agent must not paraphrase a recorded decision).

    Rationale: a factory cannot wait for a client. The oracle author will
    otherwise resolve every open question implicitly and differently per
    slice; this register makes each resolution explicit, auditable, and
    revisitable when the real answer arrives. Never overwrites an existing
    file. Falls under the plan gate's accountability umbrella.
    """
    target = assumptions_path(project)
    if target.exists():
        return target

    # The decision register is the richer source when it exists: it carries
    # the board's questions PLUS analyst- and human-raised ones, and human
    # answers recorded there must reach the oracle author verbatim. Fall back
    # to raw board questions only for pre-register projects.
    from . import decisions as decmod

    pending, resolved = [], []
    register = decmod.load_decisions(project)
    if register:
        for q in register.get("questions", []):
            if q.get("status") == "answered":
                resolved.append({
                    "question_id": q["id"], "question": q.get("question"),
                    "answer": q.get("answer"),
                    "source": f"decision register — answered by "
                              f"{q.get('answered_by')} (imported verbatim)"})
            else:
                pending.append({
                    "id": q["id"], "question": q.get("question"),
                    "criticality": ("critical" if q.get("blocker") == "start"
                                    else "normal"),
                    "event_id": q.get("event_id"),
                    "event_name": q.get("context")})
    else:
        board = cfgmod.load_board(project.board_path)
        in_scope, _ = _board_events(board)
        seen: set = set()
        for e in in_scope:
            for q in e.get("questions", []):
                if q.get("id") in seen:
                    continue
                seen.add(q.get("id"))
                if q.get("status") == "answered":
                    resolved.append({
                        "question_id": q.get("id"), "question": q.get("question"),
                        "answer": q.get("answer"),
                        "source": "board (recorded answer — imported verbatim)"})
                else:
                    pending.append({
                        "id": q.get("id"), "question": q.get("question"),
                        "criticality": q.get("criticality", "normal"),
                        "event_id": e["id"], "event_name": e["name"]})

    if not pending:
        # Every recorded question is answered — nothing to assume. Write the
        # register with the answers alone; no agent call, no spend.
        data = {"working_assumptions": [], "resolved": resolved,
                "drafted_by": "no agent — every question was answered",
                "note": "All recorded questions carried answers when the "
                        "register was drafted."}
        target.write_text(
            "# ASSUMPTIONS REGISTER — every question was answered; no "
            "working assumptions were needed.\n\n"
            + yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
        return target

    backlog_ctx = ""
    if project.backlog_path:
        backlog = json.loads(project.backlog_path.read_text())
        backlog_ctx = (
            "\nReviewed-backlog context (constraints your assumptions must "
            "not violate):\n"
            f"hard_constraints: {json.dumps(backlog.get('hard_constraints', []), indent=1)}\n"
            f"not_priced (choose the assumption that keeps these OUT of scope): "
            f"{json.dumps([{k: n.get(k) for k in ('title', 'why_out', 'do_not_price')} for n in backlog.get('not_priced', [])], indent=1)}\n"
            f"spikes (provider decisions still open — assume the most "
            f"conventional provider-agnostic answer): "
            f"{json.dumps([{k: s.get(k) for k in ('name', 'questions_to_close')} for s in backlog.get('spikes', [])], indent=1)}\n")

    prompt = (
        "You are recording the WORKING ASSUMPTIONS an autonomous build will "
        "proceed on. The client is unavailable; every pending question below "
        "blocks an in-scope event, and the build cannot wait. For EACH "
        "pending question, choose the single most conventional, least-scope, "
        "most-reversible answer and record it as an assumption.\n\n"
        "Rules:\n"
        "  * assumption = a decision, stated as fact, buildable today — never "
        "'TBD', never a menu of options.\n"
        "  * Prefer the reading that keeps unpriced capabilities out of "
        "scope; never assume a deferred capability into existence.\n"
        # Deliberately data-driven: the ROLE/actor model is an engagement
        # fact carried by the board and the backlog's hard_constraints (in
        # backlog_ctx below) — hard-coding one engagement's role count here is
        # how barcode facts leak into every future project's assumptions.
        "  * Never invent actors, roles, or permission tiers beyond those "
        "the board and the hard constraints already name; an assumption may "
        "narrow the actor model, never widen it.\n"
        "  * rationale = one sentence on why this is the safe default and "
        "what would change if the client answers differently.\n\n"
        f"Pending questions ({len(pending)}):\n{json.dumps(pending, indent=1)}\n"
        f"{backlog_ctx}\n"
        "Return ONLY YAML:\n"
        "working_assumptions:\n"
        "  - id: a_<question_id>\n"
        "    question_id: <question_id>\n"
        "    criticality: <copied>\n"
        "    assumption: <the decision>\n"
        "    rationale: <one sentence>\n"
        "    impacts: [<event_ids>]\n"
    )

    errors: list[str] = []
    text = ""
    for attempt in range(2):
        out = claude(
            "assumptions_author",
            prompt if not errors
            else prompt + "\n\nYour previous draft failed validation — fix "
                          "exactly these and return the full YAML again:\n  "
                          + "\n  ".join(errors),
            cwd=str(project.dir),
            attempt=attempt, usage=usage, budget_usd=budget_usd,
            log_path=log_path)
        text = _strip_fences(out)
        errors = _validate_assumptions(text, pending)
        if not errors:
            break
        if log_path:
            livelog.append(log_path, f"assumptions draft attempt {attempt + 1} "
                           "failed validation: " + "; ".join(errors)[:400])
    if errors:
        raise PlanError("assumptions draft failed validation after 2 "
                        "attempts:\n  " + "\n  ".join(errors))

    data = yaml.safe_load(text)
    data["resolved"] = resolved
    data["drafted_by"] = "assumptions_author (agent)"
    data["note"] = ("Working assumptions for an autonomous run — each entry "
                    "resolves a question the client has not answered. "
                    "Revisit every impacted slice when real answers arrive.")
    target.write_text(
        "# ASSUMPTIONS REGISTER — drafted by the factory before oracle "
        "authoring.\n# One working assumption per pending board question "
        "blocking an in-scope event.\n# Falls under the project plan gate's "
        "accountability.\n\n"
        + yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    return target


def _validate_assumptions(text: str, pending: list[dict]) -> list[str]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        return [f"not valid YAML: {e}"]
    if not isinstance(data, dict) or not isinstance(
            data.get("working_assumptions"), list):
        return ["missing top-level working_assumptions list"]
    errors = []
    covered = set()
    for a in data["working_assumptions"]:
        qid = a.get("question_id")
        covered.add(qid)
        if not a.get("assumption") or "TBD" in str(a.get("assumption", "")):
            errors.append(f"{qid}: assumption missing or undecided")
    critical = {q["id"] for q in pending if q.get("criticality") == "critical"}
    missing = critical - covered
    if missing:
        errors.append(f"critical questions without an assumption: {sorted(missing)}")
    return errors


# -----------------------------------------------------------------------------
# Agent 1.6: the schema architect (project data backbone)
# -----------------------------------------------------------------------------
_BACKBONE_CARDINALITIES = {"one_to_one", "one_to_many", "many_to_many"}
_BACKBONE_DELETES = {"cascade", "restrict", "set_null"}


def backbone_path(project: cfgmod.Project) -> pathlib.Path:
    return project.dir / "specs" / "data-backbone.yaml"


def load_backbone(project: cfgmod.Project) -> str | None:
    p = backbone_path(project)
    return p.read_text() if p.exists() else None


def draft_backbone(project: cfgmod.Project, *, usage: Usage | None = None,
                   budget_usd: float | None = None,
                   log_path: pathlib.Path | None = None) -> pathlib.Path:
    """
    Draft specs/data-backbone.yaml once per project, after plan approval and
    the assumptions register: the ENTITY-LEVEL data model — entities, business
    keys, relations (cardinality + delete semantics) and the slice that OWNS
    each entity. Deliberately NOT columns, enums, indexes or constraints:
    those stay slice-owned, decided when the slice's scenarios exist, because
    the schema's best decisions are often deliberate non-decisions pending
    client answers (barcode-v2: no unique on ScanEvent while duplicate-scan
    semantics were an open question).

    Rationale: without this, cross-slice data decisions are made by whichever
    slice moves first, and every later add-a-back-relation forces a wholesale
    model redeclaration. Falls under the plan gate's accountability umbrella.
    Never overwrites an existing file — hand-author it before the run to keep
    full control.
    """
    target = backbone_path(project)
    if target.exists():
        return target

    plan = load_plan(project) or {}
    board = cfgmod.load_board(project.board_path)
    in_scope, _ = _board_events(board)
    slice_ids = [s["id"] for s in plan.get("slices", [])]
    owned = {eid for s in plan.get("slices", [])
             for eid in (s.get("event_ids") or [])}
    events = [_digest_event(e) for e in in_scope if e["id"] in owned]
    forbidden = _forbidden_capabilities(board)

    extras = ""
    if plan.get("hard_constraints"):
        extras += ("HARD CONSTRAINTS (engagement-wide, non-negotiable):\n"
                   f"{json.dumps(plan['hard_constraints'], indent=1)}\n\n")
    assumptions = load_assumptions(project)
    if assumptions:
        extras += ("APPROVED WORKING ASSUMPTIONS — each is a decided fact; "
                   "the backbone must be consistent with them:\n"
                   + yaml.safe_dump(
                       {"working_assumptions":
                        assumptions.get("working_assumptions", [])},
                       sort_keys=False, allow_unicode=True) + "\n")
    if forbidden:
        names = [f"{f['name']} ({f['id']})" for f in forbidden]
        extras += ("DELIBERATELY UNPRICED capabilities — model NO entity, "
                   f"relation or read model that exists to serve these: "
                   f"{names}\n\n")
    # The starter's schema is not a blank page: its `User` model carries the
    # whole auth stack. A backbone that names the account entity anything
    # else (truev drafted `UserAccount`) sends every slice into a collision
    # the contract validator rejects — the slice architect can't rename the
    # starter's model, and the oracle faithfully follows the backbone.
    extras += ("STARTER-KIT EXISTING MODELS — the generated repo starts from "
               "a starter whose apps/api/prisma/schema.prisma already ships "
               "the `User` model that the entire auth stack depends on "
               f"(starter rule: {starter_constraints()['schema_rule'].strip()!r}). "
               "The account/actor entity in your backbone MUST be the "
               "starter's `User` — extend it, never model a parallel account "
               "entity under another name.\n\n")

    prompt = (
        "You are the SCHEMA ARCHITECT for a whole project that will be built "
        "in vertical slices, one wave at a time, by agents who each see only "
        "their own slice. Draft the project's DATA BACKBONE: the one "
        "entity-level data model every slice must build against.\n\n"
        "Decide ONLY:\n"
        "  * the entities (PascalCase Prisma model names) and one-sentence "
        "purposes;\n"
        "  * each entity's business key (the identifier users see), or null;\n"
        "  * every relation between entities — including relations a LATER "
        "wave will need: declaring them now is the whole point;\n"
        "  * which plan slice OWNS each entity (the slice that first creates "
        "its table — the earliest wave that needs it);\n"
        "  * read models: derived projections a slice recomputes from owned "
        "entities, with their sources.\n\n"
        "Do NOT decide columns, enums, indexes, or uniqueness/constraints — "
        "those belong to the owning slice, when its scenarios exist. Where an "
        "OPEN QUESTION would decide a relation, follow the assumptions "
        "register; never resolve a question the register leaves open.\n"
        "Model nothing for deferred/unpriced capabilities or parked events.\n\n"
        f"The approved plan's slices (wave order):\n"
        f"{json.dumps([{k: s.get(k) for k in ('id', 'name', 'wave', 'bounded_context', 'event_ids')} for s in plan.get('slices', [])], indent=1)}\n\n"
        f"In-scope board events:\n{json.dumps(events, indent=1)}\n\n"
        + extras +
        "Return ONLY YAML in exactly this shape (no fences, no commentary):\n"
        "entities:\n"
        "  - name: <PascalCase>\n"
        "    purpose: <one sentence>\n"
        "    owned_by: <slice id from the plan>\n"
        "    business_key: <fieldName or null>\n"
        "    relations:\n"
        "      - to: <entity name>\n"
        "        cardinality: one_to_one | one_to_many | many_to_many\n"
        "        on_parent_delete: cascade | restrict | set_null\n"
        "read_models:\n"
        "  - name: <PascalCase>\n"
        "    purpose: <one sentence>\n"
        "    owned_by: <slice id>\n"
        "    derived_from: [<entity names>]\n"
    )

    errors: list[str] = []
    text = ""
    for attempt in range(2):
        out = claude(
            "schema_architect",
            prompt if not errors
            else prompt + "\n\nYour previous draft failed validation — fix "
                          "exactly these and return the full YAML again:\n  "
                          + "\n  ".join(errors),
            cwd=str(project.dir),
            attempt=attempt, usage=usage, budget_usd=budget_usd,
            log_path=log_path)
        text = _strip_fences(out)
        errors = _validate_backbone(text, slice_ids)
        if not errors:
            break
        if log_path:
            livelog.append(log_path, f"backbone draft attempt {attempt + 1} "
                           "failed validation: " + "; ".join(errors)[:400])
    if errors:
        raise PlanError("data backbone failed validation after 2 attempts:\n  "
                        + "\n  ".join(errors))

    target.write_text(
        "# DATA BACKBONE — drafted by the factory's schema_architect after plan\n"
        "# approval. Entities, business keys, relations and slice ownership are\n"
        "# decided HERE, once, project-wide. Columns, enums, indexes and\n"
        "# constraints stay slice-owned. Falls under the project plan gate's\n"
        "# accountability; edit freely before the first slice runs.\n\n"
        + text.strip() + "\n")
    return target


def _validate_backbone(text: str, slice_ids: list[str]) -> list[str]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        return [f"not valid YAML: {e}"]
    if not isinstance(data, dict) or not isinstance(data.get("entities"), list) \
            or not data["entities"]:
        return ["missing or empty top-level entities list"]
    errors: list[str] = []
    entity_names = {e.get("name") for e in data["entities"]
                    if isinstance(e, dict)}
    declared = entity_names | {r.get("name")
                               for r in data.get("read_models") or []
                               if isinstance(r, dict)}
    for e in data["entities"]:
        name = e.get("name") or "?"
        if not re.fullmatch(r"[A-Z][A-Za-z0-9]*", str(name)):
            errors.append(f"{name}: entity name must be a PascalCase word")
        if slice_ids and e.get("owned_by") not in slice_ids:
            errors.append(f"{name}: owned_by {e.get('owned_by')!r} is not a "
                          "slice id from the plan")
        for rel in e.get("relations") or []:
            if rel.get("to") not in declared:
                errors.append(f"{name}: relation to undeclared entity "
                              f"{rel.get('to')!r}")
            if rel.get("cardinality") not in _BACKBONE_CARDINALITIES:
                errors.append(f"{name}: bad cardinality {rel.get('cardinality')!r}")
            if rel.get("on_parent_delete") not in _BACKBONE_DELETES:
                errors.append(f"{name}: bad on_parent_delete "
                              f"{rel.get('on_parent_delete')!r}")
    for r in data.get("read_models") or []:
        name = r.get("name") or "?"
        if slice_ids and r.get("owned_by") not in slice_ids:
            errors.append(f"{name}: owned_by {r.get('owned_by')!r} is not a "
                          "slice id from the plan")
        for src in r.get("derived_from") or []:
            if src not in entity_names:
                errors.append(f"{name}: derived_from unknown entity {src!r}")
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

    board = cfgmod.load_board(project.board_path)
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
    starter_block = yaml.safe_dump({"starter_constraints": starter_constraints()},
                                   sort_keys=False, allow_unicode=True)
    # Vocabulary of every OTHER project in the workspace: their nouns may not
    # appear in this project's oracle unless this board also uses them.
    try:
        extra_nouns = harness.collect_domain_nouns(
            project.dir.parent, exclude_project=project.dir.name)
    except Exception:
        extra_nouns = []
    board_text = json.dumps(events) + json.dumps(planned)

    # Reviewed-decomposition extras (present when the plan came from a
    # backlog): the stories this slice implements, the engagement's hard
    # constraints, the assumptions register, and the capabilities that are
    # deliberately unpriced — which the oracle must NOT specify.
    plan = load_plan(project) or {}
    forbidden = _forbidden_capabilities(board)
    extras = ""
    if planned.get("stories"):
        extras += ("Reviewed backlog stories this slice implements — honour "
                   "their boundaries, task intent and caveats:\n"
                   f"{json.dumps(planned['stories'], indent=1)}\n\n")
    if plan.get("hard_constraints"):
        extras += ("HARD CONSTRAINTS (engagement-wide, non-negotiable):\n"
                   f"{json.dumps(plan['hard_constraints'], indent=1)}\n\n")
    assumptions = load_assumptions(project)
    if assumptions:
        extras += ("APPROVED WORKING ASSUMPTIONS — treat each as decided; "
                   "scenarios must be consistent with them (cite the "
                   "assumption id in a scenario comment where one is "
                   "load-bearing):\n"
                   + yaml.safe_dump(
                       {"working_assumptions":
                        assumptions.get("working_assumptions", [])},
                       sort_keys=False, allow_unicode=True) + "\n")
    uiux_map = project.dir / "specs" / "uiux-map.yaml"
    if uiux_map.exists():
        extras += ("APPROVED UI/UX MAP — the human-gated route map from the "
                   "UI/UX preview. BINDING: web scenarios must target these "
                   "routes and screens exactly, never a route or screen not "
                   "listed here, and each screen's key_states are states its "
                   "scenarios must cover:\n"
                   + uiux_map.read_text() + "\n")
    backbone = load_backbone(project)
    if backbone:
        extras += (
            "PROJECT DATA BACKBONE (project-wide contract, drafted once after "
            "plan approval — BINDING): entity names, business keys, relations "
            "and slice ownership are decided here. Use these entity names; an "
            "entity OWNED by an earlier wave is data your scenarios set up in "
            "given, never behaviour to respecify. Columns and constraints "
            "beyond this remain the slice's to decide:\n"
            f"```yaml\n{backbone}\n```\n\n")
    # The DELIVERED seed is a cross-slice contract. Wave 2's oracle pinned
    # john.doe as Operator across 12 frozen call sites; wave 3's oracle named
    # the same address as Admin, and the only ways to satisfy both were to
    # break wave 2's frozen tests (the implementer's choice) or re-point wave
    # 3's (the driver's cleanup). Giving the oracle author the shipped seed
    # removes the conflict at the source: role assignments already delivered
    # are facts, not choices.
    seed_file = (project.repo_path / "apps" / "api" / "src" / "constants"
                 / "demoData.ts")
    if seed_file.exists():
        extras += (
            "DELIVERED SEED USERS (shipped by earlier waves — BINDING). "
            "These identities and roles are facts about the running product; "
            "your scenarios must never re-assign a seeded user's role. A "
            "scenario needing a role signs in as the seeded user that "
            "ALREADY holds it, or provisions a synthetic user:\n"
            f"```ts\n{seed_file.read_text()}\n```\n\n")

    if forbidden or plan.get("not_priced"):
        names = [f"{f['name']} ({f['id']})" for f in forbidden]
        behaviours = [b for n in plan.get("not_priced", [])
                      for b in n.get("do_not_price", [])]
        extras += (
            "DELIBERATELY UNPRICED — the board carries Deferred cards for "
            f"these capabilities: {names}. Your oracle must specify NOTHING "
            "for them: no scenario, no rejection path, no test may assert "
            "any of the following behaviours (they are the unpriced "
            f"capabilities by the back door): {behaviours}. Where a failure "
            "BRANCH is legitimately in scope (an outbound call can mechanically "
            "fail), "
            "the scenario may assert the failure is SURFACED — never that it "
            "is retried, queued, recovered or corrected.\n\n")

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
        "BOUNDARIES: you run inside THIS project's directory. Everything you "
        "may use is in this prompt and this project's specs/. Never read the "
        "factory's own source code (its validators are not yours to "
        "reverse-engineer) and never read any other project's files.\n\n"
        "Hold yourself to the standard of this exemplar (structure, precision, "
        "explicit rejection paths, test-data strategy). Its domain is "
        "deliberately FICTIONAL and its nouns are canaries: copy its FORM, "
        "never its content — a draft containing exemplar vocabulary that is "
        "not on this project's board fails validation. Its workflow shape "
        "(a file-ingestion slice) is equally just an example; your shape "
        "comes from the board.\n\n"
        f"```yaml\n{exemplar}\n```\n\n"
        "Starter-kit constraints (truth about the template, NOT domain "
        "content) — include this block VERBATIM as `starter_constraints` in "
        "your YAML:\n\n"
        f"```yaml\n{starter_block}\n```\n\n"
        f"Slice to author (from the approved project plan):\n"
        f"{json.dumps({k: planned[k] for k in ('id', 'name', 'wave', 'bounded_context', 'event_ids')}, indent=1)}\n\n"
        + (f"Events shipped by earlier waves — the screens to connect, NOT "
           f"behaviour to respecify:\n{json.dumps(events, indent=1)}\n\n"
           if horizontal else
           f"Full board events for this slice:\n{json.dumps(events, indent=1)}\n\n")
        + extras
        + "Requirements:\n"
        "  * slice.id / wave / bounded_context must match the plan exactly; "
        "approved_by/approved_at stay null.\n"
        "  * Include the starter_constraints block given above VERBATIM.\n"
        "  * Use ONLY vocabulary from this project's board and plan — never "
        "the exemplar's, and never another project's.\n"
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
        + "YAML MECHANICS — two drafts have died here, so treat these as "
        "hard rules:\n"
        # The example is deliberately CONTENT-FREE (k1/k2): a domain-flavored
        # example here would be vocabulary the drafting agent copies into
        # every project's oracle — the exact leak the exemplar-canary check
        # exists to catch.
        "  * A plain (unquoted) scalar may never contain '{', '[', ': ' or "
        "' #'. Concrete example values are exactly where this bites: write\n"
        "      then: ['response mapping is {\"k1\": 10, \"k2\": 25}']\n"
        "    (single-quoted, one line) or a YAML block mapping — never a bare "
        "brace-and-colon blob after 'then:'.\n"
        "  * Quote every value containing a colon-space, and keep each "
        "scenario field on ONE line unless you use a proper block scalar "
        "('|' or '>').\n"
        "  * Re-read your draft as YAML before returning it: if a human "
        "would have to guess where a value ends, it will not parse.\n\n"
        "Return ONLY the YAML document (no fences, no commentary)."
    )

    errors: list[str] = []
    # Three attempts, not two: a YAML slip costs one whole attempt and says
    # nothing about whether the agent understood the domain — losing the
    # slice to a syntax error after ~$2.50 of good reasoning is the worst
    # trade in the pipeline.
    for attempt in range(3):
        out = claude(
            "oracle_author",
            prompt if not errors
            else prompt + "\n\nYour previous draft failed validation — fix "
                          "exactly these and return the full YAML again:\n  "
                          + "\n  ".join(errors),
            cwd=str(project.dir),
            attempt=attempt, usage=usage, budget_usd=budget_usd, log_path=log_path,
        )
        text = _strip_fences(out)
        errors = validate_oracle(text, planned, board_text=board_text,
                                 extra_nouns=extra_nouns,
                                 require_starter=True,
                                 forbidden=forbidden)
        if errors and log_path:
            # $2 of drafting silently retried is a mystery in the log —
            # truev's first oracle burned attempt 1 with no visible reason.
            livelog.append(log_path, f"oracle draft attempt {attempt + 1} "
                           "failed validation: " + "; ".join(errors)[:400])
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
        f"oracle for {planned['id']} failed validation after 3 attempts:\n  "
        + "\n  ".join(errors))


def _strip_fences(out: str) -> str:
    m = re.search(r"```(?:yaml)?\s*\n(.*?)```", out, re.S)
    return m.group(1) if m else out


def _forbidden_capabilities(board: dict) -> list[dict]:
    """Deferred-carded events inside a REAL bounded context (not the As-Is
    lane): capabilities the client has not bought. Scenario text mentioning
    one means the oracle is specifying unpriced scope."""
    out = []
    for e in board.get("business_events", []):
        if not e.get("deferred") or e.get("out_of_scope") \
                or "Out-of-Scope" in (e.get("bounded_context") or ""):
            continue
        terms = {e["id"], e["name"]}
        label = (e.get("event") or {}).get("label")
        if label:
            terms.add(label)
        out.append({"id": e["id"], "name": e["name"], "terms": sorted(terms),
                    "reason": "; ".join(d.get("label", "") for d in e["deferred"])})
    return out


def validate_oracle(text: str, planned: dict, *,
                    board_text: str | None = None,
                    extra_nouns: list[str] | None = None,
                    require_starter: bool = False,
                    forbidden: list[dict] | None = None) -> list[str]:
    """Mechanical validation of a drafted oracle. Content review is the gate's
    job; this catches everything a schema can catch.

    With board_text, it also catches DOMAIN LEAKAGE — the one failure a
    schema can't see: a draft that echoes the exemplar's fictional domain or
    a past project's vocabulary instead of this board's. Any registered noun
    appearing in the draft but not on the board is an error."""
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

    if require_starter and data.get("starter_constraints") != starter_constraints():
        errors.append("starter_constraints missing or altered — include the "
                      "block from the prompt VERBATIM")

    # Unpriced-capability guard: scenario sections may not mention a Deferred
    # capability (asserting its behaviour prices it by the back door). The
    # out_of_scope section is exempt — naming it there as excluded is correct.
    if forbidden:
        scen_text = json.dumps([data.get(g) for g in
                                ("scenarios", "web_scenarios", "e2e_scenarios",
                                 "provisional_scenarios")])
        for f in forbidden:
            for term in f["terms"]:
                if harness.mentions_noun(term, scen_text):
                    errors.append(
                        f"scenario text mentions '{term}' — '{f['name']}' is "
                        f"a Deferred, unpriced capability ({f['reason']}); "
                        "specify nothing for it (list it under out_of_scope "
                        "instead)")
                    break

    if board_text is not None:
        # Scan CONTENT, not boilerplate: the verbatim starter_constraints
        # block is starter-kit truth that legitimately contains words other
        # projects' boards harvested, and mapping KEYS are exemplar structure
        # (field names, not domain content). Scanning the raw text made every
        # oracle for a project whose board lacks those words unpassable —
        # three Opus attempts died on it before this scan was narrowed to
        # the draft's own string VALUES.
        content_parts: list[str] = []

        def _walk(v) -> None:
            if isinstance(v, str):
                content_parts.append(v)
            elif isinstance(v, dict):
                for x in v.values():
                    _walk(x)
            elif isinstance(v, list):
                for x in v:
                    _walk(x)

        _walk({k: v for k, v in data.items() if k != "starter_constraints"})
        content_text = " ".join(content_parts)
        registry = (harness.DOMAIN_NOUNS + harness.EXEMPLAR_NOUNS
                    + (extra_nouns or []))
        for noun in registry:
            if (harness.mentions_noun(noun, content_text)
                    and not harness.mentions_noun(noun, board_text)):
                errors.append(
                    f"domain noun '{noun}' does not appear on this project's "
                    "board — copied from the exemplar or a past project; "
                    "rewrite using the board's own vocabulary")
    return errors
