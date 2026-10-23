# SPDX-License-Identifier: AGPL-3.0-only
"""A7 product path through ``PhysicalLiveRoute`` and real node processes.

These tests parallel ``tests/a7_acceptance/test_a7_two_node_native.py`` but drive
through the product route boundary: a real ``PhysicalLiveRoute`` opens two real
``physical_inference_node.py`` processes over native iroh, the staged A7 route
authority claims a killed stage-1 source, and the entry resumes through the
stage-1 standby placement on the entry node.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import sys
import tempfile
import time
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import mlx.core as mx
import pytest

import mycelium_request_gateway.contracts as gateway_contracts
from mycelium_a7_node_wiring import validate_against_graph, validate_node_config
from mycelium_a7_standby_plan import (
    compile_standby_artifacts,
    extend_graph_with_standby,
    node_configs,
    write_standby_secret,
)
from mycelium_a7_standby_qualification import (
    parity_challenge_result,
    port_placement_identity,
    qualify_standby,
)
from mycelium_live.route import PhysicalLiveRoute, _ConcurrentNodeSession
from mycelium_live.router_port import LiveRouterPort
from mycelium_live.supervisor import ColdRouteRestartOwner
from mycelium_physical_runner.remote_probe import derive_local_run_scoped_identity
from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_qualification.physical_deployment import (
    build_execution_graph,
    build_physical_device_states,
    prepare_physical_deployment,
)
from mycelium_router.contracts import PathHop, PathManifest
from mycelium_router.decoding import quantized_greedy_token_id
from mycelium_router.layer_builder import layer_load_proof_digest
from mycelium_router.mlx_runtime import MLXRuntimePort
from mycelium_router.payloads import encode_token_ids
from mycelium_router.serialization import path_manifest_to_dict
from physical_inference_qualification import PeerIdentity, QualificationController
from runtime_loader import canonical_json, execute_loaded_stage, load_assignment_stage
from tests.physical_qualification.test_node_service import NODE_SCRIPT, ROOT, SIDECAR_BINARY
from two_process_runtime_qualification import _LocalOnlyFetcher

STANDBY_ID = "placement-001-standby"
_PROMPT = (1, 2, 3)
_MAX_NEW = 5
_QUALIFICATION_DIGEST = "sha256:" + "7" * 64
_NODE_A = "node-a"
_NODE_B = "node-b"
_SOURCE_NODE_ID = _NODE_B
_ENTRY_NODE_ID = _NODE_A

pytestmark = pytest.mark.skipif(
    not SIDECAR_BINARY.is_file(), reason="native iroh sidecar not built"
)


class _RecordingSink:
    def __init__(self) -> None:
        self.tokens: list[tuple[int, int]] = []

    def emit(self, token_index: int, token_id: int) -> None:
        self.tokens.append((token_index, token_id))


class _KillAfterCommittedTokens(_RecordingSink):
    """SIGKILL the source once ``kill_after`` tokens are committed and acknowledged."""

    def __init__(self, state: "_RouteState", request_id: str, *, kill_after: int = 2) -> None:
        super().__init__()
        self._state = state
        self._request_id = request_id
        self._kill_after = kill_after
        self.kill_at_monotonic_s: float | None = None
        self.killed = False

    def emit(self, token_index: int, token_id: int) -> None:
        super().emit(token_index, token_id)
        if token_index == self._kill_after - 1 and not self.killed:
            _wait_for_a7_acknowledged(self._state.route, self._request_id, acknowledged=self._kill_after)
            source = self._state.session_factory.sessions[_SOURCE_NODE_ID]
            pid = source._process.pid
            self.kill_at_monotonic_s = time.monotonic()
            os.killpg(os.getpgid(pid), signal.SIGKILL)
            self.killed = True
            _wait_for_process_exit(source)


@dataclass
class _RouteState:
    route: PhysicalLiveRoute
    graph: Any
    deployment: Any
    session_factory: Any
    root: Path
    reference_tokens: tuple[int, ...]
    dtype_mismatch: bool
    qualification_expired: bool

    def close(self) -> None:
        try:
            self.route.close()
        finally:
            shutil.rmtree(self.root, ignore_errors=True)


class _A7SessionFactory:
    """Stage A7 config and launch the real node process with local-only iroh."""

    def __init__(
        self,
        *,
        roots: Mapping[str, Path],
        socket_roots: Mapping[str, Path],
        configs: Mapping[str, Mapping[str, Any]],
        graph: Any,
    ) -> None:
        self._roots = dict(roots)
        self._socket_roots = dict(socket_roots)
        self._configs = {node: dict(config) for node, config in configs.items()}
        self._graph = graph
        self.sessions: dict[str, Any] = {}
        self.token_files = {}

    def __call__(
        self,
        *,
        argv: tuple[str, ...],
        node_id: str,
        run_id: str,
        deployment_id: str,
        timeout_seconds: float,
    ) -> Any:
        # ``argv`` is the route's ordinary subprocess command.  This test keeps
        # the product route session seam but uses the same direct local argv
        # shape as ``_NodeClient`` plus A7's longer timeout and local-only iroh.
        _ = argv
        config = validate_node_config(
            self._configs[node_id], deployment_id=deployment_id, node_id=node_id
        )
        validate_against_graph(config, self._graph, node_id=node_id)
        _write(self._roots[node_id] / "a7" / "node.json", config)
        command = (
            sys.executable,
            "-B",
            str(NODE_SCRIPT),
            "--run-id",
            run_id,
            "--deployment-id",
            deployment_id,
            "--node-id",
            node_id,
            "--artifact-root",
            str(self._roots[node_id]),
            "--socket-root",
            str(self._socket_roots[node_id]),
            "--sidecar-binary",
            str(SIDECAR_BINARY),
            "--sidecar-local-only",
            "--command-timeout",
            "900",
            "--decode-mode",
            "stage_local_kv",
        )
        if self.token_files:
            command += ("--endpoint-secret-file", str(self.token_files[node_id][0]),
                        "--token-policy-trust-file", str(self.token_files[node_id][1]))
        session = _ConcurrentNodeSession(
            argv=command,
            node_id=node_id,
            run_id=run_id,
            deployment_id=deployment_id,
            timeout_seconds=timeout_seconds,
        )
        self.sessions[node_id] = session
        return session


def _write(path: Path, document: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(document))


def _sha_json(document: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _request(request_id: str) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "prompt_token_ids": list(_PROMPT),
        "max_new_tokens": _MAX_NEW,
        "expected_new_tokens": _MAX_NEW,
        "qos_class": "interactive",
        "admitted_at": 0.0,
        "target_ttft_ms": 1_000.0,
        "target_tpot_ms": 1_000.0,
        "target_tokens_per_second": 1.0,
        "sampling_seed": 17,
        "generation_config_digest": "sha256:" + "a" * 64,
    }


def _reference_tokens(deployment: Any) -> tuple[int, ...]:
    reference = load_assignment_stage(
        deployment.reference_assignment, deployment.reference_report, load_generation=7
    )
    context = list(_PROMPT)
    expected: list[int] = []
    for _ in range(_MAX_NEW):
        logits = execute_loaded_stage(
            reference, token_ids=mx.array((tuple(context),), dtype=mx.uint32)
        )
        mx.eval(logits)
        token = quantized_greedy_token_id(logits[0, -1, :].tolist())
        expected.append(token)
        context.append(token)
    return tuple(expected)


def _hop_item(placement, stage_index, payload, *, phase, path_id, token_index, position, span, graph, attempt=0):
    from mycelium_router.contracts import HopWorkItem, RuntimeBatchKey

    return HopWorkItem(
        request_id="qualification",
        path_id=path_id,
        path_attempt=attempt,
        phase=phase,
        token_index=token_index,
        hop_index=stage_index,
        placement_id=placement.placement_id,
        qos_class="interactive",
        deficit_ratio=0.0,
        enqueued_at=0.0,
        idempotency_key=f"{path_id}:{phase}:{token_index}:{stage_index}",
        payload=payload,
        batch_key=RuntimeBatchKey(
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
            token_span=span,
        ),
        position=position,
        lease_expires_at=1e15,
    )


def _qualification(graph: Any, loaded: list[Any], standby_loaded: Any, *, dtype_mismatch: bool, qualification_expired: bool = False) -> dict[str, Any]:
    """Exact standby record; incompatible records are valid but unqualified.

    When ``dtype_mismatch`` is True the standby runtime was built with a
    dtype that does not match the source's. The MLX runtime refuses to
    bind a STANDBY whose activation bytes differ from the graph's, so the
    in-process parity challenge cannot run. We still emit a *closed*
    unqualified record with no parity challenge; the A7 entry controller
    will treat this as ``full_context_replay`` on cutover.
    """

    source_id = graph.stages[1].placements[0].placement_id
    entry_id = graph.stages[0].placements[0].placement_id
    entry_a = MLXRuntimePort(_NODE_A, graph, {entry_id: loaded[0]}, decode_mode="stage_local_kv")
    entry_b = MLXRuntimePort(_NODE_A, graph, {entry_id: loaded[0]}, decode_mode="stage_local_kv")
    source = MLXRuntimePort(_NODE_B, graph, {source_id: loaded[1]}, decode_mode="stage_local_kv")
    if dtype_mismatch:
        # A STANDBY whose dtype does not match the graph's activation_bytes
        # is refused by MLXRuntimePort at bind time -- this is the in-process
        # mirror of the A7 cross-host fallback.  Emit a closed *but* unqualified
        # record; the A7 cutover predicate will reject the import and route the
        # request to A6.
        record = qualify_standby(
            source_identity=port_placement_identity(source, source_id),
            standby_identity={
                "protocol": "mycelium.a7_standby_placement_identity.v1",
                "placement_id": "sha256:" + hashlib.sha256(STANDBY_ID.encode("utf-8")).hexdigest(),
                "lifecycle_state": "STANDBY",
                "standby_runtime_dtype_mismatch": True,
            },
            parity_challenge=None,
            reserved_kv_bytes=1 << 20,
            available_bytes=1 << 30,
            issued_at_unix_ms=int(time.time() * 1000) - 1_000,
            ttl_ms=3_600_000,
        )
        assert record["qualified"] is False
        return record
    standby = MLXRuntimePort(
        _NODE_A, graph, {STANDBY_ID: standby_loaded}, decode_mode="stage_local_kv"
    )

    stage0 = graph.stages[0].placements[0]
    stage1 = graph.stages[1].placements[0]
    standby_placement = graph.stages[1].placements[1]
    challenge = None
    hidden = entry_a.execute(
        _hop_item(
            stage0,
            0,
            encode_token_ids(tuple(_PROMPT)),
            phase="PREFILL",
            path_id="q:src",
            token_index=-1,
            position=0,
            span=len(_PROMPT),
            graph=graph,
        )
    )
    first = source.execute(
        _hop_item(
            stage1,
            1,
            hidden.payload,
            phase="PREFILL",
            path_id="q:src",
            token_index=-1,
            position=0,
            span=len(_PROMPT),
            graph=graph,
        )
    )
    assert first.success, first.failure_reason
    standby.import_kv_checkpoint(
        source.export_kv_checkpoint("q:src"),
        request_id="qualification",
        path_id="q:sb",
        path_attempt=1,
        placement_id=STANDBY_ID,
        lease_expires_at=1e15,
        expected_next_position=len(_PROMPT),
        max_bytes=1 << 24,
    )
    entry_b.import_kv_checkpoint(
        entry_a.export_kv_checkpoint("q:src"),
        request_id="qualification",
        path_id="q:sb",
        path_attempt=1,
        placement_id=entry_id,
        lease_expires_at=1e15,
        expected_next_position=len(_PROMPT),
        max_bytes=1 << 24,
    )
    step = encode_token_ids((first.token_id,))
    hidden_a = entry_a.execute(
        _hop_item(
            stage0,
            0,
            step,
            phase="DECODE",
            path_id="q:src",
            token_index=1,
            position=len(_PROMPT),
            span=1,
            graph=graph,
        )
    )
    hidden_b = entry_b.execute(
        _hop_item(
            stage0,
            0,
            step,
            phase="DECODE",
            path_id="q:sb",
            token_index=1,
            position=len(_PROMPT),
            span=1,
            graph=graph,
            attempt=1,
        )
    )
    source_next = source.execute(
        _hop_item(
            stage1,
            1,
            hidden_a.payload,
            phase="DECODE",
            path_id="q:src",
            token_index=1,
            position=len(_PROMPT),
            span=1,
            graph=graph,
        )
    )
    standby_next = standby.execute(
        _hop_item(
            standby_placement,
            1,
            hidden_b.payload,
            phase="DECODE",
            path_id="q:sb",
            token_index=1,
            position=len(_PROMPT),
            span=1,
            graph=graph,
            attempt=1,
        )
    )
    assert source_next.success and standby_next.success, (
        source_next.failure_reason,
        standby_next.failure_reason,
    )
    challenge = parity_challenge_result(
        challenge_id="a7-product-path",
        next_position=len(_PROMPT),
        source_output=str(source_next.token_id).encode(),
        standby_output=str(standby_next.token_id).encode(),
    )

    if qualification_expired:
        # Issue the record 10 s in the past with a 1 ms TTL so
        # standby_qualification_is_current() is False at cutover time.
        record = qualify_standby(
            source_identity=port_placement_identity(source, source_id),
            standby_identity=port_placement_identity(standby, STANDBY_ID),
            parity_challenge=challenge,
            reserved_kv_bytes=1 << 20,
            available_bytes=1 << 30,
            issued_at_unix_ms=int(time.time() * 1000) - 10_000,
            ttl_ms=1,
        )
        assert record["qualified"] is True, record
        assert record["expires_at_unix_ms"] < int(time.time() * 1000)
        return record
    record = qualify_standby(
        source_identity=port_placement_identity(source, source_id),
        standby_identity=port_placement_identity(standby, STANDBY_ID),
        parity_challenge=challenge,
        reserved_kv_bytes=1 << 20,
        available_bytes=1 << 30,
        issued_at_unix_ms=int(time.time() * 1000) - 1_000,
        ttl_ms=3_600_000,
    )
    assert record["qualified"] is True, record
    return record


def _path_manifest(graph: Any, request_id: str) -> tuple[dict[str, Any], str]:
    path_id = f"a7-product-{request_id}"
    manifest = PathManifest(
        path_id=path_id,
        path_attempt=1,
        request_id=request_id,
        deployment_id=graph.deployment_id,
        deployment_epoch=graph.deployment_epoch,
        topology_version=graph.topology_version,
        manifest_digest=graph.manifest_digest,
        ordered_hops=tuple(
            PathHop(
                stage_id=stage.stage_id,
                placement_id=stage.placements[0].placement_id,
                reservation_id=f"reservation-{request_id}-{index}",
                reservation_expires_at=1e15,
                reservation_epoch=1,
            )
            for index, stage in enumerate(graph.stages)
        ),
        loopback_edge_id=graph.loopback_edges[0].edge_id,
    )
    document = path_manifest_to_dict(manifest)
    return document, _sha_json(document)


def _install_endpoint_learning_verifier(route: PhysicalLiveRoute) -> None:
    """Keep signature verification while learning local-only endpoint ids."""

    def verify(node_id: str, response: Mapping[str, Any], *, event: str, require_known_key: bool = True) -> dict[str, Any]:
        signed = response.get("result")
        if not isinstance(signed, Mapping):
            raise RuntimeError("node_response_missing_signed_result")
        observation = signed.get("observation")
        if not isinstance(observation, Mapping):
            raise RuntimeError("node_observation_missing")
        endpoint_id = observation.get("endpoint_id")
        if not isinstance(endpoint_id, str) or not endpoint_id:
            raise RuntimeError("node_endpoint_id_missing")
        if event == "configured":
            route._endpoints[node_id]["endpoint_id"] = endpoint_id
        verified = route._controller._verified_observation(
            response,
            event=event,
            peer=route._peers[node_id],
            process_id=route._identities[node_id]["process_id"],
            run_id=route._plan["run_id"],
            deployment_id=route._plan["deployment_id"],
            endpoint_id=endpoint_id,
            expected_verification_key=(
                route._verification_keys.get(node_id) if require_known_key else None
            ),
            signed_observation_sink=route._signed_observations,
        )
        subject = route._liveness_subjects.get(node_id)
        if subject is not None:
            from mycelium_live.liveness import ObservationSource

            route._liveness.observe_receipt(
                subject,
                observed_at_ms=int(time.monotonic() * 1_000),
                source=ObservationSource.APPLICATION_RECEIPT,
                signed=True,
            )
        return verified

    route._verify_observation = verify  # type: ignore[method-assign]


def _build_r7_staged_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    dtype_mismatch: bool = False,
    qualification_expired: bool = False,
) -> _RouteState:
    monkeypatch.setattr(
        gateway_contracts, "qualification_digest", lambda _qualification: _QUALIFICATION_DIGEST
    )
    root = Path(
        tempfile.mkdtemp(prefix="mycelium-a7-product-", dir="/tmp")
    ).resolve()
    root.chmod(0o700)
    deployment = prepare_physical_deployment(tmp_path / "deployment", runtime_backend="mlx")
    assignments = deployment.assignments
    source_runtime = dict(assignments[1]["runtime"])
    standby_runtime = {
        key: source_runtime[key] for key in ("backend", "dtype", "quantization")
    }
    if dtype_mismatch:
        standby_runtime["dtype"] = (
            "float32" if standby_runtime["dtype"] != "float32" else "float16"
        )
    standby = compile_standby_artifacts(
        manifest=deployment.manifest,
        route_plan=deployment.route_plan,
        stage_index=1,
        standby_node_id=_NODE_A,
        deployment_id=assignments[0]["deployment_id"],
        deployment_epoch=assignments[0]["deployment_epoch"],
        cache_root=deployment.root,
        runtime=standby_runtime,
        control_plane_binding=assignments[0]["control_plane_binding"],
        fetch_file=_LocalOnlyFetcher(deployment.root),
    )
    loaded = [
        load_assignment_stage(assignment, report, load_generation=7)
        for assignment, report in zip(assignments, deployment.artifact_reports, strict=True)
    ]
    standby_loaded = load_assignment_stage(
        standby["assignment"], standby["loader_report"], load_generation=7
    )
    base = build_execution_graph(
        assignments,
        [stage.proof for stage in loaded],
        link_scheme="iroh",
        runtime_scheme="iroh",
    )
    graph = extend_graph_with_standby(
        base,
        stage_index=1,
        placement_id=STANDBY_ID,
        node_id=_NODE_A,
        assignment_id=standby["assignment"]["assignment_id"],
        load_proof_digest=layer_load_proof_digest(json.loads(canonical_json(standby_loaded.proof))),
    )
    assert graph.stages[1].placements[0].lifecycle_state == "ACTIVE"
    assert graph.stages[1].placements[1].lifecycle_state == "STANDBY"

    graph_document = json.loads(json.dumps(asdict(graph)))
    states = build_physical_device_states(graph)
    state_document = json.loads(json.dumps({node: asdict(state) for node, state in states.items()}))

    roots = {node: root / node / "artifacts" for node in (_NODE_A, _NODE_B)}
    socket_roots = {node: root / node / "sockets" for node in (_NODE_A, _NODE_B)}
    for path in [*roots.values(), *socket_roots.values()]:
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)
    for index, node in enumerate((_NODE_A, _NODE_B)):
        shutil.copy2(NODE_SCRIPT, roots[node] / "physical_inference_node.py")
        _write(roots[node] / "model-manifest.json", deployment.manifest)
        _write(roots[node] / f"{node}-assignment.json", assignments[index])
        _write(roots[node] / f"{node}-stage-pack.json", deployment.stage_packs[index])
        (root / node / "identity").mkdir(parents=True, exist_ok=True)
        (root / node / "identity").chmod(0o700)
    _write(roots[_NODE_A] / "a7/standby-assignment.json", standby["assignment"])
    _write(roots[_NODE_A] / "a7/standby-stage-pack.json", standby["stage_pack"])
    secret = write_standby_secret(roots[_NODE_A] / "a7/standby.key")
    _write(
        roots[_NODE_A] / "a7/qualification.json",
        _qualification(graph, loaded, standby_loaded, dtype_mismatch=dtype_mismatch, qualification_expired=qualification_expired),
    )
    incarnation = "sha256:" + "1" * 64
    configs = node_configs(
        deployment_id=graph.deployment_id,
        graph=graph,
        stage_index=1,
        standby_placement_id=STANDBY_ID,
        standby_secret=secret,
        standby_incarnation=incarnation,
        standby_files={
            "assignment_file": "a7/standby-assignment.json",
            "manifest_file": "model-manifest.json",
            "stage_pack_file": "a7/standby-stage-pack.json",
            "secret_file": "a7/standby.key",
        },
        standby_load_generation=7,
        qualification_file="a7/qualification.json",
        ack_timeout_seconds=5.0,
        receipt_timeout_seconds=5.0,
    )
    assert set(configs) == {_NODE_A, _NODE_B}
    for node, config in configs.items():
        _write(roots[node] / "a7/node.json", config)

    run_id = "a7prod" + uuid.uuid4().hex[:24]
    host_id, boot_id = derive_local_run_scoped_identity(run_id)
    peers = tuple(
        PeerIdentity(
            node_id=node,
            ssh_target="local@example.com",
            host_id=host_id,
            boot_id=boot_id,
            staging_root=str(roots[node]),
            process_transport="local",
        )
        for node in (_NODE_A, _NODE_B)
    )
    transfer_manifest = {
        "protocol": "mycelium.controller_transfer_manifest.v1",
        "files": [
            {
                "path": "physical_inference_node.py",
                "size_bytes": NODE_SCRIPT.stat().st_size,
                "content_digest": _file_digest(NODE_SCRIPT),
            }
        ],
    }
    membership_snapshot = {
        "protocol": "mycelium.controller_membership_snapshot.v1",
        "deployment_id": graph.deployment_id,
        "assignment_offers": [],
    }
    expected = _reference_tokens(deployment)
    run_plan = {
        "protocol": "mycelium.controller_run_plan.v1",
        "run_id": run_id,
        "deployment_id": graph.deployment_id,
        "entry_node_id": _ENTRY_NODE_ID,
        "nodes": [
            {
                "node_id": node,
                "python_executable": sys.executable,
                "socket_root": str(socket_roots[node]),
                "sidecar_binary": str(SIDECAR_BINARY),
                "endpoint_secret_file": str(root / node / "identity" / "endpoint.key"),
                "configure": {
                    "assignment_file": f"{node}-assignment.json",
                    "manifest_file": "model-manifest.json",
                    "stage_pack_file": f"{node}-stage-pack.json",
                    "graph": graph_document,
                    "device_states": state_document,
                    "load_generation": 7,
                },
            }
            for node in (_NODE_A, _NODE_B)
        ],
        "request": _request("startup-a7-product"),
        "decode_count": _MAX_NEW - 1,
        "expected_token_ids": list(expected),
        "decode_mode": "stage_local_kv",
    }
    controller = QualificationController(
        mode="local",
        peers=peers,
        source_root=ROOT,
        transfer_manifest=transfer_manifest,
        membership_snapshot=membership_snapshot,
        now=time.time(),
        run_plan=run_plan,
    )
    session_factory = _A7SessionFactory(
        roots=roots, socket_roots=socket_roots, configs=configs, graph=graph
    )
    endpoints = {
        _NODE_A: {"endpoint_id": "pending-node-a", "membership_generation": 1},
        _NODE_B: {"endpoint_id": "pending-node-b", "membership_generation": 2},
    }
    from tests.live.local_token_authority import LocalTokenAuthority
    token_authority = LocalTokenAuthority(SimpleNamespace(
        root=root, node_ids=tuple(endpoints), endpoints_template=endpoints,
        run_plan=run_plan, session_factory=session_factory))
    session_factory.token_files = token_authority.files
    route = PhysicalLiveRoute(
        controller=controller,
        endpoints=endpoints,
        run_plan=run_plan,
        session_factory=session_factory,
        token_path_owner=token_authority.owner,
    )
    _install_endpoint_learning_verifier(route)
    return _RouteState(
        route=route,
        graph=graph,
        deployment=deployment,
        session_factory=session_factory,
        root=root,
        reference_tokens=expected,
        dtype_mismatch=dtype_mismatch,
        qualification_expired=qualification_expired,
    )


def _open_with_a7_authority(state: _RouteState) -> Any:
    """Open the route, then install the authority as the supervisor does after open.

    Detection reads the nodes' signed ``configured`` attestations, so nothing is
    installed before the nodes have configured.
    """

    owner = ColdRouteRestartOwner(  # type: ignore[arg-type]
        route_factory=lambda: None,
        open_and_qualify=lambda _route, _generation: None,
    )
    owner._apply_cached_configuration(state.route)
    assert state.route._peer_exit_successor_authority is None
    identity = state.route.open()
    owner._apply_cached_configuration(state.route)
    assert state.route._peer_exit_successor_authority is not None
    return identity


def _node_observation(
    route: PhysicalLiveRoute, node_id: str, command: str, payload: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    response = route._sessions[node_id].send(
        command_id=route._command_id(node_id, command),
        command=command,
        payload={} if payload is None else dict(payload),
    )
    return route._verify_observation(node_id, response, event=command)


def _a7_snapshot(route: PhysicalLiveRoute, request_id: str) -> dict[str, Any]:
    return _node_observation(route, _ENTRY_NODE_ID, "a7_snapshot", {"request_id": request_id})[
        "details"
    ]


def _wait_for_a7_acknowledged(route: PhysicalLiveRoute, request_id: str, *, acknowledged: int) -> dict[str, Any]:
    deadline = time.monotonic() + 15.0
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        latest = _a7_snapshot(route, request_id)
        request = latest.get("request") if isinstance(latest, Mapping) else None
        if isinstance(request, Mapping) and request.get("acknowledged", -1) >= acknowledged:
            return latest
        time.sleep(0.05)
    pytest.fail(f"A7 acknowledgement did not reach {acknowledged}: {latest!r}")


def _wait_for_a7_result(route: PhysicalLiveRoute, request_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 20.0
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        latest = _a7_snapshot(route, request_id)
        request = latest.get("request") if isinstance(latest, Mapping) else None
        result = request.get("result") if isinstance(request, Mapping) else None
        if isinstance(result, Mapping):
            return latest
        time.sleep(0.1)
    pytest.fail(f"A7 cutover result was not recorded: {latest!r}")


def _wait_for_process_exit(session: Any) -> None:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if session.returncode is not None:
            return
        time.sleep(0.02)
    pytest.fail("source process did not exit after SIGKILL")


def _product_identity(graph: Any, request_id: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    manifest, manifest_digest = _path_manifest(graph, request_id)
    route_identity = {
        "request_id": request_id,
        "request_attempt": 1,
        "path_id": manifest["path_id"],
        "path_attempt": manifest["path_attempt"],
        "path_manifest_digest": manifest_digest,
        "deployment_id": graph.deployment_id,
        "deployment_epoch": graph.deployment_epoch,
        "topology_generation": graph.topology_version,
    }
    command_identity = {
        "deployment_id": graph.deployment_id,
        "deployment_epoch": graph.deployment_epoch,
        "qualification_digest": _QUALIFICATION_DIGEST,
        "request_id": request_id,
        "request_attempt": 1,
        "path_id": manifest["path_id"],
        "path_attempt": manifest["path_attempt"],
        "path_digest": manifest_digest,
        "topology_generation": graph.topology_version,
        "command_id": f"owner-command-{request_id}",
        "publisher_generation": 1,
        "absolute_deadline_ms": int(time.monotonic() * 1_000) + 600_000,
        "cancellation_generation": 0,
    }
    return manifest, route_identity, command_identity


def _published_receipts_for_request(receipts: list[dict[str, Any]], request_id: str) -> list[dict[str, Any]]:
    return [receipt for receipt in receipts if receipt.get("request_id") == request_id]


def _scoped_node_exit_incidents(route: PhysicalLiveRoute, request_id: str) -> list[dict[str, Any]]:
    return [
        dict(item)
        for item in route.a4_scoped_runtime_incidents()
        if item.get("request_id") == request_id
        and item.get("subject_id") == _SOURCE_NODE_ID
        and item.get("reason") == "node_process_exited"
    ]


def _router_port_has_a6_incident(route: PhysicalLiveRoute, request_id: str | None) -> bool:
    port = object.__new__(LiveRouterPort)
    port._route = route
    request = None if request_id is None else SimpleNamespace(request_id=request_id)
    return LiveRouterPort._route_has_scoped_incident_for(port, request)  # type: ignore[arg-type]


def _run_infer_with_source_kill(state: _RouteState, request_id: str, *, expect_success: bool, kill_after: int = 2):
    manifest, route_identity, command_identity = _product_identity(state.graph, request_id)
    receipts: list[dict[str, Any]] = []
    sink = _KillAfterCommittedTokens(state, request_id, kill_after=kill_after)
    cancel_calls: list[dict[str, Any]] = []
    real_cancel = state.route.cancel_request

    def cancel_request(request: str, *, deadline_monotonic_s: float | None = None) -> bool:
        cancel_calls.append(
            {
                "request_id": request,
                "deadline_monotonic_s": deadline_monotonic_s,
                "emitted_tokens": len(sink.tokens),
            }
        )
        return real_cancel(request, deadline_monotonic_s=deadline_monotonic_s)

    state.route.cancel_request = cancel_request  # type: ignore[method-assign]
    kwargs = dict(
        max_new_tokens=_MAX_NEW,
        request_id=request_id,
        sink=sink,
        route_identity=route_identity,
        locked_path_manifest=manifest,
        command_identity=command_identity,
        authorize_cleanup=lambda deadline: deadline,
        publish_cleanup_receipt=lambda receipt: receipts.append(dict(receipt)),
    )
    if expect_success:
        result = state.route.infer(_PROMPT, **kwargs)
        return SimpleNamespace(
            result=result,
            sink=sink,
            receipts=receipts,
            cancel_calls=cancel_calls,
            exception=None,
        )
    with pytest.raises(Exception) as raised:
        state.route.infer(_PROMPT, **kwargs)
    return SimpleNamespace(
        result=None,
        sink=sink,
        receipts=receipts,
        cancel_calls=cancel_calls,
        exception=raised.value,
    )


@pytest.mark.parametrize(
    "kill_after",
    [
        pytest.param(2, id="mid_request"),
        # The source is lost while the final decode step is outstanding: the
        # surviving stage has already executed the terminal hop, and its KV
        # must still be there for the fenced rekey.
        pytest.param(_MAX_NEW - 1, id="final_decode_step"),
    ],
)
def test_two_node_physical_live_route_cuts_over_after_source_kill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kill_after: int
) -> None:
    state = _build_r7_staged_route(tmp_path, monkeypatch, dtype_mismatch=False)
    try:
        identity = _open_with_a7_authority(state)
        assert len(set(identity.endpoint_ids)) == 2
        state.route._deployment_qualification = SimpleNamespace(route_ready=True)
        request_id = "a7ok" + uuid.uuid4().hex[:24]

        run = _run_infer_with_source_kill(state, request_id, expect_success=True, kill_after=kill_after)

        assert run.result.token_ids == state.reference_tokens
        assert tuple(token for _index, token in run.sink.tokens) == state.reference_tokens
        assert run.sink.kill_at_monotonic_s is not None
        final = _wait_for_a7_result(state.route, request_id)
        outcome = final["request"]["result"]
        assert outcome["outcome"] == "fenced_kv_successor", outcome
        assert final["standby_runtime"]["prefill_operation_count"] == 0
        assert final["standby_runtime"]["decode_operation_count"] == _MAX_NEW - kill_after

        scoped = _scoped_node_exit_incidents(state.route, request_id)
        assert len(scoped) == 1
        claim_delta_ms = scoped[0]["observed_at_monotonic_ms"] - int(
            run.sink.kill_at_monotonic_s * 1_000
        )
        assert -50 <= claim_delta_ms < 1_000
        assert state.route._bound_participants(request_id, frozenset({_NODE_A, _NODE_B})) == frozenset({_NODE_A})

        published = _published_receipts_for_request(run.receipts, request_id)
        assert published, "completion cleanup receipt was not published"
        assert published[-1]["node_ids"] == [_NODE_A]
        # Completion cleanup uses cancel_request after all tokens are emitted;
        # the A7 source-loss path must not trigger the F1/failure cancellation.
        assert [call for call in run.cancel_calls if call["emitted_tokens"] < _MAX_NEW] == []
        assert not any(
            incident.get("request_id") == request_id
            and incident.get("state") == "request_failed_closed"
            for incident in state.route._incidents
        )
        # A completed terminal has no pending router-port request to recover;
        # the A7 process-exit incident above is evidence for the cutover, not A6.
        assert _router_port_has_a6_incident(state.route, None) is False
    finally:
        state.close()


def test_standby_incompatible_dtype_routes_to_a6_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _build_r7_staged_route(tmp_path, monkeypatch, dtype_mismatch=False, qualification_expired=True)
    try:
        identity = _open_with_a7_authority(state)
        assert len(set(identity.endpoint_ids)) == 2
        state.route._deployment_qualification = SimpleNamespace(route_ready=True)
        request_id = "a7bad" + uuid.uuid4().hex[:24]

        run = _run_infer_with_source_kill(state, request_id, expect_success=False)

        assert run.sink.killed is True
        final = _wait_for_a7_result(state.route, request_id)
        outcome = final["request"]["result"]
        assert outcome["outcome"] == "full_context_replay", outcome
        assert outcome["reason"] in {"compatibility_incompatible", "qualification_not_current"}
        published = _published_receipts_for_request(run.receipts, request_id)
        assert published, "failed request cleanup receipt was not published"
        assert published[-1]["node_ids"] == [_NODE_A]
        scoped = _scoped_node_exit_incidents(state.route, request_id)
        assert len(scoped) == 1
        assert _router_port_has_a6_incident(state.route, request_id) is True
        assert any(
            incident.get("request_id") == request_id
            and incident.get("state") == "request_failed_closed"
            for incident in state.route._incidents
        )
    finally:
        state.close()
