#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Inspect static target/draft compatibility from existing local snapshots only."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mycelium_model_catalog import LocalModelEntry, scan_huggingface_cache  # noqa: E402


PROTOCOL = "mycelium.local_speculation_compatibility_inspection.v1"
MAX_JSON_BYTES = 64 * 1024 * 1024
RUNTIME_PREREQUISITES = (
    "authorized_exact_target_and_draft_runtime_bindings",
    "a10_physically_qualified_target_verifier_binding",
    "identical_noncanonical_and_reserved_id_runtime_masking",
    "runtime_position_attention_and_context_truncation_parity",
    "separate_target_draft_and_temporary_verifier_kv_ownership",
    "physical_output_parity_rejection_rollback_and_cleanup_gates",
    "same_route_physical_materiality_benchmark",
)


def _json(path: Path) -> Mapping[str, Any]:
    resolved = path.resolve(strict=True)
    if not resolved.is_file() or resolved.stat().st_size > MAX_JSON_BYTES:
        raise ValueError(f"local_model_metadata_invalid:{path.name}")
    value = json.loads(resolved.read_text("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"local_model_metadata_invalid:{path.name}")
    return value


def _digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _digest_json(value: object) -> str:
    return _digest_bytes(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    )


def tokenizer_identity(
    snapshot: Path, model_config: Mapping[str, Any] | None = None
) -> dict[str, object]:
    tokenizer_path = snapshot / "tokenizer.json"
    tokenizer = _json(tokenizer_path)
    tokenizer_config = _json(snapshot / "tokenizer_config.json")
    model = tokenizer.get("model")
    if not isinstance(model, dict) or not isinstance(model.get("vocab"), dict):
        raise ValueError("local_tokenizer_vocabulary_invalid")
    vocab = model["vocab"]
    added = tokenizer.get("added_tokens", [])
    if not isinstance(added, list):
        raise ValueError("local_tokenizer_vocabulary_invalid")
    canonical_mapping: dict[str, int] = {}
    id_to_token: dict[int, str] = {}

    def add_token(token: object, token_id: object) -> None:
        if not isinstance(token, str) or not token:
            raise ValueError("local_tokenizer_vocabulary_invalid")
        if type(token_id) is not int or token_id < 0:
            raise ValueError("local_tokenizer_vocabulary_invalid")
        existing_id = canonical_mapping.get(token)
        existing_token = id_to_token.get(token_id)
        if (existing_id is not None and existing_id != token_id) or (
            existing_token is not None and existing_token != token
        ):
            raise ValueError("local_tokenizer_vocabulary_not_bijective")
        canonical_mapping[token] = token_id
        id_to_token[token_id] = token

    for token, token_id in vocab.items():
        add_token(token, token_id)
    for record in added:
        if not isinstance(record, dict):
            raise ValueError("local_tokenizer_vocabulary_invalid")
        add_token(record.get("content"), record.get("id"))
    token_ids = set(id_to_token)
    if not token_ids or token_ids != set(range(max(token_ids) + 1)):
        raise ValueError("local_tokenizer_vocabulary_noncontiguous")
    special_token_keys = (
        "bos_token",
        "eos_token",
        "pad_token",
        "unk_token",
        "sep_token",
        "cls_token",
        "mask_token",
        "additional_special_tokens",
        "added_tokens_decoder",
    )
    special_id_keys = (
        "bos_token_id",
        "eos_token_id",
        "pad_token_id",
        "unk_token_id",
        "sep_token_id",
        "cls_token_id",
        "mask_token_id",
    )
    special = {
        "tokenizer_config": {
            key: tokenizer_config.get(key) for key in special_token_keys
        },
        "model_config_ids": {
            key: (model_config or {}).get(key) for key in special_id_keys
        },
        "special_added_tokens": [
            record for record in added if record.get("special") is True
        ],
    }
    model_without_vocab = {key: value for key, value in model.items() if key != "vocab"}
    implementation = {
        key: tokenizer.get(key)
        for key in (
            "version",
            "truncation",
            "padding",
            "normalizer",
            "pre_tokenizer",
            "post_processor",
            "decoder",
        )
    }
    implementation["model"] = model_without_vocab
    implementation["added_tokens"] = added
    byte_fallback = {
        "model_byte_fallback": model.get("byte_fallback"),
        "decoder": tokenizer.get("decoder"),
    }
    return {
        "canonical_token_count": len(token_ids),
        "canonical_token_id_mapping_digest": _digest_json(canonical_mapping),
        "maximum_canonical_token_id": max(token_ids),
        "normalization_digest": _digest_json(tokenizer.get("normalizer")),
        "byte_fallback_digest": _digest_json(byte_fallback),
        "special_token_semantics_digest": _digest_json(special),
        "tokenizer_implementation_digest": _digest_json(implementation),
        "tokenizer_digest": _digest_json(tokenizer),
    }


def active_position_identity(config: Mapping[str, Any]) -> dict[str, object]:
    use_sliding_window = config.get("use_sliding_window") is True
    semantics: dict[str, object] = {
        "model_type": config.get("model_type"),
        "max_position_embeddings": config.get("max_position_embeddings"),
        "rope_scaling": config.get("rope_scaling"),
        "rope_theta": config.get("rope_theta"),
        "use_sliding_window": use_sliding_window,
    }
    if use_sliding_window:
        semantics["sliding_window"] = config.get("sliding_window")
    return {
        "active_position_semantics_digest": _digest_json(semantics),
        "inactive_sliding_window_value": (
            None if use_sliding_window else config.get("sliding_window")
        ),
    }


def snapshot_identity(entry: LocalModelEntry) -> dict[str, object]:
    config = _json(entry.snapshot_path / "config.json")
    tokenizer = tokenizer_identity(entry.snapshot_path, config)
    model_vocab_size = config.get("vocab_size")
    if type(model_vocab_size) is not int or model_vocab_size <= 0:
        raise ValueError("local_model_output_domain_invalid")
    canonical_count = tokenizer["canonical_token_count"]
    assert isinstance(canonical_count, int)
    return {
        "active_position_semantics_digest": active_position_identity(config)[
            "active_position_semantics_digest"
        ],
        "artifact_digest": entry.artifact_digest,
        "canonical_token_count": canonical_count,
        "canonical_token_id_mapping_digest": tokenizer[
            "canonical_token_id_mapping_digest"
        ],
        "byte_fallback_digest": tokenizer["byte_fallback_digest"],
        "maximum_canonical_token_id": tokenizer["maximum_canonical_token_id"],
        "model_id": entry.model_id,
        "model_output_domain_covers_tokenizer": model_vocab_size >= canonical_count,
        "model_output_domain_size": model_vocab_size,
        "revision": entry.revision,
        "normalization_digest": tokenizer["normalization_digest"],
        "special_token_semantics_digest": tokenizer[
            "special_token_semantics_digest"
        ],
        "tokenizer_implementation_digest": tokenizer[
            "tokenizer_implementation_digest"
        ],
        "tokenizer_digest": tokenizer["tokenizer_digest"],
    }


def inspect(target: LocalModelEntry, draft: LocalModelEntry) -> dict[str, object]:
    if not target.compatible or not draft.compatible:
        raise ValueError("local_model_runtime_compatibility_required")
    target_identity = snapshot_identity(target)
    draft_identity = snapshot_identity(draft)
    checks = {
        "active_position_semantics": target_identity[
            "active_position_semantics_digest"
        ]
        == draft_identity["active_position_semantics_digest"],
        "byte_fallback": target_identity["byte_fallback_digest"]
        == draft_identity["byte_fallback_digest"],
        "canonical_token_id_mapping": target_identity[
            "canonical_token_id_mapping_digest"
        ]
        == draft_identity["canonical_token_id_mapping_digest"],
        "canonical_token_count": target_identity["canonical_token_count"]
        == draft_identity["canonical_token_count"],
        "draft_output_domain_covers_tokenizer": draft_identity[
            "model_output_domain_covers_tokenizer"
        ],
        "special_token_semantics": target_identity[
            "special_token_semantics_digest"
        ]
        == draft_identity["special_token_semantics_digest"],
        "normalization": target_identity["normalization_digest"]
        == draft_identity["normalization_digest"],
        "target_output_domain_covers_tokenizer": target_identity[
            "model_output_domain_covers_tokenizer"
        ],
        "tokenizer_implementation": target_identity[
            "tokenizer_implementation_digest"
        ]
        == draft_identity["tokenizer_implementation_digest"],
    }
    reasons = sorted(name for name, passed in checks.items() if passed is not True)
    return {
        "protocol": PROTOCOL,
        "claim_boundary": "design_input_only",
        "network_access": False,
        "target": target_identity,
        "draft": draft_identity,
        "checks": checks,
        "static_compatibility_observed": not reasons,
        "blockers": reasons,
        "runtime_compatibility_evaluated": False,
        "unresolved_runtime_prerequisites": list(RUNTIME_PREREQUISITES),
        "route_ready": False,
        "qualification_evaluated": False,
    }


def _entry(
    entries: tuple[LocalModelEntry, ...], model_id: str, revision: str
) -> LocalModelEntry:
    matches = [
        entry
        for entry in entries
        if entry.model_id == model_id and entry.revision == revision
    ]
    if len(matches) != 1:
        raise ValueError("exact_local_model_snapshot_required")
    return matches[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hf-cache", type=Path, required=True)
    parser.add_argument("--target-model-id", required=True)
    parser.add_argument("--target-revision", required=True)
    parser.add_argument("--draft-model-id", required=True)
    parser.add_argument("--draft-revision", required=True)
    arguments = parser.parse_args()
    entries = scan_huggingface_cache(arguments.hf_cache)
    result = inspect(
        _entry(entries, arguments.target_model_id, arguments.target_revision),
        _entry(entries, arguments.draft_model_id, arguments.draft_revision),
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
