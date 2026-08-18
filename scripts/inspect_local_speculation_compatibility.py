#!/usr/bin/env python3
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
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def tokenizer_identity(snapshot: Path) -> dict[str, object]:
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
    token_ids: set[int] = set()
    for token_id in vocab.values():
        if type(token_id) is not int or token_id < 0:
            raise ValueError("local_tokenizer_vocabulary_invalid")
        token_ids.add(token_id)
    for record in added:
        if not isinstance(record, dict) or type(record.get("id")) is not int:
            raise ValueError("local_tokenizer_vocabulary_invalid")
        token_id = record["id"]
        if token_id < 0:
            raise ValueError("local_tokenizer_vocabulary_invalid")
        token_ids.add(token_id)
    if not token_ids or token_ids != set(range(max(token_ids) + 1)):
        raise ValueError("local_tokenizer_vocabulary_noncontiguous")
    special = {
        key: tokenizer_config.get(key)
        for key in (
            "bos_token",
            "eos_token",
            "pad_token",
            "unk_token",
            "added_tokens_decoder",
        )
    }
    return {
        "canonical_token_count": len(token_ids),
        "maximum_canonical_token_id": max(token_ids),
        "special_token_semantics_digest": _digest_json(special),
        "tokenizer_digest": _digest_bytes(
            tokenizer_path.resolve(strict=True).read_bytes()
        ),
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
    tokenizer = tokenizer_identity(entry.snapshot_path)
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
        "maximum_canonical_token_id": tokenizer["maximum_canonical_token_id"],
        "model_id": entry.model_id,
        "model_output_domain_covers_tokenizer": model_vocab_size >= canonical_count,
        "model_output_domain_size": model_vocab_size,
        "revision": entry.revision,
        "special_token_semantics_digest": tokenizer[
            "special_token_semantics_digest"
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
        "canonical_token_count": target_identity["canonical_token_count"]
        == draft_identity["canonical_token_count"],
        "draft_output_domain_covers_tokenizer": draft_identity[
            "model_output_domain_covers_tokenizer"
        ],
        "special_token_semantics": target_identity[
            "special_token_semantics_digest"
        ]
        == draft_identity["special_token_semantics_digest"],
        "target_output_domain_covers_tokenizer": target_identity[
            "model_output_domain_covers_tokenizer"
        ],
        "tokenizer": target_identity["tokenizer_digest"]
        == draft_identity["tokenizer_digest"],
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
