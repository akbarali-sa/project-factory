"""
The project data backbone (schema_architect) and the redeclaration drop
guard. Both exist because of the same barcode-v2 failure surface: per-slice
schema ownership let cross-slice data decisions be made by whichever slice
moved first, and redeclare-to-modify made every add-a-back-relation a
wholesale model rewrite where one dropped line silently deletes a prior
wave's column.
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

from project_factory import config as cfgmod
from project_factory import harness
from project_factory import planner

from test_backlog_input import _project, _tree

SLICES = ["slice_ingestion", "slice_scanning"]

VALID = """\
entities:
  - name: Container
    purpose: A registered shipping container.
    owned_by: slice_ingestion
    business_key: containerNumber
    relations:
      - to: ItemRow
        cardinality: one_to_many
        on_parent_delete: cascade
  - name: ItemRow
    purpose: One row of an accepted packing list.
    owned_by: slice_ingestion
    business_key: null
read_models:
  - name: ContainerProgress
    purpose: Per-barcode counting progress.
    owned_by: slice_scanning
    derived_from: [Container, ItemRow]
"""


class BackboneValidationTests(unittest.TestCase):
    def test_valid_backbone_passes(self) -> None:
        self.assertEqual(planner._validate_backbone(VALID, SLICES), [])

    def test_unknown_owner_fails(self) -> None:
        errors = planner._validate_backbone(
            VALID.replace("owned_by: slice_scanning", "owned_by: slice_ghost"),
            SLICES)
        self.assertTrue(any("slice_ghost" in e for e in errors), errors)

    def test_relation_to_undeclared_entity_fails(self) -> None:
        errors = planner._validate_backbone(
            VALID.replace("to: ItemRow", "to: Warehouse"), SLICES)
        self.assertTrue(any("Warehouse" in e for e in errors), errors)

    def test_bad_cardinality_and_delete_fail(self) -> None:
        errors = planner._validate_backbone(
            VALID.replace("one_to_many", "1:n")
                 .replace("on_parent_delete: cascade",
                          "on_parent_delete: nuke"), SLICES)
        self.assertTrue(any("cardinality" in e for e in errors), errors)
        self.assertTrue(any("on_parent_delete" in e for e in errors), errors)

    def test_read_model_from_unknown_entity_fails(self) -> None:
        errors = planner._validate_backbone(
            VALID.replace("derived_from: [Container, ItemRow]",
                          "derived_from: [CountEntry]"), SLICES)
        self.assertTrue(any("CountEntry" in e for e in errors), errors)

    def test_empty_entities_fails(self) -> None:
        self.assertEqual(planner._validate_backbone("entities: []", SLICES),
                         ["missing or empty top-level entities list"])

    def test_non_pascal_name_fails(self) -> None:
        errors = planner._validate_backbone(
            VALID.replace("name: ItemRow", "name: item_row"), SLICES)
        self.assertTrue(any("PascalCase" in e for e in errors), errors)


class DraftBackboneTests(unittest.TestCase):
    def test_prompt_is_a_string_and_file_lands(self) -> None:
        """End-to-end through draft_backbone with the agent stubbed — same
        prompt-construction guard as DraftAssumptionsTests (a stray trailing
        comma once made a prompt a tuple and killed Popen mid-run)."""
        captured: dict = {}

        def fake_claude(agent, prompt, **kw):
            captured["agent"], captured["prompt"] = agent, prompt
            return VALID

        with tempfile.TemporaryDirectory() as td:
            project = _project(td)
            (project.dir / "specs" / "project-plan.json").write_text(
                json.dumps({"slices": [
                    {"id": "slice_ingestion", "name": "Ingestion", "wave": 1,
                     "bounded_context": "Packing List Ingestion",
                     "event_ids": ["be_uploaded", "be_registered"]},
                    {"id": "slice_scanning", "name": "Scanning", "wave": 2,
                     "bounded_context": "Warehouse Scanning",
                     "event_ids": ["be_scanned", "be_synced"]},
                ], "out_of_scope": [],
                    "approved_by": "test", "approved_at": "2026-08-12"}))
            orig = planner.claude
            planner.claude = fake_claude
            try:
                path = planner.draft_backbone(project)
            finally:
                planner.claude = orig
            self.assertIsInstance(captured["prompt"], str)
            self.assertEqual(captured["agent"], "schema_architect")
            # The plan's slices and this slice's events reached the prompt.
            self.assertIn("slice_scanning", captured["prompt"])
            self.assertIn("be_scanned", captured["prompt"])
            # Parked/deferred events are named only in the UNPRICED warning,
            # never offered as events to model (the digest carries "id":).
            self.assertNotIn('"id": "be_miscount_fixed"', captured["prompt"])
            self.assertIn("model NO entity", captured["prompt"])
            data = __import__("yaml").safe_load(
                path.read_text().split("\n\n", 1)[1])
            self.assertEqual(data["entities"][0]["name"], "Container")
            # never overwrites: second call returns without the agent
            planner.claude = None  # would explode if called
            try:
                self.assertEqual(planner.draft_backbone(project), path)
            finally:
                planner.claude = orig


BASE_SCHEMA = """\
model User {
  id    String @id @default(cuid())
  email String @unique
  /// doc comment that may change freely
  name  String

  @@index([email])
}

enum Role {
  ADMIN
  OPERATOR
}
"""


class RedeclarationDropTests(unittest.TestCase):
    def test_pure_addition_is_fine(self) -> None:
        new = ("model User {\n  id    String @id @default(cuid())\n"
               "  email String @unique\n  name  String\n"
               "  role  Role   @default(OPERATOR)\n}\n")
        self.assertEqual(harness.redeclaration_drops(BASE_SCHEMA, new), [])

    def test_dropped_field_is_an_error(self) -> None:
        new = ("model User {\n  id    String @id @default(cuid())\n"
               "  email String @unique\n}\n")
        errors = harness.redeclaration_drops(BASE_SCHEMA, new)
        self.assertEqual(len(errors), 1)
        self.assertIn("['name']", errors[0])

    def test_dropped_enum_value_is_an_error(self) -> None:
        errors = harness.redeclaration_drops(
            BASE_SCHEMA, "enum Role {\n  ADMIN\n}\n")
        self.assertEqual(len(errors), 1)
        self.assertIn("['OPERATOR']", errors[0])

    def test_new_model_is_fine(self) -> None:
        self.assertEqual(
            harness.redeclaration_drops(
                BASE_SCHEMA, "model Container {\n  id String @id\n}\n"),
            [])

    def test_doc_comment_and_attribute_changes_are_fine(self) -> None:
        new = ("model User {\n  id    String @id @default(cuid())\n"
               "  email String @unique\n  name  String\n\n"
               "  @@index([name])\n}\n")
        self.assertEqual(harness.redeclaration_drops(BASE_SCHEMA, new), [])

    def test_drop_guard_wired_into_check_contract(self) -> None:
        """check_contract must surface the drop before prisma validate ever
        sees the (perfectly valid) post-drop schema."""
        with tempfile.TemporaryDirectory() as td:
            schema = pathlib.Path(td) / "apps/api/prisma/schema.prisma"
            schema.parent.mkdir(parents=True)
            schema.write_text(BASE_SCHEMA)
            contract = ("```prisma\nmodel User {\n  id String @id\n}\n```\n"
                        "```yaml\npaths: {}\n```\n")
            res = harness.check_contract(
                contract, {"aggregates": [], "scenarios": []}, td)
            self.assertFalse(res.ok)
            self.assertTrue(
                any("drops existing member" in e for e in res.errors),
                res.errors)


if __name__ == "__main__":
    unittest.main()
