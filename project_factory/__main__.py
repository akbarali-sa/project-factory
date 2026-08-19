"""
CLI — invoke by project NAME, not by paths.

    python -m project_factory doctor
    python -m project_factory list
    python -m project_factory new acme-crm --board ~/Downloads/x.board.json

    python -m project_factory run acme-crm --dry-run     resolve + print, no spend
    python -m project_factory run acme-crm --until baseline    cheap-first ladder
    python -m project_factory run acme-crm               next unstarted slice
    python -m project_factory run acme-crm --slice orders

    python -m project_factory status acme-crm            where is it paused?
    python -m project_factory approve acme-crm           pass the pending gate
    python -m project_factory approve acme-crm --reject --note "fix SC-003"

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
from . import repo as repo_mod


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
    if usage and usage.total_tokens:
        print(f"tokens: out={usage.output_tokens:,} in={usage.input_tokens:,} "
              f"cache_read={usage.cache_read_tokens:,} "
              f"cache_write={usage.cache_write_tokens:,} "
              f"(total {usage.total_tokens:,})")
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
        project.record_slice_cost(
            chosen.id, truth,
            tokens={"input": usage.input_tokens,
                    "output": usage.output_tokens,
                    "cache_read": usage.cache_read_tokens,
                    "cache_write": usage.cache_write_tokens}
            if usage and usage.total_tokens else None)

    # A Gate C approval on a PARKED slice is the human override — someone
    # reviewed the branch (typically after hand-fixing the parked phase) and
    # accepted it. Treat it as delivered; refusing it here would strand every
    # human-rescued slice one step from done.
    accepted = status == "green" or bool(res.get("pr_approved"))
    if accepted:
        if status != "green":
            print("parked but Gate-C-approved — accepting as delivered "
                  "(human override recorded in the gate note)")
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


def _cmd_serve(args) -> int:
    """
    Run the INTEGRATED product: everything merged onto main, on the project's
    sticky ports. This is the acceptance-testing entry point — one app, all
    waves, one URL — as opposed to `run`, which brings a stack up as a side
    effect of building one slice.
    """
    try:
        project = cfgmod.discover(args.slug, args.workspace)
    except cfgmod.ConfigError as e:
        print(f"config error:\n{e}", file=sys.stderr)
        return 2

    repo = str(project.repo_path)
    if not project.repo_path.exists():
        print(f"no repo yet at {repo} — run the project first", file=sys.stderr)
        return 2

    branch = repo_mod.current_branch(repo)
    if args.stop:
        infra.stop_stack()
        print(f"stopped any factory-launched stack for {args.slug}")
        return 0
    if branch != "main" and not args.allow_branch:
        print(f"repo is on '{branch}', not main — `serve` exists to exercise "
              f"the INTEGRATED product.\n"
              f"  git -C {repo} switch main    (or pass --allow-branch)",
              file=sys.stderr)
        return 2

    slug = repo_mod.slugify(project.slug)
    api_port, web_port = infra.sticky_ports(
        str(project.dir), repo,
        project.cfg.get("api_port", 3001), project.cfg.get("web_port", 3000))
    stack = infra.Stack(
        project_slug=slug, api_port=api_port, web_port=web_port,
        db_name=slug.replace("-", "_"),
        db_host=project.cfg.get("db_host", "localhost"),
        db_port=project.cfg.get("db_port", 5432),
        db_user=project.cfg.get("db_user", "postgres"),
        db_password=project.cfg.get("db_password", "postgres"),
    )
    infra.write_env(repo, stack, project.cfg.get("jwt_secret", "factory-local-dev"))
    print(f"branch      {branch}")
    print(f"slices      {len(project.state.get('completed_slices', []))} delivered")
    print(infra.launch_stack(repo, stack))
    # Roles per seeded user are a PROJECT fact, not a factory fact — the seed
    # is the authority (barcode-v2 already inverted the starter's default).
    print(f"\n  web  {stack.web_url}   (seeded logins + roles: "
          f"apps/api/src/constants/demoData.ts)")
    print(f"  api  {stack.api_url}/api/docs")
    print("\nThe stack keeps running after this command returns.")
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

    from .models import BudgetExceeded, RateLimited

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
        # run.json while the slice sat at this gate. In project mode the
        # run-project overlay (gate policy, merge_on_approval, remaining
        # budget) must survive the refresh — a bare project.cfg silently
        # turns merge_slice into a no-op right when Gate C approves.
        app.update_state(thread, {"cfg": _resume_cfg(project)})

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
    except RateLimited as e:
        return _report_rate_limited(e, args.slug, getattr(chosen, "id", None))
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

    from .models import BudgetExceeded, RateLimited

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
            # resume (project-mode overlay preserved — see _resume_cfg).
            app.update_state(thread, {"cfg": _resume_cfg(project)})

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
    except RateLimited as e:
        return _report_rate_limited(e, args.slug, getattr(chosen, "id", None))
    except BudgetExceeded as e:
        print(f"\nABORTED (budget): {e}", file=sys.stderr)
        return 3
    finally:
        cm.__exit__(None, None, None)


HUMAN_ACTION_NEEDED = 10  # run-project stopped at a human gate — not an error
RATE_LIMITED = 11         # account limit, not a defect — resume after it resets


def _report_rate_limited(e: Exception, slug: str, slice_id: str | None) -> int:
    """
    A rate limit is a PAUSE, and must not read like a crash.

    The crash path tells you to re-run immediately, which against a limit
    produces the identical failure — and `run-project`, which loops over
    slices, would keep walking into it. Say plainly that the work is safe in
    the checkpoint and that the wait is the point. On a subscription seat the
    binding limit is usually weekly, so check the window before resuming
    rather than guessing.
    """
    resume = (f"python -m project_factory run-project {slug}" if slice_id is None
              else f"python -m project_factory run-project {slug}  # resumes {slice_id}")
    print(f"\nPAUSED (account limit) — this is NOT a failure of the generated code."
          f"\n{e}\n"
          f"\nEverything completed so far is in the checkpoint; nothing is lost and"
          f"\nnothing needs re-doing. Only the interrupted node re-runs."
          f"\n\nCheck your remaining window (`/usage` in an interactive claude"
          f"\nsession), wait for it to reset, then:\n  {resume}",
          file=sys.stderr)
    return RATE_LIMITED


def _resume_cfg(project) -> dict:
    """
    cfg for resuming an existing thread: run.json as edited on disk, plus —
    when the project runs in plan mode — the same overlay run-project's
    _drive_slice applies (gate policy, merge-on-approval, remaining budget),
    so a resume through `run` or `approve` behaves identically to one
    through run-project.
    """
    from . import planner as planmod
    if not planmod.plan_is_approved(planmod.load_plan(project)):
        return project.cfg
    project_budget = float(project.cfg.get("project_budget_usd", 100.0))
    return {**project.cfg,
            "gates": _project_gate_policy(project, None),
            "merge_on_approval": True,
            "fresh_clone": False,
            "budget_usd": round(max(project_budget - project.spent_usd(), 0.0), 2)}


def _parse_gates(spec: str | None) -> dict | None:
    if not spec:
        return None
    gates = {}
    for part in spec.split(","):
        k, _, v = part.strip().partition("=")
        if k not in ("spec", "contract", "pr", "decisions", "uiux") \
                or v not in ("auto", "human"):
            raise SystemExit(f"bad --gates entry '{part}' "
                             "(want spec|contract|pr|decisions|uiux"
                             "=auto|human)")
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
    Returns 0 green, 1 parked/failed, 3 budget, RATE_LIMITED when the account
    refused, HUMAN_ACTION_NEEDED at a gate.
    """
    from .models import BudgetExceeded, RateLimited

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
    except RateLimited as e:
        # MUST precede the RuntimeError arm below (RateLimited subclasses it):
        # the crash advice — "re-run to resume" — is actively wrong here, and
        # run-project loops over slices, so it would walk straight back into
        # the same wall on the next one.
        return _report_rate_limited(e, project.slug, chosen.id)
    except BudgetExceeded as e:
        print(f"\nABORTED (budget): {e}", file=sys.stderr)
        return 3
    except RuntimeError as e:
        # A node crash (agent connection drop, lint failure, subprocess error)
        # must not escape as a bare traceback: report it, keep the exit code
        # meaningful, and let the finally below record what was spent — the
        # checkpoint loses the crashed node's usage, the live log doesn't.
        print(f"\n{chosen.id} crashed mid-node:\n{e}\n\nre-run run-project to "
              f"resume from the checkpoint (only the crashed node re-runs)",
              file=sys.stderr)
        return 1
    finally:
        # Ground-truth spend on EVERY exit path — green, gate, crash, abort —
        # so run-project's next budget check never reasons from a stale ledger.
        truth = livelog.live_log_cost(str(project.dir), chosen.id)
        if truth:
            project.record_slice_cost(chosen.id, truth)
        cm.__exit__(None, None, None)


