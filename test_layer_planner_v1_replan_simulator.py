import json
import tempfile
import unittest
from pathlib import Path

from mycelium_layer_planner.replan_simulator import simulate_bundle
from test_layer_planner_v1_planner import snapshot


ROOT = Path(__file__).resolve().parent


class ReplanSimulatorTests(unittest.TestCase):
    def test_bundle_reports_expected_recovery_tiers(self):
        report = simulate_bundle(ROOT / "scenarios/product-v1-replanning.json")
        self.assertEqual(report["protocol"], "mycelium.layer_replan_simulation_report.v1")
        self.assertEqual(report["handoff_state"], "placement_intent_only")
        self.assertEqual(
            [case["action"] for case in report["cases"]],
            [
                "existing_track_intent",
                "full_replan",
                "existing_track_intent",
                "candidate_replan",
            ],
        )
        self.assertEqual(
            report["cases"][1]["escalation_order"],
            ["successor_standby_candidate", "full_replan"],
        )
        self.assertEqual(
            report["cases"][1]["recommendation"],
            "prefer_route_ready_successor_standby_else_provision_candidate",
        )
        self.assertEqual(report["cases"][3]["recommendation"], "provision_candidate")
        self.assertGreater(report["cases"][3]["capacity_gain_fraction"], 0.05)
        for case in report["cases"]:
            self.assertNotIn("runtime_ready", repr(case).lower())
            self.assertNotIn("failover_ready", repr(case).lower())

    def test_bundle_rejects_base_snapshot_path_traversal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scenario_dir = root / "scenario"
            scenario_dir.mkdir()
            (root / "outside.json").write_text(json.dumps(snapshot(6)), encoding="utf-8")
            bundle = {
                "protocol": "mycelium.layer_replan_simulation.v1",
                "base_snapshot": "../outside.json",
                "cases": [
                    {
                        "name": "no-op",
                        "event": {
                            "event_id": "outside",
                            "snapshot_generation": 1,
                            "kind": "device_unavailable",
                            "node_ids": ["not-in-plan"],
                            "edges": [],
                        },
                    }
                ],
            }
            path = scenario_dir / "bundle.json"
            path.write_text(json.dumps(bundle), encoding="utf-8")
            with self.assertRaises(ValueError):
                simulate_bundle(path)

    def test_bundle_is_deterministic(self):
        path = ROOT / "scenarios/product-v1-replanning.json"
        self.assertEqual(simulate_bundle(path), simulate_bundle(path))


if __name__ == "__main__":
    unittest.main()
