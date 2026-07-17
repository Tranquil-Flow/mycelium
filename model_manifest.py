#!/usr/bin/env python3
"""Immutable, architecture-aware Hugging Face model manifest compiler."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from model_adapters import adapter_for_config
from runtime_contracts import normalize_gpt2_model_config


_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def canonical_json(document: Any) -> str:
   return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest_document(document: dict[str, Any]) -> dict[str, str]:
   value = hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()
   return {"algorithm": "sha256", "value": value}


def manifest_digest_ref(manifest: dict[str, Any]) -> str:
   digest = manifest.get("manifest_digest") or {}
   if digest.get("algorithm") != "sha256" or not _SHA256_RE.fullmatch(str(digest.get("value", ""))):
      raise ValueError("manifest has invalid manifest_digest")
   return f"sha256:{digest['value']}"


def verify_manifest_digest(manifest: dict[str, Any]) -> bool:
   unsigned = copy.deepcopy(manifest)
   supplied = unsigned.pop("manifest_digest", None)
   return supplied == _digest_document(unsigned)


def _static_component_tensor_keys(
   weight_map: dict[str, str],
   components: dict[str, tuple[str, ...]],
) -> dict[str, list[str]]:
   result: dict[str, list[str]] = {}
   for name, prefixes in components.items():
      if name == "decoder":
         continue
      result[name] = sorted(
         tensor
         for tensor in weight_map
         if any("{layer}" not in prefix and tensor.startswith(prefix) for prefix in prefixes)
      )
   return result


def _component_files(
   weight_map: dict[str, str],
   component_tensor_keys: dict[str, list[str]],
) -> dict[str, list[str]]:
   return {
      name: sorted({weight_map[key] for key in keys})
      for name, keys in component_tensor_keys.items()
   }


def _is_causal_lm(config: dict[str, Any]) -> bool:
   architectures = config.get("architectures")
   return isinstance(architectures, list) and any(
      isinstance(name, str) and (
         name.endswith("ForCausalLM")
         or name in {"GPT2LMHeadModel"}
      )
      for name in architectures
   )


def compile_model_manifest(
   *,
   model_id: str,
   requested_revision: str,
   resolved_commit: str,
   config: dict[str, Any],
   checkpoint_index: dict[str, Any],
   file_metadata: dict[str, dict[str, Any]],
   index_file: str = "model.safetensors.index.json",
) -> dict[str, Any]:
   if not isinstance(model_id, str) or "/" not in model_id:
      raise ValueError("model_id must be a repository ID")
   if not isinstance(requested_revision, str) or not requested_revision:
      raise ValueError("requested_revision is required")
   if not _COMMIT_RE.fullmatch(resolved_commit):
      raise ValueError("resolved_commit must be a lowercase 40-hex commit")
   if not isinstance(checkpoint_index, dict):
      raise ValueError("checkpoint index must be an object")
   weight_map = checkpoint_index.get("weight_map")
   if not isinstance(weight_map, dict) or not weight_map:
      raise ValueError("checkpoint index requires non-empty weight_map")
   if not all(isinstance(key, str) and isinstance(value, str) for key, value in weight_map.items()):
      raise ValueError("checkpoint weight_map must map tensor names to files")

   adapter = adapter_for_config(config)
   adapter.validate_architectures(config)
   num_layers = adapter.layer_count(config)
   block_prefix_template = None
   for candidate in (
      adapter.block_prefix_template,
      *adapter.alternate_block_prefix_templates,
   ):
      if all(
         any(key.startswith(candidate.format(layer=layer)) for key in weight_map)
         for layer in range(num_layers)
      ):
         block_prefix_template = candidate
         break
   if block_prefix_template is None:
      for layer in range(num_layers):
         if not any(
            key.startswith(candidate.format(layer=layer))
            for candidate in (
               adapter.block_prefix_template,
               *adapter.alternate_block_prefix_templates,
            )
            for key in weight_map
         ):
            raise ValueError(f"missing tensor coverage for layer {layer}")
      raise ValueError("layer tensors mix incompatible checkpoint namespaces")

   tensor_keys_by_layer: dict[str, list[str]] = {}
   layer_files: dict[str, list[str]] = {}
   for layer in range(num_layers):
      prefix = block_prefix_template.format(layer=layer)
      keys = sorted(key for key in weight_map if key.startswith(prefix))
      tensor_keys_by_layer[str(layer)] = keys
      layer_files[str(layer)] = sorted({weight_map[key] for key in keys})

   referenced_files = sorted(set(weight_map.values()))
   missing = [path for path in referenced_files if path not in file_metadata]
   if missing:
      raise ValueError(f"missing file metadata for: {', '.join(missing)}")

   files = []
   for path in referenced_files:
      metadata = file_metadata[path]
      size = metadata.get("size_bytes")
      digest = metadata.get("sha256")
      if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
         raise ValueError(f"invalid size metadata for {path}")
      if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
         raise ValueError(f"invalid sha256 metadata for {path}")
      record = {
         "path": path,
         "size_bytes": size,
         "content_digest": {"algorithm": "sha256", "value": digest},
      }
      if metadata.get("source_etag"):
         record["source_etag"] = str(metadata["source_etag"])
      files.append(record)

   components = {name: list(prefixes) for name, prefixes in adapter.components.items()}
   component_tensor_keys = _static_component_tensor_keys(weight_map, adapter.components)
   recognized_tensor_keys = {
      key
      for keys in tensor_keys_by_layer.values()
      for key in keys
   }
   recognized_tensor_keys.update(
      key
      for keys in component_tensor_keys.values()
      for key in keys
   )
   unowned_tensor_keys = sorted(set(weight_map) - recognized_tensor_keys)
   if unowned_tensor_keys:
      preview = ", ".join(unowned_tensor_keys[:5])
      suffix = "" if len(unowned_tensor_keys) <= 5 else f" (+{len(unowned_tensor_keys) - 5} more)"
      raise ValueError(f"unowned tensor keys for {adapter.architecture}: {preview}{suffix}")
   component_aliases: dict[str, str] = {}
   if _is_causal_lm(config):
      for required in ("input_embedding", "final_norm"):
         if not component_tensor_keys.get(required):
            raise ValueError(f"causal LM missing {required} tensor coverage")
      if config.get("tie_word_embeddings") is True:
         tied_keys = sorted(
            key
            for key in weight_map
            if any(key.startswith(prefix) for prefix in adapter.tied_lm_head_source)
         )
         if not tied_keys:
            raise ValueError("tied lm_head source tensor coverage missing")
         component_tensor_keys["lm_head"] = tied_keys
         component_aliases["lm_head"] = "input_embedding"
      elif not component_tensor_keys.get("lm_head"):
         raise ValueError("causal LM missing lm_head tensors without explicit tied embeddings")
   component_files = _component_files(weight_map, component_tensor_keys)
   runtime_model = None
   if adapter.architecture == "gpt2" and (
      _is_causal_lm(config)
      or any(field in config for field in ("n_embd", "n_head", "vocab_size", "n_positions"))
   ):
      runtime_model = {
         "architecture": "gpt2",
         "model_config": normalize_gpt2_model_config(
            config, expected_layers=num_layers
         ),
      }
   manifest = {
      "protocol": "mycelium.model_manifest.v1",
      "model_id": model_id,
      "source": "huggingface",
      "requested_revision": requested_revision,
      "resolved_commit": resolved_commit,
      "format": "safetensors_sharded",
      "index_file": index_file,
      "architecture": adapter.architecture,
      "num_layers": num_layers,
      "block_prefix_template": block_prefix_template,
      "components": components,
      "component_tensor_keys": component_tensor_keys,
      "component_aliases": component_aliases,
      "component_files": component_files,
      "files": files,
      "layer_files": layer_files,
      "tensor_keys_by_layer": tensor_keys_by_layer,
   }
   if runtime_model is not None:
      manifest["runtime_model"] = runtime_model
   manifest["manifest_digest"] = _digest_document(manifest)
   return manifest


def resolve_huggingface_manifest(
   model_id: str,
   *,
   requested_revision: str = "main",
   cache_root: str | Path | None = None,
) -> dict[str, Any]:
   """Resolve metadata online, pin commit, then invoke pure manifest compiler."""
   try:
      from huggingface_hub import HfApi, hf_hub_download
   except ImportError as exc:
      raise RuntimeError("huggingface_hub is required for online resolution") from exc

   api = HfApi()
   info = api.model_info(model_id, revision=requested_revision, files_metadata=True)
   resolved_commit = str(info.sha)
   if not _COMMIT_RE.fullmatch(resolved_commit):
      raise ValueError("Hub did not resolve revision to a 40-hex commit")
   siblings = {item.rfilename: item for item in info.siblings or []}
   index_file = "model.safetensors.index.json"
   if index_file not in siblings:
      raise ValueError("V1 supports sharded Safetensors with model.safetensors.index.json only")
   if "config.json" not in siblings:
      raise ValueError("resolved commit lacks config.json")

   cache_dir = str(Path(cache_root).expanduser().resolve()) if cache_root is not None else None
   config_path = hf_hub_download(
      repo_id=model_id,
      filename="config.json",
      revision=resolved_commit,
      cache_dir=cache_dir,
   )
   index_path = hf_hub_download(
      repo_id=model_id,
      filename=index_file,
      revision=resolved_commit,
      cache_dir=cache_dir,
   )
   config = json.loads(Path(config_path).read_text())
   checkpoint_index = json.loads(Path(index_path).read_text())
   weight_map = checkpoint_index.get("weight_map") or {}

   file_metadata: dict[str, dict[str, Any]] = {}
   for path in sorted(set(weight_map.values())):
      sibling = siblings.get(path)
      if sibling is None:
         raise ValueError(f"index references file absent at resolved commit: {path}")
      lfs = getattr(sibling, "lfs", None)
      sha256 = getattr(lfs, "sha256", None)
      size = getattr(sibling, "size", None)
      if not _SHA256_RE.fullmatch(str(sha256 or "")):
         raise ValueError(f"upstream file lacks SHA-256 metadata: {path}")
      if not isinstance(size, int) or size <= 0:
         raise ValueError(f"upstream file lacks size metadata: {path}")
      file_metadata[path] = {
         "size_bytes": size,
         "sha256": sha256,
         "source_etag": getattr(sibling, "blob_id", None),
      }

   return compile_model_manifest(
      model_id=model_id,
      requested_revision=requested_revision,
      resolved_commit=resolved_commit,
      config=config,
      checkpoint_index=checkpoint_index,
      file_metadata=file_metadata,
      index_file=index_file,
   )