def _cmd_run_project(args) -> int:
    """
    Drive a whole project from its board: plan slices (once, human-approved),
    then for each planned slice draft the oracle and run the slice pipeline,
    merging each Gate-C-approved slice into main. Re-entrant: it advances as
    far as it can and exits at the first decision a human owns.
    """
    from . import planner as planmod
    from .models import RateLimited

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
            print("\n--dry-run: would plan slices from the board "
                  + ("(deterministic backlog converter)" if project.backlog_path
                     else "(planner, opus)"))
            return 0
        print("\nplanning slices from the board "
              + ("(converting the reviewed backlog — no agent)…"
                 if project.backlog_path else "(planner, opus)…"))
        try:
            plan = planmod.plan_slices(
                project, budget_usd=project_budget - spent,
                log_path=livelog.path_for(str(project.dir), "project"))
        except RateLimited as e:
            return _report_rate_limited(e, args.slug, None)
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

    # ---- phase 1.4: the decision register (project-level gate) --------------
    # Every question the build needs answered, in git, with who answered it.
    # Board questions import deterministically; the analyst agent adds the
    # ones nobody recorded. With gates.decisions=human the run holds while
    # any `blocker: start` question is open — answering and deferring are
    # both explicit human decisions and both unblock.
    from . import decisions as decmod

    decs = decmod.load_decisions(project)
    if decs is None:
        decs = decmod.import_board_questions(project)
        print(f"\ndecision register: {decmod.stats(decs)['total']} question(s) "
              f"imported from the board → {decmod.decisions_path(project).name}")
    if not decs.get("analyst_done"):
        spent = project.spent_usd()
        print(f"\nraising unrecorded questions "
              f"(decision_analyst, opus, ${project_budget - spent:.2f} remaining)…")
        try:
            decs = decmod.draft_analyst_questions(
                project, budget_usd=project_budget - spent,
                log_path=livelog.path_for(str(project.dir), "project"))
        except RateLimited as e:
            return _report_rate_limited(e, args.slug, None)
        except decmod.DecisionError as e:
            print(f"\nanalyst questions failed: {e}", file=sys.stderr)
            return 1
        st = decmod.stats(decs)
        print(f"register now holds {st['total']} question(s), "
              f"{st['open_start_blockers']} open start-blocker(s)")

    if gates.get("decisions", "human") == "human":
        blockers = decmod.open_start_blockers(decs)
        if blockers:
            print(f"\nTHE DECISION REGISTER IS A PROJECT-LEVEL GATE — "
                  f"{len(blockers)} start-blocker(s) open:")
            for q in blockers:
                print(f"  [{q['id']}] {q['question']}")
                if q.get("recommended"):
                    print(f"        recommended: {q['recommended']}")
            print(f"\nAnswer or defer them (deferring records a working "
                  f"assumption):\n"
                  f"  dashboard: /p/{args.slug}/decisions\n"
                  f"  cli:       python -m project_factory decide {args.slug} "
                  f"<id> --by <you> --answer \"…\"  (or --defer)")
            return HUMAN_ACTION_NEEDED

    # ---- phase 1.5: the assumptions register (once, before any oracle) ------
    # The decision register (or a reviewed backlog) supplies structured
    # questions; the oracle author consumes this register on every slice.
    if not planmod.assumptions_path(project).exists():
        spent = project.spent_usd()
        print(f"\ndrafting working-assumptions register "
              f"(assumptions_author, opus, ${project_budget - spent:.2f} remaining)…")
        try:
            planmod.draft_assumptions(
                project, budget_usd=project_budget - spent,
                log_path=livelog.path_for(str(project.dir), "project"))
        except RateLimited as e:
            return _report_rate_limited(e, args.slug, None)
        except planmod.PlanError as e:
            print(f"\nassumptions draft failed: {e}", file=sys.stderr)
            return 1
        print(f"assumptions register: {planmod.assumptions_path(project).name}")

    # ---- phase 1.6: the data backbone (once, before any oracle) -------------
    # Entity-level data model for the WHOLE project — entities, keys,
    # relations, owning slice — so cross-slice data decisions aren't made by
    # whichever slice moves first. Columns/constraints stay slice-owned.
    # Hand-author specs/data-backbone.yaml before running to keep full control.
    if not planmod.backbone_path(project).exists():
        spent = project.spent_usd()
        print(f"\ndrafting project data backbone "
              f"(schema_architect, opus, ${project_budget - spent:.2f} remaining)…")
        try:
            planmod.draft_backbone(
                project, budget_usd=project_budget - spent,
                log_path=livelog.path_for(str(project.dir), "project"))
        except RateLimited as e:
            return _report_rate_limited(e, args.slug, None)
        except planmod.PlanError as e:
            print(f"\ndata backbone draft failed: {e}", file=sys.stderr)
            return 1
        print(f"data backbone: {planmod.backbone_path(project).name}")

    # ---- phase 1.7: UI/UX preview (project-level gate) -----------------------
    # Render what the product will feel like — tokens, primitives, low-fi
    # screens — BEFORE slice work, and hold for human approval. With
    # gates.uiux=auto the phase is skipped outright: a preview nobody
    # reviews is pure spend.
    if gates.get("uiux", "human") == "human":
        from . import uiux as uiuxmod

        if not uiuxmod.preview_path(project).exists():
            spent = project.spent_usd()
            print(f"\ndrafting UI/UX preview "
                  f"(uiux_previewer, opus, ${project_budget - spent:.2f} remaining)…")
            try:
                uiuxmod.draft_preview(
                    project, budget_usd=project_budget - spent,
                    log_path=livelog.path_for(str(project.dir), "project"))
            except RateLimited as e:
                return _report_rate_limited(e, args.slug, None)
            except uiuxmod.UiuxError as e:
                print(f"\nUI/UX preview failed: {e}", file=sys.stderr)
                return 1
            print(f"preview: {uiuxmod.preview_path(project)}")
        if not uiuxmod.is_approved(project):
            print(f"\nTHE UI/UX PREVIEW IS A PROJECT-LEVEL GATE. Review it:\n"
                  f"  dashboard: /p/{args.slug}/uiux\n"
                  f"  file:      {uiuxmod.preview_path(project)}\n"
                  f"then either approve:\n"
                  f"  python -m project_factory approve-uiux {args.slug} --by <you>\n"
                  f"or demand a redraw (records the note, drops the preview):\n"
                  f"  python -m project_factory approve-uiux {args.slug} "
                  f"--by <you> --redraw \"<what must change>\"")
            return HUMAN_ACTION_NEEDED

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
            except RateLimited as e:
                return _report_rate_limited(e, args.slug, planned["id"])
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
        if s.get("size_warning"):
            print(f"      ! {s['size_warning']}")
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


