"""
Background process control for the dashboard — Start/Stop/Resume runs and
Approve/Reject gates by shelling out to the SAME CLI entry points a human
would type (`python -m project_factory run|approve ...`), never
reimplementing LangGraph resume/interrupt logic here. One source of truth
for pipeline execution semantics (budget checks, resume correctness, gate
handling); the dashboard is a UI over it, not a second implementation of it.

Both `run` and `approve` are long-running (`approve` resumes execution and
can run just as long as `run` — through however many nodes until the next
gate, park, or END), so both go through the same background-launch path.

CONCURRENCY: the run lock itself (get_run_state/pidfile) lives in
project_factory.livelog, NOT here, because it must protect a slice's
checkpoint thread regardless of who invokes it — a human typing the command
in a terminal registers in the exact same pidfile `_cmd_run`/`_cmd_approve`
check, so a manually-started run is visible to (and respected by) this
dashboard too, not just runs the dashboard itself launched.

PROCESS LIFECYCLE
    The launched CLI process is independent once started, same as if you'd
    typed the command yourself in a terminal and closed the terminal —
    tracking survives a dashboard restart because it's a file, not
    in-memory state. `start_new_session=True` makes it its own process
    group, so Stop can kill the whole group (pnpm/docker/claude children it
    spawned) without touching the dashboard's own process.

    stdout/stderr are redirected straight to a FILE (livelog.py's live
    activity log for the slice), not read via a PIPE in this process: a pipe
    nobody is actively draining eventually fills its OS buffer and makes the
    child block on write() — meaning a dashboard restart could silently hang
    a real pipeline run. A file has no such limit.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

from project_factory import livelog


class RunnerError(RuntimeError):
    """Actionable, user-facing — app.py surfaces this as an HTTP 409."""


def get_state(project_dir: str, slice_id: str) -> dict:
    return livelog.get_run_state(project_dir, slice_id)


def _launch(factory_root: str, project_dir: str, slice_id: str, argv: list[str]) -> dict:
    state = livelog.get_run_state(project_dir, slice_id)
    if state["running"]:
        raise RunnerError(f"a process (pid {state['pid']}) is already running for this slice")

    log_path = livelog.path_for(project_dir, slice_id)
    cmd = [sys.executable, "-u", "-m", "project_factory", *argv]
    out = open(log_path, "a")
    try:
        proc = subprocess.Popen(
            cmd, cwd=factory_root, stdout=out, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        out.close()  # child holds its own dup'd fd — safe to close ours

    # Pre-register the CHILD's pid immediately so a `status` poll right after
    # Start shows "running" with no gap — the child then confirms this same
    # registration itself via acquire_run_lock() once it reaches that point
    # in _cmd_run/_cmd_approve (matching pid = confirmation, not a collision).
    livelog.pidfile_for(project_dir, slice_id).write_text(
        json.dumps({"pid": proc.pid, "started_at": livelog.now_iso(), "cmd": cmd})
    )
    livelog.append(log_path, f"════ dashboard launched: {' '.join(argv)} (pid {proc.pid}) ════")
    return livelog.get_run_state(project_dir, slice_id)


def start_run(factory_root: str, project_dir: str, slug: str, slice_id: str,
              until: str | None = None) -> dict:
    argv = ["run", slug, "--slice", slice_id]
    if until:
        argv += ["--until", until]
    return _launch(factory_root, project_dir, slice_id, argv)


def start_run_project(factory_root: str, project_dir: str, slug: str,
                      gates: str | None = None) -> dict:
    """
    Launch `run-project <slug>` detached. It logs to the 'project' live log
    (planning + oracle drafting) and each slice it drives then logs to that
    slice's own live log via the per-slice run locks it acquires — the
    'project' pidfile here only guards against two concurrent run-projects.
    """
    argv = ["run-project", slug]
    if gates:
        argv += ["--gates", gates]
    return _launch(factory_root, project_dir, "project", argv)


def start_approve(factory_root: str, project_dir: str, slug: str, slice_id: str, *,
                   reject: bool = False, note: str = "", by: str = "") -> dict:
    argv = ["approve", slug, "--slice", slice_id]
    if reject:
        argv += ["--reject"]
    if note:
        argv += ["--note", note]
    if by:
        argv += ["--by", by]
    return _launch(factory_root, project_dir, slice_id, argv)


def stop(project_dir: str, slice_id: str, grace_s: float = 10.0) -> dict:
    state = livelog.get_run_state(project_dir, slice_id)
    if not state["running"]:
        return {"stopped": False, "reason": "nothing running"}

    pid = state["pid"]
    log_path = livelog.path_for(project_dir, slice_id)
    livelog.append(log_path, f"════ dashboard: stopping pid {pid} (SIGTERM) ════")
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass

    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline and livelog.is_alive(pid):
        time.sleep(0.3)

    if livelog.is_alive(pid):
        livelog.append(log_path, f"════ dashboard: pid {pid} still alive after {grace_s:.0f}s — SIGKILL ════")
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    livelog.release_run_lock(project_dir, slice_id)
    livelog.append(
        log_path,
        "════ dashboard: stopped. Progress is safe up to the last completed "
        "checkpoint node — Run resumes from there. ════",
    )
    return {"stopped": True}
