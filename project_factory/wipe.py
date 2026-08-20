"""
Permanently remove a project and every trace the factory left behind:
tracked runner processes, repo-owned dev servers, the generated app's
database, its LangGraph checkpoint threads, and the project directory
itself (specs, repo + all branches, .factory state, live logs).

Deletion must work on BROKEN projects too (half-scaffolded, bad board,
missing plan), so nothing here requires discover() to succeed — config is
read leniently and every step records its own error instead of aborting
the wipe. The caller decides how loudly to surface partial failures.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import signal
import time
from typing import Any

from . import config as cfgmod
from . import livelog
from .infra import _kill_repo_dev_servers, _owned_by_repo, _pids_listening

# LangGraph's PostgresSaver tables, children first so the wipe never trips
# over a foreign-key order. checkpoint_migrations is schema bookkeeping,
# not project data — it stays.
_CHECKPOINT_TABLES = ("checkpoint_writes", "checkpoint_blobs", "checkpoints")


def _lenient_cfg(pdir: pathlib.Path) -> dict:
    """discover()-free config: defaults + whatever of run.json still parses."""
    cfg = {**cfgmod.BUILT_IN_DEFAULTS, **cfgmod._load_defaults()}
    rf = pdir / "run.json"
    if rf.exists():
        try:
            cfg.update({k: v for k, v in json.loads(rf.read_text()).items()
                        if not k.startswith("_")})
        except (json.JSONDecodeError, OSError):
            pass
    return cfg


def _stop_tracked_runs(pdir: pathlib.Path) -> list[int]:
    """
    Kill every process registered in .factory/live/*.pid — slice runs and the
    project runner alike. Process GROUPS, not single pids: runner._launch
    starts children in their own session, so the group catches the claude
    subprocesses a bare kill(pid) would orphan mid-write.
    """
    killed: list[int] = []
    live = pdir / ".factory" / "live"
    for pf in (live.glob("*.pid") if live.is_dir() else ()):
        try:
            pid = int(json.loads(pf.read_text())["pid"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError):
            continue
        if not livelog.is_alive(pid):
            continue
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(os.getpgid(pid), sig)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    os.kill(pid, sig)
                except (ProcessLookupError, PermissionError):
                    break
            for _ in range(50):  # up to 5s of grace before escalating
                if not livelog.is_alive(pid):
                    break
                time.sleep(0.1)
            if not livelog.is_alive(pid):
                killed.append(pid)
                break
    return killed


def _kill_stack(pdir: pathlib.Path, repo: pathlib.Path) -> list[int]:
    """The generated app's servers: whatever listens on the project's saved
    ports (api + web) and is owned by the repo, plus any repo-owned `next
    dev` on another port (Next's repo-scoped guard finds those; lsof can't)."""
    killed: list[int] = []
    state_file = pdir / ".factory" / "state.json"
    ports: dict = {}
    if state_file.exists():
        try:
            ports = json.loads(state_file.read_text()).get("ports") or {}
        except (json.JSONDecodeError, OSError):
            pass
    for port in {p for p in ports.values() if isinstance(p, int)}:
        for pid in _pids_listening(port):
            if _owned_by_repo(pid, str(repo)):
                try:
                    os.kill(pid, signal.SIGKILL)
                    killed.append(pid)
                except ProcessLookupError:
                    pass
    if repo.is_dir():
        killed += _kill_repo_dev_servers(str(repo))
    return killed


def _drop_database(cfg: dict, slug: str) -> str:
    """Drop the generated app's database (slug with dashes as underscores —
    the same derivation provision_db uses). FORCE evicts lingering
    connections (Prisma Studio, a psql tab) that would otherwise block."""
    import psycopg

    db_name = slug.replace("-", "_")
    admin_url = (f"postgresql://{cfg['db_user']}:{cfg['db_password']}@"
                 f"{cfg['db_host']}:{cfg['db_port']}/postgres")
    with psycopg.connect(admin_url, autocommit=True, connect_timeout=5) as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
            except psycopg.errors.SyntaxError:
                # Postgres < 13 has no WITH (FORCE)
                cur.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
    return db_name


def _delete_checkpoints(cfg: dict, slug: str) -> int:
    """Delete every checkpoint thread of this project — thread ids are
    '<slug>:<slice_id>' plus ':gN' re-run generations, so one LIKE catches
    slices, generations, and the audit trail of abandoned runs."""
    import psycopg

    esc = (slug.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_"))
    pattern = f"{esc}:%"
    deleted = 0
    with psycopg.connect(cfg["checkpoint_db_url"], autocommit=True,
                         connect_timeout=5) as conn:
        with conn.cursor() as cur:
            for table in _CHECKPOINT_TABLES:
                cur.execute("SELECT to_regclass(%s)", (table,))
                if cur.fetchone()[0] is None:
                    continue
                cur.execute(
                    f"DELETE FROM {table} WHERE thread_id LIKE %s ESCAPE '\\'",  # noqa: S608 — table from fixed tuple
                    (pattern,))
                deleted += cur.rowcount
    return deleted


def wipe_project(slug: str, workspace: str | None = None) -> dict[str, Any]:
    """
    The one entry point. Returns a summary of what was removed; per-step
    failures land in `errors` (step -> message) rather than raising, because
    a dead Postgres must not leave a half-deleted project undeletable.
    Raises only when the project directory itself doesn't exist or the
    final rmtree fails — those the caller must hear about.
    """
    if cfgmod.slugify(slug) != slug:
        raise cfgmod.ConfigError(f"not a valid project slug: {slug!r}")
    ws = cfgmod.resolve_workspace(workspace)
    pdir = (ws / slug).resolve()
    if pdir.parent != ws.resolve() or not pdir.is_dir():
        raise cfgmod.ConfigError(f"no project directory {ws / slug}")

    cfg = _lenient_cfg(pdir)
    repo = pdir / "repo"
    summary: dict[str, Any] = {"slug": slug, "dir": str(pdir), "errors": {}}

    try:
        summary["runs_killed"] = _stop_tracked_runs(pdir)
    except Exception as e:  # noqa: BLE001 — every step reports, none aborts
        summary["errors"]["runs"] = str(e)
    try:
        summary["servers_killed"] = _kill_stack(pdir, repo)
    except Exception as e:  # noqa: BLE001
        summary["errors"]["servers"] = str(e)
    try:
        summary["database_dropped"] = _drop_database(cfg, slug)
    except Exception as e:  # noqa: BLE001
        summary["errors"]["database"] = str(e)
    try:
        summary["checkpoint_rows_deleted"] = _delete_checkpoints(cfg, slug)
    except Exception as e:  # noqa: BLE001
        summary["errors"]["checkpoints"] = str(e)

    shutil.rmtree(pdir)
    summary["dir_deleted"] = True
    return summary
