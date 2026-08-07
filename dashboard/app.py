"""
Project Factory live dashboard — monitoring AND control over real pipeline
state.

Reads directly from the same LangGraph Postgres checkpoint that
`python -m project_factory status <slug>` reads, plus project_factory's own
config/discovery — no separate data store, no fake data. Start/Stop/Resume
and Gate approve/reject (runner.py) shell out to the exact same `run` /
`approve` CLI entry points a human would type — this file never
reimplements LangGraph resume/interrupt semantics, it's a UI over them.
Project creation goes through config.scaffold(), the same code behind
`python -m project_factory new`.

Run (from the factory root — dashboard/ is a package, port fixed at 8420):
    uvicorn dashboard.app:app --port 8420
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import yaml

FACTORY_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(FACTORY_ROOT))

from fastapi import Body, FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import HTMLResponse, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from psycopg import Connection  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from project_factory import config as cfgmod  # noqa: E402
from project_factory import infra  # noqa: E402
from project_factory.graph import build_graph  # noqa: E402

from . import runner  # noqa: E402

# -----------------------------------------------------------------------------
# Long-lived checkpoint connections, one per distinct checkpoint_db_url (in
# practice always the one shared instance, but per-project run.json COULD
# override it). Opening a fresh Postgres connection + rebuilding the graph on
# every poll from every open browser tab is wasteful and, under load, slow
# enough to make the UI feel laggy — so we keep one connection per DB alive
# for the life of the server and guard it with a lock (a psycopg Connection
# is not safe for concurrent use from multiple threads, and FastAPI's sync
# endpoints each run in their own threadpool thread).
# -----------------------------------------------------------------------------
from langgraph.checkpoint.postgres import PostgresSaver  # noqa: E402

_conn_cache_lock = threading.Lock()
_conn_cache: dict[str, tuple[threading.Lock, Any]] = {}


def _graph_for(conn_string: str):
    with _conn_cache_lock:
        entry = _conn_cache.get(conn_string)
        if entry is None:
            conn = Connection.connect(
                conn_string, autocommit=True, prepare_threshold=0, row_factory=dict_row
            )
            saver = PostgresSaver(conn)
            saver.setup()
            gapp = build_graph(saver)
            entry = (threading.Lock(), gapp)
            _conn_cache[conn_string] = entry
        return entry


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    with _conn_cache_lock:
        for _lock, gapp in _conn_cache.values():
            try:
                gapp.checkpointer.conn.close()
            except Exception:  # noqa: BLE001 — best-effort on shutdown
                pass
        _conn_cache.clear()


app = FastAPI(title="Project Factory Dashboard", lifespan=lifespan)
app.mount(
    "/static",
    StaticFiles(directory=pathlib.Path(__file__).resolve().parent / "static"),
    name="static",
)

# -----------------------------------------------------------------------------
# The pipeline's ~20 graph nodes grouped into the phases the README diagram
# uses, each matched against the distinctive log line its node(s) append.
# Grouping (rather than one row per node) keeps the implement/verify/diagnose
# retry loops readable as one phase instead of three near-duplicate rows.
# -----------------------------------------------------------------------------
PHASES = [
    {"id": "spec", "label": "Ingest & gap detection", "markers": [r"^ingest:", r"^gap_detect:"]},
    {"id": "gate_a", "label": "Gate A — spec approval", "markers": [r"^gate_A:"]},
    {"id": "clone", "label": "Clone starter & baseline",
     "markers": [r"^clone:", r"^db:", r"^baseline:", r"specs copied"]},
    {"id": "architect", "label": "Architect & contract lint", "markers": [r"^architect:", r"^contract_lint:"]},
    {"id": "gate_b", "label": "Gate B — contract freeze", "markers": [r"^gate_B:"]},
    {"id": "migrate", "label": "Migrate schema", "markers": [r"^migrate:"]},
    {"id": "tests", "label": "Write tests (keystone)", "markers": [r"^write_tests:"]},
    {"id": "api", "label": "Implement & verify API",
     "markers": [r"^api: attempt", r"^verify_api:", r"^diagnose\[api\]", r"^PARKED api"]},
    {"id": "web", "label": "Implement & verify web",
     "markers": [r"^web: attempt", r"^verify_web:", r"^diagnose\[web\]", r"^PARKED web"]},
    {"id": "e2e", "label": "Launch stack & verify E2E",
     "markers": [r"^launch:", r"^verify_e2e:", r"^diagnose\[e2e\]", r"^PARKED e2e"]},
    {"id": "finish", "label": "Teardown, commit & PR", "markers": [r"stack stopped", r"stack left running", r"^pr:"]},
    {"id": "gate_c", "label": "Gate C — PR review", "markers": [r"^gate_C:"]},
]
_PARKABLE = {"api", "web", "e2e"}
_DIAGNOSABLE = {"api", "web", "e2e"}

# Test runners (vitest/turbo) emit ANSI color codes even into piped,
# non-interactive output; those land verbatim in checkpointed log/diagnosis
# text and render as literal "[39m" noise in a browser. Some summaries are
# also hard-truncated to 80 chars upstream (harness.py), which can slice an
# escape sequence in half — the trailing letter is made optional so those
# dangling fragments get stripped too. Strip at the API boundary only — the
# raw text in Postgres is untouched.
_ANSI_RE = re.compile(r"\x1b\[?[0-9;]*[a-zA-Z]?")


def _clean(s: str) -> str:
    return _ANSI_RE.sub("", s)


def _phase_status(phase: dict, log: list[str], parked: list[str]) -> tuple[str, str]:
    matched = [l for l in log if any(re.search(m, l) for m in phase["markers"])]
    if phase["id"] in _PARKABLE and phase["id"] in parked:
        return "parked", (matched[-1] if matched else "")
    if matched:
        last = matched[-1]
        return ("retrying" if "FAIL" in last else "done"), last
    return "pending", ""


def _port_open(port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.socket() as s:
            s.settimeout(timeout)
            return s.connect_ex(("127.0.0.1", port)) == 0
    except OSError:
        return False


def _repo_info(project) -> dict | None:
    """Where the generated code LIVES — path + current branch — so the
    dashboard can point at the deliverable, not just the pipeline."""
    repo = project.repo_path
    if not repo.is_dir():
        return None
    try:
        branch = subprocess.run(
            ["git", "-C", str(repo), "branch", "--show-current"],
            capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        branch = ""
    return {"path": str(repo), "branch": branch or None}


def _status_payload(slug: str, slice_id: str) -> dict:
    try:
        project = cfgmod.discover(slug)
    except cfgmod.ConfigError as e:
        raise HTTPException(404, str(e)) from e

    conn_string = infra.ensure_state_db(project.cfg["checkpoint_db_url"])
    lock, gapp = _graph_for(conn_string)
    thread = {"configurable": {"thread_id": project.thread_id(slice_id)}}
    with lock:
        snap = gapp.get_state(thread)

    values = snap.values or {}
    log = [_clean(l) for l in (values.get("log") or [])]
    usage = values.get("usage")
    parked = values.get("parked") or []
    attempts = values.get("attempts") or {}
    diagnosis = values.get("diagnosis") or {}
    phase_out = values.get("phase_out") or {}
    next_nodes = list(snap.next or ())

    gate_payload = None
    for t in (snap.tasks or ()):
        for intr in (getattr(t, "interrupts", None) or ()):
            val = getattr(intr, "value", None)
            if isinstance(val, dict):
                gate_payload = val

    phases = []
    for phase in PHASES:
        status, detail = _phase_status(phase, log, parked)
        entry = {"id": phase["id"], "label": phase["label"], "status": status, "detail": detail}
        if phase["id"] in _DIAGNOSABLE:
            entry["attempts"] = attempts.get(phase["id"], 0)
            if diagnosis.get(phase["id"]):
                entry["diagnosis"] = _clean(diagnosis[phase["id"]])[:4000]
            if status in ("retrying", "parked") and phase_out.get(phase["id"]):
                entry["failure_output"] = _clean(phase_out[phase["id"]])[-3000:]
        phases.append(entry)
    done_count = sum(1 for p in phases if p["status"] == "done")

    completed = slice_id in set(project.state.get("completed_slices", []))

    stack = values.get("stack")
    stack_payload = None
    if stack is not None:
        try:
            stack_payload = {
                "api_url": stack.api_url, "web_url": stack.web_url,
                "api_up": _port_open(stack.api_port), "web_up": _port_open(stack.web_port),
                "db_name": stack.db_name,
            }
        except Exception:  # noqa: BLE001 — deserialized shape drift shouldn't 500 the page
            stack_payload = None
    if stack_payload is not None:
        # The MODULES this slice generated, as things a human can click:
        # every screen the approved web scenarios name (the oracle is the
        # source of truth for what UI exists), plus the Nest Swagger UI.
        stack_payload["docs_url"] = f"{stack_payload['api_url']}/api/docs"
        slice_obj = next((s for s in project.slices if s.id == slice_id), None)
        sc_data = slice_obj.data if slice_obj else {}
        screens, seen = [], set()
        for sc in sc_data.get("web_scenarios") or []:
            path = (sc.get("screen") or "").strip()
            if path and path not in seen:
                seen.add(path)
                screens.append({"path": path,
                                "url": stack_payload["web_url"].rstrip("/") + path})
        stack_payload["screens"] = screens
        users = (sc_data.get("starter_constraints") or {}).get("seeded_users") or []
        stack_payload["sign_in_as"] = users[0] if users else None

    process_state = runner.get_state(str(project.dir), slice_id)

    if completed:
        status_label = "done"
    elif process_state["running"]:
        # A just-clicked Start/Approve may not have written its first
        # checkpoint yet — `next`/`log` alone would still read "idle" for
        # that brief window, which would make the button flicker.
        status_label = "running"
    elif parked:
        status_label = "parked"
    elif gate_payload:
        status_label = "gate"
    elif not next_nodes and not log:
        status_label = "idle"
    elif not next_nodes:
        status_label = "done" if done_count == len(phases) else "idle"
    elif next_nodes:
        # Steps are queued (e.g. stopped by --until, or Stop was clicked) but
        # OUR tracking sees no process executing right now — distinct from
        # "running" so the control panel offers Run, not just a spinner.
        # Caveat: a run started before this pidfile mechanism existed (or by
        # some other untracked means) is invisible to us and would also land
        # here even though it's genuinely still executing — tracking can only
        # see what it was told about.
        status_label = "paused"
    else:
        status_label = "running"

    return {
        "project": slug,
        "slice": slice_id,
        "status_label": status_label,
        "next": next_nodes,
        "parked": parked,
        "completed": completed,
        "process": process_state,
        "cost_usd": round(getattr(usage, "cost_usd", 0.0) or 0.0, 3) if usage else 0.0,
        "cost_by_agent": {k: round(v, 3) for k, v in (getattr(usage, "by_agent", None) or {}).items()},
        "budget_usd": project.cfg.get("budget_usd", 25.0),
        "progress_pct": round(done_count / len(phases) * 100),
        "phases": phases,
        "gate": gate_payload,
        "stack": stack_payload,
        "repo": _repo_info(project),
        "log_tail": log[-15:],
        "log_count": len(log),
        "idle": not next_nodes,
    }


def _timeline_payload(slug: str, slice_id: str) -> dict:
    try:
        project = cfgmod.discover(slug)
    except cfgmod.ConfigError as e:
        raise HTTPException(404, str(e)) from e

    conn_string = infra.ensure_state_db(project.cfg["checkpoint_db_url"])
    lock, gapp = _graph_for(conn_string)
    thread = {"configurable": {"thread_id": project.thread_id(slice_id)}}
    with lock:
        hist = list(gapp.get_state_history(thread))
    hist.reverse()  # oldest -> newest

    events: list[dict] = []
    prev_len = 0
    for snap in hist:
        log = (snap.values or {}).get("log") or []
        for line in log[prev_len:]:
            events.append({"ts": snap.created_at, "line": _clean(line)})
        prev_len = len(log)

    started_at = hist[0].created_at if hist else None
    last_at = hist[-1].created_at if hist else None
    elapsed_s = None
    if started_at and last_at:
        try:
            elapsed_s = (
                datetime.fromisoformat(last_at) - datetime.fromisoformat(started_at)
            ).total_seconds()
        except ValueError:
            elapsed_s = None

    return {
        "started_at": started_at,
        "last_update_at": last_at,
        "elapsed_s": elapsed_s,
        "events": events,
    }


@app.get("/api/projects")
def list_projects():
    out = []
    for slug in cfgmod.list_projects():
        try:
            project = cfgmod.discover(slug)
        except cfgmod.ConfigError:
            continue
        done = set(project.state.get("completed_slices", []))
        out.append({
            "slug": slug,
            "slices": [
                {"id": s.id, "name": s.name, "wave": s.wave, "completed": s.id in done}
                for s in sorted(project.slices, key=lambda s: (s.wave, s.path.name))
            ],
        })
    return out


# -----------------------------------------------------------------------------
# Project creation + spec authoring — the CLI-free path from "I have a board"
# to "Run". Creation goes through config.scaffold() (the code behind
# `python -m project_factory new`), so the dashboard can never produce a
# layout the CLI wouldn't. Spec editing exists because the scaffolded
# scenarios file is a TEMPLATE — the human must author the oracle before the
# first run, and 'no CLI' means giving them an editor here.
# -----------------------------------------------------------------------------
def _validate_board(obj: Any) -> dict:
    if not isinstance(obj, dict):
        raise HTTPException(400, "board must be a JSON object")
    if not obj.get("name"):
        raise HTTPException(400, "board is missing 'name'")
    if not isinstance(obj.get("business_events"), list) or not obj["business_events"]:
        raise HTTPException(400, "board needs a non-empty 'business_events' list")
    return obj


def _load_board_from_body(body: dict) -> tuple[dict | None, str | None]:
    """(board_obj, preferred_filename) from board_path OR board_json in the
    request — or (None, None) when neither was provided (allowed only when
    reusing a project that already has a board)."""
    board_path = ((body or {}).get("board_path") or "").strip() or None
    board_json = (body or {}).get("board_json") or None
    if board_path:
        src = pathlib.Path(board_path).expanduser()
        if not src.is_file():
            raise HTTPException(400, f"board file not found: {src}")
        try:
            return _validate_board(json.loads(src.read_text())), src.name
        except json.JSONDecodeError as e:
            raise HTTPException(400, f"board file is not valid JSON: {e}") from e
    if board_json:
        if isinstance(board_json, str):
            try:
                board_obj = json.loads(board_json)
            except json.JSONDecodeError as e:
                raise HTTPException(400, f"pasted board is not valid JSON: {e}") from e
        else:
            board_obj = board_json
        # The browser's file picker sends the original filename alongside the
        # content (basename only — browsers never reveal the real path).
        name = pathlib.Path((body or {}).get("board_filename") or "").name or None
        return _validate_board(board_obj), name
    return None, None


def _board_slice_candidates(board: dict) -> list[dict]:
    """
    Slice-shaped units derived from the board: its in-scope bounded contexts
    (the same 'Out-of-Scope' filter ingest() applies), waved in the board's
    own flow order so the ingestion slice naturally lands before the ones
    that consume its data.
    """
    flow_order = {f.get("name"): i for i, f in enumerate(board.get("flows", []))}
    groups: dict[str, list[dict]] = {}
    for e in board.get("business_events", []):
        bc = e.get("bounded_context") or ""
        if not bc or "Out-of-Scope" in bc:
            continue
        groups.setdefault(bc, []).append({"id": e["id"], "name": e["name"]})
    ordered = sorted(groups.items(), key=lambda kv: flow_order.get(kv[0], 99))
    return [
        {
            "bounded_context": bc,
            "name": bc,
            "wave": wave,
            "file_stem": cfgmod.slugify(bc),
            "slice_id": cfgmod.slugify(bc).replace("-", "_"),
            "events": events,
        }
        for wave, (bc, events) in enumerate(ordered, start=1)
    ]


@app.get("/api/workspace")
def workspace_info():
    ws = cfgmod.resolve_workspace()
    return {"workspace": str(ws), "exists": ws.is_dir()}


@app.post("/api/board/slices")
def board_slices(body: dict = Body(...)):
    """Inspect a board (path or pasted/picked JSON) BEFORE creating anything:
    returns the slice candidates the create dialog offers as checkboxes."""
    board, _ = _load_board_from_body(body)
    if board is None:
        raise HTTPException(400, "provide board_path or board_json")
    return {"board_name": board.get("name"), "candidates": _board_slice_candidates(board)}


@app.post("/api/projects")
def create_project(body: dict = Body(...)):
    """
    Create OR reuse: an existing project directory is not an error — the
    scaffold only fills in what's missing (new seeded slice files land next
    to existing ones, an existing board is kept). This is what lets 'start a
    project' be an idempotent dashboard action rather than a one-shot.
    """
    slug = cfgmod.slugify((body or {}).get("slug") or "")
    if not slug:
        raise HTTPException(400, "slug is required (letters/numbers/dashes)")
    ws = cfgmod.resolve_workspace()
    pdir = ws / slug
    existed = pdir.is_dir()
    specs = pdir / "specs"
    has_board = existed and specs.is_dir() and bool(list(specs.glob("*.board.json")))

    board, board_filename = _load_board_from_body(body)
    if board is None and not has_board:
        raise HTTPException(400, "provide board_path (a .board.json file) or board_json — "
                                 "this project has no board yet")

    # Which slices to scaffold: the client sends the bounded-context names it
    # selected; we re-derive the candidates server-side from the board rather
    # than trusting client-sent event lists. No selection sent -> all of them.
    selected_names = (body or {}).get("slices")
    candidates = _board_slice_candidates(board) if board else []
    if selected_names is not None:
        if not isinstance(selected_names, list) or not selected_names:
            raise HTTPException(400, "'slices' must be a non-empty list of bounded-context names")
        wanted = set(selected_names)
        unknown = wanted - {c["bounded_context"] for c in candidates}
        if unknown:
            raise HTTPException(400, f"unknown slice(s): {sorted(unknown)}")
        candidates = [c for c in candidates if c["bounded_context"] in wanted]

    specs.mkdir(parents=True, exist_ok=True)
    seeded = []
    for c in candidates:
        f = specs / f"{c['file_stem']}.scenarios.yaml"
        if not f.exists():
            f.write_text(cfgmod.seeded_scenarios_yaml(
                c["slice_id"], c["name"], c["wave"], c["bounded_context"], c["events"]))
            seeded.append(f.name)

    # scaffold() fills in the rest (board copy, run.json, a fallback template
    # slice when the board yielded no candidates). An existing board is kept:
    # passing a second one would leave two *.board.json files and brick
    # discover(), so the provided board is ignored in that case.
    fallback_slice = ((body or {}).get("slice_name") or "slice-001").strip()
    first_slice = candidates[0]["file_stem"] if candidates else fallback_slice
    if board is not None and not has_board:
        name = board_filename if (board_filename or "").endswith(".board.json") \
            else f"{slug}.board.json"
        with tempfile.TemporaryDirectory() as td:
            tmp = pathlib.Path(td) / name
            tmp.write_text(json.dumps(board, indent=2))
            project = cfgmod.scaffold(slug, str(tmp), slice_name=first_slice)
    else:
        project = cfgmod.scaffold(slug, None, slice_name=first_slice)

    # Consent for destructive DB resets — written ONLY when the human ticked
    # the dialog's explicit checkbox (unchecked by default; the stored text
    # matches the label they agreed to). Never invented or defaulted here.
    consent_written = False
    if (body or {}).get("db_reset_consent") is True:
        consent_written = cfgmod.record_db_reset_consent(project)

    return {
        "slug": slug,
        "created": not existed,
        "dir": str(project.dir),
        "board": project.board_path.name,
        "board_kept_existing": bool(board is not None and has_board),
        "seeded_files": seeded,
        "db_reset_consent_written": consent_written,
        "slices": [{"id": s.id, "name": s.name, "wave": s.wave, "file": s.path.name}
                   for s in project.slices],
    }


def _project_or_404(slug: str) -> cfgmod.Project:
    try:
        return cfgmod.discover(slug)
    except cfgmod.ConfigError as e:
        raise HTTPException(404, str(e)) from e


def _spec_path(project: cfgmod.Project, name: str) -> pathlib.Path:
    specs = (project.dir / "specs").resolve()
    p = (specs / name).resolve()
    if p.parent != specs or p.suffix not in (".yaml", ".yml", ".json"):
        raise HTTPException(400, "invalid spec filename")
    return p


@app.get("/api/projects/{slug}/specs")
def list_specs(slug: str):
    project = _project_or_404(slug)
    return [{"name": f.name, "size": f.stat().st_size}
            for f in sorted((project.dir / "specs").iterdir())
            if f.is_file() and f.suffix in (".yaml", ".yml", ".json")]


@app.get("/api/projects/{slug}/specs/{name}")
def get_spec(slug: str, name: str):
    project = _project_or_404(slug)
    p = _spec_path(project, name)
    if not p.is_file():
        raise HTTPException(404, f"no spec file '{name}'")
    return {"name": name, "content": p.read_text()}


@app.put("/api/projects/{slug}/specs/{name}")
def put_spec(slug: str, name: str, body: dict = Body(...)):
    """
    Save an edited spec file — validating that the result would still be
    loadable by config.discover(), so a typo can't silently brick the
    project's next run with a parse error deep in ingest.
    """
    project = _project_or_404(slug)
    p = _spec_path(project, name)
    content = (body or {}).get("content")
    if not isinstance(content, str):
        raise HTTPException(400, "body must include 'content' (string)")

    if p.suffix in (".yaml", ".yml"):
        try:
            data = yaml.safe_load(content)
        except yaml.YAMLError as e:
            raise HTTPException(400, f"not valid YAML: {e}") from e
        if name.endswith(".scenarios.yaml") or name.endswith(".scenarios.yml"):
            if not isinstance(data, dict) or "slice" not in data or "id" not in data.get("slice", {}):
                raise HTTPException(400, "scenarios file needs top-level 'slice:' with an 'id'")
    else:
        try:
            data = json.loads(content)
        except json.JSONDecodeError as e:
            raise HTTPException(400, f"not valid JSON: {e}") from e
        if name.endswith(".board.json"):
            _validate_board(data)

    p.write_text(content)
    return {"saved": name, "bytes": len(content.encode())}


@app.get("/api/overview")
def overview():
    """
    Lightweight fleet-wide view for the grid: every project × every slice,
    with a status snapshot each — enough to render the overview without
    forcing the user to pick a project first. Errors are per-slice so one
    misconfigured project can't blank the whole page.
    """
    out = []
    for slug in cfgmod.list_projects():
        try:
            project = cfgmod.discover(slug)
        except cfgmod.ConfigError as e:
            out.append({"slug": slug, "error": str(e).splitlines()[0], "slices": []})
            continue
        slices = []
        for s in sorted(project.slices, key=lambda s: (s.wave, s.path.name)):
            try:
                d = _status_payload(slug, s.id)
                slices.append({
                    "id": s.id, "name": s.name, "wave": s.wave,
                    "status_label": d["status_label"], "progress_pct": d["progress_pct"],
                    "cost_usd": d["cost_usd"], "budget_usd": d["budget_usd"],
                    "gate": d["gate"]["gate"] if d["gate"] else None,
                    "parked": d["parked"], "next": d["next"],
                    "detail": d["log_tail"][-1] if d["log_tail"] else "not started",
                    "stack": d["stack"],
                })
            except HTTPException:
                slices.append({"id": s.id, "name": s.name, "wave": s.wave,
                               "status_label": "idle", "progress_pct": 0,
                               "cost_usd": 0.0, "budget_usd": project.cfg.get("budget_usd", 25.0),
                               "gate": None, "parked": [], "next": [], "detail": "not started",
                               "stack": None})
        out.append({"slug": slug, "error": None, "slices": slices})
    return out


@app.get("/api/projects/{slug}/slices/{slice_id}/status")
def slice_status(slug: str, slice_id: str):
    return _status_payload(slug, slice_id)


@app.get("/api/meta")
def meta():
    """Ladder step metadata for the "run until" selector — sourced from
    config.py so the dropdown can never drift from what --until accepts."""
    return {"ladder": cfgmod.LADDER, "node_order": cfgmod.NODE_ORDER}


@app.post("/api/projects/{slug}/slices/{slice_id}/run")
def api_start_run(slug: str, slice_id: str, body: dict = Body(default={})):
    """Start or Resume — both are just `python -m project_factory run`;
    LangGraph's checkpoint makes resuming from wherever it last stopped
    automatic, so there's no separate "resume" code path to build."""
    project = _project_or_404(slug)
    until = (body or {}).get("until") or None
    if until and until not in cfgmod.NODE_ORDER:
        raise HTTPException(400, f"unknown --until node '{until}'")
    try:
        return runner.start_run(str(FACTORY_ROOT), str(project.dir), slug, slice_id, until=until)
    except runner.RunnerError as e:
        raise HTTPException(409, str(e)) from e


