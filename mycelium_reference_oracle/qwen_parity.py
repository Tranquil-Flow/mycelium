"""Independent comparison of Qwen reference and distributed logits archives."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping, Sequence

import numpy as np


POLICY_PROTOCOL = "mycelium.qwen_parity_policy.v1"
DISTRIBUTED_PROTOCOL = "mycelium.qwen_distributed_capture.v1"
PARITY_PROTOCOL = "mycelium.model_reference_parity.v1"
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_REVISION = re.compile(r"[0-9a-f]{40}\Z")


class QwenParityError(RuntimeError):
    """A bounded reference/distributed parity failure."""


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _digest(value: object) -> str:
    return _digest_bytes(_canonical_bytes(value))


def _valid_digest(value: object) -> bool:
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def _finite_nonnegative(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and value >= 0
    )


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise QwenParityError("qwen_parity_document_invalid")
    return value


def validate_policy(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "protocol",
        "model_id",
        "revision",
        "source_artifact_digest",
        "representation_digest",
        "corpus_digest",
        "absolute_tolerance",
        "relative_tolerance",
        "frozen_at_unix_ms",
    }:
        raise QwenParityError("qwen_parity_policy_invalid")
    if (
        value.get("protocol") != POLICY_PROTOCOL
        or not isinstance(value.get("model_id"), str)
        or not value["model_id"].startswith("Qwen/")
        or not isinstance(value.get("revision"), str)
        or _REVISION.fullmatch(value["revision"]) is None
        or not all(
            _valid_digest(value.get(field))
            for field in (
                "source_artifact_digest",
                "representation_digest",
                "corpus_digest",
            )
        )
        or not _finite_nonnegative(value.get("absolute_tolerance"))
        or not _finite_nonnegative(value.get("relative_tolerance"))
        or type(value.get("frozen_at_unix_ms")) is not int
        or value["frozen_at_unix_ms"] <= 0
    ):
        raise QwenParityError("qwen_parity_policy_invalid")
    return json.loads(json.dumps(dict(value)))


def _archive(path: Path, descriptor: Mapping[str, Any]) -> dict[str, np.ndarray]:
    if (
        descriptor.get("file_name") != path.name
        or descriptor.get("content_digest") != _digest_bytes(path.read_bytes())
        or descriptor.get("dtype") != "float32"
        or type(descriptor.get("array_count")) is not int
    ):
        raise QwenParityError("qwen_logits_archive_invalid")
    with np.load(path, allow_pickle=False) as archive:
        arrays = {name: np.array(archive[name], dtype=np.float32, copy=True) for name in archive.files}
    if len(arrays) != descriptor["array_count"]:
        raise QwenParityError("qwen_logits_archive_invalid")
    return arrays


def _cases(value: object, *, label: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list) or not value:
        raise QwenParityError(f"qwen_{label}_cases_invalid")
    result = []
    seen: set[str] = set()
    for case in value:
        if not isinstance(case, Mapping):
            raise QwenParityError(f"qwen_{label}_cases_invalid")
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or not case_id or case_id in seen:
            raise QwenParityError(f"qwen_{label}_cases_invalid")
        seen.add(case_id)
        result.append(case)
    return result


def _steps(value: object, *, label: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list) or not value:
        raise QwenParityError(f"qwen_{label}_steps_invalid")
    for index, step in enumerate(value):
        if (
            not isinstance(step, Mapping)
            or step.get("index") != index
            or type(step.get("token_id")) is not int
            or not isinstance(step.get("logits_key"), str)
            or not _valid_digest(step.get("logits_digest"))
        ):
            raise QwenParityError(f"qwen_{label}_steps_invalid")
    return value


def compare(
    *,
    policy: Mapping[str, Any],
    reference: Mapping[str, Any],
    reference_logits: Path,
    distributed: Mapping[str, Any],
    distributed_logits: Path,
) -> dict[str, Any]:
    frozen = validate_policy(policy)
    identity = (frozen["model_id"], frozen["revision"])
    if (
        reference.get("protocol") != "mycelium.qwen_transformers_reference.v1"
        or (reference.get("model_id"), reference.get("revision")) != identity
        or reference.get("source_artifact_digest")
        != frozen["source_artifact_digest"]
        or reference.get("corpus_digest") != frozen["corpus_digest"]
        or reference.get("reference_process_participates_in_route") is not False
        or distributed.get("protocol") != DISTRIBUTED_PROTOCOL
        or (distributed.get("model_id"), distributed.get("revision")) != identity
        or distributed.get("source_artifact_digest")
        != frozen["source_artifact_digest"]
        or distributed.get("representation_digest")
        != frozen["representation_digest"]
        or distributed.get("corpus_digest") != frozen["corpus_digest"]
        or distributed.get("simulated") is not False
        or distributed.get("backend_fallback") is not False
        or not _valid_digest(distributed.get("runtime_digest"))
        or not _valid_digest(reference.get("tokenizer_digest"))
        or distributed.get("tokenizer_digest") != reference.get("tokenizer_digest")
    ):
        raise QwenParityError("qwen_parity_binding_mismatch")
    reference_runtime = reference.get("runtime")
    if not isinstance(reference_runtime, Mapping) or not _valid_digest(
        reference_runtime.get("runtime_digest")
    ):
        raise QwenParityError("qwen_reference_runtime_invalid")
    reference_arrays = _archive(
        reference_logits,
        reference.get("logits_archive")
        if isinstance(reference.get("logits_archive"), Mapping)
        else {},
    )
    distributed_arrays = _archive(
        distributed_logits,
        distributed.get("logits_archive")
        if isinstance(distributed.get("logits_archive"), Mapping)
        else {},
    )
    reference_cases = _cases(reference.get("cases"), label="reference")
    distributed_cases = _cases(distributed.get("cases"), label="distributed")
    by_id = {case["case_id"]: case for case in distributed_cases}
    if set(by_id) != {case["case_id"] for case in reference_cases}:
        raise QwenParityError("qwen_parity_case_mismatch")

    absolute_tolerance = float(frozen["absolute_tolerance"])
    relative_tolerance = float(frozen["relative_tolerance"])
    cases = []
    for reference_case in reference_cases:
        distributed_case = by_id[reference_case["case_id"]]
        if (
            reference_case.get("prompt_tokens_digest")
            != distributed_case.get("prompt_tokens_digest")
            or not _valid_digest(reference_case.get("prompt_tokens_digest"))
        ):
            raise QwenParityError("qwen_parity_prompt_mismatch")
        reference_steps = _steps(reference_case.get("steps"), label="reference")
        distributed_steps = _steps(distributed_case.get("steps"), label="distributed")
        if len(reference_steps) != len(distributed_steps):
            raise QwenParityError("qwen_parity_step_mismatch")
        maximum_absolute = 0.0
        maximum_relative = 0.0
        finite = True
        within = True
        for reference_step, distributed_step in zip(
            reference_steps, distributed_steps, strict=True
        ):
            reference_array = reference_arrays.get(reference_step["logits_key"])
            distributed_array = distributed_arrays.get(distributed_step["logits_key"])
            if (
                reference_array is None
                or distributed_array is None
                or reference_array.shape != distributed_array.shape
                or _digest_bytes(reference_array.tobytes(order="C"))
                != reference_step["logits_digest"]
                or _digest_bytes(distributed_array.tobytes(order="C"))
                != distributed_step["logits_digest"]
            ):
                raise QwenParityError("qwen_parity_logits_binding_mismatch")
            finite_step = bool(
                np.isfinite(reference_array).all()
                and np.isfinite(distributed_array).all()
            )
            finite = finite and finite_step
            if not finite_step:
                within = False
                continue
            absolute = np.abs(reference_array - distributed_array)
            relative = absolute / np.maximum(np.abs(reference_array), 1e-12)
            maximum_absolute = max(maximum_absolute, float(np.max(absolute)))
            maximum_relative = max(maximum_relative, float(np.max(relative)))
            within = within and bool(
                np.all(
                    absolute
                    <= absolute_tolerance
                    + relative_tolerance * np.abs(reference_array)
                )
            )
        reference_tokens = [step["token_id"] for step in reference_steps]
        distributed_tokens = [step["token_id"] for step in distributed_steps]
        cases.append(
            {
                "case_id": reference_case["case_id"],
                "prompt_tokens_digest": reference_case["prompt_tokens_digest"],
                "greedy_tokens_match": reference_tokens == distributed_tokens,
                "finite_logits": finite,
                "within_tolerance": within,
                "maximum_logit_error": maximum_absolute,
                "maximum_relative_logit_error": maximum_relative,
            }
        )
    result_without_digest = {
        "protocol": PARITY_PROTOCOL,
        "model_id": frozen["model_id"],
        "revision": frozen["revision"],
        "source_artifact_digest": frozen["source_artifact_digest"],
        "representation_digest": frozen["representation_digest"],
        "tokenizer_digest": reference["tokenizer_digest"],
        "policy_digest": _digest(frozen),
        "reference_process_participates_in_route": False,
        "reference_runtime_digest": reference_runtime["runtime_digest"],
        "distributed_runtime_digest": distributed["runtime_digest"],
        "logit_tolerance": absolute_tolerance,
        "relative_logit_tolerance": relative_tolerance,
        "cases": cases,
        "passed": all(
            case["greedy_tokens_match"]
            and case["finite_logits"]
            and case["within_tolerance"]
            for case in cases
        ),
    }
    return {
        **result_without_digest,
        "parity_digest": _digest(result_without_digest),
    }


def _write_private(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare offline Qwen reference and distributed logits evidence."
    )
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--reference-logits", type=Path, required=True)
    parser.add_argument("--distributed", type=Path, required=True)
    parser.add_argument("--distributed-logits", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = compare(
        policy=_read(args.policy),
        reference=_read(args.reference),
        reference_logits=args.reference_logits,
        distributed=_read(args.distributed),
        distributed_logits=args.distributed_logits,
    )
    _write_private(args.output, result)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "passed": result["passed"],
                "case_count": len(result["cases"]),
                "parity_digest": result["parity_digest"],
            },
            sort_keys=True,
        )
    )
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DISTRIBUTED_PROTOCOL",
    "PARITY_PROTOCOL",
    "POLICY_PROTOCOL",
    "QwenParityError",
    "compare",
    "validate_policy",
]
