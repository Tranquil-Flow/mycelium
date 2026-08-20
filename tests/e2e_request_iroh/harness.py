"""Local-only request gateway -> Router -> native Iroh qualification harness.

The authority input is the repository's in-memory synthetic fixture. The
resulting RouteQualificationV1 opens the production gateway gate for this test
only. Reports deliberately remain local_evidence_only=true and
route_ready=false; they do not claim physical or distributed qualification.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any

from mycelium_qualification.evidence import sha256_bytes
from mycelium_qualification.qualifier import qualify_route
from mycelium_request_gateway.asgi import RequestGatewayASGIApplication
from mycelium_request_gateway.auth import StaticBearerAuthenticator
from mycelium_request_gateway.backend import RouterSessionBackend
from mycelium_request_gateway.contracts import InferenceSubmission, qualification_binding
from mycelium_request_gateway.service import RequestGatewayService
from mycelium_router.contracts import (
    HopWorkItem,
    RouterConfig,
    RuntimeBatch,
    RuntimeResult,
    TokenEvent,
)
from mycelium_router.fakes import (
    FakeCapacityPort,
    FakeDeviceStateProvider,
    FakeTopologyProvider,
    ManualClock,
    SequenceIdSource,
)
from mycelium_router.router import Router
from mycelium_router.transports.iroh import (
    IrohTransport,
    IrohTransportError,
    PeerBinding,
)
from mycelium_router.wire import encode_frame
from test_router_contracts import graph_fixture
from test_router_policy import state_table


AUTH_TOKEN = "request-iroh-local-test-credential"
REQUEST_ID = "request-iroh-e2e-001"
PROMPT = "deterministic local moonlight"
LOCAL_EVIDENCE_ONLY = True
ROUTE_READY = False
ROOT = Path(__file__).resolve().parents[2]
BINARY = ROOT / "native" / "iroh_transport" / "target" / "debug" / "mycelium-iroh-sidecar"
_SUBPROCESS_ENVIRONMENT_KEYS = (
    "HOME",
    "LANG",
    "LC_ALL",
    "PATH",
    "TMPDIR",
)


def _subprocess_environment() -> dict[str, str]:
    return {
        key: os.environ[key]
        for key in _SUBPROCESS_ENVIRONMENT_KEYS
        if key in os.environ
    }


class _RunningSidecar:
    """Native sidecar child with synthetic secret and sanitized environment."""

    def __init__(self, base: Path, secret: bytes, *, queue_capacity: int = 8) -> None:
        self.base = base
        self.secret = secret
        self.socket_path = base / "run" / "sidecar.sock"
        self.process: subprocess.Popen[str] | None = None
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, secret)
        finally:
            os.close(write_fd)
        try:
            self.process = subprocess.Popen(
                [
                    str(BINARY),
                    "--uds",
                    str(self.socket_path),
                    "--bootstrap-fd",
                    str(read_fd),
                    "--local-only",
                    "--queue-capacity",
                    str(queue_capacity),
                ],
                cwd=ROOT,
                env=_subprocess_environment(),
                pass_fds=(read_fd,),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        finally:
            os.close(read_fd)
        try:
            self.ready = self._read_ready()
            self._assert_loopback_advertisement()
        except BaseException:
            self.stop()
            raise

    def _read_ready(self) -> dict[str, Any]:
        process = self.process
        if process is None or process.stdout is None:
            raise AssertionError("sidecar_process_unavailable")
        readable, _, _ = select.select([process.stdout], [], [], 20)
        if not readable:
            raise AssertionError("sidecar_did_not_become_ready")
        line = process.stdout.readline()
        if not line:
            stderr = process.stderr.read() if process.stderr else ""
            raise AssertionError(f"sidecar_exited_before_ready:{stderr}")
        ready = json.loads(line)
        if ready.get("event") != "ready":
            raise AssertionError("sidecar_invalid_ready_event")
        if ready.get("alpn") != "mycelium.iroh.sidecar.v1":
            raise AssertionError("sidecar_invalid_alpn")
        return ready

    def _assert_loopback_advertisement(self) -> None:
        endpoint = self.ready.get("endpoint_addr")
        addrs = endpoint.get("addrs") if isinstance(endpoint, dict) else None
        if not isinstance(addrs, list) or not addrs:
            raise AssertionError("sidecar_missing_endpoint_addresses")
        for address in addrs:
            if not isinstance(address, dict) or set(address) != {"Ip"}:
                raise AssertionError("sidecar_nonlocal_endpoint_address")
            host_port = address["Ip"]
            if not isinstance(host_port, str):
                raise AssertionError("sidecar_invalid_endpoint_address")
            host = host_port.rsplit(":", 1)[0].strip("[]")
            if host not in {"127.0.0.1", "::1"}:
                raise AssertionError("sidecar_nonloopback_endpoint_address")

    @property
    def pid(self) -> int:
        process = self.process
        if process is None:
            raise AssertionError("sidecar_process_unavailable")
        return process.pid

    def stop(self) -> str:
        process = self.process
        if process is None:
            return ""
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            if process.stderr is None or process.stderr.closed:
                return ""
            return process.stderr.read()
        finally:
            for stream in (process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()


@dataclass(frozen=True)
class CompleteRequestEvidence:
    authenticated_status: int
    unqualified_router_mutations: int
    accepted_status: int
    router_admissions: int
    production_router_count: int
    native_sidecar_count: int
    transport_type: str
    prefill_stage_indexes: tuple[int, ...]
    decode_route_steps: int
    decode_stage_indexes: tuple[int, ...]
    token_indexes: tuple[int, ...]
    acknowledged_cursor: int
    replayed_token_indexes: tuple[int, ...]
    activation_digests: tuple[str, ...]
    decode_payload_digests: tuple[str, ...]
    token_frame_digests: tuple[str, ...]
    local_evidence_only: bool = LOCAL_EVIDENCE_ONLY
    route_ready: bool = ROUTE_READY


@dataclass(frozen=True)
class CancellationEvidence:
    gateway_released: bool
    adapter_released: bool
    entry_router_released: bool
    remote_router_released: bool
    pending_deliveries: int
    local_evidence_only: bool = LOCAL_EVIDENCE_ONLY
    route_ready: bool = ROUTE_READY


@dataclass(frozen=True)
class GenerationRotationEvidence:
    rejected: bool
    error_code: str
    old_generation: int
    new_generation: int
    pending_deliveries: int
    local_evidence_only: bool = LOCAL_EVIDENCE_ONLY
    route_ready: bool = ROUTE_READY


@dataclass(frozen=True)
class _Execution:
    stage_index: int
    phase: str
    token_index: int
    payload_digest: str
    path_id: str


class _ExecutionLog:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: list[_Execution] = []

    def append(self, item: _Execution) -> None:
        with self._lock:
            self._items.append(item)

    def snapshot(self) -> tuple[_Execution, ...]:
        with self._lock:
            return tuple(self._items)


class _DeterministicStageRuntime:
    """Test model preserving bytes while enforcing stage-local path state."""

    decode_mode = "stage_local_kv"

    def __init__(
        self,
        *,
        stage_by_placement: dict[str, int],
        final_stage_index: int,
        executions: _ExecutionLog,
        block_stage_index: int | None = None,
        block_token_index: int | None = None,
    ) -> None:
        self._stage_by_placement = dict(stage_by_placement)
        self._final_stage_index = final_stage_index
        self._executions = executions
        self._block_stage_index = block_stage_index
        self._block_token_index = block_token_index
        self.block_entered = threading.Event()
        self.block_release = threading.Event()
        self._lock = threading.Lock()
        self.active_paths: set[str] = set()
        self.cancel_calls: list[str] = []

    def execute(self, item: HopWorkItem) -> RuntimeResult:
        if not isinstance(item.payload, bytes):
            return RuntimeResult(
                success=False,
                failure_scope="PLACEMENT",
                failure_reason="noncanonical_test_payload",
            )
        stage_index = self._stage_by_placement[item.placement_id]
        if item.phase in {"PREFILL", "RECOVERY_PREFILL"}:
            with self._lock:
                self.active_paths.add(item.path_id)
        elif item.phase == "DECODE":
            with self._lock:
                if item.path_id not in self.active_paths:
                    return RuntimeResult(
                        success=False,
                        failure_scope="PLACEMENT",
                        failure_reason="missing_stage_local_state",
                    )

        self._executions.append(
            _Execution(
                stage_index=stage_index,
                phase=item.phase,
                token_index=item.token_index,
                payload_digest=_digest(item.payload),
                path_id=item.path_id,
            )
        )
        if (
            item.phase == "DECODE"
            and stage_index == self._block_stage_index
            and item.token_index == self._block_token_index
        ):
            self.block_entered.set()
            if not self.block_release.wait(timeout=5.0):
                return RuntimeResult(
                    success=False,
                    failure_scope="PLACEMENT",
                    failure_reason="test_interlock_timeout",
                )

        is_final = stage_index == self._final_stage_index
        token_id: int | None = None
        if is_final and item.phase in {"PREFILL", "RECOVERY_PREFILL"}:
            token_id = 100 + max(0, item.token_index)
        elif is_final and item.phase == "DECODE":
            token_id = 100 + item.token_index
        return RuntimeResult(success=True, payload=item.payload, token_id=token_id)

    def execute_batch(self, batch: RuntimeBatch) -> tuple[RuntimeResult, ...]:
        return tuple(self.execute(item) for item in batch.items)

    def cancel(self, path_id: str) -> None:
        with self._lock:
            self.cancel_calls.append(path_id)
            self.active_paths.discard(path_id)

    def has_path(self, path_id: str) -> bool:
        with self._lock:
            return path_id in self.active_paths


class _EvidenceRouter(Router):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.received_tokens: list[TokenEvent] = []

    def receive_token_event(self, event: TokenEvent, *, source_node_id: str | None = None):
        self.received_tokens.append(event)
        return super().receive_token_event(event, source_node_id=source_node_id)


class _DistributedRouterPort:
    """Map the production session adapter onto Router's distributed methods."""

    def __init__(self, router: Router) -> None:
        self.router = router
        self.admissions = 0

    def admit(self, request, client_sink, **kwargs):
        self.admissions += 1
        return self.router.start_distributed_prefill(request, client_sink, **kwargs)

    def decode_one(self, request_id: str) -> bool:
        return self.router.decode_one_distributed(request_id)

    def request_status(self, request_id: str) -> str:
        return self.router.request_status(request_id)

    def cancel(self, request_id: str) -> bool:
        return self.router.cancel(request_id)


