"""
CLI — invoke by project NAME, not by paths.

    python -m project_factory doctor
    python -m project_factory list
    python -m project_factory new barcode-mvp --board ~/Downloads/x.board.json

    python -m project_factory run barcode-mvp --dry-run     resolve + print, no spend
    python -m project_factory run barcode-mvp --until baseline    cheap-first ladder
    python -m project_factory run barcode-mvp               next unstarted slice
    python -m project_factory run barcode-mvp --slice orders

    python -m project_factory status barcode-mvp            where is it paused?
    python -m project_factory approve barcode-mvp           pass the pending gate
    python -m project_factory approve barcode-mvp --reject --note "fix SC-003"

Board path, scenario files, repo location and thread id are all discovered from
the project directory layout — see config.py.

GATES AND RESUME
    The graph pauses at Gate A (spec/scenarios), Gate B (contract freeze) and
    Gate C (PR review) using LangGraph interrupts. State lives in Postgres, so a
    pause can last hours or survive a reboot. `status` shows what is pending and
    `approve` resumes from that exact checkpoint — the same mechanism backs
    `--until`, which is just a pause at a node you chose.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import config as cfgmod
from . import infra
from . import livelog


# -----------------------------------------------------------------------------
# helpers
# -----------------------------------------------------------------------------
def _open_app(project, interrupt_after=None):
    """Build the graph on the Postgres checkpointer. Imports late so that
    list/new/doctor/--dry-run work without langgraph installed."""
    from langgraph.checkpoint.postgres import PostgresSaver

    from .graph import build_graph

    conn = infra.ensure_state_db(project.cfg["checkpoint_db_url"])
    cm = PostgresSaver.from_conn_string(conn)
    saver = cm.__enter__()
    saver.setup()
    return cm, build_graph(saver, interrupt_after=interrupt_after)


def _pending(app, thread) -> tuple[list[str], list[dict]]:
    """(next nodes, interrupt payloads) for a thread, or ([], []) if idle."""
    try:
        snap = app.get_state(thread)
    except Exception:  # noqa: BLE001 — no checkpoint yet
        return [], []
    nxt = list(snap.next or ())
    payloads: list[dict] = []
    for task in getattr(snap, "tasks", ()) or ():
        for intr in getattr(task, "interrupts", ()) or ():
            val = getattr(intr, "value", None)
            if isinstance(val, dict):
                payloads.append(val)
    return nxt, payloads


def _show_gate(payloads: list[dict]) -> None:
    for p in payloads:
        print(f"\n  gate: {p.get('gate', '?')}")
        for k, v in p.items():
            if k in ("gate", "ask"):
                continue
            s = json.dumps(v, default=str) if not isinstance(v, str) else v
            if len(s) > 1500:
                s = s[:1500] + f"… (+{len(s) - 1500} chars)"
            print(f"    {k}: {s}")
        if p.get("ask"):
            print(f"    ASK: {p['ask']}")


def _report(res: dict, project, chosen) -> int:
    print("\n".join(res.get("log", [])))
    usage = res.get("usage")
    cost = f"${usage.cost_usd:.2f}" if usage else "n/a"
    status = res.get("status")
    print(f"\nstatus={status} parked={res.get('parked', [])} cost={cost}")
    if usage and usage.by_agent:
        print("cost by agent: "
              + json.dumps({k: round(v, 3) for k, v in usage.by_agent.items()}))

    # Ground-truth cost per slice, recorded win or lose: the live log keeps
    # agent lines whose state update a crash later discarded, so it is the
    # number project budgets must trust (see livelog.live_log_cost).
    truth = livelog.live_log_cost(str(project.dir), chosen.id)
    if usage:
        truth = max(truth, round(usage.cost_usd, 2))
    if truth:
        project.record_slice_cost(chosen.id, truth)

    if status == "green":
        project.mark_completed(chosen.id)
        print(f"recorded in {project.state_file}")
        nxt = project.next_slice()
        print(f"next slice: {nxt.id}" if nxt else "all slices complete")
        from . import planner as planmod
        if planmod.plan_is_approved(planmod.load_plan(project)):
            print(f"continue the project: python -m project_factory "
                  f"run-project {project.slug}")
        return 0
    return 1


# -----------------------------------------------------------------------------
# commands
# -----------------------------------------------------------------------------
def _cmd_list(args) -> int:
    ws = cfgmod.resolve_workspace(args.workspace)
    names = cfgmod.list_projects(args.workspace)
    print(f"workspace: {ws}")
    if not names:
        print("  (no projects yet — python -m project_factory new <slug> --board <file>)")
        return 0
    for n in names:
        try:
            p = cfgmod.discover(n, args.workspace)
            done = len(p.state.get("completed_slices", []))
            print(f"  {n:24} {len(p.slices)} slice(s), {done} done, "
                  f"repo {'present' if p.repo_path.exists() else 'not created'}")
        except cfgmod.ConfigError as e:
            print(f"  {n:24} INVALID — {str(e).splitlines()[0]}")
    return 0


def _cmd_new(args) -> int:
    p = cfgmod.scaffold(args.slug, args.board, args.workspace, args.slice_name,
                        project_mode=bool(getattr(args, "project", False)))
    print(f"created {p.dir}")
    print(f"  specs/     {p.board_path.name}"
          + (f" + {', '.join(s.path.name for s in p.slices)}" if p.slices
             else " (slices come from the plan — run-project drafts them)"))
    print("  run.json   overrides only")

    # Db-reset consent is collected HERE, at scaffold time, so provision_db
    # never stalls mid-run on Prisma's AI-agent guardrail. The decision stays
    # a human one: --db-reset-consent is an explicit flag, and the interactive
    # prompt defaults to No. See config.record_db_reset_consent.
    consent = bool(args.db_reset_consent)
    if not consent and sys.stdin.isatty():
        print("\nEvery run resets this project's LOCAL DEV database "
              "(prisma migrate reset --force — destroys all data in it).")
        ans = input("Record your consent now so runs never pause on it? [y/N] ")
        consent = ans.strip().lower() in ("y", "yes")
    if consent:
        if cfgmod.record_db_reset_consent(p):
            print("  run.json   db_reset_consent recorded (dev DB "
                  f"{p.slug.replace('-', '_')} only)")
    else:
        print("  note: no db_reset_consent — the first run will stop at "
              "provision_db until you add it to run.json")
    if getattr(args, "project", False):
        print("\nNext: plan the whole project from the board:")
        print(f"  python -m project_factory run-project {args.slug}")
    else:
        print("\nNext: author the scenarios — this is the ORACLE, the "
              "highest-value work in the pipeline. Then:")
        print(f"  python -m project_factory run {args.slug} --dry-run")
    return 0


def _cmd_doctor(args) -> int:
    try:
        print(infra.preflight())
    except RuntimeError as e:
        print(e)
        return 1
    print(f"workspace   {cfgmod.resolve_workspace(args.workspace)}")
    print(f"factory     {cfgmod.factory_root()}")
    cfg = {**cfgmod.BUILT_IN_DEFAULTS, **cfgmod._load_defaults()}
    host, port = cfg["db_host"], cfg["db_port"]
    reachable = infra.postgres_reachable(host, port)
    print(f"postgres    {'ok' if reachable else 'UNREACHABLE'} at {host}:{port} "
          f"(one shared instance for every local project — see defaults.json)")
    return 0 if reachable else 1


def _cmd_status(args) -> int:
    try:
        project = cfgmod.discover(args.slug, args.workspace)
        chosen = project.pick_slice(args.slice)
    except cfgmod.ConfigError as e:
        print(f"config error:\n{e}", file=sys.stderr)
        return 2

    cm, app = _open_app(project)
    try:
        thread = {"configurable": {"thread_id": project.thread_id(chosen.id)}}
        nxt, payloads = _pending(app, thread)
        print(f"thread   {project.thread_id(chosen.id)}")
        if not nxt:
            print("state    no run in progress (start with: run "
                  f"{args.slug})")
            return 0
        print(f"paused   before {', '.join(nxt)}")
        if payloads:
            _show_gate(payloads)
            print(f"\nresume:  python -m project_factory approve {args.slug}")
        else:
            print(f"resume:  python -m project_factory run {args.slug}"
                  "   (stopped by --until, not a gate)")
        return 0
    finally:
        cm.__exit__(None, None, None)


def _cmd_approve(args) -> int:
    try:
        project = cfgmod.discover(args.slug, args.workspace)
        chosen = project.pick_slice(args.slice)
    except cfgmod.ConfigError as e:
        print(f"config error:\n{e}", file=sys.stderr)
        return 2

    from langgraph.types import Command

    from .models import BudgetExceeded

    cm, app = _open_app(project)
    try:
        thread = {"configurable": {"thread_id": project.thread_id(chosen.id)}}
        nxt, payloads = _pending(app, thread)
        if not nxt:
            print(f"nothing pending for {project.thread_id(chosen.id)}")
            return 0
        if not payloads and not args.force:
            print(f"paused before {', '.join(nxt)} but no gate is awaiting input "
                  f"(likely stopped by --until).\n"
                  f"  continue with: python -m project_factory run {args.slug}")
            return 1

        _show_gate(payloads)
        decision = {
            "approved": not args.reject,
            "note": args.note or "",
            "by": args.by,
        }
        if args.reject:
            print("\nREJECTED — the graph will surface this and stop. Fix the "
                  "specs/contract, then re-run.")
        print(f"\nresuming with: {json.dumps(decision)}\n")

        # Same cfg refresh as _cmd_run's resume path: a human may have edited
        # run.json while the slice sat at this gate.
        app.update_state(thread, {"cfg": project.cfg})

        try:
            livelog.acquire_run_lock(str(project.dir), chosen.id, cmd=sys.argv)
        except RuntimeError as e:
            print(f"\n{e}", file=sys.stderr)
            return 4
        try:
            res = app.invoke(Command(resume=decision), thread)
        finally:
            livelog.release_run_lock(str(project.dir), chosen.id)

        # It may pause again at the next gate.
        nxt2, payloads2 = _pending(app, thread)
        if nxt2:
            print("\n".join(res.get("log", [])) if res.get("log") else "")
            print(f"\npaused again before {', '.join(nxt2)}")
            _show_gate(payloads2)
            print(f"\nresume:  python -m project_factory approve {args.slug}")
            return 0
        rc = _report(res, project, chosen)
        if rc == 0:
            # In project mode (approved plan), a Gate C approval should carry
            # the project forward without a separate human step: chain into
            # run-project, which drafts the next oracle and drives the next
            # slice until ITS human gate. Single-slice projects (no plan)
            # keep today's behaviour.
            from . import planner as planmod
            if planmod.plan_is_approved(planmod.load_plan(project)):
                print("\nproject mode: continuing with the next planned slice…\n")
                return _cmd_run_project(argparse.Namespace(
                    slug=args.slug, workspace=args.workspace,
                    dry_run=False, gates=None, max_slices=None))
        return rc
    except BudgetExceeded as e:
        print(f"\nABORTED (budget): {e}", file=sys.stderr)
        return 3
    finally:
        cm.__exit__(None, None, None)


def _cmd_run(args) -> int:
    # From config (langgraph-free), so --dry-run and flag validation work on a
    # checkout that only has pyyaml installed.
    LADDER, NODE_ORDER = cfgmod.LADDER, cfgmod.NODE_ORDER

    # Validate flags BEFORE discovery — a typo in --until shouldn't require a
    # valid project to be reported.
    if args.until and args.until not in NODE_ORDER:
        print(f"unknown --until node '{args.until}'.\n\nUseful stopping points:",
              file=sys.stderr)
        for n, why in LADDER.items():
            print(f"  {n:16} {why}", file=sys.stderr)
        print(f"\nall nodes: {', '.join(NODE_ORDER)}", file=sys.stderr)
        return 2

    try:
        project = cfgmod.discover(args.slug, args.workspace)
        chosen = project.pick_slice(args.slice)
    except cfgmod.ConfigError as e:
        print(f"config error:\n{e}", file=sys.stderr)
        return 2

    # Resolve-and-print BEFORE spending a single token.
    print(cfgmod.describe(project, chosen))
    if args.until:
        print(f"until       {args.until}  ({LADDER.get(args.until, 'custom stop')})")
    if args.dry_run:
        print("\n--dry-run: nothing executed")
        return 0

    print()
    print(infra.preflight())

    from .models import BudgetExceeded

    cm, app = _open_app(project, interrupt_after=[args.until] if args.until else None)
    try:
        thread = {"configurable": {"thread_id": project.thread_id(chosen.id)}}

        # Resume an in-flight thread instead of starting over. Same project +
        # slice = same thread, so a killed run continues where it stopped.
        nxt, payloads = _pending(app, thread)

        # A FINISHED thread must never be restarted in place: the graph's
        # merge/append reducers (attempts, diagnosis, phase_out, parked, log)
        # survive ingest's reset and poison the new run — stale attempt counts
        # park phases early, stale diagnoses leak into fresh prompts. Start a
        # new thread GENERATION instead; the old thread stays in the
        # checkpoint DB for cost/attempt comparison across runs.
        fresh = args.fresh
        if not fresh and not nxt:
            try:
                fresh = bool(app.get_state(thread).values)
            except Exception:  # noqa: BLE001 — no checkpoint yet: plain first run
                fresh = False
        if fresh:
            if nxt:  # only reachable via an explicit --fresh
                print(f"\n--fresh: abandoning thread paused before "
                      f"{', '.join(nxt)} (kept in the checkpoint DB; "
                      "decrement thread_generations in .factory/state.json "
                      "to point back at it)")
            gen = project.bump_thread_generation(chosen.id)
            why = "--fresh" if args.fresh else "previous run already finished"
            print(f"\nfresh run ({why}): thread generation g{gen} — earlier "
                  "generations remain in the checkpoint DB")
            thread = {"configurable": {"thread_id": project.thread_id(chosen.id)}}
            nxt, payloads = [], []

        if nxt and payloads:
            print(f"\nthis slice is paused at a gate before {', '.join(nxt)} — "
                  f"approve it first:\n  python -m project_factory approve {args.slug}")
            _show_gate(payloads)
            return 1
        payload = None if nxt else {
            "cfg": project.cfg,
            "board_path": str(project.board_path),
            "scenarios_path": str(chosen.path),
            "repo_path": str(project.repo_path),
            "project_dir": str(project.dir),
            "slice_id": chosen.id,
        }
        if nxt:
            print(f"\nresuming from checkpoint (was before {', '.join(nxt)})")
            # The checkpoint holds the cfg AS OF the thread's first start.
            # run.json is exactly where a human fixes things while a run sits
            # paused (add db_reset_consent, raise budget_usd) — resuming with
            # the stale checkpointed copy silently ignores those edits, which
            # reads as "the fix didn't work". Refresh cfg from disk on every
            # resume so the file a human just edited is the one that runs.
            app.update_state(thread, {"cfg": project.cfg})

        try:
            livelog.acquire_run_lock(str(project.dir), chosen.id, cmd=sys.argv)
        except RuntimeError as e:
            print(f"\n{e}", file=sys.stderr)
            return 4
        try:
            res = app.invoke(payload, thread)
        finally:
            livelog.release_run_lock(str(project.dir), chosen.id)

        nxt2, payloads2 = _pending(app, thread)
        if nxt2:
            if res.get("log"):
                print("\n".join(res["log"]))
            usage = res.get("usage")
            if usage:
                print(f"\ncost so far: ${usage.cost_usd:.2f}")
            print(f"\npaused before {', '.join(nxt2)}")
            if payloads2:
                _show_gate(payloads2)
                print(f"\nresume:  python -m project_factory approve {args.slug}")
            else:
                print(f"resume:  python -m project_factory run {args.slug}")
            return 0
        return _report(res, project, chosen)
    except BudgetExceeded as e:
        print(f"\nABORTED (budget): {e}", file=sys.stderr)
        return 3
    finally:
        cm.__exit__(None, None, None)


HUMAN_ACTION_NEEDED = 10  # run-project stopped at a human gate — not an error


def _parse_gates(spec: str | None) -> dict | None:
    if not spec:
        return None
    gates = {}
    for part in spec.split(","):
        k, _, v = part.strip().partition("=")
        if k not in ("spec", "contract", "pr") or v not in ("auto", "human"):
            raise SystemExit(f"bad --gates entry '{part}' "
                             "(want spec|contract|pr=auto|human)")
        gates[k] = v
    return gates


def _project_gate_policy(project, cli_gates: dict | None) -> dict:
    """--gates > run.json's explicit \"gates\" > PROJECT_GATE_POLICY."""
    if cli_gates:
        return {**cfgmod.PROJECT_GATE_POLICY, **cli_gates}
    run_file = project.dir / "run.json"
    if run_file.exists():
        raw = json.loads(run_file.read_text())
        if isinstance(raw.get("gates"), dict):
            return {**cfgmod.PROJECT_GATE_POLICY, **raw["gates"]}
    return dict(cfgmod.PROJECT_GATE_POLICY)


