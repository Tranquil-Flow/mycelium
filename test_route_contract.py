#!/usr/bin/env python3
import copy
import unittest

import route_contract as rc


class RouteContractTests(unittest.TestCase):
   def v1(self):
      return {
         "ok": True,
         "protocol": "mycelium.route_plan.v1",
         "model": {
            "model_id": "org/model",
            "num_layers": 5,
            "manifest_digest": "sha256:" + "a" * 64,
            "resolved_commit": "b" * 40,
         },
         "route": [
            {"node_id": "node-a", "layers": [0, 1], "layer_count": 2},
            {"node_id": "node-b", "layers": [2, 4], "layer_count": 3},
         ],
         "node_order": ["node-a", "node-b"],
      }

   def test_upgrade_converts_inclusive_ranges_exactly_once(self):
      original = self.v1()
      before = copy.deepcopy(original)
      upgraded = rc.upgrade_legacy_route_plan_v1(original)

      self.assertEqual(original, before)
      self.assertEqual(upgraded["protocol"], "mycelium.manual_provisioning_route.v1")
      self.assertEqual(upgraded["source_protocol"], "mycelium.route_plan.v1")
      self.assertNotIn("layers", upgraded["route"][0])
      self.assertEqual(
         upgraded["route"][0]["range"],
         {"start_layer": 0, "end_layer_exclusive": 2, "layer_count": 2},
      )
      self.assertEqual(
         upgraded["route"][1]["range"],
         {"start_layer": 2, "end_layer_exclusive": 5, "layer_count": 3},
      )

   def test_model_identity_is_required(self):
      plan = {
         "ok": True,
         "protocol": "mycelium.manual_provisioning_route.v1",
         "model": {"model_id": "one", "num_layers": 1},
         "claim_boundary": "manual provisioning only",
         "route": [{
            "node_id": "only",
            "range": {"start_layer": 0, "end_layer_exclusive": 1, "layer_count": 1},
         }],
      }
      with self.assertRaisesRegex(ValueError, "manifest_digest"):
         rc.validate_manual_provisioning_route_v1(plan)

   def test_one_layer_and_full_model_ranges_validate(self):
      one = {
         "ok": True,
         "protocol": "mycelium.manual_provisioning_route.v1",
         "model": {
            "model_id": "one",
            "num_layers": 1,
            "manifest_digest": "sha256:" + "a" * 64,
            "resolved_commit": "b" * 40,
         },
         "claim_boundary": "manual provisioning only",
         "route": [{
            "node_id": "only",
            "range": {"start_layer": 0, "end_layer_exclusive": 1, "layer_count": 1},
         }],
      }
      rc.validate_manual_provisioning_route_v1(one)

      full = copy.deepcopy(one)
      full["model"] = {
         "model_id": "full",
         "num_layers": 5,
         "manifest_digest": "sha256:" + "c" * 64,
         "resolved_commit": "d" * 40,
      }
      full["route"][0]["range"] = {
         "start_layer": 0,
         "end_layer_exclusive": 5,
         "layer_count": 5,
      }
      rc.validate_manual_provisioning_route_v1(full)

      missing_claim = copy.deepcopy(one)
      del missing_claim["claim_boundary"]
      with self.assertRaisesRegex(ValueError, "claim_boundary"):
         rc.validate_manual_provisioning_route_v1(missing_claim)

   def test_gap_fails_closed(self):
      broken = self.v1()
      broken["route"][1]["layers"] = [3, 4]
      broken["route"][1]["layer_count"] = 2
      with self.assertRaisesRegex(ValueError, "gap"):
         rc.upgrade_legacy_route_plan_v1(broken)

   def test_overlap_fails_closed(self):
      broken = self.v1()
      broken["route"][1]["layers"] = [1, 4]
      broken["route"][1]["layer_count"] = 4
      with self.assertRaisesRegex(ValueError, "overlap"):
         rc.upgrade_legacy_route_plan_v1(broken)

   def test_bad_layer_count_fails_closed(self):
      broken = self.v1()
      broken["route"][0]["layer_count"] = 1
      with self.assertRaisesRegex(ValueError, "layer_count"):
         rc.upgrade_legacy_route_plan_v1(broken)


if __name__ == "__main__":
   unittest.main()
