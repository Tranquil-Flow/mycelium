# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import hashlib
import json

import pytest

from mycelium_a6_recovery import build_recovery_intent, validate_recovery_intent

SHA = "sha256:" + "a" * 64
BINDING = {
    "deployment_id": "deployment-a6",
    "deployment_epoch": 1,
    "topology_generation": 1,
    "model_id": "model-a6",
    "model_revision": "immutable-revision",
    "representation_digest": SHA,
    "graph_digest": SHA,
}


def _digest(document: dict[str, object]) -> str:
    encoded = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _valid_intent() -> dict[str, object]:
    return build_recovery_intent(
        BINDING,
        retained_path_ids=("path-source",),
        candidate_path_ids=("path-successor",),
        equivalent_candidate_generations=3,
        candidate_first_seen_unix_ms=1_000,
        generated_at_unix_ms=4_000,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("candidate_path_ids", ["duplicate", "duplicate"]),
        ("candidate_path_ids", [f"candidate-{index}" for index in range(65)]),
        ("retained_path_ids", ["x" * 129]),
        ("rejection_reasons", ["x" * 129]),
    ],
)
def test_recovery_intent_rejects_duplicate_or_unbounded_lists(
    field: str, value: list[str]
) -> None:
    intent = _valid_intent()
    intent[field] = value
    body = dict(intent)
    body.pop("intent_digest")
    intent["intent_digest"] = _digest(body)

    with pytest.raises(ValueError, match="recovery_intent_invalid"):
        validate_recovery_intent(intent)