@app.post("/api/projects/{slug}/slices/{slice_id}/stop")
def api_stop_run(slug: str, slice_id: str):
    project = _project_or_404(slug)
    return runner.stop(str(project.dir), slice_id)


@app.post("/api/projects/{slug}/slices/{slice_id}/gate")
def api_gate_decision(slug: str, slice_id: str, body: dict = Body(...)):
    """Approve or reject the pending gate. Like `run`, this can resume
    execution for a long time (through however many nodes until the next
    gate/park/END) — it's launched as a background process, not run
    synchronously in the request."""
    project = _project_or_404(slug)
    action = (body or {}).get("action")
    if action not in ("approve", "reject"):
        raise HTTPException(400, "action must be 'approve' or 'reject'")
    try:
        return runner.start_approve(
            str(FACTORY_ROOT), str(project.dir), slug, slice_id,
            reject=(action == "reject"),
            note=(body or {}).get("note") or "",
            by=(body or {}).get("by") or "",
        )
    except runner.RunnerError as e:
        raise HTTPException(409, str(e)) from e


@app.get("/api/projects/{slug}/slices/{slice_id}/timeline")
def slice_timeline(slug: str, slice_id: str):
    return _timeline_payload(slug, slice_id)


@app.get("/api/projects/{slug}/slices/{slice_id}/live")
async def live_tail(request: Request, slug: str, slice_id: str):
    """
    Tails project_factory/livelog.py's per-slice live-activity file — the
    ONLY place tool calls, reasoning, and raw pnpm/docker output are visible
    as they happen, since LangGraph's checkpoint (everything else in this
    API) only updates once a node returns. This is genuinely `tail -f`, not
    a re-poll of the checkpoint: a fresh backlog replay on connect, then new
    lines as the file grows. A shrinking file (livelog.reset() truncates it
    at the start of a new run) restarts the tail from the top.
    """
    try:
        project = cfgmod.discover(slug)
    except cfgmod.ConfigError as e:
        raise HTTPException(404, str(e)) from e
    log_path = project.dir / ".factory" / "live" / f"{slice_id}.log"

    async def gen():
        pos = 0

        def read_new() -> list[str]:
            nonlocal pos
            if not log_path.exists():
                return []
            data = log_path.read_bytes()
            if len(data) < pos:  # truncated — a fresh run started
                pos = 0
            new = data[pos:]
            pos = len(data)
            text = new.decode("utf-8", errors="replace")
            return [l for l in text.split("\n") if l.strip()]

        for line in await asyncio.to_thread(read_new):
            yield f"data: {json.dumps(_clean(line))}\n\n"
        while True:
            if await request.is_disconnected():
                break
            lines = await asyncio.to_thread(read_new)
            if lines:
                for line in lines:
                    yield f"data: {json.dumps(_clean(line))}\n\n"
            else:
                yield ": heartbeat\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@app.get("/api/projects/{slug}/slices/{slice_id}/stream")
