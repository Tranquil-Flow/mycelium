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

   def test_namespaced_bloom_family(self):
      manifest = self.compile_family("bloom", "transformer.h.{layer}.")
      self.assertEqual(manifest["block_prefix_template"], "transformer.h.{layer}.")

   def test_legacy_bare_bloom_family_remains_supported(self):
      manifest = self.compile_family("bloom", "h.{layer}.")
      self.assertEqual(manifest["block_prefix_template"], "h.{layer}.")

   def test_transformer_h_family(self):
      manifest = self.compile_family("falcon", "transformer.h.{layer}.")
      self.assertEqual(manifest["block_prefix_template"], "transformer.h.{layer}.")

   def test_bert_pooler_is_not_mislabeled_as_language_model_head(self):
      manifest = self.compile_family("bert", "encoder.layer.{layer}.")
      self.assertIn("pooler", manifest["components"])
      self.assertNotIn("lm_head", manifest["components"])

   def test_namespaced_bert_classifier_checkpoint_has_complete_static_ownership(self):
      weight_map = {
         "bert.embeddings.word_embeddings.weight": "model.safetensors",
         "bert.encoder.layer.0.attention.self.query.weight": "model.safetensors",
         "bert.encoder.layer.1.attention.self.query.weight": "model.safetensors",
         "bert.pooler.dense.weight": "model.safetensors",
         "classifier.weight": "model.safetensors",
      }
      manifest = mm.compile_model_manifest(
         model_id="org/bert-classifier",
         requested_revision="main",
         resolved_commit="a" * 40,
         config={
            "model_type": "bert",
            "num_hidden_layers": 2,
            "architectures": ["BertForSequenceClassification"],
         },
         checkpoint_index={"weight_map": weight_map},
         file_metadata={
            "model.safetensors": {"size_bytes": 10, "sha256": "1" * 64},
         },
      )

      self.assertEqual(manifest["block_prefix_template"], "bert.encoder.layer.{layer}.")
      self.assertEqual(
         manifest["component_tensor_keys"]["input_embedding"],
         ["bert.embeddings.word_embeddings.weight"],
      )
      self.assertEqual(
         manifest["component_tensor_keys"]["pooler"],
         ["bert.pooler.dense.weight"],
      )
      self.assertEqual(
         manifest["component_tensor_keys"]["classifier"],
         ["classifier.weight"],
      )

   def test_bert_checkpoint_with_unknown_unowned_head_fails_closed(self):
      with self.assertRaisesRegex(ValueError, "unowned tensor keys"):
         mm.compile_model_manifest(
            model_id="org/bert-custom",
            requested_revision="main",
            resolved_commit="a" * 40,
            config={"model_type": "bert", "num_hidden_layers": 1},
            checkpoint_index={
               "weight_map": {
                  "bert.embeddings.word_embeddings.weight": "model.safetensors",
                  "bert.encoder.layer.0.attention.self.query.weight": "model.safetensors",
                  "custom_head.weight": "model.safetensors",
               },
            },
            file_metadata={
               "model.safetensors": {"size_bytes": 10, "sha256": "1" * 64},
            },
         )

   def test_tied_bert_mlm_fails_closed_until_partial_tensor_aliases_are_supported(self):
      with self.assertRaisesRegex(ValueError, "unsupported bert architecture"):
         mm.compile_model_manifest(
            model_id="org/bert-mlm",
            requested_revision="main",
            resolved_commit="a" * 40,
            config={
               "model_type": "bert",
               "num_hidden_layers": 1,
               "architectures": ["BertForMaskedLM"],
               "tie_word_embeddings": True,
            },
            checkpoint_index={
               "weight_map": {
                  "bert.embeddings.word_embeddings.weight": "model.safetensors",
                  "bert.encoder.layer.0.attention.self.query.weight": "model.safetensors",
                  "cls.predictions.transform.dense.weight": "model.safetensors",
                  "cls.predictions.decoder.bias": "model.safetensors",
               },
            },
            file_metadata={
               "model.safetensors": {"size_bytes": 10, "sha256": "1" * 64},
            },
         )

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
