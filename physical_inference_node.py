#!/usr/bin/env python3.14
"""Command-bounded physical Mycelium Router/runtime node service.

Control uses canonical JSON lines on stdin/stdout. Diagnostics and native
sidecar logs remain on stderr. Every command is bound to one immutable run,
deployment, and node identity. Private signing and sidecar key material never
enters a serialization-facing object.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import fields, is_dataclass, replace
import json
import os
from pathlib import Path, PurePosixPath
import platform
import select
import signal
import stat
import subprocess
import sys
import time
import traceback
from typing import Any, Mapping
import uuid

from mycelium_iroh_sidecar import SidecarClient
from mycelium_qualification.evidence import canonical_json_bytes, canonical_json_loads
from mycelium_qualification.signing import generate_ed25519_signer
from mycelium_router.contracts import (
    DeviceState,
    ExecutionGraph,
    LayerRange,
    Placement,
    PlacementEdge,
    RequestContext,
    RouterConfig,
    Stage,
    StageCost,
)
from mycelium_router.live_ports import (
    PublishedDeviceStateProvider,
    PublishedTopologyProvider,
)
from mycelium_router.layer_builder import layer_load_proof_digest
from mycelium_router.router import Router
from mycelium_router.transports.iroh import IrohTransport, PeerBinding
from mycelium_router.validation import validate_execution_graph
from physical_sqlite_capacity import SQLiteQualificationCapacityPort
from runtime_loader import canonical_json, load_assignment_stage
from stage_pack import artifact_report_for_loader, verify_stage_pack

NODE_CONTROL_PROTOCOL = "mycelium.physical_node_control.v1"
NODE_OBSERVATION_PROTOCOL = "mycelium.physical_node_observation.v1"
MAX_COMMAND_BYTES = 4 * 1024 * 1024
_COMMAND_FIELDS = frozenset(
    {"protocol", "command_id", "run_id", "deployment_id", "command", "payload"}
)


class NodeCommandError(RuntimeError):
    """Fail-closed node command error carrying a stable public code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise NodeCommandError(code)


def _validated_endpoint_secret_file(value: Path | None) -> Path | None:
    if value is None:
        return None
    _require(isinstance(value, Path), "invalid_endpoint_secret_file")
    lexical = os.fspath(value)
    _require(
        bool(lexical)
        and value.is_absolute()
        and not any(
            ord(character) <= 0x1F or 0x7F <= ord(character) <= 0x9F
            for character in lexical
        ),
        "invalid_endpoint_secret_file",
    )
    return value


def _exact_fields(document: Any, expected: set[str] | frozenset[str], code: str) -> dict[str, Any]:
    _require(isinstance(document, dict) and set(document) == set(expected), code)
    return document


