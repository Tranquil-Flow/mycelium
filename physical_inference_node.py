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
from mycelium_router.transports.iroh import (
    IrohTransport,
    IrohTransportError,
    PeerBinding,
)
from mycelium_router.validation import validate_execution_graph, validate_manifest
from physical_sqlite_capacity import SQLiteQualificationCapacityPort

NODE_CONTROL_PROTOCOL = "mycelium.physical_node_control.v1"
NODE_OBSERVATION_PROTOCOL = "mycelium.physical_node_observation.v1"
CLEANUP_CONTROL_FRAME_PREFIX = b"MYCELIUM_CLEANUP_RESPONSE_V1 "
MAX_COMMAND_BYTES = 4 * 1024 * 1024
MAXIMUM_PHYSICAL_CLOCK_SKEW_SECONDS = 5.0
_MAX_IROH_DELIVERY_TIMEOUT_SECONDS = 240.0
_MAX_INFERENCE_COMPLETION_TIMEOUT_SECONDS = 240.0
_CANCEL_RECEIPT_POLL_SECONDS = 0.05
# Reserve one complete parent snapshot attempt inside the immutable two-second
# owner budget.  If node-local teardown has not sealed by this handoff, the
# correlated command returns its non-authoritative pending observation and the
# route can perform exactly one fresh fallback proof without extending the
# deadline or racing duplicate work for the whole interval.
_CANCEL_RECEIPT_FALLBACK_RESERVE_SECONDS = 0.5
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
_INFERENCE_COMMANDS = frozenset({"infer_start", "infer_decode"})


def _is_exact_owner_cancellation_successor(
    command_control: Mapping[str, Any],
    owner_control: object,
) -> bool:
    """Return whether owner cancellation monotonically overtook a command."""

    if not isinstance(owner_control, Mapping):
        return False
    immutable_fields = _REQUEST_CONTROL_FIELDS - {"cancellation_generation"}
    return bool(
        type(owner_control.get("cancellation_generation")) is int
        and type(command_control.get("cancellation_generation")) is int
        and owner_control["cancellation_generation"]
        > command_control["cancellation_generation"]
        and all(
            owner_control.get(field) == command_control.get(field)
            for field in immutable_fields
        )
    )


def _command_uses_inference_lane(document: object) -> bool:
    """Keep model execution out of the reserved cleanup/control lane."""

    return (
        isinstance(document, Mapping) and document.get("command") in _INFERENCE_COMMANDS
    )


def _command_uses_cleanup_response_priority(document: object) -> bool:
    """Return whether this response carries deadline-bound cleanup authority."""

    if not isinstance(document, Mapping):
        return False
    operation = document.get("command")
    if operation == "infer_cancel_wait":
        return True
    payload = document.get("payload")
    return bool(
        operation == "snapshot"
        and isinstance(payload, Mapping)
        and payload.get("receipt_only") is True
    )


def _owner_cancellation_interrupted_inference(
    service: object,
    document: object,
    error: BaseException,
) -> bool:
    """Recognize only the exact owner fence that overtook this command."""

    if not isinstance(error, IrohTransportError) or error.code != "path_cancelled":
        return False
    if not isinstance(document, Mapping):
        return False
    operation = document.get("command")
    payload = document.get("payload")
    if operation not in _INFERENCE_COMMANDS or not isinstance(payload, Mapping):
        return False
    if operation == "infer_start":
        request = payload.get("request")
        request_id = request.get("request_id") if isinstance(request, Mapping) else None
    else:
        request_id = payload.get("request_id")
    command_control = payload.get("control")
    if not isinstance(request_id, str) or not isinstance(command_control, Mapping):
        return False
    control_lock = getattr(service, "_control_lock", None)
    cancellation_controls = getattr(service, "_cancellation_controls", None)
    if control_lock is None or not isinstance(cancellation_controls, Mapping):
        return False
    with control_lock:
        owner_control = cancellation_controls.get(request_id)
        if not isinstance(owner_control, Mapping):
            # A cleanup snapshot retires transient cancellation-control state
            # as soon as exact runtime/transport absence is proved.  An
            # inference command already executing on another worker can then
            # unwind through its late ``path_cancelled`` exception.  The
            # bounded complete receipt is the monotonic successor authority;
            # use it to classify that already-fenced command instead of
            # turning a successful cancellation into request_failed_closed.
            receipts = getattr(service, "_request_cleanup_receipts", None)
            receipt = (
                receipts.get(request_id) if isinstance(receipts, Mapping) else None
            )
            if isinstance(receipt, Mapping) and receipt.get("complete") is True:
                owner_control = receipt
        if not isinstance(owner_control, Mapping):
            return False
        return _is_exact_owner_cancellation_successor(
            command_control,
            owner_control,
        )


