"""
Infrastructure — drives the STARTER'S OWN tooling. Deterministic, no LLM.

WHY THIS FILE GOT SIMPLER
    The starter (akbarali-sa/turborepo-starter-kit @ starter-minimal) already
    ships everything we were about to rebuild:

        pnpm db:up        confirms the shared Postgres (see below) is reachable
        pnpm db:migrate   prisma migrate (creates + applies)
        pnpm db:deploy    prisma migrate deploy (CI/prod path)
        pnpm init-db      seeds 3 demo users
        pnpm db:init      db:up + db:migrate + init-db --force
        pnpm db:reset     drop + re-migrate + re-seed   <- our determinism lever
        pnpm test:e2e     turbo run test:e2e --filter=@repo/web (Playwright)

    Rule: never reimplement what the starter already does. Every custom
    equivalent is a thing that silently drifts from the template.

ONE SHARED POSTGRES, NOT A CONTAINER PER PROJECT
    Every local project — this factory's own checkpoints, every generated
    app's DB, unrelated local tools — talks to the SAME already-running
    Postgres instance (pgvector-based; db_host/db_port/db_user/db_password in
    defaults.json), each in its own DATABASE namespaced by project slug.
    `db_up`/`db_down` in this file own creating/dropping that database
    (`CREATE DATABASE`/`DROP DATABASE` via psycopg); the starter's own
    `db:up` script just confirms reachability over DATABASE_URL. Spinning up
    a dedicated container per project was the old design — it fragments a
    laptop into N slightly-different Postgres images/containers for no
    benefit, since Postgres already isolates by database.

WHY api/web ARE NOT IN DOCKER (for slice 1)
    The starter has NO Dockerfile for api or web. Adding them means solving
    pnpm-workspace container builds (`turbo prune`, platform-specific binaries
    like lightningcss and the Prisma engines). That is a real side-quest that
    de-risks nothing about the FACTORY.

    So: api+web run as local processes via `start-server-and-test`, which the
    starter already has as a devDependency. E2E hits real HTTP against the
    real shared Postgres — the oracle strength is identical. Containerising
    the apps is a later, optional step (see docker/docker-compose.app.yml).
"""

from __future__ import annotations

import os
import pathlib
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

# Pin the toolchain the starter declares (engines.node >= 24, pnpm 11.15.1).
# A different pnpm major resolves the lockfile differently -> non-reproducible.
EXPECTED_PNPM_MAJOR = 11
MIN_NODE_MAJOR = 24


def _ensure_database(admin_conn_url: str, dbname: str) -> None:
    """CREATE DATABASE if missing, on the ONE shared Postgres instance every
    local project uses. Lazy import: infra.py must stay importable without
    psycopg installed (dry-run works on a pyyaml-only checkout)."""
    import psycopg

    with psycopg.connect(admin_conn_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname=%s", (dbname,))
            if not cur.fetchone():
                cur.execute(f'CREATE DATABASE "{dbname}"')


@dataclass
class Stack:
    project_slug: str
    api_port: int
    web_port: int
    db_name: str
    db_host: str = "localhost"
    db_port: int = 5432
    db_user: str = "postgres"
    db_password: str = "postgres"

    @property
    def api_url(self) -> str:
        return f"http://localhost:{self.api_port}"

    @property
    def web_url(self) -> str:
        return f"http://localhost:{self.web_port}"

    @property
    def database_url(self) -> str:
        return (f"postgresql://{self.db_user}:{self.db_password}@"
                f"{self.db_host}:{self.db_port}/{self.db_name}?schema=public")

    @property
    def admin_url(self) -> str:
        """Connects to the shared server's own `postgres` maintenance db —
        needed to CREATE/DROP the project's database by name."""
        return (f"postgresql://{self.db_user}:{self.db_password}@"
                f"{self.db_host}:{self.db_port}/postgres")


def _run(cmd: list[str], cwd: str | None = None, timeout: int = 900,
         check: bool = True, env: dict | None = None) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                       timeout=timeout, env={**os.environ, **(env or {})})
    if check and p.returncode != 0:
        raise RuntimeError(f"$ {' '.join(cmd)}\n{(p.stdout + p.stderr)[-2500:]}")
    return p


