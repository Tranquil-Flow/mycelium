# SPDX-License-Identifier: AGPL-3.0-only
"""Generate the phone stage-only node's cross-language wire golden vectors.

A stage-only phone sits in the MIDDLE of a locked three-placement route
(Mac entry -> phone -> Mac final). These vectors are produced by running the
real Python ``mycelium_router.router.Router`` as that middle node (node id
``node-phone``) with a deterministic stand-in runtime, so they record exactly
what a conforming middle node receives, what work item it runs and what it
must send:

* ``ManifestLocked`` for the route (finite reservation expiries), plus the
  fields the phone must extract from it (entry node, its own hop, next hop,
  prompt length);
* PREFILL and DECODE ``HopHeader`` frames into the phone carrying MYAC float32
  activations ``(1, seq, hidden)``, the ``HopWorkItem`` the Router derives
  (position, idempotency key, placement), and the byte-exact forwarded frame
  for the next hop (recomputed ``hop_index``/placements/idempotency key);
* the ``FailureReport`` the phone originates when its stage fails;
* the entry's ``PathCancellation`` for the path;
* MYAC f32 encodings and the payload fingerprint the replay cache keys on.

The Rust node core must reproduce every forwarded/originated frame byte for
byte (``native/mobile_core/tests/phone_stage_wire.rs``), and the Python test
``tests/contracts/test_phone_stage_wire_golden.py`` regenerates them, so a
drift on either side fails a test.

Run from the repository root:
    python3 -B scripts/generate_phone_stage_wire_golden.py [--check]
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import struct
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mycelium_router.contracts import (  # noqa: E402
    ExecutionGraph,
    HopHeader,
    LayerRange,
    ManifestLocked,
    PathBuildState,
    PathCancellation,
    PathHop,
    PathManifest,
    Placement,
    PlacementEdge,
    RequestContext,
    RouterConfig,
    RuntimeResult,
    Stage,
    StageCost,
)
from mycelium_router.fakes import (  # noqa: E402
    FakeCapacityPort,
    FakeDeviceStateProvider,
    FakeTopologyProvider,
    FakeTransportPort,
    ManualClock,
    SequenceIdSource,
)
from mycelium_router.idempotency import hop_idempotency_key, payload_fingerprint  # noqa: E402
from mycelium_router.payloads import encode_activation  # noqa: E402
from mycelium_router.router import Router  # noqa: E402
from mycelium_router.wire import ROUTER_WIRE_PROTOCOL, decode_frame, encode_frame  # noqa: E402

OUTPUT_DIR = ROOT / "contracts" / "phone-stage-wire-golden"
INDEX_SCHEMA = "mycelium.phone_stage_wire.golden.v1"
HIDDEN = 8
PHONE_NODE = "node-phone"
PROMPT = (151644, 872, 198, 785)


def _graph() -> ExecutionGraph:
    rows = (
        ("placement-entry", "node-mac-entry", "mlx", (0, 12), ("TOKEN_EMBEDDING", "DECODER_BLOCKS")),
        ("placement-phone", PHONE_NODE, "mobile-rust-qwen2", (12, 14), ("DECODER_BLOCKS",)),
        ("placement-final", "node-mac-final", "mlx", (14, 24), ("DECODER_BLOCKS", "FINAL_NORM", "LM_HEAD")),
    )
    stages = tuple(
        Stage(
            stage_id=f"stage-{index:03}",
            layer_range=LayerRange(start, end, end - start),
            component_roles=roles,
            stage_cost=StageCost(1.0, 1.0, 16),
            placements=(
                Placement(
                    placement_id=placement_id,
                    node_id=node_id,
                    replica_group_id=f"replica-{index}",
                    assignment_id=f"assignment-{index}",
                    stage_signature=f"stage-signature-{index}",
                    load_proof_digest=f"load-proof-{index}",
                    runtime_backend=backend,
                    runtime_endpoint="",
                ),
            ),
        )
        for index, (placement_id, node_id, backend, (start, end), roles) in enumerate(rows)
    )
    ids = [stage.placements[0].placement_id for stage in stages]
    return ExecutionGraph(
        deployment_id="deployment-phone-golden",
        deployment_epoch=5,
        topology_version=9,
        model_id="Qwen/Qwen2.5-0.5B-Instruct",
        resolved_commit="7ae557604adf67be50417f59c2c2f167def9a775",
        manifest_digest="manifest-phone-golden",
        entry_stage_id=stages[0].stage_id,
        final_stage_id=stages[-1].stage_id,
        hidden_size=HIDDEN,
        activation_bytes=HIDDEN * 4,
        token_envelope_bytes=16,
        stages=stages,
        edges=(
            PlacementEdge("edge-entry-phone", ids[0], ids[1], "link-entry-phone"),
            PlacementEdge("edge-phone-final", ids[1], ids[2], "link-phone-final"),
        ),
        loopback_edges=(PlacementEdge("edge-final-entry", ids[2], ids[0], "link-final-entry"),),
    )


def _activation(seq: int, scale: float) -> bytes:
    # Exact f32 values (multiples of 1/8), little-endian, shape (1, seq, hidden).
    values = [scale * ((i % 17) - 8) / 8.0 for i in range(seq * HIDDEN)]
    return encode_activation(dtype="float32", shape=(1, seq, HIDDEN), data=struct.pack(f"<{len(values)}f", *values))


class _StageRuntime:
    """Deterministic stand-in for the phone's Qwen2 stage: returns a fixed
    output activation per work item (the wire contract does not depend on the
    numbers), or the injected failure."""

    decode_mode = "complete_context_replay"

    def __init__(self) -> None:
        self.executed: list[Any] = []
        self.fail = False

    def execute(self, item: Any) -> RuntimeResult:
        self.executed.append(item)
        if self.fail:
            return RuntimeResult(success=False, failure_scope="PLACEMENT", failure_reason="stage_runtime_failed")
        seq = len(PROMPT) if item.phase == "PREFILL" else item.position + 1
        return RuntimeResult(success=True, payload=_activation(seq, 0.5))

    def execute_batch(self, batch: Any) -> tuple[RuntimeResult, ...]:
        return tuple(self.execute(item) for item in batch.items)

    def cancel(self, path_id: str) -> None:
        pass


def _route():
    graph = _graph()
    request = RequestContext(
        request_id="request-phone-golden",
        prompt_token_ids=PROMPT,
        max_new_tokens=8,
        expected_new_tokens=4,
        qos_class="interactive",
        admitted_at=1_790_000_000.0,
        target_ttft_ms=2000.0,
        target_tpot_ms=1500.0,
        target_tokens_per_second=1.0,
        sampling_seed=7,
        generation_config_digest="generation-phone-golden",
    )
    hops = tuple(
        PathHop(
            stage_id=stage.stage_id,
            placement_id=stage.placements[0].placement_id,
            reservation_id=f"reservation-{index}",
            reservation_expires_at=1_790_000_600.0,
            reservation_epoch=graph.deployment_epoch,
        )
        for index, stage in enumerate(graph.stages)
    )
    build = PathBuildState(request=request, graph=graph, path_id="path-phone-golden", path_attempt=2, ordered_hops=hops)
    manifest = PathManifest(
        path_id=build.path_id,
        path_attempt=build.path_attempt,
        request_id=request.request_id,
        deployment_id=graph.deployment_id,
        deployment_epoch=graph.deployment_epoch,
        topology_version=graph.topology_version,
        manifest_digest=graph.manifest_digest,
        ordered_hops=hops,
        loopback_edge_id=graph.loopback_edges[0].edge_id,
    )
    return graph, request, build, manifest


def _phone_router(graph: ExecutionGraph, request: RequestContext, manifest: PathManifest):
    runtime = _StageRuntime()
    transport = FakeTransportPort()
    router = Router(
        node_id=PHONE_NODE,
        topology=FakeTopologyProvider(graph),
        device_states=FakeDeviceStateProvider({}),
        capacity=FakeCapacityPort(),
        runtime=runtime,
        transport=transport,
        clock=ManualClock(),
        id_source=SequenceIdSource(),
        config=RouterConfig(),
    )
    if not router.relay.register_path(request, manifest, graph, entry_node_id="node-mac-entry"):
        raise RuntimeError("phone router refused the locked path")
    return router, runtime, transport


def _header(manifest: PathManifest, request: RequestContext, phase: str, token_index: int) -> HopHeader:
    return HopHeader(
        request_id=request.request_id,
        path_id=manifest.path_id,
        path_attempt=manifest.path_attempt,
        phase=phase,
        token_index=token_index,
        hop_index=1,
        source_placement_id="placement-entry",
        destination_placement_id="placement-phone",
        topology_version=manifest.topology_version,
        idempotency_key=hop_idempotency_key(
            request_id=request.request_id,
            path_id=manifest.path_id,
            path_attempt=manifest.path_attempt,
            phase=phase,
            token_index=token_index,
            hop_index=1,
        ),
        prefill_chunk_token_count=len(PROMPT) if phase == "PREFILL" else 0,
    )


def _work_item(item: Any) -> dict[str, Any]:
    return {
        "request_id": item.request_id,
        "path_id": item.path_id,
        "path_attempt": item.path_attempt,
        "phase": item.phase,
        "token_index": item.token_index,
        "hop_index": item.hop_index,
        "placement_id": item.placement_id,
        "idempotency_key": item.idempotency_key,
        "position": item.position,
        "emits_token": item.emits_token,
        "terminal": item.terminal,
    }


def _cases() -> tuple[list[tuple[str, bytes]], list[dict[str, Any]]]:
    graph, request, build, manifest = _route()
    frames: list[tuple[str, bytes]] = []
    cases: list[dict[str, Any]] = []

    def frame(name: str, message: object, payload: bytes = b"") -> str:
        data = encode_frame(message, payload)
        decoded = decode_frame(data)
        if type(decoded.message) is not type(message) or decoded.payload != payload:
            raise RuntimeError(f"round trip failed: {name}")
        frames.append((name, data))
        return name

    locked = ManifestLocked(request.request_id, build.path_id, build.path_attempt, manifest, build)
    cases.append(
        {
            "case": "manifest_locked",
            "input": frame("01-manifest-locked.bin", locked),
            "expect": {
                "request_id": request.request_id,
                "path_id": manifest.path_id,
                "path_attempt": manifest.path_attempt,
                "topology_version": manifest.topology_version,
                "entry_node_id": "node-mac-entry",
                "local_node_id": PHONE_NODE,
                "local_hop_index": 1,
                "local_placement_id": "placement-phone",
                "next_placement_id": "placement-final",
                "next_node_id": "node-mac-final",
                "previous_placement_id": "placement-entry",
                "previous_node_id": "node-mac-entry",
                "prompt_length": len(PROMPT),
                "hidden_size": HIDDEN,
            },
        }
    )
    for name, phase, token_index in (("prefill", "PREFILL", -1), ("decode", "DECODE", 2)):
        router, runtime, transport = _phone_router(graph, request, manifest)
        header = _header(manifest, request, phase, token_index)
        seq = len(PROMPT) if phase == "PREFILL" else len(PROMPT) + token_index
        payload = _activation(seq, 1.0)
        result = router.receive_hop(header, payload)
        if result.disposition != "FORWARDED" or len(transport.hops) != 1 or len(runtime.executed) != 1:
            raise RuntimeError(f"phone did not forward {name}: {result}")
        # A duplicate delivery (sender retry) must not execute or forward again.
        again = router.receive_hop(header, payload)
        if len(runtime.executed) != 1 or len(transport.hops) != 1 or again.disposition != result.disposition:
            raise RuntimeError(f"duplicate {name} hop was re-executed")
        out_header, out_payload = transport.hops[0]
        index = 2 if phase == "PREFILL" else 4
        cases.append(
            {
                "case": f"hop_{name}",
                "input": frame(f"0{index}-hop-{name}-in.bin", header, payload),
                "work_item": _work_item(runtime.executed[0]),
                "input_seq": seq,
                "stage_output_payload_sha256": hashlib.sha256(out_payload).hexdigest(),
                "forward": frame(f"0{index + 1}-hop-{name}-forward.bin", out_header, out_payload),
                "forward_destination_node_id": "node-mac-final",
                "input_payload_fingerprint": payload_fingerprint(payload),
                "duplicate_delivery": "cached_no_reexecution_no_resend",
            }
        )
    router, runtime, transport = _phone_router(graph, request, manifest)
    runtime.fail = True
    header = _header(manifest, request, "DECODE", 3)
    result = router.receive_hop(header, _activation(len(PROMPT) + 3, 1.0))
    if result.disposition != "FAILED" or len(transport.failure_reports) != 1:
        raise RuntimeError(f"phone failure did not report: {result}")
    cases.append(
        {
            "case": "stage_failure",
            "input": frame("06-hop-failure-in.bin", header, _activation(len(PROMPT) + 3, 1.0)),
            "originate": frame("07-failure-report.bin", transport.failure_reports[0]),
            "originate_destination_node_id": "node-mac-entry",
        }
    )
    cancellation = PathCancellation(request.request_id, manifest.path_id, manifest.path_attempt, manifest.topology_version)
    router, runtime, transport = _phone_router(graph, request, manifest)
    if not router.receive_path_cancellation(cancellation, source_node_id="node-mac-entry"):
        raise RuntimeError("phone refused the entry's cancellation")
    late = router.receive_hop(_header(manifest, request, "DECODE", 4), _activation(len(PROMPT) + 4, 1.0))
    cases.append(
        {
            "case": "path_cancellation",
            "input": frame("08-path-cancellation.bin", cancellation),
            "accepted_from_node_id": "node-mac-entry",
            "late_hop": frame("09-hop-after-cancel-in.bin", _header(manifest, request, "DECODE", 4), _activation(len(PROMPT) + 4, 1.0)),
            "late_hop_disposition": late.disposition,
            "late_hop_reason": late.reason,
            "late_hop_executed": len(runtime.executed),
        }
    )
    # Hops a middle node must refuse, each with the reason the Python Router
    # gives (the phone must refuse the same frames, with the same codes, and
    # never execute them).
    from dataclasses import replace  # noqa: PLC0415

    base = _header(manifest, request, "DECODE", 1)
    payload = _activation(len(PROMPT) + 1, 1.0)

    def rekey(header: HopHeader) -> HopHeader:
        return replace(
            header,
            idempotency_key=hop_idempotency_key(
                request_id=header.request_id,
                path_id=header.path_id,
                path_attempt=header.path_attempt,
                phase=header.phase,
                token_index=header.token_index,
                hop_index=header.hop_index,
            ),
        )

    mutations = [
        ("invalid_idempotency_key", replace(base, idempotency_key=base.idempotency_key + "x"), "node-mac-entry"),
        ("unknown_path", rekey(replace(base, path_id="path-other")), "node-mac-entry"),
        ("stale_path_attempt", rekey(replace(base, path_attempt=1)), "node-mac-entry"),
        ("topology_version_mismatch", replace(base, topology_version=8), "node-mac-entry"),
        ("invalid_hop_index", rekey(replace(base, hop_index=3)), "node-mac-entry"),
        ("manifest_hop_mismatch", replace(base, destination_placement_id="placement-final"), "node-mac-entry"),
        ("source_placement_mismatch", replace(base, source_placement_id="placement-final"), "node-mac-entry"),
        ("source_node_mismatch", base, "node-mac-final"),
        ("destination_not_local", rekey(replace(base, hop_index=2, source_placement_id="placement-phone", destination_placement_id="placement-final")), "node-phone"),
    ]
    rejections = []
    for index, (reason, header, source) in enumerate(mutations):
        router, runtime, _transport = _phone_router(graph, request, manifest)
        result = router.receive_hop(header, payload, source_node_id=source)
        if result.disposition != "REJECTED" or result.reason != reason or runtime.executed:
            raise RuntimeError(f"expected {reason}, got {result}")
        rejections.append(
            {"frame": frame(f"{10 + index:02}-reject-{reason}.bin", header, payload), "source_node_id": source, "reason": reason}
        )
    # The same idempotency key with a different payload after a completed hop.
    router, runtime, _transport = _phone_router(graph, request, manifest)
    router.receive_hop(base, payload, source_node_id="node-mac-entry")
    other = _activation(len(PROMPT) + 1, 2.0)
    result = router.receive_hop(base, other, source_node_id="node-mac-entry")
    if result.reason != "idempotency_payload_mismatch" or len(runtime.executed) != 1:
        raise RuntimeError(f"expected idempotency_payload_mismatch, got {result}")
    cases.append(
        {
            "case": "rejections",
            "rejections": rejections,
            "replay_first": frame("19-replay-first-in.bin", base, payload),
            "replay_conflict": frame("20-replay-conflict-in.bin", base, other),
            "replay_conflict_reason": "idempotency_payload_mismatch",
        }
    )
    return frames, cases


def generate(check: bool = False) -> dict[str, Any]:
    frames, cases = _cases()
    entries = [
        {"file": name, "frame_sha256": hashlib.sha256(data).hexdigest(), "byte_length": len(data)} for name, data in frames
    ]
    index = {"schema": INDEX_SCHEMA, "protocol": ROUTER_WIRE_PROTOCOL, "hidden_size": HIDDEN, "frames": entries, "cases": cases}
    rendered = json.dumps(index, indent=2, sort_keys=True) + "\n"
    if check:
        current = (OUTPUT_DIR / "index.json").read_text(encoding="utf-8")
        same = current == rendered and all((OUTPUT_DIR / name).read_bytes() == data for name, data in frames)
        if not same:
            raise SystemExit("phone stage wire golden vectors are stale; regenerate them")
        return index
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for old in OUTPUT_DIR.glob("*.bin"):
        if old.name not in {name for name, _ in frames}:
            old.unlink()
    for name, data in frames:
        (OUTPUT_DIR / name).write_bytes(data)
    (OUTPUT_DIR / "index.json").write_text(rendered, encoding="utf-8")
    return index


if __name__ == "__main__":
    result = generate(check="--check" in sys.argv[1:])
    print(f"phone stage wire golden: {len(result['frames'])} frames, {len(result['cases'])} cases")
