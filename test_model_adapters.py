#!/usr/bin/env python3
import unittest

import model_manifest as mm


class ModelAdapterFamilyTests(unittest.TestCase):
   def compile_family(self, model_type, prefix, config_extra=None):
      config = {"model_type": model_type, "num_hidden_layers": 2}
      if config_extra:
         config.update(config_extra)
      return mm.compile_model_manifest(
         model_id=f"org/{model_type}",
         requested_revision="main",
         resolved_commit="a" * 40,
         config=config,
         checkpoint_index={
            "weight_map": {
               prefix.format(layer=0) + "attn.weight": "shard-a.safetensors",
               prefix.format(layer=1) + "mlp.weight": "shard-b.safetensors",
            },
         },
         file_metadata={
            "shard-a.safetensors": {"size_bytes": 10, "sha256": "1" * 64},
            "shard-b.safetensors": {"size_bytes": 20, "sha256": "2" * 64},
         },
      )

   def test_model_layers_family(self):
      manifest = self.compile_family("llama", "model.layers.{layer}.")
      self.assertEqual(manifest["block_prefix_template"], "model.layers.{layer}.")
      self.assertEqual(manifest["layer_files"]["1"], ["shard-b.safetensors"])

   def test_h_family(self):
      manifest = self.compile_family("bloom", "h.{layer}.")
      self.assertEqual(manifest["block_prefix_template"], "h.{layer}.")

   def test_transformer_h_family(self):
      manifest = self.compile_family("falcon", "transformer.h.{layer}.")
      self.assertEqual(manifest["block_prefix_template"], "transformer.h.{layer}.")

   def test_nested_language_model_family_reads_nested_layer_count(self):
      manifest = self.compile_family(
         "gemma3",
         "model.language_model.layers.{layer}.",
         config_extra={"num_hidden_layers": None, "text_config": {"num_hidden_layers": 2}},
      )
      self.assertEqual(
         manifest["block_prefix_template"],
         "model.language_model.layers.{layer}.",
      )

   def test_gemma3_text_uses_non_nested_hugging_face_prefix(self):
      manifest = self.compile_family("gemma3_text", "model.layers.{layer}.")
      self.assertEqual(manifest["block_prefix_template"], "model.layers.{layer}.")


if __name__ == "__main__":
   unittest.main()
