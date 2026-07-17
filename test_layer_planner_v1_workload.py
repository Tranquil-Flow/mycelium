import unittest

from mycelium_layer_planner.workload import (
    ScenarioMetrics,
    WorkloadProfile,
    empirical_interactive_chat,
    expected_response_ms,
    select_robust_plan,
)
from mycelium_layer_planner.contracts import WorkloadScenario


class WorkloadTests(unittest.TestCase):
    def test_empirical_preset_keeps_token_basis_and_load_sweep(self):
        profile = empirical_interactive_chat(user_scale=2.0, system_prefix_tokens=100)
        self.assertEqual(profile.source, "LMSYS-Chat-1M")
        self.assertEqual(tuple(s.concurrency for s in profile.scenarios), (1, 2, 4, 8, 16, 32))
        self.assertTrue(all(s.prompt_tokens == 70 and s.output_tokens == 215 for s in profile.scenarios))
        self.assertTrue(all(s.user_scale == 2.0 for s in profile.scenarios))
        self.assertTrue(all(s.effective_prompt_tokens == 170 for s in profile.scenarios))

    def test_expected_response_formula(self):
        self.assertEqual(expected_response_ms(100, 5, 10), 150)

    def test_probability_mode_requires_sum_one(self):
        with self.assertRaises(ValueError):
            WorkloadProfile(
                "bad",
                (WorkloadScenario("a", 1, 1, 1, 0.2), WorkloadScenario("b", 1, 1, 1, 0.2)),
                mode="probability",
                source="test",
            )

    def test_grid_is_not_silently_weighted(self):
        profile = empirical_interactive_chat()
        self.assertEqual(profile.mode, "sensitivity_grid")
        self.assertTrue(all(s.probability is None for s in profile.scenarios))

    def test_robust_selection_modes_are_deterministic(self):
        profile = WorkloadProfile(
            "p",
            (WorkloadScenario("s1", 10, 10, 1, 0.5), WorkloadScenario("s2", 10, 10, 2, 0.5)),
            mode="probability",
            source="test",
        )
        metrics = {
            "balanced": (ScenarioMetrics("s1", 1, 1, 8, 1), ScenarioMetrics("s2", 1, 1, 8, 1)),
            "spiky": (ScenarioMetrics("s1", 1, 1, 12, 1), ScenarioMetrics("s2", 1, 1, 2, 1)),
        }
        self.assertEqual(select_robust_plan(metrics, profile, "expected_value")[0], "balanced")
        self.assertEqual(select_robust_plan(metrics, profile, "worst_case")[0], "balanced")


if __name__ == "__main__":
    unittest.main()
