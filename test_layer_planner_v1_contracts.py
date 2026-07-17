import json
import math
import tempfile
import unittest
from pathlib import Path

from mycelium_layer_planner.contracts import (
    DirectedLinkObservation,
    LayerRange,
    LegalTrack,
    ModelIdentity,
    NodeCapability,
    PlanningPolicy,
    SpeculativeConfig,
    StagePlacement,
    WorkloadScenario,
)
from mycelium_layer_planner.model_config import load_model_identity


class ContractTests(unittest.TestCase):
    def test_layer_ranges_are_positive_half_open(self):
        self.assertEqual(LayerRange(0, 4).count, 4)
        with self.assertRaises(ValueError):
            LayerRange(3, 3)
        with self.assertRaises(ValueError):
            LayerRange(-1, 3)

    def test_model_identity_is_immutable_and_validated(self):
        model = ModelIdentity(
            model_id="org/model",
            revision="0123456789abcdef",
            weight_digest="sha256:" + "a" * 64,
            architecture="Decoder",
            num_layers=32,
            hidden_size=4096,
            dtype_bytes=2,
            kv_heads=8,
            head_dim=128,
            weight_bytes=8_000_000_000,
        )
        self.assertEqual(model.activation_bytes(tokens=2, batch=3), 49152)
        with self.assertRaises(Exception):
            model.num_layers = 1

    def test_directed_links_do_not_imply_reverse_direction(self):
        ab = DirectedLinkObservation("a", "b", 20, 1, 10_000_000)
        ba = DirectedLinkObservation("b", "a", 30, 2, 5_000_000)
        self.assertNotEqual(ab.bandwidth_Bps, ba.bandwidth_Bps)
        with self.assertRaises(ValueError):
            DirectedLinkObservation("a", "a", 1, 0, 1)

    def test_workload_and_speculative_validation(self):
        workload = WorkloadScenario("chat", 70, 215, 4, probability=1.0)
        self.assertEqual(workload.total_context_tokens, 285)
        SpeculativeConfig("draft/model", "rev", 4, (0.1, 0.2, 0.3, 0.2, 0.2))
        with self.assertRaises(ValueError):
            SpeculativeConfig("draft/model", "rev", 4, (0.5, 0.5))

    def test_policy_has_no_max_nodes(self):
        policy = PlanningPolicy()
        self.assertNotIn("max_nodes", policy.__dataclass_fields__)

    def test_policy_uses_deterministic_candidate_budget(self):
        fields = PlanningPolicy.__dataclass_fields__
        self.assertIn("search_candidate_budget", fields)
        self.assertNotIn("search_budget_ms", fields)
        self.assertNotIn("random_seed", fields)
        with self.assertRaises(ValueError):
            PlanningPolicy(search_candidate_budget=0)

    def test_stage_replicas_keep_identical_ranges(self):
        primary = StagePlacement("p0", "g0", "n0", LayerRange(0, 8), True, 12.0)
        replica = StagePlacement("p1", "g0", "n1", LayerRange(0, 8), False, 10.0)
        self.assertEqual(primary.layer_range, replica.layer_range)
        track = LegalTrack("t0", (primary.placement_id,), 1.0)
        self.assertEqual(track.traffic_fraction, 1.0)

    def test_node_capability_rejects_negative_capacity(self):
        with self.assertRaises(ValueError):
            NodeCapability("n", -1, 1, 1, 1, 1, 1)

    def test_node_capability_rejects_non_finite_measurements(self):
        for invalid in (math.nan, math.inf):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                NodeCapability("n", invalid, 1, 1, 1, 1, 1)

    def test_machine_readable_model_config_loader(self):
        config = {
            "_name_or_path": "org/model",
            "architectures": ["DecoderForCausalLM"],
            "num_hidden_layers": 24,
            "hidden_size": 2048,
            "num_attention_heads": 16,
            "num_key_value_heads": 4,
            "torch_dtype": "float16",
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            model = load_model_identity(
                path,
                revision="0123456789abcdef",
                weight_digest="sha256:" + "b" * 64,
                weight_bytes=4_800_000_000,
            )
        self.assertEqual(model.num_layers, 24)
        self.assertEqual(model.head_dim, 128)
        self.assertEqual(model.activation_bytes(70), 286720)


if __name__ == "__main__":
    unittest.main()
