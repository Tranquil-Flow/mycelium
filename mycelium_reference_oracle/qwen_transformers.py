"""Offline PyTorch/Transformers reference execution for exact local Qwen revisions.

This module deliberately does not import Mycelium runtime, planner, router, assignment,
or qualification code. It is a correctness oracle, never a serving stage.
"""

from __future__ import annotations

import argparse
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping, Sequence

import numpy as np


CORPUS_PROTOCOL = "mycelium.qwen_reference_corpus.v1"
REPORT_PROTOCOL = "mycelium.qwen_transformers_reference.v1"
IMPLEMENTATION_VERSION = "qwen-transformers-offline-reference-v1"
CLAIM_BOUNDARY = (
    "offline independent local reference only; no route participation, transport, "
    "distributed execution, model preparation, qualification, or selection claim"
)
_REVISION = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_CASE_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_TOKENIZER_FILES = (
    "merges.txt",
    "tokenizer.json",
    "tokenizer_config.json",
    "vocab.json",
)


class QwenReferenceError(RuntimeError):
    """A bounded independent-reference input or execution failure."""


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


def _package_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "unknown"


def _source_digest() -> str:
    return _digest_bytes(Path(__file__).read_bytes())


def _regular_local_file(root: Path, name: str) -> Path:
    path = root / name
    if not path.exists() or not path.is_file():
        raise QwenReferenceError(f"required_local_file_missing:{name}")
    return path


def tokenizer_identity(snapshot: Path) -> str:
    """Hash the exact local tokenizer inputs with explicit filename framing."""

    digest = hashlib.sha256()
    for name in _TOKENIZER_FILES:
        path = _regular_local_file(snapshot, name)
        raw = path.read_bytes()
        encoded = name.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
    return "sha256:" + digest.hexdigest()


def _blob_identity(path: Path) -> str | None:
    """Return a content-addressed Hugging Face blob identity without reading weights."""

    try:
        resolved_name = path.resolve(strict=True).name
    except OSError:
        return None
    if re.fullmatch(r"[0-9a-f]{64}", resolved_name) is None:
        return None
    return f"sha256:{resolved_name}"


def snapshot_artifact_identity(snapshot: Path, *, model_id: str) -> str:
    """Independently reproduce the immutable source-checkpoint catalogue identity."""

    if not isinstance(model_id, str) or not model_id.startswith("Qwen/"):
        raise QwenReferenceError("reference_model_identity_invalid")
    resolved = snapshot.resolve(strict=True)
    config_path = _regular_local_file(resolved, "config.json")
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QwenReferenceError("reference_config_invalid") from exc
    if not isinstance(config, dict):
        raise QwenReferenceError("reference_config_invalid")

    index_path = resolved / "model.safetensors.index.json"
    single_path = resolved / "model.safetensors"
    if index_path.is_file():
        checkpoint_format = "safetensors_sharded"
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise QwenReferenceError("reference_checkpoint_index_invalid") from exc
        weight_map = index.get("weight_map") if isinstance(index, dict) else None
        if not isinstance(weight_map, dict) or not weight_map or not all(
            isinstance(name, str) and isinstance(file_name, str)
            for name, file_name in weight_map.items()
        ):
            raise QwenReferenceError("reference_checkpoint_index_invalid")
        weight_files = sorted(set(weight_map.values()))
    elif single_path.is_file():
        checkpoint_format = "safetensors_single"
        weight_files = [single_path.name]
    else:
        raise QwenReferenceError("reference_weight_artifact_missing")

    files = []
    for name in sorted({"config.json", *weight_files}):
        path = _regular_local_file(resolved, name)
        record: dict[str, object] = {"name": name, "size_bytes": path.stat().st_size}
        content_digest = _blob_identity(path)
        if content_digest is not None:
            record["content_digest"] = content_digest
        files.append(record)

    quantization = "unknown"
    quantization_config = config.get("quantization_config")
    if isinstance(quantization_config, dict):
        method = quantization_config.get("quant_method") or quantization_config.get(
            "method"
        )
        bits = quantization_config.get("bits")
        if isinstance(method, str) and method:
            quantization = (
                f"{method}-{bits}bit" if isinstance(bits, int) else method
            )
    if quantization == "unknown":
        dtype = config.get("torch_dtype") or config.get("dtype")
        if isinstance(dtype, str) and dtype:
            quantization = dtype

    descriptor = {
        "model_id": model_id,
        "revision": resolved.name,
        "checkpoint_format": checkpoint_format,
        "model_type": config.get("model_type")
        if isinstance(config.get("model_type"), str)
        else "unknown",
        "quantization": quantization,
        "files": files,
    }
    return _digest(descriptor)


