"""
Infrastructure — drives the STARTER'S OWN tooling. Deterministic, no LLM.

WHY THIS FILE GOT SIMPLER
    The starter (akbarali-sa/turborepo-starter-kit @ starter-minimal) already
    ships everything we were about to rebuild:

        pnpm db:up        docker compose -f apps/api/database/docker-compose.yml up -d
        pnpm db:migrate   prisma migrate (creates + applies)
        pnpm db:deploy    prisma migrate deploy (CI/prod path)
        pnpm init-db      seeds 3 demo users
        pnpm db:init      db:up + db:migrate + init-db --force
        pnpm db:reset     drop + re-migrate + re-seed   <- our determinism lever
        pnpm test:e2e     turbo run test:e2e --filter=@repo/web (Playwright)

    Rule: never reimplement what the starter already does. Every custom
    equivalent is a thing that silently drifts from the template.

WHY api/web ARE NOT IN DOCKER (for slice 1)
    The starter has NO Dockerfile for api or web — only the Postgres compose.
    Adding them means solving pnpm-workspace container builds (`turbo prune`,
    platform-specific binaries like lightningcss and the Prisma engines). That
    is a real side-quest that de-risks nothing about the FACTORY.

    So: Postgres runs in Docker (exactly as the template intends), and api+web
    run as local processes via `start-server-and-test`, which the starter already
    has as a devDependency. E2E hits real HTTP against a real Postgres — the
    oracle strength is identical. Containerising the apps is a later, optional
    step (see docker/docker-compose.app.yml).
"""

from __future__ import annotations

import os
import pathlib
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

STATE_COMPOSE = "docker/docker-compose.state.yml"
STARTER_DB_COMPOSE = "apps/api/database/docker-compose.yml"

# Pin the toolchain the starter declares (engines.node >= 24, pnpm 11.15.1).
# A different pnpm major resolves the lockfile differently -> non-reproducible.
EXPECTED_PNPM_MAJOR = 11
MIN_NODE_MAJOR = 24


@dataclass
class Stack:
    project_slug: str
    api_port: int
    web_port: int
    compose_project: str          # COMPOSE_PROJECT_NAME -> isolates containers

    @property
    def api_url(self) -> str:
        return f"http://localhost:{self.api_port}"

    @property
    def web_url(self) -> str:
        return f"http://localhost:{self.web_port}"


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
# Factory-state Postgres (LangGraph checkpoints) — our own, port 5433
# -----------------------------------------------------------------------------
def ensure_state_db(factory_root: str, conn: str) -> str:
    _run(["docker", "compose", "-f", STATE_COMPOSE, "up", "-d"], cwd=factory_root)
    for _ in range(45):
        if _run(["docker", "exec", "project-factory-state", "pg_isready",
                 "-U", "root", "-d", "project_factory_state"], check=False,
                timeout=30).returncode == 0:
            return conn
        time.sleep(1)
    raise RuntimeError("project-factory-state postgres never became ready")


# -----------------------------------------------------------------------------
# Project app DB — the starter's compose, namespaced per project
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
    }
    kept = [l for l in lines if l.split("=")[0].strip() not in over and l.strip()]
    api_env.write_text("\n".join(kept + [f"{k}={v}" for k, v in over.items()]) + "\n")

    web_env = pathlib.Path(repo) / "apps/web/.env"
    wl = web_env.read_text().splitlines() if web_env.exists() else []
    wkept = [l for l in wl if not l.startswith("NEXT_PUBLIC_API_URL") and l.strip()]
    web_env.write_text("\n".join(wkept + [f"NEXT_PUBLIC_API_URL={stack.api_url}"]) + "\n")


def db_up(repo: str, stack: Stack) -> None:
    """Start Postgres using the STARTER'S compose, namespaced per project."""
    env = {"COMPOSE_PROJECT_NAME": stack.compose_project}
    _run(["pnpm", "db:up"], cwd=repo, env=env, timeout=600)

    # Wait on the container the starter's compose created.
    for _ in range(60):
        p = _run(["docker", "compose", "-f", STARTER_DB_COMPOSE, "ps", "-q"],
                 cwd=repo, env=env, check=False, timeout=60)
        cid = (p.stdout or "").strip().splitlines()
        if cid:
            hp = _run(["docker", "exec", cid[0], "pg_isready"], check=False, timeout=30)
            if hp.returncode == 0:
                return
        time.sleep(1)
    raise RuntimeError("project postgres never became ready")


def install(repo: str) -> None:
    _run(["pnpm", "install", "--frozen-lockfile"], cwd=repo, timeout=2400)


def reset_db(repo: str, stack: Stack) -> None:
    """
    Deterministic starting state: drop + re-migrate + re-seed.

    `db:reset` is the single most valuable determinism lever we have — every run
    starts from an identical database, so a test failure is about the code, never
    about leftover rows from the previous attempt.
    """
    env = {"COMPOSE_PROJECT_NAME": stack.compose_project, "TZ": "UTC"}
    _run(["pnpm", "db:reset"], cwd=repo, env=env, timeout=900)


def migrate(repo: str, stack: Stack) -> None:
    """Create + apply a migration for the Architect's approved schema."""
    env = {"COMPOSE_PROJECT_NAME": stack.compose_project, "TZ": "UTC"}
    _run(["pnpm", "db:migrate"], cwd=repo, env=env, timeout=900)
    _run(["pnpm", "db:generate"], cwd=repo, env=env, timeout=600)


def seed_template_users(repo: str, stack: Stack) -> None:
    """Layer 1 seed: the starter's 3 demo users (identity for E2E login)."""
    env = {"COMPOSE_PROJECT_NAME": stack.compose_project, "TZ": "UTC"}
    _run(["pnpm", "init-db:force"], cwd=repo, env=env, timeout=600)


def db_down(repo: str, stack: Stack) -> None:
    _run(["pnpm", "db:down"], cwd=repo, check=False,
         env={"COMPOSE_PROJECT_NAME": stack.compose_project}, timeout=300)


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
           "COMPOSE_PROJECT_NAME": stack.compose_project,
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
    p = subprocess.run(
        cmd, cwd=repo, capture_output=True, text=True, timeout=2400,
        env={**os.environ, "TZ": "UTC", "BASE_URL": stack.web_url,
             "API_URL": stack.api_url,
             "COMPOSE_PROJECT_NAME": stack.compose_project},
    )
    out = (p.stdout or "") + (p.stderr or "")
    if p.returncode != 0:
        out += "\n\n--- api process output ---\n" + tail("api")
    return E2EResult(p.returncode == 0, out, f"exit={p.returncode}")


def teardown(repo: str, stack: Stack, keep_db: bool = False) -> None:
    stop_stack()
    if not keep_db:
        db_down(repo, stack)
