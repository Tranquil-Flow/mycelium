"""Offline logit capture through assignment-bound split Qwen serving stages."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping, Sequence

import numpy as np

from layer_assignment import assignment_id_for
from runtime_loader import load_assignment_stage, select_stage_backend

from mycelium_reference_oracle.qwen_parity import DISTRIBUTED_PROTOCOL
from mycelium_reference_oracle.qwen_transformers import (
    tokenizer_identity,
    validate_corpus,
)


IMPLEMENTATION_VERSION = "qwen-assignment-split-capture-v1"
CLAIM_BOUNDARY = (
    "offline assignment-bound split-stage execution; no route qualification, "
    "physical transport, selection, or serving claim"
)
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


class QwenDistributedCaptureError(RuntimeError):
    """A bounded distributed-candidate capture failure."""


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _json_mutable(value: Any) -> Any:
    """Copy immutable runtime evidence into ordinary JSON-compatible values."""

    if isinstance(value, Mapping):
        return {str(key): _json_mutable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_mutable(item) for item in value]
    return value


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise QwenDistributedCaptureError("distributed_capture_input_invalid") from exc
    if not isinstance(value, dict):
        raise QwenDistributedCaptureError("distributed_capture_input_invalid")
    return value


def _private_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
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
        Path(temporary).unlink(missing_ok=True)
        raise


def _private_npz(path: Path, arrays: Mapping[str, np.ndarray]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".npz", dir=path.parent
    )
    try:
        os.fchmod(descriptor, 0o600)
        os.close(descriptor)
        descriptor = -1
        np.savez_compressed(temporary, **arrays)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        Path(temporary).unlink(missing_ok=True)
        raise


def _rebase_artifacts(
    assignment: Mapping[str, Any],
    report: Mapping[str, Any],
    *,
    deployment_root: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Bind an immutable assignment to its local candidate-bundle materialization."""

    root = deployment_root.resolve(strict=True)
    assignment_copy = copy.deepcopy(dict(assignment))
    report_copy = copy.deepcopy(dict(report))
    assignment_copy["artifact_cache_root"] = str(root)
    # The cache root is part of the assignment's semantic identity.  Offline
    # parity executes an explicitly local rematerialization of the immutable
    # product assignment, so derive a new identity instead of retaining an ID
    # that no longer authenticates the assignment document.
    assignment_copy["assignment_id"] = assignment_id_for(assignment_copy)
    report_copy["assignment_id"] = assignment_copy["assignment_id"]
    report_copy["artifact_cache_root"] = str(root)
    report_copy["resolved_artifact_cache_root"] = str(root)
    files = report_copy.get("verified_files")
    if not isinstance(files, list) or not files:
        raise QwenDistributedCaptureError("distributed_artifact_report_invalid")
    for record in files:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise QwenDistributedCaptureError("distributed_artifact_report_invalid")
        target = root / record["path"]
        if not target.is_file() or target.is_symlink():
            raise QwenDistributedCaptureError("distributed_artifact_missing")
        record["local_path"] = str(target)
    for field in (
        "stage_pack",
        "stage_pack_manifest",
        "stage_pack_verification",
        "stage_pack_digest",
        "stage_pack_verification_digest",
    ):
        report_copy.pop(field, None)
    return assignment_copy, report_copy


def load_bundle_stages(bundle_root: Path) -> tuple[Any, ...]:
    """Load every contiguous candidate assignment from local verified bytes."""

    root = Path(bundle_root).resolve(strict=True)
    control = root / "control"
    deployment = root / "deployment"
    assignment_paths = sorted(control.glob("node-*-assignment.json"))
    if len(assignment_paths) < 2:
        raise QwenDistributedCaptureError("distributed_assignments_unavailable")
    loaded = []
    cursor = 0
    for index, assignment_path in enumerate(assignment_paths):
        prefix = assignment_path.name.removesuffix("-assignment.json")
        report_path = control / f"{prefix}-artifact-report.json"
        proof_path = control / f"{prefix}-load-proof.json"
        assignment, report = _rebase_artifacts(
            _read(assignment_path),
            _read(report_path),
            deployment_root=deployment,
        )
        layer_range = assignment.get("range")
        if (
            not isinstance(layer_range, Mapping)
            or layer_range.get("start_layer") != cursor
            or type(layer_range.get("end_layer_exclusive")) is not int
            or layer_range["end_layer_exclusive"] <= cursor
        ):
            raise QwenDistributedCaptureError("distributed_assignment_order_invalid")
        cursor = layer_range["end_layer_exclusive"]
        prior_proof = _read(proof_path)
        load_generation = prior_proof.get("load_generation")
        if type(load_generation) is not int or load_generation <= 0:
            raise QwenDistributedCaptureError("distributed_load_generation_invalid")
        loaded.append(
            load_assignment_stage(
                assignment,
                report,
                load_generation=load_generation,
            )
        )
    return tuple(loaded)


