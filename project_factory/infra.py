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

import json
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

from . import livelog

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
         check: bool = True, env: dict | None = None,
         log_path: pathlib.Path | None = None) -> subprocess.CompletedProcess:
    """
    `log_path` streams stdout/stderr line-by-line to the slice's live log as
    the process runs (see livelog.py) — without it, callers get the exact
    same blocking-then-all-at-once behavior as before.
    """
    if log_path is not None:
        return livelog.tee_subprocess(cmd, cwd=cwd, env=env, timeout=timeout,
                                      check=check, log_path=log_path)
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
    """
    Distinct free ports for api and web.

    `taken` is the whole point: scanning each base independently hands BOTH
    services the same port whenever the low ports are busy (defaults 3001/3000
    with 3000-3003 occupied → 3004 twice). The api then binds it, the web spawn
    dies with EADDRINUSE, and the web health check passes against the API —
    so the run tests every web page against a REST server and fails everything
    with a timeout that looks like a product bug.
    """
    taken: set[int] = set()
    out = []
    for base in (base_api, base_web):
        p = base
        while p in taken or not _free(p):
            p += 1
            if p > base + 50:
                raise RuntimeError(f"no free port near {base}")
        taken.add(p)
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
        # The ALLOCATED port, never a literal: allocate_ports may have moved
        # off the default because another project (or a stray process) holds
        # it. Anything that reads .env instead of inheriting launch_stack's
        # env — Playwright's spawned webServer, a human running `pnpm dev` —
        # must agree with the port the factory actually launched on, or e2e
        # spawns a second API that binds the wrong (possibly taken) port.
        "PORT": str(stack.api_port),
        "DATABASE_URL": stack.database_url,
        # The API's CORS allowlist is built from NEXT_PUBLIC_WEB_URL
        # (apps/api/src/main.ts). Left at the template's localhost:3000 while
        # sticky_ports put the web app on another port, EVERY browser fetch
        # is rejected by CORS — which surfaces in Playwright as
        # `net::ERR_FAILED` console noise and a wall of failing e2e tests that
        # look exactly like product bugs. Project #1 happened to land on 3000
        # and hid this; barcode-v2 landed on 3005 and lost three e2e attempts
        # plus two Opus diagnosticians to it.
        "NEXT_PUBLIC_WEB_URL": stack.web_url,
        "NEXT_PUBLIC_API_URL": stack.api_url,
    }
    kept = [l for l in lines if l.split("=")[0].strip() not in over and l.strip()]
    api_env.write_text("\n".join(kept + [f"{k}={v}" for k, v in over.items()]) + "\n")

    web_env = pathlib.Path(repo) / "apps/web/.env"
    wover = {
        "NEXT_PUBLIC_API_URL": stack.api_url,
        "NEXT_PUBLIC_WEB_URL": stack.web_url,
    }
    wl = web_env.read_text().splitlines() if web_env.exists() else []
    wkept = [l for l in wl if l.split("=")[0].strip() not in wover and l.strip()]
    web_env.write_text(
        "\n".join(wkept + [f"{k}={v}" for k, v in wover.items()]) + "\n")


def db_up(repo: str, stack: Stack, log_path: pathlib.Path | None = None) -> None:
    """Create the project's database on the shared instance (idempotent), then
    let the starter's own db:up confirm it can reach it via DATABASE_URL."""
    _ensure_database(stack.admin_url, stack.db_name)
    _run(["pnpm", "db:up"], cwd=repo, timeout=60, log_path=log_path)


def install(repo: str, log_path: pathlib.Path | None = None) -> None:
    _run(["pnpm", "install", "--frozen-lockfile"], cwd=repo, timeout=2400, log_path=log_path)


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


def reset_db(repo: str, stack: Stack, consent: str | None = None,
            log_path: pathlib.Path | None = None) -> None:
    """
    Deterministic starting state: drop + re-migrate + re-seed.

    `db:reset` is the single most valuable determinism lever we have — every run
    starts from an identical database, so a test failure is about the code, never
    about leftover rows from the previous attempt.
    """
    _run(["pnpm", "db:reset"], cwd=repo,
         env={"TZ": "UTC", **_consent_env(consent)}, timeout=900, log_path=log_path)


def migrate(repo: str, stack: Stack, consent: str | None = None,
           log_path: pathlib.Path | None = None) -> None:
    """Create + apply a migration for the Architect's approved schema."""
    _run(["pnpm", "db:migrate"], cwd=repo,
         env={"TZ": "UTC", **_consent_env(consent)}, timeout=900, log_path=log_path)
    _run(["pnpm", "db:generate"], cwd=repo, env={"TZ": "UTC"}, timeout=600, log_path=log_path)


def seed_template_users(repo: str, stack: Stack, log_path: pathlib.Path | None = None) -> None:
    """Layer 1 seed: the starter's 3 demo users (identity for E2E login)."""
    _run(["pnpm", "init-db:force"], cwd=repo, env={"TZ": "UTC"}, timeout=600, log_path=log_path)


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
_process_logs: dict[str, pathlib.Path] = {}


