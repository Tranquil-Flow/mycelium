from __future__ import annotations

import copy
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
import struct
import threading
import urllib.error
import urllib.request

import mlx.core as mx
import pytest

import mycelium_mobile.pixel_runtime as pixel_runtime
from mycelium_mobile.pixel_stage import (
    PixelStage,
    PixelStageError,
    STAGE_REQUEST_PROTOCOL,
    TOKEN_HEADER,
    _StageServer,
    build_stage_pack,
)
from mycelium_mobile.pixel_runtime import PixelRuntimeError, PixelStageRuntimePort
from mycelium_router.contracts import (
    ExecutionGraph,
    HopWorkItem,
    LayerRange,
    Placement,
    PlacementEdge,
    RuntimeBatch,
    RuntimeBatchKey,
    Stage,
    StageCost,
)
from mycelium_router.mlx_runtime import _gpt2_block_with_kv
from mycelium_router.payloads import decode_activation, encode_activation
from mycelium_router.stage_signatures import stage_signature_for_backend
from two_process_runtime_qualification import _layer_tensors

PARENT_ASSIGNMENT_DIGEST = "sha256:" + "c" * 64


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
        parent_assignment_digest=PARENT_ASSIGNMENT_DIGEST,
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

    stage = PixelStage.from_document(_pack())
    with pytest.raises(TypeError):
        stage.document["deployment_id"] = "attacker"  # type: ignore[index]
    with pytest.raises(TypeError):
        stage.tensors[stage.prefix + "ln_1.weight"][0] = 99.0  # type: ignore[index]
    for attribute, replacement in (
        ("document", {}),
        ("tensors", {}),
        ("inner_size", 1),
        ("assignment_id", "attacker"),
    ):
        with pytest.raises(AttributeError):
            setattr(stage, attribute, replacement)


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


def _runtime_graph() -> ExecutionGraph:
    cost = StageCost(
        prefill_work_units_per_prompt_token=1.0,
        decode_work_units_per_token=1.0,
        kv_bytes_per_context_token=16,
    )
    entry_placement = Placement(
        placement_id="placement-entry",
        node_id="node-entry",
        replica_group_id="entry-replicas",
        assignment_id="assignment-entry",
        stage_signature="entry-signature",
        load_proof_digest="sha256:" + "1" * 64,
        runtime_backend="numpy",
        runtime_endpoint="iroh://node-entry/assignment-entry",
    )
    pixel_placement = Placement(
        placement_id="placement-pixel",
        node_id="node-pixel",
        replica_group_id="pixel-replicas",
        assignment_id="assignment-pixel",
        stage_signature="pending-pixel-signature",
        load_proof_digest="sha256:" + "d" * 64,
        runtime_backend="pixel-stdlib",
        runtime_endpoint="iroh://node-pixel/assignment-pixel",
    )
    final_placement = Placement(
        placement_id="placement-final",
        node_id="node-final",
        replica_group_id="final-replicas",
        assignment_id="assignment-final",
        stage_signature="final-signature",
        load_proof_digest="sha256:" + "2" * 64,
        runtime_backend="numpy",
        runtime_endpoint="iroh://node-final/assignment-final",
    )
    stages = (
        Stage(
            stage_id="stage-000",
            layer_range=LayerRange(0, 1, 1),
            component_roles=("input_embedding", "decoder"),
            stage_cost=cost,
            placements=(entry_placement,),
        ),
        Stage(
            stage_id="stage-001",
            layer_range=LayerRange(1, 2, 1),
            component_roles=("decoder",),
            stage_cost=cost,
            placements=(pixel_placement,),
        ),
        Stage(
            stage_id="stage-002",
            layer_range=LayerRange(2, 3, 1),
            component_roles=("decoder", "final_norm", "lm_head"),
            stage_cost=cost,
            placements=(final_placement,),
        ),
    )
    graph = ExecutionGraph(
        deployment_id="deployment-pixel",
        deployment_epoch=7,
        topology_version=1,
        model_id="fixture/gpt2-tiny",
        resolved_commit="a" * 40,
        manifest_digest="sha256:" + "b" * 64,
        entry_stage_id="stage-000",
        final_stage_id="stage-002",
        hidden_size=4,
        activation_bytes=4,
        token_envelope_bytes=4,
        stages=stages,
        edges=(
            PlacementEdge(
                "edge-entry-pixel",
                "placement-entry",
                "placement-pixel",
                "iroh:node-entry->node-pixel",
            ),
            PlacementEdge(
                "edge-pixel-final",
                "placement-pixel",
                "placement-final",
                "iroh:node-pixel->node-final",
            ),
        ),
        loopback_edges=(
            PlacementEdge(
                "loopback-final-entry",
                "placement-final",
                "placement-entry",
                "iroh:node-final->node-entry",
            ),
        ),
    )
    signature = stage_signature_for_backend(graph, stages[1], "pixel-stdlib")
    bound_pixel = replace(pixel_placement, stage_signature=signature)
    return graph.with_stages(
        (stages[0], replace(stages[1], placements=(bound_pixel,)), stages[2])
    )


