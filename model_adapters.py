#!/usr/bin/env python3
"""Architecture adapters shared by manifest compilation and future loaders."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


GPT2_DECODER_TENSOR_SUFFIXES = (
   "ln_1.weight",
   "ln_1.bias",
   "attn.c_attn.weight",
   "attn.c_attn.bias",
   "attn.c_proj.weight",
   "attn.c_proj.bias",
   "ln_2.weight",
   "ln_2.bias",
   "mlp.c_fc.weight",
   "mlp.c_fc.bias",
   "mlp.c_proj.weight",
   "mlp.c_proj.bias",
)
QWEN2_DECODER_TENSOR_SUFFIXES = (
   "input_layernorm.weight",
   "self_attn.q_proj.weight",
   "self_attn.q_proj.bias",
   "self_attn.k_proj.weight",
   "self_attn.k_proj.bias",
   "self_attn.v_proj.weight",
   "self_attn.v_proj.bias",
   "self_attn.o_proj.weight",
   "post_attention_layernorm.weight",
   "mlp.gate_proj.weight",
   "mlp.up_proj.weight",
   "mlp.down_proj.weight",
)
QWEN3_DECODER_TENSOR_SUFFIXES = (
   "input_layernorm.weight",
   "self_attn.q_proj.weight",
   "self_attn.q_norm.weight",
   "self_attn.k_proj.weight",
   "self_attn.k_norm.weight",
   "self_attn.v_proj.weight",
   "self_attn.o_proj.weight",
   "post_attention_layernorm.weight",
   "mlp.gate_proj.weight",
   "mlp.up_proj.weight",
   "mlp.down_proj.weight",
)


@dataclass(frozen=True)
class ModelAdapter:
   architecture: str
   layer_count_fields: tuple[str, ...]
   block_prefix_template: str
   components: dict[str, tuple[str, ...]]
   tied_lm_head_source: tuple[str, ...] = ()
   alternate_block_prefix_templates: tuple[str, ...] = ()
   supported_architectures: tuple[str, ...] = ()
   decoder_tensor_suffixes: tuple[str, ...] = ()
   runtime_backends: tuple[str, ...] = ()

   @property
   def runtime_supported(self) -> bool:
      return bool(self.decoder_tensor_suffixes and self.runtime_backends)

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

   def validate_architectures(self, config: dict[str, Any]) -> None:
      if not self.supported_architectures or "architectures" not in config:
         return
      architectures = config.get("architectures")
      if not isinstance(architectures, list) or not all(
         isinstance(name, str) and name for name in architectures
      ):
         raise ValueError("architectures must be a list of non-empty strings")
      unsupported = sorted(set(architectures) - set(self.supported_architectures))
      if unsupported:
         raise ValueError(
            f"unsupported {self.architecture} architecture: {', '.join(unsupported)}"
         )


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
      block_prefix_template="transformer.h.{layer}.",
      components={
         "input_embedding": ("transformer.wte.", "transformer.wpe.", "wte.", "wpe."),
         "decoder": ("transformer.h.{layer}.", "h.{layer}."),
         "final_norm": ("transformer.ln_f.", "ln_f."),
         "lm_head": ("lm_head.",),
      },
      tied_lm_head_source=("transformer.wte.", "wte."),
      alternate_block_prefix_templates=("h.{layer}.",),
      decoder_tensor_suffixes=GPT2_DECODER_TENSOR_SUFFIXES,
      runtime_backends=("mlx", "numpy"),
   ),
   "bloom": ModelAdapter(
      architecture="bloom",
      layer_count_fields=("n_layer", "num_hidden_layers"),
      block_prefix_template="transformer.h.{layer}.",
      components={
         "input_embedding": (
            "transformer.word_embeddings.",
            "transformer.word_embeddings_layernorm.",
            "word_embeddings.",
            "word_embeddings_layernorm.",
         ),
         "decoder": ("transformer.h.{layer}.", "h.{layer}."),
         "final_norm": ("transformer.ln_f.", "ln_f."),
         "lm_head": ("lm_head.",),
      },
      tied_lm_head_source=("transformer.word_embeddings.", "word_embeddings."),
      alternate_block_prefix_templates=("h.{layer}.",),
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
      tied_lm_head_source=("transformer.word_embeddings.",),
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
      tied_lm_head_source=("model.language_model.embed_tokens.",),
   ),
   "bert": ModelAdapter(
      architecture="bert",
      layer_count_fields=("num_hidden_layers",),
      block_prefix_template="bert.encoder.layer.{layer}.",
      components={
         "input_embedding": ("bert.embeddings.", "embeddings."),
         "decoder": ("bert.encoder.layer.{layer}.", "encoder.layer.{layer}."),
         "final_norm": (),
         "pooler": ("bert.pooler.", "pooler."),
         "classifier": ("classifier.",),
         "qa_head": ("qa_outputs.",),
      },
      alternate_block_prefix_templates=("encoder.layer.{layer}.",),
      supported_architectures=(
         "BertModel",
         "BertForSequenceClassification",
         "BertForTokenClassification",
         "BertForQuestionAnswering",
         "BertForMultipleChoice",
      ),
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
      tied_lm_head_source=("model.embed_tokens.",),
      decoder_tensor_suffixes=(
         QWEN2_DECODER_TENSOR_SUFFIXES
         if _model_type == "qwen2"
         else QWEN3_DECODER_TENSOR_SUFFIXES
         if _model_type == "qwen3"
         else ()
      ),
      runtime_backends=("mlx", "numpy") if _model_type in {"qwen2", "qwen3"} else (),
   )


def adapter_for_config(config: dict[str, Any]) -> ModelAdapter:
   model_type = config.get("model_type")
   if not isinstance(model_type, str) or model_type not in ADAPTERS:
      raise ValueError(f"unsupported model_type: {model_type!r}")
   return ADAPTERS[model_type]


def adapter_for_runtime(architecture: str) -> ModelAdapter:
   """Return the one adapter authorized to define runtime tensor ownership."""

   adapter = ADAPTERS.get(architecture)
   if adapter is None or not adapter.runtime_supported:
      raise ValueError(f"runtime adapter unavailable: {architecture!r}")
   return adapter