def _spawn_app(name: str, cmd: list[str], repo: str, env: dict) -> subprocess.Popen:
    """
    Server stdout goes to a FILE, never a PIPE. Nothing drains these pipes for
    the (long) lifetime of the servers, and once the ~64KB pipe buffer fills,
    the child blocks on its next write — the server keeps LISTENing but never
    answers another request. That wedge is exactly the opaque "Timed out
    waiting from config.webServer" verify_e2e kept hitting once the api's
    Prisma/request logging filled the buffer.
    """
    import tempfile

    log = pathlib.Path(tempfile.gettempdir()) / f"project-factory-{name}.log"
    _process_logs[name] = log
    f = open(log, "w")
    try:
        return subprocess.Popen(cmd, cwd=repo, env=env,
                                stdout=f, stderr=subprocess.STDOUT, text=True)
    finally:
        f.close()  # Popen dup'ed the fd; the child keeps writing


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
    # stop_stack() only knows THIS process's children. A previous driver
    # process (a resumed run, a crashed one, a dashboard relaunch) leaves
    # servers holding these ports, and its `_processes` dict died with it.
    # Without this sweep the new spawn loses the port bind, while _wait_http
    # happily succeeds against the OLD server — so the run tests code from
    # before the current slice existed and every failure looks like a product
    # bug. Ownership-checked, exactly like ensure_stack.
    _free_project_ports(repo, stack)
    _processes["api"] = _spawn_app("api", ["pnpm", "--filter=@repo/api", "dev"],
                                   repo, env)
    if not _wait_http(f"{stack.api_url}/health") and not _wait_http(stack.api_url):
        raise RuntimeError("api never became healthy:\n" + tail("api"))

    if stack.web_port == stack.api_port:
        # Belt to allocate_ports' braces: a collision here is unrecoverable and
        # masquerades as healthy (the api answers the web URL), so it must be
        # loud rather than merely unlikely.
        raise RuntimeError(
            f"api and web are both assigned port {stack.api_port} — refusing "
            "to launch (see infra.allocate_ports)")

    _processes["web"] = _spawn_app("web", ["pnpm", "--filter=@repo/web", "dev"],
                                   repo, {**env, "PORT": str(stack.web_port)})
    if not _wait_http(stack.web_url):
        raise RuntimeError("web never became healthy:\n" + tail("web"))
    if (proc := _processes.get("web")) is not None and proc.poll() is not None:
        # Something else is answering the web URL — our own web process is
        # already dead (EADDRINUSE is the usual reason). Health-checking a URL
        # only proves SOMEONE is listening, not that it is the app under test.
        raise RuntimeError(
            f"web process exited (code {proc.returncode}) while "
            f"{stack.web_url} still answers — another process owns that "
            "port:\n" + tail("web"))

    return f"api={stack.api_url} web={stack.web_url} (postgres in docker)"


def sticky_ports(project_dir: str, repo: str, base_api: int,
                 base_web: int) -> tuple[int, int]:
    """
    The SAME api/web ports for every slice of a project, remembered in
    .factory/state.json.

    Without this, each slice allocates fresh: a previous slice's servers are
    still listening, so allocate_ports increments past them and slice N lands
    on 3004 while slice N-1 sits on 3000. The generated app then has no stable
    address — which matters most at the end, when the whole integrated product
    is what you want to open and click through.

    Ports we allocated before are reclaimed by killing the listener when it is
    OURS (a previous slice's leftover); a foreign squatter forces a fresh
    allocation instead, since taking someone else's port is never ok.
    """
    state_file = pathlib.Path(project_dir) / ".factory" / "state.json"
    state: dict = {}
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text())
        except json.JSONDecodeError:
            state = {}

    saved = state.get("ports") or {}
    api, web = saved.get("api"), saved.get("web")
    if isinstance(api, int) and isinstance(web, int) and api != web:
        for port in (api, web):
            for pid in _pids_listening(port):
                if _owned_by_repo(pid, repo):
                    try:
                        os.kill(pid, 9)
                    except ProcessLookupError:
                        pass
        time.sleep(1)
        if _free(api) and _free(web):
            return api, web

    api, web = allocate_ports(base_api, base_web)
    state["ports"] = {"api": api, "web": web}
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state, indent=2) + "\n")
    return api, web


def _pids_listening(port: int) -> list[int]:
    p = subprocess.run(["lsof", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"],
                       capture_output=True, text=True)
    return [int(x) for x in p.stdout.split()]


def _free_project_ports(repo: str, stack: Stack) -> list[int]:
    """
    Kill listeners on this stack's ports that verifiably belong to this
    project's repo. A foreign squatter is a loud error, never a casualty —
    same rule as ensure_stack. Returns the pids killed.
    """
    killed: list[int] = []
    for port in (stack.api_port, stack.web_port):
        for pid in _pids_listening(port):
            if not _owned_by_repo(pid, repo):
                raise RuntimeError(
                    f"port {port} is held by a process not from this project "
                    f"(pid {pid}) — refusing to kill it. Free the port and re-run.")
            try:
                os.kill(pid, 9)
                killed.append(pid)
            except ProcessLookupError:
                pass
    if killed:
        time.sleep(2)   # let the kernel release the bind before we re-bind
    return killed


def _owned_by_repo(pid: int, repo: str) -> bool:
    """Does this pid's command line or cwd reference the project repo?"""
    cmd = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                         capture_output=True, text=True).stdout
    if repo in cmd:
        return True
    cwd = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                         capture_output=True, text=True).stdout
    return repo in cwd


