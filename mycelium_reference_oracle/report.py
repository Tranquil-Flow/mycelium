"""Canonical disclosure-minimized reporting for the tiny GPT-2 oracle."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import mlx.core as mx

from .gpt2 import (
    ABSOLUTE_TOLERANCE,
    IMPLEMENTATION_VERSION,
    RELATIVE_TOLERANCE,
    GPT2FixtureOracle,
    OracleValidationError,
)

REPORT_PROTOCOL = "mycelium.independent_reference_oracle_report.v1"
CLAIM_BOUNDARY = (
    "local independent numerical oracle only; no transport, no distributed execution, "
    "no physical-host qualification, and no route_ready claim"
)
_IMPLEMENTATION = {
    "algorithm": "direct-gpt2-fp32-from-local-safetensors",
    "name": "mycelium_reference_oracle.gpt2",
    "version": IMPLEMENTATION_VERSION,
}
_SOURCE_FILES = ("gpt2.py", "init.py", "report.py")
_DIGEST_PREFIX = "sha256:"


def _canonical_text(document: Any) -> str:
    return json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _source_digest() -> str:
    digest = hashlib.sha256()
    root = Path(__file__).parent
    for name in _SOURCE_FILES:
        raw = (root / name).read_bytes()
        encoded_name = name.encode("utf-8")
        digest.update(len(encoded_name).to_bytes(8, "big"))
        digest.update(encoded_name)
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
    return _DIGEST_PREFIX + digest.hexdigest()


def _mlx_version() -> str:
    try:
        return version("mlx")
    except PackageNotFoundError:
        return "unknown"


def _implementation_identity(source_digest: str, runtime_version: str) -> str:
    encoded = _canonical_text(
        {
            **_IMPLEMENTATION,
            "source_digest": source_digest,
            "numeric_runtime_version": runtime_version,
        }
    ).encode("utf-8")
    return _DIGEST_PREFIX + hashlib.sha256(encoded).hexdigest()


def _is_digest(value: Any) -> bool:
    if not isinstance(value, str) or not value.startswith(_DIGEST_PREFIX):
        return False
    suffix = value.removeprefix(_DIGEST_PREFIX)
    return len(suffix) == 64 and all(character in "0123456789abcdef" for character in suffix)


def _require_keys(value: Any, expected: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise OracleValidationError(f"canonical report {label} schema is invalid")
    return value


def _validate_report_schema(report: dict[str, Any]) -> None:
    top = _require_keys(
        report,
        {
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
        },
        "top-level",
    )
    if (
        top["protocol"] != REPORT_PROTOCOL
        or top["claim_boundary"] != CLAIM_BOUNDARY
        or top["qualified"] is not True
        or top["route_ready"] is not False
    ):
        raise OracleValidationError("canonical report fixed claims are invalid")

    implementation = _require_keys(
        top["implementation"],
        {"algorithm", "identity", "name", "source_digest", "version"},
        "implementation",
    )
    if (
        any(implementation[key] != value for key, value in _IMPLEMENTATION.items())
        or not _is_digest(implementation["identity"])
        or not _is_digest(implementation["source_digest"])
    ):
        raise OracleValidationError("canonical report implementation identity is invalid")

    runtime = _require_keys(
        top["numeric_runtime"],
        {"backend", "device", "dtype", "version"},
        "numeric runtime",
    )
    if (
        runtime["backend"] != "mlx.core"
        or runtime["dtype"] != "float32"
        or not isinstance(runtime["device"], str)
        or re.fullmatch(r"Device\(gpu, [0-9]+\)", runtime["device"]) is None
        or not isinstance(runtime["version"], str)
        or re.fullmatch(r"[0-9A-Za-z.+-]{1,64}", runtime["version"]) is None
    ):
        raise OracleValidationError("canonical report numeric runtime is invalid")

    model = _require_keys(
        top["model"],
        {
            "checkpoint_index_digest",
            "config_digest",
            "identity_digest",
            "tensor_artifact_digests",
            "tensor_set_digest",
            "tensor_value_digest",
        },
        "model",
    )
    scalar_model_digests = (
        model["checkpoint_index_digest"],
        model["config_digest"],
        model["identity_digest"],
        model["tensor_set_digest"],
        model["tensor_value_digest"],
    )
    artifacts = model["tensor_artifact_digests"]
    if (
        not all(_is_digest(item) for item in scalar_model_digests)
        or not isinstance(artifacts, list)
        or len(artifacts) != 2
        or not all(_is_digest(item) for item in artifacts)
    ):
        raise OracleValidationError("canonical report model evidence is invalid")

    prompt = _require_keys(top["prompt"], {"digest", "token_count"}, "prompt")
    if not _is_digest(prompt["digest"]) or prompt["token_count"] != 1:
        raise OracleValidationError("canonical report prompt evidence is invalid")

    generation = _require_keys(
        top["generation"],
        {"greedy_step_count", "mode", "steps"},
        "generation",
    )
    steps = generation["steps"]
    if (
        generation["mode"] != "greedy_full_context_recompute"
        or generation["greedy_step_count"] != 8
        or not isinstance(steps, list)
        or len(steps) != 8
    ):
        raise OracleValidationError("canonical report generation evidence is invalid")
    for index, step_value in enumerate(steps):
        step = _require_keys(
            step_value,
            {"activation_digests", "index", "logits_digest", "token_id"},
            "generation step",
        )
        activations = step["activation_digests"]
        if (
            step["index"] != index
            or type(step["token_id"]) is not int
            or not 0 <= step["token_id"] < 7
            or not _is_digest(step["logits_digest"])
            or not isinstance(activations, list)
            or len(activations) != 2
            or not all(_is_digest(item) for item in activations)
        ):
            raise OracleValidationError("canonical report generation step is invalid")

    tolerances = _require_keys(
        top["tolerances"], {"absolute", "relative"}, "tolerances"
    )
    if tolerances != {
        "absolute": ABSOLUTE_TOLERANCE,
        "relative": RELATIVE_TOLERANCE,
    }:
        raise OracleValidationError("canonical report tolerances are invalid")


def canonical_report_json(report: dict[str, Any]) -> str:
    """Validate and serialize one closed-schema report with a final newline."""

    _validate_report_schema(report)
    return _canonical_text(report) + "\n"


def build_report(
    oracle: GPT2FixtureOracle,
    prompt_token_ids: Sequence[int],
    *,
    steps: int,
    expected_token_ids: Sequence[int] | None = None,
    expected_prompt_digest: str | None = None,
    expected_config_digest: str | None = None,
    expected_model_digest: str | None = None,
) -> dict[str, Any]:
    """Run the identity-bound eight-step challenge and return digest-only evidence."""

    prompt = tuple(prompt_token_ids)
    if any(
        value is None
        for value in (
            expected_token_ids,
            expected_prompt_digest,
            expected_config_digest,
            expected_model_digest,
        )
    ):
        raise OracleValidationError(
            "qualification report requires trusted identity bindings"
        )
    if steps != 8:
        raise OracleValidationError("qualification report requires exactly eight steps")
    expected_tokens = tuple(expected_token_ids or ())
    if len(expected_tokens) != 8 or any(type(token) is not int for token in expected_tokens):
        raise OracleValidationError("qualification report expected token sequence is invalid")

    device = str(mx.default_device())
    if "gpu" not in device.lower():
        raise OracleValidationError(
            "current tiny-fixture qualification report requires the MLX GPU backend"
        )
    generation = oracle.greedy_decode(
        prompt,
        steps=steps,
        expected_prompt_digest=expected_prompt_digest,
        expected_config_digest=expected_config_digest,
        expected_model_digest=expected_model_digest,
    )
    if generation.generated_token_ids != expected_tokens:
        raise OracleValidationError("qualification report token sequence mismatch")

    source_digest = _source_digest()
    runtime_version = _mlx_version()
    if runtime_version == "unknown":
        raise OracleValidationError(
            "current tiny-fixture qualification report requires an identified MLX version"
        )
    report = {
        "protocol": REPORT_PROTOCOL,
        "qualified": True,
        "route_ready": False,
        "claim_boundary": CLAIM_BOUNDARY,
        "implementation": {
            **_IMPLEMENTATION,
            "source_digest": source_digest,
            "identity": _implementation_identity(source_digest, runtime_version),
        },
        "numeric_runtime": {
            "backend": "mlx.core",
            "device": device,
            "dtype": "float32",
            "version": runtime_version,
        },
        "model": {
            "config_digest": oracle.identity.config_digest,
            "identity_digest": oracle.identity.model_digest,
            "checkpoint_index_digest": oracle.identity.checkpoint_index_digest,
            "tensor_artifact_digests": list(
                oracle.identity.tensor_artifact_digests
            ),
            "tensor_set_digest": oracle.identity.tensor_set_digest,
            "tensor_value_digest": oracle.identity.tensor_value_digest,
        },
        "prompt": {
            "digest": generation.prompt_digest,
            "token_count": len(prompt),
        },
        "generation": {
            "mode": "greedy_full_context_recompute",
            "greedy_step_count": len(generation.steps),
            "steps": [
                {
                    "index": step.index,
                    "token_id": step.token_id,
                    "logits_digest": step.logits_digest,
                    "activation_digests": list(step.activation_digests),
                }
                for step in generation.steps
            ],
        },
        "tolerances": {
            "absolute": ABSOLUTE_TOLERANCE,
            "relative": RELATIVE_TOLERANCE,
        },
    }
    _validate_report_schema(report)
    return report