def _drive_slice(project, chosen, overlay: dict) -> int:
    """
    Run (or resume) one slice thread with project-mode cfg overlaid
    (remaining budget, gate policy, merge_on_approval, fresh_clone=False).
    Returns 0 green, 1 parked/failed, 3 budget, HUMAN_ACTION_NEEDED at a gate.
    """
    from .models import BudgetExceeded

    cfg = {**project.cfg, **overlay}
    cm, app = _open_app(project)
    try:
        thread = {"configurable": {"thread_id": project.thread_id(chosen.id)}}
        nxt, payloads = _pending(app, thread)

        finished_thread = False
        if not nxt:
            try:
                finished_thread = bool(app.get_state(thread).values)
            except Exception:  # noqa: BLE001 — no checkpoint yet
                finished_thread = False
        if finished_thread:
            gen = project.bump_thread_generation(chosen.id)
            print(f"slice {chosen.id}: previous thread finished — "
                  f"new generation g{gen}")
            thread = {"configurable": {"thread_id": project.thread_id(chosen.id)}}
            nxt, payloads = [], []

        if nxt and payloads:
            print(f"\n{chosen.id} is waiting at a gate:")
            _show_gate(payloads)
            print(f"\n  python -m project_factory approve {project.slug} "
                  f"--slice {chosen.id} --by <you>")
            return HUMAN_ACTION_NEEDED

        payload = None if nxt else {
            "cfg": cfg,
            "board_path": str(project.board_path),
            "scenarios_path": str(chosen.path),
            "repo_path": str(project.repo_path),
            "project_dir": str(project.dir),
            "slice_id": chosen.id,
        }
        if nxt:
            print(f"resuming {chosen.id} from checkpoint "
                  f"(was before {', '.join(nxt)})")
            app.update_state(thread, {"cfg": cfg})

        livelog.acquire_run_lock(str(project.dir), chosen.id, cmd=sys.argv)
        try:
            res = app.invoke(payload, thread)
        finally:
            livelog.release_run_lock(str(project.dir), chosen.id)

        nxt2, payloads2 = _pending(app, thread)
        if nxt2:
            if res.get("log"):
                print("\n".join(res["log"]))
            if payloads2:
                print(f"\n{chosen.id} paused at a gate:")
                _show_gate(payloads2)
                print(f"\napprove it (CLI below, or the dashboard), then re-run "
                      f"run-project to continue:\n"
                      f"  python -m project_factory approve {project.slug} "
                      f"--slice {chosen.id} --by <you>")
                return HUMAN_ACTION_NEEDED
            print(f"\n{chosen.id} paused before {', '.join(nxt2)} — re-run "
                  f"run-project to continue")
            return HUMAN_ACTION_NEEDED
        return _report(res, project, chosen)
    except BudgetExceeded as e:
        truth = livelog.live_log_cost(str(project.dir), chosen.id)
        if truth:
            project.record_slice_cost(chosen.id, truth)
        print(f"\nABORTED (budget): {e}", file=sys.stderr)
        return 3
    finally:
        cm.__exit__(None, None, None)