def _cmd_approve_uiux(args) -> int:
    from . import uiux as uiuxmod
    try:
        project = cfgmod.discover(args.slug, args.workspace)
    except cfgmod.ConfigError as e:
        print(f"config error:\n{e}", file=sys.stderr)
        return 2
    if not args.by:
        print("--by <name> is required: the UI/UX gate must be attributable",
              file=sys.stderr)
        return 2
    try:
        if args.redraw:
            uiuxmod.invalidate(project, by=args.by, note=args.redraw)
            print("redraw recorded — the preview was dropped; the next "
                  f"run-project regenerates it:\n"
                  f"  python -m project_factory run-project {args.slug}")
            return 0
        uiuxmod.approve(project, args.by)
    except uiuxmod.UiuxError as e:
        print(str(e), file=sys.stderr)
        return 1
    print(f"UI/UX approved by {args.by}. Continue: "
          f"python -m project_factory run-project {args.slug}")
    return 0


def _cmd_decide(args) -> int:
    """Record one decision on the register (answer, defer, add, or list)."""
    from . import decisions as decmod
    try:
        project = cfgmod.discover(args.slug, args.workspace)
    except cfgmod.ConfigError as e:
        print(f"config error:\n{e}", file=sys.stderr)
        return 2

    if not args.qid and not args.add:      # list mode
        data = decmod.load_decisions(project)
        if not data:
            print("no decision register yet — run run-project first")
            return 0
        st = decmod.stats(data)
        print(f"register: {st['answered']} answered, {st['deferred']} "
              f"deferred, {st['open']} open "
              f"({st['open_start_blockers']} start-blocker(s))")
        for q in data.get("questions", []):
            mark = {"answered": "✓", "deferred": "→"}.get(q["status"], " ")
            star = "*" if q.get("blocker") == "start" and q["status"] == "open" else " "
            print(f" {mark}{star}[{q['id']:>4}] {q['question']}")
            if q.get("answer"):
                print(f"        = {q['answer']}  ({q['answered_by']})")
        return 0

    try:
        if args.add:
            q = decmod.add_question(
                project, question=args.add, by=args.by,
                blocker=args.blocker or "slices")
            print(f"added [{q['id']}] {q['question']}")
        else:
            q = decmod.resolve(project, args.qid, by=args.by,
                               answer=args.answer, defer=args.defer)
            print(f"[{q['id']}] {q['status']} by {q['answered_by']}")
    except decmod.DecisionError as e:
        print(str(e), file=sys.stderr)
        return 2
    data = decmod.load_decisions(project)
    left = decmod.stats(data)["open_start_blockers"]
    print(f"{left} start-blocker(s) still open"
          + ("" if left else " — run-project can proceed"))
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
    n.add_argument("--board",
                   help="path to the presales *.board.json (a board tree "
                        "export works too), or to a whole engagement FOLDER: "
                        "its board, backlog.json and prose are all taken in, "
                        "and a backlog makes planning deterministic")
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
                    help="override gate policy, e.g. spec=human,decisions=auto "
                         "(default: spec=auto,contract=auto,pr=human,"
                         "decisions=human,uiux=human)")
    rp.add_argument("--max-slices", type=int,
                    help="stop after N completed slices (checkpointing a "
                         "long project into sessions)")
    rp.set_defaults(fn=_cmd_run_project)

    sv = sub.add_parser("serve",
                        help="run the INTEGRATED product (main, all delivered "
                             "slices) on the project's sticky ports")
    sv.add_argument("slug")
    sv.add_argument("--allow-branch", action="store_true",
                    help="serve whatever branch is checked out instead of "
                         "requiring main")
    sv.add_argument("--stop", action="store_true", help="stop the stack")
    sv.set_defaults(fn=_cmd_serve)

    apl = sub.add_parser("approve-plan",
                         help="record human approval of specs/project-plan.json "
                              "(the project-level gate)")
    apl.add_argument("slug")
    apl.add_argument("--by", default="",
                     help="who approved — required, recorded in the plan")
    apl.set_defaults(fn=_cmd_approve_plan)

    dc = sub.add_parser("decide",
                        help="answer/defer a decision-register question "
                             "(specs/decisions.yaml); no id lists the register")
    dc.add_argument("slug")
    dc.add_argument("qid", nargs="?", help="question id (omit to list)")
    dc.add_argument("--answer", help="the decision, recorded verbatim")
    dc.add_argument("--defer", action="store_true",
                    help="explicitly defer — a working assumption is recorded "
                         "downstream and the gate unblocks")
    dc.add_argument("--add", metavar="QUESTION",
                    help="add a new question to the register instead")
    dc.add_argument("--blocker",
                    choices=("start", "wave0", "slices", "completion", "none"),
                    help="blocker level for --add (default: slices)")
    dc.add_argument("--by", default="",
                    help="who decided — required, recorded in the register")
    dc.set_defaults(fn=_cmd_decide)

    au = sub.add_parser("approve-uiux",
                        help="approve the UI/UX preview (project-level gate), "
                             "or --redraw to demand changes")
    au.add_argument("slug")
    au.add_argument("--by", default="",
                    help="who decided — required, recorded in specs/uiux.yaml")
    au.add_argument("--redraw", metavar="NOTE",
                    help="reject: record what must change and drop the "
                         "preview so run-project regenerates it")
    au.set_defaults(fn=_cmd_approve_uiux)

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
