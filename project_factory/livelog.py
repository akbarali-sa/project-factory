"""
Per-slice LIVE activity log — deliberately separate from the LangGraph
checkpoint.

WHY THIS EXISTS
    LangGraph only durably records state once a node RETURNS. `app.invoke()`
    runs the whole graph in one blocking call, and `claude -p
    --output-format json` returns a single JSON blob at process exit — so a
    20-minute Architect call or a `pnpm install` was, until now, completely
    silent: nothing to watch, on the CLI or in the checkpoint, for its whole
    duration. This module gives the dashboard something to tail in real
    time. It is disposable, per-run scratch (plain text file, reset at the
    start of a fresh run) — not state that needs to survive a schema
    migration, so it deliberately isn't in Postgres.

Kept dependency-light (stdlib only) so it stays importable everywhere
project_factory is (config.py's langgraph-free constraint).
"""

from __future__ import annotations

import datetime
import json
import os
import pathlib
import re
import select
import subprocess
import time
from collections.abc import Iterator


def path_for(project_dir: str, slice_id: str) -> pathlib.Path:
    p = pathlib.Path(project_dir) / ".factory" / "live" / f"{slice_id}.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _day_marker_path(log_path: pathlib.Path) -> pathlib.Path:
    return log_path.with_suffix(log_path.suffix + ".day")


_AGENT_COST = re.compile(r"✓ \w+ done in \d+s \(\$(\d+\.\d+)\)")


def live_log_cost(project_dir: str, slice_id: str,
                  include_archived: bool = True) -> float:
    """
    Ground-truth spend for a slice: the sum of every completed agent's
    '✓ <agent> done in Ns ($X.XXX)' line in the live log. The checkpointed
    Usage counter loses any node that raises AFTER its agent call completed
    (the state update never commits), so it is a floor — this is the total.
    """
    live = pathlib.Path(project_dir) / ".factory" / "live"
    current = live / f"{slice_id}.log"
    # include_archived: every generation of this slice, because the project
    # ledger asks "what has this slice cost me", not "what did the latest
    # attempt cost".
    paths = [current] if not include_archived else \
        [current, *sorted(live.glob(f"{slice_id}.log.[0-9]*"))]
    total = 0.0
    for p in paths:
        if p.exists():
            total += sum(float(m.group(1)) for m in _AGENT_COST.finditer(p.read_text()))
    return round(total, 2)


def reset(project_dir: str, slice_id: str) -> None:
    """Start a fresh run's log without destroying the previous one.

    The old log is ARCHIVED, not truncated. Thread generations exist so a
    slice can be re-run and the two runs compared — and truncating on the new
    generation deleted exactly the evidence needed to compare them. It also
    destroyed the cost record: live_log_cost() is the ground truth the project
    ledger trusts, so slice 1 of the barcode project reported $0.00 after its
    g2 rerun even though it had cost $48.12.

    Never called on resume (ingest runs once per thread — LangGraph skips it
    when resuming), so a resumed run keeps its history, including across
    however many days a paused-at-a-gate slice takes to finish; see
    `_ensure_day_banner` for how that stays legible.
    """
    p = path_for(project_dir, slice_id)
    if p.exists() and p.stat().st_size > 0:
        archive = _next_archive_path(p)
        p.replace(archive)
    p.write_text("")
    _day_marker_path(p).unlink(missing_ok=True)


def _next_archive_path(log_path: pathlib.Path) -> pathlib.Path:
    """<slice>.log -> <slice>.log.1, .2, … — oldest run keeps the lowest
    number, so the sequence reads chronologically."""
    n = 1
    while (candidate := log_path.with_suffix(log_path.suffix + f".{n}")).exists():
        n += 1
    return candidate


def _ensure_day_banner(log_path: pathlib.Path) -> None:
    """
    Every line is timestamped HH:MM:SS only — fine within a day, ambiguous
    across one. A slice can sit paused at a gate for days, so rather than
    rotating to a new file (which would break "one file per slice, tail it
    from the start" for the dashboard and for anyone `tail -f`-ing by hand),
    write one banner line the first time a new calendar day is seen.

    A tiny sidecar file records the last banner's date so this is an O(1)
    check per append — no need to re-scan the (potentially long) log to find
    out whether today's banner already went out, and it survives the CLI
    exiting and being re-invoked hours or days later for the same slice.
    """
    today = datetime.date.today().isoformat()
    marker = _day_marker_path(log_path)
    try:
        already = marker.read_text().strip() == today
    except OSError:
        already = False
    if already:
        return
    with log_path.open("a") as f:
        f.write(f"───── {today} ─────\n")
    marker.write_text(today)


