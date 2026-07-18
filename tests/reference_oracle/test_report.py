from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from conftest import (
    EIGHT_STEP_PROMPT,
    EXPECTED_CONFIG_DIGEST,
    EXPECTED_EIGHT_TOKENS,
    EXPECTED_MODEL_DIGEST,
    EXPECTED_PROMPT_DIGEST,
    MODEL_ID,
    RESOLVED_COMMIT,
)
from mycelium_reference_oracle.gpt2 import (
    ABSOLUTE_TOLERANCE,
    RELATIVE_TOLERANCE,
    OracleValidationError,
    load_gpt2_fixture,
    prompt_digest,
)
from mycelium_reference_oracle.report import (
    REPORT_PROTOCOL,
    build_report,
    canonical_report_json,
)

EXPECTED_SOURCE_DIGEST = (
    "sha256:1eb3546f8671e26db803b387fa24878376a42f4e18c5cfb2b827bf5822bde2d6"
)
EXPECTED_IMPLEMENTATION_IDENTITY = (
    "sha256:64623fda74286f837e9a79b83d2e0a48d6227e13852f1761389d8d62ec70569f"
)
EXPECTED_CANONICAL_REPORT_DIGEST = (
    "sha256:cb3a71488345f6b4aeec556e1417db3490bc1ca3964a9566d07d0860cd5983cb"
)


def all_keys(value: Any):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from all_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from all_keys(item)


def load_oracle(fixture_dir: Path):
    return load_gpt2_fixture(
        fixture_dir,
        model_id=MODEL_ID,
        resolved_commit=RESOLVED_COMMIT,
    )


def bound_report(oracle):
    return build_report(
        oracle,
        EIGHT_STEP_PROMPT,
        steps=8,
        expected_token_ids=EXPECTED_EIGHT_TOKENS,
        expected_prompt_digest=EXPECTED_PROMPT_DIGEST,
        expected_config_digest=EXPECTED_CONFIG_DIGEST,
        expected_model_digest=EXPECTED_MODEL_DIGEST,
    )


def test_emits_canonical_privacy_safe_eight_step_report(fixture_dir: Path) -> None:
    oracle = load_oracle(fixture_dir)
    report = bound_report(oracle)
    encoded = canonical_report_json(report)
    reparsed = json.loads(encoded)

    assert reparsed == report
    assert encoded == canonical_report_json(report)
    assert encoded.endswith("\n")
    assert report["protocol"] == REPORT_PROTOCOL
    assert report["qualified"] is True
    assert report["route_ready"] is False
    assert report["implementation"]["identity"] == EXPECTED_IMPLEMENTATION_IDENTITY
    assert report["implementation"]["source_digest"] == EXPECTED_SOURCE_DIGEST
    assert "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest() == (
        EXPECTED_CANONICAL_REPORT_DIGEST
    )
    assert report["model"] == {
        "config_digest": oracle.identity.config_digest,
        "identity_digest": oracle.identity.model_digest,
        "checkpoint_index_digest": oracle.identity.checkpoint_index_digest,
        "tensor_artifact_digests": list(oracle.identity.tensor_artifact_digests),
        "tensor_set_digest": oracle.identity.tensor_set_digest,
        "tensor_value_digest": oracle.identity.tensor_value_digest,
    }
    assert report["prompt"] == {
        "digest": prompt_digest(EIGHT_STEP_PROMPT),
        "token_count": 1,
    }
    assert report["tolerances"] == {
        "absolute": ABSOLUTE_TOLERANCE,
        "relative": RELATIVE_TOLERANCE,
    }
    assert report["generation"]["greedy_step_count"] == 8
    assert tuple(
        step["token_id"] for step in report["generation"]["steps"]
    ) == EXPECTED_EIGHT_TOKENS
    for index, step in enumerate(report["generation"]["steps"]):
        assert step["index"] == index
        assert step["logits_digest"].startswith("sha256:")
        assert len(step["activation_digests"]) == 2
        assert all(item.startswith("sha256:") for item in step["activation_digests"])

    assert set(report) == {
        "claim_boundary",
        "generation",
        "implementation",
        "model",
        "numeric_runtime",
        "prompt",
        "protocol",
        "qualified",
        "route_ready",
        "tolerances",
    }
    assert set(report["implementation"]) == {
        "algorithm",
        "identity",
        "name",
        "source_digest",
        "version",
    }
    assert set(report["numeric_runtime"]) == {
        "backend",
        "device",
        "dtype",
        "version",
    }
    assert set(report["model"]) == {
        "checkpoint_index_digest",
        "config_digest",
        "identity_digest",
        "tensor_artifact_digests",
        "tensor_set_digest",
        "tensor_value_digest",
    }
    assert set(report["prompt"]) == {"digest", "token_count"}
    assert set(report["generation"]) == {
        "greedy_step_count",
        "mode",
        "steps",
    }
    assert all(
        set(step) == {"activation_digests", "index", "logits_digest", "token_id"}
        for step in report["generation"]["steps"]
    )

    forbidden_key_fragments = (
        "path",
        "credential",
        "secret",
        "prompt_text",
        "prompt_token",
        "tensor_values",
        "model_weights",
    )
    for key in all_keys(report):
        assert not any(fragment in key.lower() for fragment in forbidden_key_fragments)
    assert str(fixture_dir) not in encoded
    assert MODEL_ID not in encoded
    assert RESOLVED_COMMIT not in encoded
    assert "prompt text sentinel" not in encoded


def test_canonical_serializer_rejects_added_raw_prompt_field(fixture_dir: Path) -> None:
    report = bound_report(load_oracle(fixture_dir))
    report["prompt"]["prompt_token_ids"] = [1]

    with pytest.raises(OracleValidationError, match="prompt schema"):
        canonical_report_json(report)


def test_report_requires_bound_eight_step_exact_decode(fixture_dir: Path) -> None:
    oracle = load_oracle(fixture_dir)

    with pytest.raises(OracleValidationError, match="requires trusted identity bindings"):
        build_report(oracle, EIGHT_STEP_PROMPT, steps=8)
    with pytest.raises(OracleValidationError, match="exactly eight"):
        build_report(
            oracle,
            EIGHT_STEP_PROMPT,
            steps=1,
            expected_token_ids=(6,),
            expected_prompt_digest=EXPECTED_PROMPT_DIGEST,
            expected_config_digest=EXPECTED_CONFIG_DIGEST,
            expected_model_digest=EXPECTED_MODEL_DIGEST,
        )
    with pytest.raises(OracleValidationError, match="token sequence mismatch"):
        build_report(
            oracle,
            EIGHT_STEP_PROMPT,
            steps=8,
            expected_token_ids=(0,) * 8,
            expected_prompt_digest=EXPECTED_PROMPT_DIGEST,
            expected_config_digest=EXPECTED_CONFIG_DIGEST,
            expected_model_digest=EXPECTED_MODEL_DIGEST,
        )


def test_report_scope_stops_at_local_numerical_oracle(fixture_dir: Path) -> None:
    report = bound_report(load_oracle(fixture_dir))

    boundary = report["claim_boundary"]
    assert "local independent numerical oracle only" in boundary
    assert "no transport" in boundary
    assert "no distributed execution" in boundary
    assert "no physical-host" in boundary
    assert "no route_ready" in boundary
