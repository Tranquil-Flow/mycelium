from __future__ import annotations

import copy
import json
from pathlib import Path
import stat
import subprocess
import sys

import numpy as np
import pytest

from mycelium_reference_oracle.qwen_transformers import (
    CORPUS_PROTOCOL,
    QwenReferenceError,
    _private_json,
    _private_npz,
    snapshot_artifact_identity,
    tokenizer_identity,
    validate_corpus,
)


DIGEST = "sha256:" + "a" * 64


def _corpus() -> dict:
    return {
        "protocol": CORPUS_PROTOCOL,
        "model_id": "Qwen/Qwen2.5-7B-Instruct",
        "revision": "b" * 40,
        "source_artifact_digest": DIGEST,
        "tokenizer_digest": DIGEST,
        "maximum_new_tokens": 4,
        "prompts": [
            {"case_id": "capital", "text": "What is the capital of France?"},
            {"case_id": "arithmetic", "text": "Return only the result of 17 + 25."},
        ],
    }


def test_corpus_is_closed_bounded_and_exactly_bound() -> None:
    value = _corpus()
    assert validate_corpus(value) == value

    for mutation in (
        {**value, "download": True},
        {**value, "revision": "main"},
        {**value, "maximum_new_tokens": 33},
    ):
        with pytest.raises(QwenReferenceError, match="reference_corpus_invalid"):
            validate_corpus(mutation)

    duplicate = copy.deepcopy(value)
    duplicate["prompts"][1]["case_id"] = duplicate["prompts"][0]["case_id"]
    with pytest.raises(QwenReferenceError, match="reference_corpus_invalid"):
        validate_corpus(duplicate)


def test_tokenizer_identity_binds_filename_and_bytes(tmp_path: Path) -> None:
    for index, name in enumerate(
        ("merges.txt", "tokenizer.json", "tokenizer_config.json", "vocab.json")
    ):
        (tmp_path / name).write_bytes(f"value-{index}".encode())
    first = tokenizer_identity(tmp_path)
    (tmp_path / "vocab.json").write_bytes(b"changed")
    second = tokenizer_identity(tmp_path)

    assert first.startswith("sha256:")
    assert first != second


def test_snapshot_identity_binds_exact_content_addressed_source(tmp_path: Path) -> None:
    snapshot = tmp_path / ("b" * 40)
    blobs = tmp_path / "blobs"
    snapshot.mkdir()
    blobs.mkdir()
    config = {
        "model_type": "qwen2",
        "torch_dtype": "bfloat16",
    }
    config_blob = blobs / ("1" * 64)
    weight_blob = blobs / ("2" * 64)
    config_blob.write_text(json.dumps(config), encoding="utf-8")
    weight_blob.write_bytes(b"weights")
    (snapshot / "config.json").symlink_to(config_blob)
    (snapshot / "model.safetensors").symlink_to(weight_blob)

    first = snapshot_artifact_identity(
        snapshot, model_id="Qwen/Qwen2.5-7B-Instruct"
    )
    replacement = blobs / ("3" * 64)
    replacement.write_bytes(b"weights")
    (snapshot / "model.safetensors").unlink()
    (snapshot / "model.safetensors").symlink_to(replacement)
    second = snapshot_artifact_identity(
        snapshot, model_id="Qwen/Qwen2.5-7B-Instruct"
    )

    assert first.startswith("sha256:")
    assert first != second


def test_private_outputs_are_atomic_owner_only(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    logits = tmp_path / "logits.npz"
    _private_json(report, {"ok": True})
    _private_npz(logits, {"case_000_step_000": np.array([1.0, 2.0], dtype=np.float32)})

    assert json.loads(report.read_text(encoding="utf-8")) == {"ok": True}
    with np.load(logits) as archive:
        assert archive["case_000_step_000"].tolist() == [1.0, 2.0]
    assert stat.S_IMODE(report.stat().st_mode) == 0o600
    assert stat.S_IMODE(logits.stat().st_mode) == 0o600


def test_qwen_reference_entrypoint_loads_without_product_runtime(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "mycelium_reference_oracle.qwen_transformers",
            "--help",
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "offline independent Qwen reference" in completed.stdout
