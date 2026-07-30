"""
Workspace resolution, project discovery, layered config.

THE LAYOUT (workspace lives OUTSIDE the factory, deliberately)

    ~/dev/
    ├── project-factory/            the tool — its own git repo
    │   ├── project_factory/        this package
    │   ├── docker/                 checkpoint DB compose
    │   └── defaults.json           true for EVERY project
    └── projects/                   sibling; never inside the factory
        └── barcode-mvp/
            ├── run.json            per-project OVERRIDES only
            ├── specs/
            │   ├── *.board.json    presales IR
            │   └── *.scenarios.yaml one file per slice
            ├── .factory/state.json completed slices (we write this)
            └── repo/               GENERATED: git init, no starter history

Why the workspace is not nested inside the factory:
  * nested git repos confuse IDEs, ripgrep, and coding agents that walk up the
    tree to find the repo root — an agent can end up resolving the WRONG repo
  * one `git add -f` away from committing a client codebase into your tool
  * client code may carry retention/encryption obligations the tool does not
  * anything run at factory root would crawl generated node_modules
  * in CI the workspace wants to be a mounted volume, not repo-internal

Why `repo/` nests inside the project dir rather than being the project dir:
  inputs (board, scenarios, run.json) must exist BEFORE the repo is created.
  Separating human-owned inputs from factory-owned output removes that
  ordering problem entirely.

CONFIG PRECEDENCE (highest wins)
    CLI flag  >  projects/<slug>/run.json  >  defaults.json  >  built-in
"""

from __future__ import annotations

import json
import os
import pathlib
import re
from dataclasses import dataclass, field
from typing import Any

import yaml

ENV_WORKSPACE = "PROJECT_FACTORY_WORKSPACE"

# -----------------------------------------------------------------------------
# Graph node metadata.
#
# Deliberately here rather than in graph.py: graph.py imports langgraph at module
# level, and `run --until <node>` / `--dry-run` must work on a checkout that only
# has pyyaml. graph.py re-exports these.
# -----------------------------------------------------------------------------
NODE_ORDER = [
    "ingest", "gap_detect", "gate_spec",
    "clone_starter", "provision_db", "baseline", "commit_specs",
    "architect", "contract_lint", "gate_contract", "migrate",
    "write_tests",
    "implement_api", "verify_api",
    "implement_web", "verify_web",
    "launch_stack", "verify_e2e",
    "teardown", "finish", "gate_pr",
]

# Rungs of the cheap-first ladder, with what each one buys you.
LADDER = {
    "gap_detect": "IR parses, gaps found (~free, Haiku)",
    "baseline": "starter clones at pinned ref and builds green ($0)",
    "provision_db": "postgres up, migrated, template users seeded ($0)",
    "migrate": "contract validated and schema applied (~$1)",
    "write_tests": "THE KEYSTONE: tests exist and fail red (~$2)",
    "verify_api": "backend slice green",
    "verify_e2e": "full stack green end to end",
}

BUILT_IN_DEFAULTS: dict[str, Any] = {
    "starter_url": "https://github.com/akbarali-sa/turborepo-starter-kit.git",
    "starter_ref": "starter-minimal",
    "fresh_clone": True,
    "api_port": 3001,
    "web_port": 3000,
    "budget_usd": 25.0,
    "keep_stack_running": True,
    "keep_db": True,
    "jwt_secret": "project-factory-local-dev-secret-not-for-production",
    "checkpoint_db_url":
        "postgresql://root:123456@localhost:5433/project_factory_state",
    "git_remote": None,
}


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40]


# -----------------------------------------------------------------------------
# Workspace resolution
# -----------------------------------------------------------------------------
def factory_root() -> pathlib.Path:
    """Directory containing the project_factory package (i.e. the repo root)."""
    return pathlib.Path(__file__).resolve().parent.parent


def resolve_workspace(explicit: str | None = None) -> pathlib.Path:
    """
    CLI flag > $PROJECT_FACTORY_WORKSPACE > defaults.json > ../projects

    Sibling-by-default means zero config for the common case while staying
    overridable for CI (mounted volume) or a shared team location.
    """
    if explicit:
        return pathlib.Path(explicit).expanduser().resolve()
    if env := os.environ.get(ENV_WORKSPACE):
        return pathlib.Path(env).expanduser().resolve()
    d = _load_defaults()
    if d.get("workspace_root"):
        return pathlib.Path(d["workspace_root"]).expanduser().resolve()
    return (factory_root().parent / "projects").resolve()


