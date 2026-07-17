import unittest

from mycelium_layer_planner.contracts import SpeculativeConfig
from mycelium_layer_planner.speculative import score_speculative


class SpeculativeTests(unittest.TestCase):
    def test_missing_capability_disables_feature_with_target_fallback(self):
        config = SpeculativeConfig("draft", "draft-rev", 2, (0.2, 0.3, 0.5))
        result = score_speculative(config, 20, 1, 5, 1, runtime_supported=False)
        self.assertFalse(result.enabled)
        self.assertTrue(result.target_fallback)

    def test_expected_tokens_use_full_distribution(self):
        config = SpeculativeConfig("draft", "draft-rev", 3, (0.1, 0.2, 0.3, 0.4))
        result = score_speculative(config, 20, 1, 5, 1)
        self.assertAlmostEqual(result.expected_accepted_tokens, 2.0)
        self.assertNotEqual(result.draft_kv_owner, result.target_kv_owner)

    def test_low_acceptance_keeps_target_only(self):
        config = SpeculativeConfig("draft", "draft-rev", 4, (0.99, 0.01, 0, 0, 0))
        result = score_speculative(config, 10, 3, 10, 2)
        self.assertFalse(result.use_speculative)
        self.assertGreater(result.target_only_tps, result.speculative_tps)

    def test_proposal_transfer_is_control_payload_not_activation(self):
        config = SpeculativeConfig("draft", "draft-rev", 2, (0, 0, 1))
        result = score_speculative(config, 20, 1, 2, 0.1, proposal_payload_bytes=32)
        self.assertEqual(result.proposal_payload_bytes, 32)


if __name__ == "__main__":
    unittest.main()
