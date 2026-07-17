#!/usr/bin/env python3
import unittest

import layer_assignment as la
import model_manifest as mm


class LayerAssignmentTests(unittest.TestCase):
   def manifest(self):
      return mm.compile_model_manifest(
         model_id="org/model",
         requested_revision="main",
         resolved_commit="a" * 40,
         config={"model_type": "gpt2", "architectures": ["GPT2Model"], "n_layer": 3},
         checkpoint_index={
            "metadata": {"total_size": 50},
            "weight_map": {
               "wte.weight": "shard-1.safetensors",
               "h.0.attn.weight": "shard-2.safetensors",
               "h.1.attn.weight": "shard-2.safetensors",
               "h.1.mlp.weight": "shard-3.safetensors",
               "h.2.attn.weight": "shard-3.safetensors",
               "ln_f.weight": "shard-3.safetensors",
            },
         },
         file_metadata={
            "shard-1.safetensors": {"size_bytes": 10, "sha256": "1" * 64},
            "shard-2.safetensors": {"size_bytes": 20, "sha256": "2" * 64},
            "shard-3.safetensors": {"size_bytes": 30, "sha256": "3" * 64},
         },
      )

   def route(self):
      return {
         "ok": True,
         "protocol": "mycelium.route_plan.v2",
         "model": {"model_id": "org/model", "num_layers": 3},
         "route": [
            {
               "node_id": "node-a",
               "range": {"start_layer": 0, "end_layer_exclusive": 2, "layer_count": 2},
            },
            {
               "node_id": "node-b",
               "range": {"start_layer": 2, "end_layer_exclusive": 3, "layer_count": 1},
            },
         ],
         "node_order": ["node-a", "node-b"],
      }

   def compile(self):
      return la.compile_layer_assignments(
         route_plan=self.route(),
         manifest=self.manifest(),
         deployment_id="12345678-1234-5678-1234-567812345678",
         deployment_epoch=1,
         cache_roots={
            "node-a": "/tmp/mycelium-node-a",
            "node-b": "/tmp/mycelium-node-b",
         },
         runtime_by_node={
            "node-a": {"backend": "artifact_verifier", "dtype": "source", "quantization": "none"},
            "node-b": {"backend": "artifact_verifier", "dtype": "source", "quantization": "none"},
         },
      )

   def test_assignment_uses_minimal_covering_upstream_shards(self):
      assignments = {item["node_id"]: item for item in self.compile()}
      node_a = assignments["node-a"]
      node_b = assignments["node-b"]

      self.assertEqual(node_a["protocol"], "mycelium.layer_assignment.v1")
      self.assertEqual([f["path"] for f in node_a["files"]], [
         "shard-2.safetensors",
         "shard-3.safetensors",
      ])
      self.assertEqual([f["path"] for f in node_b["files"]], ["shard-3.safetensors"])
      self.assertEqual(node_a["expected_tensor_prefixes"], ["h.0.", "h.1."])
      self.assertEqual(node_b["expected_tensor_prefixes"], ["h.2."])
      self.assertIn("h.1.mlp.weight", node_a["expected_tensor_keys"])
      self.assertEqual(node_a["resolved_commit"], "a" * 40)
      self.assertEqual(node_a["manifest_digest"], mm.manifest_digest_ref(self.manifest()))
      self.assertFalse(node_a["route_ready"])

   def test_assignments_are_deterministic_for_same_deployment(self):
      first = self.compile()
      second = self.compile()
      self.assertEqual(
         [item["assignment_id"] for item in first],
         [item["assignment_id"] for item in second],
      )

   def test_assignment_identity_detects_semantic_tampering(self):
      assignment = self.compile()[0]
      assignment["files"][0]["size_bytes"] += 1
      with self.assertRaisesRegex(ValueError, "assignment_id"):
         la.validate_assignment_identity(assignment)

   def test_assignment_identity_validation_rejects_route_ready_claim(self):
      assignment = self.compile()[0]
      assignment["route_ready"] = True
      with self.assertRaisesRegex(ValueError, "route_ready"):
         la.validate_assignment_identity(assignment)

   def test_compile_accepts_allocator_v1_route_through_single_upgrade_boundary(self):
      route = {
         "ok": True,
         "protocol": "mycelium.route_plan.v1",
         "model": {"model_id": "org/model", "num_layers": 3},
         "route": [
            {"node_id": "node-a", "layers": [0, 1], "layer_count": 2},
            {"node_id": "node-b", "layers": [2, 2], "layer_count": 1},
         ],
         "node_order": ["node-a", "node-b"],
      }
      assignments = la.compile_layer_assignments(
         route_plan=route,
         manifest=self.manifest(),
         deployment_id="12345678-1234-5678-1234-567812345678",
         deployment_epoch=1,
         cache_roots={"node-a": "/tmp/a", "node-b": "/tmp/b"},
         runtime_by_node={
            "node-a": {"backend": "artifact_verifier", "dtype": "source", "quantization": "none"},
            "node-b": {"backend": "artifact_verifier", "dtype": "source", "quantization": "none"},
         },
      )
      self.assertEqual(assignments[0]["range"]["end_layer_exclusive"], 2)
      self.assertEqual(assignments[1]["range"]["start_layer"], 2)

   def test_target_cache_root_is_preserved_for_remote_peer(self):
      assignments = la.compile_layer_assignments(
         route_plan=self.route(),
         manifest=self.manifest(),
         deployment_id="12345678-1234-5678-1234-567812345678",
         deployment_epoch=1,
         cache_roots={"node-a": "/tmp/remote-a", "node-b": "/var/tmp/remote-b"},
         runtime_by_node={
            "node-a": {"backend": "artifact_verifier", "dtype": "source", "quantization": "none"},
            "node-b": {"backend": "artifact_verifier", "dtype": "source", "quantization": "none"},
         },
      )
      self.assertEqual(assignments[0]["artifact_cache_root"], "/tmp/remote-a")
      self.assertEqual(assignments[1]["artifact_cache_root"], "/var/tmp/remote-b")

   def test_noncanonical_target_cache_root_fails_closed(self):
      with self.assertRaisesRegex(ValueError, "canonical"):
         la.compile_layer_assignments(
            route_plan=self.route(),
            manifest=self.manifest(),
            deployment_id="12345678-1234-5678-1234-567812345678",
            deployment_epoch=1,
            cache_roots={"node-a": "/tmp/../escape", "node-b": "/tmp/node-b"},
            runtime_by_node={
               "node-a": {"backend": "artifact_verifier", "dtype": "source", "quantization": "none"},
               "node-b": {"backend": "artifact_verifier", "dtype": "source", "quantization": "none"},
            },
         )

   def test_relative_cache_root_fails_closed(self):
      with self.assertRaisesRegex(ValueError, "absolute"):
         la.compile_layer_assignments(
            route_plan=self.route(),
            manifest=self.manifest(),
            deployment_id="12345678-1234-5678-1234-567812345678",
            deployment_epoch=1,
            cache_roots={"node-a": "relative", "node-b": "/tmp/node-b"},
            runtime_by_node={
               "node-a": {"backend": "artifact_verifier", "dtype": "source", "quantization": "none"},
               "node-b": {"backend": "artifact_verifier", "dtype": "source", "quantization": "none"},
            },
         )

   def test_manifest_model_mismatch_fails_closed(self):
      route = self.route()
      route["model"]["model_id"] = "other/model"
      with self.assertRaisesRegex(ValueError, "model_id"):
         la.compile_layer_assignments(
            route_plan=route,
            manifest=self.manifest(),
            deployment_id="12345678-1234-5678-1234-567812345678",
            deployment_epoch=1,
            cache_roots={"node-a": "/tmp/a", "node-b": "/tmp/b"},
            runtime_by_node={
               "node-a": {"backend": "artifact_verifier", "dtype": "source", "quantization": "none"},
               "node-b": {"backend": "artifact_verifier", "dtype": "source", "quantization": "none"},
            },
         )


if __name__ == "__main__":
   unittest.main()