def ensure_stack(repo: str, stack: Stack) -> str | None:
    """
    Health-check api+web and relaunch when either is down or wedged.

    A run resumed after a crash or a laptop sleep reaches verify_e2e with
    `launch_stack` long since checkpointed, but its processes dead — or worse,
    suspend-damaged: still LISTENing on the port yet never answering a request,
    which surfaces as Playwright's opaque "timed out waiting from
    config.webServer". Only processes that verifiably belong to this project's
    repo are killed; a foreign squatter on the port is a loud error instead.

    Returns the relaunch summary, or None if the stack was already healthy.
    """
    if _wait_http(f"{stack.api_url}/health", attempts=1, delay=0) and \
            _wait_http(stack.web_url, attempts=3, delay=2):
        return None
    # launch_stack frees the ports itself (_free_project_ports) — one
    # implementation of "whose port is this", used by both paths.
    return launch_stack(repo, stack)


def assert_web_serves_app(stack: Stack) -> None:
    """
    Confirm the web URL is answered by the WEB APP, not by something else.

    A health check proves only that someone is listening. Twice in one project
    that someone was the API — once because api and web were handed the same
    port, once because a previous driver's server still held it — and the whole
    e2e suite then ran against a REST server, failing every test at the login
    page. Three slices spent their entire diagnose budgets on it, roughly eight
    hours, because "every test failed" reads like a product collapse.

    One HTTP request turns that into a sentence.
    """
    try:
        with urllib.request.urlopen(stack.web_url, timeout=15) as response:
            content_type = (response.headers.get("Content-Type") or "").lower()
            body = response.read(2048).decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001 — any failure here is the same verdict
        raise RuntimeError(
            f"web URL {stack.web_url} did not answer ({e}). The stack is not "
            "serving the app under test — fix that before reading any test "
            "failure."
        ) from e

    if "text/html" in content_type or "<html" in body.lower():
        return
    raise RuntimeError(
        f"{stack.web_url} is answering, but not with HTML "
        f"(content-type: {content_type or 'none'}). Something other than the "
        f"web app owns that port — most likely the API. First 200 bytes:\n"
        f"{body[:200]}\n"
        "Every e2e test would fail at the login page and look like a product "
        "bug. Free the port and relaunch instead."
    )


def tail(which: str, lines: int = 80) -> str:
    log = _process_logs.get(which)
    if not log or not log.exists():
        return f"(no output captured for {which})"
    try:
        return "\n".join(log.read_text(errors="replace").splitlines()[-lines:])
    except OSError:
        return f"(could not read log for {which})"


def stop_stack() -> None:
    for name, p in list(_processes.items()):
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=20)
            except subprocess.TimeoutExpired:
                p.kill()
        _processes.pop(name, None)


# The e2e suite GROWS with every delivered slice — each one adds its own specs
# and they all run against the merged app (that cumulative regression net is the
# point). A fixed ceiling that fit slice 1 (44 tests) killed slice 2 mid-run
# (76 tests, failures retried), and the kill looks exactly like a hang. Scale
# generously; a real hang is caught by the timeout either way, just later.
E2E_TIMEOUT_S = 7200


@dataclass
class E2EResult:
    ok: bool
    output: str
    summary: str


def run_e2e(repo: str, stack: Stack, grep: str | None = None,
           consent: str | None = None,
           log_path: pathlib.Path | None = None,
           timeout: int = E2E_TIMEOUT_S) -> E2EResult:
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
    # Consent rides along because generated e2e setups may shell out to
    # `pnpm db:reset` themselves (the oracle's isolation strategy) — same
    # project, same dev DB, same human consent as provision_db/migrate.
    env = {"TZ": "UTC", "NEXT_PUBLIC_WEB_URL": stack.web_url, "NEXT_PUBLIC_API_URL": stack.api_url,
           **_consent_env(consent)}
    if log_path is not None:
        p = livelog.tee_subprocess(cmd, cwd=repo, env=env, timeout=timeout,
                                   check=False, log_path=log_path)
    else:
        p = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, **env})
    out = (p.stdout or "") + (getattr(p, "stderr", "") or "")
    if p.returncode != 0:
        out += "\n\n--- api process output ---\n" + tail("api")
    return E2EResult(p.returncode == 0, out, f"exit={p.returncode}")


def teardown(repo: str, stack: Stack, keep_db: bool = False) -> None:
    stop_stack()
    if not keep_db:
        db_down(repo, stack)
