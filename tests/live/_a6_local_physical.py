# SPDX-License-Identifier: AGPL-3.0-only
"""Local multi-process physical route fixture for A6 recovery/restart tests.

Builds a real ``PhysicalLiveRoute`` over real ``physical_inference_node.py``
processes speaking native iroh (local-only), using the tiny offline MLX
deployment from ``prepare_physical_deployment``. Two graph shapes are offered:

* ``single``: one path, stage-000 on ``node-a`` and stage-001 on ``node-b``
  (the run-day topology that has no disjoint successor);
* ``full_replica``: two complete disjoint single-stage paths, the whole model
  on ``node-a`` and again on ``node-b`` (complete-model replica tracks), so a
  successor survives the loss of every source placement. This is the only
  disjoint two-path shape two physically distinct hosts can carry: the
  production controller refuses two node processes on one host. With
  ``replica_count=3`` a third complete replica runs on ``node-c``, as the run-day
  A6 plan places the model on node-0, node-2 and node-3.

This is a controlled local session: never physical or browser qualification.
"""
from __future__ import annotations

import copy
import hashlib
import json
import shutil
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

from layer_assignment import assignment_id_for
from mycelium_live.route import PhysicalLiveRoute, _ConcurrentNodeSession
from mycelium_physical_runner.remote_probe import _boot_identity, derive_run_scoped_identity
from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_qualification.physical_deployment import (
    build_execution_graph,
    build_physical_device_states,
    build_replicated_execution_graph,
    prepare_physical_deployment,
)
from mycelium_router.decoding import quantized_greedy_token_id
from physical_inference_qualification import PeerIdentity, QualificationController
from runtime_loader import execute_loaded_stage, load_assignment_stage
from stage_pack import artifact_report_for_loader, compile_stage_pack, verify_stage_pack
from two_process_runtime_qualification import _LocalOnlyFetcher
from weight_provisioning import provision_assignment
from tests.physical_qualification.test_node_service import NODE_SCRIPT, ROOT, SIDECAR_BINARY

PROMPT = (1, 2, 3)
MAX_NEW = 5
LOAD_GENERATION = 7


def _write(path: Path, document: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(document))


def _file_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


#: Each local node process measures a distinct simulated host, so the
#: production physical-distinctness checks (controller and live qualification)
#: run unchanged against one machine. Only the host *measurement* is replaced.
#: An optional per-stage-execution delay paces local decode so a controlled
#: fault lands while a node command is in flight, as on a real fleet.
_LAUNCHER = """\
import runpy, sys, time
root, host, delay, script = sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4]
sys.path.insert(0, root)
import mycelium_physical_runner.remote_probe as probe
probe._host_identity = lambda: host
if delay > 0:
    import mycelium_router.mlx_runtime as mlx_runtime
    bound = mlx_runtime.MLXRuntimePort._execute_bound
    def paced(self, item):
        time.sleep(delay)
        return bound(self, item)
    mlx_runtime.MLXRuntimePort._execute_bound = paced
sys.argv = [script, *sys.argv[5:]]
runpy.run_path(script, run_name="__main__")
"""


def simulated_host(node_id: str) -> str:
    return f"local-simulated-host-{node_id}"


def peer_run_scoped_identity(run_id: str, node_id: str) -> tuple[str, str]:
    host = simulated_host(node_id)
    return derive_run_scoped_identity(
        run_id=run_id, observed_host_id=host, observed_boot_id=_boot_identity(host))


class LocalSessionFactory:
    """Launch real node processes with local-only iroh; remember sessions."""

    def __init__(
        self, roots: Mapping[str, Path], socket_roots: Mapping[str, Path], launcher: Path
    ) -> None:
        self._roots = dict(roots)
        self._socket_roots = dict(socket_roots)
        self._launcher = launcher
        self.step_delay_s = 0.0
        self.sessions: dict[str, Any] = {}
        self.launches: list[str] = []

    def __call__(self, *, argv, node_id, run_id, deployment_id, timeout_seconds):
        _ = argv
        command = (
            sys.executable, "-B", str(self._launcher), str(ROOT), simulated_host(node_id),
            repr(float(self.step_delay_s)), str(NODE_SCRIPT),
            "--run-id", run_id,
            "--deployment-id", deployment_id,
            "--node-id", node_id,
            "--artifact-root", str(self._roots[node_id]),
            "--socket-root", str(self._socket_roots[node_id]),
            "--sidecar-binary", str(SIDECAR_BINARY),
            "--sidecar-local-only",
            "--command-timeout", "900",
            "--decode-mode", "stage_local_kv",
        )
        session = _ConcurrentNodeSession(
            argv=command, node_id=node_id, run_id=run_id,
            deployment_id=deployment_id, timeout_seconds=timeout_seconds,
        )
        self.sessions[node_id] = session
        self.launches.append(node_id)
        return session


