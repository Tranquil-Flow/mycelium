# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.inspect_local_speculation_compatibility import (
    RUNTIME_PREREQUISITES,
    active_position_identity,
    inspect,
    tokenizer_identity,
)


def _write_tokenizer(
    root: Path,
    *,
    vocab: dict[str, int] | None = None,
    normalizer: dict[str, object] | None = None,
    eos_token: str = "<end>",
    vocab_size: int = 8,
    byte_fallback: bool = False,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "tokenizer.json").write_text(
        json.dumps(
            {
                "version": "1.0",
                "normalizer": normalizer,
                "model": {
                    "type": "BPE",
                    "byte_fallback": byte_fallback,
                    "vocab": vocab or {"a": 0, "b": 1},
                },
                "added_tokens": [{"id": 2, "content": "<end>"}],
            }
        ),
        "utf-8",
    )
    (root / "tokenizer_config.json").write_text(
        json.dumps(
            {"eos_token": eos_token, "added_tokens_decoder": {"2": eos_token}}
        ),
        "utf-8",
    )
    (root / "config.json").write_text(
        json.dumps(
            {
                "model_type": "qwen2",
                "vocab_size": vocab_size,
                "max_position_embeddings": 32_768,
                "rope_theta": 1_000_000.0,
                "rope_scaling": None,
                "use_sliding_window": False,
                "eos_token_id": 2,
            }
        ),
        "utf-8",
    )


def _entry(snapshot: Path, model_id: str) -> SimpleNamespace:
    digest_character = "1" if model_id == "target" else "2"
    return SimpleNamespace(
        compatible=True,
        snapshot_path=snapshot,
        artifact_digest=f"sha256:{digest_character * 64}",
        model_id=model_id,
        revision="a" * 40,
    )


def test_tokenizer_identity_uses_canonical_mapping_not_padded_model_head(
    tmp_path: Path,
) -> None:
    _write_tokenizer(tmp_path)

    identity = tokenizer_identity(tmp_path)

    assert identity["canonical_token_count"] == 3
    assert identity["maximum_canonical_token_id"] == 2
    assert "model_output_domain_size" not in identity


def test_tokenizer_identity_is_canonical_json_not_file_format_sensitive(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write_tokenizer(first)
    _write_tokenizer(second)
    value = json.loads((second / "tokenizer.json").read_text("utf-8"))
    (second / "tokenizer.json").write_text(
        json.dumps(value, indent=4, ensure_ascii=False), "utf-8"
    )

    assert tokenizer_identity(first) == tokenizer_identity(second)


def test_tokenizer_identity_rejects_non_bijective_canonical_mapping(
    tmp_path: Path,
) -> None:
    _write_tokenizer(tmp_path, vocab={"a": 0, "b": 0})

    with pytest.raises(ValueError, match="local_tokenizer_vocabulary_not_bijective"):
        tokenizer_identity(tmp_path)


def test_inspection_detects_mapping_normalization_and_special_token_mismatch(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    draft = tmp_path / "draft"
    _write_tokenizer(target, normalizer={"type": "NFC"})
    _write_tokenizer(
        draft,
        vocab={"b": 0, "a": 1},
        normalizer={"type": "NFD"},
        eos_token="<different-end>",
        byte_fallback=True,
    )

    result = inspect(_entry(target, "target"), _entry(draft, "draft"))

    assert result["checks"]["canonical_token_id_mapping"] is False
    assert result["checks"]["byte_fallback"] is False
    assert result["checks"]["normalization"] is False
    assert result["checks"]["special_token_semantics"] is False
    assert result["static_compatibility_observed"] is False


def test_static_compatibility_never_claims_runtime_or_qualification(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    draft = tmp_path / "draft"
    _write_tokenizer(target, vocab_size=8)
    _write_tokenizer(draft, vocab_size=16)

    result = inspect(_entry(target, "target"), _entry(draft, "draft"))

    assert result["static_compatibility_observed"] is True
    assert result["runtime_compatibility_evaluated"] is False
    assert result["unresolved_runtime_prerequisites"] == list(RUNTIME_PREREQUISITES)
    assert result["route_ready"] is False
    assert result["qualification_evaluated"] is False


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