def validate_corpus(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "protocol",
        "model_id",
        "revision",
        "source_artifact_digest",
        "tokenizer_digest",
        "maximum_new_tokens",
        "prompts",
    }:
        raise QwenReferenceError("reference_corpus_invalid")
    model_id = value.get("model_id")
    revision = value.get("revision")
    maximum_new_tokens = value.get("maximum_new_tokens")
    prompts = value.get("prompts")
    if (
        value.get("protocol") != CORPUS_PROTOCOL
        or not isinstance(model_id, str)
        or not model_id.startswith("Qwen/")
        or len(model_id) > 256
        or not isinstance(revision, str)
        or _REVISION.fullmatch(revision) is None
        or not isinstance(value.get("source_artifact_digest"), str)
        or _DIGEST.fullmatch(value["source_artifact_digest"]) is None
        or not isinstance(value.get("tokenizer_digest"), str)
        or _DIGEST.fullmatch(value["tokenizer_digest"]) is None
        or type(maximum_new_tokens) is not int
        or not 1 <= maximum_new_tokens <= 32
        or not isinstance(prompts, list)
        or not 1 <= len(prompts) <= 16
    ):
        raise QwenReferenceError("reference_corpus_invalid")
    normalized = []
    seen: set[str] = set()
    for prompt in prompts:
        if not isinstance(prompt, Mapping) or set(prompt) != {"case_id", "text"}:
            raise QwenReferenceError("reference_corpus_invalid")
        case_id = prompt.get("case_id")
        text = prompt.get("text")
        if (
            not isinstance(case_id, str)
            or _CASE_ID.fullmatch(case_id) is None
            or case_id in seen
            or not isinstance(text, str)
            or not text
            or len(text.encode("utf-8")) > 16_384
        ):
            raise QwenReferenceError("reference_corpus_invalid")
        seen.add(case_id)
        normalized.append({"case_id": case_id, "text": text})
    return {
        "protocol": CORPUS_PROTOCOL,
        "model_id": model_id,
        "revision": revision,
        "source_artifact_digest": value["source_artifact_digest"],
        "tokenizer_digest": value["tokenizer_digest"],
        "maximum_new_tokens": maximum_new_tokens,
        "prompts": normalized,
    }


def _private_json(path: Path, value: Mapping[str, Any]) -> None:
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


