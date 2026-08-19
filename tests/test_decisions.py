"""
The decision register: board import, attributable resolution, the
start-blocker gate check, analyst-draft validation, and the assumptions
register consuming decisions instead of raw board questions.

The behavior under test is auditability: every answer carries WHO and WHEN,
answers reach the assumptions register verbatim (never paraphrased), and the
gate only holds on open `blocker: start` questions — deferring is a decision
and unblocks.
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

import yaml

from project_factory import config as cfgmod
from project_factory import decisions
from project_factory import planner

from test_backlog_input import _event, _tree


def _project_with_questions(td: str) -> cfgmod.Project:
    tree = _tree()
    root = tree["root"] if "root" in tree else tree
    root["business_events"][0]["questions"] = [
        {"id": "q_fmt", "question": "Which upload formats?",
         "criticality": "critical", "status": "pending"},
        {"id": "q_dup", "question": "Duplicate container numbers?",
         "status": "answered", "answer": "Reject with an explicit error."},
    ]
    root["business_events"][1]["questions"] = [
        {"id": "q_tz", "question": "Which timezone for timestamps?",
         "criticality": "normal", "status": "pending"},
    ]
    ws = pathlib.Path(td)
    specs = ws / "widget" / "specs"
    specs.mkdir(parents=True)
    (specs / "widget.board.json").write_text(json.dumps(tree))
    (specs / "project-plan.json").write_text(json.dumps(
        {"slices": [], "out_of_scope": [],
         "approved_by": None, "approved_at": None}))
    return cfgmod.discover("widget", td)


class ImportTests(unittest.TestCase):
    def test_board_import_maps_status_and_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = _project_with_questions(td)
            data = decisions.import_board_questions(project)
            by_id = {q["id"]: q for q in data["questions"]}

            self.assertEqual(by_id["q_fmt"]["status"], "open")
            self.assertEqual(by_id["q_fmt"]["blocker"], "start")  # critical
            self.assertEqual(by_id["q_tz"]["blocker"], "slices")  # normal
            # A recorded board answer arrives verbatim, already answered.
            self.assertEqual(by_id["q_dup"]["status"], "answered")
            self.assertEqual(by_id["q_dup"]["answer"],
                             "Reject with an explicit error.")
            self.assertIn("board", by_id["q_dup"]["answered_by"])

    def test_import_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = _project_with_questions(td)
            first = decisions.import_board_questions(project)
            again = decisions.import_board_questions(project)
            self.assertEqual(len(first["questions"]), len(again["questions"]))


class ResolveTests(unittest.TestCase):
    def _reg(self, td: str) -> cfgmod.Project:
        project = _project_with_questions(td)
        decisions.import_board_questions(project)
        return project

    def test_answer_records_who_and_when(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = self._reg(td)
            q = decisions.resolve(project, "q_fmt", by="akbar",
                                  answer="CSV and XLSX only.")
            self.assertEqual(q["status"], "answered")
            self.assertEqual(q["answered_by"], "akbar")
            self.assertTrue(q["answered_at"])
            # And it persisted.
            reloaded = decisions.load_decisions(project)
            hit = [x for x in reloaded["questions"] if x["id"] == "q_fmt"][0]
            self.assertEqual(hit["answer"], "CSV and XLSX only.")

    def test_by_is_required(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = self._reg(td)
            with self.assertRaises(decisions.DecisionError):
                decisions.resolve(project, "q_fmt", by="", answer="x")

    def test_answer_xor_defer(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = self._reg(td)
            with self.assertRaises(decisions.DecisionError):
                decisions.resolve(project, "q_fmt", by="akbar")
            with self.assertRaises(decisions.DecisionError):
                decisions.resolve(project, "q_fmt", by="akbar",
                                  answer="x", defer=True)

    def test_defer_unblocks_the_gate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = self._reg(td)
            data = decisions.load_decisions(project)
            self.assertEqual(len(decisions.open_start_blockers(data)), 1)
            decisions.resolve(project, "q_fmt", by="akbar", defer=True)
            data = decisions.load_decisions(project)
            self.assertEqual(decisions.open_start_blockers(data), [])

    def test_reopen_keeps_history(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = self._reg(td)
            decisions.resolve(project, "q_fmt", by="akbar", answer="CSV only.")
            q = decisions.reopen(project, "q_fmt", by="akbar")
            self.assertEqual(q["status"], "open")
            self.assertEqual(q["history"][0]["answer"], "CSV only.")

    def test_add_question_requires_by_and_valid_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = self._reg(td)
            q = decisions.add_question(project, question="Dark mode?",
                                       by="akbar", section="A",
                                       blocker="wave0")
            self.assertEqual(q["source"], "human")
            self.assertTrue(q["id"].startswith("H"))
            with self.assertRaises(decisions.DecisionError):
                decisions.add_question(project, question="x", by="")
            with self.assertRaises(decisions.DecisionError):
                decisions.add_question(project, question="x", by="akbar",
                                       blocker="someday")


class AgentTierRegistrationTests(unittest.TestCase):
    """Every agent name passed to claude() must resolve in AGENT_TIER —
    a missing entry is a KeyError that kills run-project mid-pipeline
    (exactly how the truev run died on 2026-08-19)."""

    def test_new_agents_have_a_tier(self) -> None:
        from project_factory.models import model_for
        self.assertEqual(model_for("decision_analyst"), "opus")
        self.assertEqual(model_for("uiux_previewer"), "opus")


class AnalystValidationTests(unittest.TestCase):
    def test_valid_draft_passes(self) -> None:
        text = ("questions:\n"
                "  - question: Who owns screen layout?\n"
                "    section: A\n"
                "    blocker: start\n"
                "    context: nothing can start\n"
                "    recommended: factory infers\n")
        self.assertEqual(decisions._validate_analyst(text), [])

    def test_bad_blocker_and_section_fail(self) -> None:
        text = ("questions:\n"
                "  - question: x\n"
                "    section: Z\n"
                "    blocker: whenever\n")
        errors = decisions._validate_analyst(text)
        self.assertTrue(any("blocker" in e for e in errors), errors)
        self.assertTrue(any("section" in e for e in errors), errors)

    def test_empty_fails(self) -> None:
        self.assertTrue(decisions._validate_analyst("questions: []"))


class AssumptionsConsumeRegisterTests(unittest.TestCase):
    """draft_assumptions must prefer the register: human answers imported
    verbatim as resolved, open/deferred falling through as pending."""

    def test_all_answered_needs_no_agent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = _project_with_questions(td)
            decisions.import_board_questions(project)
            decisions.resolve(project, "q_fmt", by="akbar",
                              answer="CSV and XLSX only.")
            decisions.resolve(project, "q_tz", by="sana", answer="UTC.")

            # Every question answered → the register writes with NO agent
            # call (claude() would raise here if invoked: no harness in CI).
            target = planner.draft_assumptions(project)
            data = yaml.safe_load(target.read_text())
            self.assertEqual(data["working_assumptions"], [])
            answers = {r["question_id"]: r for r in data["resolved"]}
            self.assertEqual(answers["q_fmt"]["answer"], "CSV and XLSX only.")
            self.assertIn("akbar", answers["q_fmt"]["source"])
            self.assertIn("sana", answers["q_tz"]["source"])


class SpecSyncTests(unittest.TestCase):
    def test_commit_spec_artifacts_carries_registers(self) -> None:
        from project_factory import repo as repo_mod
        with tempfile.TemporaryDirectory() as td:
            project = _project_with_questions(td)
            decisions.import_board_questions(project)
            specs = project.dir / "specs"
            (specs / "s1.scenarios.yaml").write_text("slice:\n  id: s1\n")

            repo = pathlib.Path(td) / "repo"
            repo.mkdir()
            repo_mod._run(["git", "init", "-b", "main"], cwd=str(repo))
            repo_mod.commit_spec_artifacts(
                str(repo), str(project.board_path),
                str(specs / "s1.scenarios.yaml"))
            self.assertTrue((repo / "specs" / "decisions.yaml").is_file())


if __name__ == "__main__":
    unittest.main()