def _cmd_run_project(args) -> int:
    """
    Drive a whole project from its board: plan slices (once, human-approved),
    then for each planned slice draft the oracle and run the slice pipeline,
    merging each Gate-C-approved slice into main. Re-entrant: it advances as
    far as it can and exits at the first decision a human owns.
    """
    from . import planner as planmod

    try:
        project = cfgmod.discover(args.slug, args.workspace)
    except cfgmod.ConfigError as e:
        print(f"config error:\n{e}", file=sys.stderr)
        return 2

    gates = _project_gate_policy(project, _parse_gates(args.gates))
    project_budget = float(project.cfg.get("project_budget_usd", 100.0))
    spent = project.spent_usd()

    print(f"project      {project.slug}  ({project.dir})")
    print(f"board        {project.board_path.name}")
    print(f"budget       ${project_budget:.2f} project-wide, "
          f"${spent:.2f} recorded so far")
    print(f"gate policy  {json.dumps(gates)}  (Gate C human by default)")

    # ---- phase 1: the plan (project-level gate) ----------------------------
    plan = planmod.load_plan(project)
    if not (plan and plan.get("slices")):
        if args.dry_run:
            print("\n--dry-run: would plan slices from the board (planner, opus)")
            return 0
        print("\nplanning slices from the board (planner, opus)…")
        try:
            plan = planmod.plan_slices(
                project, budget_usd=project_budget - spent,
                log_path=livelog.path_for(str(project.dir), "project"))
        except planmod.PlanError as e:
            print(f"\nplan failed: {e}", file=sys.stderr)
            return 1
        _print_plan(plan)
        print(f"\nTHE PLAN IS THE PROJECT-LEVEL GATE. Review "
              f"{planmod.plan_path(project)},\nedit it freely, then:\n"
              f"  python -m project_factory approve-plan {args.slug} --by <you>")
        return HUMAN_ACTION_NEEDED

    if not planmod.plan_is_approved(plan):
        _print_plan(plan)
        print(f"\nplan awaits approval:\n"
              f"  python -m project_factory approve-plan {args.slug} --by <you>")
        return HUMAN_ACTION_NEEDED

    if args.dry_run:
        _print_plan(plan)
        print("\n--dry-run: nothing executed")
        return 0

    print()
    print(infra.preflight())

    # ---- phase 2: slices, in wave order -------------------------------------
    done = set(project.state.get("completed_slices", []))
    ordered = sorted(plan["slices"], key=lambda s: (s["wave"], s["id"]))
    for planned in ordered:
        if planned["id"] in done:
            continue
        if args.max_slices is not None and len(done) >= args.max_slices:
            print(f"\n--max-slices {args.max_slices} reached")
            return 0

        spent = project.spent_usd()
        remaining = round(project_budget - spent, 2)
        if remaining < 2.0:
            print(f"\nproject budget exhausted: ${spent:.2f} of "
                  f"${project_budget:.2f} recorded (raise project_budget_usd "
                  f"in run.json to continue)", file=sys.stderr)
            return 3

        spath = planmod.scenarios_path_for(project, planned["id"])
        if not spath.exists():
            print(f"\ndrafting oracle for {planned['id']} "
                  f"(oracle_author, opus, ${remaining:.2f} remaining)…")
            try:
                planmod.author_oracle(
                    project, planned, budget_usd=remaining,
                    log_path=livelog.path_for(str(project.dir), "project"))
            except planmod.PlanError as e:
                print(f"\noracle draft failed: {e}", file=sys.stderr)
                return 1
            print(f"oracle drafted: {spath.name}")

        # Re-discover so the drafted file becomes a Slice object.
        project = cfgmod.discover(args.slug, args.workspace)
        matches = [s for s in project.slices if s.id == planned["id"]]
        if not matches:
            print(f"drafted oracle's slice.id doesn't match the plan "
                  f"({planned['id']}) — fix {spath}", file=sys.stderr)
            return 1

        print(f"\n=== slice {planned['id']} (wave {planned['wave']}, "
              f"${remaining:.2f} of budget remaining) ===")
        overlay = {"budget_usd": remaining, "gates": gates,
                   "merge_on_approval": True, "fresh_clone": False}
        rc = _drive_slice(project, matches[0], overlay)
        if rc == HUMAN_ACTION_NEEDED:
            return 0        # not an error — a human owns the next move
        if rc != 0:
            return rc
        project = cfgmod.discover(args.slug, args.workspace)
        done = set(project.state.get("completed_slices", []))

    print(f"\nALL PLANNED SLICES COMPLETE — ${project.spent_usd():.2f} of "
          f"${project_budget:.2f} spent. Repo: {project.repo_path}")
    return 0