async def slice_stream(request: Request, slug: str, slice_id: str):
    """
    Server-sent events: the server does the polling (against the reused
    connection above) and only pushes a frame when something actually
    changed, so N open dashboard tabs don't turn into N independent client
    poll loops hammering Postgres in lockstep.
    """
    async def gen():
        last = None
        while True:
            if await request.is_disconnected():
                break
            try:
                data = await asyncio.to_thread(_status_payload, slug, slice_id)
                payload = json.dumps(data)
            except HTTPException as e:
                yield f"event: error\ndata: {json.dumps({'error': e.detail})}\n\n"
                await asyncio.sleep(3)
                continue
            if payload != last:
                yield f"data: {payload}\n\n"
                last = payload
            else:
                yield ": heartbeat\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


# -----------------------------------------------------------------------------
# Health — is the factory's own toolchain actually usable right now? Cached
# briefly since `docker info` is a real subprocess call and this is polled.
# -----------------------------------------------------------------------------
_health_cache: dict[str, Any] = {"at": 0.0, "data": None}
_HEALTH_TTL_S = 8.0


def _check_docker() -> tuple[bool, str]:
    if shutil.which("docker") is None:
        return False, "not on PATH"
    try:
        p = subprocess.run(["docker", "info"], capture_output=True, text=True, timeout=3)
        return (p.returncode == 0), ("daemon running" if p.returncode == 0 else "daemon not running")
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def _check_version(cmd: str) -> tuple[bool, str]:
    path = shutil.which(cmd)
    if not path:
        return False, "not on PATH"
    try:
        p = subprocess.run([cmd, "--version"], capture_output=True, text=True, timeout=3)
        return True, (p.stdout or p.stderr).strip()
    except Exception as e:  # noqa: BLE001
        return False, str(e)