# -----------------------------------------------------------------------------
# Preflight
# -----------------------------------------------------------------------------
def preflight() -> str:
    """Fail fast and loudly rather than 20 minutes into a run."""
    problems: list[str] = []
    if shutil.which("docker") is None:
        problems.append("docker not on PATH (Docker Desktop / OrbStack)")
    else:
        if _run(["docker", "info"], timeout=60, check=False).returncode != 0:
            problems.append("docker daemon not running")
    if shutil.which("pnpm") is None:
        problems.append("pnpm not on PATH")
    else:
        v = _run(["pnpm", "--version"], timeout=60, check=False).stdout.strip()
        if v and int(v.split(".")[0]) != EXPECTED_PNPM_MAJOR:
            problems.append(f"pnpm {v} — starter expects {EXPECTED_PNPM_MAJOR}.x")
    if shutil.which("node") is None:
        problems.append("node not on PATH")
    else:
        v = _run(["node", "--version"], timeout=60, check=False).stdout.strip().lstrip("v")
        if v and int(v.split(".")[0]) < MIN_NODE_MAJOR:
            problems.append(f"node {v} — starter requires >= {MIN_NODE_MAJOR}")
    if shutil.which("claude") is None:
        problems.append("claude CLI not on PATH (run: claude setup-token)")
    if os.environ.get("ANTHROPIC_API_KEY"):
        problems.append("ANTHROPIC_API_KEY is set — unset it to bill your subscription")
    if problems:
        raise RuntimeError("preflight failed:\n  - " + "\n  - ".join(problems))
    return "preflight ok"


def _free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def postgres_reachable(host: str, port: int, timeout: float = 3.0) -> bool:
    """Is the ONE shared Postgres instance (see defaults.json) up? We never
    start/stop it ourselves — it's expected to already be running."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def allocate_ports(base_api: int, base_web: int) -> tuple[int, int]:
    out = []
    for base in (base_api, base_web):
        p = base
        while not _free(p):
            p += 1
            if p > base + 50:
                raise RuntimeError(f"no free port near {base}")
        out.append(p)
    return out[0], out[1]


# -----------------------------------------------------------------------------
# Factory-state Postgres (LangGraph checkpoints) — a DATABASE on the shared
# instance, not a dedicated container.
# -----------------------------------------------------------------------------
def ensure_state_db(conn_url: str) -> str:
    parsed = urllib.parse.urlsplit(conn_url)
    dbname = parsed.path.lstrip("/")
    admin_url = urllib.parse.urlunsplit(parsed._replace(path="/postgres"))
    _ensure_database(admin_url, dbname)
    return conn_url


# -----------------------------------------------------------------------------
# Project app DB — its own DATABASE on the same shared instance, namespaced
# per project by name (not by container).
# -----------------------------------------------------------------------------
def write_env(repo: str, stack: Stack, jwt_secret: str) -> None:
    """
    Create .env files from the starter's env.example files.

    We copy env.example first so we inherit any keys the template adds later,
    then override only what we need. Blindly writing our own .env is how you
    silently lose a new required variable after an upstream sync.
    """
    for app in ("api", "web"):
        example = pathlib.Path(repo) / f"apps/{app}/env.example"
        target = pathlib.Path(repo) / f"apps/{app}/.env"
        if example.exists() and not target.exists():
            shutil.copy2(example, target)

    api_env = pathlib.Path(repo) / "apps/api/.env"
    lines = api_env.read_text().splitlines() if api_env.exists() else []
    over = {
        "NODE_ENV": "test",
        "TZ": "UTC",
        "JWT_SECRET": jwt_secret,
        "PORT": "3001",
        "DATABASE_URL": stack.database_url,
    }
    kept = [l for l in lines if l.split("=")[0].strip() not in over and l.strip()]
    api_env.write_text("\n".join(kept + [f"{k}={v}" for k, v in over.items()]) + "\n")

    web_env = pathlib.Path(repo) / "apps/web/.env"
    wl = web_env.read_text().splitlines() if web_env.exists() else []
    wkept = [l for l in wl if not l.startswith("NEXT_PUBLIC_API_URL") and l.strip()]
    web_env.write_text("\n".join(wkept + [f"NEXT_PUBLIC_API_URL={stack.api_url}"]) + "\n")


def db_up(repo: str, stack: Stack) -> None:
    """Create the project's database on the shared instance (idempotent), then
    let the starter's own db:up confirm it can reach it via DATABASE_URL."""
    _ensure_database(stack.admin_url, stack.db_name)
    _run(["pnpm", "db:up"], cwd=repo, timeout=60)


def install(repo: str) -> None:
    _run(["pnpm", "install", "--frozen-lockfile"], cwd=repo, timeout=2400)


def _consent_env(consent: str | None) -> dict:
    """
    Prisma's CLI detects it's being invoked by an AI agent and refuses to run a
    destructive command (migrate reset, and sometimes migrate dev when it would
    reset) without PRISMA_USER_CONSENT_FOR_DANGEROUS_AI_ACTION set to the exact
    text of the human's consent message. `consent` must come from a human who
    reviewed and approved it for THIS project (config.py's db_reset_consent) —
    never invent or default this value.
    """
    return {"PRISMA_USER_CONSENT_FOR_DANGEROUS_AI_ACTION": consent} if consent else {}