# -----------------------------------------------------------------------------
# Cross-invocation run lock — lives HERE (not in the dashboard) because it
# must protect a slice's checkpoint thread regardless of who invokes it: a
# human typing `python -m project_factory run` in a terminal, and the
# dashboard's Start/Stop/Approve buttons, must see the SAME lock. Two
# `app.invoke()` calls racing on the same LangGraph thread_id would corrupt
# the checkpoint, so this is a correctness guard, not just UI bookkeeping.
# -----------------------------------------------------------------------------
def pidfile_for(project_dir: str, slice_id: str) -> pathlib.Path:
    return path_for(project_dir, slice_id).with_suffix(".pid.json")


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def is_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False


def get_run_state(project_dir: str, slice_id: str) -> dict:
    """{"running": False, ...} if nothing holds the lock or its pid is dead
    (self-healing: a stale pidfile from a crashed process is removed here,
    not left to jam every future run/approve for this slice)."""
    pf = pidfile_for(project_dir, slice_id)
    if not pf.exists():
        return {"running": False, "pid": None, "started_at": None, "cmd": None}
    try:
        info = json.loads(pf.read_text())
    except (json.JSONDecodeError, OSError):
        return {"running": False, "pid": None, "started_at": None, "cmd": None}
    if not is_alive(info.get("pid")):
        pf.unlink(missing_ok=True)
        return {"running": False, "pid": None, "started_at": None, "cmd": None}
    return {"running": True, **info}


def acquire_run_lock(project_dir: str, slice_id: str, cmd: list[str]) -> None:
    """
    Raise RuntimeError if a DIFFERENT process already holds the lock.
    Matching `os.getpid()` is not a conflict — it's the dashboard's launcher
    having pre-written the pidfile with the child's pid before the child
    reaches this call (see dashboard/runner.py), so the child registering
    itself here is just confirming, not colliding.
    """
    state = get_run_state(project_dir, slice_id)
    if state["running"] and state["pid"] != os.getpid():
        raise RuntimeError(
            f"another process (pid {state['pid']}, started {state['started_at']}) "
            f"is already running this slice — refusing to start a second one "
            f"against the same checkpoint thread"
        )
    pidfile_for(project_dir, slice_id).write_text(json.dumps({
        "pid": os.getpid(),
        "started_at": state.get("started_at") or now_iso(),
        "cmd": cmd,
    }))


def release_run_lock(project_dir: str, slice_id: str) -> None:
    pidfile_for(project_dir, slice_id).unlink(missing_ok=True)


def append(log_path: pathlib.Path, line: str) -> None:
    _ensure_day_banner(log_path)
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    with log_path.open("a") as f:
        for l in line.splitlines() or [""]:
            f.write(f"[{ts}] {l}\n")


def iter_lines_with_timeout(proc: subprocess.Popen, timeout: float) -> Iterator[str]:
    """
    Yield stdout lines from `proc` as they arrive.

    A plain `for line in proc.stdout` loop can't enforce a timeout: a hung
    tool call that produces zero output would block readline() forever,
    past the timeout, with nothing to interrupt it. Polling the fd with
    `select` every second lets us check elapsed time even while the process
    is silent — this is the entire reason not to just use
    `subprocess.run(..., timeout=...)`.
    """
    start = time.monotonic()
    while True:
        if time.monotonic() - start > timeout:
            proc.kill()
            proc.wait(timeout=5)
            raise subprocess.TimeoutExpired(proc.args, timeout)
        ready, _, _ = select.select([proc.stdout], [], [], 1.0)
        if ready:
            line = proc.stdout.readline()
            if line:
                yield line
            elif proc.poll() is not None:
                break
        elif proc.poll() is not None:
            break
    for line in proc.stdout:  # drain whatever was buffered after the last poll
        yield line


def tee_subprocess(
    cmd: list[str], *, cwd: str | None = None, env: dict | None = None,
    timeout: int = 900, check: bool = True, log_path: pathlib.Path | None = None,
) -> subprocess.CompletedProcess:
    """
    `subprocess.run` replacement that also streams stdout/stderr to
    `log_path` line-by-line as the process runs, instead of only returning
    the full text after it exits. Drop-in: same return shape, same
    RuntimeError-on-nonzero-exit contract as infra.py's existing `_run`, so
    every call site behaves identically whether or not `log_path` is given.
    """
    import os

    if log_path is not None:
        append(log_path, f"$ {' '.join(cmd)}")
    proc = subprocess.Popen(
        cmd, cwd=cwd, env={**os.environ, **(env or {})},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    lines: list[str] = []
    try:
        for line in iter_lines_with_timeout(proc, timeout):
            lines.append(line)
            if log_path is not None:
                append(log_path, line.rstrip("\n"))
    finally:
        proc.wait(timeout=5)
    out = "".join(lines)
    if check and proc.returncode != 0:
        raise RuntimeError(f"$ {' '.join(cmd)}\n{out[-2500:]}")
    return subprocess.CompletedProcess(cmd, proc.returncode, out, "")