def _print_plan(plan: dict) -> None:
    print(f"\nplan ({len(plan.get('slices', []))} slices"
          + (f", approved by {plan['approved_by']}" if plan.get("approved_by")
             else ", NOT yet approved") + "):")
    for s in sorted(plan.get("slices", []), key=lambda x: (x["wave"], x["id"])):
        print(f"  w{s['wave']} {s['id']:40} {len(s.get('event_ids', []))} events"
              f"  — {s.get('name', '')}")
    for o in plan.get("out_of_scope", []):
        print(f"  --  {o.get('id', '?'):40} out of scope: {o.get('reason', '')[:80]}")


def _cmd_approve_plan(args) -> int:
    from . import planner as planmod
    try:
        project = cfgmod.discover(args.slug, args.workspace)
    except cfgmod.ConfigError as e:
        print(f"config error:\n{e}", file=sys.stderr)
        return 2
    if not args.by:
        print("--by <name> is required: plan approval is the project-level "
              "human gate and must be attributable", file=sys.stderr)
        return 2
    try:
        plan = planmod.approve_plan(project, args.by)
    except planmod.PlanError as e:
        print(str(e), file=sys.stderr)
        return 1
    _print_plan(plan)
    print(f"\napproved. Continue: python -m project_factory run-project {args.slug}")
    return 0