def _plain_json(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _plain_json(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain_json(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise NodeCommandError("nonserializable_observation")


def execution_graph_from_document(document: Any) -> ExecutionGraph:
    root_fields = {
        "deployment_id",
        "deployment_epoch",
        "topology_version",
        "model_id",
        "resolved_commit",
        "manifest_digest",
        "entry_stage_id",
        "final_stage_id",
        "hidden_size",
        "activation_bytes",
        "token_envelope_bytes",
        "stages",
        "edges",
        "loopback_edges",
        "protocol",
    }
    root = _exact_fields(document, root_fields, "invalid_execution_graph_fields")
    _require(root["protocol"] == "mycelium.execution_graph.v1", "invalid_execution_graph_protocol")
    _require(isinstance(root["stages"], list), "invalid_execution_graph_stages")
    stages: list[Stage] = []
    for stage_document in root["stages"]:
        stage_data = _exact_fields(
            stage_document,
            {"stage_id", "layer_range", "component_roles", "stage_cost", "placements"},
            "invalid_stage_fields",
        )
        layer = _exact_fields(
            stage_data["layer_range"],
            {"start_layer", "end_layer_exclusive", "layer_count"},
            "invalid_layer_range_fields",
        )
        cost = _exact_fields(
            stage_data["stage_cost"],
            {
                "prefill_work_units_per_prompt_token",
                "decode_work_units_per_token",
                "kv_bytes_per_context_token",
            },
            "invalid_stage_cost_fields",
        )
        _require(isinstance(stage_data["component_roles"], list), "invalid_component_roles")
        _require(isinstance(stage_data["placements"], list), "invalid_placements")
        placements: list[Placement] = []
        for placement_document in stage_data["placements"]:
            placement_data = _exact_fields(
                placement_document,
                {
                    "placement_id",
                    "node_id",
                    "replica_group_id",
                    "assignment_id",
                    "stage_signature",
                    "load_proof_digest",
                    "runtime_backend",
                    "runtime_endpoint",
                    "lifecycle_state",
                },
                "invalid_placement_fields",
            )
            placements.append(Placement(**placement_data))
        stages.append(
            Stage(
                stage_id=stage_data["stage_id"],
                layer_range=LayerRange(**layer),
                component_roles=tuple(stage_data["component_roles"]),
                stage_cost=StageCost(**cost),
                placements=tuple(placements),
            )
        )

    def parse_edges(value: Any, code: str) -> tuple[PlacementEdge, ...]:
        _require(isinstance(value, list), code)
        parsed: list[PlacementEdge] = []
        for edge_document in value:
            edge = _exact_fields(
                edge_document,
                {"edge_id", "from_placement_id", "to_placement_id", "link_id"},
                "invalid_edge_fields",
            )
            parsed.append(PlacementEdge(**edge))
        return tuple(parsed)

    graph = ExecutionGraph(
        deployment_id=root["deployment_id"],
        deployment_epoch=root["deployment_epoch"],
        topology_version=root["topology_version"],
        model_id=root["model_id"],
        resolved_commit=root["resolved_commit"],
        manifest_digest=root["manifest_digest"],
        entry_stage_id=root["entry_stage_id"],
        final_stage_id=root["final_stage_id"],
        hidden_size=root["hidden_size"],
        activation_bytes=root["activation_bytes"],
        token_envelope_bytes=root["token_envelope_bytes"],
        stages=tuple(stages),
        edges=parse_edges(root["edges"], "invalid_edges"),
        loopback_edges=parse_edges(root["loopback_edges"], "invalid_loopback_edges"),
        protocol=root["protocol"],
    )
    try:
        return validate_execution_graph(graph)
    except (TypeError, ValueError, AttributeError) as exc:
        raise NodeCommandError("invalid_execution_graph") from exc


def device_states_from_document(document: Any) -> dict[str, DeviceState]:
    _require(isinstance(document, dict) and bool(document), "invalid_device_states")
    expected = {
        "node_id",
        "state_seq",
        "last_updated",
        "availability",
        "compute_units_per_second",
        "free_compute_fraction",
        "available_kv_bytes",
        "pending_hop_queue_depth",
        "neighbor_rtt_ms",
        "neighbor_bandwidth_bytes_per_second",
    }
    states: dict[str, DeviceState] = {}
    now = time.monotonic()
    for node_id, state_document in document.items():
        state_data = _exact_fields(state_document, expected, "invalid_device_state_fields")
        _require(state_data["node_id"] == node_id, "device_state_key_mismatch")
        _require(isinstance(state_data["neighbor_rtt_ms"], dict), "invalid_neighbor_rtt")
        _require(
            isinstance(state_data["neighbor_bandwidth_bytes_per_second"], dict),
            "invalid_neighbor_bandwidth",
        )
        state = DeviceState(**state_data)
        states[node_id] = replace(state, last_updated=now)
    return states


class _UuidSource:
    def new(self, namespace: str) -> str:
        _require(isinstance(namespace, str) and bool(namespace), "invalid_id_namespace")
        return f"{namespace}-{uuid.uuid4()}"


class _MonotonicClock:
    def now(self) -> float:
        return time.monotonic()


class _CaptureSink:
    def __init__(self) -> None:
        self.token_ids: list[int] = []
        self.token_indexes: list[int] = []

    def emit(self, token_index: int, token_id: int) -> None:
        self.token_indexes.append(token_index)
        self.token_ids.append(token_id)

    def snapshot(self) -> dict[str, Any]:
        return {
            "token_indexes": list(self.token_indexes),
            "token_ids": list(self.token_ids),
        }


class NativeSidecarProcess:
    """Launch native sidecar with bootstrap bytes inherited through a pipe only."""

    def __init__(
        self,
        *,
        binary: Path,
        socket_root: Path,
        local_only: bool,
        queue_capacity: int,
        startup_timeout: float,
        endpoint_secret_file: Path | None = None,
    ) -> None:
        self.binary = binary
        self.socket_root = socket_root
        self.local_only = local_only
        self.queue_capacity = queue_capacity
        self.startup_timeout = startup_timeout
        self.endpoint_secret_file = _validated_endpoint_secret_file(
            endpoint_secret_file
        )
        self.socket_path = socket_root / "i.sock"
        self._bootstrap_material: bytes | None = None
        self.process: subprocess.Popen[str] | None = None
        self.ready: dict[str, Any] | None = None

    @property
    def bootstrap_material(self) -> bytes:
        _require(self._bootstrap_material is not None, "sidecar_not_started")
        return self._bootstrap_material

    def _argv(self, bootstrap_fd: int) -> list[str]:
        command = [
            str(self.binary),
            "--uds",
            str(self.socket_path),
            "--bootstrap-fd",
            str(bootstrap_fd),
            "--queue-capacity",
            str(self.queue_capacity),
        ]
        if self.local_only:
            command.append("--local-only")
        if self.endpoint_secret_file is not None:
            command.extend(
                ["--endpoint-secret-file", str(self.endpoint_secret_file)]
            )
        return command

    def start(self) -> dict[str, Any]:
        _require(self.process is None, "sidecar_already_started")
        _require(self.binary.is_file() and os.access(self.binary, os.X_OK), "sidecar_binary_unavailable")
        self.socket_root.mkdir(parents=True, exist_ok=False)
        _require(len(os.fsencode(self.socket_path)) < 100, "sidecar_socket_path_too_long")
        material = os.urandom(32)
        read_fd, write_fd = os.pipe()
        try:
            try:
                os.write(write_fd, material)
            finally:
                os.close(write_fd)
            command = self._argv(read_fd)
            try:
                process = subprocess.Popen(
                    command,
                    pass_fds=(read_fd,),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                )
            finally:
                os.close(read_fd)
        except BaseException:
            try:
                os.close(read_fd)
            except OSError:
                pass
            raise
        self.process = process
        try:
            assert process.stdout is not None
            readable, _, _ = select.select([process.stdout], [], [], self.startup_timeout)
            _require(bool(readable), "sidecar_start_timeout")
            line = process.stdout.readline()
            _require(bool(line), "sidecar_exited_before_ready")
            ready = json.loads(line)
            _require(
                isinstance(ready, dict)
                and ready.get("event") == "ready"
                and ready.get("alpn") == "mycelium.iroh.sidecar.v1"
                and isinstance(ready.get("endpoint_id"), str)
                and isinstance(ready.get("endpoint_addr"), dict)
                and ready["endpoint_addr"].get("id") == ready["endpoint_id"],
                "invalid_sidecar_ready_record",
            )
            client = SidecarClient(self.socket_path, material, timeout=self.startup_timeout)
            try:
                client.connect()
                _require(client.endpoint_id == ready["endpoint_id"], "sidecar_endpoint_mismatch")
                client.ping()
            finally:
                client.close()
            self._bootstrap_material = material
            self.ready = ready
            return ready
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        process = self.process
        if process is not None:
            try:
                if process.poll() is None:
                    process.send_signal(signal.SIGINT)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            pass
                for stream in (process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        try:
                            stream.close()
                        except OSError:
                            pass
            finally:
                self.process = None
                self._bootstrap_material = None
        else:
            self._bootstrap_material = None


class PhysicalNodeService:
    def __init__(
        self,
        *,
        run_id: str,
        deployment_id: str,
        node_id: str,
        artifact_root: Path,
        socket_root: Path,
        sidecar_binary: Path,
        sidecar_local_only: bool,
        command_timeout: float,
        endpoint_secret_file: Path | None = None,
    ) -> None:
        for value, code in (
            (run_id, "invalid_run_id"),
            (deployment_id, "invalid_deployment_id"),
            (node_id, "invalid_node_id"),
        ):
            _require(isinstance(value, str) and bool(value) and value == value.strip(), code)
        _require(command_timeout > 0, "invalid_command_timeout")
        self.run_id = run_id
        self.deployment_id = deployment_id
        self.node_id = node_id
        self.artifact_root = artifact_root.resolve(strict=True)
        self.socket_root = socket_root
        self.sidecar_binary = sidecar_binary.resolve(strict=False)
        self.sidecar_local_only = sidecar_local_only
        self.command_timeout = command_timeout
        self.endpoint_secret_file = _validated_endpoint_secret_file(
            endpoint_secret_file
        )
        self.host_id = platform.node()
        self.process_id = os.getpid()
        self.state = "NEW"
        self.stop_requested = False
        self.graph: ExecutionGraph | None = None
        self.topology: PublishedTopologyProvider | None = None
        self.device_states: PublishedDeviceStateProvider | None = None
        self.capacity: SQLiteQualificationCapacityPort | None = None
        self.runtime: Any = None
        self.sidecar: NativeSidecarProcess | None = None
        self.transport: IrohTransport | None = None
        self.router: Router | None = None
        self.signer: Any | None = None
        self.endpoint_id: str | None = None
        self.endpoint_addr: dict[str, Any] | None = None
        self.peer_generation = 0
        self._ids = _UuidSource()
        self._clock = _MonotonicClock()
        self._sinks: dict[str, _CaptureSink] = {}

    def _safe_document(self, relative_path: Any, code: str) -> dict[str, Any]:
        _require(
            isinstance(relative_path, str)
            and bool(relative_path)
            and len(relative_path) <= 1024,
            code,
        )
        relative = PurePosixPath(relative_path)
        _require(
            not relative.is_absolute()
            and str(relative) == relative_path
            and 0 < len(relative.parts) <= 16
            and all(part not in {"", ".", ".."} for part in relative.parts),
            code,
        )
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        cloexec = getattr(os, "O_CLOEXEC", 0)
        directory = getattr(os, "O_DIRECTORY", 0)
        root_fd: int | None = None
        current_fd: int | None = None
        document_fd: int | None = None

        def fingerprint(metadata: os.stat_result) -> tuple[int, ...]:
            return (
                metadata.st_mode,
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
                metadata.st_nlink,
            )

        try:
            root_before = os.stat(self.artifact_root, follow_symlinks=False)
            root_fd = os.open(
                self.artifact_root,
                os.O_RDONLY | directory | nofollow | cloexec,
            )
            current_fd = root_fd
            opened_root = os.fstat(root_fd)
            _require(
                stat.S_ISDIR(opened_root.st_mode)
                and fingerprint(root_before) == fingerprint(opened_root),
                code,
            )
            for part in relative.parts[:-1]:
                child_fd = os.open(
                    part,
                    os.O_RDONLY | directory | nofollow | cloexec,
                    dir_fd=current_fd,
                )
                if current_fd != root_fd:
                    os.close(current_fd)
                current_fd = child_fd
            document_fd = os.open(
                relative.parts[-1],
                os.O_RDONLY | nofollow | cloexec,
                dir_fd=current_fd,
            )
            before = os.fstat(document_fd)
            _require(
                stat.S_ISREG(before.st_mode)
                and before.st_nlink == 1
                and 0 < before.st_size <= MAX_COMMAND_BYTES,
                code,
            )
            chunks: list[bytes] = []
            remaining = before.st_size
            while remaining:
                chunk = os.read(document_fd, min(remaining, 1024 * 1024))
                _require(bool(chunk), code)
                chunks.append(chunk)
                remaining -= len(chunk)
            _require(fingerprint(os.fstat(document_fd)) == fingerprint(before), code)
            root_after = os.stat(self.artifact_root, follow_symlinks=False)
            _require(fingerprint(root_after) == fingerprint(opened_root), code)
            raw = b"".join(chunks)
        except NodeCommandError:
            raise
        except (OSError, ValueError) as exc:
            raise NodeCommandError(code) from exc
        finally:
            if document_fd is not None:
                os.close(document_fd)
            if current_fd is not None and current_fd != root_fd:
                os.close(current_fd)
            if root_fd is not None:
                os.close(root_fd)
        try:
            document = canonical_json_loads(raw, path=relative_path)
        except Exception as exc:
            raise NodeCommandError(code) from exc
        _require(isinstance(document, dict), code)
        return document

    def _identity(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "deployment_id": self.deployment_id,
            "node_id": self.node_id,
            "host_id": self.host_id,
            "process_id": self.process_id,
            "endpoint_id": self.endpoint_id,
            "peer_generation": self.peer_generation,
            "state": self.state,
            "route_ready": False,
        }

    def _signed_result(self, event: str, details: Mapping[str, Any] | None = None) -> dict[str, Any]:
        _require(self.signer is not None, "signer_not_ready")
        statement = {
            "protocol": NODE_OBSERVATION_PROTOCOL,
            "event": event,
            "monotonic_ns": time.monotonic_ns(),
            **self._identity(),
            "details": {} if details is None else _plain_json(details),
        }
        return {
            "observation": statement,
            "signature": self.signer.sign(statement),
            "verification_key": self.signer.public_key_record(),
        }

    def _new_sidecar_process(self) -> NativeSidecarProcess:
        return NativeSidecarProcess(
            binary=self.sidecar_binary,
            socket_root=self.socket_root,
            local_only=self.sidecar_local_only,
            queue_capacity=128,
            startup_timeout=min(self.command_timeout, 30.0),
            endpoint_secret_file=self.endpoint_secret_file,
        )

    def _build_runtime_port(
        self,
        placement: Placement,
        graph: ExecutionGraph,
        loaded: Any,
    ) -> Any:
        """Lazily import and instantiate the placement's declared runtime port.

        The placement record selects the backend at runtime so that, for
        example, Linux nodes never import the Apple-only MLX backend and macOS
        Apple-Silicon nodes still receive the optimized path.  Both backends
        expose the RuntimePort protocol used by the Router.
        """
        backend = placement.runtime_backend
        clock = self._clock.now
        loaded_stages = {placement.placement_id: loaded}
        if backend == "numpy":
            from mycelium_router.numpy_runtime import NumpyRuntimePort

            return NumpyRuntimePort(
                self.node_id,
                graph,
                loaded_stages,
                clock=clock,
            )
        if backend == "mlx":
            from mycelium_router.mlx_runtime import MLXRuntimePort

            return MLXRuntimePort(
                self.node_id,
                graph,
                loaded_stages,
                clock=clock,
            )
        raise NodeCommandError("unsupported_runtime_backend")

    def _configure(self, payload: dict[str, Any]) -> dict[str, Any]:
        _require(self.state == "NEW", "invalid_state_for_configure")
        legacy_fields = {
            "assignment_file",
            "artifact_report_file",
            "graph",
            "device_states",
            "load_generation",
        }
        stage_pack_fields = {
            "assignment_file",
            "manifest_file",
            "stage_pack_file",
            "graph",
            "device_states",
            "load_generation",
        }
        _require(
            isinstance(payload, dict)
            and set(payload) in (legacy_fields, stage_pack_fields),
            "invalid_configure_fields",
        )
        data = payload
        _require(
            isinstance(data["load_generation"], int)
            and not isinstance(data["load_generation"], bool)
            and data["load_generation"] > 0,
            "invalid_load_generation",
        )
        graph = execution_graph_from_document(data["graph"])
        _require(graph.deployment_id == self.deployment_id, "graph_deployment_id_mismatch")
        states = device_states_from_document(data["device_states"])
        _require(set(states) == {placement.node_id for stage in graph.stages for placement in stage.placements}, "device_state_node_mismatch")
        assignment = self._safe_document(data["assignment_file"], "invalid_assignment_file")
        _require(assignment.get("deployment_id") == self.deployment_id, "assignment_deployment_id_mismatch")
        _require(assignment.get("node_id") == self.node_id, "assignment_node_id_mismatch")
        stage_pack_digest: str | None = None
        if "stage_pack_file" in data:
            manifest = self._safe_document(
                data["manifest_file"],
                "invalid_manifest_file",
            )
            pack = self._safe_document(data["stage_pack_file"], "invalid_stage_pack_file")
            try:
                verification = verify_stage_pack(
                    pack,
                    assignment=assignment,
                    manifest=manifest,
                )
                report = artifact_report_for_loader(
                    pack,
                    verification,
                    assignment=assignment,
                    manifest=manifest,
                )
            except (TypeError, ValueError) as exc:
                raise NodeCommandError("invalid_stage_pack_file") from exc
            stage_pack_digest = pack["stage_pack_digest"]
        else:
            report = self._safe_document(
                data["artifact_report_file"],
                "invalid_artifact_report_file",
            )
        local_placements = [
            placement
            for stage in graph.stages
            for placement in stage.placements
            if placement.node_id == self.node_id and placement.lifecycle_state == "ACTIVE"
        ]
        _require(len(local_placements) == 1, "invalid_local_placement_count")
        placement = local_placements[0]
        _require(placement.assignment_id == assignment.get("assignment_id"), "placement_assignment_mismatch")
        loaded = load_assignment_stage(assignment, report, load_generation=data["load_generation"])
        load_proof_document = json.loads(canonical_json(loaded.proof))
        _require(
            layer_load_proof_digest(load_proof_document)
            == placement.load_proof_digest,
            "placement_load_proof_mismatch",
        )
        topology = PublishedTopologyProvider(graph)
        device_provider = PublishedDeviceStateProvider(topology, states)
        capacity = SQLiteQualificationCapacityPort(
            self.socket_root.parent / f"{self.deployment_id}.capacity.sqlite3",
            topology,
            {node: state.available_kv_bytes for node, state in states.items()},
            clock=self._clock,
            id_source=self._ids,
        )
        runtime = self._build_runtime_port(placement, graph, loaded)
        sidecar = self._new_sidecar_process()
        try:
            ready = sidecar.start()
        except BaseException:
            runtime.close(reason="configure_failed")
            raise
        self.graph = graph
        self.topology = topology
        self.device_states = device_provider
        self.capacity = capacity
        self.runtime = runtime
        self.sidecar = sidecar
        self.endpoint_id = ready["endpoint_id"]
        self.endpoint_addr = ready["endpoint_addr"]
        self.signer = generate_ed25519_signer(endpoint_id=self.endpoint_id)
        self.state = "CONFIGURED"
        configured_details = {
            "assignment_id": assignment["assignment_id"],
            "placement_id": placement.placement_id,
            "manifest_digest": graph.manifest_digest,
            "endpoint_addr": self.endpoint_addr,
            "runtime_mode": runtime.decode_mode,
        }
        if stage_pack_digest is not None:
            configured_details["stage_pack_digest"] = stage_pack_digest
            configured_details["stage_pack_verification_digest"] = report[
                "stage_pack_verification_digest"
            ]
        return self._signed_result("configured", configured_details)

    def _start(self, payload: dict[str, Any]) -> dict[str, Any]:
        _require(self.state == "CONFIGURED", "invalid_state_for_start")
        data = _exact_fields(payload, {"peer"}, "invalid_start_fields")
        peer_data = _exact_fields(
            data["peer"],
            {"node_id", "endpoint_id", "endpoint_addr", "generation"},
            "invalid_peer_fields",
        )
        try:
            peer = PeerBinding(**peer_data)
        except (TypeError, ValueError) as exc:
            raise NodeCommandError("invalid_peer_binding") from exc
        _require(peer.node_id != self.node_id, "self_peer_binding")
        assert self.sidecar is not None
        assert self.endpoint_id is not None
        assert self.runtime is not None
        assert self.topology is not None
        assert self.device_states is not None
        assert self.capacity is not None
        transport = IrohTransport(
            node_id=self.node_id,
            socket_path=self.sidecar.socket_path,
            bootstrap_secret=self.sidecar.bootstrap_material,
            peer=peer,
            expected_endpoint_id=self.endpoint_id,
            queue_capacity=128,
            delivery_timeout_seconds=min(self.command_timeout, 10.0),
            poll_interval_seconds=0.02,
        )
        router = Router(
            node_id=self.node_id,
            topology=self.topology,
            device_states=self.device_states,
            capacity=self.capacity,
            runtime=self.runtime,
            transport=transport,
            clock=self._clock,
            id_source=self._ids,
            config=RouterConfig(),
        )
        try:
            transport.bind_router(router)
            transport.start()
        except BaseException:
            transport.close()
            raise
        self.transport = transport
        self.router = router
        self.peer_generation = peer.generation
        self.state = "RUNNING"
        return self._signed_result("started", {"peer": _plain_json(peer)})

    def _snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        _exact_fields(payload, set(), "invalid_snapshot_fields")
        _require(self.state in {"CONFIGURED", "RUNNING"}, "invalid_state_for_snapshot")
        assert self.runtime is not None
        details: dict[str, Any] = {
            "runtime": self.runtime.kv_snapshot(),
            "capacity": None if self.capacity is None else _plain_json(self.capacity.snapshot()),
            "transport": None,
        }
        if self.transport is not None:
            details["transport"] = _plain_json(self.transport.evidence())
            fatal = self.transport.fatal_error
            details["transport_fatal_error"] = (
                None
                if fatal is None
                else {"code": fatal.code, "detail": fatal.detail}
            )
            details["transport_worker_threads"] = self.transport.worker_threads_alive
            details["transport_dispatcher_phase"] = self.transport.dispatcher_phase
            details["transport_outbound_trace"] = list(self.transport.outbound_trace)
        return self._signed_result("snapshot", details)

    def _cancel(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = _exact_fields(payload, {"request_id"}, "invalid_cancel_fields")
        _require(isinstance(data["request_id"], str) and bool(data["request_id"]), "invalid_request_id")
        _require(self.state == "RUNNING" and self.router is not None, "invalid_state_for_cancel")
        result = self.router.cancel(data["request_id"])
        return self._signed_result("cancelled", {"request_id": data["request_id"], "result": _plain_json(result)})

    def _infer_start(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = _exact_fields(payload, {"request"}, "invalid_infer_start_fields")
        request_data = _exact_fields(
            data["request"],
            {
                "request_id",
                "prompt_token_ids",
                "max_new_tokens",
                "expected_new_tokens",
                "qos_class",
                "admitted_at",
                "target_ttft_ms",
                "target_tpot_ms",
                "target_tokens_per_second",
                "sampling_seed",
                "generation_config_digest",
            },
            "invalid_request_fields",
        )
        _require(isinstance(request_data["prompt_token_ids"], list), "invalid_prompt_token_ids")
        request = RequestContext(
            **{
                **request_data,
                "prompt_token_ids": tuple(request_data["prompt_token_ids"]),
                "admitted_at": time.monotonic(),
            }
        )
        _require(self.state == "RUNNING" and self.router is not None, "invalid_state_for_infer_start")
        _require(request.request_id not in self._sinks, "duplicate_request_id")
        sink = _CaptureSink()
        request_id = self.router.start_distributed_prefill(request, sink)
        _require(request_id == request.request_id, "request_id_changed")
        self._sinks[request_id] = sink
        deadline = time.monotonic() + min(self.command_timeout * 0.8, 20.0)
        status = self.router.request_status(request_id)
        while status == "PREFILL" and time.monotonic() < deadline:
            time.sleep(0.01)
            status = self.router.request_status(request_id)
        _require(status != "PREFILL", "prefill_completion_timeout")
        return self._signed_result(
            "inference_started",
            {
                "request_id": request_id,
                "status": status,
                "output": sink.snapshot(),
            },
        )

    def _infer_decode(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = _exact_fields(payload, {"request_id", "count"}, "invalid_infer_decode_fields")
        _require(isinstance(data["request_id"], str) and bool(data["request_id"]), "invalid_request_id")
        _require(
            isinstance(data["count"], int)
            and not isinstance(data["count"], bool)
            and 1 <= data["count"] <= 128,
            "invalid_decode_count",
        )
        _require(self.state == "RUNNING" and self.router is not None, "invalid_state_for_infer_decode")
        sink = self._sinks.get(data["request_id"])
        _require(sink is not None, "unknown_request_id")
        dispatched = 0
        for _ in range(data["count"]):
            output_count = len(sink.token_ids)
            if not self.router.decode_one_distributed(data["request_id"]):
                break
            dispatched += 1
            deadline = time.monotonic() + min(self.command_timeout * 0.8, 20.0)
            status = self.router.request_status(data["request_id"])
            while (
                len(sink.token_ids) == output_count
                and status not in {"FAILED", "CANCELLED"}
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
                status = self.router.request_status(data["request_id"])
            _require(len(sink.token_ids) > output_count, "decode_completion_timeout")
        return self._signed_result(
            "inference_decoded",
            {
                "request_id": data["request_id"],
                "dispatched": dispatched,
                "status": self.router.request_status(data["request_id"]),
                "output": sink.snapshot(),
            },
        )

    def _rotate(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = _exact_fields(payload, {"peer"}, "invalid_rotate_fields")
        peer_data = _exact_fields(
            data["peer"],
            {"node_id", "endpoint_id", "endpoint_addr", "generation"},
            "invalid_peer_fields",
        )
        _require(self.state == "RUNNING" and self.transport is not None, "invalid_state_for_rotate")
        try:
            peer = PeerBinding(**peer_data)
            self.transport.rotate_peer(peer)
        except (TypeError, ValueError) as exc:
            raise NodeCommandError("invalid_peer_binding") from exc
        self.peer_generation = peer.generation
        return self._signed_result("peer_rotated", {"peer": _plain_json(peer)})

    def close(self) -> None:
        if self.state == "STOPPED":
            return
        if self.transport is not None:
            try:
                self.transport.close()
            except BaseException:
                pass
            self.transport = None
        if self.runtime is not None:
            try:
                self.runtime.close(reason="worker_shutdown")
            except BaseException:
                pass
        if self.sidecar is not None:
            self.sidecar.close()
            self.sidecar = None
        self.router = None
        self.state = "STOPPED"

    def _stop(self, payload: dict[str, Any]) -> dict[str, Any]:
        _exact_fields(payload, set(), "invalid_stop_fields")
        signed: dict[str, Any] | None = None
        if self.signer is not None:
            self.state = "STOPPING"
            signed = self._signed_result("stopping")
        self.close()
        self.stop_requested = True
        return {"state": self.state, "final_observation": signed}

    def dispatch(self, command: Any) -> dict[str, Any]:
        document = _exact_fields(command, _COMMAND_FIELDS, "invalid_command_fields")
        _require(document["protocol"] == NODE_CONTROL_PROTOCOL, "invalid_command_protocol")
        _require(isinstance(document["command_id"], str) and bool(document["command_id"]), "invalid_command_id")
        _require(document["run_id"] == self.run_id, "run_id_mismatch")
        _require(document["deployment_id"] == self.deployment_id, "deployment_id_mismatch")
        _require(isinstance(document["command"], str), "invalid_command")
        _require(isinstance(document["payload"], dict), "invalid_command_payload")
        handlers = {
            "hello": self._hello,
            "configure": self._configure,
            "start": self._start,
            "snapshot": self._snapshot,
            "cancel": self._cancel,
            "infer_start": self._infer_start,
            "infer_decode": self._infer_decode,
            "rotate": self._rotate,
            "stop": self._stop,
        }
        handler = handlers.get(document["command"])
        _require(handler is not None, "unsupported_command")
        return handler(document["payload"])

    def _hello(self, payload: dict[str, Any]) -> dict[str, Any]:
        _exact_fields(payload, set(), "invalid_hello_fields")
        return {**self._identity(), "protocol": NODE_CONTROL_PROTOCOL}


@contextmanager
def _command_deadline(seconds: float):
    if not hasattr(signal, "setitimer"):
        yield
        return

    def timeout_handler(_signum: int, _frame: Any) -> None:
        raise NodeCommandError("command_timeout")

    previous = signal.signal(signal.SIGALRM, timeout_handler)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, previous)


def _response(
    service: PhysicalNodeService,
    *,
    command_id: str,
    ok: bool,
    result: Any = None,
    error_code: str | None = None,
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "protocol": NODE_CONTROL_PROTOCOL,
        "command_id": command_id,
        "node_id": service.node_id,
        "ok": ok,
        "route_ready": False,
    }
    if ok:
        response["result"] = _plain_json(result)
    else:
        response["error"] = {"code": error_code or "node_command_failed"}
    return response


def _emit(document: dict[str, Any]) -> None:
    encoded = canonical_json_bytes(document)
    sys.stdout.buffer.write(encoded + b"\n")
    sys.stdout.buffer.flush()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--deployment-id", required=True)
    parser.add_argument("--node-id", required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--socket-root", type=Path, required=True)
    parser.add_argument("--sidecar-binary", type=Path, required=True)
    parser.add_argument("--sidecar-local-only", action="store_true")
    parser.add_argument("--endpoint-secret-file", type=Path)
    parser.add_argument("--command-timeout", type=float, default=30.0)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        service = PhysicalNodeService(
            run_id=args.run_id,
            deployment_id=args.deployment_id,
            node_id=args.node_id,
            artifact_root=args.artifact_root,
            socket_root=args.socket_root,
            sidecar_binary=args.sidecar_binary,
            sidecar_local_only=args.sidecar_local_only,
            command_timeout=args.command_timeout,
            endpoint_secret_file=args.endpoint_secret_file,
        )
    except (NodeCommandError, OSError) as exc:
        print(f"physical-node startup rejected: {type(exc).__name__}", file=sys.stderr)
        return 2

    try:
        for raw_line in sys.stdin.buffer:
            command_id = "unknown"
            if len(raw_line) > MAX_COMMAND_BYTES:
                _emit(_response(service, command_id=command_id, ok=False, error_code="command_too_large"))
                continue
            try:
                command = canonical_json_loads(raw_line.rstrip(b"\n"), path="stdin")
                if isinstance(command, dict) and isinstance(command.get("command_id"), str):
                    command_id = command["command_id"]
                with _command_deadline(service.command_timeout):
                    result = service.dispatch(command)
                _emit(_response(service, command_id=command_id, ok=True, result=result))
            except NodeCommandError as exc:
                _emit(_response(service, command_id=command_id, ok=False, error_code=exc.code))
                if exc.code == "command_timeout":
                    service.close()
                    return 3
            except BaseException as exc:
                print(f"physical-node command failed: {type(exc).__name__}", file=sys.stderr)
                traceback.print_exc(file=sys.stderr)
                _emit(_response(service, command_id=command_id, ok=False, error_code="node_command_failed"))
            if service.stop_requested:
                return 0
        service.close()
        return 0
    finally:
        service.close()


if __name__ == "__main__":
    raise SystemExit(main())