def install_endpoint_learning_verifier(route: PhysicalLiveRoute) -> None:
    """Keep signature verification while learning local-only endpoint ids."""

    def verify(node_id, response, *, event, require_known_key=True):
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
            response, event=event, peer=route._peers[node_id],
            process_id=route._identities[node_id]["process_id"],
            run_id=route._plan["run_id"], deployment_id=route._plan["deployment_id"],
            endpoint_id=endpoint_id,
            expected_verification_key=(
                route._verification_keys.get(node_id) if require_known_key else None),
            signed_observation_sink=route._signed_observations,
        )
        subject = route._liveness_subjects.get(node_id)
        if subject is not None:
            from mycelium_live.liveness import ObservationSource

            route._liveness.observe_receipt(
                subject, observed_at_ms=int(time.monotonic() * 1_000),
                source=ObservationSource.APPLICATION_RECEIPT, signed=True,
            )
        return verified

    route._verify_observation = verify  # type: ignore[method-assign]


def _reference_tokens(deployment: Any) -> tuple[int, ...]:
    reference = load_assignment_stage(
        deployment.reference_assignment, deployment.reference_report,
        load_generation=LOAD_GENERATION,
    )
    context = list(PROMPT)
    expected: list[int] = []
    for _ in range(MAX_NEW):
        logits = execute_loaded_stage(
            reference, token_ids=mx.array((tuple(context),), dtype=mx.uint32))
        mx.eval(logits)
        token = quantized_greedy_token_id(logits[0, -1, :].tolist())
        expected.append(token)
        context.append(token)
    return tuple(expected)


def full_model_replica(deployment: Any, node_id: str):
    """The complete model bound to ``node_id``: assignment, stage pack, loader report."""

    assignment = copy.deepcopy(dict(deployment.reference_assignment))
    assignment["node_id"] = node_id
    assignment["assignment_id"] = assignment_id_for(assignment)
    report = provision_assignment(
        assignment, fetch_file=_LocalOnlyFetcher(deployment.root), local_files_only=True)
    pack = compile_stage_pack(assignment, deployment.manifest, report)
    verification = verify_stage_pack(pack, assignment=assignment, manifest=deployment.manifest)
    loader_report = artifact_report_for_loader(
        pack, verification, assignment=assignment, manifest=deployment.manifest)
    return assignment, pack, loader_report