def _age_cleanup_command_budget(
    document: object,
    *,
    queued_seconds: float,
) -> object:
    """Keep node-local receipt polling inside the frame's original budget."""

    if (
        not isinstance(document, Mapping)
        or document.get("command") != "infer_cancel_wait"
        or not isinstance(document.get("payload"), Mapping)
    ):
        return document
    payload = document["payload"]
    budget_ms = payload.get("deadline_budget_ms")
    if type(budget_ms) is not int or not 1 <= budget_ms <= 2_000:
        return document
    queued_ms = max(0, int(queued_seconds * 1_000.0))
    aged = dict(document)
    aged["payload"] = {
        **payload,
        # A command that reaches its reserved worker near expiry must still
        # apply the generation fence and take one exact snapshot, but it must
        # not start a new duration window after waiting in the stdin/executor
        # queue. The parent independently enforces the absolute owner deadline.
        "deadline_budget_ms": max(1, budget_ms - queued_ms),
    }
    return aged


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
        self._cancellations_by_subject: dict[tuple[str, str, int], dict[str, Any]] = {}
        self._control_lock = threading.RLock()
        self._request_controls: dict[str, dict[str, Any]] = {}
        self._pending_cancellations: dict[str, dict[str, Any]] = {}
        self._cancellation_controls: dict[str, dict[str, Any]] = {}
        self._cancellation_workers: dict[str, threading.Thread] = {}
        # ``infer_cancel_wait`` may wait briefly for the exact receipt produced
        # by its independently running teardown worker.  The event is only a
        # delivery optimization: the signed request-scoped receipt remains the
        # sole cleanup authority.
        self._cancellation_receipt_events: dict[str, threading.Event] = {}
        # A worker disappearing is not, by itself, cleanup proof.  Retain a
        # bounded request-scoped failure marker until the same generation-
        # fenced teardown succeeds.  Without this marker an exception in the
        # daemon worker's body was erased by its ``finally`` block and a later
        # snapshot could mistake "thread absent" for "teardown complete".
        self._cancellation_worker_errors: dict[str, str] = {}
        self._request_cleanup_receipts: dict[str, dict[str, Any]] = {}
        self._request_cleanup_receipt_counters: dict[str, dict[str, int]] = {}

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
            {mode for modes in decode_modes_by_architecture.values() for mode in modes}
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
            runtime_proof.get("runtime") if isinstance(runtime_proof, Mapping) else None
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

    def _bind_router_config_to_runtime(self, runtime: Any) -> None:
        """Make Router prefill framing follow the runtime's resolved mode.

        ``requested_decode_mode`` may be omitted in a physical operator plan.
        In that case the runtime resolves the architecture-supported mode while
        the service is configured.  Basing chunking only on the optional CLI
        request leaves an auto-selected stage-local-KV runtime receiving one
        unbounded prefill operation, defeating its cooperative cancellation
        boundary.
        """

        mode = getattr(runtime, "decode_mode", None)
        _require(
            mode in {"complete_context_replay", "stage_local_kv"},
            "invalid_runtime_decode_mode",
        )
        self._router_config = replace(
            self._router_config,
            prefill_chunk_size_tokens=8 if mode == "stage_local_kv" else 0,
        )

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
        self._bind_router_config_to_runtime(runtime)
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
            and re.fullmatch(r"sha256:[0-9a-f]{64}", control["path_digest"]) is not None
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
            stored_cleanup = self._request_cleanup_receipts.get(data["request_id"])
            if stored_cleanup is not None:
                # Owner cancellation may overtake a generation-0 bind on the
                # independent priority command lane and seal exact cleanup
                # before this ordinary response reaches the node.  The sealed
                # receipt is the monotonic successor of that bind identity:
                # validate it and acknowledge the bind without recreating any
                # request control after cleanup.  Forcing physical fanout to
                # wait for every bind response consumed the cancellation's
                # fixed two-second budget under concurrent admission even
                # though the node already had all immutable identity fields in
                # infer_cancel_wait.
                immutable_fields = _REQUEST_CONTROL_FIELDS - {"cancellation_generation"}
                _require(
                    stored_cleanup.get("complete") is True
                    and all(
                        stored_cleanup.get(field) == control[field]
                        for field in immutable_fields
                    )
                    and stored_cleanup.get("cancellation_generation")
                    == control["cancellation_generation"] + 1,
                    "request_already_cleaned",
                )
                bound_control = {
                    field: stored_cleanup[field] for field in _REQUEST_CONTROL_FIELDS
                }
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
            current = self._request_controls.get(data["request_id"])
            pending_cancel = self._pending_cancellations.get(data["request_id"])
            bound_control = dict(control)
            if current is not None:
                immutable_fields = _REQUEST_CONTROL_FIELDS - {"cancellation_generation"}
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
                + hashlib.sha256(canonical_json_bytes(bound_control)).hexdigest(),
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
        details: dict[str, Any] = {
            "state": self.state,
            "sidecar_process": (
                None if self.sidecar is None else self.sidecar.status()
            ),
            "transport_fatal_error": (
                None if fatal is None else {"code": fatal.code, "detail": fatal.detail}
            ),
            "transport_running": (
                False if self.transport is None else self.transport.running
            ),
        }
        if self.transport is not None:
            details["transport_counters"] = _plain_json(
                self.transport.counter_snapshot()
            )
        runtime = getattr(self, "runtime", None)
        operation_counters = getattr(runtime, "operation_counter_snapshot", None)
        if callable(operation_counters):
            details["runtime_counters"] = _plain_json(operation_counters())
        return self._signed_result(
            "health",
            details,
        )

    def _snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        _require(
            set(payload)
            in (
                set(),
                {"cleanup_subject"},
                {"cleanup_subject", "receipt_only"},
            ),
            "invalid_snapshot_fields",
        )
        cleanup_subject = payload.get("cleanup_subject")
        receipt_only = payload.get("receipt_only", False)
        _require(type(receipt_only) is bool, "invalid_snapshot_fields")
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
        if receipt_only and cleanup_subject is not None:
            # A committed receipt is the monotonic successor of every mutable
            # runtime/transport observation for this exact subject.  Check it
            # before touching even the nonblocking probe path: under sustained
            # node work, a fallback command used to spend its entire bounded
            # response attempt observing unrelated state even though the
            # lifecycle worker had already sealed the requested receipt.
            with self._control_lock:
                stored_cleanup = self._request_cleanup_receipts.get(
                    cleanup_subject["request_id"]
                )
                stored_counters = getattr(
                    self,
                    "_request_cleanup_receipt_counters",
                    {},
                ).get(cleanup_subject["request_id"])
            if stored_cleanup is not None:
                _require(
                    all(
                        stored_cleanup.get(field) == value
                        for field, value in cleanup_subject.items()
                    ),
                    "cleanup_receipt_identity_mismatch",
                )
                details = {"request_cleanup": dict(stored_cleanup)}
                if isinstance(stored_counters, Mapping):
                    details["transport_counters"] = dict(stored_counters)
                elif self.transport is not None:
                    # Compatibility for a receipt restored or injected without
                    # the co-committed counter projection. Production receipt
                    # sealing always stores the exact counters observed in the
                    # same snapshot below, so the fast path does not reacquire
                    # mutable transport state under load.
                    details["transport_counters"] = _plain_json(
                        self.transport.counter_snapshot()
                    )
                return self._signed_result("snapshot", details)
        runtime_observation_complete = True
        runtime_subject_clean: bool | None = None
        if receipt_only and cleanup_subject is not None:
            subject_probe = getattr(self.runtime, "kv_subject_clean", None)
            if callable(subject_probe):
                runtime_subject_clean = bool(
                    subject_probe(
                        cleanup_subject["request_id"],
                        cleanup_subject["path_id"],
                        cleanup_subject["path_attempt"],
                    )
                )
            nonblocking_snapshot = getattr(
                self.runtime, "kv_snapshot_nonblocking", None
            )
            runtime_snapshot = (
                nonblocking_snapshot()
                if callable(nonblocking_snapshot)
                else self.runtime.kv_snapshot()
            )
            if runtime_snapshot is None:
                runtime_observation_complete = False
                runtime_snapshot = {
                    "mode": getattr(self.runtime, "decode_mode", None),
                    "states": {},
                    "active_state_count": 1,
                    "active_kv_bytes": 0,
                    "cleanup_observation_pending": True,
                }
        else:
            runtime_snapshot = self.runtime.kv_snapshot()
        details: dict[str, Any] = {
            "runtime": runtime_snapshot,
            "runtime_cleanup_observation_complete": runtime_observation_complete,
        }
        cancellation = self._last_cancellation
        transport_receipt_observation: (
            tuple[Mapping[str, Any], Mapping[str, Any]] | None
        ) = None
        transport_observation_complete = True
        if receipt_only and self.transport is not None:
            nonblocking_transport_observation = getattr(
                self.transport,
                "cancellation_cleanup_observation_nonblocking",
                None,
            )
            if callable(nonblocking_transport_observation):
                transport_receipt_observation = nonblocking_transport_observation(
                    cleanup_subject["request_id"],
                    cleanup_subject["path_id"],
                    cleanup_subject["path_attempt"],
                )
                if transport_receipt_observation is None:
                    transport_observation_complete = False
                else:
                    details["transport_counters"] = _plain_json(
                        transport_receipt_observation[0]
                    )
            else:
                details["transport_counters"] = _plain_json(
                    self.transport.counter_snapshot()
                )
        details["transport_cleanup_observation_complete"] = (
            transport_observation_complete
        )
        if not receipt_only:
            details.update(
                {
                    "capacity": None
                    if self.capacity is None
                    else _plain_json(self.capacity.snapshot()),
                    "host_resources": self._host_resources(),
                    "transport": None,
                    "sidecar_process": (
                        None if self.sidecar is None else self.sidecar.status()
                    ),
                }
            )
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
                "backend_candidate": details["runtime"].get("mode") == "stage_local_kv",
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
            if self.transport is not None:
                details["transport"] = _plain_json(self.transport.evidence())
                fatal = self.transport.fatal_error
                details["transport_fatal_error"] = (
                    None
                    if fatal is None
                    else {"code": fatal.code, "detail": fatal.detail}
                )
                details["transport_worker_threads"] = (
                    self.transport.worker_threads_alive
                )
                details["transport_dispatcher_phase"] = self.transport.dispatcher_phase
                details["transport_last_dispatch_error"] = (
                    self.transport.last_dispatch_error
                )
                details["transport_outbound_trace"] = list(
                    self.transport.outbound_trace
                )
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
            if runtime_subject_clean is None:
                runtime_subject_clean = runtime_observation_complete and not any(
                    path_id == cleanup_subject["path_id"]
                    and state.get("request_id") == cleanup_subject["request_id"]
                    and state.get("path_attempt") == cleanup_subject["path_attempt"]
                    for path_id, state in runtime_states.items()
                )
            transport_state: Mapping[str, Any] | None = None
            if self.transport is None:
                transport_subject_clean = True
            elif receipt_only and not transport_observation_complete:
                transport_subject_clean = False
            elif transport_receipt_observation is not None:
                transport_state = transport_receipt_observation[1]
                transport_subject_clean = all(
                    value in (0, False)
                    for key, value in transport_state.items()
                    if key != "cancellation_observed"
                )
            else:
                # One lock-bounded transport snapshot owns both the cleanup
                # decision and its signed blocker projection. Calling the
                # boolean probe and state probe separately allowed a racing
                # path lifecycle to produce internally contradictory evidence
                # from two different instants. A receipt must describe exactly
                # one observed state.
                transport_state = self.transport.cancellation_cleanup_state(
                    cleanup_subject["request_id"],
                    cleanup_subject["path_id"],
                    cleanup_subject["path_attempt"],
                )
                transport_subject_clean = all(
                    value in (0, False)
                    for key, value in transport_state.items()
                    if key != "cancellation_observed"
                )
            with self._control_lock:
                cancellation_worker = getattr(self, "_cancellation_workers", {}).get(
                    cleanup_subject["request_id"]
                )
                cancellation_worker_error = getattr(
                    self,
                    "_cancellation_worker_errors",
                    {},
                ).get(cleanup_subject["request_id"])
            cancellation_worker_complete = (
                cancellation_worker is None or not cancellation_worker.is_alive()
            ) and cancellation_worker_error is None
            resource_cleanup_complete = (
                runtime_subject_clean
                and transport_subject_clean
                and cancellation_worker_complete
            )
            details["request_cleanup"] = {
                **cleanup_subject,
                "runtime_clean": runtime_subject_clean,
                "transport_clean": transport_subject_clean,
                "cancellation_worker_complete": cancellation_worker_complete,
                # Resource absence alone cannot prove that the owner-issued
                # cancellation generation was applied.  A receipt becomes
                # complete only after the control proof below also succeeds.
                "complete": False,
            }
            if cancellation_worker_error is not None:
                details["request_cleanup"]["cancellation_worker_error"] = (
                    cancellation_worker_error
                )
            if transport_state is not None:
                details["request_cleanup"]["transport_state"] = dict(transport_state)
            if resource_cleanup_complete:
                with self._control_lock:
                    cancellation_control = self._cancellation_controls.get(
                        cleanup_subject["request_id"]
                    )
                    pending_cancellation = self._pending_cancellations.get(
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
                if pending_cancellation is not None:
                    cancellation_observed = cancellation_observed or all(
                        pending_cancellation.get(field) == value
                        for field, value in cleanup_subject.items()
                    )
                with self._control_lock:
                    control = self._request_controls.get(cleanup_subject["request_id"])
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
                            control["cancellation_generation"] = cleanup_subject[
                                "cancellation_generation"
                            ]
                            self._request_controls[cleanup_subject["request_id"]] = (
                                dict(control)
                            )
                    immutable_fields = _REQUEST_CONTROL_FIELDS - {
                        "cancellation_generation"
                    }
                    cancellation_generation_proven = False
                    if (
                        control is not None
                        and cleanup_subject["cancellation_generation"]
                        == control["cancellation_generation"] + 1
                    ):
                        if cancellation_observed:
                            control["cancellation_generation"] = cleanup_subject[
                                "cancellation_generation"
                            ]
                            cancellation_generation_proven = True
                    elif (
                        control is not None
                        and cleanup_subject["cancellation_generation"]
                        == control["cancellation_generation"]
                    ):
                        cancellation_generation_proven = (
                            cleanup_subject["cancellation_generation"] == 0
                            or cancellation_observed
                        )
                    if control is not None:
                        _require(
                            all(
                                cleanup_subject[field] == control[field]
                                for field in immutable_fields
                            ),
                            "cleanup_subject_generation_mismatch",
                        )
                    if cancellation_generation_proven:
                        details["request_cleanup"]["complete"] = True
                        receipt = dict(details["request_cleanup"])
                        if len(self._request_cleanup_receipts) >= 256:
                            oldest_request_id = next(
                                iter(self._request_cleanup_receipts)
                            )
                            self._request_cleanup_receipts.pop(oldest_request_id, None)
                            getattr(
                                self,
                                "_request_cleanup_receipt_counters",
                                {},
                            ).pop(oldest_request_id, None)
                        self._request_cleanup_receipts[
                            cleanup_subject["request_id"]
                        ] = receipt
                        transport_counters = details.get("transport_counters")
                        if isinstance(transport_counters, Mapping):
                            receipt_counters = getattr(
                                self,
                                "_request_cleanup_receipt_counters",
                                None,
                            )
                            if receipt_counters is None:
                                receipt_counters = {}
                                self._request_cleanup_receipt_counters = (
                                    receipt_counters
                                )
                            receipt_counters[cleanup_subject["request_id"]] = {
                                str(key): int(value)
                                for key, value in transport_counters.items()
                                if type(value) is int
                            }
                        self._request_controls.pop(cleanup_subject["request_id"], None)
                        self._pending_cancellations.pop(
                            cleanup_subject["request_id"], None
                        )
                        self._cancellation_controls.pop(
                            cleanup_subject["request_id"], None
                        )
                if details["request_cleanup"]["complete"] is True:
                    self._sinks.pop(cleanup_subject["request_id"], None)
            # A route fallback snapshot can begin immediately after a pending
            # ``infer_cancel_wait`` response. Both observers may have passed
            # the initial receipt lookup before either commits cleanup. The
            # winner retires request control state; without this final
            # monotonic read, the loser can then sign its stale
            # ``complete=False`` projection even though an authoritative
            # receipt already exists. Always project the committed receipt so
            # cleanup proof cannot move backwards across adjacent observers.
            with self._control_lock:
                committed_cleanup = self._request_cleanup_receipts.get(
                    cleanup_subject["request_id"]
                )
            if committed_cleanup is not None:
                _require(
                    all(
                        committed_cleanup.get(field) == value
                        for field, value in cleanup_subject.items()
                    ),
                    "cleanup_receipt_identity_mismatch",
                )
                details["request_cleanup"] = dict(committed_cleanup)
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
            self._cancellations_by_subject[(request_id, path_id, path_attempt)] = dict(
                self._last_cancellation
            )
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
                isinstance(item, str) and bool(item) for item in excluded_placement_ids
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
                immutable_fields = _REQUEST_CONTROL_FIELDS - {"cancellation_generation"}
                cancellation_preceded_start = bool(
                    current is not None
                    and current["cancellation_generation"]
                    == control["cancellation_generation"] + 1
                    and all(
                        current[field] == control[field] for field in immutable_fields
                    )
                )
                _require(
                    current is None
                    or current == control
                    or cancellation_preceded_start,
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
            _require(
                not excluded_placement_ids, "locked_path_cannot_exclude_placements"
            )
            manifest_data = data.get("path_manifest")
            _require(isinstance(manifest_data, dict), "invalid_path_manifest")
            try:
                manifest = path_manifest_from_dict(manifest_data)
                _require(self.graph is not None, "missing_execution_graph")
                validate_manifest(manifest, self.graph)
            except (TypeError, ValueError):
                raise NodeCommandError("invalid_path_manifest") from None
            manifest_digest = (
                "sha256:"
                + hashlib.sha256(
                    json.dumps(
                        manifest_data,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
            )
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
            if status == "CANCELLED":
                # Owner cancellation can overtake the separately ordered
                # first TokenEvent after prefill has already transitioned the
                # Router to DECODING.  That is the same valid cancellation
                # terminal as cancel-before-start, not a missing-token
                # failure.  Do not dereference the Router record here: exact
                # generation-fenced teardown may already have retired it.
                return self._signed_result(
                    "inference_started",
                    {
                        "request_id": request_id,
                        "status": "CANCELLED",
                        "output": sink.snapshot(),
                        "path": None,
                    },
                )
            _require(
                bool(sink.token_ids) or status == "COMPLETED",
                "prefill_token_timeout",
            )
        record = self.router.get_request(request_id)
        manifest = record.manifest
        if control_data is None and status in {"COMPLETED", "FAILED", "CANCELLED"}:
            self._retire_legacy_terminal_path(request_id)
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
            fields
            in (
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
        request_id = data["request_id"]
        command_control: dict[str, Any] | None = None
        owner_cancellation_overtook_decode = False
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
                current = self._request_controls.get(request_id)
                owner_control: object = current
                if current is None:
                    receipt = self._request_cleanup_receipts.get(request_id)
                    if (
                        isinstance(receipt, Mapping)
                        and receipt.get("complete") is True
                    ):
                        owner_control = receipt
                owner_cancellation_overtook_decode = (
                    _is_exact_owner_cancellation_successor(control, owner_control)
                )
                _require(
                    current == control or owner_cancellation_overtook_decode,
                    "stale_infer_decode_generation",
                )
            command_control = dict(control)
        sink = self._sinks.get(request_id)
        if sink is None:
            _require(
                owner_cancellation_overtook_decode,
                "unknown_request_id",
            )

        def cancelled_decode_result() -> dict[str, Any]:
            # Owner cancellation can advance before a queued decode enters the
            # inference lane, while it is inside transport dispatch, or just
            # before its terminal status read.  In each ordering, wait only
            # inside this command's existing completion budget for the Router
            # teardown that the exact successor generation authorized.
            deadline = time.monotonic() + min(
                self.command_timeout * 0.8,
                _MAX_INFERENCE_COMPLETION_TIMEOUT_SECONDS,
            )
            while True:
                try:
                    status = self.router.request_status(request_id)
                except KeyError:
                    status = "CANCELLED"
                if status in {"COMPLETED", "FAILED", "CANCELLED"}:
                    break
                _require(
                    time.monotonic() < deadline,
                    "decode_cancellation_completion_timeout",
                )
                time.sleep(0.01)
            return self._signed_result(
                "inference_decoded",
                {
                    "request_id": request_id,
                    "dispatched": 0,
                    "status": status,
                    "output": (
                        sink.snapshot()
                        if sink is not None
                        else {"token_indexes": [], "token_ids": []}
                    ),
                },
            )

        if owner_cancellation_overtook_decode:
            return cancelled_decode_result()

        assert sink is not None
        dispatched = 0
        for _ in range(data["count"]):
            output_count = len(sink.token_ids)
            try:
                decode_dispatched = self.router.decode_one_distributed(
                    data["request_id"]
                )
            except IrohTransportError as exc:
                if exc.code != "path_cancelled" or command_control is None:
                    raise
                with self._control_lock:
                    current = self._request_controls.get(request_id)
                    owner_control = current
                    if current is None:
                        receipt = self._request_cleanup_receipts.get(request_id)
                        if (
                            isinstance(receipt, Mapping)
                            and receipt.get("complete") is True
                        ):
                            owner_control = receipt
                    owner_cancellation_overtook_decode = (
                        _is_exact_owner_cancellation_successor(
                            command_control,
                            owner_control,
                        )
                    )
                if not owner_cancellation_overtook_decode:
                    raise
                return cancelled_decode_result()
            if not decode_dispatched:
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
        terminal_status = self.router.request_status(data["request_id"])
        if command_control is None and terminal_status in {
            "COMPLETED",
            "FAILED",
            "CANCELLED",
        }:
            self._retire_legacy_terminal_path(data["request_id"])
        return self._signed_result(
            "inference_decoded",
            {
                "request_id": data["request_id"],
                "dispatched": dispatched,
                "status": terminal_status,
                "output": sink.snapshot(),
            },
        )

    def _retire_legacy_terminal_path(self, request_id: str) -> None:
        """Retire a completed legacy qualification path before acknowledging it.

        Product inference owns generation-fenced cleanup through
        ``infer_cancel_wait`` on every participant. The startup qualifier is the
        sole legacy caller: it has no product control identity, but its terminal
        Router record still contains the exact request/path attempt needed to
        release remote relay state and mirrored capacity. Dispatch that existing
        entry-authoritative teardown and wait for its delivery receipt before the
        legacy command returns. This prevents route shutdown from racing the
        asynchronous cancellation worker and leaving persistent capacity behind.
        """

        router = self.router
        transport = self.transport
        _require(
            router is not None and transport is not None,
            "legacy_cleanup_unavailable",
        )
        try:
            record = router.get_request(request_id)
        except KeyError:
            return
        _require(
            record.status in {"COMPLETED", "FAILED", "CANCELLED"},
            "legacy_cleanup_before_terminal",
        )
        manifest = record.manifest
        cancellation = PathCancellation(
            request_id=request_id,
            path_id=manifest.path_id,
            path_attempt=manifest.path_attempt,
            topology_version=manifest.topology_version,
        )
        transport.send_path_cancellation_if_entry(cancellation)
        deadline = time.monotonic() + min(2.0, self.command_timeout * 0.8)
        while not transport.cancellation_cleanup_complete(
            request_id,
            manifest.path_id,
            manifest.path_attempt,
        ):
            fatal = transport.fatal_error
            if fatal is not None:
                raise NodeCommandError(fatal.code)
            _require(
                time.monotonic() < deadline,
                "legacy_terminal_cleanup_timeout",
            )
            time.sleep(0.01)

    def _apply_generation_fenced_cancellation(self, data: Mapping[str, Any]) -> None:
        transport = getattr(self, "transport", None)
        deadline_budget_ms = data.get("deadline_budget_ms")
        cleanup_deadline_monotonic_s = (
            None
            if type(deadline_budget_ms) is not int
            else time.monotonic()
            + max(
                0.0,
                deadline_budget_ms / 1_000.0 - _CANCEL_RECEIPT_FALLBACK_RESERVE_SECONDS,
            )
        )
        # Fence model execution before touching Router/relay ownership. Entry
        # record teardown and participant path teardown may briefly wait on
        # their own subject locks; the runtime marker must already be visible
        # so active MLX/NumPy work exits at its next per-layer checkpoint.
        # Repeating runtime.cancel later through relay release is idempotent.
        runtime_cancel = getattr(getattr(self, "runtime", None), "cancel", None)
        if callable(runtime_cancel):
            runtime_cancel(data["path_id"])
        controlled_cancellation = PathCancellation(
            request_id=data["request_id"],
            path_id=data["path_id"],
            path_attempt=data["path_attempt"],
            topology_version=data["topology_generation"],
        )
        # Fence and interrupt exact-subject transport work before Router
        # teardown. A send can hold RelayEngine's per-path operation lock;
        # calling cancel_local first would then wait behind the very send that
        # controlled transport cancellation is responsible for interrupting.
        # This ordering is runtime fence -> transport fence -> Router release.
        apply_controlled = getattr(
            transport,
            "apply_controlled_path_cancellation",
            None,
        )
        transport_cancelled = False
        if callable(apply_controlled):
            transport_cancelled = bool(
                apply_controlled(
                    controlled_cancellation,
                    entry_cancelled=False,
                    cleanup_deadline_monotonic_s=cleanup_deadline_monotonic_s,
                )
            )
        cancel_local = getattr(self.router, "cancel_local", None)
        if callable(cancel_local):
            router_cancelled = bool(cancel_local(data["request_id"]))
            cancelled = transport_cancelled or router_cancelled
        else:
            cancelled = bool(self.router.cancel(data["request_id"]))
        if not cancelled and transport is not None:
            transport.send_path_cancellation_if_entry(controlled_cancellation)

    @staticmethod
    def _cancellation_worker_error_code(error: BaseException) -> str:
        """Return a bounded non-payload diagnostic for fail-closed receipts."""

        error_type = type(error).__name__
        code = getattr(error, "code", None)
        if not isinstance(code, str) or not code:
            code = "unclassified"
        return f"{error_type}:{code}"[:128]

    def _cancellation_resources_clean(self, data: Mapping[str, Any]) -> bool:
        """Return whether the exact runtime and transport subject is released.

        This deliberately excludes the cancellation worker marker: this helper is
        called by that worker while it still owns teardown.  Router teardown is
        synchronous; the two lower layers are the ones that can finish or fail
        asynchronously after a sweep was successfully dispatched.
        """

        request_id = str(data["request_id"])
        path_id = str(data["path_id"])
        path_attempt = int(data["path_attempt"])

        runtime_clean = True
        runtime_subject_clean = getattr(
            getattr(self, "runtime", None),
            "kv_subject_clean",
            None,
        )
        if callable(runtime_subject_clean):
            runtime_clean = bool(
                runtime_subject_clean(request_id, path_id, path_attempt)
            )

        transport_clean = True
        transport_subject_clean = getattr(
            getattr(self, "transport", None),
            "cancellation_cleanup_complete",
            None,
        )
        if callable(transport_subject_clean):
            transport_clean = bool(
                transport_subject_clean(request_id, path_id, path_attempt)
            )

        return runtime_clean and transport_clean

    def _apply_cancellation_until_deadline(self, data: Mapping[str, Any]) -> None:
        """Retry one idempotent teardown inside the original owner budget.

        The cancellation fence is monotonic and every release operation is
        idempotent.  Retrying the same exact identity is therefore the correct
        recovery when a transient lower-layer release raises after publishing
        only part of teardown *or* when asynchronous delivery cancellation
        returns from dispatch before the exact subject is clean.  The retry
        window is derived from the aged ``deadline_budget_ms`` received from the
        owner and preserves the same fallback reserve used by
        ``infer_cancel_wait``; it never extends the frozen two-second protocol
        deadline.
        """

        request_id = str(data["request_id"])
        deadline_budget_ms = data.get("deadline_budget_ms")
        retry_deadline = (
            time.monotonic()
            if type(deadline_budget_ms) is not int
            else time.monotonic()
            + max(
                0.0,
                deadline_budget_ms / 1_000.0 - _CANCEL_RECEIPT_FALLBACK_RESERVE_SECONDS,
            )
        )
        attempt = dict(data)
        while True:
            try:
                self._apply_generation_fenced_cancellation(attempt)
                with self._control_lock:
                    getattr(self, "_cancellation_worker_errors", {}).pop(
                        request_id,
                        None,
                    )
                if self._cancellation_resources_clean(attempt):
                    return
            except Exception as error:
                with self._control_lock:
                    errors = getattr(self, "_cancellation_worker_errors", None)
                    if errors is None:
                        errors = {}
                        self._cancellation_worker_errors = errors
                    errors[request_id] = self._cancellation_worker_error_code(error)
                    while len(errors) > 256:
                        errors.pop(next(iter(errors)), None)
            remaining = retry_deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(_CANCEL_RECEIPT_POLL_SECONDS, remaining))
            # ``_apply_generation_fenced_cancellation`` passes an absolute
            # deadline to transport after subtracting the fixed fallback
            # reserve.  Re-age the duration so every retry observes the same
            # original deadline rather than receiving a fresh one.
            remaining = max(0.0, retry_deadline - time.monotonic())
            attempt["deadline_budget_ms"] = max(
                1,
                int((remaining + _CANCEL_RECEIPT_FALLBACK_RESERVE_SECONDS) * 1_000),
            )

    def _seal_cancellation_receipt_until_deadline(
        self,
        data: Mapping[str, Any],
        *,
        deadline_monotonic_s: float,
    ) -> None:
        """Commit exact cleanup proof from the teardown lifecycle itself.

        A route snapshot remains a fail-closed fallback, but it must not be the
        only component capable of turning completed physical teardown into a
        receipt.  Under concurrent cancellation that ordering produced a
        positive-feedback loop: every owner observed the worker as pending,
        queued another snapshot, and exhausted the response lane before any
        snapshot could commit the already-clean subject.  The lifecycle worker
        owns the exact identity and original deadline, so it seals the same
        receipt as soon as its worker marker has been retired.
        """

        # Several direct unit fixtures exercise only generation fencing and do
        # not construct a runtime.  A configured physical service always has
        # one; leaving those narrow fixtures on the explicit snapshot path does
        # not alter production behavior.
        if getattr(self, "runtime", None) is None:
            return
        cleanup_subject = {
            key: value for key, value in data.items() if key != "deadline_budget_ms"
        }
        while True:
            try:
                observation = self._snapshot(
                    {
                        "cleanup_subject": cleanup_subject,
                        "receipt_only": True,
                    }
                )
            except Exception:
                # Snapshot validation remains fail closed.  A transient
                # nonblocking observation can be retried only inside the
                # immutable owner deadline; the route's independent observer
                # retains the same authority and diagnostic surface.
                observation = None
            details = (
                observation.get("details") if isinstance(observation, Mapping) else None
            )
            receipt = (
                details.get("request_cleanup") if isinstance(details, Mapping) else None
            )
            if isinstance(receipt, Mapping) and receipt.get("complete") is True:
                return
            remaining = deadline_monotonic_s - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(_CANCEL_RECEIPT_POLL_SECONDS, remaining))

    def _infer_cancel(
        self,
        payload: dict[str, Any],
        *,
        defer_cleanup: bool = True,
    ) -> dict[str, Any]:
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
        pending_start = False
        if len(fields) > 1:
            cancel_control = self._validated_request_control(
                {field: data[field] for field in _REQUEST_CONTROL_FIELDS},
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
                    pending_start = True
                else:
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
                    control["cancellation_generation"] = data["cancellation_generation"]
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
            if not defer_cleanup:
                # ``infer_cancel_wait`` already owns a reserved control-lane
                # worker and cannot acknowledge until its ordered receipt
                # attempt is complete. Spawning a detached lifecycle worker
                # here lets this command publish ``cleanup_pending`` before the
                # only component capable of sealing its receipt has finished.
                # Under concurrent cancellation every owner then queues
                # fallback snapshots over the same remote control channel,
                # even though teardown is already complete locally. Apply and
                # retry the generation fence in this reserved worker instead,
                # and keep it registered for the whole teardown. A fallback
                # snapshot can still run concurrently on another reserved
                # worker, but cannot seal a transiently clean observation.
                with self._control_lock:
                    workers = getattr(self, "_cancellation_workers", None)
                    if workers is None:
                        workers = {}
                        self._cancellation_workers = workers
                    existing = workers.get(data["request_id"])
                    _require(
                        existing is None or not existing.is_alive(),
                        "duplicate_infer_cancel_worker",
                    )
                    inline_worker = threading.current_thread()
                    workers[data["request_id"]] = inline_worker
                try:
                    self._apply_cancellation_until_deadline(dict(data))
                finally:
                    with self._control_lock:
                        current = self._cancellation_workers.get(data["request_id"])
                        if current is inline_worker:
                            self._cancellation_workers.pop(data["request_id"], None)
                return self._signed_result(
                    "inference_cancelled",
                    {
                        "request_id": data["request_id"],
                        "cancelled": True,
                        "status": "CANCELLING",
                        "cleanup_pending": True,
                        **({"pending_start": True} if pending_start else {}),
                    },
                )
            with self._control_lock:
                workers = getattr(self, "_cancellation_workers", None)
                if workers is None:
                    workers = {}
                    self._cancellation_workers = workers
                _require(
                    data["request_id"] not in workers,
                    "duplicate_infer_cancel_worker",
                )
                receipt_events = getattr(
                    self,
                    "_cancellation_receipt_events",
                    None,
                )
                if receipt_events is None:
                    receipt_events = {}
                    self._cancellation_receipt_events = receipt_events
                receipt_event = threading.Event()
                receipt_events[data["request_id"]] = receipt_event
                receipt_deadline_monotonic_s = time.monotonic() + (
                    data["deadline_budget_ms"] / 1_000.0
                )

                def apply_cancellation() -> None:
                    try:
                        self._apply_cancellation_until_deadline(dict(data))
                    finally:
                        with self._control_lock:
                            current = self._cancellation_workers.get(data["request_id"])
                            if current is threading.current_thread():
                                self._cancellation_workers.pop(data["request_id"], None)
                    try:
                        self._seal_cancellation_receipt_until_deadline(
                            dict(data),
                            deadline_monotonic_s=receipt_deadline_monotonic_s,
                        )
                    finally:
                        receipt_event.set()
                        with self._control_lock:
                            current_event = self._cancellation_receipt_events.get(
                                data["request_id"]
                            )
                            if current_event is receipt_event:
                                self._cancellation_receipt_events.pop(
                                    data["request_id"], None
                                )

                worker = threading.Thread(
                    target=apply_cancellation,
                    name=f"mycelium-cancel-{data['request_id'][:12]}",
                    daemon=True,
                )
                workers[data["request_id"]] = worker
                try:
                    worker.start()
                except BaseException:
                    workers.pop(data["request_id"], None)
                    raise
            return self._signed_result(
                "inference_cancelled",
                {
                    "request_id": data["request_id"],
                    "cancelled": True,
                    "status": "CANCELLING",
                    "cleanup_pending": True,
                    **({"pending_start": True} if pending_start else {}),
                },
            )

        cancelled = self.router.cancel(data["request_id"])
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

    def _infer_cancel_wait(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Fence cancellation and return the ordered lifecycle receipt.

        This reserved worker owns generation fencing, teardown, receipt
        sealing, and response publication in that order. It never extends the
        owner's fixed budget: both its teardown and receipt polling stop before
        the existing route fallback reserve. Request-scoped route snapshots may
        still race this worker, but the worker marker prevents them from proving
        cleanup until teardown has retired and committed its monotonic receipt.
        """

        _require(
            "deadline_budget_ms" in payload,
            "invalid_infer_cancel_control",
        )
        started_at = time.monotonic()
        result = self._infer_cancel(payload, defer_cleanup=False)
        request_id = payload.get("request_id")
        if not isinstance(request_id, str):
            return result

        inline_deadline = started_at + max(
            0.0,
            payload["deadline_budget_ms"] / 1_000.0
            - _CANCEL_RECEIPT_FALLBACK_RESERVE_SECONDS,
        )
        self._seal_cancellation_receipt_until_deadline(
            payload,
            deadline_monotonic_s=inline_deadline,
        )

        def stored_receipt() -> Mapping[str, Any] | None:
            lock = getattr(self, "_control_lock", None)
            receipts = getattr(self, "_request_cleanup_receipts", None)
            if lock is None or not isinstance(receipts, Mapping):
                return None
            with lock:
                receipt = receipts.get(request_id)
                return dict(receipt) if isinstance(receipt, Mapping) else None

        receipt = stored_receipt()
        if not isinstance(receipt, Mapping) or receipt.get("complete") is not True:
            return result
        details = result.get("details")
        if not isinstance(details, Mapping):
            details = {}
        return self._signed_result(
            "inference_cancelled",
            {
                **details,
                "cleanup_pending": False,
                "request_cleanup": dict(receipt),
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
            "infer_cancel_wait": self._infer_cancel_wait,
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


def _emit(encoded: bytes, *, cleanup_priority: bool = False) -> None:
    if cleanup_priority:
        # Deadline-bound cleanup authority uses SSH's separately supervised
        # stderr data stream. An ordinary response can already occupy stdout
        # before infer_cancel_wait reaches stdin; no in-process priority queue
        # can reorder bytes after that point. The parent recognizes only this
        # exact prefix and then applies the same canonical response,
        # command-ID, node-ID, and signed-observation validation as stdout.
        # Diagnostics remain unprefixed stderr data.
        sys.stderr.buffer.write(CLEANUP_CONTROL_FRAME_PREFIX + encoded + b"\n")
        sys.stderr.buffer.flush()
        return
    sys.stdout.buffer.write(encoded + b"\n")
    sys.stdout.buffer.flush()


class _ResponseEmitter:
    """Publish ordinary and cleanup responses on independent wire lanes."""

    def __init__(self) -> None:
        self._stdout_condition = threading.Condition()
        self._stdout_active = False
        self._cleanup_lock = threading.Lock()

    def emit(
        self,
        document: dict[str, Any],
        *,
        cleanup_priority: bool = False,
    ) -> None:
        encoded = canonical_json_bytes(document)
        if cleanup_priority:
            # Cleanup authority has a separately supervised stderr channel.
            # Serialize that channel against itself, but never wait behind a
            # large or backpressured stdout inference response: doing so
            # defeats the independent lane and can strand an already-complete
            # signed receipt beyond its immutable owner deadline.
            with self._cleanup_lock:
                _emit(encoded, cleanup_priority=True)
            return
        with self._stdout_condition:
            # stdout and the deadline-bound cleanup channel are deliberately
            # independent. Reserving stdout for a cleanup response that will
            # never use it couples unrelated request lifecycles: sustained
            # cancellation can then retain at least one reservation forever,
            # starving completed inference responses and creating a backlog of
            # late replies after their parent waiters have already retired.
            while self._stdout_active:
                self._stdout_condition.wait()
            self._stdout_active = True
        try:
            _emit(encoded, cleanup_priority=False)
        finally:
            with self._stdout_condition:
                self._stdout_active = False
                self._stdout_condition.notify_all()


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

    response_emitter = _ResponseEmitter()
    active_lock = threading.Lock()
    active_command_ids: set[str] = set()
    inference_capacity = threading.BoundedSemaphore(8)
    control_capacity = threading.BoundedSemaphore(16)
    cleanup_capacity = threading.BoundedSemaphore(16)

    def execute(
        command: Any,
        command_id: str,
        lane_capacity: threading.BoundedSemaphore,
        received_at_monotonic_s: float,
    ) -> None:
        cleanup_priority = _command_uses_cleanup_response_priority(command)
        try:
            # Long inference commands own absolute deadlines and cooperative
            # cancellation points. The stdin reader must stay free to accept
            # cancellation, cleanup, probes, and unrelated request commands.
            result = service.dispatch(
                _age_cleanup_command_budget(
                    command,
                    queued_seconds=max(
                        0.0,
                        time.monotonic() - received_at_monotonic_s,
                    ),
                )
            )
            response_emitter.emit(
                _response(service, command_id=command_id, ok=True, result=result),
                cleanup_priority=cleanup_priority,
            )
        except NodeCommandError as exc:
            response_emitter.emit(
                _response(
                    service, command_id=command_id, ok=False, error_code=exc.code
                ),
                cleanup_priority=cleanup_priority,
            )
        except IrohTransportError as exc:
            if _owner_cancellation_interrupted_inference(service, command, exc):
                response_emitter.emit(
                    _response(
                        service,
                        command_id=command_id,
                        ok=False,
                        error_code="request_cancelled",
                    ),
                    cleanup_priority=cleanup_priority,
                )
            else:
                print(
                    f"physical-node command failed: {type(exc).__name__}",
                    file=sys.stderr,
                )
                traceback.print_exc(file=sys.stderr)
                response_emitter.emit(
                    _response(
                        service,
                        command_id=command_id,
                        ok=False,
                        error_code="node_command_failed",
                    ),
                    cleanup_priority=cleanup_priority,
                )
        except BaseException as exc:
            print(
                f"physical-node command failed: {type(exc).__name__}",
                file=sys.stderr,
            )
            traceback.print_exc(file=sys.stderr)
            response_emitter.emit(
                _response(
                    service,
                    command_id=command_id,
                    ok=False,
                    error_code="node_command_failed",
                ),
                cleanup_priority=cleanup_priority,
            )
        finally:
            with active_lock:
                active_command_ids.discard(command_id)
            lane_capacity.release()

    # Long inference commands and cleanup/liveness control use independently
    # bounded lanes.  Retired or slow model execution can consume only the
    # inference lane; generation-fenced cancellation, cleanup snapshots, and
    # health probes retain reserved workers and cannot queue behind it.
    inference_executor = ThreadPoolExecutor(
        max_workers=8,
        thread_name_prefix="mycelium-node-inference",
    )
    control_executor = ThreadPoolExecutor(
        max_workers=16,
        thread_name_prefix="mycelium-node-control",
    )
    cleanup_executor = ThreadPoolExecutor(
        max_workers=16,
        thread_name_prefix="mycelium-node-cleanup",
    )
    try:
        for raw_line in sys.stdin.buffer:
            received_at_monotonic_s = time.monotonic()
            command_id = "unknown"
            command: Any = None
            if len(raw_line) > MAX_COMMAND_BYTES:
                response_emitter.emit(
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
                if isinstance(command, dict) and isinstance(
                    command.get("command_id"), str
                ):
                    command_id = command["command_id"]
                inference_lane = _command_uses_inference_lane(command)
                cleanup_lane = _command_uses_cleanup_response_priority(command)
                if cleanup_lane:
                    lane_capacity = cleanup_capacity
                    lane_executor = cleanup_executor
                elif inference_lane:
                    lane_capacity = inference_capacity
                    lane_executor = inference_executor
                else:
                    lane_capacity = control_capacity
                    lane_executor = control_executor
                if not lane_capacity.acquire(blocking=False):
                    response_emitter.emit(
                        _response(
                            service,
                            command_id=command_id,
                            ok=False,
                            error_code="command_capacity_exhausted",
                        ),
                        cleanup_priority=(
                            _command_uses_cleanup_response_priority(command)
                        ),
                    )
                    continue
                with active_lock:
                    if command_id in active_command_ids:
                        lane_capacity.release()
                        response_emitter.emit(
                            _response(
                                service,
                                command_id=command_id,
                                ok=False,
                                error_code="duplicate_command_id",
                            ),
                            cleanup_priority=(
                                _command_uses_cleanup_response_priority(command)
                            ),
                        )
                        continue
                    active_command_ids.add(command_id)
                lane_executor.submit(
                    execute,
                    command,
                    command_id,
                    lane_capacity,
                    received_at_monotonic_s,
                )
                if isinstance(command, dict) and command.get("command") == "stop":
                    break
            except NodeCommandError as exc:
                response_emitter.emit(
                    _response(
                        service, command_id=command_id, ok=False, error_code=exc.code
                    ),
                    cleanup_priority=_command_uses_cleanup_response_priority(command),
                )
            except BaseException as exc:
                print(
                    f"physical-node command failed: {type(exc).__name__}",
                    file=sys.stderr,
                )
                response_emitter.emit(
                    _response(
                        service,
                        command_id=command_id,
                        ok=False,
                        error_code="node_command_failed",
                    ),
                    cleanup_priority=_command_uses_cleanup_response_priority(command),
                )
        return 0
    finally:
        inference_executor.shutdown(wait=True, cancel_futures=False)
        control_executor.shutdown(wait=True, cancel_futures=False)
        cleanup_executor.shutdown(wait=True, cancel_futures=False)
        for signum, handler in prior_signal_handlers.items():
            signal.signal(signum, handler)
        service.close()


if __name__ == "__main__":
    raise SystemExit(main())
