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
    if status == "green":
        project.mark_completed(chosen.id)
        print(f"recorded in {project.state_file}")
        nxt = project.next_slice()
        print(f"next slice: {nxt.id}" if nxt else "all slices complete")
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
    p = cfgmod.scaffold(args.slug, args.board, args.workspace, args.slice_name)
    print(f"created {p.dir}")
    print(f"  specs/     {p.board_path.name} + "
          f"{', '.join(s.path.name for s in p.slices)}")
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
    print("\nNext: author the scenarios — this is the ORACLE, the highest-value "
          "work in the pipeline. Then:")
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
        return _report(res, project, chosen)
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
    n.add_argument("--db-reset-consent", action="store_true",
                   help="record consent for the destructive dev-DB reset "
                        "(prisma migrate reset) in run.json now, instead of "
                        "being prompted interactively / stopping mid-run")
    n.set_defaults(fn=_cmd_new)

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