@app.get("/api/health")
def health():
    now = time.monotonic()
    if _health_cache["data"] is not None and now - _health_cache["at"] < _HEALTH_TTL_S:
        return _health_cache["data"]

    cfg = {**cfgmod.BUILT_IN_DEFAULTS, **cfgmod._load_defaults()}
    pg_ok = infra.postgres_reachable(cfg["db_host"], cfg["db_port"])
    docker_ok, docker_detail = _check_docker()
    claude_path = shutil.which("claude")
    node_ok, node_detail = _check_version("node")
    pnpm_ok, pnpm_detail = _check_version("pnpm")

    checks = [
        {"id": "postgres", "label": "Postgres", "ok": pg_ok,
         "detail": f"{cfg['db_host']}:{cfg['db_port']}" if pg_ok else "unreachable — one shared instance for every local project"},
        {"id": "docker", "label": "Docker", "ok": docker_ok, "detail": docker_detail},
        {"id": "claude", "label": "Claude CLI", "ok": claude_path is not None,
         "detail": "on PATH" if claude_path else "run: claude setup-token"},
        {"id": "node", "label": "Node", "ok": node_ok, "detail": node_detail},
        {"id": "pnpm", "label": "pnpm", "ok": pnpm_ok, "detail": pnpm_detail},
    ]
    payload = {"checks": checks, "ok": all(c["ok"] for c in checks),
               "checked_at": datetime.now(timezone.utc).isoformat()}
    _health_cache["at"], _health_cache["data"] = now, payload
    return payload


@app.get("/")
async def home():
    html = (pathlib.Path(__file__).resolve().parent / "static" / "index.html").read_text()
    return HTMLResponse(html)
