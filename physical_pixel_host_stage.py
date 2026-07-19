#!/usr/bin/env python3.14
"""Long-lived M4 entry/final stage worker for Pixel physical qualification."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import sys
from typing import Any, NoReturn, Sequence

import mlx.core as mx

from mycelium_router.mlx_runtime import _layer_norm
from runtime_loader import canonical_json, execute_loaded_stage, load_assignment_stage

PROTOCOL = "mycelium.pixel_host_stage_control.v1"
MAX_COMMAND_BYTES = 1024 * 1024


class HostStageError(ValueError):
    pass


def _reject(code: str) -> NoReturn:
    raise HostStageError(code)


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError):
        _reject("noncanonical_document")


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        _reject("artifact_path_invalid")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        _reject("artifact_json_invalid")
    if not isinstance(value, dict):
        _reject("artifact_json_invalid")
    return value


def _rows(value: Any, hidden_size: int) -> list[list[float]]:
    if not isinstance(value, list) or not value or len(value) > 256:
        _reject("hidden_shape_invalid")
    rows: list[list[float]] = []
    for row in value:
        if not isinstance(row, list) or len(row) != hidden_size:
            _reject("hidden_shape_invalid")
        normalized: list[float] = []
        for item in row:
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                _reject("hidden_value_invalid")
            number = float(item)
            if not math.isfinite(number):
                _reject("hidden_value_invalid")
            normalized.append(number)
        rows.append(normalized)
    return rows


def _final_logits(loaded: Any, hidden: list[list[float]]) -> mx.array:
    proof = loaded.proof
    runtime = proof["runtime"]
    config = runtime["model_config"]
    namespace = runtime.get("tensor_namespace", "transformer.")
    array = mx.array([hidden], dtype=mx.float32)
    normalized = _layer_norm(
        array,
        loaded.tensors[f"{namespace}ln_f.weight"],
        loaded.tensors[f"{namespace}ln_f.bias"],
        float(config["layer_norm_epsilon"]),
    )
    alias = loaded.resolved_aliases.get("lm_head", {})
    keys = alias.get("tensor_keys", ["lm_head.weight"])
    if not isinstance(keys, (list, tuple)) or len(keys) != 1:
        _reject("lm_head_alias_invalid")
    logits = mx.matmul(normalized, loaded.tensors[keys[0]].transpose(1, 0))
    mx.eval(logits)
    return logits


def run(
    *,
    role: str,
    assignment_file: Path,
    report_file: Path,
    load_generation: int,
) -> int:
    assignment = _read_json(assignment_file)
    report = _read_json(report_file)
    loaded = load_assignment_stage(assignment, report, load_generation=load_generation)
    components = set(loaded.proof["loaded_components"])
    if role == "entry" and "input_embedding" not in components:
        _reject("entry_assignment_invalid")
    if role == "final" and not {"final_norm", "lm_head"}.issubset(components):
        _reject("final_assignment_invalid")
    config = loaded.proof["runtime"]["model_config"]
    hidden_size = int(config["n_embd"])
    max_sequence_length = int(config["n_positions"])
    vocab_size = int(config["vocab_size"])
    identity = {
        "role": role,
        "pid": os.getpid(),
        "process_host_id": platform.node(),
        "assignment_id": loaded.proof["assignment_id"],
        "load_proof_digest": _digest(json.loads(canonical_json(loaded.proof))),
        "route_ready": False,
    }
    while True:
        raw = sys.stdin.buffer.readline(MAX_COMMAND_BYTES + 1)
        if not raw:
            break
        if len(raw) > MAX_COMMAND_BYTES:
            while raw and not raw.endswith(b"\n"):
                raw = sys.stdin.buffer.readline(MAX_COMMAND_BYTES + 1)
            response = {"ok": False, "error": "command_too_large", **identity}
        else:
            try:
                command = json.loads(raw.decode("utf-8"))
                if (
                    not isinstance(command, dict)
                    or set(command)
                    != {"protocol", "command_id", "operation", "payload"}
                    or command.get("protocol") != PROTOCOL
                    or not isinstance(command.get("command_id"), str)
                    or not isinstance(command.get("payload"), dict)
                ):
                    _reject("command_invalid")
                operation = command["operation"]
                if operation == "stop":
                    response = {
                        "ok": True,
                        "command_id": command["command_id"],
                        "result": {"stopped": True},
                        **identity,
                    }
                    sys.stdout.write(json.dumps(response, sort_keys=True) + "\n")
                    sys.stdout.flush()
                    return 0
                if role == "entry" and operation == "entry":
                    payload = command["payload"]
                    if set(payload) != {"token_ids"}:
                        _reject("entry_payload_invalid")
                    token_ids = payload["token_ids"]
                    if (
                        not isinstance(token_ids, list)
                        or not 1 <= len(token_ids) <= max_sequence_length
                        or any(
                            isinstance(token, bool)
                            or not isinstance(token, int)
                            or not 0 <= token < vocab_size
                            for token in token_ids
                        )
                    ):
                        _reject("entry_tokens_invalid")
                    output = execute_loaded_stage(
                        loaded,
                        token_ids=mx.array((tuple(token_ids),), dtype=mx.uint32),
                    )
                    mx.eval(output)
                    result_value = output.tolist()[0]
                elif role == "final" and operation == "final":
                    payload = command["payload"]
                    if set(payload) != {"hidden"}:
                        _reject("final_payload_invalid")
                    output = _final_logits(
                        loaded, _rows(payload["hidden"], hidden_size)
                    )
                    result_value = output.tolist()[0]
                else:
                    _reject("operation_invalid")
                response = {
                    "ok": True,
                    "command_id": command["command_id"],
                    "result": {
                        "output": result_value,
                        "output_digest": _digest(result_value),
                    },
                    **identity,
                }
            except HostStageError as exc:
                response = {"ok": False, "error": str(exc), **identity}
            except Exception:
                response = {"ok": False, "error": "host_stage_failed", **identity}
        sys.stdout.write(
            json.dumps(response, sort_keys=True, separators=(",", ":")) + "\n"
        )
        sys.stdout.flush()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", choices=("entry", "final"), required=True)
    parser.add_argument("--assignment-file", type=Path, required=True)
    parser.add_argument("--report-file", type=Path, required=True)
    parser.add_argument("--load-generation", type=int, required=True)
    args = parser.parse_args(argv)
    return run(
        role=args.role,
        assignment_file=args.assignment_file,
        report_file=args.report_file,
        load_generation=args.load_generation,
    )


if __name__ == "__main__":
    raise SystemExit(main())