def _private_npz(path: Path, arrays: Mapping[str, np.ndarray]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _runtime_identity(*, dtype: str, device: str) -> dict[str, Any]:
    value = {
        "implementation": IMPLEMENTATION_VERSION,
        "source_digest": _source_digest(),
        "torch_version": _package_version("torch"),
        "transformers_version": _package_version("transformers"),
        "numpy_version": _package_version("numpy"),
        "dtype": dtype,
        "device": device,
        "offline_only": True,
    }
    return {**value, "runtime_digest": _digest(value)}


def run_reference(
    *,
    snapshot: Path,
    corpus: Mapping[str, Any],
    dtype: str,
    logits_output: Path,
) -> dict[str, Any]:
    """Execute exact greedy full-context reference decoding and seal logits."""

    normalized = validate_corpus(corpus)
    resolved = snapshot.resolve(strict=True)
    if resolved.name != normalized["revision"]:
        raise QwenReferenceError("reference_revision_mismatch")
    if (
        snapshot_artifact_identity(resolved, model_id=normalized["model_id"])
        != normalized["source_artifact_digest"]
    ):
        raise QwenReferenceError("reference_source_artifact_mismatch")
    if tokenizer_identity(resolved) != normalized["tokenizer_digest"]:
        raise QwenReferenceError("reference_tokenizer_mismatch")
    if dtype not in {"float32", "bfloat16"}:
        raise QwenReferenceError("reference_dtype_invalid")

    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    try:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except ImportError as exc:
        raise QwenReferenceError("reference_runtime_unavailable") from exc

    torch_dtype = torch.float32 if dtype == "float32" else torch.bfloat16
    tokenizer = AutoTokenizer.from_pretrained(
        resolved,
        local_files_only=True,
        trust_remote_code=False,
    )
    model = AutoModelForCausalLM.from_pretrained(
        resolved,
        local_files_only=True,
        trust_remote_code=False,
        dtype=torch_dtype,
        device_map=None,
    )
    model.to("cpu")
    model.eval()

    arrays: dict[str, np.ndarray] = {}
    cases: list[dict[str, Any]] = []
    with torch.inference_mode():
        for case_index, prompt in enumerate(normalized["prompts"]):
            # Corpus text is the exact frozen model input (including any chat
            # envelope), so neither reference nor serving capture may add a
            # second implicit wrapper.
            encoded = tokenizer(
                prompt["text"],
                return_tensors="pt",
                add_special_tokens=False,
            )
            token_ids = encoded["input_ids"].to("cpu")
            prompt_ids = [int(item) for item in token_ids[0].tolist()]
            steps = []
            generated = []
            for step_index in range(normalized["maximum_new_tokens"]):
                output = model(input_ids=token_ids, use_cache=False, return_dict=True)
                logits = output.logits[0, -1, :].float().cpu().numpy().astype(np.float32, copy=False)
                if not bool(np.isfinite(logits).all()):
                    raise QwenReferenceError("reference_non_finite_logits")
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
                next_token = torch.tensor([[token_id]], dtype=token_ids.dtype, device="cpu")
                token_ids = torch.cat((token_ids, next_token), dim=1)
            cases.append(
                {
                    "case_id": prompt["case_id"],
                    "prompt_tokens_digest": _digest({"token_ids": prompt_ids}),
                    "prompt_token_count": len(prompt_ids),
                    "generated_token_ids": generated,
                    "decoded_output_digest": _digest_bytes(
                        tokenizer.decode(generated, skip_special_tokens=False).encode("utf-8")
                    ),
                    "steps": steps,
                }
            )

    _private_npz(logits_output, arrays)
    runtime = _runtime_identity(dtype=dtype, device="cpu")
    report_without_digest = {
        "protocol": REPORT_PROTOCOL,
        "claim_boundary": CLAIM_BOUNDARY,
        "model_id": normalized["model_id"],
        "revision": normalized["revision"],
        "source_artifact_digest": normalized["source_artifact_digest"],
        "tokenizer_digest": normalized["tokenizer_digest"],
        "reference_process_participates_in_route": False,
        "runtime": runtime,
        "corpus_digest": _digest(normalized),
        "maximum_new_tokens": normalized["maximum_new_tokens"],
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
    return {**report_without_digest, "report_digest": _digest(report_without_digest)}


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise QwenReferenceError("reference_corpus_invalid")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run an offline independent Qwen reference against an exact local revision."
    )
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--dtype", choices=("float32", "bfloat16"), default="bfloat16")
    parser.add_argument("--logits-output", type=Path, required=True)
    parser.add_argument("--report-output", type=Path, required=True)
    args = parser.parse_args(argv)
    report = run_reference(
        snapshot=args.snapshot,
        corpus=_read_object(args.corpus),
        dtype=args.dtype,
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
    "CORPUS_PROTOCOL",
    "IMPLEMENTATION_VERSION",
    "QwenReferenceError",
    "REPORT_PROTOCOL",
    "run_reference",
    "snapshot_artifact_identity",
    "tokenizer_identity",
    "validate_corpus",
]
