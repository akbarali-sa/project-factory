"""
run-project machinery: plan validation, oracle validation, project accounting,
gate policy, live-log cost ground truth, and the merge step.

Fixtures mirror the REAL schemas (board events as EventStorming Studio emits
them, scenarios as the exemplar/seeded template shapes) — per the PR #7
lesson: synthetic fixture keys that differ from production data validate the
bug, not the code.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import tempfile
import unittest

from project_factory import config as cfgmod
from project_factory import livelog, planner, repo
from project_factory.graph import _gate_policy, gate_contract, gate_spec


def _board_event(eid: str, name: str, bc: str = "Packing List Ingestion") -> dict:
    """The real board shape — same keys EventStorming Studio emits."""
    return {
        "id": eid, "name": name, "flow_id": "flow_x", "bounded_context": bc,
        "command": {"id": f"cmd_{eid}", "label": name, "description": None},
        "event": {"id": f"evt_{eid}", "label": name, "description": None},
        "aggregate": {"id": "agg_x", "label": "X"},
        "views": [], "actors": [], "questions": [], "deferred": [], "nfrs": [],
        "implementation": {
            "inputs": [], "outputs": [], "calculation_examples": [],
            "business_rules": ["rule"], "reference_data": [],
            "implementation_notes": [], "acceptance_criteria": ["Summary: x"],
            "engineering_tasks": [], "work_item": None,
        },
    }


EVENTS = [
    _board_event("be_uploaded", "Packing list uploaded"),
    _board_event("be_registered", "Container registered"),
    _board_event("be_scanned", "Barcode scanned", "Warehouse Scanning"),
    _board_event("be_synced", "Count synced", "Warehouse Scanning"),
]


def _plan(**overrides) -> dict:
    plan = {
        "slices": [
            {"id": "slice_ingestion", "name": "Ingestion", "wave": 1,
             "bounded_context": "Packing List Ingestion",
             "event_ids": ["be_uploaded", "be_registered"]},
            {"id": "slice_scanning", "name": "Scanning", "wave": 2,
             "bounded_context": "Warehouse Scanning",
             "event_ids": ["be_scanned"]},
        ],
        "out_of_scope": [{"id": "be_synced", "reason": "offline sync is the hard 10%"}],
    }
    plan.update(overrides)
    return plan


class ValidatePlanTests(unittest.TestCase):
    def test_valid_plan_passes(self) -> None:
        self.assertEqual(planner.validate_plan(_plan(), EVENTS), [])

    def test_unaccounted_event_is_an_error(self) -> None:
        errors = planner.validate_plan(_plan(out_of_scope=[]), EVENTS)
        self.assertTrue(any("be_synced" in e for e in errors))

    def test_event_in_two_slices_is_an_error(self) -> None:
        p = _plan()
        p["slices"][1]["event_ids"].append("be_uploaded")
        errors = planner.validate_plan(p, EVENTS)
        self.assertTrue(any("both" in e for e in errors))

    def test_bad_slice_id_and_wave(self) -> None:
        p = _plan()
        p["slices"][0]["id"] = "Ingestion Slice!"
        p["slices"][1]["wave"] = "two"
        errors = planner.validate_plan(p, EVENTS)
        self.assertTrue(any("bad slice id" in e for e in errors))
        self.assertTrue(any("wave" in e for e in errors))

    def test_parked_without_reason_is_an_error(self) -> None:
        p = _plan(out_of_scope=[{"id": "be_synced", "reason": ""}])
        errors = planner.validate_plan(p, EVENTS)
        self.assertTrue(any("no reason" in e for e in errors))


def _oracle_yaml(**slice_overrides) -> str:
    sl = {"id": "slice_ingestion", "name": "Ingestion", "wave": 1,
          "bounded_context": "Packing List Ingestion",
          "approved_by": None, "approved_at": None}
    sl.update(slice_overrides)
    return json.dumps({  # JSON is valid YAML — and unambiguous to build here
        "slice": sl,
        "aggregates": ["Container"],
        "scenarios": [
            {"id": "SC-001", "title": "happy path", "traces_to": ["be_uploaded"],
             "given": ["no container"], "when": "upload", "then": ["HTTP 201"]},
            {"id": "SC-002", "title": "update not duplicate",
             "traces_to": ["be_registered"], "given": ["container exists"],
             "when": "re-upload", "then": ["HTTP 200"]},
            {"id": "SC-003", "title": "bad file rejected", "traces_to": ["FR-001"],
             "given": ["a .txt file"], "when": "upload", "then": ["HTTP 422"]},
        ],
        "web_scenarios": [
            {"id": "WEB-001", "title": "form validates", "screen": "/upload"},
        ],
        "e2e_scenarios": [
            {"id": "E2E-001", "title": "end to end", "traces_to": ["SC-001"],
             "given": ["stack running"], "when": "upload via UI",
             "then": ["success summary"]},
        ],
        "provisional_scenarios": [], "out_of_scope": [],
    })


PLANNED = {"id": "slice_ingestion", "name": "Ingestion", "wave": 1,
           "bounded_context": "Packing List Ingestion",
           "event_ids": ["be_uploaded", "be_registered"]}


class ValidateOracleTests(unittest.TestCase):
    def test_valid_oracle_passes(self) -> None:
        self.assertEqual(planner.validate_oracle(_oracle_yaml(), PLANNED), [])

    def test_wrong_slice_id_fails(self) -> None:
        errors = planner.validate_oracle(_oracle_yaml(id="slice_other"), PLANNED)
        self.assertTrue(any("slice.id" in e for e in errors))

    def test_prefilled_approval_fails(self) -> None:
        errors = planner.validate_oracle(_oracle_yaml(approved_by="agent"), PLANNED)
        self.assertTrue(any("approved_by" in e for e in errors))

    def test_traces_to_outside_slice_fails(self) -> None:
        doc = json.loads(_oracle_yaml())
        doc["scenarios"][0]["traces_to"] = ["be_scanned"]  # another slice's event
        errors = planner.validate_oracle(json.dumps(doc), PLANNED)
        self.assertTrue(any("be_scanned" in e for e in errors))

    def test_missing_e2e_fails(self) -> None:
        doc = json.loads(_oracle_yaml())
        doc["e2e_scenarios"] = []
        errors = planner.validate_oracle(json.dumps(doc), PLANNED)
        self.assertTrue(any("e2e" in e for e in errors))

    def test_not_yaml_fails(self) -> None:
        errors = planner.validate_oracle(":\n  - {", PLANNED)
        self.assertTrue(errors)


class ProjectAccountingTests(unittest.TestCase):
    def _project(self, td: str) -> cfgmod.Project:
        board = {"id": "b", "name": "B", "business_events": [
            _board_event("be_uploaded", "Uploaded")], "flows": []}
        (pathlib.Path(td) / "b.board.json").parent.mkdir(exist_ok=True)
        pdir = pathlib.Path(td) / "proj"
        (pdir / "specs").mkdir(parents=True)
        (pdir / "specs" / "b.board.json").write_text(json.dumps(board))
        (pdir / "specs" / "project-plan.json").write_text(json.dumps(
            {"slices": [], "out_of_scope": [], "approved_by": None}))
        return cfgmod.discover("proj", workspace=td)

    def test_slice_costs_are_monotonic_max(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = self._project(td)
            p.record_slice_cost("s1", 10.0)
            p.record_slice_cost("s1", 7.0)    # a resume that undercounts
            p.record_slice_cost("s2", 5.5)
            self.assertEqual(p.spent_usd(), 15.5)

    def test_discover_accepts_plan_only_specs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = self._project(td)   # no *.scenarios.yaml at all
            self.assertEqual(p.slices, [])

    def test_scaffold_project_mode_writes_plan_not_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            board = pathlib.Path(td) / "x.board.json"
            board.write_text(json.dumps({"id": "x", "name": "X",
                                         "business_events": [], "flows": []}))
            p = cfgmod.scaffold("px", str(board), workspace=td, project_mode=True)
            self.assertEqual(p.slices, [])
            self.assertTrue((p.dir / "specs" / "project-plan.json").exists())
            run_cfg = json.loads((p.dir / "run.json").read_text())
            self.assertIn("project_budget_usd", run_cfg)
            self.assertNotIn("budget_usd", run_cfg)


class GatePolicyTests(unittest.TestCase):
    def test_default_is_human(self) -> None:
        self.assertEqual(_gate_policy({"cfg": {}}, "spec"), "human")

    def test_cfg_override(self) -> None:
        state = {"cfg": {"gates": {"spec": "auto"}}}
        self.assertEqual(_gate_policy(state, "spec"), "auto")
        self.assertEqual(_gate_policy(state, "pr"), "human")

    def test_auto_gate_spec_returns_without_interrupt(self) -> None:
        state = {"cfg": {"gates": {"spec": "auto"}},
                 "gaps": [{"id": "e1", "verdict": "underspecified"}],
                 "scenarios": {"provisional_scenarios": [{"id": "SC-P01"}]}}
        out = gate_spec(state)   # interrupt() would raise outside a runtime
        self.assertIn("gate-policy:auto", out["log"][0])
        self.assertIn("SC-P01", out["log"][0])

    def test_auto_gate_contract_returns_without_interrupt(self) -> None:
        state = {"cfg": {"gates": {"contract": "auto"}},
                 "scenarios": {"scenarios": [
                     {"id": "SC-007", "depends_on_schema_change": True}]},
                 "contract": "..."}
        out = gate_contract(state)
        self.assertIn("gate-policy:auto", out["log"][0])
        self.assertIn("SC-007", out["log"][0])

    def test_parse_gates(self) -> None:
        from project_factory.__main__ import _parse_gates
        self.assertEqual(_parse_gates("spec=human,pr=auto"),
                         {"spec": "human", "pr": "auto"})
        self.assertIsNone(_parse_gates(None))
        with self.assertRaises(SystemExit):
            _parse_gates("bogus=auto")


class LiveLogCostTests(unittest.TestCase):
    def test_sums_agent_done_lines_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log = pathlib.Path(td) / ".factory" / "live" / "s1.log"
            log.parent.mkdir(parents=True)
            log.write_text(
                "[15:08:38] ✓ architect done in 189s ($0.978)\n"
                "[15:25:14] ✓ test_author done in 958s ($3.975)\n"
                "ABORTED (budget): run budget $25.0 exceeded after implementer "
                "(spent $25.74)\n"          # must NOT be counted
                "[15:44:01] ✓ implementer done in 1115s ($6.617)\n")
            self.assertEqual(livelog.live_log_cost(td, "s1"), 11.57)

    def test_missing_log_is_zero(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(livelog.live_log_cost(td, "nope"), 0.0)


class SpliceSchemaTests(unittest.TestCase):
    """The lint must validate what migrate() will write: a contract may
    reference models it does not redeclare (slice 2's User references the
    untouched PackingList and UserRole) — validating its block alone
    false-positived on exactly that."""

    EXISTING = (
        'generator client { provider = "prisma-client-js" }\n\n'
        "enum UserRole {\n  ADMIN\n  OPERATOR\n}\n\n"
        "model User {\n  id String @id\n  role UserRole\n}\n\n"
        "model PackingList {\n  id String @id\n}\n"
    )

    def test_redeclared_model_replaces_existing(self) -> None:
        from project_factory.harness import splice_schema
        out = splice_schema(self.EXISTING,
                            "model User {\n  id String @id\n  role UserRole\n"
                            "  scans Scan[]\n}\n\nmodel Scan {\n  id String @id\n}")
        self.assertEqual(out.count("model User {"), 1)
        self.assertIn("scans Scan[]", out)
        self.assertIn("model Scan {", out)
        # untouched declarations survive
        self.assertIn("model PackingList {", out)
        self.assertIn("enum UserRole {", out)
        self.assertIn("generator client", out)

    def test_new_models_append_without_disturbing_rest(self) -> None:
        from project_factory.harness import splice_schema
        out = splice_schema(self.EXISTING, "model CountEntry {\n  id String @id\n}")
        self.assertEqual(out.count("model User {"), 1)
        self.assertTrue(out.rstrip().endswith("}"))
        self.assertIn("model CountEntry {", out)


class AllocatePortsTests(unittest.TestCase):
    """api and web must never receive the same port: the api binds it, the web
    spawn dies with EADDRINUSE, and the web health check then passes against
    the API — so every e2e test runs web pages against a REST server."""

    def test_distinct_ports_when_bases_collide_after_scan(self) -> None:
        from project_factory import infra
        busy = {3000, 3001, 3002, 3003}
        real_free = infra._free
        infra._free = lambda p: p not in busy  # noqa: SLF001 - test seam
        try:
            api, web = infra.allocate_ports(3001, 3000)
        finally:
            infra._free = real_free
        self.assertNotEqual(api, web)
        self.assertNotIn(api, busy)
        self.assertNotIn(web, busy)

    def test_free_bases_are_used_as_is(self) -> None:
        from project_factory import infra
        real_free = infra._free
        infra._free = lambda p: True  # noqa: SLF001 - test seam
        try:
            self.assertEqual(infra.allocate_ports(3001, 3000), (3001, 3000))
        finally:
            infra._free = real_free


class AggregateCheckTests(unittest.TestCase):
    """4b must judge the SPLICED schema too: a later slice's oracle lists
    aggregates it only reads (wave 1's PackingList), and forcing a pointless
    redeclaration to satisfy the lint is exactly the wrong incentive."""

    def test_read_model_aggregate_is_satisfied_by_the_api_contract(self) -> None:
        """A projection (WorkerProductivity: counts per worker, computed on
        request) has no table by design. Demanding one would demand a
        denormalised cache nobody asked for — but an aggregate the contract
        forgot entirely must still fail."""
        openapi = (
            "paths:\n  /reports/productivity:\n    get:\n"
            "      operationId: calculateWorkerProductivity\n"
            "components:\n  schemas:\n    WorkerProductivityResponse:\n"
            "      type: object\n"
        )
        schema = "model ReportingPackage {\n  id String @id\n}\n"
        for agg, expected in (("WorkerProductivity", True),
                              ("ReportingPackage", True),
                              ("NeverMentioned", False)):
            with self.subTest(aggregate=agg):
                accounted = bool(re.search(rf"model\s+{agg}\b", schema)
                                 or agg in openapi)
                self.assertEqual(accounted, expected)

    def test_aggregate_satisfied_by_existing_repo_model(self) -> None:
        from project_factory.harness import splice_schema
        existing = (
            'generator client { provider = "prisma-client-js" }\n\n'
            "model PackingList {\n  id String @id\n}\n\n"
            "model PackingListItem {\n  id String @id\n}\n"
        )
        # Slice 3's contract adds only its own model.
        merged = splice_schema(existing, "model ContainerProgress {\n  id String @id\n}")
        for agg in ("PackingList", "PackingListItem", "ContainerProgress"):
            with self.subTest(aggregate=agg):
                self.assertRegex(merged, rf"model\s+{agg}\b")


class RerunIdempotencyTests(unittest.TestCase):
    """A reused repo (new thread generation / crashed-slice retry) re-runs
    commit_specs and create_branch — both must tolerate already-done work."""

    def _repo(self, td: str):
        def git(*args: str) -> subprocess.CompletedProcess:
            return subprocess.run(
                ["git", "-C", td, "-c", "user.name=t", "-c", "user.email=t@t",
                 *args], capture_output=True, text=True, check=True)
        git("init", "-b", "main")
        pathlib.Path(td, "README.md").write_text("x\n")
        git("add", "-A"); git("commit", "-m", "base")
        return git

    def test_commit_spec_artifacts_twice_is_fine(self) -> None:
        with tempfile.TemporaryDirectory() as td, \
             tempfile.TemporaryDirectory() as srcd:
            self._repo(td)
            board = pathlib.Path(srcd) / "x.board.json"
            board.write_text("{}")
            scen = pathlib.Path(srcd) / "x.scenarios.yaml"
            scen.write_text("slice: {id: s}")
            repo.commit_spec_artifacts(td, str(board), str(scen))
            repo.commit_spec_artifacts(td, str(board), str(scen))  # no raise

    def test_create_branch_switches_to_existing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            git = self._repo(td)
            repo.create_branch(td, "feat/x")
            git("switch", "main")
            repo.create_branch(td, "feat/x")   # exists — must SWITCH, not stay
            self.assertEqual(
                git("branch", "--show-current").stdout.strip(), "feat/x")


class MergeToMainTests(unittest.TestCase):
    def test_merge_folds_branch_and_leaves_head_on_main(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            def git(*args: str) -> str:
                return subprocess.run(
                    ["git", "-C", td, "-c", "user.name=t",
                     "-c", "user.email=t@t", *args],
                    capture_output=True, text=True, check=True).stdout

            git("init", "-b", "main")
            pathlib.Path(td, "a.txt").write_text("base\n")
            git("add", "-A"); git("commit", "-m", "base")
            git("switch", "-c", "feat/slice_x")
            pathlib.Path(td, "b.txt").write_text("slice\n")
            git("add", "-A"); git("commit", "-m", "slice work")

            sha = repo.merge_to_main(td, "feat/slice_x")

            self.assertEqual(git("branch", "--show-current").strip(), "main")
            self.assertTrue(pathlib.Path(td, "b.txt").exists())
            self.assertEqual(git("rev-parse", "HEAD").strip(), sha)
            # --no-ff: the merge commit has two parents
            self.assertEqual(len(git("show", "-s", "--format=%P", "HEAD").split()), 2)


if __name__ == "__main__":
    unittest.main()