@dataclass
class LocalPhysical:
    root: Path
    graph: Any
    graph_document: dict[str, Any]
    run_plan: dict[str, Any]
    peers: tuple[Any, ...]
    node_ids: tuple[str, ...]
    reference_tokens: tuple[int, ...]
    session_factory: LocalSessionFactory
    endpoints_template: dict[str, dict[str, Any]]

    def new_route(self) -> PhysicalLiveRoute:
        controller = QualificationController(
            mode="local",
            peers=self.peers,
            source_root=ROOT,
            transfer_manifest={
                "protocol": "mycelium.controller_transfer_manifest.v1",
                "files": [{
                    "path": "physical_inference_node.py",
                    "size_bytes": NODE_SCRIPT.stat().st_size,
                    "content_digest": _file_digest(NODE_SCRIPT),
                }],
            },
            membership_snapshot={
                "protocol": "mycelium.controller_membership_snapshot.v1",
                "deployment_id": self.graph.deployment_id,
                "assignment_offers": [],
            },
            now=time.time(),
            run_plan=self.run_plan,
        )
        route = PhysicalLiveRoute(
            controller=controller,
            endpoints=copy.deepcopy(self.endpoints_template),
            run_plan=self.run_plan,
            session_factory=self.session_factory,
        )
        install_endpoint_learning_verifier(route)
        return route

    def close(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def build_local_physical(
    tmp_path: Path, *, shape: str = "single", replica_count: int = 2
) -> LocalPhysical:
    if shape not in {"single", "full_replica"}:
        raise ValueError(shape)
    if replica_count not in {2, 3} or (shape == "single" and replica_count != 2):
        raise ValueError(replica_count)
    root = Path(tempfile.mkdtemp(prefix="mycelium-a6-local-", dir="/tmp")).resolve()
    root.chmod(0o700)
    deployment = prepare_physical_deployment(tmp_path / "deployment", runtime_backend="mlx")
    node_ids = ("node-a", "node-b", "node-c")[:replica_count]
    if shape == "single":
        assignments = list(deployment.assignments)
        loaded = [
            load_assignment_stage(assignment, report, load_generation=LOAD_GENERATION)
            for assignment, report in zip(assignments, deployment.artifact_reports, strict=True)
        ]
        graph = build_execution_graph(
            assignments, [stage.proof for stage in loaded],
            link_scheme="iroh", runtime_scheme="iroh",
        )
        by_node = {
            "node-a": (assignments[0], deployment.stage_packs[0]),
            "node-b": (assignments[1], deployment.stage_packs[1]),
        }
    else:
        replicas = {node: full_model_replica(deployment, node) for node in node_ids}
        proofs = {
            node: load_assignment_stage(
                assignment, loader_report, load_generation=LOAD_GENERATION).proof
            for node, (assignment, _pack, loader_report) in replicas.items()
        }
        placement_ids = {node: f"placement-{index:03d}" for index, node in enumerate(node_ids)}
        layers = deployment.reference_assignment["range"]["end_layer_exclusive"]
        route_plan = {
            "protocol": "mycelium.route_plan.v2",
            "placements": [
                {
                    "placement_id": placement_ids[node],
                    "replica_group_id": "full-model",
                    "node_id": node,
                    "layer_range": {"start": 0, "end": layers},
                    "primary": node == "node-a",
                }
                for node in node_ids
            ],
            "forward_edges": [],
            "loopbacks": [],
        }
        graph = build_replicated_execution_graph(
            {placement_ids[node]: replicas[node][0] for node in node_ids},
            {placement_ids[node]: proofs[node] for node in node_ids},
            route_plan,
            link_scheme="iroh",
            runtime_scheme="iroh",
        )
        by_node = {node: (replicas[node][0], replicas[node][1]) for node in node_ids}
    graph_document = json.loads(json.dumps(asdict(graph)))
    states = build_physical_device_states(graph)
    state_document = json.loads(json.dumps({n: asdict(s) for n, s in states.items()}))
    roots = {node: root / node / "artifacts" for node in node_ids}
    socket_roots = {node: root / node / "sockets" for node in node_ids}
    for path in [*roots.values(), *socket_roots.values()]:
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)
    for node in node_ids:
        assignment, pack = by_node[node]
        shutil.copy2(NODE_SCRIPT, roots[node] / "physical_inference_node.py")
        _write(roots[node] / "model-manifest.json", deployment.manifest)
        _write(roots[node] / f"{node}-assignment.json", assignment)
        _write(roots[node] / f"{node}-stage-pack.json", pack)
        (root / node / "identity").mkdir(parents=True, exist_ok=True)
        (root / node / "identity").chmod(0o700)
    run_id = "a6local" + uuid.uuid4().hex[:20]
    peers = tuple(
        PeerIdentity(
            node_id=node, ssh_target="local@example.com",
            host_id=peer_run_scoped_identity(run_id, node)[0],
            boot_id=peer_run_scoped_identity(run_id, node)[1],
            staging_root=str(roots[node]), process_transport="local",
        )
        for node in node_ids
    )
    launcher = root / "node_launcher.py"
    launcher.write_text(_LAUNCHER, encoding="utf-8")
    expected = _reference_tokens(deployment)
    run_plan = {
        "protocol": "mycelium.controller_run_plan.v1",
        "run_id": run_id,
        "deployment_id": graph.deployment_id,
        "entry_node_id": "node-a",
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
                    "load_generation": LOAD_GENERATION,
                },
            }
            for node in node_ids
        ],
        "request": {
            "request_id": "startup-a6-local",
            "prompt_token_ids": list(PROMPT),
            "max_new_tokens": MAX_NEW,
            "expected_new_tokens": MAX_NEW,
            "qos_class": "interactive",
            "admitted_at": 0.0,
            "target_ttft_ms": 1_000.0,
            "target_tpot_ms": 1_000.0,
            "target_tokens_per_second": 1.0,
            "sampling_seed": 17,
            "generation_config_digest": "sha256:" + "a" * 64,
        },
        "decode_count": MAX_NEW - 1,
        "expected_token_ids": list(expected),
        "decode_mode": "stage_local_kv",
    }
    endpoints = {
        node: {"endpoint_id": f"pending-{node}", "membership_generation": index + 1}
        for index, node in enumerate(node_ids)
    }
    return LocalPhysical(
        root=root, graph=graph, graph_document=graph_document, run_plan=run_plan,
        peers=peers, node_ids=node_ids, reference_tokens=expected,
        session_factory=LocalSessionFactory(roots, socket_roots, launcher),
        endpoints_template=endpoints,
    )


def synthetic_open_and_qualify(route: PhysicalLiveRoute, _generation: int):
    identity = route.open()
    qualification = SimpleNamespace(route_ready=True)
    route._deployment_qualification = qualification
    return identity, qualification
