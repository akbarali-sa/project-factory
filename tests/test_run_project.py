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
from project_factory import harness, livelog, planner, repo
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


class HorizontalSliceTests(unittest.TestCase):
    """
    Four correct vertical slices are not a product. On the first full run every
    screen worked and four of them were reachable only by typing a URL, because
    no slice owned navigation. The horizontal slice closes that — but it owns
    no events, so it needs an exemption from the event_ids rule and a guard
    that keeps it last.
    """

    @staticmethod
    def _with_horizontal(**over) -> dict:
        h = {"id": "slice_navigation_and_hub", "name": "Navigation & hub",
             "wave": 3, "kind": "horizontal", "bounded_context": "Product Shell",
             "event_ids": [], "rationale": "links what waves 1-2 shipped"}
        h.update(over)
        p = _plan()
        p["slices"] = [*p["slices"], h]
        return p

    def test_horizontal_slice_needs_no_events(self) -> None:
        self.assertEqual(planner.validate_plan(self._with_horizontal(), EVENTS), [])

    def test_vertical_slice_still_needs_events(self) -> None:
        """The exemption must be keyed on kind, not on emptiness — otherwise
        any slice that forgot its events silently becomes legal."""
        p = self._with_horizontal()
        p["slices"][1]["event_ids"] = []
        errors = planner.validate_plan(p, EVENTS)
        self.assertTrue(any("no event_ids" in e for e in errors), errors)

    def test_horizontal_slice_must_be_the_last_wave(self) -> None:
        """Wiring navigation before the screens exist wires up half a product."""
        errors = planner.validate_plan(self._with_horizontal(wave=1), EVENTS)
        self.assertTrue(any("last wave" in e for e in errors), errors)

    def test_only_one_horizontal_slice(self) -> None:
        p = self._with_horizontal()
        p["slices"].append({"id": "slice_more_shell", "name": "More", "wave": 3,
                            "kind": "horizontal", "event_ids": [],
                            "rationale": "second one"})
        errors = planner.validate_plan(p, EVENTS)
        self.assertTrue(any("more than one horizontal" in e for e in errors), errors)

    def test_horizontal_slice_needs_a_rationale(self) -> None:
        errors = planner.validate_plan(self._with_horizontal(rationale=""), EVENTS)
        self.assertTrue(any("rationale" in e for e in errors), errors)

    def test_horizontal_slice_does_not_absorb_unaccounted_events(self) -> None:
        """It must not become a dumping ground: parking still has to be
        explicit, with a reason."""
        p = self._with_horizontal()
        p["out_of_scope"] = []
        errors = planner.validate_plan(p, EVENTS)
        self.assertTrue(any("be_synced" in e for e in errors), errors)


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
            self.assertIsNone(run_cfg["project_budget_usd"])  # auto: $50/slice
            self.assertNotIn("budget_usd", run_cfg)

    def test_project_budget_scales_with_plan(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = self._project(td)
            # no plan slices yet -> flat pre-plan bound
            self.assertEqual(planner.project_budget_usd(p),
                             planner.PRE_PLAN_BUDGET_USD)
            plan = {"slices": [{"id": f"s{i}", "wave": 1} for i in range(3)],
                    "out_of_scope": [], "approved_by": None}
            self.assertEqual(planner.project_budget_usd(p, plan), 150.0)

    def test_project_budget_explicit_override_wins(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = self._project(td)
            (p.dir / "run.json").write_text(json.dumps(
                {"project_budget_usd": 500}))
            p = cfgmod.discover("proj", workspace=td)
            plan = {"slices": [{"id": "s1", "wave": 1}]}
            self.assertEqual(planner.project_budget_usd(p, plan), 500.0)


class WipeProjectTests(unittest.TestCase):
    """wipe_project against a throwaway workspace. Postgres endpoints point
    at 127.0.0.1:1 (instant refusal), so the db/checkpoint steps must land in
    `errors` while the directory still gets removed — a dead Postgres must
    never make a project undeletable."""

    def _scaffolded(self, td: str) -> pathlib.Path:
        pdir = pathlib.Path(td) / "doomed"
        (pdir / "specs").mkdir(parents=True)
        (pdir / "repo").mkdir()
        (pdir / ".factory" / "live").mkdir(parents=True)
        # dead pid: kill path must skip it without error
        (pdir / ".factory" / "live" / "project.pid").write_text(
            json.dumps({"pid": 2 ** 22 - 1}))
        (pdir / "run.json").write_text(json.dumps({
            "db_host": "127.0.0.1", "db_port": 1,
            "checkpoint_db_url": "postgresql://x:x@127.0.0.1:1/none",
        }))
        return pdir

    def test_wipes_dir_and_reports_unreachable_db(self) -> None:
        from project_factory import wipe
        with tempfile.TemporaryDirectory() as td:
            pdir = self._scaffolded(td)
            out = wipe.wipe_project("doomed", workspace=td)
            self.assertFalse(pdir.exists())
            self.assertTrue(out["dir_deleted"])
            self.assertEqual(out["runs_killed"], [])
            self.assertIn("database", out["errors"])
            self.assertIn("checkpoints", out["errors"])

    def test_rejects_bad_slug_and_missing_project(self) -> None:
        from project_factory import wipe
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(cfgmod.ConfigError):
                wipe.wipe_project("../evil", workspace=td)
            with self.assertRaises(cfgmod.ConfigError):
                wipe.wipe_project("nope", workspace=td)


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

    def test_sticky_ports_reuse_the_same_pair_across_slices(self) -> None:
        """Every slice of a project must get ONE address, so the integrated
        app is always at the same URL — slice 3 landing on 3004 while slice 1
        sits on 3000 is exactly what this prevents."""
        from project_factory import infra
        with tempfile.TemporaryDirectory() as td:
            real_free, real_pids = infra._free, infra._pids_listening
            infra._free = lambda p: True          # noqa: SLF001 - test seam
            infra._pids_listening = lambda p: []  # noqa: SLF001 - test seam
            try:
                first = infra.sticky_ports(td, td, 3001, 3000)
                # Second slice: ports now "busy" — but they are ours, so the
                # same pair comes back rather than drifting upward.
                second = infra.sticky_ports(td, td, 3001, 3000)
            finally:
                infra._free, infra._pids_listening = real_free, real_pids
            self.assertEqual(first, second)
            saved = json.loads(
                (pathlib.Path(td) / ".factory" / "state.json").read_text())
            self.assertEqual(saved["ports"], {"api": first[0], "web": first[1]})

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


class LiveLogAccountingTests(unittest.TestCase):
    """Two accounting bugs from the barcode run: a rerun truncated the log that
    held slice 1's $48.12 (reporting $0.00), and the ledger only updated at
    slice exit, so mid-run it reported $114 while the logs said $146 — and the
    stale figure was quoted as fact for hours."""

    LINE = "[10:00:00] ✓ implementer done in 100s ($12.34)\n"

    def test_reset_archives_instead_of_truncating(self) -> None:
        from project_factory import livelog
        with tempfile.TemporaryDirectory() as td:
            log = livelog.path_for(td, "s1")
            log.write_text(self.LINE)
            livelog.reset(td, "s1")                     # a new generation starts
            self.assertEqual(log.read_text(), "")
            self.assertTrue((log.parent / "s1.log.1").exists())

    def test_cost_spans_generations(self) -> None:
        from project_factory import livelog
        with tempfile.TemporaryDirectory() as td:
            log = livelog.path_for(td, "s1")
            log.write_text(self.LINE)
            livelog.reset(td, "s1")
            log.write_text(self.LINE)                   # g2 costs the same again
            self.assertEqual(livelog.live_log_cost(td, "s1"), 24.68)
            self.assertEqual(
                livelog.live_log_cost(td, "s1", include_archived=False), 12.34)

    def test_spent_usd_prefers_the_higher_of_ledger_and_logs(self) -> None:
        from project_factory import livelog
        with tempfile.TemporaryDirectory() as td:
            board = {"id": "b", "name": "B", "business_events": [], "flows": []}
            pdir = pathlib.Path(td) / "proj"
            (pdir / "specs").mkdir(parents=True)
            (pdir / "specs" / "b.board.json").write_text(json.dumps(board))
            (pdir / "specs" / "project-plan.json").write_text(
                json.dumps({"slices": [], "out_of_scope": [], "approved_by": None}))
            livelog.path_for(str(pdir), "s1").write_text(self.LINE)   # $12.34 live
            project = cfgmod.discover("proj", workspace=td)
            project.record_slice_cost("s1", 5.0)        # ledger lags behind
            self.assertEqual(project.spent_usd(), 12.34)
            project.record_slice_cost("s1", 20.0)       # ledger ahead (log lost)
            self.assertEqual(project.spent_usd(), 20.0)


class DigestFailuresTests(unittest.TestCase):
    """Four browser projects report one root cause four times. The digest keeps
    every DISTINCT failure and drops the repetition — that repetition is what
    the diagnostician was being charged to read."""

    RAW = """
[1/24] [chromium] › upload.test.ts:10:1 › WEB-001: passing test should not appear
  2 failed
    [chromium] › scan.test.ts:5:1 › WEB-002: unmatched barcode
    [webkit] › scan.test.ts:5:1 › WEB-002: unmatched barcode
  1) [chromium] › scan.test.ts:5:1 › WEB-002: unmatched barcode
    Error: expect(locator).toBeVisible() failed
    Locator: getByText('Cotton Crew Tee')
    Timeout: 5000ms
  2) [webkit] › scan.test.ts:5:1 › WEB-002: unmatched barcode
    Error: expect(locator).toBeVisible() failed
    Locator: getByText('Cotton Crew Tee')
    Timeout: 5000ms
  30 passed (2.0m)
"""

    def test_collapses_the_same_error_across_browsers(self) -> None:
        from project_factory.harness import digest_failures
        digest = digest_failures(self.RAW)
        self.assertIn("[x2]", digest)                     # both browsers collapsed
        self.assertEqual(digest.count("Cotton Crew Tee"), 1)
        self.assertIn("30 passed", digest)                 # summary retained

    def test_excludes_passing_progress_lines(self) -> None:
        from project_factory.harness import digest_failures
        digest = digest_failures(self.RAW)
        self.assertNotIn("should not appear", digest)

    def test_unrecognised_output_falls_back_to_a_tail(self) -> None:
        """An unfamiliar reporter must degrade to today's behaviour, never to
        an empty prompt."""
        from project_factory.harness import digest_failures
        raw = "something completely different\n" * 50
        self.assertIn("something completely different", digest_failures(raw))


class InfraContractTests(unittest.TestCase):
    """The regression this exists for: slice 3's implementer added
    `--port 3000` to the web dev script, pinning the app to a port the factory
    had not allocated. The health check then passed against whatever else was
    listening, and two slices burned their diagnose budgets — roughly eight
    hours — testing web pages against a REST API."""

    def _repo(self, td: str):
        def git(*args: str) -> subprocess.CompletedProcess:
            return subprocess.run(
                ["git", "-C", td, "-c", "user.name=t", "-c", "user.email=t@t", *args],
                capture_output=True, text=True, check=True)
        git("init", "-b", "main")
        (pathlib.Path(td) / "apps" / "web").mkdir(parents=True)
        (pathlib.Path(td) / "apps" / "web" / "package.json").write_text(
            '{\n  "scripts": {\n    "dev": "next dev"\n  }\n}\n')
        (pathlib.Path(td) / "apps" / "web" / "vitest.config.ts").write_text("export default {}\n")
        git("add", "-A"); git("commit", "-m", "base")
        return git

    def test_pinning_the_dev_port_is_a_violation(self) -> None:
        from project_factory.harness import check_infra_contract
        with tempfile.TemporaryDirectory() as td:
            self._repo(td)
            pkg = pathlib.Path(td) / "apps" / "web" / "package.json"
            pkg.write_text(pkg.read_text().replace('"next dev"', '"next dev --port 3000"'))
            result = check_infra_contract(td)
            self.assertFalse(result.ok)
            self.assertTrue(any("--port 3000" in e for e in result.errors), result.errors)

    def test_other_config_edits_are_reported_not_blocked(self) -> None:
        """Serialising vitest for a shared test database was a GOOD agent edit.
        Blocking it would cost more than it saves — it is surfaced instead."""
        from project_factory.harness import check_infra_contract
        with tempfile.TemporaryDirectory() as td:
            self._repo(td)
            cfg = pathlib.Path(td) / "apps" / "web" / "vitest.config.ts"
            cfg.write_text("export default { test: { fileParallelism: false } }\n")
            result = check_infra_contract(td)
            self.assertTrue(result.ok, result.errors)
            self.assertIn("apps/web/vitest.config.ts", result.data["notable"])

    def test_untouched_repo_is_clean(self) -> None:
        from project_factory.harness import check_infra_contract
        with tempfile.TemporaryDirectory() as td:
            self._repo(td)
            result = check_infra_contract(td)
            self.assertTrue(result.ok)
            self.assertEqual(result.data["notable"], [])


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


class PromptPortabilityTests(unittest.TestCase):
    """
    The factory is generic; only the IR and run.json carry a domain. The guard
    caught three real leaks the moment it was first run, all introduced the
    same day by someone (me) writing a worked example straight from the last
    project into a prompt. It now also runs in CI (.github/workflows) and
    covers the prose surfaces agents read for orientation — the biggest leak
    ever found was in a YAML exemplar, not a .py file.
    """

    def test_no_domain_nouns_in_prompts_or_prose(self) -> None:
        r = harness.check_prompt_leak("project_factory",
                                      surfaces=harness.PROMPT_SURFACES)
        self.assertTrue(r.ok, "domain vocabulary leaked into factory code:\n  "
                              + "\n  ".join(r.errors))

    def test_the_guard_can_still_fail(self) -> None:
        """A guard that cannot fail is decoration. Point it at a fixture that
        does leak and confirm it says so — otherwise a refactor that quietly
        breaks the scan would look like a permanent pass."""
        with tempfile.TemporaryDirectory() as td:
            pathlib.Path(td, "leaky.py").write_text(
                'PROMPT = "every warehouse has a packing list"\n')
            r = harness.check_prompt_leak(td)
            self.assertFalse(r.ok)
            self.assertTrue(any("leaky.py" in e for e in r.errors), r.errors)

    def test_comments_are_not_leaks(self) -> None:
        """Comments never reach a model, and excluding them is what keeps the
        check precise enough to be trusted — 'container' is a domain noun AND
        a Docker term."""
        with tempfile.TemporaryDirectory() as td:
            pathlib.Path(td, "commented.py").write_text(
                '# the barcode project proved this: a warehouse has "packing lists"\n'
                'X = 1\n')
            self.assertTrue(harness.check_prompt_leak(td).ok)

    def test_prose_surface_catches_a_noun(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            md = pathlib.Path(td, "runbook.md")
            md.write_text("Remember that every warehouse gets a packing list.\n")
            r = harness.check_prompt_leak(td, surfaces=[td])
            self.assertFalse(r.ok)
            self.assertTrue(any("runbook.md" in e for e in r.errors), r.errors)

    def test_prose_code_spans_are_pointers_not_leaks(self) -> None:
        """A `projects/<name>/repo` path in a code span is a pointer an agent
        follows, not prose it imitates — flagging it would force runbooks to
        lie about where things are."""
        with tempfile.TemporaryDirectory() as td:
            pathlib.Path(td, "runbook.md").write_text(
                "Reference fixes live in `projects/barcode-mvp/repo`.\n"
                "```\ncd projects/barcode-mvp/repo && git log\n```\n")
            self.assertTrue(harness.check_prompt_leak(td, surfaces=[td]).ok)

    def test_case_study_marker_exempts_a_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            pathlib.Path(td, "history.md").write_text(
                harness.CASE_STUDY_MARKER
                + "\nProject #1 sorted warehouse containers by barcode.\n")
            self.assertTrue(harness.check_prompt_leak(td, surfaces=[td]).ok)

    def test_exemplar_is_fictional_and_carries_its_canaries(self) -> None:
        """The bundled exemplar must never re-acquire a past project's
        vocabulary, and must keep the canary nouns the oracle leak check
        depends on — lose those and copied exemplar content becomes
        undetectable again."""
        text = (pathlib.Path("project_factory") / "exemplars"
                / "scenarios-exemplar.yaml").read_text()
        for noun in harness.DOMAIN_NOUNS:
            self.assertFalse(harness.mentions_noun(noun, text),
                             f"past-project noun '{noun}' is back in the exemplar")
        self.assertTrue(any(harness.mentions_noun(n, text)
                            for n in harness.EXEMPLAR_NOUNS))


class OracleLeakTests(unittest.TestCase):
    """
    The output-side half of portability: validate_oracle must reject a draft
    that echoes vocabulary from the exemplar or another project when this
    project's board doesn't use it — the schema checks can't see that, and
    Gate A skims.
    """

    BOARD_TEXT = json.dumps(EVENTS)  # has container/packing list/barcode/scan

    def test_board_vocabulary_is_allowed(self) -> None:
        # _oracle_yaml speaks project #1's language; its board does too.
        errors = planner.validate_oracle(_oracle_yaml(), PLANNED,
                                         board_text=self.BOARD_TEXT)
        self.assertEqual(errors, [])

    def test_exemplar_canary_is_rejected(self) -> None:
        doc = json.loads(_oracle_yaml())
        doc["scenarios"][0]["given"] = ["A folio exists with 2 specimen entries"]
        errors = planner.validate_oracle(json.dumps(doc), PLANNED,
                                         board_text=self.BOARD_TEXT)
        self.assertTrue(any("folio" in e for e in errors), errors)
        self.assertTrue(any("specimen" in e for e in errors), errors)

    def test_other_projects_nouns_are_rejected(self) -> None:
        doc = json.loads(_oracle_yaml())
        doc["scenarios"][0]["given"] = ["An invoice ledger already exists"]
        errors = planner.validate_oracle(
            json.dumps(doc), PLANNED, board_text=self.BOARD_TEXT,
            extra_nouns=["invoice ledger"])
        self.assertTrue(any("invoice ledger" in e for e in errors), errors)

    def test_starter_block_is_required_verbatim(self) -> None:
        errors = planner.validate_oracle(_oracle_yaml(), PLANNED,
                                         require_starter=True)
        self.assertTrue(any("starter_constraints" in e for e in errors), errors)

        doc = json.loads(_oracle_yaml())
        doc["starter_constraints"] = planner.starter_constraints()
        self.assertEqual(planner.validate_oracle(json.dumps(doc), PLANNED,
                                                 require_starter=True), [])

    def test_collect_domain_nouns_harvests_boards(self) -> None:
        board = {"business_events": [
            {"name": "Invoice ledger reconciled",
             "bounded_context": "Billing",
             "aggregate": {"label": "InvoiceLedger"}},
        ]}
        with tempfile.TemporaryDirectory() as td:
            specs = pathlib.Path(td, "projA", "specs")
            specs.mkdir(parents=True)
            (specs / "a.board.json").write_text(json.dumps(board))
            nouns = harness.collect_domain_nouns(td)
            self.assertIn("invoice", nouns)
            self.assertIn("reconciled", nouns)
            self.assertNotIn("event", nouns)
            # the project being drafted is excluded — its own vocabulary is
            # exactly what its oracle SHOULD use
            self.assertEqual(
                harness.collect_domain_nouns(td, exclude_project="projA"), [])


class RateLimitDetection(unittest.TestCase):
    """
    A rate limit and a broken generation both arrive as a non-zero CLI exit.
    Telling them apart is what stops an unattended run from printing "crashed
    mid-node — re-run to resume" at 3am and then looping into the same wall.
    """

    def test_account_limit_is_recognised(self):
        from project_factory.models import _rate_limit_reason
        for text in (
            "Claude usage limit reached. Your limit will reset at 4:00 PM.",
            "API Error: 429 Too Many Requests",
            "overloaded_error: server is overloaded",
            "Your credit balance is too low to access the Anthropic API",
        ):
            self.assertIsNotNone(_rate_limit_reason(text), text)

    def test_reason_carries_the_reset_context(self):
        from project_factory.models import _rate_limit_reason
        why = _rate_limit_reason(
            "Claude usage limit reached. Your limit will reset at 4:00 PM.")
        self.assertIn("reset", why)

    def test_ordinary_failures_are_not_mistaken_for_limits(self):
        """The costly false positive: a real defect silently reported as a
        pause would leave the operator waiting for a window that was never
        the problem."""
        from project_factory.models import _rate_limit_reason
        for text in (
            "TypeError: Cannot read properties of undefined (reading 'id')",
            "1 failed\n  [chromium] > scan-screen.test.ts:228 > WEB-004",
            "error TS2345: Argument of type 'string' is not assignable",
            "Error: connect ECONNREFUSED 127.0.0.1:3001",
            "prisma migrate failed: relation \"Container\" already exists",
        ):
            self.assertIsNone(_rate_limit_reason(text), text)

    def test_rate_limited_is_a_runtime_error_but_distinguishable(self):
        """It must stay catchable by existing `except RuntimeError` arms —
        those are the crash-reporting safety nets — while the RateLimited arm
        placed ABOVE them wins."""
        from project_factory.models import RateLimited
        self.assertTrue(issubclass(RateLimited, RuntimeError))


if __name__ == "__main__":
    unittest.main()
