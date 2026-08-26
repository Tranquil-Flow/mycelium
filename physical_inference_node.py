#!/usr/bin/env python3.14
"""Command-bounded physical Mycelium Router/runtime node service.

Control uses canonical JSON lines on stdin/stdout. Diagnostics and native
sidecar logs remain on stderr. Every command is bound to one immutable run,
deployment, and node identity. Private signing and sidecar key material never
enters a serialization-facing object.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import fields, is_dataclass, replace
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import resource
import select
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import traceback
from typing import Any, Callable, Mapping
import uuid

from mycelium_iroh_sidecar import SidecarClient
from mycelium_node.identity import load_node_signer
from mycelium_physical_runner.remote_probe import derive_local_run_scoped_identity
from mycelium_qualification.evidence import canonical_json_bytes, canonical_json_loads
from mycelium_qualification.signing import generate_ed25519_signer
from mycelium_router.contracts import (
    DeviceState,
    ExecutionGraph,
    LayerRange,
    PathCancellation,
    Placement,
    PlacementEdge,
    RequestContext,
    RouterConfig,
    Stage,
    StageCost,
)
from mycelium_router.serialization import path_manifest_from_dict
from mycelium_router.live_ports import (
    PublishedDeviceStateProvider,
    PublishedTopologyProvider,
)
from mycelium_router.layer_builder import layer_load_proof_digest
from mycelium_router.router import Router
from mycelium_router.transports.iroh import IrohTransport, PeerBinding
from mycelium_router.validation import validate_execution_graph, validate_manifest
from physical_sqlite_capacity import SQLiteQualificationCapacityPort

NODE_CONTROL_PROTOCOL = "mycelium.physical_node_control.v1"
NODE_OBSERVATION_PROTOCOL = "mycelium.physical_node_observation.v1"
MAX_COMMAND_BYTES = 4 * 1024 * 1024
MAXIMUM_PHYSICAL_CLOCK_SKEW_SECONDS = 5.0
_MAX_IROH_DELIVERY_TIMEOUT_SECONDS = 240.0
_MAX_INFERENCE_COMPLETION_TIMEOUT_SECONDS = 240.0
_COMMAND_FIELDS = frozenset(
    {"protocol", "command_id", "run_id", "deployment_id", "command", "payload"}
)
_REQUEST_CONTROL_FIELDS = frozenset(
    {
        "deployment_id",
        "deployment_epoch",
        "qualification_digest",
        "command_id",
        "publisher_generation",
        "absolute_deadline_ms",
        "request_attempt",
        "path_id",
        "path_attempt",
        "path_digest",
        "topology_generation",
        "cancellation_generation",
    }
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


def _exact_fields(
    document: Any, expected: set[str] | frozenset[str], code: str
) -> dict[str, Any]:
    _require(isinstance(document, dict) and set(document) == set(expected), code)
    return document


def _plain_json(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _plain_json(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain_json(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise NodeCommandError("nonserializable_observation")


def _bounded_command_output(command: list[str]) -> str | None:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0 or len(completed.stdout) > 64 * 1024:
        return None
    return completed.stdout


def _host_available_memory_bytes() -> int:
    if sys.platform.startswith("linux"):
        try:
            values = {
                key.rstrip(":"): int(value) * 1024
                for line in Path("/proc/meminfo")
                .read_text(encoding="utf-8")
                .splitlines()
                if len(parts := line.split()) >= 2
                for key, value in [parts[:2]]
                if value.isdigit()
            }
        except OSError:
            values = {}
        if values.get("MemAvailable", 0) > 0:
            return values["MemAvailable"]
    if sys.platform == "darwin":
        output = _bounded_command_output(["/usr/bin/vm_stat"])
        if output:
            page_match = re.search(r"page size of (\d+) bytes", output)
            page_size = int(page_match.group(1)) if page_match else 4096
            pages = 0
            for label in (
                "Pages free",
                "Pages inactive",
                "Pages speculative",
                "Pages purgeable",
            ):
                match = re.search(
                    rf"^{re.escape(label)}:\s+(\d+)\.", output, re.MULTILINE
                )
                if match:
                    pages += int(match.group(1))
            if pages > 0:
                return pages * page_size
    try:
        return int(os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE"))
    except (OSError, ValueError):
        return 0


def _process_rss_bytes() -> int:
    if sys.platform.startswith("linux"):
        try:
            resident_pages = int(Path("/proc/self/statm").read_text().split()[1])
            return resident_pages * int(os.sysconf("SC_PAGE_SIZE"))
        except (OSError, ValueError, IndexError):
            pass
    if sys.platform == "darwin":
        output = _bounded_command_output(
            ["/bin/ps", "-o", "rss=", "-p", str(os.getpid())]
        )
        if output and output.strip().isdigit():
            return int(output.strip()) * 1024
    maximum = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(maximum if sys.platform == "darwin" else maximum * 1024)


def _host_swap_used_bytes() -> int:
    if sys.platform.startswith("linux"):
        try:
            values = {
                key.rstrip(":"): int(value) * 1024
                for line in Path("/proc/meminfo")
                .read_text(encoding="utf-8")
                .splitlines()
                if len(parts := line.split()) >= 2
                for key, value in [parts[:2]]
                if value.isdigit()
            }
        except OSError:
            return 0
        return max(0, values.get("SwapTotal", 0) - values.get("SwapFree", 0))
    if sys.platform == "darwin":
        output = _bounded_command_output(["/usr/sbin/sysctl", "-n", "vm.swapusage"])
        if output:
            match = re.search(r"used = ([0-9.]+)([MGT])", output)
            if match:
                multiplier = {"M": 2**20, "G": 2**30, "T": 2**40}[match.group(2)]
                return int(float(match.group(1)) * multiplier)
    return 0


def _host_thermal_state() -> str | None:
    if sys.platform.startswith("linux"):
        temperatures: list[int] = []
        for path in Path("/sys/class/thermal").glob("thermal_zone*/temp"):
            try:
                temperatures.append(int(path.read_text(encoding="utf-8").strip()))
            except (OSError, ValueError):
                continue
        if temperatures:
            peak = max(temperatures)
            if peak >= 95_000:
                return "critical"
            if peak >= 85_000:
                return "serious"
            return "nominal"
    return None


def _host_power_state() -> str | None:
    if sys.platform == "darwin":
        output = _bounded_command_output(["/usr/bin/pmset", "-g", "batt"])
        if output:
            charge = re.search(r"(\d+)%", output)
            if "AC Power" in output:
                return "external"
            if charge and int(charge.group(1)) <= 5:
                return "critical_battery"
            return "battery"
    if sys.platform.startswith("linux"):
        supplies = sorted(Path("/sys/class/power_supply").glob("BAT*"))
        if supplies:
            try:
                capacity = int((supplies[0] / "capacity").read_text().strip())
                status = (supplies[0] / "status").read_text().strip().lower()
            except (OSError, ValueError):
                return None
            if status in {"charging", "full"}:
                return "external"
            return "critical_battery" if capacity <= 5 else "battery"
    return None


def _runtime_build_digest(backend: str) -> str:
    names = [
        "physical_inference_node.py",
        "runtime_loader.py",
        "model_adapters.py",
        "numpy_runtime.py",
        "mycelium_router/mlx_runtime.py"
        if backend == "mlx"
        else "mycelium_router/numpy_runtime.py",
    ]
    digest = hashlib.sha256()
    digest.update(sys.version.encode("utf-8"))
    for name in names:
        path = Path(__file__).resolve().parent / name
        digest.update(name.encode("utf-8"))
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"missing")
    return "sha256:" + digest.hexdigest()


def _route_decode_mode(graph: ExecutionGraph, architecture: str | None) -> str:
    """Choose one decode mode supported by every active route placement."""

    route_backends = {
        placement.runtime_backend
        for stage in graph.stages
        for placement in stage.placements
        if placement.lifecycle_state == "ACTIVE"
    }
    if not route_backends or route_backends - {"mlx", "numpy", "pixel-stdlib"}:
        raise NodeCommandError("unsupported_runtime_backend")
    if "pixel-stdlib" in route_backends:
        return "complete_context_replay"
    if "numpy" in route_backends and architecture not in {"qwen2", "qwen3"}:
        return "complete_context_replay"
    return "stage_local_kv"


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
    _require(
        root["protocol"] == "mycelium.execution_graph.v1",
        "invalid_execution_graph_protocol",
    )
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
        _require(
            isinstance(stage_data["component_roles"], list), "invalid_component_roles"
        )
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


def device_states_from_document(
    document: Any,
    *,
    observed_at: float | None = None,
) -> dict[str, DeviceState]:
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
    now = time.time() if observed_at is None else observed_at
    for node_id, state_document in document.items():
        state_data = _exact_fields(
            state_document, expected, "invalid_device_state_fields"
        )
        _require(state_data["node_id"] == node_id, "device_state_key_mismatch")
        _require(
            isinstance(state_data["neighbor_rtt_ms"], dict), "invalid_neighbor_rtt"
        )
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


class _DistributedProtocolClock:
    """Unix-aligned time that advances monotonically within this process.

    Lease deadlines cross physical hosts, so raw ``time.monotonic()`` values
    cannot enter Router/runtime protocol state: each host has a different
    monotonic epoch.  Anchor once to the shared Unix domain, then advance from
    the local monotonic source to avoid wall-clock jumps during a run.
    """

    def __init__(
        self,
        *,
        unix_now: Callable[[], float] | None = None,
        monotonic_now: Callable[[], float] | None = None,
    ) -> None:
        self._monotonic_now = monotonic_now or time.monotonic
        wall_source = unix_now or time.time
        self._unix_origin = float(wall_source())
        self._monotonic_origin = float(self._monotonic_now())

    def now(self) -> float:
        return self._unix_origin + (
            float(self._monotonic_now()) - self._monotonic_origin
        )


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
        force_relay: bool = False,
    ) -> None:
        _require(
            isinstance(local_only, bool)
            and isinstance(force_relay, bool)
            and not (local_only and force_relay),
            "invalid_sidecar_network_mode",
        )
        self.binary = binary
        self.socket_root = socket_root
        self.local_only = local_only
        self.force_relay = force_relay
        self.queue_capacity = queue_capacity
        self.startup_timeout = startup_timeout
        self.endpoint_secret_file = _validated_endpoint_secret_file(
            endpoint_secret_file
        )
        self.socket_path = socket_root / "i.sock"
        self._bootstrap_material: bytes | None = None
        self._socket_root_created = False
        self.process: subprocess.Popen[str] | None = None
        self.ready: dict[str, Any] | None = None

    @property
    def bootstrap_material(self) -> bytes:
        _require(self._bootstrap_material is not None, "sidecar_not_started")
        return self._bootstrap_material

    def status(self) -> dict[str, Any]:
        """Return bounded child health without exposing process identity."""

        process = self.process
        returncode = None if process is None else process.poll()
        return {
            "started": process is not None,
            "alive": process is not None and returncode is None,
            "returncode": returncode,
        }

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
        if self.force_relay:
            command.append("--force-relay")
        if self.endpoint_secret_file is not None:
            command.extend(["--endpoint-secret-file", str(self.endpoint_secret_file)])
        return command

    def start(self) -> dict[str, Any]:
        _require(self.process is None, "sidecar_already_started")
        _require(
            self.binary.is_file() and os.access(self.binary, os.X_OK),
            "sidecar_binary_unavailable",
        )
        self._prepare_socket_root()
        _require(
            len(os.fsencode(self.socket_path)) < 100, "sidecar_socket_path_too_long"
        )
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
            readable, _, _ = select.select(
                [process.stdout], [], [], self.startup_timeout
            )
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
            client = SidecarClient(
                self.socket_path, material, timeout=self.startup_timeout
            )
            try:
                client.connect()
                _require(
                    client.endpoint_id == ready["endpoint_id"],
                    "sidecar_endpoint_mismatch",
                )
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
                self.ready = None
                self._cleanup_socket_root()
        else:
            self._bootstrap_material = None
            self.ready = None
            self._cleanup_socket_root()

    def _cleanup_socket_root(self) -> None:
        if not self._socket_root_created:
            return
        try:
            socket_metadata = self.socket_path.lstat()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        else:
            if not stat.S_ISDIR(socket_metadata.st_mode):
                try:
                    self.socket_path.unlink()
                except OSError:
                    pass
        try:
            root_metadata = self.socket_root.lstat()
        except FileNotFoundError:
            self._socket_root_created = False
            return
        except OSError:
            return
        if stat.S_ISDIR(root_metadata.st_mode):
            try:
                self.socket_root.rmdir()
            except OSError:
                return
            self._socket_root_created = False

    def _prepare_socket_root(self) -> None:
        """Create the sidecar socket root, clearing crashed-cycle residue.

        A socket root left behind by a crashed cycle (orphaned sidecar)
        must not wedge a fresh configure. A LIVE sidecar — an accepting
        socket — still fails closed: the create-exclusive mkdir that
        follows the cleanup preserves the original mutual exclusion
        between two node instances.
        """
        try:
            self.socket_root.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            if self.socket_path.exists():
                _require(not self._socket_is_live(), "sidecar_socket_conflict")
                try:
                    self.socket_path.unlink()
                except OSError:
                    _require(False, "sidecar_socket_conflict")
            try:
                self.socket_root.rmdir()
            except OSError:
                _require(False, "sidecar_socket_conflict")
            self.socket_root.mkdir(parents=True, exist_ok=False)
        self._socket_root_created = True

    def _socket_is_live(self) -> bool:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(0.25)
        try:
            client.connect(str(self.socket_path))
        except OSError:
            return False
        else:
            client.close()
            return True


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
        requested_decode_mode: str | None = None,
        sidecar_force_relay: bool = False,
    ) -> None:
        for value, code in (
            (run_id, "invalid_run_id"),
            (deployment_id, "invalid_deployment_id"),
            (node_id, "invalid_node_id"),
        ):
            _require(
                isinstance(value, str) and bool(value) and value == value.strip(), code
            )
        _require(command_timeout > 0, "invalid_command_timeout")
        _require(
            isinstance(sidecar_local_only, bool)
            and isinstance(sidecar_force_relay, bool)
            and not (sidecar_local_only and sidecar_force_relay),
            "invalid_sidecar_network_mode",
        )
        _require(
            requested_decode_mode in {None, "complete_context_replay", "stage_local_kv"},
            "invalid_requested_decode_mode",
        )
        self.run_id = run_id
        self.deployment_id = deployment_id
        self.node_id = node_id
        self.artifact_root = artifact_root.resolve(strict=True)
        self.socket_root = socket_root
        self.sidecar_binary = sidecar_binary.resolve(strict=False)
        self.sidecar_local_only = sidecar_local_only
        self.sidecar_force_relay = sidecar_force_relay
        self.command_timeout = command_timeout
        self.endpoint_secret_file = _validated_endpoint_secret_file(
            endpoint_secret_file
        )
        self.requested_decode_mode = requested_decode_mode
        self.host_id, _boot_id = derive_local_run_scoped_identity(run_id)
        self.process_id = os.getpid()
        self.state = "NEW"
        self.stop_requested = False
        self.graph: ExecutionGraph | None = None
        self.topology: PublishedTopologyProvider | None = None
        self.device_states: PublishedDeviceStateProvider | None = None
        self.capacity: SQLiteQualificationCapacityPort | None = None
        self.runtime: Any = None
        self.runtime_identity: dict[str, str] | None = None
        self.sidecar: NativeSidecarProcess | None = None
        self.transport: IrohTransport | None = None
        self.router: Router | None = None
        self.signer: Any | None = None
        self.endpoint_id: str | None = None
        self.endpoint_addr: dict[str, Any] | None = None
        self.peer_generation = 0
        self._ids = _UuidSource()
        self._clock = _DistributedProtocolClock()
        self._router_config = RouterConfig(
            prefill_chunk_size_tokens=(
                8 if requested_decode_mode == "stage_local_kv" else 0
            )
        )
        self._sinks: dict[str, _CaptureSink] = {}
        self._last_cancellation: dict[str, Any] | None = None
        self._cancellations_by_subject: dict[
            tuple[str, str, int], dict[str, Any]
        ] = {}
        self._control_lock = threading.RLock()
        self._request_controls: dict[str, dict[str, Any]] = {}
        self._pending_cancellations: dict[str, dict[str, Any]] = {}
        self._cancellation_controls: dict[str, dict[str, Any]] = {}
        self._request_cleanup_receipts: dict[str, dict[str, Any]] = {}

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

    def _signed_result(
        self, event: str, details: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
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

    def _host_resources(self) -> dict[str, Any]:
        _require(self.runtime_identity is not None, "runtime_identity_unavailable")
        runtime_identity = self.runtime_identity
        backend = runtime_identity["backend"]
        from model_adapters import ADAPTERS

        supported_architectures = sorted(
            adapter.architecture
            for adapter in ADAPTERS.values()
            if backend in adapter.runtime_backends
        )
        supported_dtypes = {"float32", "float16", "bfloat16"}
        supported_quantizations = {
            "none",
            "float32",
            "float16",
            "bfloat16",
            "int8-weight-only",
        }
        supported_dtypes.add(runtime_identity["dtype"])
        supported_quantizations.add(runtime_identity["quantization"])
        decode_modes_by_architecture = {
            architecture: (
                ["complete_context_replay", "stage_local_kv"]
                if backend == "mlx"
                or (backend == "numpy" and architecture in {"qwen2", "qwen3"})
                else ["complete_context_replay"]
            )
            for architecture in supported_architectures
        }
        supported_decode_modes = sorted(
            {
                mode
                for modes in decode_modes_by_architecture.values()
                for mode in modes
            }
        )
        object_root = self.artifact_root / ".mycelium" / "objects" / "sha256"
        cached_content_digests = (
            sorted(
                f"sha256:{path.name}"
                for path in object_root.iterdir()
                if path.is_file() and re.fullmatch(r"[0-9a-f]{64}", path.name)
            )
            if object_root.is_dir()
            else []
        )
        _require(
            len(cached_content_digests) <= 4096,
            "cached_content_digest_limit_exceeded",
        )
        disk = shutil.disk_usage(self.artifact_root)
        now_unix_ms = int(time.time() * 1_000)
        document: dict[str, Any] = {
            "protocol": "mycelium.host_resource_snapshot.v1",
            "observed_at_unix_ms": now_unix_ms,
            "valid_until_unix_ms": now_unix_ms + 120_000,
            "backend": backend,
            "supported_architectures": supported_architectures,
            "supported_dtypes": sorted(supported_dtypes),
            "supported_quantizations": sorted(supported_quantizations),
            "supported_decode_modes": supported_decode_modes,
            "decode_modes_by_architecture": decode_modes_by_architecture,
            "runtime_build_digest": _runtime_build_digest(backend),
            "available_memory_bytes": _host_available_memory_bytes(),
            "rss_bytes": _process_rss_bytes(),
            "swap_used_bytes": _host_swap_used_bytes(),
            "disk_free_bytes": disk.free,
            "disk_total_bytes": disk.total,
            "cached_content_digests": cached_content_digests,
            "thermal_state": _host_thermal_state(),
            "power_state": _host_power_state(),
            "route_ready": False,
        }
        document["resource_digest"] = (
            "sha256:" + hashlib.sha256(canonical_json_bytes(document)).hexdigest()
        )
        return document

    def _new_sidecar_process(self) -> NativeSidecarProcess:
        return NativeSidecarProcess(
            binary=self.sidecar_binary,
            socket_root=self.socket_root,
            local_only=self.sidecar_local_only,
            force_relay=self.sidecar_force_relay,
            queue_capacity=128,
            startup_timeout=min(self.command_timeout, 30.0),
            endpoint_secret_file=self.endpoint_secret_file,
        )

    @staticmethod
    def _validate_pixel_assignment_binding(
        assignment: dict[str, Any],
        pixel_stage: Any,
        graph: ExecutionGraph,
        graph_stage: Stage,
        placement: Placement,
    ) -> None:
        document = pixel_stage.document
        string_fields = (
            "deployment_id",
            "model_id",
            "resolved_commit",
            "manifest_digest",
            "node_id",
            "assignment_id",
        )
        _require(
            all(type(assignment.get(field)) is str for field in string_fields)
            and type(assignment.get("deployment_epoch")) is int,
            "pixel_assignment_pack_mismatch",
        )
        expected_scalars = {
            "deployment_id": graph.deployment_id,
            "deployment_epoch": graph.deployment_epoch,
            "model_id": graph.model_id,
            "resolved_commit": graph.resolved_commit,
            "manifest_digest": graph.manifest_digest,
            "node_id": placement.node_id,
            "assignment_id": placement.assignment_id,
        }
        _require(
            all(
                assignment.get(field) == expected
                for field, expected in expected_scalars.items()
            ),
            "pixel_assignment_pack_mismatch",
        )
        _require(
            all(
                assignment.get(field) == document[field]
                for field in (
                    "deployment_id",
                    "assignment_id",
                    "model_id",
                    "resolved_commit",
                    "manifest_digest",
                )
            ),
            "pixel_assignment_pack_mismatch",
        )
        layer_range = assignment.get("range")
        expected_range = {
            "start_layer": graph_stage.layer_range.start_layer,
            "end_layer_exclusive": graph_stage.layer_range.end_layer_exclusive,
            "layer_count": graph_stage.layer_range.layer_count,
        }
        _require(
            isinstance(layer_range, dict)
            and set(layer_range) == set(expected_range)
            and all(type(layer_range.get(field)) is int for field in expected_range)
            and layer_range == expected_range
            and document["start_layer"] == expected_range["start_layer"]
            and document["end_layer_exclusive"]
            == expected_range["end_layer_exclusive"],
            "pixel_assignment_pack_mismatch",
        )
        roles = list(graph_stage.component_roles)
        _require(
            type(assignment.get("components")) is list
            and all(type(role) is str for role in assignment["components"])
            and assignment["components"] == roles
            and list(document["component_roles"]) == roles,
            "pixel_assignment_pack_mismatch",
        )
        tensor_keys = sorted(pixel_stage.tensors)
        component_tensor_keys = assignment.get("component_tensor_keys")
        expected_tensor_keys = assignment.get("expected_tensor_keys")
        expected_tensor_prefixes = assignment.get("expected_tensor_prefixes")
        _require(
            type(component_tensor_keys) is dict
            and set(component_tensor_keys) == {"decoder"}
            and type(component_tensor_keys["decoder"]) is list
            and all(type(key) is str for key in component_tensor_keys["decoder"])
            and component_tensor_keys["decoder"] == tensor_keys
            and type(expected_tensor_keys) is list
            and all(type(key) is str for key in expected_tensor_keys)
            and expected_tensor_keys == tensor_keys
            and type(expected_tensor_prefixes) is list
            and all(type(prefix) is str for prefix in expected_tensor_prefixes)
            and expected_tensor_prefixes == [pixel_stage.prefix],
            "pixel_assignment_pack_mismatch",
        )

    def _build_runtime_port(
        self,
        placement: Placement,
        graph: ExecutionGraph,
        loaded: Any,
        *,
        parent_assignment_digest: str | None = None,
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
        runtime_proof = getattr(loaded, "proof", None)
        runtime_document = (
            runtime_proof.get("runtime")
            if isinstance(runtime_proof, Mapping)
            else None
        )
        architecture = (
            runtime_document.get("architecture")
            if isinstance(runtime_document, Mapping)
            else None
        )
        supported_route_mode = _route_decode_mode(graph, architecture)
        if (
            self.requested_decode_mode == "stage_local_kv"
            and supported_route_mode != "stage_local_kv"
        ):
            raise NodeCommandError("requested_decode_mode_unsupported")
        route_decode_mode = self.requested_decode_mode or supported_route_mode
        if backend == "pixel-stdlib":
            from mycelium_mobile.pixel_runtime import (
                PixelRuntimeError,
                PixelStageRuntimePort,
            )
            from mycelium_mobile.pixel_stage import PixelStage

            _require(isinstance(loaded, PixelStage), "invalid_pixel_stage_pack_file")
            _require(
                isinstance(parent_assignment_digest, str)
                and bool(parent_assignment_digest),
                "invalid_parent_assignment_digest",
            )
            assert isinstance(parent_assignment_digest, str)
            try:
                return PixelStageRuntimePort(
                    loaded,
                    graph=graph,
                    placement_id=placement.placement_id,
                    parent_assignment_digest=parent_assignment_digest,
                )
            except PixelRuntimeError as exc:
                raise NodeCommandError("invalid_pixel_runtime_binding") from exc
        if backend == "numpy":
            from mycelium_router.numpy_runtime import NumpyRuntimePort

            return NumpyRuntimePort(
                self.node_id,
                graph,
                loaded_stages,
                clock=clock,
                decode_mode=route_decode_mode,
            )
        if backend == "mlx":
            from mycelium_router.mlx_runtime import MLXRuntimePort

            return MLXRuntimePort(
                self.node_id,
                graph,
                loaded_stages,
                clock=clock,
                decode_mode=route_decode_mode,
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
        pixel_stage_pack_fields = {
            "assignment_file",
            "pixel_stage_pack_file",
            "graph",
            "device_states",
            "load_generation",
        }
        _require(
            isinstance(payload, dict)
            and set(payload)
            in (legacy_fields, stage_pack_fields, pixel_stage_pack_fields),
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
        _require(
            graph.deployment_id == self.deployment_id, "graph_deployment_id_mismatch"
        )
        states = device_states_from_document(
            data["device_states"],
            observed_at=self._clock.now(),
        )
        _require(
            set(states)
            == {
                placement.node_id
                for stage in graph.stages
                for placement in stage.placements
            },
            "device_state_node_mismatch",
        )
        assignment = self._safe_document(
            data["assignment_file"], "invalid_assignment_file"
        )
        _require(
            assignment.get("deployment_id") == self.deployment_id,
            "assignment_deployment_id_mismatch",
        )
        _require(
            assignment.get("node_id") == self.node_id, "assignment_node_id_mismatch"
        )
        stage_pack_digest: str | None = None
        report: dict[str, Any] | None = None
        pixel_stage: Any = None
        parent_assignment_digest: str | None = None
        loaded_runtime_identity: dict[str, str]
        if "pixel_stage_pack_file" in data:
            from layer_assignment import (
                LAYER_ASSIGNMENT_PROTOCOL,
                validate_assignment_identity,
            )
            from mycelium_mobile.pixel_stage import PixelStage, PixelStageError

            try:
                _require(
                    assignment.get("protocol") == LAYER_ASSIGNMENT_PROTOCOL,
                    "invalid_assignment_file",
                )
                validate_assignment_identity(assignment)
            except (KeyError, TypeError, ValueError) as exc:
                raise NodeCommandError("invalid_assignment_file") from exc
            runtime_identity = assignment.get("runtime")
            _require(
                isinstance(runtime_identity, dict)
                and set(runtime_identity) == {"backend", "dtype", "quantization"}
                and runtime_identity.get("backend") == "pixel-stdlib"
                and runtime_identity.get("dtype") == "float32"
                and runtime_identity.get("quantization") == "none",
                "invalid_assignment_file",
            )
            pixel_pack = self._safe_document(
                data["pixel_stage_pack_file"],
                "invalid_pixel_stage_pack_file",
            )
            try:
                pixel_stage = PixelStage.from_document(pixel_pack)
            except PixelStageError as exc:
                raise NodeCommandError("invalid_pixel_stage_pack_file") from exc
            _require(
                pixel_stage.document["run_id"] == self.run_id,
                "pixel_stage_run_id_mismatch",
            )
            parent_assignment_digest = (
                "sha256:" + hashlib.sha256(canonical_json_bytes(assignment)).hexdigest()
            )
            stage_pack_digest = pixel_pack["pack_digest"]
            loaded_runtime_identity = {
                "backend": "pixel-stdlib",
                "dtype": "float32",
                "quantization": "none",
                "architecture": "pixel_stage",
            }
        elif "stage_pack_file" in data:
            from stage_pack import artifact_report_for_loader, verify_stage_pack

            manifest = self._safe_document(
                data["manifest_file"],
                "invalid_manifest_file",
            )
            pack = self._safe_document(
                data["stage_pack_file"], "invalid_stage_pack_file"
            )
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
        local_bindings = [
            (stage, placement)
            for stage in graph.stages
            for placement in stage.placements
            if placement.node_id == self.node_id
            and placement.lifecycle_state == "ACTIVE"
        ]
        _require(len(local_bindings) == 1, "invalid_local_placement_count")
        graph_stage, placement = local_bindings[0]
        _require(
            placement.assignment_id == assignment.get("assignment_id"),
            "placement_assignment_mismatch",
        )
        if pixel_stage is not None:
            self._validate_pixel_assignment_binding(
                assignment,
                pixel_stage,
                graph,
                graph_stage,
                placement,
            )
            loaded = pixel_stage
        else:
            from runtime_loader import canonical_json, load_assignment_stage

            assert report is not None
            loaded = load_assignment_stage(
                assignment,
                report,
                load_generation=data["load_generation"],
            )
            load_proof_document = json.loads(canonical_json(loaded.proof))
            _require(
                layer_load_proof_digest(load_proof_document)
                == placement.load_proof_digest,
                "placement_load_proof_mismatch",
            )
            proof_runtime_identity = load_proof_document.get("runtime_identity")
            _require(
                isinstance(proof_runtime_identity, dict)
                and all(
                    isinstance(proof_runtime_identity.get(field), str)
                    and bool(proof_runtime_identity[field])
                    for field in ("backend", "dtype", "quantization", "architecture")
                ),
                "invalid_runtime_identity",
            )
            loaded_runtime_identity = {
                field: proof_runtime_identity[field]
                for field in ("backend", "dtype", "quantization", "architecture")
            }
        topology = PublishedTopologyProvider(graph)
        device_provider = PublishedDeviceStateProvider(topology, states)
        capacity = SQLiteQualificationCapacityPort(
            self.socket_root.parent / f"{self.deployment_id}.capacity.sqlite3",
            topology,
            {node: state.available_kv_bytes for node, state in states.items()},
            clock=self._clock,
            id_source=self._ids,
            maximum_imported_lease_seconds=(
                self._router_config.reservation_lease_seconds
                + MAXIMUM_PHYSICAL_CLOCK_SKEW_SECONDS
            ),
        )
        runtime = self._build_runtime_port(
            placement,
            graph,
            loaded,
            parent_assignment_digest=parent_assignment_digest,
        )
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
        self.runtime_identity = loaded_runtime_identity
        self.sidecar = sidecar
        endpoint_id = ready["endpoint_id"]
        _require(
            isinstance(endpoint_id, str) and bool(endpoint_id),
            "invalid_sidecar_ready",
        )
        self.endpoint_id = endpoint_id
        self.endpoint_addr = ready["endpoint_addr"]
        self.signer = (
            generate_ed25519_signer(endpoint_id=endpoint_id)
            if self.endpoint_secret_file is None
            else load_node_signer(
                self.endpoint_secret_file,
                endpoint_id=endpoint_id,
            )
        )
        self.state = "CONFIGURED"
        configured_details = {
            "assignment_id": assignment["assignment_id"],
            "placement_id": placement.placement_id,
            "manifest_digest": graph.manifest_digest,
            "endpoint_addr": self.endpoint_addr,
            "runtime_mode": runtime.decode_mode,
        }
        if stage_pack_digest is not None:
            if pixel_stage is not None:
                configured_details["pixel_stage_pack_digest"] = stage_pack_digest
            else:
                assert report is not None
                configured_details["stage_pack_digest"] = stage_pack_digest
                configured_details["stage_pack_verification_digest"] = report[
                    "stage_pack_verification_digest"
                ]
        return self._signed_result("configured", configured_details)

    def _start(self, payload: dict[str, Any]) -> dict[str, Any]:
        _require(self.state == "CONFIGURED", "invalid_state_for_start")
        _require(
            isinstance(payload, dict)
            and set(payload)
            in (
                {"peer", "local_generation"},
                {"peer", "peers", "local_generation"},
            ),
            "invalid_start_fields",
        )
        data = payload
        local_generation = data["local_generation"]
        _require(
            isinstance(local_generation, int)
            and not isinstance(local_generation, bool)
            and 0 < local_generation <= (1 << 64) - 1,
            "invalid_local_generation",
        )
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
        additional_peer_documents = data.get("peers", [])
        _require(isinstance(additional_peer_documents, list), "invalid_peers")
        additional_peers: list[PeerBinding] = []
        for document in additional_peer_documents:
            peer_document = _exact_fields(
                document,
                {"node_id", "endpoint_id", "endpoint_addr", "generation"},
                "invalid_peer_fields",
            )
            try:
                additional_peer = PeerBinding(**peer_document)
            except (TypeError, ValueError) as exc:
                raise NodeCommandError("invalid_peer_binding") from exc
            _require(additional_peer.node_id != self.node_id, "self_peer_binding")
            additional_peers.append(additional_peer)
        all_peers = [peer, *additional_peers]
        _require(
            len({item.node_id for item in all_peers}) == len(all_peers),
            "duplicate_peer_node_id",
        )
        _require(
            len({item.endpoint_id for item in all_peers}) == len(all_peers),
            "duplicate_peer_endpoint_id",
        )
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
            peers=additional_peers,
            local_generation=local_generation,
            expected_endpoint_id=self.endpoint_id,
            queue_capacity=128,
            # A confirmed delivery acknowledges completed local Router dispatch.
            # Progressive prefill dispatch can synchronously execute and forward
            # through a slower downstream stage before the acknowledgement returns,
            # so a ten-second ceiling spuriously rejects healthy heterogeneous routes.
            delivery_timeout_seconds=min(
                self.command_timeout,
                _MAX_IROH_DELIVERY_TIMEOUT_SECONDS,
            ),
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
            config=self._router_config,
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
        return self._signed_result(
            "started",
            {
                "peer": _plain_json(peer),
                "peers": [_plain_json(item) for item in all_peers],
            },
        )

    def _validated_request_control(
        self,
        value: object,
        *,
        code: str,
        initial: bool,
    ) -> dict[str, Any]:
        control = _exact_fields(value, set(_REQUEST_CONTROL_FIELDS), code)
        _require(
            isinstance(control["deployment_id"], str)
            and bool(control["deployment_id"])
            and type(control["deployment_epoch"]) is int
            and control["deployment_epoch"] >= 1
            and isinstance(control["qualification_digest"], str)
            and re.fullmatch(r"sha256:[0-9a-f]{64}", control["qualification_digest"])
            is not None
            and isinstance(control["command_id"], str)
            and bool(control["command_id"])
            and len(control["command_id"].encode("utf-8")) <= 256
            and type(control["publisher_generation"]) is int
            and control["publisher_generation"] >= 1
            and type(control["absolute_deadline_ms"]) is int
            and control["absolute_deadline_ms"] > 0
            and type(control["request_attempt"]) is int
            and control["request_attempt"] >= 1
            and isinstance(control["path_id"], str)
            and bool(control["path_id"])
            and type(control["path_attempt"]) is int
            and control["path_attempt"] >= 0
            and isinstance(control["path_digest"], str)
            and re.fullmatch(r"sha256:[0-9a-f]{64}", control["path_digest"])
            is not None
            and type(control["topology_generation"]) is int
            and control["topology_generation"] >= 1
            and type(control["cancellation_generation"]) is int
            and control["cancellation_generation"] >= 0
            and (not initial or control["cancellation_generation"] == 0)
            and self.graph is not None
            and control["deployment_id"] == self.graph.deployment_id
            and control["deployment_epoch"] == self.graph.deployment_epoch
            and control["topology_generation"] == self.graph.topology_version,
            code,
        )
        return control

    def _bind_request_control(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = _exact_fields(
            payload,
            {"request_id", "control"},
            "invalid_bind_request_control_fields",
        )
        _require(
            self.state == "RUNNING" and self.router is not None,
            "invalid_state_for_bind_request_control",
        )
        _require(
            isinstance(data["request_id"], str) and bool(data["request_id"]),
            "invalid_request_id",
        )
        control = self._validated_request_control(
            data["control"],
            code="invalid_bind_request_control",
            initial=True,
        )
        with self._control_lock:
            _require(
                data["request_id"] not in self._request_cleanup_receipts,
                "request_already_cleaned",
            )
            current = self._request_controls.get(data["request_id"])
            pending_cancel = self._pending_cancellations.get(data["request_id"])
            bound_control = dict(control)
            if current is not None:
                immutable_fields = _REQUEST_CONTROL_FIELDS - {
                    "cancellation_generation"
                }
                cancellation_control = self._cancellation_controls.get(
                    data["request_id"]
                )
                advanced_duplicate = (
                    all(current[field] == control[field] for field in immutable_fields)
                    and current["cancellation_generation"]
                    == control["cancellation_generation"] + 1
                    and cancellation_control is not None
                    and all(
                        cancellation_control[field] == current[field]
                        for field in _REQUEST_CONTROL_FIELDS
                    )
                )
                _require(
                    current == control or advanced_duplicate,
                    "conflicting_request_control",
                )
                bound_control = dict(current)
            elif pending_cancel is not None:
                _require(
                    all(
                        pending_cancel[field] == control[field]
                        for field in (
                            "deployment_id",
                            "deployment_epoch",
                            "qualification_digest",
                            "command_id",
                            "publisher_generation",
                            "absolute_deadline_ms",
                            "request_attempt",
                            "path_id",
                            "path_attempt",
                            "path_digest",
                            "topology_generation",
                        )
                    )
                    and pending_cancel["cancellation_generation"]
                    == control["cancellation_generation"] + 1,
                    "stale_infer_cancel_generation",
                )
                bound_control["cancellation_generation"] = pending_cancel[
                    "cancellation_generation"
                ]
                self._cancellation_controls[data["request_id"]] = {
                    key: value
                    for key, value in pending_cancel.items()
                    if key != "deadline_budget_ms"
                }
                self._pending_cancellations.pop(data["request_id"], None)
            self._request_controls[data["request_id"]] = bound_control
        return self._signed_result(
            "request_control_bound",
            {
                "request_id": data["request_id"],
                "control_digest": "sha256:"
                + hashlib.sha256(
                    canonical_json_bytes(bound_control)
                ).hexdigest(),
            },
        )

    def _update_request_control(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = _exact_fields(
            payload,
            {"request_id", "control"},
            "invalid_update_request_control_fields",
        )
        _require(
            self.state == "RUNNING" and self.router is not None,
            "invalid_state_for_update_request_control",
        )
        _require(
            isinstance(data["request_id"], str) and bool(data["request_id"]),
            "invalid_request_id",
        )
        control = self._validated_request_control(
            data["control"],
            code="invalid_update_request_control",
            initial=False,
        )
        with self._control_lock:
            current = self._request_controls.get(data["request_id"])
            immutable = _REQUEST_CONTROL_FIELDS - {"publisher_generation"}
            _require(current is not None, "request_control_unknown")
            _require(
                all(current[field] == control[field] for field in immutable),
                "request_control_identity_mismatch",
            )
            if control["publisher_generation"] == current["publisher_generation"]:
                duplicate = True
            else:
                _require(
                    control["publisher_generation"]
                    == current["publisher_generation"] + 1,
                    "publisher_generation_cas_mismatch",
                )
                duplicate = False
                self._request_controls[data["request_id"]] = dict(control)
        return self._signed_result(
            "request_control_updated",
            {
                "request_id": data["request_id"],
                "publisher_generation": control["publisher_generation"],
                "duplicate": duplicate,
            },
        )

    def _health(self, payload: dict[str, Any]) -> dict[str, Any]:
        _exact_fields(payload, set(), "invalid_health_fields")
        _require(self.state in {"CONFIGURED", "RUNNING"}, "invalid_state_for_health")
        fatal = None if self.transport is None else self.transport.fatal_error
        return self._signed_result(
            "health",
            {
                "state": self.state,
                "sidecar_process": (
                    None if self.sidecar is None else self.sidecar.status()
                ),
                "transport_fatal_error": (
                    None
                    if fatal is None
                    else {"code": fatal.code, "detail": fatal.detail}
                ),
                "transport_running": (
                    False if self.transport is None else self.transport.running
                ),
            },
        )

    def _snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        _require(
            set(payload) in (set(), {"cleanup_subject"}),
            "invalid_snapshot_fields",
        )
        cleanup_subject = payload.get("cleanup_subject")
        if cleanup_subject is not None:
            cleanup_subject = _exact_fields(
                cleanup_subject,
                {
                    "deployment_id",
                    "deployment_epoch",
                    "qualification_digest",
                    "request_id",
                    "request_attempt",
                    "path_id",
                    "path_attempt",
                    "path_digest",
                    "topology_generation",
                    "command_id",
                    "cancellation_generation",
                    "publisher_generation",
                    "absolute_deadline_ms",
                },
                "invalid_cleanup_subject",
            )
            _require(
                isinstance(cleanup_subject["deployment_id"], str)
                and bool(cleanup_subject["deployment_id"])
                and type(cleanup_subject["deployment_epoch"]) is int
                and cleanup_subject["deployment_epoch"] >= 1
                and isinstance(cleanup_subject["qualification_digest"], str)
                and cleanup_subject["qualification_digest"].startswith("sha256:")
                and len(cleanup_subject["qualification_digest"]) == 71
                and isinstance(cleanup_subject["request_id"], str)
                and bool(cleanup_subject["request_id"])
                and type(cleanup_subject["request_attempt"]) is int
                and cleanup_subject["request_attempt"] >= 1
                and isinstance(cleanup_subject["path_id"], str)
                and bool(cleanup_subject["path_id"])
                and type(cleanup_subject["path_attempt"]) is int
                and cleanup_subject["path_attempt"] >= 0
                and isinstance(cleanup_subject["path_digest"], str)
                and cleanup_subject["path_digest"].startswith("sha256:")
                and len(cleanup_subject["path_digest"]) == 71
                and type(cleanup_subject["topology_generation"]) is int
                and cleanup_subject["topology_generation"] >= 1
                and isinstance(cleanup_subject["command_id"], str)
                and bool(cleanup_subject["command_id"])
                and type(cleanup_subject["cancellation_generation"]) is int
                and cleanup_subject["cancellation_generation"] >= 0
                and type(cleanup_subject["publisher_generation"]) is int
                and cleanup_subject["publisher_generation"] >= 1
                and type(cleanup_subject["absolute_deadline_ms"]) is int
                and cleanup_subject["absolute_deadline_ms"] > 0,
                "invalid_cleanup_subject",
            )
        _require(self.state in {"CONFIGURED", "RUNNING"}, "invalid_state_for_snapshot")
        assert self.runtime is not None
        details: dict[str, Any] = {
            "runtime": self.runtime.kv_snapshot(),
            "capacity": None
            if self.capacity is None
            else _plain_json(self.capacity.snapshot()),
            "host_resources": self._host_resources(),
            "transport": None,
            "sidecar_process": (
                None if self.sidecar is None else self.sidecar.status()
            ),
        }
        details["interruptibility"] = {
            "runtime_backend": details["runtime"].get("backend"),
            "decode_mode": details["runtime"].get("mode"),
            "work_unit": "transformer_layer",
            "maximum_observed_work_unit_ms": details["runtime"].get(
                "maximum_observed_work_unit_ms"
            ),
            "observed_work_unit_count": details["runtime"].get(
                "observed_work_unit_count", 0
            ),
            "maximum_total_cleanup_ms": 2_000,
            "physical_proof_required": True,
            "backend_candidate": details["runtime"].get("mode")
            == "stage_local_kv",
            "cooperative_bound_candidate": (
                details["runtime"].get("mode") == "stage_local_kv"
                and type(details["runtime"].get("observed_work_unit_count")) is int
                and details["runtime"].get("observed_work_unit_count", 0) > 0
                and isinstance(
                    details["runtime"].get("maximum_observed_work_unit_ms"),
                    (int, float),
                )
                and details["runtime"].get("maximum_observed_work_unit_ms", 2_000)
                < 2_000
            ),
        }
        cancellation = self._last_cancellation
        if self.transport is not None:
            details["transport"] = _plain_json(self.transport.evidence())
            fatal = self.transport.fatal_error
            details["transport_fatal_error"] = (
                None if fatal is None else {"code": fatal.code, "detail": fatal.detail}
            )
            details["transport_worker_threads"] = self.transport.worker_threads_alive
            details["transport_dispatcher_phase"] = self.transport.dispatcher_phase
            details["transport_last_dispatch_error"] = (
                self.transport.last_dispatch_error
            )
            details["transport_outbound_trace"] = list(self.transport.outbound_trace)
            details["transport_pending_delivery_count"] = (
                self.transport.pending_delivery_count
            )
            cancellation = cancellation or self.transport.last_cancellation
            details["transport_cancellation_cleanup_complete"] = (
                False
                if cancellation is None
                else self.transport.cancellation_cleanup_complete(
                    str(cancellation["request_id"]),
                    str(cancellation["path_id"]),
                )
            )
        if cleanup_subject is not None:
            with self._control_lock:
                stored_cleanup = self._request_cleanup_receipts.get(
                    cleanup_subject["request_id"]
                )
            if stored_cleanup is not None:
                _require(
                    all(
                        stored_cleanup.get(field) == value
                        for field, value in cleanup_subject.items()
                    ),
                    "cleanup_receipt_identity_mismatch",
                )
                details["request_cleanup"] = dict(stored_cleanup)
                return self._signed_result("snapshot", details)
            runtime_states = details["runtime"].get("states", {})
            runtime_subject_clean = not any(
                path_id == cleanup_subject["path_id"]
                and state.get("request_id") == cleanup_subject["request_id"]
                and state.get("path_attempt") == cleanup_subject["path_attempt"]
                for path_id, state in runtime_states.items()
            )
            transport_subject_clean = (
                True
                if self.transport is None
                else self.transport.cancellation_cleanup_complete(
                    cleanup_subject["request_id"],
                    cleanup_subject["path_id"],
                    cleanup_subject["path_attempt"],
                )
            )
            details["request_cleanup"] = {
                **cleanup_subject,
                "runtime_clean": runtime_subject_clean,
                "transport_clean": transport_subject_clean,
                "complete": runtime_subject_clean and transport_subject_clean,
            }
            if self.transport is not None:
                details["request_cleanup"]["transport_state"] = (
                    self.transport.cancellation_cleanup_state(
                        cleanup_subject["request_id"],
                        cleanup_subject["path_id"],
                        cleanup_subject["path_attempt"],
                    )
                )
            if runtime_subject_clean and transport_subject_clean:
                with self._control_lock:
                    cancellation_control = self._cancellation_controls.get(
                        cleanup_subject["request_id"]
                    )
                cancellation_observed = (
                    cleanup_subject["request_id"],
                    cleanup_subject["path_id"],
                    cleanup_subject["path_attempt"],
                ) in self._cancellations_by_subject
                if self.transport is not None:
                    cancellation_observed = (
                        cancellation_observed
                        or self.transport.cancellation_observed(
                            cleanup_subject["request_id"],
                            cleanup_subject["path_id"],
                            cleanup_subject["path_attempt"],
                        )
                    )
                if cancellation_control is not None:
                    cancellation_observed = cancellation_observed or all(
                        cancellation_control.get(field) == value
                        for field, value in cleanup_subject.items()
                    )
                with self._control_lock:
                    control = self._request_controls.get(
                        cleanup_subject["request_id"]
                    )
                    if control is None:
                        # Cancel-before-start: this node received the owner's
                        # infer_cancel before any infer_start/infer_decode
                        # bound a request control (e.g. cancellation won the
                        # race with the first stage's prefill). The recorded
                        # pending cancellation is the authoritative subject
                        # identity; seed the control from it so the standard
                        # immutable-field and generation verification below
                        # applies exactly as it does for started requests.
                        pending = self._pending_cancellations.get(
                            cleanup_subject["request_id"]
                        )
                        if pending is not None and all(
                            pending.get(field) == value
                            for field, value in cleanup_subject.items()
                            if field != "cancellation_generation"
                        ):
                            control = {
                                key: value
                                for key, value in pending.items()
                                if key != "deadline_budget_ms"
                            }
                            control["cancellation_generation"] = (
                                cleanup_subject["cancellation_generation"]
                            )
                            self._request_controls[
                                cleanup_subject["request_id"]
                            ] = dict(control)
                    immutable_fields = _REQUEST_CONTROL_FIELDS - {
                        "cancellation_generation"
                    }
                    if (
                        control is not None
                        and cleanup_subject["cancellation_generation"]
                        == control["cancellation_generation"] + 1
                    ):
                        _require(
                            cancellation_observed,
                            "cleanup_cancellation_generation_unproven",
                        )
                        control["cancellation_generation"] = cleanup_subject[
                            "cancellation_generation"
                        ]
                    _require(
                        control is not None
                        and all(
                            cleanup_subject[field] == control[field]
                            for field in immutable_fields
                        ),
                        "cleanup_subject_generation_mismatch",
                    )
                    _require(
                        cleanup_subject["cancellation_generation"]
                        == control["cancellation_generation"],
                        "cleanup_cancellation_generation_mismatch",
                    )
                    receipt = dict(details["request_cleanup"])
                    if len(self._request_cleanup_receipts) >= 256:
                        oldest_request_id = next(iter(self._request_cleanup_receipts))
                        self._request_cleanup_receipts.pop(oldest_request_id, None)
                    self._request_cleanup_receipts[
                        cleanup_subject["request_id"]
                    ] = receipt
                    self._request_controls.pop(
                        cleanup_subject["request_id"], None
                    )
                    self._pending_cancellations.pop(
                        cleanup_subject["request_id"], None
                    )
                    self._cancellation_controls.pop(
                        cleanup_subject["request_id"], None
                    )
                self._sinks.pop(cleanup_subject["request_id"], None)
        return self._signed_result("snapshot", details)

    def _inbound_admission_snapshot(
        self, payload: dict[str, Any]
    ) -> dict[str, Any]:
        fields = {
            "case_id",
            "member_id",
            "spec_digest",
            "source_digest",
            "challenge",
            "expected_endpoint_id",
            "dialed_endpoint_id",
        }
        data = _exact_fields(payload, fields, "invalid_inbound_admission_fields")
        _require(
            self.state == "RUNNING" and self.transport is not None,
            "invalid_state_for_inbound_admission_snapshot",
        )
        _require(
            data["case_id"] == "endpoint_identity_mismatch"
            and all(
                isinstance(data[field], str) and bool(data[field])
                for field in fields
            )
            and data["expected_endpoint_id"] != data["dialed_endpoint_id"]
            and re.fullmatch(r"sha256:[0-9a-f]{64}", data["spec_digest"])
            is not None
            and re.fullmatch(r"sha256:[0-9a-f]{64}", data["source_digest"])
            is not None
            and re.fullmatch(r"[A-Za-z0-9._:-]{16,256}", data["challenge"])
            is not None,
            "invalid_inbound_admission_identity",
        )
        admission = self.transport.inbound_admission_snapshot(
            data["dialed_endpoint_id"]
        )
        paths = self.transport.evidence().transport_path_observations
        expected_paths = [
            path
            for path in paths
            if path.get("remote_endpoint_id") == data["expected_endpoint_id"]
        ]
        _require(
            all(path.get("path_class") == "unknown" for path in expected_paths),
            "inbound_admission_path_resolved",
        )
        details = {
            "protocol": "mycelium.physical_node.inbound_admission_evidence.v1",
            "case_id": data["case_id"],
            "member_id": data["member_id"],
            "spec_digest": data["spec_digest"],
            "source_digest": data["source_digest"],
            "sidecar_binary_digest": "sha256:"
            + hashlib.sha256(self.sidecar_binary.read_bytes()).hexdigest(),
            "challenge": data["challenge"],
            "expected_endpoint_id": data["expected_endpoint_id"],
            "dialed_endpoint_id": data["dialed_endpoint_id"],
            "expected_peer_path_class": "unknown",
            "admission": dict(admission),
        }
        return self._signed_result("inbound_admission_snapshot", details)

    def _cancel(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = _exact_fields(payload, {"request_id"}, "invalid_cancel_fields")
        _require(
            isinstance(data["request_id"], str) and bool(data["request_id"]),
            "invalid_request_id",
        )
        _require(
            self.state == "RUNNING" and self.router is not None,
            "invalid_state_for_cancel",
        )
        request_id = data["request_id"]
        router = self.router
        assert router is not None
        path_id: str | None = None
        path_attempt: int | None = None
        status_before: str | None = None
        pre_cancel_token_count = 0
        try:
            record = router.get_request(request_id)
            path_id = record.manifest.path_id
            path_attempt = record.manifest.path_attempt
            status_before = record.status
            pre_cancel_token_count = len(record.generated_token_ids)
        except (KeyError, AttributeError):
            pass
        cancelled = router.cancel(request_id)
        try:
            status_after = router.request_status(request_id)
        except KeyError:
            status_after = "UNKNOWN"
        sink = self._sinks.get(request_id)
        observed_token_count = (
            len(sink.token_ids) if sink is not None else pre_cancel_token_count
        )
        post_cancel_token_count = max(0, observed_token_count - pre_cancel_token_count)
        result = {
            "cancelled": bool(cancelled),
            "path_id": path_id,
            "path_attempt": path_attempt,
            "status_before": status_before,
            "status_after": status_after,
            "pre_cancel_token_count": pre_cancel_token_count,
            "post_cancel_token_count": post_cancel_token_count,
        }
        if cancelled and isinstance(path_id, str) and isinstance(path_attempt, int):
            self._last_cancellation = {
                "request_id": request_id,
                "path_id": path_id,
                "path_attempt": path_attempt,
            }
            self._cancellations_by_subject[
                (request_id, path_id, path_attempt)
            ] = dict(self._last_cancellation)
        return self._signed_result(
            "cancelled", {"request_id": request_id, "result": _plain_json(result)}
        )

    def _infer_start(self, payload: dict[str, Any]) -> dict[str, Any]:
        _require(
            set(payload)
            in (
                {"request"},
                {"request", "excluded_placement_ids"},
                {"request", "control", "path_manifest"},
                {
                    "request",
                    "control",
                    "path_manifest",
                    "excluded_placement_ids",
                },
            ),
            "invalid_infer_start_fields",
        )
        data = payload
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
        _require(
            isinstance(request_data["prompt_token_ids"], list),
            "invalid_prompt_token_ids",
        )
        request = RequestContext(
            **{
                **request_data,
                "prompt_token_ids": tuple(request_data["prompt_token_ids"]),
                "admitted_at": self._clock.now(),
            }
        )
        _require(
            self.state == "RUNNING" and self.router is not None,
            "invalid_state_for_infer_start",
        )
        excluded_placement_ids = data.get("excluded_placement_ids", [])
        _require(
            isinstance(excluded_placement_ids, list)
            and all(
                isinstance(item, str) and bool(item)
                for item in excluded_placement_ids
            )
            and len(excluded_placement_ids) == len(set(excluded_placement_ids)),
            "invalid_excluded_placement_ids",
        )
        _require(request.request_id not in self._sinks, "duplicate_request_id")
        control_data = data.get("control")
        if control_data is not None:
            control = self._validated_request_control(
                control_data,
                code="invalid_infer_start_control",
                initial=True,
            )
            with self._control_lock:
                current = self._request_controls.get(request.request_id)
                immutable_fields = _REQUEST_CONTROL_FIELDS - {
                    "cancellation_generation"
                }
                cancellation_preceded_start = bool(
                    current is not None
                    and current["cancellation_generation"]
                    == control["cancellation_generation"] + 1
                    and all(current[field] == control[field] for field in immutable_fields)
                )
                _require(
                    current is None or current == control or cancellation_preceded_start,
                    "conflicting_request_control",
                )
                if not cancellation_preceded_start:
                    self._request_controls[request.request_id] = dict(control)
                pending_cancel = self._pending_cancellations.pop(
                    request.request_id, None
                )
                if cancellation_preceded_start or pending_cancel is not None:
                    if pending_cancel is None:
                        pending_cancel = current
                    assert pending_cancel is not None
                    _require(
                        all(
                            pending_cancel[field] == control[field]
                            for field in (
                                "deployment_id",
                                "deployment_epoch",
                                "qualification_digest",
                                "command_id",
                                "publisher_generation",
                                "absolute_deadline_ms",
                                "request_attempt",
                                "path_id",
                                "path_attempt",
                                "path_digest",
                                "topology_generation",
                            )
                        )
                        and pending_cancel["cancellation_generation"] == 1,
                        "stale_infer_cancel_generation",
                    )
                    self._request_controls[request.request_id][
                        "cancellation_generation"
                    ] = 1
                    cancellation_controls = getattr(
                        self,
                        "_cancellation_controls",
                        None,
                    )
                    if cancellation_controls is None:
                        cancellation_controls = {}
                        self._cancellation_controls = cancellation_controls
                    cancellation_controls[request.request_id] = {
                        key: value
                        for key, value in pending_cancel.items()
                        if key != "deadline_budget_ms"
                    }
                    return self._signed_result(
                        "inference_started",
                        {
                            "request_id": request.request_id,
                            "status": "CANCELLED",
                            "output": {"token_indexes": [], "token_ids": []},
                            "path": None,
                        },
                    )
        sink = _CaptureSink()
        if control_data is None:
            request_id = self.router.start_distributed_prefill(
                request,
                sink,
                excluded_placements=frozenset(excluded_placement_ids),
            )
        else:
            _require(not excluded_placement_ids, "locked_path_cannot_exclude_placements")
            manifest_data = data.get("path_manifest")
            _require(isinstance(manifest_data, dict), "invalid_path_manifest")
            try:
                manifest = path_manifest_from_dict(manifest_data)
                _require(self.graph is not None, "missing_execution_graph")
                validate_manifest(manifest, self.graph)
            except (TypeError, ValueError):
                raise NodeCommandError("invalid_path_manifest") from None
            manifest_digest = "sha256:" + hashlib.sha256(
                json.dumps(
                    manifest_data,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            _require(
                manifest.request_id == request.request_id
                and manifest.path_id == control["path_id"]
                and manifest.path_attempt == control["path_attempt"]
                and manifest_digest == control["path_digest"],
                "path_manifest_identity_mismatch",
            )
            request_id = self.router.start_locked_distributed_prefill(
                request,
                sink,
                manifest=manifest,
                pinned_deployment=self.graph,
            )
        _require(request_id == request.request_id, "request_id_changed")
        self._sinks[request_id] = sink
        deadline = time.monotonic() + min(
            self.command_timeout * 0.8,
            _MAX_INFERENCE_COMPLETION_TIMEOUT_SECONDS,
        )
        status = self.router.request_status(request_id)
        while status in {"PREFILL", "LOCKED"} and time.monotonic() < deadline:
            if self.transport is not None and self.transport.fatal_error is not None:
                raise NodeCommandError(self.transport.fatal_error.code)
            time.sleep(0.01)
            status = self.router.request_status(request_id)
        _require(status not in {"PREFILL", "LOCKED"}, "prefill_completion_timeout")
        if getattr(self.runtime, "decode_mode", None) == "stage_local_kv":
            # PrefillChunkCompleted and the first TokenEvent are separately
            # ordered physical frames. The former may transition the entry to
            # DECODING just before the latter reaches its sink. Stage-local KV
            # cannot dispatch decode until that first generated token exists,
            # so wait for the correlated event instead of issuing an invalid
            # early decode command.
            while (
                status == "DECODING"
                and not sink.token_ids
                and time.monotonic() < deadline
            ):
                if (
                    self.transport is not None
                    and self.transport.fatal_error is not None
                ):
                    raise NodeCommandError(self.transport.fatal_error.code)
                time.sleep(0.01)
                status = self.router.request_status(request_id)
            _require(
                bool(sink.token_ids) or status == "COMPLETED",
                "prefill_token_timeout",
            )
        record = self.router.get_request(request_id)
        manifest = record.manifest
        return self._signed_result(
            "inference_started",
            {
                "request_id": request_id,
                "status": status,
                "output": sink.snapshot(),
                "path": {
                    "path_id": manifest.path_id,
                    "path_attempt": manifest.path_attempt,
                    "placement_ids": [
                        hop.placement_id for hop in manifest.ordered_hops
                    ],
                },
            },
        )

    def _infer_decode(self, payload: dict[str, Any]) -> dict[str, Any]:
        fields = set(payload)
        _require(
            fields in (
                {"request_id", "count"},
                {"request_id", "count", "control"},
            ),
            "invalid_infer_decode_fields",
        )
        data = payload
        _require(
            isinstance(data["request_id"], str) and bool(data["request_id"]),
            "invalid_request_id",
        )
        _require(
            isinstance(data["count"], int)
            and not isinstance(data["count"], bool)
            and 1 <= data["count"] <= 128,
            "invalid_decode_count",
        )
        _require(
            self.state == "RUNNING" and self.router is not None,
            "invalid_state_for_infer_decode",
        )
        sink = self._sinks.get(data["request_id"])
        _require(sink is not None, "unknown_request_id")
        if "control" in data:
            control = _exact_fields(
                data["control"],
                {
                    "deployment_id",
                    "deployment_epoch",
                    "qualification_digest",
                    "request_attempt",
                    "path_id",
                    "path_attempt",
                    "path_digest",
                    "topology_generation",
                    "command_id",
                    "cancellation_generation",
                    "publisher_generation",
                    "absolute_deadline_ms",
                },
                "invalid_infer_decode_control",
            )
            with self._control_lock:
                current = self._request_controls.get(data["request_id"])
                _require(
                    current is not None and control == current,
                    "stale_infer_decode_generation",
                )
        dispatched = 0
        for _ in range(data["count"]):
            output_count = len(sink.token_ids)
            if not self.router.decode_one_distributed(data["request_id"]):
                break
            dispatched += 1
            deadline = time.monotonic() + min(
                self.command_timeout * 0.8,
                _MAX_INFERENCE_COMPLETION_TIMEOUT_SECONDS,
            )
            status = self.router.request_status(data["request_id"])
            while (
                len(sink.token_ids) == output_count
                and status not in {"COMPLETED", "FAILED", "CANCELLED"}
                and time.monotonic() < deadline
            ):
                if (
                    self.transport is not None
                    and self.transport.fatal_error is not None
                ):
                    raise NodeCommandError(self.transport.fatal_error.code)
                time.sleep(0.01)
                status = self.router.request_status(data["request_id"])
            _require(
                len(sink.token_ids) > output_count
                or status in {"COMPLETED", "CANCELLED", "FAILED"},
                "decode_completion_timeout",
            )
        return self._signed_result(
            "inference_decoded",
            {
                "request_id": data["request_id"],
                "dispatched": dispatched,
                "status": self.router.request_status(data["request_id"]),
                "output": sink.snapshot(),
            },
        )

    def _infer_cancel(self, payload: dict[str, Any]) -> dict[str, Any]:
        fields = set(payload)
        _require(
            fields
            in (
                {"request_id"},
                {
                    "request_id",
                    "deployment_id",
                    "deployment_epoch",
                    "qualification_digest",
                    "command_id",
                    "publisher_generation",
                    "absolute_deadline_ms",
                    "request_attempt",
                    "path_id",
                    "path_attempt",
                    "path_digest",
                    "topology_generation",
                    "cancellation_generation",
                    "deadline_budget_ms",
                },
            ),
            "invalid_infer_cancel_fields",
        )
        data = payload
        _require(
            isinstance(data["request_id"], str) and bool(data["request_id"]),
            "invalid_request_id",
        )
        _require(
            self.state == "RUNNING" and self.router is not None,
            "invalid_state_for_infer_cancel",
        )
        if len(fields) > 1:
            cancel_control = self._validated_request_control(
                {
                    field: data[field]
                    for field in _REQUEST_CONTROL_FIELDS
                },
                code="invalid_infer_cancel_control",
                initial=False,
            )
            _require(
                cancel_control["cancellation_generation"] >= 1
                and type(data["deadline_budget_ms"]) is int
                and 1 <= data["deadline_budget_ms"] <= 2_000,
                "invalid_infer_cancel_control",
            )
            with self._control_lock:
                control = self._request_controls.get(data["request_id"])
                if control is None:
                    previous = self._pending_cancellations.get(data["request_id"])
                    _require(
                        previous is None or previous == dict(data),
                        "conflicting_pending_cancellation",
                    )
                    self._pending_cancellations[data["request_id"]] = dict(data)
                    return self._signed_result(
                        "inference_cancelled",
                        {
                            "request_id": data["request_id"],
                            "cancelled": True,
                            "status": "CANCELLED",
                            "pending_start": True,
                        },
                    )
                _require(
                    all(
                        data[field] == control[field]
                        for field in (
                            "deployment_id",
                            "deployment_epoch",
                            "qualification_digest",
                            "command_id",
                            "publisher_generation",
                            "absolute_deadline_ms",
                            "request_attempt",
                            "path_id",
                            "path_attempt",
                            "path_digest",
                            "topology_generation",
                        )
                    )
                    and data["cancellation_generation"]
                    == control["cancellation_generation"] + 1,
                    "stale_infer_cancel_generation",
                )
                control["cancellation_generation"] = data[
                    "cancellation_generation"
                ]
        cancelled = self.router.cancel(data["request_id"])
        transport = getattr(self, "transport", None)
        if not cancelled and len(fields) > 1 and transport is not None:
            transport.send_path_cancellation_if_entry(
                PathCancellation(
                    request_id=data["request_id"],
                    path_id=data["path_id"],
                    path_attempt=data["path_attempt"],
                    topology_version=data["topology_generation"],
                )
            )
        if len(fields) > 1:
            with self._control_lock:
                cancellation_controls = getattr(
                    self,
                    "_cancellation_controls",
                    None,
                )
                if cancellation_controls is None:
                    cancellation_controls = {}
                    self._cancellation_controls = cancellation_controls
                cancellation_controls[data["request_id"]] = {
                    key: value
                    for key, value in data.items()
                    if key != "deadline_budget_ms"
                }
        try:
            status = self.router.request_status(data["request_id"])
        except KeyError:
            # Successful Router cleanup may retire the request before this
            # command builds its observation.  Absence after the exact,
            # generation-fenced cancellation is the expected cancelled state;
            # cleanup snapshots still prove every request/path-scoped resource.
            status = "CANCELLED"
        return self._signed_result(
            "inference_cancelled",
            {
                "request_id": data["request_id"],
                "cancelled": bool(cancelled),
                "status": status,
            },
        )

    def _rotate(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = _exact_fields(payload, {"peer"}, "invalid_rotate_fields")
        peer_data = _exact_fields(
            data["peer"],
            {"node_id", "endpoint_id", "endpoint_addr", "generation"},
            "invalid_peer_fields",
        )
        _require(
            self.state == "RUNNING" and self.transport is not None,
            "invalid_state_for_rotate",
        )
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
        _require(
            document["protocol"] == NODE_CONTROL_PROTOCOL, "invalid_command_protocol"
        )
        _require(
            isinstance(document["command_id"], str) and bool(document["command_id"]),
            "invalid_command_id",
        )
        _require(document["run_id"] == self.run_id, "run_id_mismatch")
        _require(
            document["deployment_id"] == self.deployment_id, "deployment_id_mismatch"
        )
        _require(isinstance(document["command"], str), "invalid_command")
        _require(isinstance(document["payload"], dict), "invalid_command_payload")
        handlers = {
            "hello": self._hello,
            "configure": self._configure,
            "start": self._start,
            "health": self._health,
            "snapshot": self._snapshot,
            "inbound_admission_snapshot": self._inbound_admission_snapshot,
            "cancel": self._cancel,
            "bind_request_control": self._bind_request_control,
            "update_request_control": self._update_request_control,
            "infer_start": self._infer_start,
            "infer_decode": self._infer_decode,
            "infer_cancel": self._infer_cancel,
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
    network_mode = parser.add_mutually_exclusive_group()
    network_mode.add_argument("--sidecar-local-only", action="store_true")
    network_mode.add_argument("--sidecar-force-relay", action="store_true")
    parser.add_argument("--endpoint-secret-file", type=Path)
    parser.add_argument("--command-timeout", type=float, default=30.0)
    parser.add_argument(
        "--decode-mode",
        choices=("complete_context_replay", "stage_local_kv"),
    )
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
            sidecar_force_relay=args.sidecar_force_relay,
            command_timeout=args.command_timeout,
            endpoint_secret_file=args.endpoint_secret_file,
            requested_decode_mode=args.decode_mode,
        )
    except (NodeCommandError, OSError) as exc:
        print(f"physical-node startup rejected: {type(exc).__name__}", file=sys.stderr)
        return 2

    def stop_on_signal(signum: int, _frame: Any) -> None:
        service.close()
        raise SystemExit(128 + signum)

    prior_signal_handlers = {
        signum: signal.signal(signum, stop_on_signal)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }

    write_lock = threading.Lock()
    active_lock = threading.Lock()
    active_command_ids: set[str] = set()
    capacity = threading.BoundedSemaphore(16)

    def emit(document: dict[str, Any]) -> None:
        with write_lock:
            _emit(document)

    def execute(command: Any, command_id: str) -> None:
        try:
            # Long inference commands own absolute deadlines and cooperative
            # cancellation points. The stdin reader must stay free to accept
            # cancellation, cleanup, probes, and unrelated request commands.
            result = service.dispatch(command)
            emit(_response(service, command_id=command_id, ok=True, result=result))
        except NodeCommandError as exc:
            emit(_response(service, command_id=command_id, ok=False, error_code=exc.code))
        except BaseException as exc:
            print(
                f"physical-node command failed: {type(exc).__name__}",
                file=sys.stderr,
            )
            traceback.print_exc(file=sys.stderr)
            emit(
                _response(
                    service,
                    command_id=command_id,
                    ok=False,
                    error_code="node_command_failed",
                )
            )
        finally:
            with active_lock:
                active_command_ids.discard(command_id)
            capacity.release()

    executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="mycelium-node-command")
    try:
        for raw_line in sys.stdin.buffer:
            command_id = "unknown"
            if len(raw_line) > MAX_COMMAND_BYTES:
                emit(
                    _response(
                        service,
                        command_id=command_id,
                        ok=False,
                        error_code="command_too_large",
                    )
                )
                continue
            try:
                command = canonical_json_loads(raw_line.rstrip(b"\n"), path="stdin")
                if isinstance(command, dict) and isinstance(command.get("command_id"), str):
                    command_id = command["command_id"]
                if not capacity.acquire(blocking=False):
                    emit(
                        _response(
                            service,
                            command_id=command_id,
                            ok=False,
                            error_code="command_capacity_exhausted",
                        )
                    )
                    continue
                with active_lock:
                    if command_id in active_command_ids:
                        capacity.release()
                        emit(
                            _response(
                                service,
                                command_id=command_id,
                                ok=False,
                                error_code="duplicate_command_id",
                            )
                        )
                        continue
                    active_command_ids.add(command_id)
                executor.submit(execute, command, command_id)
                if (
                    isinstance(command, dict)
                    and command.get("command") == "stop"
                ):
                    break
            except NodeCommandError as exc:
                emit(_response(service, command_id=command_id, ok=False, error_code=exc.code))
            except BaseException as exc:
                print(
                    f"physical-node command failed: {type(exc).__name__}",
                    file=sys.stderr,
                )
                emit(
                    _response(
                        service,
                        command_id=command_id,
                        ok=False,
                        error_code="node_command_failed",
                    )
                )
        return 0
    finally:
        executor.shutdown(wait=True, cancel_futures=False)
        for signum, handler in prior_signal_handlers.items():
            signal.signal(signum, handler)
        service.close()


if __name__ == "__main__":
    raise SystemExit(main())