class _DeterministicCodec:
    def encode(self, prompt: str) -> tuple[int, ...]:
        if prompt != PROMPT:
            raise ValueError("unexpected_test_prompt")
        return (11, 12, 13, 14)

    def decode_token(self, token_id: int) -> str:
        return f"<{token_id}>"


class _QualificationSource:
    def __init__(self, value: Any | None) -> None:
        self.value = value

    def current(self):
        return self.value


class _FixedRequestId:
    def __init__(self) -> None:
        self._issued = False

    def __call__(self) -> str:
        if self._issued:
            raise AssertionError("qualification harness admitted more than one request")
        self._issued = True
        return REQUEST_ID


class _ASGIHarness:
    @staticmethod
    async def request(
        app: RequestGatewayASGIApplication,
        path: str,
        *,
        method: str,
        token: str | None,
        document: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        body = b"" if document is None else json.dumps(document).encode("utf-8")
        headers = [(b"content-type", b"application/json")]
        if token is not None:
            headers.append((b"authorization", f"Bearer {token}".encode("ascii")))
        scope = {
            "type": "http",
            "method": method,
            "path": path,
            "query_string": b"",
            "headers": headers,
        }
        incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        await incoming.put({"type": "http.request", "body": body, "more_body": False})
        sent: list[dict[str, Any]] = []

        async def receive() -> dict[str, Any]:
            return await incoming.get()

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        await app(scope, receive, send)
        start = next(item for item in sent if item["type"] == "http.response.start")
        payload = b"".join(
            item.get("body", b"")
            for item in sent
            if item["type"] == "http.response.body"
        )
        return start["status"], json.loads(payload or b"{}")


class _Topology:
    def __init__(
        self,
        binary: Path,
        *,
        block_remote_decode: bool = False,
    ) -> None:
        if binary.resolve() != BINARY.resolve() or not binary.is_file():
            raise AssertionError("unexpected_native_sidecar_binary")
        self.root = Path(tempfile.mkdtemp(prefix="mycelium-request-iroh-", dir="/tmp"))
        try:
            self.first_sidecar = _RunningSidecar(
                self.root / "first",
                bytes(range(32)),
            )
        except BaseException:
            shutil.rmtree(self.root)
            raise
        try:
            self.second_sidecar = _RunningSidecar(
                self.root / "second", bytes(range(32, 64))
            )
        except BaseException:
            self.first_sidecar.stop()
            shutil.rmtree(self.root)
            raise

        self.graph = _local_three_stage_graph()
        stage_by_placement = {
            placement.placement_id: stage_index
            for stage_index, stage in enumerate(self.graph.stages)
            for placement in stage.placements
        }
        self.executions = _ExecutionLog()
        self.clock = ManualClock()
        self.capacity = FakeCapacityPort(clock=self.clock)
        self.runtime_a = _DeterministicStageRuntime(
            stage_by_placement=stage_by_placement,
            final_stage_index=2,
            executions=self.executions,
        )
        self.runtime_c = _DeterministicStageRuntime(
            stage_by_placement=stage_by_placement,
            final_stage_index=2,
            executions=self.executions,
            block_stage_index=1 if block_remote_decode else None,
            block_token_index=1 if block_remote_decode else None,
        )
        self.first = _make_transport(
            "node-a", self.first_sidecar, "node-c", self.second_sidecar
        )
        self.second = _make_transport(
            "node-c", self.second_sidecar, "node-a", self.first_sidecar
        )
        self.router_a = self._make_router("node-a", self.runtime_a, self.first)
        self.router_c = self._make_router("node-c", self.runtime_c, self.second)
        self.first.bind_router(self.router_a)
        self.second.bind_router(self.router_c)
        try:
            self.first.start()
            self.second.start()
        except BaseException:
            self.close()
            raise

    def _make_router(self, node_id: str, runtime: Any, transport: IrohTransport):
        return _EvidenceRouter(
            node_id=node_id,
            topology=FakeTopologyProvider(self.graph),
            device_states=FakeDeviceStateProvider(state_table(slow_b_bandwidth=True)),
            capacity=self.capacity,
            runtime=runtime,
            transport=transport,
            clock=self.clock,
            id_source=SequenceIdSource(),
            config=RouterConfig(),
        )

    def mutation_snapshot(self, port: _DistributedRouterPort) -> tuple[int, ...]:
        first_evidence = self.first.evidence()
        second_evidence = self.second.evidence()
        return (
            port.admissions,
            len(self.capacity.requests),
            len(self.capacity.committed_ids),
            len(self.executions.snapshot()),
            first_evidence.remote_frames_sent,
            first_evidence.remote_frames_received,
            second_evidence.remote_frames_sent,
            second_evidence.remote_frames_received,
            len(self.router_a.entry._requests),
            len(self.router_a.entry._pending_prefills),
        )

    def close(self) -> None:
        self.runtime_c.block_release.set()
        errors: list[BaseException] = []
        for close_resource in (
            self.first.close,
            self.second.close,
            self.first_sidecar.stop,
            self.second_sidecar.stop,
        ):
            try:
                close_resource()
            except BaseException as error:
                errors.append(error)
        try:
            shutil.rmtree(self.root)
        except FileNotFoundError:
            pass
        except BaseException as error:
            errors.append(error)
        if errors:
            raise errors[0]


class _PausedAfterNativeReceive(IrohTransport):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.native_delivery_entered = threading.Event()
        self.release_native_delivery = threading.Event()
        self._pause_once = True

    def _recv(self, client: Any):
        delivery = super()._recv(client)
        if delivery is not None and self._pause_once:
            self._pause_once = False
            self.native_delivery_entered.set()
            if not self.release_native_delivery.wait(timeout=5.0):
                raise AssertionError("native_delivery_interlock_timeout")
        return delivery


class _TokenProbe:
    def __init__(self) -> None:
        self.tokens: list[TokenEvent] = []

    def receive_token_event(self, event: TokenEvent, *, source_node_id: str | None = None):
        del source_node_id
        self.tokens.append(event)
        return True


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _local_three_stage_graph():
    graph = graph_fixture()
    final_stage = graph.stages[-1]
    return replace(
        graph,
        stages=graph.stages[:-1]
        + (
            replace(
                final_stage,
                placements=tuple(
                    replace(placement, node_id="node-c")
                    for placement in final_stage.placements
                ),
            ),
        ),
    )


def _binding(node_id: str, sidecar: _RunningSidecar, *, generation: int = 1):
    return PeerBinding(
        node_id=node_id,
        endpoint_id=sidecar.ready["endpoint_id"],
        endpoint_addr=sidecar.ready["endpoint_addr"],
        generation=generation,
    )


def _make_transport(
    node_id: str,
    sidecar: _RunningSidecar,
    peer_node_id: str,
    peer: _RunningSidecar,
    *,
    transport_type: type[IrohTransport] = IrohTransport,
):
    return transport_type(
        node_id=node_id,
        socket_path=sidecar.socket_path,
        bootstrap_secret=sidecar.secret,
        peer=_binding(peer_node_id, peer),
        expected_endpoint_id=sidecar.ready["endpoint_id"],
        delivery_timeout_seconds=3.0,
        poll_interval_seconds=0.02,
    )


def _synthetic_qualification():
    root = Path(__file__).resolve().parents[2]
    fixture_path = root / "tests" / "qualification" / "conftest.py"
    module_name = "_request_iroh_e2e_qualification_fixture"
    spec = importlib.util.spec_from_file_location(module_name, fixture_path)
    if spec is None or spec.loader is None:
        raise AssertionError("qualification_fixture_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    case = module.make_case()
    files, manifest = case.render()

    def verify(statement: bytes, signature: dict[str, Any]) -> bool:
        return (
            signature.get("algorithm") == "ed25519"
            and signature.get("signature")
            == "synthetic-test-signature-never-production"
            and signature.get("signed_statement_digest") == sha256_bytes(statement)
        )

    qualification = qualify_route(
        evidence_files=files,
        evidence_manifest=manifest,
        now_unix_ms=case.now_unix_ms,
        verify_gossip_signature=verify,
        verify_load_proof_signature=verify,
    )
    if not qualification.route_ready:
        raise AssertionError("test_qualification_not_issued")
    return qualification


def _service_stack(topology: _Topology):
    qualification = _synthetic_qualification()
    source = _QualificationSource(None)
    port = _DistributedRouterPort(topology.router_a)
    backend = RouterSessionBackend(
        router=port,
        codec=_DeterministicCodec(),
        clock=topology.clock.now,
        excluded_placements=frozenset({"node-b-stage-000"}),
        sampling_seed=0,
    )
    service = RequestGatewayService(
        qualification_source=source,
        backend=backend,
        request_id_source=_FixedRequestId(),
        max_buffered_events=64,
        max_sessions=2,
    )
    app = RequestGatewayASGIApplication(
        service,
        authenticator=StaticBearerAuthenticator(AUTH_TOKEN),
    )
    submission = InferenceSubmission(
        prompt=PROMPT,
        max_new_tokens=9,
        qualification=qualification_binding(qualification),
    )
    return qualification, source, port, backend, service, app, submission


def _post(app: RequestGatewayASGIApplication, submission: InferenceSubmission):
    return asyncio.run(
        _ASGIHarness.request(
            app,
            "/v1/inference",
            method="POST",
            token=AUTH_TOKEN,
            document=submission.to_dict(),
        )
    )


def _wait_worker_done(service: RequestGatewayService, request_id: str) -> None:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        session = service._sessions[request_id]
        with session.condition:
            if session.worker_done:
                return
        time.sleep(0.01)
    raise AssertionError("gateway_worker_did_not_finish")


def run_complete_request(native_binary: Path) -> CompleteRequestEvidence:
    topology = _Topology(native_binary)
    service: RequestGatewayService | None = None
    try:
        (
            qualification,
            source,
            port,
            _backend,
            service,
            app,
            submission,
        ) = _service_stack(topology)
        before = topology.mutation_snapshot(port)
        unqualified_status, unqualified_body = _post(app, submission)
        after = topology.mutation_snapshot(port)
        if unqualified_body != {"error": "route_dropped"}:
            raise AssertionError(f"unexpected_unqualified_response:{unqualified_body}")
        unqualified_mutations = sum(left != right for left, right in zip(before, after))

        source.value = qualification
        accepted_status, accepted_body = _post(app, submission)
        request_id = accepted_body.get("request_id")
        if request_id != REQUEST_ID:
            raise AssertionError(f"unexpected_request_id:{request_id}")
        session_token = accepted_body.get("session_token")
        if not isinstance(session_token, str):
            raise AssertionError("missing_session_token")

        first = service.subscribe(
            request_id,
            last_event_id=None,
            owner_token=session_token,
        )
        try:
            accepted_event = first.next_event(timeout=5.0)
            if accepted_event is None or accepted_event.kind != "accepted":
                raise AssertionError("missing_accepted_event")
            first.ack(accepted_event.sequence)
            first_token = first.next_event(timeout=5.0)
            if first_token is None or first_token.token_index != 0:
                raise AssertionError("missing_first_token")
            first.ack(first_token.sequence)
            unacknowledged = first.next_event(timeout=5.0)
            if unacknowledged is None or unacknowledged.token_index != 1:
                raise AssertionError("missing_unacknowledged_token")
        finally:
            first.close()

        resumed = service.subscribe(
            request_id,
            last_event_id=first_token.sequence,
            owner_token=session_token,
        )
        replayed: list[int] = []
        last_token_cursor = first_token.sequence
        try:
            while True:
                event = resumed.next_event(timeout=5.0)
                if event is None:
                    break
                if event.kind == "token":
                    if event.token_index is None:
                        raise AssertionError("token_index_missing")
                    replayed.append(event.token_index)
                    last_token_cursor = event.sequence
                resumed.ack(event.sequence)
                if event.terminal:
                    if event.kind != "completed":
                        raise AssertionError(f"unexpected_terminal:{event.kind}")
                    break
        finally:
            resumed.close()
        _wait_worker_done(service, request_id)

        executions = topology.executions.snapshot()
        prefills = tuple(item for item in executions if item.phase == "PREFILL")
        decodes = tuple(item for item in executions if item.phase == "DECODE")
        token_events = tuple(topology.router_a.received_tokens)
        native_pids = {
            topology.first_sidecar.pid,
            topology.second_sidecar.pid,
        }
        if os.getpid() in native_pids:
            raise AssertionError("sidecar_not_native_child_process")
        return CompleteRequestEvidence(
            authenticated_status=unqualified_status,
            unqualified_router_mutations=unqualified_mutations,
            accepted_status=accepted_status,
            router_admissions=port.admissions,
            production_router_count=2,
            native_sidecar_count=len(native_pids),
            transport_type=type(topology.first).__name__,
            prefill_stage_indexes=tuple(item.stage_index for item in prefills),
            decode_route_steps=len(decodes) // len(topology.graph.stages),
            decode_stage_indexes=tuple(item.stage_index for item in decodes),
            token_indexes=tuple(event.token_index for event in token_events),
            acknowledged_cursor=last_token_cursor,
            replayed_token_indexes=tuple(replayed),
            activation_digests=tuple(item.payload_digest for item in prefills),
            decode_payload_digests=tuple(item.payload_digest for item in decodes),
            token_frame_digests=tuple(_digest(encode_frame(event)) for event in token_events),
        )
    finally:
        try:
            if service is not None:
                service.close()
        finally:
            topology.close()


def run_cancellation_probe(native_binary: Path) -> CancellationEvidence:
    topology = _Topology(native_binary, block_remote_decode=True)
    service: RequestGatewayService | None = None
    try:
        (
            qualification,
            source,
            _port,
            backend,
            service,
            app,
            submission,
        ) = _service_stack(topology)
        source.value = qualification
        status, body = _post(app, submission)
        if status != 202 or body.get("request_id") != REQUEST_ID:
            raise AssertionError(f"cancellation_admission_failed:{status}:{body}")
        session_token = body.get("session_token")
        if not isinstance(session_token, str):
            raise AssertionError("missing_session_token")
        if not topology.runtime_c.block_entered.wait(timeout=5.0):
            raise AssertionError("remote_decode_interlock_not_reached")
        record = topology.router_a.get_request(REQUEST_ID)
        path_id = record.manifest.path_id
        if not service.cancel(REQUEST_ID, owner_token=session_token):
            raise AssertionError("gateway_cancellation_not_started")
        topology.runtime_c.block_release.set()
        _wait_worker_done(service, REQUEST_ID)
        deadline = time.monotonic() + 5.0
        while (
            topology.runtime_c.has_path(path_id)
            or path_id in topology.router_c.relay._paths
            or not topology.first.cancellation_cleanup_complete(REQUEST_ID, path_id)
            or not topology.second.cancellation_cleanup_complete(REQUEST_ID, path_id)
        ):
            fatal = topology.first.fatal_error or topology.second.fatal_error
            if fatal is not None:
                raise AssertionError(f"cancellation_transport_failed:{fatal}")
            if time.monotonic() >= deadline:
                raise AssertionError("remote_cancellation_release_timeout")
            time.sleep(0.01)

        session = service._sessions[REQUEST_ID]
        with session.condition:
            gateway_released = (
                session.worker_done
                and session.submission is None
                and session.captured is None
                and not session.active_subscription
            )
        adapter_released = REQUEST_ID not in backend._cancelled
        entry_router_released = (
            not topology.runtime_a.has_path(path_id)
            and path_id not in topology.router_a.relay._paths
            and set(record_hop.reservation_id for record_hop in record.manifest.ordered_hops)
            <= topology.capacity.released_ids
        )
        remote_router_released = (
            not topology.runtime_c.has_path(path_id)
            and path_id not in topology.router_c.relay._paths
        )
        pending = (
            topology.first.pending_delivery_count
            + topology.second.pending_delivery_count
        )
        return CancellationEvidence(
            gateway_released=gateway_released,
            adapter_released=adapter_released,
            entry_router_released=entry_router_released,
            remote_router_released=remote_router_released,
            pending_deliveries=pending,
        )
    finally:
        topology.runtime_c.block_release.set()
        try:
            if service is not None:
                service.close()
        finally:
            topology.close()


def run_generation_rotation_probe(native_binary: Path) -> GenerationRotationEvidence:
    if native_binary.resolve() != BINARY.resolve() or not native_binary.is_file():
        raise AssertionError("unexpected_native_sidecar_binary")
    root = Path(tempfile.mkdtemp(prefix="mycelium-request-iroh-rotation-", dir="/tmp"))
    first_sidecar: _RunningSidecar | None = None
    second_sidecar: _RunningSidecar | None = None
    sender: IrohTransport | None = None
    receiver: _PausedAfterNativeReceive | None = None
    delivery_thread: threading.Thread | None = None
    try:
        first_sidecar = _RunningSidecar(root / "first", b"a" * 32)
        second_sidecar = _RunningSidecar(root / "second", b"b" * 32)
        sender_transport = _make_transport(
            "node-a", first_sidecar, "node-c", second_sidecar
        )
        receiver_candidate = _make_transport(
            "node-c",
            second_sidecar,
            "node-a",
            first_sidecar,
            transport_type=_PausedAfterNativeReceive,
        )
        if not isinstance(receiver_candidate, _PausedAfterNativeReceive):
            raise AssertionError("paused_transport_factory_mismatch")
        receiver_transport = receiver_candidate
        sender = sender_transport
        receiver = receiver_transport
        sender_probe = _TokenProbe()
        receiver_probe = _TokenProbe()
        sender_transport.bind_router(sender_probe)
        receiver_transport.bind_router(receiver_probe)
        sender_transport.start()
        receiver_transport.start()
        frame = encode_frame(
            TokenEvent(
                request_id=REQUEST_ID,
                path_id="path-1",
                path_attempt=0,
                token_index=0,
                token_id=100,
                sampling_counter=1,
            )
        )
        outcome: list[BaseException | object] = []

        def deliver() -> None:
            try:
                outcome.append(
                    sender_transport.send_router_frame(
                        frame,
                        destination_node_id="node-c",
                    )
                )
            except BaseException as error:
                outcome.append(error)

        delivery_thread = threading.Thread(target=deliver, name="request-iroh-stale-send")
        delivery_thread.start()
        if not receiver_transport.native_delivery_entered.wait(timeout=5.0):
            raise AssertionError("native_rotation_interlock_not_reached")
        old_generation = receiver_transport.peer_binding.generation
        replacement = replace(
            receiver_transport.peer_binding,
            generation=old_generation + 1,
        )
        receiver_transport.rotate_peer(replacement)
        delivery_thread.join(timeout=5.0)
        if delivery_thread.is_alive():
            raise AssertionError("stale_delivery_did_not_finish")
        receiver_transport.release_native_delivery.set()
        deadline = time.monotonic() + 2.0
        while receiver_transport.fatal_error is None and time.monotonic() < deadline:
            time.sleep(0.01)
        error = outcome[0] if outcome else AssertionError("missing_delivery_outcome")
        rejected = isinstance(error, IrohTransportError)
        error_code = ""
        if isinstance(error, IrohTransportError):
            error_code = error.detail if error.detail == "peer_rotated" else error.code
        if receiver_probe.tokens:
            raise AssertionError("stale_frame_reached_router")
        return GenerationRotationEvidence(
            rejected=rejected,
            error_code=error_code,
            old_generation=old_generation,
            new_generation=replacement.generation,
            pending_deliveries=(
                len(sender_transport._pending) + len(receiver_transport._pending)
            ),
        )
    finally:
        active_exception = sys.exc_info()[0] is not None
        cleanup_errors: list[BaseException] = []
        if receiver is not None:
            receiver.release_native_delivery.set()
        for close_resource in (
            sender.close if sender is not None else None,
            receiver.close if receiver is not None else None,
            first_sidecar.stop if first_sidecar is not None else None,
            second_sidecar.stop if second_sidecar is not None else None,
        ):
            if close_resource is None:
                continue
            try:
                close_resource()
            except BaseException as error:
                cleanup_errors.append(error)
        if delivery_thread is not None and delivery_thread.is_alive():
            delivery_thread.join(timeout=5.0)
            if delivery_thread.is_alive():
                cleanup_errors.append(
                    AssertionError("stale_delivery_thread_not_released")
                )
        try:
            shutil.rmtree(root)
        except FileNotFoundError:
            pass
        except BaseException as error:
            cleanup_errors.append(error)
        if cleanup_errors and not active_exception:
            raise cleanup_errors[0]
