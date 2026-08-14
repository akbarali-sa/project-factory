"""
The generated repo's code graph: when it is built, and why it must never
reach the client.

Both cases below are the same latent defect seen from two ends. The graph
was only built at `finish`, the last node of a slice — so for the whole of
slice 1 the _GRAFT_NUDGE injected into the implementer and the diagnosticians
pointed at a `graft/` directory that did not exist, and the agents fell back
to reading whole starter sources: exactly the cost the nudge exists to avoid.
Fixing that by indexing right after the clone then exposes the second end —
every commit helper stages with `git add -A`, so an unignored index in the
worktree ships inside the client's PR diff.
"""

from __future__ import annotations

import pathlib
import subprocess
import tempfile
import unittest

from project_factory import repo as repo_mod


def _git_repo(td: str) -> str:
    subprocess.run(["git", "init", "-b", "main"], cwd=td, capture_output=True, check=True)
    return td


class ExcludeGraft(unittest.TestCase):
    def test_writes_exclude_entry(self):
        with tempfile.TemporaryDirectory() as td:
            _git_repo(td)
            repo_mod._exclude_graft(td)
            body = (pathlib.Path(td) / ".git" / "info" / "exclude").read_text()
            self.assertIn("graft/", body)

    def test_git_actually_ignores_the_index(self):
        """The entry is only worth anything if `git add -A` skips the graph."""
        with tempfile.TemporaryDirectory() as td:
            _git_repo(td)
            repo_mod._exclude_graft(td)
            (pathlib.Path(td) / "graft").mkdir()
            (pathlib.Path(td) / "graft" / "INDEX.md").write_text("# map\n")
            (pathlib.Path(td) / "app.ts").write_text("export const x = 1\n")

            subprocess.run(["git", "add", "-A"], cwd=td, capture_output=True, check=True)
            staged = subprocess.run(["git", "diff", "--cached", "--name-only"],
                                    cwd=td, capture_output=True, text=True).stdout
            self.assertIn("app.ts", staged)
            self.assertNotIn("graft/", staged)

    def test_is_idempotent(self):
        """Re-indexing runs on every slice; the entry must not stack up."""
        with tempfile.TemporaryDirectory() as td:
            _git_repo(td)
            repo_mod._exclude_graft(td)
            repo_mod._exclude_graft(td)
            body = (pathlib.Path(td) / ".git" / "info" / "exclude").read_text()
            self.assertEqual(body.count("graft/"), 1)

    def test_preserves_existing_exclusions(self):
        with tempfile.TemporaryDirectory() as td:
            _git_repo(td)
            exclude = pathlib.Path(td) / ".git" / "info" / "exclude"
            exclude.write_text("*.local")          # no trailing newline, on purpose
            repo_mod._exclude_graft(td)
            body = exclude.read_text()
            self.assertIn("*.local", body)
            self.assertIn("\ngraft/\n", body)

    def test_survives_a_non_git_directory(self):
        """Best-effort contract: indexing must never be able to fail a run."""
        with tempfile.TemporaryDirectory() as td:
            repo_mod._exclude_graft(td)           # no .git/ — must not raise
            self.assertFalse((pathlib.Path(td) / ".git").exists())


class IndexedBeforeAgentsRead(unittest.TestCase):
    def test_clone_starter_indexes(self):
        """`architect` is the first reader and runs long before `finish`, so
        the graph has to exist by the end of the clone node."""
        from project_factory import graph as graph_mod

        calls: list[str] = []
        real_create, real_index = repo_mod.create_project_repo, repo_mod.index_with_graft
        graph_repo = graph_mod.repo_mod
        try:
            graph_repo.create_project_repo = lambda **kw: repo_mod.RepoInfo(
                kw["repo_path"], kw["starter_ref"], "a" * 40)
            graph_repo.index_with_graft = lambda r: (calls.append(r), "graft index refreshed")[1]

            out = graph_mod.clone_starter({
                "cfg": {"starter_url": "https://example.invalid/starter.git",
                        "starter_ref": "starter-minimal"},
                "repo_path": "/tmp/does-not-need-to-exist",
            })
        finally:
            graph_repo.create_project_repo = real_create
            graph_repo.index_with_graft = real_index

        self.assertEqual(calls, ["/tmp/does-not-need-to-exist"])
        self.assertTrue(any("graft" in line for line in out["log"]))


if __name__ == "__main__":
    unittest.main()
