from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Union

from .contracts import ModelIdentity


_DTYPE_BYTES = {
    "float64": 8,
    "float32": 4,
    "float16": 2,
    "bfloat16": 2,
    "int8": 1,
    "uint8": 1,
}


def model_identity_from_config(
    config: Mapping[str, Any],
    *,
    revision: str,
    weight_digest: str,
    weight_bytes: int,
    model_id: str | None = None,
) -> ModelIdentity:
    """Build immutable model dimensions from a machine-readable model config.

    Model-card prose is intentionally not scraped. The model repository's
    config.json (or an equivalent signed manifest) is the construction source.
    """
    num_layers = config.get("num_hidden_layers", config.get("n_layer"))
    hidden_size = config.get("hidden_size", config.get("n_embd"))
    attention_heads = config.get("num_attention_heads", config.get("n_head"))
    kv_heads = config.get("num_key_value_heads", attention_heads)
    if num_layers is None or hidden_size is None or attention_heads is None:
        raise ValueError("model config lacks layer, hidden-size, or attention-head dimensions")
    if int(hidden_size) % int(attention_heads) != 0:
        raise ValueError("hidden size must be divisible by attention head count")
    dtype_name = str(config.get("torch_dtype", "float16")).lower()
    dtype_bytes = _DTYPE_BYTES.get(dtype_name)
    if dtype_bytes is None:
        raise ValueError(f"unsupported activation dtype: {dtype_name}")
    architectures = config.get("architectures") or [config.get("model_type", "unknown")]
    return ModelIdentity(
        model_id=model_id or str(config.get("_name_or_path") or config.get("model_id") or "unknown/model"),
        revision=revision,
        weight_digest=weight_digest,
        architecture=str(architectures[0]),
        num_layers=int(num_layers),
        hidden_size=int(hidden_size),
        dtype_bytes=dtype_bytes,
        kv_heads=int(kv_heads),
        head_dim=int(hidden_size) // int(attention_heads),
        weight_bytes=int(weight_bytes),
    )


def load_model_identity(
    path: Union[str, Path],
    *,
    revision: str,
    weight_digest: str,
    weight_bytes: int,
    model_id: str | None = None,
) -> ModelIdentity:
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("model config must be a JSON object")
    return model_identity_from_config(
        config,
        revision=revision,
        weight_digest=weight_digest,
        weight_bytes=weight_bytes,
        model_id=model_id,
    )