def capture_loaded_stages(
    *,
    loaded_stages: Sequence[Any],
    encode: Any,
    prompts: Sequence[Mapping[str, Any]],
    maximum_new_tokens: int,
) -> tuple[list[dict[str, Any]], dict[str, np.ndarray]]:
    """Run deterministic full-context replay through every loaded stage."""

    if len(loaded_stages) < 2 or not callable(encode):
        raise QwenDistributedCaptureError("distributed_capture_stages_invalid")
    try:
        import mlx.core as mx
    except ImportError as exc:
        raise QwenDistributedCaptureError("distributed_mlx_unavailable") from exc
    backends = [
        select_stage_backend(
            runtime=_json_mutable(stage.proof["runtime"]),
            prefer="auto",
        )
        for stage in loaded_stages
    ]
    arrays: dict[str, np.ndarray] = {}
    cases: list[dict[str, Any]] = []
    for case_index, prompt in enumerate(prompts):
        case_id = prompt.get("case_id")
        text = prompt.get("text")
        if not isinstance(case_id, str) or not isinstance(text, str):
            raise QwenDistributedCaptureError("distributed_capture_prompt_invalid")
        prompt_ids = tuple(int(item) for item in encode(text))
        if not prompt_ids:
            raise QwenDistributedCaptureError("distributed_capture_prompt_invalid")
        generated: list[int] = []
        steps = []
        for step_index in range(maximum_new_tokens):
            hidden: Any = mx.array([(*prompt_ids, *generated)], dtype=mx.int32)
            for stage_index, (stage, backend) in enumerate(
                zip(loaded_stages, backends, strict=True)
            ):
                hidden = backend.execute_loaded_stage(
                    stage,
                    token_ids=hidden if stage_index == 0 else None,
                    hidden_states=None if stage_index == 0 else hidden,
                )
            logits = np.asarray(hidden, dtype=np.float32)[0, -1, :]
            if not bool(np.isfinite(logits).all()):
                raise QwenDistributedCaptureError("distributed_non_finite_logits")
            token_id = int(np.argmax(logits))
            key = f"case_{case_index:03}_step_{step_index:03}"
            arrays[key] = np.array(logits, dtype=np.float32, copy=True)
            steps.append(
                {
                    "index": step_index,
                    "token_id": token_id,
                    "logits_key": key,
                    "logits_digest": _digest_bytes(arrays[key].tobytes(order="C")),
                }
            )
            generated.append(token_id)
        cases.append(
            {
                "case_id": case_id,
                "prompt_tokens_digest": _digest({"token_ids": list(prompt_ids)}),
                "prompt_token_count": len(prompt_ids),
                "generated_token_ids": generated,
                "steps": steps,
            }
        )
    return cases, arrays


def run_capture(
    *,
    bundle_root: Path,
    corpus: Mapping[str, Any],
    representation_digest: str,
    logits_output: Path,
) -> dict[str, Any]:
    normalized = validate_corpus(corpus)
    if _DIGEST.fullmatch(representation_digest) is None:
        raise QwenDistributedCaptureError("distributed_representation_invalid")
    deployment = Path(bundle_root).resolve(strict=True) / "deployment"
    if tokenizer_identity(deployment) != normalized["tokenizer_digest"]:
        raise QwenDistributedCaptureError("distributed_tokenizer_mismatch")
    try:
        from tokenizers import Tokenizer
    except ImportError as exc:
        raise QwenDistributedCaptureError("distributed_tokenizer_unavailable") from exc
    tokenizer = Tokenizer.from_file(str(deployment / "tokenizer.json"))
    stages = load_bundle_stages(bundle_root)
    cases, arrays = capture_loaded_stages(
        loaded_stages=stages,
        encode=lambda text: tokenizer.encode(text, add_special_tokens=False).ids,
        prompts=normalized["prompts"],
        maximum_new_tokens=normalized["maximum_new_tokens"],
    )
    _private_npz(logits_output, arrays)
    load_proofs = [
        {
            "assignment_id": stage.proof["assignment_id"],
            "node_id": stage.proof["node_id"],
            "loaded_range": _json_mutable(stage.proof["loaded_range"]),
            "loaded_tensor_digest": stage.proof["loaded_tensor_digest"],
            "probe_digest": stage.proof["probe_digest"],
            "runtime_identity": _json_mutable(stage.proof["runtime_identity"]),
        }
        for stage in stages
    ]
    runtime = {
        "implementation": IMPLEMENTATION_VERSION,
        "implementation_digest": _digest_bytes(Path(__file__).read_bytes()),
        "load_proofs": load_proofs,
        "assignment_count": len(load_proofs),
    }
    runtime_digest = _digest(runtime)
    report_without_digest = {
        "protocol": DISTRIBUTED_PROTOCOL,
        "claim_boundary": CLAIM_BOUNDARY,
        "model_id": normalized["model_id"],
        "revision": normalized["revision"],
        "source_artifact_digest": normalized["source_artifact_digest"],
        "representation_digest": representation_digest,
        "tokenizer_digest": normalized["tokenizer_digest"],
        "corpus_digest": _digest(normalized),
        "runtime": runtime,
        "runtime_digest": runtime_digest,
        "simulated": False,
        "backend_fallback": False,
        "reference_process_participates_in_route": False,
        "cases": cases,
        "logits_archive": {
            "file_name": logits_output.name,
            "content_digest": _digest_bytes(logits_output.read_bytes()),
            "array_count": len(arrays),
            "dtype": "float32",
        },
        "route_ready": False,
        "qualified": False,
    }
    return {
        **report_without_digest,
        "report_digest": _digest(report_without_digest),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-root", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--representation-digest", required=True)
    parser.add_argument("--logits-output", type=Path, required=True)
    parser.add_argument("--report-output", type=Path, required=True)
    args = parser.parse_args(argv)
    report = run_capture(
        bundle_root=args.bundle_root,
        corpus=_read(args.corpus),
        representation_digest=args.representation_digest,
        logits_output=args.logits_output,
    )
    _private_json(args.report_output, report)
    print(
        json.dumps(
            {
                "report_output": str(args.report_output),
                "logits_output": str(args.logits_output),
                "report_digest": report["report_digest"],
                "case_count": len(report["cases"]),
                "route_ready": False,
                "qualified": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CLAIM_BOUNDARY",
    "IMPLEMENTATION_VERSION",
    "QwenDistributedCaptureError",
    "capture_loaded_stages",
    "load_bundle_stages",
    "run_capture",
]
