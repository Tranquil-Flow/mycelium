#!/usr/bin/env python3
"""Architecture adapters shared by manifest compilation and future loaders."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ModelAdapter:
   architecture: str
   layer_count_fields: tuple[str, ...]
   block_prefix_template: str
   components: dict[str, tuple[str, ...]]

   def layer_count(self, config: dict[str, Any]) -> int:
      for field in self.layer_count_fields:
         value: Any = config
         for part in field.split("."):
            if not isinstance(value, dict):
               value = None
               break
            value = value.get(part)
         if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
      fields = ", ".join(self.layer_count_fields)
      raise ValueError(f"missing positive layer count field ({fields})")


_MODEL_LAYERS_COMPONENTS = {
   "input_embedding": ("model.embed_tokens.",),
   "decoder": ("model.layers.{layer}.",),
   "final_norm": ("model.norm.",),
   "lm_head": ("lm_head.",),
}


ADAPTERS = {
   "gpt2": ModelAdapter(
      architecture="gpt2",
      layer_count_fields=("n_layer", "num_hidden_layers"),
      block_prefix_template="h.{layer}.",
      components={
         "input_embedding": ("wte.", "wpe."),
         "decoder": ("h.{layer}.",),
         "final_norm": ("ln_f.",),
         "lm_head": ("lm_head.",),
      },
   ),
   "bloom": ModelAdapter(
      architecture="bloom",
      layer_count_fields=("n_layer", "num_hidden_layers"),
      block_prefix_template="h.{layer}.",
      components={
         "input_embedding": ("word_embeddings.", "word_embeddings_layernorm."),
         "decoder": ("h.{layer}.",),
         "final_norm": ("ln_f.",),
         "lm_head": ("lm_head.",),
      },
   ),
   "falcon": ModelAdapter(
      architecture="falcon",
      layer_count_fields=("num_hidden_layers", "n_layer"),
      block_prefix_template="transformer.h.{layer}.",
      components={
         "input_embedding": ("transformer.word_embeddings.",),
         "decoder": ("transformer.h.{layer}.",),
         "final_norm": ("transformer.ln_f.",),
         "lm_head": ("lm_head.",),
      },
   ),
   "gemma3": ModelAdapter(
      architecture="gemma3",
      layer_count_fields=("text_config.num_hidden_layers", "num_hidden_layers"),
      block_prefix_template="model.language_model.layers.{layer}.",
      components={
         "input_embedding": ("model.language_model.embed_tokens.",),
         "decoder": ("model.language_model.layers.{layer}.",),
         "final_norm": ("model.language_model.norm.",),
         "lm_head": ("lm_head.",),
      },
   ),
   "bert": ModelAdapter(
      architecture="bert",
      layer_count_fields=("num_hidden_layers",),
      block_prefix_template="encoder.layer.{layer}.",
      components={
         "input_embedding": ("embeddings.",),
         "decoder": ("encoder.layer.{layer}.",),
         "final_norm": (),
         "lm_head": ("pooler.",),
      },
   ),
}

for _model_type in (
   "llama",
   "mistral",
   "mixtral",
   "qwen2",
   "qwen3",
   "gemma",
   "gemma2",
   "gemma3_text",
):
   ADAPTERS[_model_type] = ModelAdapter(
      architecture=_model_type,
      layer_count_fields=("num_hidden_layers", "n_layer"),
      block_prefix_template="model.layers.{layer}.",
      components=_MODEL_LAYERS_COMPONENTS,
   )


def adapter_for_config(config: dict[str, Any]) -> ModelAdapter:
   model_type = config.get("model_type")
   if not isinstance(model_type, str) or model_type not in ADAPTERS:
      raise ValueError(f"unsupported model_type: {model_type!r}")
   return ADAPTERS[model_type]
