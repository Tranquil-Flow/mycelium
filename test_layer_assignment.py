#!/usr/bin/env python3
import unittest

import layer_assignment as la
import model_manifest as mm
import runtime_loader


class LayerAssignmentTests(unittest.TestCase):
   def manifest(self):
      return mm.compile_model_manifest(
         model_id="org/model",
         requested_revision="main",
         resolved_commit="a" * 40,
         config={
            "model_type": "gpt2",
            "architectures": ["GPT2LMHeadModel"],
            "n_layer": 3,
            "n_embd": 4,
            "n_head": 2,
            "n_inner": 8,
            "vocab_size": 7,
            "n_positions": 8,
            "layer_norm_epsilon": 1e-5,
            "activation_function": "gelu_new",
            "scale_attn_weights": True,
            "scale_attn_by_inverse_layer_idx": False,
            "reorder_and_upcast_attn": False,
            "add_cross_attention": False,
            "tie_word_embeddings": False,
         },
         checkpoint_index={
            "metadata": {"total_size": 50},
            "weight_map": {
               "transformer.wte.weight": "shard-1.safetensors",
               "transformer.wpe.weight": "shard-1.safetensors",
               "transformer.h.0.attn.weight": "shard-2.safetensors",
               "transformer.h.1.attn.weight": "shard-2.safetensors",
               "transformer.h.1.mlp.weight": "shard-3.safetensors",
               "transformer.h.2.attn.weight": "shard-3.safetensors",
               "transformer.ln_f.weight": "shard-3.safetensors",
               "lm_head.weight": "shard-1.safetensors",
            },
         },
         file_metadata={
            "shard-1.safetensors": {"size_bytes": 10, "sha256": "1" * 64},
            "shard-2.safetensors": {"size_bytes": 20, "sha256": "2" * 64},
            "shard-3.safetensors": {"size_bytes": 30, "sha256": "3" * 64},
         },
      )

   def tied_manifest(self):
      return mm.compile_model_manifest(
         model_id="org/model",
         requested_revision="main",
         resolved_commit="a" * 40,
         config={
            "model_type": "gpt2",
            "architectures": ["GPT2LMHeadModel"],
            "n_layer": 3,
            "n_embd": 4,
            "n_head": 2,
            "n_inner": 8,
            "vocab_size": 7,
            "n_positions": 8,
            "layer_norm_epsilon": 1e-5,
            "activation_function": "gelu_new",
            "scale_attn_weights": True,
            "scale_attn_by_inverse_layer_idx": False,
            "reorder_and_upcast_attn": False,
            "add_cross_attention": False,
            "tie_word_embeddings": True,
         },
         checkpoint_index={
            "metadata": {"total_size": 50},
            "weight_map": {
               "transformer.wte.weight": "shard-1.safetensors",
               "transformer.wpe.weight": "shard-1.safetensors",
               "transformer.h.0.attn.weight": "shard-2.safetensors",
               "transformer.h.1.attn.weight": "shard-2.safetensors",
               "transformer.h.1.mlp.weight": "shard-3.safetensors",
               "transformer.h.2.attn.weight": "shard-3.safetensors",
               "transformer.ln_f.weight": "shard-3.safetensors",
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
         "protocol": "mycelium.manual_provisioning_route.v1",
         "claim_boundary": "manual provisioning only",
         "model": {
            "model_id": "org/model",
            "num_layers": 3,
            "manifest_digest": mm.manifest_digest_ref(self.manifest()),
            "resolved_commit": "a" * 40,
         },
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

      self.assertEqual(node_a["protocol"], "mycelium.layer_assignment.v2")
      self.assertEqual(node_a["components"], ["input_embedding", "decoder"])
      self.assertEqual(node_b["components"], ["decoder", "final_norm", "lm_head"])
      self.assertEqual([f["path"] for f in node_a["files"]], [
         "shard-1.safetensors",
         "shard-2.safetensors",
         "shard-3.safetensors",
      ])
      self.assertEqual([f["path"] for f in node_b["files"]], [
         "shard-1.safetensors",
         "shard-3.safetensors",
      ])
      self.assertEqual(node_a["expected_tensor_prefixes"], ["transformer.h.0.", "transformer.h.1."])
      self.assertEqual(node_b["expected_tensor_prefixes"], ["transformer.h.2."])
      self.assertEqual(
         node_a["component_tensor_keys"]["input_embedding"],
         ["transformer.wpe.weight", "transformer.wte.weight"],
      )
      self.assertEqual(node_b["component_tensor_keys"]["final_norm"], ["transformer.ln_f.weight"])
      self.assertEqual(node_b["component_tensor_keys"]["lm_head"], ["lm_head.weight"])
      self.assertEqual(node_b["component_aliases"], {})
      self.assertIn("transformer.wte.weight", node_a["expected_tensor_keys"])
      self.assertIn("transformer.ln_f.weight", node_b["expected_tensor_keys"])
      self.assertIn("lm_head.weight", node_b["expected_tensor_keys"])
      self.assertEqual(node_a["resolved_commit"], "a" * 40)
      self.assertEqual(node_a["manifest_digest"], mm.manifest_digest_ref(self.manifest()))
      self.assertFalse(node_a["route_ready"])

   def test_mlx_assignment_binds_normalized_model_runtime_from_manifest(self):
      manifest = self.manifest()
      assignments = la.compile_layer_assignments(
         route_plan=self.route(),
         manifest=manifest,
         deployment_id="12345678-1234-5678-1234-567812345678",
         deployment_epoch=1,
         cache_roots={"node-a": "/tmp/a", "node-b": "/tmp/b"},
         runtime_by_node={
            node: {"backend": "mlx", "dtype": "float16", "quantization": "none"}
            for node in ("node-a", "node-b")
         },
      )

      for assignment in assignments:
         self.assertEqual(
            assignment["runtime"],
            {
               "backend": "mlx",
               "dtype": "float16",
               "quantization": "none",
               **manifest["runtime_model"],
            },
         )
         la.validate_assignment_identity(assignment)

   def test_numpy_assignment_compiles_to_loader_accepted_manifest_bound_runtime(self):
      manifest = self.manifest()
      assignments = la.compile_layer_assignments(
         route_plan=self.route(),
         manifest=manifest,
         deployment_id="12345678-1234-5678-1234-567812345678",
         deployment_epoch=1,
         cache_roots={"node-a": "/tmp/a", "node-b": "/tmp/b"},
         runtime_by_node={
            node: {"backend": "numpy", "dtype": "float32", "quantization": "none"}
            for node in ("node-a", "node-b")
         },
      )

      for assignment in assignments:
         self.assertEqual(
            assignment["runtime"],
            {
               "backend": "numpy",
               "dtype": "float32",
               "quantization": "none",
               **manifest["runtime_model"],
            },
         )
         normalized, dtype = runtime_loader._validate_runtime(assignment["runtime"])
         self.assertEqual(normalized, assignment["runtime"])
         self.assertEqual(str(dtype), "float32")
         la.validate_assignment_identity(assignment)

   def test_node_runtime_cannot_override_manifest_model_identity(self):
      runtime = {
         "backend": "mlx",
         "dtype": "float16",
         "quantization": "none",
         "architecture": "attacker-model",
      }
      with self.assertRaisesRegex(ValueError, "runtime fields"):
         la.compile_layer_assignments(
            route_plan=self.route(),
            manifest=self.manifest(),
            deployment_id="12345678-1234-5678-1234-567812345678",
            deployment_epoch=1,
            cache_roots={"node-a": "/tmp/a", "node-b": "/tmp/b"},
            runtime_by_node={"node-a": runtime, "node-b": runtime},
         )

   def test_tied_lm_head_assignment_receives_embedding_source(self):
      manifest = self.tied_manifest()
      route = self.route()
      route["model"]["manifest_digest"] = mm.manifest_digest_ref(manifest)
      assignments = la.compile_layer_assignments(
         route_plan=route,
         manifest=manifest,
         deployment_id="12345678-1234-5678-1234-567812345678",
         deployment_epoch=1,
         cache_roots={"node-a": "/tmp/a", "node-b": "/tmp/b"},
         runtime_by_node={
            "node-a": {"backend": "artifact_verifier", "dtype": "source", "quantization": "none"},
            "node-b": {"backend": "artifact_verifier", "dtype": "source", "quantization": "none"},
         },
      )
      final = assignments[-1]
      self.assertEqual(final["component_aliases"], {"lm_head": "input_embedding"})
      self.assertEqual(final["component_tensor_keys"]["lm_head"], ["transformer.wte.weight"])
      self.assertIn("transformer.wte.weight", final["expected_tensor_keys"])
      self.assertIn("shard-1.safetensors", [item["path"] for item in final["files"]])

   def test_final_stage_owns_all_present_non_decoder_static_components(self):
      manifest = mm.compile_model_manifest(
         model_id="org/bert-classifier",
         requested_revision="main",
         resolved_commit="b" * 40,
         config={
            "model_type": "bert",
            "num_hidden_layers": 2,
            "architectures": ["BertForSequenceClassification"],
         },
         checkpoint_index={
            "weight_map": {
               "bert.embeddings.word_embeddings.weight": "model.safetensors",
               "bert.encoder.layer.0.attention.self.query.weight": "model.safetensors",
               "bert.encoder.layer.1.attention.self.query.weight": "model.safetensors",
               "bert.pooler.dense.weight": "model.safetensors",
               "classifier.weight": "model.safetensors",
            },
         },
         file_metadata={
            "model.safetensors": {"size_bytes": 10, "sha256": "1" * 64},
         },
      )
      route = {
         "ok": True,
         "protocol": "mycelium.manual_provisioning_route.v1",
         "claim_boundary": "manual provisioning only",
         "model": {
            "model_id": manifest["model_id"],
            "num_layers": manifest["num_layers"],
            "manifest_digest": mm.manifest_digest_ref(manifest),
            "resolved_commit": manifest["resolved_commit"],
         },
         "route": [
            {
               "node_id": "node-a",
               "range": {"start_layer": 0, "end_layer_exclusive": 1, "layer_count": 1},
            },
            {
               "node_id": "node-b",
               "range": {"start_layer": 1, "end_layer_exclusive": 2, "layer_count": 1},
            },
         ],
         "node_order": ["node-a", "node-b"],
      }
      assignments = la.compile_layer_assignments(
         route_plan=route,
         manifest=manifest,
         deployment_id="12345678-1234-5678-1234-567812345678",
         deployment_epoch=1,
         cache_roots={"node-a": "/tmp/a", "node-b": "/tmp/b"},
         runtime_by_node={
            "node-a": {"backend": "artifact_verifier", "dtype": "source", "quantization": "none"},
            "node-b": {"backend": "artifact_verifier", "dtype": "source", "quantization": "none"},
         },
      )

      self.assertEqual(assignments[0]["components"], ["input_embedding", "decoder"])
      self.assertEqual(assignments[1]["components"], ["decoder", "pooler", "classifier"])
      self.assertEqual(
         assignments[1]["component_tensor_keys"]["pooler"],
         ["bert.pooler.dense.weight"],
      )
      self.assertEqual(
         assignments[1]["component_tensor_keys"]["classifier"],
         ["classifier.weight"],
      )

   def test_assignments_are_deterministic_for_same_deployment(self):
      first = self.compile()
      second = self.compile()
      self.assertEqual(
         [item["assignment_id"] for item in first],
         [item["assignment_id"] for item in second],
      )

   def test_compile_rejects_route_manifest_identity_drift(self):
      route = self.route()
      route["model"]["manifest_digest"] = "sha256:" + "f" * 64
      with self.assertRaisesRegex(ValueError, "manifest_digest"):
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

   def test_assignment_identity_detects_semantic_tampering(self):
      assignment = self.compile()[0]
      assignment["files"][0]["size_bytes"] += 1
      with self.assertRaisesRegex(ValueError, "assignment_id"):
         la.validate_assignment_identity(assignment)

   def test_assignment_identity_detects_protocol_tampering(self):
      assignment = self.compile()[0]
      assignment["protocol"] = "mycelium.layer_assignment.v1"
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
         "model": {
            "model_id": "org/model",
            "num_layers": 3,
            "manifest_digest": mm.manifest_digest_ref(self.manifest()),
            "resolved_commit": "a" * 40,
         },
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