def _load_defaults() -> dict:
    p = factory_root() / "defaults.json"
    if not p.exists():
        return {}
    raw = json.loads(p.read_text())
    return {k: v for k, v in raw.items() if not k.startswith("_")}


# -----------------------------------------------------------------------------
# Discovered project
# -----------------------------------------------------------------------------
@dataclass
class Slice:
    path: pathlib.Path
    data: dict

    @property
    def id(self) -> str:
        return self.data["slice"]["id"]

    @property
    def wave(self) -> int:
        return int(self.data["slice"].get("wave", 99))

    @property
    def name(self) -> str:
        return self.data["slice"].get("name", self.id)


@dataclass
class Project:
    slug: str
    dir: pathlib.Path
    board_path: pathlib.Path
    slices: list[Slice]
    cfg: dict
    state: dict = field(default_factory=dict)

    @property
    def repo_path(self) -> pathlib.Path:
        return self.dir / "repo"

    @property
    def state_file(self) -> pathlib.Path:
        return self.dir / ".factory" / "state.json"

    def thread_id(self, slice_id: str) -> str:
        """
        Derived, never hand-written. Same project + slice = same thread, so a
        killed run RESUMES automatically instead of depending on you
        remembering to reuse an id.
        """
        return f"{self.slug}:{slice_id}"

    def next_slice(self) -> Slice | None:
        """First slice not marked completed, in wave order."""
        done = set(self.state.get("completed_slices", []))
        for s in self.slices:
            if s.id not in done:
                return s
        return None

    def pick_slice(self, wanted: str | None) -> Slice:
        if wanted:
            for s in self.slices:
                if wanted in (s.id, s.path.stem):
                    return s
            raise ConfigError(
                f"no slice matching '{wanted}'. Available: "
                + ", ".join(f"{s.id} (wave {s.wave})" for s in self.slices))
        nxt = self.next_slice()
        if nxt is None:
            raise ConfigError(
                f"all {len(self.slices)} slice(s) already completed for "
                f"'{self.slug}'. Use --slice <id> to re-run one.")
        return nxt

    def mark_completed(self, slice_id: str) -> None:
        done = list(self.state.get("completed_slices", []))
        if slice_id not in done:
            done.append(slice_id)
        self.state["completed_slices"] = done
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(self.state, indent=2) + "\n")


class ConfigError(RuntimeError):
    """Actionable configuration/discovery failure."""


# -----------------------------------------------------------------------------
# Discovery — convention over configuration
# -----------------------------------------------------------------------------
def discover(slug: str, workspace: str | None = None) -> Project:
    """
    Everything is inferred from the project directory layout, so run.json holds
    only genuine overrides. Failures are loud and name the fix.
    """
    ws = resolve_workspace(workspace)
    pdir = ws / slug
    if not pdir.is_dir():
        existing = sorted(p.name for p in ws.glob("*") if p.is_dir()) if ws.is_dir() else []
        raise ConfigError(
            f"no project '{slug}' in {ws}\n"
            f"  existing: {', '.join(existing) or '(none)'}\n"
            f"  create it: python -m project_factory new {slug} --board <file>")

    specs = pdir / "specs"
    if not specs.is_dir():
        raise ConfigError(f"{pdir} has no specs/ directory")

    boards = sorted(specs.glob("*.board.json")) or sorted(specs.glob("*board*.json"))
    if not boards:
        raise ConfigError(f"no *.board.json in {specs}")
    if len(boards) > 1:
        raise ConfigError(
            f"{len(boards)} board files in {specs} — exactly one expected:\n  "
            + "\n  ".join(b.name for b in boards))

    slice_files = sorted(specs.glob("*.scenarios.y*ml"))
    if not slice_files:
        raise ConfigError(
            f"no *.scenarios.yaml in {specs}\n"
            f"  a slice file must define slice.id and at least one scenario")

    slices: list[Slice] = []
    for f in slice_files:
        data = yaml.safe_load(f.read_text())
        if not isinstance(data, dict) or "slice" not in data:
            raise ConfigError(f"{f.name}: missing top-level 'slice:' key")
        if "id" not in data["slice"]:
            raise ConfigError(f"{f.name}: slice.id is required")
        slices.append(Slice(f, data))
    # wave order, then filename — deterministic, so runs are comparable
    slices.sort(key=lambda s: (s.wave, s.path.name))

    ids = [s.id for s in slices]
    if len(ids) != len(set(ids)):
        raise ConfigError(f"duplicate slice ids in {specs}: {ids}")

    run_file = pdir / "run.json"
    overrides = {}
    if run_file.exists():
        overrides = {k: v for k, v in json.loads(run_file.read_text()).items()
                     if not k.startswith("_")}

    cfg = {**BUILT_IN_DEFAULTS, **_load_defaults(), **overrides}
    cfg["project_slug"] = slug

    state = {}
    sf = pdir / ".factory" / "state.json"
    if sf.exists():
        state = json.loads(sf.read_text())

    return Project(slug=slug, dir=pdir, board_path=boards[0],
                   slices=slices, cfg=cfg, state=state)