def _runtime_key(
    *, phase: str = "PREFILL", token_span: int = 3
) -> RuntimeBatchKey:
    graph = _runtime_graph()
    placement = graph.stages[1].placements[0]
    return RuntimeBatchKey(
        deployment_id=graph.deployment_id,
        deployment_epoch=graph.deployment_epoch,
        model_commit=graph.resolved_commit,
        manifest_digest=graph.manifest_digest,
        placement_id=placement.placement_id,
        assignment_id=placement.assignment_id,
        stage_signature=placement.stage_signature,
        load_proof_digest=placement.load_proof_digest,
        runtime_backend=placement.runtime_backend,
        phase=phase,
        hidden_size=graph.hidden_size,
        activation_bytes=graph.activation_bytes,
        token_span=token_span,
    )


def _runtime_item(*, payload: bytes | None = None) -> HopWorkItem:
    values = (
        0.1,
        -0.2,
        0.3,
        -0.4,
        0.05,
        0.06,
        -0.07,
        0.08,
        -0.3,
        0.2,
        0.1,
        -0.05,
    )
    return HopWorkItem(
        request_id="request-pixel",
        path_id="path-pixel",
        path_attempt=0,
        phase="PREFILL",
        token_index=-1,
        hop_index=1,
        placement_id="placement-pixel",
        qos_class="interactive",
        deficit_ratio=0.0,
        enqueued_at=0.0,
        idempotency_key="pixel-operation-1",
        payload=(
            payload
            if payload is not None
            else encode_activation(
                dtype="float32",
                shape=(1, 3, 4),
                data=struct.pack("<12f", *values),
            )
        ),
        batch_key=_runtime_key(),
        position=0,
        terminal=False,
    )


def _runtime(
    stage: PixelStage,
    *,
    graph: ExecutionGraph | None = None,
) -> PixelStageRuntimePort:
    graph = graph or _runtime_graph()
    return PixelStageRuntimePort(
        stage,
        graph=graph,
        placement_id=graph.stages[1].placements[0].placement_id,
        parent_assignment_digest=PARENT_ASSIGNMENT_DIGEST,
    )


def test_pixel_runtime_port_executes_router_activation_payload() -> None:
    stage = PixelStage.from_document(_pack())
    item = _runtime_item()

    result = _runtime(stage).execute(item)

    assert result.success is True
    assert result.token_id is None
    assert isinstance(result.payload, bytes)
    output = decode_activation(result.payload)
    assert output.dtype == "float32"
    assert output.shape == (1, 3, 4)
    expected = PixelStage.from_document(_pack()).execute(
        request_id="expected",
        assignment_id="assignment-pixel",
        stage_id="stage-001",
        hidden=[
            list(values)
            for values in struct.iter_unpack("<4f", decode_activation(item.payload).data)
        ],
    )
    actual = [list(values) for values in struct.iter_unpack("<4f", output.data)]
    assert max(
        abs(left - right)
        for actual_row, expected_row in zip(actual, expected)
        for left, right in zip(actual_row, expected_row)
    ) < 1e-6
    assert stage.request_count == 1


