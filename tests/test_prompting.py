import unittest

from project_factory.prompting import render_scenarios


def _scenario(i: int, **overrides) -> dict:
    # Mirrors the real schema seeded by config.seeded_scenarios_yaml:
    # id / title / traces_to / given / when / then.
    sc = {
        "id": f"SC-{i:03d}",
        "title": f"Scenario {i}",
        "traces_to": [f"EV-{i}"],
        "given": ["a signed-in customer"],
        "when": "POST /orders with a valid payload",
        "then": ["HTTP 201", "the order is persisted"],
    }
    sc.update(overrides)
    return sc


class RenderScenariosTests(unittest.TestCase):
    def test_renders_full_oracle_content(self) -> None:
        scenarios = [
            _scenario(1, given=["a signed-in customer", "an empty cart"]),
            _scenario(2, title="Malformed payload rejected",
                      when="POST /orders with a missing sku",
                      then=["HTTP 422"]),
        ]

        out = render_scenarios(scenarios)

        # Every field the downstream agents assert on must survive verbatim:
        # ids (check_traceability), HTTP codes in then-clauses (check_contract),
        # and the given/when/then text the test author transcribes.
        for needle in [
            "SC-001", "SC-002",
            "an empty cart", "a signed-in customer",
            "POST /orders with a valid payload",
            "POST /orders with a missing sku",
            "HTTP 201", "HTTP 422", "the order is persisted",
            "EV-1", "EV-2",
        ]:
            self.assertIn(needle, out)

    def test_never_drops_items(self) -> None:
        scenarios = [_scenario(i) for i in range(1, 41)]

        out = render_scenarios(scenarios)

        for i in range(1, 41):
            self.assertIn(f"SC-{i:03d}", out)

    def test_unknown_keys_survive(self) -> None:
        out = render_scenarios([_scenario(1, screen="/orders/new",
                                          notes=["flaky on CI"])])

        self.assertIn("screen: /orders/new", out)
        self.assertIn("notes: flaky on CI", out)

    def test_empty_and_none(self) -> None:
        self.assertEqual(render_scenarios([]), "")
        self.assertEqual(render_scenarios(None), "")

    def test_is_more_compact_than_yaml(self) -> None:
        import yaml

        scenarios = [_scenario(i) for i in range(1, 11)]
        self.assertLess(len(render_scenarios(scenarios)),
                        len(yaml.safe_dump(scenarios, sort_keys=False)))


if __name__ == "__main__":
    unittest.main()