def list_projects(workspace: str | None = None) -> list[str]:
    ws = resolve_workspace(workspace)
    if not ws.is_dir():
        return []
    return sorted(p.name for p in ws.iterdir()
                  if p.is_dir() and (p / "specs").is_dir())


# -----------------------------------------------------------------------------
# Scaffolding — enforce the convention instead of documenting it
# -----------------------------------------------------------------------------
SCENARIOS_TEMPLATE = """\
# Oracle artifact — human-authored, approved at Gate A.
# The Test Author converts these into executable tests. The Implementer may
# never edit this file or the tests derived from it.

slice:
  id: {slice_id}
  name: Describe this vertical slice
  wave: 1
  bounded_context: TODO
  approved_by: null
  approved_at: null

aggregates: []

scenarios:
  - id: SC-001
    title: TODO — one behaviour per scenario
    traces_to: []
    given:
      - TODO
    when: TODO
    then:
      - TODO

web_scenarios: []
e2e_scenarios: []
provisional_scenarios: []
out_of_scope: []
"""


def scaffold(slug: str, board: str | None, workspace: str | None = None,
             slice_name: str | None = None) -> Project:
    ws = resolve_workspace(workspace)
    pdir = ws / slug
    (pdir / "specs").mkdir(parents=True, exist_ok=True)

    if board:
        src = pathlib.Path(board).expanduser().resolve()
        if not src.exists():
            raise ConfigError(f"board not found: {src}")
        name = src.name if src.name.endswith(".board.json") else f"{slug}.board.json"
        (pdir / "specs" / name).write_bytes(src.read_bytes())

    sid = slugify(slice_name or "slice-001").replace("-", "_")
    sfile = pdir / "specs" / f"{slugify(slice_name or 'slice-001')}.scenarios.yaml"
    if not sfile.exists():
        sfile.write_text(SCENARIOS_TEMPLATE.format(slice_id=sid))

    run_file = pdir / "run.json"
    if not run_file.exists():
        run_file.write_text(json.dumps({
            "_comment": ("Per-project OVERRIDES only. Everything else comes from "
                         "the factory's defaults.json. Paths are discovered from "
                         "specs/ — do not list them here."),
            "budget_usd": BUILT_IN_DEFAULTS["budget_usd"],
        }, indent=2) + "\n")

    return discover(slug, workspace)


# -----------------------------------------------------------------------------
# Resolve-and-print: never spend tokens on a misresolved run
# -----------------------------------------------------------------------------
def describe(project: Project, chosen: Slice) -> str:
    done = set(project.state.get("completed_slices", []))
    lines = [
        f"workspace   {project.dir.parent}",
        f"project     {project.slug}  ({project.dir})",
        f"board       {project.board_path.name}",
        f"repo        {project.repo_path}"
        f"{'  (exists)' if project.repo_path.exists() else '  (will be created)'}",
        f"starter     {project.cfg['starter_url']} @ {project.cfg['starter_ref']}",
        f"budget      ${project.cfg['budget_usd']}",
        f"thread_id   {project.thread_id(chosen.id)}",
        f"slices      {len(project.slices)} found:",
    ]
    for s in project.slices:
        mark = "DONE" if s.id in done else ("->  " if s.id == chosen.id else "    ")
        n_api = len(s.data.get("scenarios", []))
        n_web = len(s.data.get("web_scenarios", []))
        n_e2e = len(s.data.get("e2e_scenarios", []))
        lines.append(f"              {mark} w{s.wave} {s.id} "
                     f"({n_api} api / {n_web} web / {n_e2e} e2e)")
    return "\n".join(lines)