def test_pixel_runtime_port_rejects_wrong_router_batch_binding() -> None:
    stage = PixelStage.from_document(_pack())
    runtime = _runtime(stage)
    item = _runtime_item()
    assert isinstance(item.batch_key, RuntimeBatchKey)
    tampered = replace(
        item,
        batch_key=replace(item.batch_key, runtime_backend="numpy"),
    )

    result = runtime.execute(tampered)

    assert result.success is False
    assert result.failure_scope == "PLACEMENT"
    assert result.failure_reason == "batch_key_runtime_backend_mismatch"

    wrong_prefill_index = runtime.execute(
        replace(item, token_index=0, idempotency_key="pixel-operation-2")
    )
    assert wrong_prefill_index.success is False
    assert wrong_prefill_index.failure_reason == "prefill_sequence_mismatch"

    oversized = encode_activation(
        dtype="float32",
        shape=(1, 257, 4),
        data=b"\x00" * (257 * 4 * 4),
    )
    oversized_result = runtime.execute(
        replace(
            item,
            idempotency_key="pixel-operation-3",
            payload=oversized,
            batch_key=replace(_runtime_key(), token_span=257),
        )
    )
    assert oversized_result.success is False
    assert oversized_result.failure_reason == "activation_sequence_too_long"

    malformed_path = replace(item, path_id=[])  # type: ignore[arg-type]
    malformed_path_result = runtime.execute(malformed_path)
    assert malformed_path_result.success is False
    assert malformed_path_result.failure_reason == "invalid_runtime_path_id"

    graph = _runtime_graph()
    pixel_stage = graph.stages[1]
    tampered_placement = replace(
        pixel_stage.placements[0], stage_signature="sha256:" + "f" * 64
    )
    tampered_graph = graph.with_stages(
        (
            graph.stages[0],
            replace(pixel_stage, placements=(tampered_placement,)),
            graph.stages[2],
        )
    )
    with pytest.raises(PixelRuntimeError, match="stage_signature_mismatch"):
        PixelStageRuntimePort(
            stage,
            graph=tampered_graph,
            placement_id=tampered_placement.placement_id,
            parent_assignment_digest=PARENT_ASSIGNMENT_DIGEST,
        )

    with pytest.raises(
        PixelRuntimeError,
        match="stage_pack_parent_assignment_digest_mismatch",
    ):
        PixelStageRuntimePort(
            stage,
            graph=graph,
            placement_id=graph.stages[1].placements[0].placement_id,
            parent_assignment_digest="sha256:" + "e" * 64,
        )

    boolean_span_key = replace(
        _runtime_key(phase="DECODE", token_span=1),
        token_span=True,  # type: ignore[arg-type]
    )
    boolean_span = runtime.execute(
        replace(
            item,
            phase="DECODE",
            token_index=0,
            position=2,
            idempotency_key="pixel-operation-4",
            batch_key=boolean_span_key,
        )
    )
    assert boolean_span.success is False
    assert boolean_span.failure_reason == "invalid_batch_key_token_span"

    boolean_index = runtime.execute(
        replace(
            item,
            phase="RECOVERY_PREFILL",
            token_index=False,  # type: ignore[arg-type]
            idempotency_key="pixel-operation-5",
            batch_key=_runtime_key(phase="RECOVERY_PREFILL"),
        )
    )
    assert boolean_index.success is False
    assert boolean_index.failure_reason == "invalid_runtime_token_index"

    boolean_attempt = runtime.execute(
        replace(
            item,
            path_attempt=False,  # type: ignore[arg-type]
            idempotency_key="pixel-operation-6",
        )
    )
    assert boolean_attempt.success is False
    assert boolean_attempt.failure_reason == "invalid_runtime_path_attempt"

    boolean_width = runtime.execute(
        replace(
            item,
            idempotency_key="pixel-operation-7",
            batch_key=replace(item.batch_key, speculative_width=False),  # type: ignore[arg-type]
        )
    )
    assert boolean_width.success is False
    assert boolean_width.failure_reason == "invalid_batch_key_speculative_width"


def test_pixel_runtime_port_core_binding_and_batch_container_are_immutable() -> None:
    stage = PixelStage.from_document(_pack())
    runtime = _runtime(stage)
    replacement_stage = PixelStage.from_document(_pack())

    for attribute, replacement in (
        ("stage", replacement_stage),
        ("placement_id", "attacker-placement"),
        ("_binding", {}),
    ):
        with pytest.raises(AttributeError):
            setattr(runtime, attribute, replacement)

    item = _runtime_item()
    mutable_batch = RuntimeBatch(
        compatibility_key=item.batch_key,
        items=[item],  # type: ignore[arg-type]
    )
    results = runtime.execute_batch(mutable_batch)
    assert results == (
        pixel_runtime.RuntimeResult(
            success=False,
            failure_scope="PLACEMENT",
            failure_reason="invalid_runtime_batch_items",
        ),
    )


def test_pixel_runtime_port_rejects_boolean_graph_integer_metadata() -> None:
    stage = PixelStage.from_document(_pack())
    graph = replace(_runtime_graph(), topology_version=False)  # type: ignore[arg-type]

    with pytest.raises(PixelRuntimeError, match="invalid_graph_topology_version"):
        _runtime(stage, graph=graph)