# -----------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="project_factory")
    ap.add_argument("--workspace", help=f"override ${cfgmod.ENV_WORKSPACE}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="list projects in the workspace").set_defaults(fn=_cmd_list)
    sub.add_parser("doctor", help="check toolchain prerequisites").set_defaults(fn=_cmd_doctor)

    n = sub.add_parser("new", help="scaffold a project directory")
    n.add_argument("slug")
    n.add_argument("--board", help="path to the presales *.board.json")
    n.add_argument("--slice-name", default="slice-001")
    n.add_argument("--project", action="store_true",
                   help="board-only project for run-project: no placeholder "
                        "scenarios file — the planner derives slices and the "
                        "oracle_author drafts each scenarios.yaml")
    n.add_argument("--db-reset-consent", action="store_true",
                   help="record consent for the destructive dev-DB reset "
                        "(prisma migrate reset) in run.json now, instead of "
                        "being prompted interactively / stopping mid-run")
    n.set_defaults(fn=_cmd_new)

    rp = sub.add_parser("run-project",
                        help="drive ALL slices from the board: plan → draft "
                             "oracles → run each slice → merge on Gate C")
    rp.add_argument("slug")
    rp.add_argument("--dry-run", action="store_true",
                    help="print the plan and stop; never spends")
    rp.add_argument("--gates",
                    help="override gate policy, e.g. spec=human,contract=auto "
                         "(default: spec=auto,contract=auto,pr=human)")
    rp.add_argument("--max-slices", type=int,
                    help="stop after N completed slices (checkpointing a "
                         "long project into sessions)")
    rp.set_defaults(fn=_cmd_run_project)

    apl = sub.add_parser("approve-plan",
                         help="record human approval of specs/project-plan.json "
                              "(the project-level gate)")
    apl.add_argument("slug")
    apl.add_argument("--by", default="",
                     help="who approved — required, recorded in the plan")
    apl.set_defaults(fn=_cmd_approve_plan)

    r = sub.add_parser("run", help="run (or resume) one slice")
    r.add_argument("slug")
    r.add_argument("--slice", help="slice id or filename stem (default: next unstarted)")
    r.add_argument("--dry-run", action="store_true",
                   help="resolve and print the plan, execute nothing")
    r.add_argument("--until", metavar="NODE",
                   help="pause after NODE (cheap-first ladder: gap_detect, "
                        "baseline, provision_db, migrate, write_tests, …)")
    r.add_argument("--fresh", action="store_true",
                   help="start a new thread generation instead of resuming — "
                        "automatic when the previous run finished; pass "
                        "explicitly to abandon a paused/in-flight thread "
                        "(old threads are kept in the checkpoint DB)")
    r.set_defaults(fn=_cmd_run)

    s = sub.add_parser("status", help="show where a slice is paused")
    s.add_argument("slug")
    s.add_argument("--slice")
    s.set_defaults(fn=_cmd_status)

    a = sub.add_parser("approve", help="pass the pending gate and resume")
    a.add_argument("slug")
    a.add_argument("--slice")
    a.add_argument("--reject", action="store_true", help="record a rejection")
    a.add_argument("--note", help="reviewer note recorded in the run log")
    a.add_argument("--by", default="", help="who approved (recorded in the log)")
    a.add_argument("--force", action="store_true",
                   help="resume even if no gate payload is waiting")
    a.set_defaults(fn=_cmd_approve)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
