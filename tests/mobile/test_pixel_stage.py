from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
import threading
import urllib.error
import urllib.request

import mlx.core as mx
import pytest

from mycelium_mobile.pixel_stage import (
    PixelStage,
    PixelStageError,
    STAGE_REQUEST_PROTOCOL,
    TOKEN_HEADER,
    _StageServer,
    build_stage_pack,
)
from mycelium_router.mlx_runtime import _gpt2_block_with_kv
from two_process_runtime_qualification import _layer_tensors


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("ascii")


def _pack() -> dict[str, object]:
    tensors = _layer_tensors(1)
    return build_stage_pack(
        run_id="run-pixel",
        deployment_id="deployment-pixel",
        assignment_id="assignment-pixel",
        stage_id="stage-001",
        model_id="fixture/gpt2-tiny",
        resolved_commit="a" * 40,
        manifest_digest="sha256:" + "b" * 64,
        parent_assignment_digest="sha256:" + "c" * 64,
        parent_load_proof_digest="sha256:" + "d" * 64,
        start_layer=1,
        end_layer_exclusive=2,
        n_head=2,
        hidden_size=4,
        epsilon=1e-5,
        activation_function="gelu_new",
        scale_attn_weights=True,
        scale_attn_by_inverse_layer_idx=False,
        reorder_and_upcast_attn=False,
        add_cross_attention=False,
        tensors={key: value.tolist() for key, value in tensors.items()},
    )


def test_pure_python_pixel_stage_matches_mlx_gpt2_block() -> None:
    tensors = _layer_tensors(1)
    hidden = mx.array(
        [
            [
                [0.1, -0.2, 0.3, -0.4],
                [0.05, 0.06, -0.07, 0.08],
                [-0.3, 0.2, 0.1, -0.05],
            ]
        ],
        dtype=mx.float32,
    )
    expected, _ = _gpt2_block_with_kv(
        hidden,
        tensors,
        "transformer.h.1.",
        2,
        1e-5,
        None,
    )
    mx.eval(expected)

    stage = PixelStage.from_document(_pack())
    actual = stage.execute(
        request_id="request-1",
        assignment_id="assignment-pixel",
        stage_id="stage-001",
        hidden=hidden.tolist()[0],
    )

    expected_rows = expected.tolist()[0]
    assert len(actual) == len(expected_rows)
    assert (
        max(
            abs(float(left) - float(right))
            for actual_row, expected_row in zip(actual, expected_rows)
            for left, right in zip(actual_row, expected_row)
        )
        < 1e-6
    )


def test_stage_pack_digest_and_exact_tensor_set_fail_closed() -> None:
    pack = _pack()
    PixelStage.from_document(pack)

    changed = copy.deepcopy(pack)
    changed["tensors"]["transformer.h.1.ln_1.bias"][0] = 99.0
    with pytest.raises(PixelStageError, match="stage_pack_digest_mismatch"):
        PixelStage.from_document(changed)

    extra = copy.deepcopy(pack)
    unsigned = {key: value for key, value in extra.items() if key != "pack_digest"}
    unsigned["tensors"]["unexpected"] = [1.0]
    extra["tensors"]["unexpected"] = [1.0]
    extra["pack_digest"] = "sha256:" + hashlib.sha256(_canonical(unsigned)).hexdigest()
    with pytest.raises(PixelStageError, match="stage_pack_tensor_set_invalid"):
        PixelStage.from_document(extra)

    unsupported = copy.deepcopy(pack)
    unsupported["activation_function"] = "relu"
    unsigned = {
        key: value for key, value in unsupported.items() if key != "pack_digest"
    }
    unsupported["pack_digest"] = (
        "sha256:" + hashlib.sha256(_canonical(unsigned)).hexdigest()
    )
    with pytest.raises(
        PixelStageError, match="stage_pack_activation_function_unsupported"
    ):
        PixelStage.from_document(unsupported)


def test_request_identity_replay_and_nonfinite_input_fail_closed() -> None:
    stage = PixelStage.from_document(_pack())
    hidden = [[0.1, 0.2, 0.3, 0.4]]
    first = stage.execute(
        request_id="request-1",
        assignment_id="assignment-pixel",
        stage_id="stage-001",
        hidden=hidden,
    )
    assert (
        stage.execute(
            request_id="request-1",
            assignment_id="assignment-pixel",
            stage_id="stage-001",
            hidden=hidden,
        )
        == first
    )
    assert stage.request_count == 1

    with pytest.raises(PixelStageError, match="request_replay_conflict"):
        stage.execute(
            request_id="request-1",
            assignment_id="assignment-pixel",
            stage_id="stage-001",
            hidden=[[0.4, 0.3, 0.2, 0.1]],
        )
    with pytest.raises(PixelStageError, match="request_assignment_mismatch"):
        stage.execute(
            request_id="request-2",
            assignment_id="wrong-assignment",
            stage_id="stage-001",
            hidden=hidden,
        )
    with pytest.raises(PixelStageError, match="request_hidden_nonfinite"):
        stage.execute(
            request_id="request-3",
            assignment_id="assignment-pixel",
            stage_id="stage-001",
            hidden=[[math.inf, 0.0, 0.0, 0.0]],
        )


def test_http_worker_reads_exact_body_and_enforces_authentication(
    tmp_path: Path,
) -> None:
    stage = PixelStage.from_document(_pack())
    token = b"t" * 48
    server = _StageServer(
        ("127.0.0.1", 0),
        stage=stage,
        token=token,
        evidence_file=tmp_path / "evidence.json",
        boot_id="test-boot-id",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    hidden = [[0.1, 0.2, 0.3, 0.4]]
    payload = {
        "protocol": STAGE_REQUEST_PROTOCOL,
        "request_id": "request-http",
        "assignment_id": "assignment-pixel",
        "stage_id": "stage-001",
        "hidden": hidden,
        "input_digest": "sha256:" + hashlib.sha256(_canonical(hidden)).hexdigest(),
    }
    raw = _canonical(payload)
    try:
        unauthorized = urllib.request.Request(
            base_url + "/execute",
            data=raw,
            headers={"content-type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as rejected:
            urllib.request.urlopen(unauthorized, timeout=2)
        assert rejected.value.code == 401
        rejected.value.close()

        authorized = urllib.request.Request(
            base_url + "/execute",
            data=raw,
            headers={
                "content-type": "application/json",
                TOKEN_HEADER: token.decode("ascii"),
            },
            method="POST",
        )
        with urllib.request.urlopen(authorized, timeout=2) as response:
            document = json.loads(response.read().decode("utf-8"))
        assert response.status == 200
        assert document["request_id"] == "request-http"
        assert document["request_count"] == 1
        assert document["route_ready"] is False
        assert (tmp_path / "evidence.json").is_file()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