def test_pixel_runtime_port_replay_cancellation_and_close_fail_closed() -> None:
    stage = PixelStage.from_document(_pack())
    runtime = _runtime(stage)
    item = _runtime_item()

    first = runtime.execute(item)
    assert runtime.execute(item) == first
    assert stage.request_count == 1
    changed_payload = encode_activation(
        dtype="float32",
        shape=(1, 3, 4),
        data=struct.pack("<12f", *([0.25] * 12)),
    )
    conflict = runtime.execute(replace(item, payload=changed_payload))
    assert conflict.success is False
    assert conflict.failure_reason == "replay_fingerprint_mismatch"

    other_path = replace(item, path_id="path-other")
    other_result = runtime.execute(other_path)
    assert other_result.success is True
    assert stage.request_count == 2

    left = replace(
        item,
        path_id="pixel-path-left",
        idempotency_key="operation\x00right",
    )
    right = replace(
        item,
        path_id="pixel-path-left\x00operation",
        idempotency_key="right",
    )
    assert runtime._stage_request_id(left) != runtime._stage_request_id(right)

    runtime.cancel(item.path_id)
    snapshot = runtime.kv_snapshot()
    assert snapshot["retained_result_count"] == 1
    assert snapshot["route_ready"] is False
    cancelled = runtime.execute(
        replace(item, idempotency_key="pixel-operation-2")
    )
    assert cancelled.success is False
    assert cancelled.failure_reason == "path_cancelled"
    assert runtime.execute(other_path) == other_result
    assert stage.request_count == 2

    runtime.close(reason="test_shutdown")
    closed = runtime.execute(
        replace(item, path_id="path-other", idempotency_key="pixel-operation-3")
    )
    assert closed.success is False
    assert closed.failure_reason == "runtime_closed"


def test_pixel_runtime_port_rolls_back_replay_when_output_encoding_fails(
    monkeypatch,
) -> None:
    stage = PixelStage.from_document(_pack())
    runtime = _runtime(stage)
    item = _runtime_item()
    execute = PixelStage.execute

    def overflow_after_execution(self, **kwargs):
        execute(self, **kwargs)
        return [[1e100] * 4 for _ in range(3)]

    monkeypatch.setattr(PixelStage, "execute", overflow_after_execution)
    failed = runtime.execute(item)
    assert failed.success is False
    assert failed.failure_reason == "pixel_stage_execution_rejected"

    monkeypatch.setattr(PixelStage, "execute", execute)
    retried = runtime.execute(item)
    assert retried.success is True
    assert stage.request_count == 2


def test_pixel_runtime_port_cancellation_fence_saturates_fail_closed(
    monkeypatch,
) -> None:
    monkeypatch.setattr(pixel_runtime, "_MAX_RETAINED_PATHS", 2)
    runtime = _runtime(PixelStage.from_document(_pack()))
    for path_id in ("cancelled-a", "cancelled-b", "cancelled-c"):
        runtime.cancel(path_id)

    stale = runtime.execute(replace(_runtime_item(), path_id="cancelled-a"))
    assert stale.success is False
    assert stale.failure_reason == "cancellation_fence_saturated"
    snapshot = runtime.kv_snapshot()
    assert snapshot["cancelled_path_count"] == 2
    assert snapshot["cancellation_fence_saturated"] is True


def test_pixel_runtime_port_replay_capacity_is_bounded_and_reclaimable(
    monkeypatch,
) -> None:
    monkeypatch.setattr(pixel_runtime, "_MAX_RETAINED_RESULTS", 2, raising=False)
    stage = PixelStage.from_document(_pack())
    runtime = _runtime(stage)
    item = _runtime_item()
    assert runtime.execute(item).success is True
    assert runtime.execute(
        replace(item, idempotency_key="pixel-operation-2")
    ).success is True

    full = runtime.execute(replace(item, idempotency_key="pixel-operation-3"))
    assert full.success is False
    assert full.failure_reason == "runtime_replay_capacity_exhausted"
    assert stage.request_count == 2
    assert runtime.kv_snapshot()["retained_result_count"] == 2

    runtime.cancel(item.path_id)
    reclaimed = runtime.execute(
        replace(
            item,
            path_id="pixel-path-reclaimed",
            idempotency_key="pixel-operation-3",
        )
    )
    assert reclaimed.success is True


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
