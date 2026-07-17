#!/usr/bin/env python3
import json
import tempfile
import unittest
from pathlib import Path

import demo_weight_provisioning as demo
import model_manifest as mm


class DemoWeightProvisioningTests(unittest.TestCase):
   def manifest(self):
      return mm.compile_model_manifest(
         model_id="org/model",
         requested_revision="main",
         resolved_commit="a" * 40,
         config={"model_type": "gpt2", "n_layer": 3},
         checkpoint_index={
            "weight_map": {
               "h.0.weight": "a.safetensors",
               "h.1.weight": "b.safetensors",
               "h.2.weight": "c.safetensors",
            },
         },
         file_metadata={
            "a.safetensors": {"size_bytes": 1, "sha256": "1" * 64},
            "b.safetensors": {"size_bytes": 2, "sha256": "2" * 64},
            "c.safetensors": {"size_bytes": 3, "sha256": "3" * 64},
         },
      )

   def test_node_specs_build_gap_free_v2_route_and_assignments(self):
      specs = demo.parse_node_specs([
         "m4pro,0,2,/tmp/mycelium-m4pro",
         "evis-macbook-pro-1,2,3,/tmp/mycelium-laptop",
      ])
      route, assignments = demo.build_demo_artifacts(
         self.manifest(),
         specs,
         deployment_id="12345678-1234-5678-1234-567812345678",
         deployment_epoch=1,
      )
      self.assertEqual(route["protocol"], "mycelium.manual_provisioning_route.v1")
      self.assertEqual(route["node_order"], ["m4pro", "evis-macbook-pro-1"])
      self.assertEqual(route["route"][0]["range"]["end_layer_exclusive"], 2)
      self.assertEqual(len(assignments), 2)
      self.assertEqual(assignments[1]["artifact_cache_root"], "/tmp/mycelium-laptop")

   def test_node_specs_preserve_target_local_path_without_coordinator_resolution(self):
      specs = demo.parse_node_specs([
         "linux-peer,0,3,/tmp/remote-linux-cache",
      ])
      self.assertEqual(specs[0].cache_root, "/tmp/remote-linux-cache")

   def test_node_specs_with_gap_fail_closed(self):
      specs = demo.parse_node_specs([
         "m4pro,0,1,/tmp/m4pro",
         "laptop,2,3,/tmp/laptop",
      ])
      with self.assertRaisesRegex(ValueError, "gap"):
         demo.build_demo_artifacts(
            self.manifest(),
            specs,
            deployment_id="12345678-1234-5678-1234-567812345678",
            deployment_epoch=1,
         )

   def test_write_orchestration_persists_manifest_route_and_per_node_assignments(self):
      specs = demo.parse_node_specs([
         "node-a,0,2,/tmp/node-a",
         "node-b,2,3,/tmp/node-b",
      ])
      route, assignments = demo.build_demo_artifacts(
         self.manifest(),
         specs,
         deployment_id="12345678-1234-5678-1234-567812345678",
         deployment_epoch=1,
      )
      with tempfile.TemporaryDirectory() as td:
         written = demo.write_orchestration(Path(td), self.manifest(), route, assignments)
         self.assertEqual(set(written), {"manifest", "route", "assignments", "deployment"})
         deployment_path = Path(written["deployment"])
         deployment = json.loads(deployment_path.read_text())
         self.assertEqual(deployment["route"], "manual-provisioning-route-v1.json")
         self.assertFalse((deployment_path.parent / "route-plan-v2.json").exists())
         self.assertEqual(set(deployment["assignments"]), {"node-a", "node-b"})
         artifact_references = [
            deployment["manifest"],
            deployment["route"],
            *deployment["assignments"].values(),
         ]
         for reference in artifact_references:
            self.assertFalse(Path(reference).is_absolute())
            self.assertTrue((deployment_path.parent / reference).is_file())

   def test_write_orchestration_rejects_colliding_node_filename_slugs_before_writes(self):
      specs = demo.parse_node_specs([
         "node/a,0,2,/tmp/node-a",
         "node-a,2,3,/tmp/node-b",
      ])
      manifest = self.manifest()
      route, assignments = demo.build_demo_artifacts(
         manifest,
         specs,
         deployment_id="12345678-1234-5678-1234-567812345678",
         deployment_epoch=1,
      )
      with tempfile.TemporaryDirectory() as td:
         output = Path(td) / "bundle"
         with self.assertRaisesRegex(ValueError, "filename collision"):
            demo.write_orchestration(output, manifest, route, assignments)
         self.assertFalse(output.exists())


if __name__ == "__main__":
   unittest.main()
