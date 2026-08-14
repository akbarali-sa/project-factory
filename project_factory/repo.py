"""
Repo lifecycle: mirror the starter, materialise a project repo, commit specs.

REPO MODEL (three repos — see REPO-LAYOUT.md)
    1. project-factory      <- THIS code. Never part of a generated project.
    2. starter template     <- your fork at a pinned ref. Read-only to us.
    3. <workspace>/<slug>/repo/  <- one per project, generated here.

The workspace lives OUTSIDE the factory repo (see config.py for why), so there
are no nested git repos inside the tool and no way to accidentally commit a
client codebase into it.

WHY A LOCAL MIRROR
    We clone the starter ONCE into <factory>/.cache/starter.git, then materialise
    each project from that mirror. Fewer network round-trips, and — more
    importantly — every project in a batch comes from the SAME commit, so runs
    are comparable. Pin `starter_ref` to a tag for real reproducibility; a moving
    branch means two runs a week apart are not the same experiment.
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
from dataclasses import dataclass


def _run(cmd: list[str], cwd: str | None = None, timeout: int = 600,
         check: bool = True) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError(f"$ {' '.join(cmd)}\n{(p.stdout + p.stderr)[-2000:]}")
    return p


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40]


@dataclass
class RepoInfo:
    path: str
    starter_ref: str
    starter_sha: str


def sync_starter_mirror(factory_root: str, starter_url: str,
                        cache_dir: str = ".cache/starter.git") -> str:
    """Clone or fetch a bare mirror of the public starter. Idempotent."""
    mirror = pathlib.Path(factory_root) / cache_dir
    if mirror.exists():
        _run(["git", "remote", "update", "--prune"], cwd=str(mirror), timeout=900)
    else:
        mirror.parent.mkdir(parents=True, exist_ok=True)
        _run(["git", "clone", "--mirror", starter_url, str(mirror)], timeout=1800)
    return str(mirror)


def create_project_repo(factory_root: str, starter_url: str, starter_ref: str,
                        repo_path: str, fresh: bool = True) -> RepoInfo:
    """
    Materialise `repo_path` (i.e. <workspace>/<slug>/repo) from the starter.

    History is squashed to a single initial commit: the generated project is a
    NEW repo, not a fork. It must carry none of the starter's history and stay
    uncoupled from upstream — client work has to be independently versioned, and
    a fork would silently tie a client deliverable to a public repo.
    """
    mirror = sync_starter_mirror(factory_root, starter_url)
    target = pathlib.Path(repo_path)

    if target.exists():
        if not fresh:
            sha = _run(["git", "rev-parse", "HEAD"], cwd=str(target)).stdout.strip()
            return RepoInfo(str(target), starter_ref, sha)
        shutil.rmtree(target)

    target.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "clone", "--branch", starter_ref, "--single-branch",
          mirror, str(target)], timeout=900)

    sha = _run(["git", "rev-parse", "HEAD"], cwd=str(target)).stdout.strip()

    # Detach from the starter: drop history and remotes, start clean.
    shutil.rmtree(target / ".git")
    _run(["git", "init", "-b", "main"], cwd=str(target))
    _run(["git", "add", "-A"], cwd=str(target))
    _run(["git", "-c", "user.name=project-factory",
          "-c", "user.email=project-factory@local",
          "commit", "-m",
          f"chore: scaffold from starter {starter_ref} ({sha[:8]})"],
         cwd=str(target))

    return RepoInfo(str(target), starter_ref, sha)


def install_app_compose(repo: str, factory_root: str) -> None:
    """Copy the app stack compose file into the generated repo if absent."""
    src = pathlib.Path(factory_root) / "docker/docker-compose.app.yml"
    dst = pathlib.Path(repo) / "docker-compose.factory-app.yml"
    if not dst.exists():
        shutil.copy2(src, dst)


def commit_spec_artifacts(repo: str, board_path: str, scenarios_path: str,
                          constitution: str | None = None) -> None:
    """
    Put the spec INTO the generated repo (spec-as-code).

    Why in-repo rather than factory-side: the spec is a project artifact. It
    should be versioned with the code it produced, visible in the PR diff, and
    auditable a year later without needing the factory. This is also the one
    genuinely good idea we take from Spec Kit's layout.
    """
    specs = pathlib.Path(repo) / "specs"
    specs.mkdir(exist_ok=True)
    shutil.copy2(board_path, specs / "board.json")
    shutil.copy2(scenarios_path, specs / pathlib.Path(scenarios_path).name)
    if constitution:
        (specs / "constitution.md").write_text(constitution)

    _run(["git", "add", "-A"], cwd=repo)
    # Tolerate "nothing to commit": a re-run on a reused repo (new thread
    # generation, or run-project retrying a crashed slice) finds the specs
    # already committed by the earlier attempt — same contract as commit().
    p = _run(["git", "-c", "user.name=project-factory", "-c", "user.email=project-factory@local",
              "commit", "-m", "docs(specs): board IR + approved scenarios"],
             cwd=repo, check=False)
    if p.returncode != 0 and "nothing to commit" not in (p.stdout + p.stderr):
        raise RuntimeError(f"spec commit failed:\n{p.stdout}{p.stderr}")


def commit(repo: str, message: str, verify: bool = True) -> None:
    """
    verify=False skips the starter's pre-commit hook. Only the red-first test
    commit may use it: those tests import modules that DO NOT EXIST YET by
    design, so no typecheck hook can ever pass them. Implementation commits
    must keep the hook — green code has no such excuse.
    """
    _run(["git", "add", "-A"], cwd=repo)
    p = _run(["git", "-c", "user.name=project-factory", "-c", "user.email=project-factory@local",
              "commit", "-m", message] + ([] if verify else ["--no-verify"]),
             cwd=repo, check=False)
    if p.returncode != 0 and "nothing to commit" not in (p.stdout + p.stderr):
        raise RuntimeError(f"commit failed:\n{p.stdout}{p.stderr}")


def create_branch(repo: str, branch: str) -> None:
    """Create-or-switch. `switch -c` alone fails when the branch already
    exists (a re-run) and check=False would leave HEAD wherever it was —
    on main after a merge — silently landing slice commits on the wrong
    branch."""
    p = _run(["git", "switch", "-c", branch], cwd=repo, check=False)
    if p.returncode != 0:
        _run(["git", "switch", branch], cwd=repo)


def _exclude_graft(repo: str) -> None:
    """
    Keep the graph out of the client deliverable.

    The index is regenerable build output, not source — the factory gitignores
    its own `graft/` for the same reason. It matters more here: every commit
    helper stages with `git add -A`, so an index left untracked in the worktree
    is swept into the NEXT slice's spec commit and ships inside the client's
    PR diff. Written to .git/info/exclude rather than .gitignore so the
    deliverable's own ignore file stays exactly as the starter shipped it.
    """
    exclude = pathlib.Path(repo) / ".git" / "info" / "exclude"
    if not exclude.parent.is_dir():
        return
    body = exclude.read_text() if exclude.exists() else ""
    if "graft/" in body:
        return
    if body and not body.endswith("\n"):
        body += "\n"
    exclude.write_text(
        body + "# project-factory: regenerable code graph, "
               "never part of the deliverable.\ngraft/\n")


def index_with_graft(repo: str) -> str:
    """
    Build/refresh the generated repo's graft graph.

    Best effort by design — an unindexed repo is a slower debugging session,
    never a failed slice, so this must not be able to break a run. Worth doing
    because the generated repo is where the diagnosis work actually happens:
    "who else uses this helper", "where is this component" are one graft call
    there instead of several greps. `build` is the free, no-key pass.
    """
    if shutil.which("graft") is None:
        return "graft not installed — skipping index"
    _exclude_graft(repo)
    p = _run(["graft", "build", "."], cwd=repo, timeout=600, check=False)
    if p.returncode != 0:
        return f"graft index skipped: {(p.stdout + p.stderr).strip()[-160:]}"
    return "graft index refreshed"


def current_branch(repo: str) -> str:
    return _run(["git", "branch", "--show-current"], cwd=repo).stdout.strip()


def merge_to_main(repo: str, branch: str) -> str:
    """
    Fold an approved slice branch into main (--no-ff so the slice stays a
    visible unit in history) and leave HEAD on main, so the next slice's
    branch starts from delivered code. --no-verify: every commit being merged
    already passed the hooks; the merge commit itself has nothing new to lint.
    """
    _run(["git", "switch", "main"], cwd=repo)
    _run(["git", "-c", "user.name=project-factory",
          "-c", "user.email=project-factory@local",
          "merge", "--no-ff", "--no-verify", branch,
          "-m", f"feat: merge {branch} (Gate C approved)"], cwd=repo)
    return _run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()


def open_draft_pr(repo: str, branch: str, title: str, body: str,
                  remote: str | None = None) -> str:
    """
    Push + open a Draft PR via gh. If no remote is configured we stop at a local
    branch — the factory must work fully offline for the first slice.
    """
    if remote is None:
        return f"(no remote) local branch '{branch}' ready for review"
    if shutil.which("gh") is None:
        return "(gh not installed) local branch ready"
    _run(["git", "remote", "add", "origin", remote], cwd=repo, check=False)
    _run(["git", "push", "-u", "origin", branch], cwd=repo, timeout=900)
    p = _run(["gh", "pr", "create", "--draft", "--title", title,
              "--body", body], cwd=repo, check=False, timeout=300)
    return (p.stdout or p.stderr).strip()
