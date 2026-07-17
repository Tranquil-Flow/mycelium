#!/usr/bin/env python3
import unittest

import model_manifest as mm


class ModelManifestTests(unittest.TestCase):
   def config(self, model_type="gpt2"):
      return {
         "architectures": ["GPT2Model"],
         "model_type": model_type,
         "n_layer": 3,
      }

   def index(self):
      return {
         "metadata": {"total_size": 60},
         "weight_map": {
            "wte.weight": "shard-1.safetensors",
            "h.0.attn.weight": "shard-2.safetensors",
            "h.1.attn.weight": "shard-2.safetensors",
            "h.1.mlp.weight": "shard-3.safetensors",
            "h.2.attn.weight": "shard-3.safetensors",
            "ln_f.weight": "shard-3.safetensors",
         },
      }

   def files(self):
      return {
         "shard-1.safetensors": {"size_bytes": 10, "sha256": "1" * 64},
         "shard-2.safetensors": {"size_bytes": 20, "sha256": "2" * 64},
         "shard-3.safetensors": {"size_bytes": 30, "sha256": "3" * 64},
      }

   def compile(self):
      return mm.compile_model_manifest(
         model_id="org/model",
         requested_revision="main",
         resolved_commit="a" * 40,
         config=self.config(),
         checkpoint_index=self.index(),
         file_metadata=self.files(),
      )

   def test_compiles_complete_architecture_aware_layer_map(self):
      manifest = self.compile()
      self.assertEqual(manifest["protocol"], "mycelium.model_manifest.v1")
      self.assertEqual(manifest["architecture"], "gpt2")
      self.assertEqual(manifest["num_layers"], 3)
      self.assertEqual(manifest["block_prefix_template"], "h.{layer}.")
      self.assertEqual(manifest["layer_files"]["0"], ["shard-2.safetensors"])
      self.assertEqual(
         manifest["layer_files"]["1"],
         ["shard-2.safetensors", "shard-3.safetensors"],
      )
      self.assertEqual(manifest["component_files"]["input_embedding"], ["shard-1.safetensors"])
      self.assertEqual(manifest["component_files"]["final_norm"], ["shard-3.safetensors"])
      self.assertEqual(manifest["manifest_digest"]["algorithm"], "sha256")
      self.assertEqual(len(manifest["manifest_digest"]["value"]), 64)

   def test_manifest_digest_is_canonical_and_deterministic(self):
      first = self.compile()
      index = self.index()
      index["weight_map"] = dict(reversed(list(index["weight_map"].items())))
      second = mm.compile_model_manifest(
         model_id="org/model",
         requested_revision="main",
         resolved_commit="a" * 40,
         config=self.config(),
         checkpoint_index=index,
         file_metadata=dict(reversed(list(self.files().items()))),
      )
      self.assertEqual(first["manifest_digest"], second["manifest_digest"])
      self.assertTrue(mm.verify_manifest_digest(first))

   def test_missing_layer_fails_closed(self):
      index = self.index()
      del index["weight_map"]["h.2.attn.weight"]
      with self.assertRaisesRegex(ValueError, "missing tensor coverage.*2"):
         mm.compile_model_manifest(
            model_id="org/model",
            requested_revision="main",
            resolved_commit="a" * 40,
            config=self.config(),
            checkpoint_index=index,
            file_metadata=self.files(),
         )

   def test_unknown_architecture_fails_closed(self):
      with self.assertRaisesRegex(ValueError, "unsupported model_type"):
         mm.compile_model_manifest(
            model_id="org/model",
            requested_revision="main",
            resolved_commit="a" * 40,
            config=self.config(model_type="mystery"),
            checkpoint_index=self.index(),
            file_metadata=self.files(),
         )

   def test_missing_upstream_file_metadata_fails_closed(self):
      files = self.files()
      del files["shard-3.safetensors"]
      with self.assertRaisesRegex(ValueError, "missing file metadata"):
         mm.compile_model_manifest(
            model_id="org/model",
            requested_revision="main",
            resolved_commit="a" * 40,
            config=self.config(),
            checkpoint_index=self.index(),
            file_metadata=files,
         )


if __name__ == "__main__":
   unittest.main()