def reset_db(repo: str, stack: Stack, consent: str | None = None) -> None:
    """
    Deterministic starting state: drop + re-migrate + re-seed.

    `db:reset` is the single most valuable determinism lever we have — every run
    starts from an identical database, so a test failure is about the code, never
    about leftover rows from the previous attempt.
    """
    _run(["pnpm", "db:reset"], cwd=repo,
         env={"TZ": "UTC", **_consent_env(consent)}, timeout=900)


def migrate(repo: str, stack: Stack, consent: str | None = None) -> None:
    """Create + apply a migration for the Architect's approved schema."""
    _run(["pnpm", "db:migrate"], cwd=repo,
         env={"TZ": "UTC", **_consent_env(consent)}, timeout=900)
    _run(["pnpm", "db:generate"], cwd=repo, env={"TZ": "UTC"}, timeout=600)


def seed_template_users(repo: str, stack: Stack) -> None:
    """Layer 1 seed: the starter's 3 demo users (identity for E2E login)."""
    _run(["pnpm", "init-db:force"], cwd=repo, env={"TZ": "UTC"}, timeout=600)


def db_down(repo: str, stack: Stack) -> None:
    """Drop the project's database from the shared instance — there is no
    per-project container to stop."""
    import psycopg

    with psycopg.connect(stack.admin_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{stack.db_name}"')


# -----------------------------------------------------------------------------
# Run the apps + E2E
# -----------------------------------------------------------------------------
def _wait_http(url: str, attempts: int = 90, delay: float = 2.0) -> bool:
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                if 200 <= r.status < 500:
                    return True
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            pass
        time.sleep(delay)
    return False


_processes: dict[str, subprocess.Popen] = {}


def launch_stack(repo: str, stack: Stack) -> str:
    """
    Boot api + web as local processes and block until both answer HTTP.

    Health polling, never `sleep` — timing-based waits are the number-one source
    of "it worked on my laptop" flakiness.
    """
    env = {**os.environ, "TZ": "UTC",
           "PORT": str(stack.api_port),
           "NEXT_PUBLIC_API_URL": stack.api_url}

    stop_stack()
    _processes["api"] = subprocess.Popen(
        ["pnpm", "--filter=@repo/api", "dev"], cwd=repo, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if not _wait_http(f"{stack.api_url}/health") and not _wait_http(stack.api_url):
        raise RuntimeError("api never became healthy:\n" + tail("api"))

    _processes["web"] = subprocess.Popen(
        ["pnpm", "--filter=@repo/web", "dev"], cwd=repo,
        env={**env, "PORT": str(stack.web_port)},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if not _wait_http(stack.web_url):
        raise RuntimeError("web never became healthy:\n" + tail("web"))

    return f"api={stack.api_url} web={stack.web_url} (postgres in docker)"


def tail(which: str, lines: int = 80) -> str:
    p = _processes.get(which)
    if not p or not p.stdout:
        return f"(no output captured for {which})"
    out: list[str] = []
    try:
        for _ in range(lines):
            line = p.stdout.readline()
            if not line:
                break
            out.append(line.rstrip())
    except Exception:  # noqa: BLE001
        pass
    return "\n".join(out[-lines:])


def stop_stack() -> None:
    for name, p in list(_processes.items()):
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=20)
            except subprocess.TimeoutExpired:
                p.kill()
        _processes.pop(name, None)


@dataclass
class E2EResult:
    ok: bool
    output: str
    summary: str


def run_e2e(repo: str, stack: Stack, grep: str | None = None) -> E2EResult:
    """
    Playwright via the starter's own script. Pin the Playwright version in
    package.json or browser behaviour drifts between machines.
    """
    cmd = ["pnpm", "test:e2e"]
    if grep:
        cmd += ["--", "--grep", grep]
    # playwright.config.ts reads NEXT_PUBLIC_WEB_URL/NEXT_PUBLIC_API_URL (not
    # BASE_URL/API_URL) to decide whether a server is already running and
    # should be reused. Passing the wrong names means it never recognizes the
    # servers launch_stack() already started, so it tries to boot its own
    # redundant copies — which then time out against config.webServer.
    p = subprocess.run(
        cmd, cwd=repo, capture_output=True, text=True, timeout=2400,
        env={**os.environ, "TZ": "UTC",
             "NEXT_PUBLIC_WEB_URL": stack.web_url,
             "NEXT_PUBLIC_API_URL": stack.api_url},
    )
    out = (p.stdout or "") + (p.stderr or "")
    if p.returncode != 0:
        out += "\n\n--- api process output ---\n" + tail("api")
    return E2EResult(p.returncode == 0, out, f"exit={p.returncode}")


def teardown(repo: str, stack: Stack, keep_db: bool = False) -> None:
    stop_stack()
    if not keep_db:
        db_down(repo, stack)
