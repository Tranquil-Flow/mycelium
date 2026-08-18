from __future__ import annotations

import json
from pathlib import Path

from scripts.inspect_local_speculation_compatibility import (
    active_position_identity,
    tokenizer_identity,
)


def _write_tokenizer(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "tokenizer.json").write_text(
        json.dumps(
            {
                "model": {"vocab": {"a": 0, "b": 1}},
                "added_tokens": [{"id": 2, "content": "<end>"}],
            }
        ),
        "utf-8",
    )
    (root / "tokenizer_config.json").write_text(
        json.dumps(
            {"eos_token": "<end>", "added_tokens_decoder": {"2": "<end>"}}
        ),
        "utf-8",
    )


def test_tokenizer_identity_uses_canonical_mapping_not_padded_model_head(
    tmp_path: Path,
) -> None:
    _write_tokenizer(tmp_path)

    identity = tokenizer_identity(tmp_path)

    assert identity["canonical_token_count"] == 3
    assert identity["maximum_canonical_token_id"] == 2
    assert "model_output_domain_size" not in identity


def test_disabled_sliding_window_value_does_not_change_active_semantics() -> None:
    base = {
        "model_type": "qwen2",
        "max_position_embeddings": 32_768,
        "rope_theta": 1_000_000.0,
        "rope_scaling": None,
        "use_sliding_window": False,
    }

    first = active_position_identity({**base, "sliding_window": 32_768})
    second = active_position_identity({**base, "sliding_window": 131_072})

    assert (
        first["active_position_semantics_digest"]
        == second["active_position_semantics_digest"]
    )


def test_enabled_sliding_window_value_changes_active_semantics() -> None:
    base = {
        "model_type": "qwen2",
        "max_position_embeddings": 32_768,
        "rope_theta": 1_000_000.0,
        "rope_scaling": None,
        "use_sliding_window": True,
    }

    first = active_position_identity({**base, "sliding_window": 32_768})
    second = active_position_identity({**base, "sliding_window": 131_072})

    assert (
        first["active_position_semantics_digest"]
        != second["active_position_semantics_digest"]
    )
