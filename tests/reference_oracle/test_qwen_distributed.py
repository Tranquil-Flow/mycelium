from __future__ import annotations

from types import MappingProxyType
from types import SimpleNamespace

import numpy as np

from mycelium_live import qwen_capture


class _Backend:
    def __init__(self, final: bool) -> None:
        self._final = final

    def execute_loaded_stage(
        self,
        _stage,
        *,
        token_ids=None,
        hidden_states=None,
    ):
        source = token_ids if token_ids is not None else hidden_states
        values = np.asarray(source)
        if not self._final:
            return np.stack((values, values), axis=-1).astype(np.float32)
        sequence = values.shape[1]
        logits = np.zeros((1, sequence, 4), dtype=np.float32)
        logits[..., 2] = 10.0
        return logits


def test_split_stage_capture_replays_each_step_and_binds_logits(monkeypatch) -> None:
    stages = (
        SimpleNamespace(
            proof={"runtime": MappingProxyType({"backend": "mlx", "stage": 0})}
        ),
        SimpleNamespace(
            proof={"runtime": MappingProxyType({"backend": "mlx", "stage": 1})}
        ),
    )

    def backend(*, runtime, prefer):
        assert prefer == "auto"
        return _Backend(final=runtime["stage"] == 1)

    monkeypatch.setattr(qwen_capture, "select_stage_backend", backend)

    cases, arrays = qwen_capture.capture_loaded_stages(
        loaded_stages=stages,
        encode=lambda _text: (5, 6),
        prompts=({"case_id": "capital", "text": "prompt"},),
        maximum_new_tokens=2,
    )

    assert cases[0]["generated_token_ids"] == [2, 2]
    assert cases[0]["prompt_token_count"] == 2
    assert [step["index"] for step in cases[0]["steps"]] == [0, 1]
    assert set(arrays) == {"case_000_step_000", "case_000_step_001"}
    assert all(array.dtype == np.float32 for array in arrays.values())


def test_rebase_artifacts_derives_identity_for_local_materialization(
    tmp_path,
) -> None:
    deployment = tmp_path / "deployment"
    deployment.mkdir()
    (deployment / "weights.safetensors").write_bytes(b"weights")
    assignment = {
        "protocol": "mycelium.layer_assignment.v2",
        "deployment_id": "11111111-1111-4111-8111-111111111111",
        "deployment_epoch": 1,
        "assignment_id": "source-assignment",
        "node_id": "node-0",
        "manifest_digest": "sha256:" + "1" * 64,
        "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
        "resolved_commit": "revision",
        "range": {"start_layer": 0, "end_layer_exclusive": 1, "layer_count": 1},
        "components": ["input_embedding", "transformer_layers"],
        "component_tensor_keys": {},
        "component_aliases": {},
        "expected_tensor_prefixes": ["model.layers.0."],
        "expected_tensor_keys": ["model.layers.0.input_layernorm.weight"],
        "files": [
            {
                "path": "weights.safetensors",
                "size_bytes": 7,
                "content_digest": "sha256:" + "2" * 64,
            }
        ],
        "artifact_cache_root": "/remote/cache",
        "runtime": {"backend": "mlx", "dtype": "bfloat16", "quantization": "none"},
        "route_ready": False,
    }
    report = {
        "assignment_id": "source-assignment",
        "artifact_cache_root": "/remote/cache",
        "resolved_artifact_cache_root": "/remote/cache",
        "verified_files": [
            {
                "path": "weights.safetensors",
                "local_path": "/remote/cache/weights.safetensors",
            }
        ],
    }

    rebased_assignment, rebased_report = qwen_capture._rebase_artifacts(
        assignment,
        report,
        deployment_root=deployment,
    )

    assert rebased_assignment["assignment_id"] != assignment["assignment_id"]
    assert rebased_report["assignment_id"] == rebased_assignment["assignment_id"]
    assert rebased_assignment["artifact_cache_root"] == str(deployment.resolve())
    assert rebased_report["verified_files"][0]["local_path"] == str(
        (deployment / "weights.safetensors").resolve()
    )
