"""
Backlog-folder inputs: tree-board normalization, the machine-readable scope
rule, backlog->plan conversion (id reconciliation, scope guard, wave-order
check), the unpriced-capability oracle guard, and assumptions validation.

Fixtures mirror the REAL export shapes (EventStorming Studio tree exports,
Luna's backlog.json fields) — synthetic keys that differ from production
data validate the bug, not the code.
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

from project_factory import config as cfgmod
from project_factory import planner


def _event(eid: str, name: str, bc: str = "Packing List Ingestion",
           **over) -> dict:
    e = {
        "id": eid, "name": name, "flow_id": "flow_x", "bounded_context": bc,
        "command": {"id": f"cmd_{eid}", "label": name, "description": None},
        "event": {"id": f"evt_{eid}", "label": name.replace(" ", ""),
                  "description": None},
        "aggregate": {"id": "agg_x", "label": "X"},
        "views": [], "actors": [], "questions": [], "deferred": [], "nfrs": [],
        "out_of_scope": False, "phase": None, "pivotal": False, "policy": None,
        "implementation": {
            "inputs": [], "outputs": [], "calculation_examples": [],
            "business_rules": ["rule"], "reference_data": [],
            "implementation_notes": [], "acceptance_criteria": ["Summary: x"],
            "engineering_tasks": [], "work_item": None,
        },
    }
    e.update(over)
    return e


def _tree() -> dict:
    """A root board with one process expanding to a subprocess board — the
    shape the 2026-08 Studio export produces."""
    root = {
        "id": "brd", "name": "Widget Flow", "kind": "root",
        "parent_process_id": None, "root_board_id": "brd",
        "flows": [{"id": "flow_x", "name": "Ingestion"}],
        "business_events": [
            _event("be_uploaded", "Packing list uploaded"),
            _event("be_registered", "Container registered"),
            _event("be_scanned", "Barcode scanned", "Warehouse Scanning",
                   policy={"id": "p1", "rule": "after registration",
                           "trigger_event_id": "be_registered"}),
            _event("be_synced", "Count synced", "Warehouse Scanning"),
            _event("be_miscount_fixed", "Miscount corrected",
                   "Warehouse Scanning",
                   deferred=[{"id": "def_1", "label": "Unconfirmed capability",
                              "reason": "no client source"}]),
            _event("be_manual_sort", "Items sorted manually",
                   "Out-of-Scope (As-Is Baseline)", out_of_scope=True),
        ],
        "processes": [{"id": "proc_val", "name": "Validation",
                       "flow_id": "flow_x",
                       "bounded_context": "Packing List Ingestion",
                       "expands_to": "brd-validation"}],
        "externals": [],
        "edges": [{"id": "e1", "from": "be_uploaded", "to": "be_registered"}],
        "decision_log": [{"id": "d1", "text": "root decision" * 200,
                          "related_node_id": None, "actor": "x",
                          "timestamp": "2026-01-01"}],
    }
    sub = {
        "id": "brd-validation", "name": "Validation", "kind": "sub",
        "parent_process_id": "proc_val", "root_board_id": "brd",
        "flows": [], "processes": [], "externals": [], "edges": [],
        "business_events": [
            _event("be_rows_produced", "Item rows produced", None),
        ],
        "decision_log": [{"id": "d2", "text": "sub decision",
                          "related_node_id": "be_rows_produced",
                          "actor": "x", "timestamp": "2026-01-02"}],
    }
    return {"root": root, "subprocess_boards": [sub]}


def _backlog() -> dict:
    return {
        "engagement": {"id": "X-1", "name": "Widget Flow"},
        "hard_constraints": [{"id": "hc1", "rule": "two roles only"}],
        "epics": [
            {"id": "EPIC-01", "name": "Foundation", "bounded_context": None,
             "depends_on": []},
            {"id": "EPIC-02", "name": "Ingestion",
             "bounded_context": "Packing List Ingestion", "depends_on": [],
             "note": "accept and register"},
            {"id": "EPIC-03", "name": "Scanning",
             "bounded_context": "Warehouse Scanning",
             "depends_on": ["EPIC-02"], "note": "scan and sync"},
        ],
        "spikes": [{"id": "SPIKE-1", "epic": "EPIC-01", "name": "IdP",
                    "wave": 0, "questions_to_close": ["q_idp"]}],
        "stories": [
            {"id": "FND-1", "epic": "EPIC-01", "wave": 0, "status": "ready",
             "priced": True, "title": "Repo & CI", "depends_on": [],
             "tasks": [{"layer": "infra", "task": "ci"}]},
            {"id": "ING-1", "epic": "EPIC-02", "wave": 1, "status": "ready",
             "priced": True, "title": "Upload", "actor": "Admin",
             "board_nodes": ["be_uploaded"], "depends_on": [],
             "tasks": [], "caveats": ["manual upload only"]},
            {"id": "ING-2", "epic": "EPIC-02", "wave": 2, "status": "ready",
             "priced": True, "title": "Validate and register",
             "board_nodes": ["be_rows_produced",
                             "be_registered (auto half)"],
             "depends_on": ["ING-1"], "tasks": []},
            {"id": "SCN-1", "epic": "EPIC-03", "wave": 3, "status": "ready",
             "priced": True, "title": "Scan",
             "board_nodes": ["be_scanned"], "depends_on": [], "tasks": []},
            {"id": "SCN-2", "epic": "EPIC-03", "wave": 4, "status": "ready",
             "priced": True, "title": "Sync",
             "board_nodes": ["be_synced_to_server"],  # drifted id
             "depends_on": ["SCN-1"], "tasks": []},
        ],
        "not_priced": [
            {"board_node": "be_miscount_fixed", "title": "Miscount corrected",
             "priced": False, "why_out": "no client source",
             "do_not_price": ["a correction UI", "mutable count entries"]},
        ],
        "out_of_scope": [], "known_open_board_items": [],
    }


def _project(td: str) -> cfgmod.Project:
    ws = pathlib.Path(td)
    specs = ws / "widget" / "specs"
    specs.mkdir(parents=True)
    (specs / "widget.board.json").write_text(json.dumps(_tree()))
    (specs / "backlog.json").write_text(json.dumps(_backlog()))
    (specs / "project-plan.json").write_text(json.dumps(
        {"slices": [], "out_of_scope": [],
         "approved_by": None, "approved_at": None}))
    return cfgmod.discover("widget", td)


class InputFolderTests(unittest.TestCase):
    """Scaffolding from a delivered engagement FOLDER. Copying only the board
    silently drops the reviewed backlog, which silently drops deterministic
    planning — the regression that matters here is a quiet one."""

    def _folder(self, td: str, *, extra_boards: bool = False) -> pathlib.Path:
        src = pathlib.Path(td) / "delivery"
        (src / "stories").mkdir(parents=True)
        (src / "board-tree.json").write_text(json.dumps(_tree()))
        (src / "backlog.json").write_text(json.dumps(_backlog()))
        (src / "README.md").write_text("# what is priced\n")
        (src / "stories" / "01-ingestion.md").write_text("# ING-1\n")
        if extra_boards:
            # A folder that also ships the tree's own parts must NOT read as
            # three boards — the tree already contains them.
            (src / "root.json").write_text(json.dumps(_tree()["root"]))
            (src / "sub.json").write_text(
                json.dumps(_tree()["subprocess_boards"][0]))
        return src

    def test_resolves_board_backlog_and_docs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            found = cfgmod.resolve_input_folder(self._folder(td))
            self.assertEqual(found.board.name, "board-tree.json")
            self.assertEqual(found.backlog.name, "backlog.json")
            self.assertEqual(sorted(p.name for p in found.docs),
                             ["01-ingestion.md", "README.md"])

    def test_tree_wins_over_its_own_parts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            found = cfgmod.resolve_input_folder(
                self._folder(td, extra_boards=True))
            self.assertEqual(found.board.name, "board-tree.json")

    def test_two_real_boards_is_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            src = self._folder(td)
            (src / "other-tree.json").write_text(json.dumps(_tree()))
            with self.assertRaises(cfgmod.ConfigError) as ctx:
                cfgmod.resolve_input_folder(src)
            self.assertIn("exactly one expected", str(ctx.exception))

    def test_folder_with_no_board_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            src = pathlib.Path(td) / "empty"
            src.mkdir()
            (src / "backlog.json").write_text("{}")
            with self.assertRaises(cfgmod.ConfigError):
                cfgmod.resolve_input_folder(src)

    def test_scaffold_from_folder_enables_deterministic_planning(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            src = self._folder(td)
            ws = pathlib.Path(td) / "ws"
            project = cfgmod.scaffold("widget", str(src), str(ws),
                                      project_mode=True)
            self.assertEqual(project.board_path.name, "widget.board.json")
            self.assertIsNotNone(project.backlog_path)
            # prose kept for provenance, but OUT of the globs discover() uses
            self.assertTrue((project.dir / "specs" / "reference"
                             / "stories" / "01-ingestion.md").exists())
            self.assertEqual(
                sorted(p.name for p in (project.dir / "specs").glob("*.json")),
                ["backlog.json", "project-plan.json", "widget.board.json"])
            # end to end: the plan converts without an agent
            plan = planner.plan_slices(project)
            self.assertIn("deterministic converter", plan["source"])

    def test_scaffold_from_single_file_still_works(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            src = self._folder(td) / "board-tree.json"
            ws = pathlib.Path(td) / "ws"
            project = cfgmod.scaffold("widget", str(src), str(ws),
                                      project_mode=True)
            self.assertEqual(project.board_path.name, "widget.board.json")
            self.assertIsNone(project.backlog_path)


class NormalizeBoardTests(unittest.TestCase):
    def test_flat_board_passes_through(self) -> None:
        flat = {"name": "X", "business_events": [{"id": "be_a"}]}
        self.assertIs(cfgmod.normalize_board(flat), flat)

    def test_tree_flattens_with_provenance(self) -> None:
        board = cfgmod.normalize_board(_tree())
        ids = {e["id"] for e in board["business_events"]}
        self.assertIn("be_rows_produced", ids)
        by_id = {e["id"]: e for e in board["business_events"]}
        self.assertEqual(by_id["be_rows_produced"]["board_id"], "brd-validation")
        self.assertEqual(by_id["be_uploaded"]["board_id"], "brd")
        # bounded_context inherited from the owning process
        self.assertEqual(by_id["be_rows_produced"]["bounded_context"],
                         "Packing List Ingestion")
        # decision logs concatenate
        self.assertEqual([d["id"] for d in board["decision_log"]], ["d1", "d2"])

    def test_load_board_reads_tree_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "x.board.json"
            p.write_text(json.dumps(_tree()))
            self.assertEqual(cfgmod.load_board(p)["name"], "Widget Flow")


class EventScopeTests(unittest.TestCase):
    def test_flag_wins(self) -> None:
        self.assertFalse(cfgmod.event_in_scope(
            {"out_of_scope": True, "bounded_context": "Real Context"}))

    def test_deferred_card_excludes(self) -> None:
        self.assertFalse(cfgmod.event_in_scope(
            {"deferred": [{"id": "d"}], "bounded_context": "Real Context"}))

    def test_legacy_marker_still_excludes(self) -> None:
        self.assertFalse(cfgmod.event_in_scope(
            {"bounded_context": "Out-of-Scope (As-Is Baseline)"}))

    def test_plain_event_is_in_scope(self) -> None:
        self.assertTrue(cfgmod.event_in_scope(
            {"deferred": [], "out_of_scope": False,
             "bounded_context": "Packing List Ingestion"}))


class ReconcileTests(unittest.TestCase):
    IDS = {"be_count_synced", "be_report_generated", "be_registered"}

    def test_exact(self) -> None:
        self.assertEqual(
            planner._reconcile_node_ref("be_registered", self.IDS),
            "be_registered")

    def test_annotation_stripped(self) -> None:
        self.assertEqual(
            planner._reconcile_node_ref(
                "be_report_generated (period-scoped half)", self.IDS),
            "be_report_generated")

    def test_suffixed_id(self) -> None:
        self.assertEqual(
            planner._reconcile_node_ref("be_count_synced_to_server", self.IDS),
            "be_count_synced")

    def test_small_rename(self) -> None:
        self.assertEqual(
            planner._reconcile_node_ref("be_reporting_package_generated",
                                        self.IDS),
            "be_report_generated")

    def test_unresolved_returns_none(self) -> None:
        self.assertIsNone(
            planner._reconcile_node_ref("be_totally_unknown_thing", self.IDS))


class PlanFromBacklogTests(unittest.TestCase):
    def test_converts_deterministically(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = _project(td)
            plan = planner.plan_from_backlog(
                project, cfgmod.load_board(project.board_path))

            ids = [s["id"] for s in plan["slices"]]
            self.assertEqual(ids, ["slice_packing_list_ingestion",
                                   "slice_warehouse_scanning",
                                   "slice_navigation_and_hub"])
            self.assertEqual([s["wave"] for s in plan["slices"]], [1, 2, 3])
            self.assertEqual(plan["slices"][-1]["kind"], "horizontal")

            ing = plan["slices"][0]
            self.assertEqual(set(ing["event_ids"]),
                             {"be_uploaded", "be_rows_produced",
                              "be_registered"})
            self.assertEqual([s["id"] for s in ing["stories"]],
                             ["ING-1", "ING-2"])
            # drifted id reconciled and recorded, never silently dropped
            self.assertEqual(plan["aliases"]["be_synced_to_server"],
                             "be_synced")
            self.assertEqual(plan["aliases"]["be_registered (auto half)"],
                             "be_registered")
            # scope guards carried onto the plan
            self.assertEqual(plan["hard_constraints"][0]["id"], "hc1")
            self.assertEqual(plan["not_priced"][0]["board_node"],
                             "be_miscount_fixed")
            self.assertEqual([f["id"] for f in plan["foundation"]], ["FND-1"])
            # deferred + as-is events are excluded, not parked
            for s in plan["slices"]:
                self.assertNotIn("be_miscount_fixed", s.get("event_ids", []))
                self.assertNotIn("be_manual_sort", s.get("event_ids", []))
            # plan persisted, unapproved
            self.assertIsNone(plan["approved_by"])
            on_disk = json.loads(planner.plan_path(project).read_text())
            self.assertEqual(on_disk["slices"][0]["id"], ids[0])

    def test_oversized_slice_is_flagged_for_the_approver(self) -> None:
        """An epic is an estimation unit, a slice is a delivery unit. The
        7-event ingestion slice cost $12.26 in tests alone and killed the
        Implementer on its ceiling — the approver must see that coming."""
        with tempfile.TemporaryDirectory() as td:
            project = _project(td)
            plan = planner.plan_from_backlog(
                project, cfgmod.load_board(project.board_path))
            small = [s for s in plan["slices"] if s["id"].endswith("scanning")]
            self.assertFalse(any(s.get("size_warning") for s in small))

            # a 7-event ingestion epic: three more board events, one more
            # story owning them
            tree = _tree()
            tree["root"]["business_events"] += [
                _event(f"be_extra_{i}", f"Extra step {i}") for i in range(3)]
            (project.dir / "specs" / "widget.board.json").write_text(
                json.dumps(tree))
            backlog = _backlog()
            backlog["stories"].append(
                {"id": "ING-3", "epic": "EPIC-02", "wave": 2,
                 "status": "ready", "priced": True, "title": "Extra steps",
                 "board_nodes": [f"be_extra_{i}" for i in range(3)],
                 "depends_on": [], "tasks": []})
            backlog["stories"][3]["epic"] = "EPIC-02"
            (project.dir / "specs" / "backlog.json").write_text(
                json.dumps(backlog))
            planner.plan_path(project).unlink()
            plan = planner.plan_from_backlog(
                project, cfgmod.load_board(project.board_path))
            ing = next(s for s in plan["slices"]
                       if s["id"] == "slice_packing_list_ingestion")
            self.assertGreater(len(ing["event_ids"]), 6)
            self.assertIn("consider splitting", ing["size_warning"])

    def test_story_pricing_a_deferred_event_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = _project(td)
            backlog = _backlog()
            backlog["stories"][3]["board_nodes"].append("be_miscount_fixed")
            (project.dir / "specs" / "backlog.json").write_text(
                json.dumps(backlog))
            with self.assertRaises(planner.PlanError) as ctx:
                planner.plan_from_backlog(
                    project, cfgmod.load_board(project.board_path))
            self.assertIn("out of scope / Deferred", str(ctx.exception))

    def test_unresolved_ref_fails_loudly(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = _project(td)
            backlog = _backlog()
            backlog["stories"][1]["board_nodes"] = ["be_never_heard_of_it"]
            (project.dir / "specs" / "backlog.json").write_text(
                json.dumps(backlog))
            with self.assertRaises(planner.PlanError) as ctx:
                planner.plan_from_backlog(
                    project, cfgmod.load_board(project.board_path))
            self.assertIn("be_never_heard_of_it", str(ctx.exception))

    def test_wave_inversion_detected(self) -> None:
        """A consumer scheduled before its producer must fail the plan —
        the BL-007 class of defect the 0-edge graph hid."""
        with tempfile.TemporaryDirectory() as td:
            project = _project(td)
            backlog = _backlog()
            # scanning epic first: be_scanned (policy-triggered by
            # be_registered) lands in wave 1, its producer in wave 2
            backlog["stories"][3]["wave"] = 0
            backlog["stories"][4]["wave"] = 0
            for s in backlog["stories"][1:3]:
                s["wave"] = 5
            (project.dir / "specs" / "backlog.json").write_text(
                json.dumps(backlog))
            with self.assertRaises(planner.PlanError) as ctx:
                planner.plan_from_backlog(
                    project, cfgmod.load_board(project.board_path))
            self.assertIn("wave inversion", str(ctx.exception))

    def test_plan_slices_routes_to_converter(self) -> None:
        """With a backlog present, plan_slices must never call an agent."""
        with tempfile.TemporaryDirectory() as td:
            project = _project(td)
            plan = planner.plan_slices(project)   # would explode calling claude
            self.assertIn("deterministic converter", plan["source"])


class ForbiddenCapabilityTests(unittest.TestCase):
    def test_collected_from_deferred_cards(self) -> None:
        board = cfgmod.normalize_board(_tree())
        forb = planner._forbidden_capabilities(board)
        self.assertEqual([f["id"] for f in forb], ["be_miscount_fixed"])
        self.assertIn("Miscount corrected", forb[0]["terms"])

    def test_oracle_mentioning_deferred_capability_fails(self) -> None:
        board = cfgmod.normalize_board(_tree())
        forb = planner._forbidden_capabilities(board)
        planned = {"id": "slice_warehouse_scanning", "wave": 2,
                   "event_ids": ["be_scanned", "be_synced"]}
        draft = (
            "slice:\n  id: slice_warehouse_scanning\n  wave: 2\n"
            "  approved_by: null\n"
            "scenarios:\n"
            "  - id: SC-001\n    title: scan\n    traces_to: [be_scanned]\n"
            "    when: scan\n    then: [ok]\n"
            "  - id: SC-002\n    title: sync\n    traces_to: [be_synced]\n"
            "    when: sync\n    then: [ok]\n"
            "  - id: SC-003\n    title: operator applies Miscount corrected\n"
            "    traces_to: [be_synced]\n    when: fix\n    then: [ok]\n"
            "e2e_scenarios:\n"
            "  - id: E2E-001\n    title: walk\n    traces_to: [SC-001]\n"
            "    when: walk\n    then: [ok]\n")
        errors = planner.validate_oracle(draft, planned, forbidden=forb)
        self.assertTrue(any("Deferred, unpriced" in e for e in errors), errors)

    def test_out_of_scope_mention_is_fine(self) -> None:
        board = cfgmod.normalize_board(_tree())
        forb = planner._forbidden_capabilities(board)
        planned = {"id": "slice_warehouse_scanning", "wave": 2,
                   "event_ids": ["be_scanned", "be_synced"]}
        draft = (
            "slice:\n  id: slice_warehouse_scanning\n  wave: 2\n"
            "  approved_by: null\n"
            "scenarios:\n"
            "  - id: SC-001\n    title: scan\n    traces_to: [be_scanned]\n"
            "    when: scan\n    then: [ok]\n"
            "  - id: SC-002\n    title: sync\n    traces_to: [be_synced]\n"
            "    when: sync\n    then: [ok]\n"
            "  - id: SC-003\n    title: sync failure surfaced\n"
            "    traces_to: [be_synced]\n    when: wifi drops\n"
            "    then: [failure shown]\n"
            "e2e_scenarios:\n"
            "  - id: E2E-001\n    title: walk\n    traces_to: [SC-001]\n"
            "    when: walk\n    then: [ok]\n"
            "out_of_scope:\n"
            "  - Miscount corrected (Deferred card, not priced)\n")
        errors = planner.validate_oracle(draft, planned, forbidden=forb)
        self.assertEqual(errors, [])


class DraftAssumptionsTests(unittest.TestCase):
    def test_prompt_is_a_string_and_file_lands(self) -> None:
        """End-to-end through draft_assumptions with the agent stubbed —
        catches prompt-construction defects (a stray trailing comma once made
        the prompt a tuple and killed subprocess.Popen mid-run)."""
        captured: dict = {}

        def fake_claude(agent, prompt, **kw):
            captured["agent"], captured["prompt"] = agent, prompt
            return ("working_assumptions:\n"
                    "  - id: a_q_x\n    question_id: q_x\n"
                    "    criticality: critical\n"
                    "    assumption: barcode is the key\n"
                    "    rationale: safest default\n    impacts: [be_scanned]\n")

        with tempfile.TemporaryDirectory() as td:
            project = _project(td)
            tree = _tree()
            tree["root"]["business_events"][2]["questions"] = [
                {"id": "q_x", "question": "key?", "criticality": "critical",
                 "status": "pending", "answer": None},
                {"id": "q_done", "question": "answered one?",
                 "criticality": "normal", "status": "answered",
                 "answer": "yes, decided"},
            ]
            (project.dir / "specs" / "widget.board.json").write_text(
                json.dumps(tree))
            orig = planner.claude
            planner.claude = fake_claude
            try:
                path = planner.draft_assumptions(project)
            finally:
                planner.claude = orig
            self.assertIsInstance(captured["prompt"], str)
            self.assertEqual(captured["agent"], "assumptions_author")
            data = __import__("yaml").safe_load(path.read_text())
            self.assertEqual(data["working_assumptions"][0]["question_id"], "q_x")
            self.assertEqual(data["resolved"][0]["question_id"], "q_done")
            # never overwrites: second call returns without the agent
            planner.claude = None  # would explode if called
            try:
                self.assertEqual(planner.draft_assumptions(project), path)
            finally:
                planner.claude = orig


class ExpectedStatusCodeTests(unittest.TestCase):
    """A scenario that pins its failure mode ("404, not 500") must not be
    read as demanding a 500 in the contract — that cost the barcode-v2 run a
    crashed contract_lint after the architect had already been paid for."""

    def test_positive_codes_expected(self) -> None:
        from project_factory.harness import _expected_status_codes
        self.assertEqual(
            _expected_status_codes("returns HTTP 201 with the batch id"),
            {"201"})

    def test_negated_code_is_not_expected(self) -> None:
        from project_factory.harness import _expected_status_codes
        text = ("A request for an unknown batch id returns HTTP 404, not "
                "HTTP 500")
        self.assertEqual(_expected_status_codes(text), {"404"})

    def test_other_negation_wordings(self) -> None:
        from project_factory.harness import _expected_status_codes
        for phrasing in ("HTTP 422 is returned, not HTTP 500",
                         "responds HTTP 422 rather than HTTP 500",
                         "returns HTTP 422 instead of HTTP 500",
                         "never HTTP 500; the API returns HTTP 422"):
            self.assertEqual(_expected_status_codes(phrasing), {"422"},
                             phrasing)

    def test_code_asserted_positively_elsewhere_still_counts(self) -> None:
        from project_factory.harness import _expected_status_codes
        text = ("the upload returns HTTP 503 when storage is unavailable. "
                "A bad id returns HTTP 404, not HTTP 503")
        self.assertEqual(_expected_status_codes(text), {"503", "404"})


class ReasoningAgentSandboxTests(unittest.TestCase):
    """A reasoning agent's deliverable is the text it returns. During the
    barcode-v2 run the planner-layer agents inherited the operator's personal
    CLAUDE.md and spent minutes each writing Obsidian notes — wall-clock,
    money, and this project's vocabulary leaked into a store other projects'
    agents read."""

    def _cmd_for(self, **kw) -> list[str]:
        from project_factory import models
        seen: dict = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            class P:
                returncode = 0
                stdout = json.dumps({"result": "ok", "total_cost_usd": 0.0})
                stderr = ""
            return P()

        orig = models.subprocess.run
        models.subprocess.run = fake_run
        try:
            models.claude("architect", "do a thing", **kw)
        finally:
            models.subprocess.run = orig
        return seen["cmd"]

    def test_no_write_scope_disallows_file_tools(self) -> None:
        cmd = self._cmd_for()
        self.assertIn("--disallowedTools", cmd)
        self.assertIn("Write", cmd)
        self.assertIn("Edit", cmd)
        appended = cmd[cmd.index("--append-system-prompt") + 1]
        self.assertIn("do not apply to this invocation", appended.lower())

    def test_write_scope_agent_keeps_its_tools(self) -> None:
        cmd = self._cmd_for(write_scope=["apps/api/**"])
        self.assertIn("acceptEdits", cmd)
        self.assertIn("Write(apps/api/**)", cmd)

    def test_write_scope_agent_is_told_not_to_take_notes(self) -> None:
        """acceptEdits auto-accepts edits beyond the allowlist, so the scope
        is a request, not a wall — the Implementer burned four turns of a
        30-minute budget on ~/.claude memory files and was killed mid-module."""
        cmd = self._cmd_for(write_scope=["apps/api/**"])
        appended = cmd[cmd.index("--append-system-prompt") + 1]
        self.assertIn("memory files", appended)
        self.assertIn("apps/api/**", appended)

    def test_bulk_writers_get_a_longer_ceiling(self) -> None:
        from project_factory import models
        self.assertGreater(models.AGENT_TIMEOUT["implementer"],
                           models.DEFAULT_TIMEOUT)
        self.assertGreater(models.AGENT_TIMEOUT["test_author"],
                           models.DEFAULT_TIMEOUT)
        self.assertNotIn("architect", models.AGENT_TIMEOUT)


class AssumptionsValidationTests(unittest.TestCase):
    PENDING = [
        {"id": "q_key", "question": "barcode or sku?",
         "criticality": "critical", "event_id": "be_scanned"},
        {"id": "q_minor", "question": "toast or banner?",
         "criticality": "normal", "event_id": "be_scanned"},
    ]

    def test_covered_criticals_pass(self) -> None:
        text = ("working_assumptions:\n"
                "  - id: a_q_key\n    question_id: q_key\n"
                "    criticality: critical\n"
                "    assumption: barcode column is the key\n"
                "    rationale: only stated column\n    impacts: [be_scanned]\n")
        self.assertEqual(planner._validate_assumptions(text, self.PENDING), [])

    def test_missing_critical_fails(self) -> None:
        text = ("working_assumptions:\n"
                "  - id: a_q_minor\n    question_id: q_minor\n"
                "    criticality: normal\n    assumption: toast\n"
                "    rationale: convention\n    impacts: [be_scanned]\n")
        errors = planner._validate_assumptions(text, self.PENDING)
        self.assertTrue(any("q_key" in e for e in errors), errors)

    def test_undecided_assumption_fails(self) -> None:
        text = ("working_assumptions:\n"
                "  - id: a_q_key\n    question_id: q_key\n"
                "    criticality: critical\n    assumption: TBD later\n"
                "    rationale: unsure\n    impacts: [be_scanned]\n")
        errors = planner._validate_assumptions(text, self.PENDING)
        self.assertTrue(any("undecided" in e for e in errors), errors)


if __name__ == "__main__":
    unittest.main()
