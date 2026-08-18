from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from mycelium_reference_oracle.qwen_parity import (
    QwenParityError,
    compare,
    validate_policy,
)


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
MODEL_ID = "Qwen/Qwen2.5-7B-Instruct"
REVISION = "d" * 40


def _digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _digest(value: object) -> str:
    return _digest_bytes(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    )


def _policy() -> dict:
    return {
        "protocol": "mycelium.qwen_parity_policy.v1",
        "model_id": MODEL_ID,
        "revision": REVISION,
        "source_artifact_digest": DIGEST_A,
        "representation_digest": DIGEST_B,
        "corpus_digest": DIGEST_C,
        "absolute_tolerance": 0.1,
        "relative_tolerance": 0.01,
        "frozen_at_unix_ms": 1_000,
    }


def _archive(tmp_path: Path, name: str, values: list[float]) -> tuple[Path, dict]:
    path = tmp_path / name
    np.savez_compressed(path, case_000_step_000=np.array(values, dtype=np.float32))
    return path, {
        "file_name": name,
        "content_digest": _digest_bytes(path.read_bytes()),
        "array_count": 1,
        "dtype": "float32",
    }


def _documents(tmp_path: Path, distributed_values: list[float]):
    reference_path, reference_archive = _archive(tmp_path, "reference.npz", [1.0, 2.0, 3.0])
    distributed_path, distributed_archive = _archive(tmp_path, "distributed.npz", distributed_values)
    logits_digest = _digest_bytes(np.array([1.0, 2.0, 3.0], dtype=np.float32).tobytes())
    distributed_logits_digest = _digest_bytes(np.array(distributed_values, dtype=np.float32).tobytes())
    step = {"index": 0, "token_id": 2, "logits_key": "case_000_step_000", "logits_digest": logits_digest}
    reference = {
        "protocol": "mycelium.qwen_transformers_reference.v1",
        "model_id": MODEL_ID,
        "revision": REVISION,
        "source_artifact_digest": DIGEST_A,
        "tokenizer_digest": DIGEST_C,
        "reference_process_participates_in_route": False,
        "runtime": {"runtime_digest": DIGEST_A},
        "corpus_digest": DIGEST_C,
        "cases": [{"case_id": "case", "prompt_tokens_digest": DIGEST_A, "steps": [step]}],
        "logits_archive": reference_archive,
    }
    distributed = {
        "protocol": "mycelium.qwen_distributed_capture.v1",
        "model_id": MODEL_ID,
        "revision": REVISION,
        "source_artifact_digest": DIGEST_A,
        "representation_digest": DIGEST_B,
        "tokenizer_digest": DIGEST_C,
        "runtime_digest": DIGEST_B,
        "corpus_digest": DIGEST_C,
        "simulated": False,
        "backend_fallback": False,
        "cases": [{"case_id": "case", "prompt_tokens_digest": DIGEST_A, "steps": [{**step, "logits_digest": distributed_logits_digest}]}],
        "logits_archive": distributed_archive,
    }
    return reference, reference_path, distributed, distributed_path


def test_comparator_accepts_exact_tokens_and_numeric_tolerance(tmp_path: Path) -> None:
    reference, reference_path, distributed, distributed_path = _documents(
        tmp_path, [1.01, 2.01, 3.01]
    )
    result = compare(
        policy=_policy(),
        reference=reference,
        reference_logits=reference_path,
        distributed=distributed,
        distributed_logits=distributed_path,
    )

    assert result["passed"] is True
    assert result["cases"][0]["greedy_tokens_match"] is True
    assert result["cases"][0]["within_tolerance"] is True
    assert result["policy_digest"] == _digest(_policy())


def test_comparator_reports_token_or_numeric_failure_without_promoting(tmp_path: Path) -> None:
    reference, reference_path, distributed, distributed_path = _documents(
        tmp_path, [1.0, 2.0, 9.0]
    )
    distributed["cases"][0]["steps"][0]["token_id"] = 1

    result = compare(
        policy=_policy(),
        reference=reference,
        reference_logits=reference_path,
        distributed=distributed,
        distributed_logits=distributed_path,
    )

    assert result["passed"] is False
    assert result["cases"][0]["greedy_tokens_match"] is False
    assert result["cases"][0]["within_tolerance"] is False


def test_comparator_rejects_identity_archive_or_fallback_drift(tmp_path: Path) -> None:
    reference, reference_path, distributed, distributed_path = _documents(
        tmp_path, [1.0, 2.0, 3.0]
    )
    for mutation in (
        {**distributed, "backend_fallback": True},
        {**distributed, "representation_digest": DIGEST_C},
    ):
        with pytest.raises(QwenParityError, match="binding_mismatch"):
            compare(
                policy=_policy(),
                reference=reference,
                reference_logits=reference_path,
                distributed=mutation,
                distributed_logits=distributed_path,
            )

    corrupted = copy.deepcopy(distributed)
    corrupted["logits_archive"]["content_digest"] = DIGEST_A
    with pytest.raises(QwenParityError, match="archive_invalid"):
        compare(
            policy=_policy(),
            reference=reference,
            reference_logits=reference_path,
            distributed=corrupted,
            distributed_logits=distributed_path,
        )


def test_policy_is_closed_and_predeclared() -> None:
    assert validate_policy(_policy()) == _policy()
    with pytest.raises(QwenParityError, match="policy_invalid"):
        validate_policy({**_policy(), "absolute_tolerance": float("nan")})
