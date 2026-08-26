"""Qualify and serve the persistent physical route on loopback."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import secrets
import signal
import stat
import threading
import time
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import unquote, urlsplit
import uuid

from mycelium_demo.product_stack import build_loopback_product_stack
from mycelium_evidence import EvidenceProjectionRegistry, sealed_evidence_projection
from mycelium_governance import governance_readiness
from mycelium_product_spine import ProductEvidenceApplication, ProductProjector
from mycelium_layer_planner.public_projection import validate_m13_placement_projection
from mycelium_layer_planner.workload_intelligence import validate_m15_plan_comparison
from mycelium_topology_evidence import validate_m14_topology_projection
from mycelium_qualification import (
    issue_live_route_qualification,
    route_qualification_to_dict,
)
from mycelium_request_gateway.asgi import MAX_REQUEST_BODY_BYTES
from mycelium_request_gateway.contracts import (
    qualification_digest,
    safe_qualification_projection,
)
from mycelium_physical_runner import load_operator_plan
from mycelium_m16_runtime import build_live_m16_runtime
from mycelium_performance_budget import validate_performance_budget_v3
from mycelium_m18_replication import validate_replica_plan, validate_replica_runtime
from mycelium_m19_recovery import (
    validate_liveness,
    validate_recovery_plan,
    validate_recovery_runtime,
)
from mycelium_m20_speculation import (
    validate_speculative_plan,
    validate_speculative_runtime,
)
from mycelium_m21_heterogeneous import validate_heterogeneous_evidence
from mycelium_m22_release import validate_release_evidence
from mycelium_m23_kv import validate_m23_kv_evidence
from mycelium_ui_gateway.coordinator import CoordinatorError
from physical_inference_qualification import ControllerError
from physical_inference_node import execution_graph_from_document

from .activation import DeploymentActivationError, PreparedDeploymentActivation
from .artifact_provisioner import ArtifactAcquisitionStore
from .codec import prompt_codec_from_deployment
from .health import RouteHealthSource
from .model_capacity import (
    ModelCapacityRefresh,
    ModelCapacityRefreshError,
    live_observations_document,
    recompute_model_operation,
)
from .local_preparer import LocalCandidatePreparer
from .member_transport import MemberArtifactTransport
from .preparation import LocalModelPreparation, ModelPreparationError
from .registry import (
    DeploymentSelectionError,
    LiveDeploymentRegistry,
    QualifiedDeploymentRuntime,
    UnavailableDeployment,
)
from .route import LiveRoute, PhysicalLiveRoute, RouteCounters, RouteIdentity
from .router_port import LiveRouterPort
from .a4_install import build_a4_qualification, load_a4_evidence_files


ROOT = Path(__file__).resolve().parents[1]
STARTUP_PROMPT = (15496, 11, 703, 389, 345, 30)
STARTUP_OUTPUT = (4599, 3329, 2506, 5145)
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "connect-src 'self'; img-src 'self'; base-uri 'none'; "
    "frame-ancestors 'none'; form-action 'self'"
)


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _counter_document(counters: RouteCounters) -> dict[str, Any]:
    return {
        "frames_sent": counters.frames_sent,
        "frames_received": counters.frames_received,
        "applied_operation_count": counters.applied_operation_count,
        "fatal": counters.fatal,
    }


def _validate_route_identity(identity: RouteIdentity, execution_graph: Any) -> None:
    """Require one distinct physical endpoint for every graph participant."""

    node_ids = {
        placement.node_id
        for stage in execution_graph.stages
        for placement in stage.placements
    }
    endpoint_ids = identity.endpoint_ids
    if (
        len(node_ids) < 2
        or len(endpoint_ids) != len(node_ids)
        or len(set(endpoint_ids)) != len(endpoint_ids)
    ):
        raise RuntimeError("startup_endpoint_identity_invalid")


class LiveObservatoryApplication:
    """Minimal read-only Observatory projection of the qualifier-owned record."""

    def __init__(self, health: RouteHealthSource) -> None:
        self._health = health
        self._lock = threading.Lock()
        # Survive backend restarts without replaying a generation below the
        # browser's fail-closed high-water mark.
        self._generation = int(time.time() * 1_000)

    def _envelope(self) -> dict[str, Any]:
        with self._lock:
            self._generation += 1
            generation = self._generation
        qualification = self._health.current()
        observed_at = int(time.time() * 1_000)
        projection = None
        if qualification is not None:
            public = safe_qualification_projection(qualification)
            observed_at = max(observed_at, qualification.issued_at_unix_ms)
            projection = {
                **public,
                "protocol": "mycelium.route_qualification.v1",
                "qualification_id": qualification.qualification_id,
            }
        route_ready = projection is not None and projection["route_ready"] is True
        return {
            "protocol": "mycelium.observatory_stream.v1",
            "generation": generation,
            "bundle": {
                "snapshot": {
                    "protocol": "mycelium.observatory.request_projection.v1",
                    "source_cursor": generation,
                    "observed_at_unix_ms": observed_at,
                    "qualification": projection,
                    "sessions": [],
                },
                "incidents": [],
                "provisioning": {
                    "protocol": "mycelium.observatory.event_adapter_status.v1",
                    "route_ready": route_ready,
                    "source_cursor": generation,
                    "buffered_sessions": 0,
                    "quarantine_capacity": 256,
                    "dropped_quarantine_count": 0,
                },
            },
        }

    async def __call__(self, scope: Mapping[str, Any], receive, send) -> None:
        if scope.get("type") == "lifespan":
            while True:
                message = await receive()
                if message.get("type") == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message.get("type") == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return

        if scope.get("type") != "http" or scope.get("method") != "GET":
            await self._send_json(send, 405, {"error": "method_not_allowed"})
            return
        envelope = self._envelope()
        if scope.get("path") == "/v1/observatory/snapshot":
            await self._send_json(send, 200, envelope)
            return
        if scope.get("path") == "/v1/observatory/events":
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", b"text/event-stream; charset=utf-8"),
                        (b"cache-control", b"no-store"),
                    ],
                }
            )
            while True:
                envelope = self._envelope()
                body = (
                    f"id: {envelope['generation']}\n"
                    "event: snapshot\n"
                    f"data: {_json_bytes(envelope).decode('utf-8')}\n\n"
                ).encode("utf-8")
                await send(
                    {"type": "http.response.body", "body": body, "more_body": True}
                )
                await asyncio.sleep(5.0)
        await self._send_json(send, 404, {"error": "not_found"})

    @staticmethod
    async def _send_json(send, status: int, value: Mapping[str, Any]) -> None:
        body = _json_bytes(value)
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"cache-control", b"no-store"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})


class LiveSwarmCoordinator:
    """Project membership and expose bounded owner-authorized mutations."""

    def __init__(
        self,
        membership_source: Any,
        health: RouteHealthSource,
        *,
        seed_url: str | None = None,
    ) -> None:
        self._health = health
        self._membership_source = membership_source
        self._seed_url = seed_url
        if not callable(getattr(membership_source, "membership_status", None)):
            raise ValueError("live_membership_source_required")

    def status(self) -> Mapping[str, Any]:
        return self._membership_source.membership_status(
            qualification=self._health.current()
        )

    def create_invite(self, document: Mapping[str, Any]) -> Mapping[str, Any]:
        if document.get("capability") != "native_inference_node":
            raise CoordinatorError("capability_not_supported")
        if self._seed_url is None:
            raise CoordinatorError("swarm_operator_unavailable")
        source = getattr(self._membership_source, "mint_native_invite", None)
        if not callable(source):
            raise CoordinatorError("swarm_operator_unavailable")
        ttl_seconds = document.get("expires_in_seconds")
        if type(ttl_seconds) is not int or not 30 <= ttl_seconds <= 300:
            raise CoordinatorError("invite_ttl_invalid")
        nonce = f"product-ui-{secrets.token_hex(16)}"
        try:
            bundle = source(
                seed_url=self._seed_url,
                ttl_seconds=ttl_seconds,
                nonce=nonce,
            )
            invite_code = json.dumps(
                bundle,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            expires_at = int((time.time() + ttl_seconds) * 1_000)
        except Exception as exc:
            code = getattr(exc, "code", "invite_mint_failed")
            raise CoordinatorError(str(code)) from None
        return {
            "protocol": "mycelium.product_ui.swarm.v1",
            "invite_id": nonce,
            "invite_code": invite_code,
            "capability": "native_inference_node",
            "expires_at_unix_ms": expires_at,
        }

    def join(self, _document: Mapping[str, Any]) -> Mapping[str, Any]:
        raise CoordinatorError("join_on_target_device_required")

    def leave(self, document: Mapping[str, Any]) -> Mapping[str, Any]:
        source = getattr(self._membership_source, "revoke_native_member", None)
        if not callable(source):
            raise CoordinatorError("swarm_operator_unavailable")
        member_id = document.get("member_id")
        try:
            source(member_id)
        except Exception as exc:
            code = getattr(exc, "code", "member_revoke_failed")
            raise CoordinatorError(str(code)) from None
        return {
            "protocol": "mycelium.product_ui.swarm.v1",
            "member_id": member_id,
            "left": True,
        }


@dataclass(frozen=True, slots=True)
class LiveStack:
    app: Any
    health: Any
    route: LiveRoute


def _load_trusted_proxy_capability(path: Path) -> bytes:
    """Load one exact owner-private capability through anchored descriptors."""

    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise ValueError("invalid_trusted_proxy_capability")
    components = candidate.parts
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    file_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptors: list[int] = []
    try:
        current = os.open("/", directory_flags)
        descriptors.append(current)
        for component in components[1:-1]:
            current = os.open(component, directory_flags, dir_fd=current)
            descriptors.append(current)
            if not stat.S_ISDIR(os.fstat(current).st_mode):
                raise ValueError("invalid_trusted_proxy_capability")
        parent = os.fstat(current)
        if (
            parent.st_uid != os.geteuid()
            or stat.S_IMODE(parent.st_mode) != 0o700
        ):
            raise ValueError("invalid_trusted_proxy_capability")
        descriptor = os.open(components[-1], file_flags, dir_fd=current)
        descriptors.append(descriptor)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink != 1
            or before.st_size != 64
        ):
            raise ValueError("invalid_trusted_proxy_capability")
        capability = os.read(descriptor, 65)
        after = os.fstat(descriptor)
        named = os.stat(
            components[-1],
            dir_fd=current,
            follow_symlinks=False,
        )
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) or (named.st_dev, named.st_ino) != (after.st_dev, after.st_ino):
            raise ValueError("invalid_trusted_proxy_capability")
        if len(capability) != 64 or any(
            value not in b"0123456789abcdef" for value in capability
        ):
            raise ValueError("invalid_trusted_proxy_capability")
        return capability
    except (OSError, ValueError) as exc:
        raise ValueError("invalid_trusted_proxy_capability") from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def build_live_stack(
    *,
    route: LiveRoute,
    deployment_dir: Path,
    execution_graph: Any,
    bearer_token: str,
    product_state_root: Path | None = None,
    seed_url: str | None = None,
    public_origin: str | None = None,
    trusted_https_proxy: bool = False,
    trusted_proxy_capability: bytes | None = None,
) -> LiveStack:
    """Compose the production browser and request gateways around a live route."""
    codec = prompt_codec_from_deployment(deployment_dir)
    stop_token_ids = getattr(codec, "stop_token_ids", frozenset())
    configure_stop_tokens = getattr(route, "set_stop_token_ids", None)
    if callable(configure_stop_tokens):
        configure_stop_tokens(stop_token_ids)
    placement = _placement_projection(deployment_dir)
    workload = _workload_comparison(deployment_dir)
    coordinator = build_live_m16_runtime(
        execution_graph,
        placement_projection=placement,
        workload_comparison=workload,
    )
    budget = _m16_performance_budget(deployment_dir)
    if budget is not None:
        coordinator.attach_performance_budget(budget)
    router = LiveRouterPort(
        route=route,
        execution_graph=execution_graph,
        runtime_coordinator=coordinator,
    )
    configure_runtime_source = getattr(route, "set_m16_runtime_source", None)
    if callable(configure_runtime_source):
        configure_runtime_source(router.runtime_status)
    health = RouteHealthSource(
        route=route,
        refresh=lambda: _qualify_open_route(route),
        refresh_allowed=router.is_idle,
    )
    product_app = _optional_product_application(
        route,
        health,
        state_root=product_state_root,
    )
    app = build_loopback_product_stack(
        qualification_source=health,
        router=router,
        codec=codec,
        observatory_app=LiveObservatoryApplication(health),
        product_app=product_app,
        swarm_coordinator=LiveSwarmCoordinator(route, health, seed_url=seed_url),
        request_bearer_token=bearer_token,
        public_origin=public_origin,
        trusted_https_proxy=trusted_https_proxy,
        trusted_proxy_capability=trusted_proxy_capability,
    )
    return LiveStack(app=app, health=health, route=route)


def build_registry_stack(
    *,
    registry: LiveDeploymentRegistry,
    bearer_token: str,
    product_state_root: Path | None = None,
    seed_url: str | None = None,
    public_origin: str | None = None,
    trusted_https_proxy: bool = False,
    trusted_proxy_capability: bytes | None = None,
) -> LiveStack:
    """Compose the product stack around multiple already-qualified routes."""

    app = build_loopback_product_stack(
        qualification_source=registry,
        router=registry,
        codec=registry,
        observatory_app=LiveObservatoryApplication(registry),
        product_app=_optional_product_application(
            registry,
            registry,
            state_root=product_state_root,
        ),
        swarm_coordinator=LiveSwarmCoordinator(registry, registry, seed_url=seed_url),
        request_bearer_token=bearer_token,
        public_origin=public_origin,
        trusted_https_proxy=trusted_https_proxy,
        trusted_proxy_capability=trusted_proxy_capability,
    )
    return LiveStack(app=app, health=registry, route=registry)


def _product_application(
    route: Any,
    qualification_source: Any,
    *,
    state_root: Path | None = None,
) -> Any:
    membership = getattr(route, "product_membership_records", None)
    salt = getattr(route, "product_pseudonym_salt", None)
    public_status = getattr(route, "public_status", None)
    assignments = getattr(route, "product_assignment_records", None)
    internet_native = getattr(route, "product_internet_native_snapshot", None)
    if not all(callable(value) for value in (membership, salt, public_status)):
        raise ValueError("product_evidence_source_unavailable")

    def qualification_document() -> Mapping[str, Any] | None:
        record = qualification_source.current()
        return None if record is None else route_qualification_to_dict(record)

    def internet_native_document() -> Mapping[str, Any]:
        if not callable(internet_native):
            raise ValueError("product_internet_native_source_unavailable")
        document = internet_native()
        if not isinstance(document, Mapping):
            raise ValueError("product_internet_native_source_invalid")
        return document

    return ProductEvidenceApplication(
        projector=ProductProjector(pseudonym_salt=salt()),
        membership_source=membership,
        assignment_source=assignments if callable(assignments) else None,
        route_source=public_status,
        qualification_source=qualification_document,
        internet_native_source=(
            internet_native_document if callable(internet_native) else None
        ),
        state_root=state_root,
    )


def _optional_product_application(
    route: Any,
    qualification_source: Any,
    *,
    state_root: Path | None = None,
) -> Any | None:
    required = (
        getattr(route, "product_membership_records", None),
        getattr(route, "product_pseudonym_salt", None),
        getattr(route, "public_status", None),
    )
    if not all(callable(value) for value in required):
        return None
    return _product_application(route, qualification_source, state_root=state_root)


class LiveHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    app: Any
    route: LiveRoute
    static_root: Path
    activation: PreparedDeploymentActivation | None
    capacity_refresh: ModelCapacityRefresh | None
    model_preparation: LocalModelPreparation | None
    governance_projection: Mapping[str, Any] | None
    evidence_registry: EvidenceProjectionRegistry | None
    artifact_acquisition_store: ArtifactAcquisitionStore | None
    trusted_https_proxy: bool


def _handler() -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def _security_headers(self) -> None:
            self.send_header("cache-control", "no-store")
            self.send_header("content-security-policy", CONTENT_SECURITY_POLICY)
            self.send_header("x-content-type-options", "nosniff")
            self.send_header("x-frame-options", "DENY")
            self.send_header("referrer-policy", "no-referrer")

        def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("content-type", content_type)
            self._security_headers()
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self, *, limit: int) -> bytes | None:
            try:
                length = int(self.headers.get("content-length", "0"))
            except ValueError:
                length = -1
            if length < 0:
                self._send_bytes(
                    400,
                    _json_bytes({"error": "invalid_request"}),
                    "application/json; charset=utf-8",
                )
                return None
            if length > limit:
                self.close_connection = True
                self._send_bytes(
                    413,
                    _json_bytes({"error": "request_body_too_large"}),
                    "application/json; charset=utf-8",
                )
                return None
            return self.rfile.read(length) if length else b""

        def _serve_static(self, path: str) -> None:
            root = self.server.static_root.resolve()
            relative = (
                "index.html"
                if path in {"/", "/index.html"}
                else unquote(path.lstrip("/"))
            )
            candidate = (root / relative).resolve()
            if root not in candidate.parents or not candidate.is_file():
                candidate = root / "index.html"
            if not candidate.is_file():
                self._send_bytes(404, b"not found", "text/plain; charset=utf-8")
                return
            content_type = (
                mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
            )
            self._send_bytes(200, candidate.read_bytes(), content_type)

        def _status(self) -> None:
            route = self.server.route
            public_status = getattr(route, "public_status", None)
            document = (
                public_status()
                if callable(public_status)
                else {
                    "protocol": "mycelium.live_status.v1",
                    "route_alive": route.is_alive(),
                    "simulated": bool(getattr(route, "is_simulated", False)),
                    "counters": _counter_document(route.counters()),
                }
            )
            body = _json_bytes(document)
            self._send_bytes(200, body, "application/json; charset=utf-8")


        def _deployment_registry(self) -> None:
            registry_status = getattr(self.server.route, "registry_status", None)
            if not callable(registry_status):
                self._send_bytes(
                    404,
                    _json_bytes({"error": "deployment_registry_unavailable"}),
                    "application/json; charset=utf-8",
                )
                return
            self._send_bytes(
                200,
                _json_bytes(registry_status()),
                "application/json; charset=utf-8",
            )

        def _deployment_activation_status(self) -> None:
            activation = self.server.activation
            if activation is None:
                self._send_bytes(
                    404,
                    _json_bytes({"error": "deployment_activation_unavailable"}),
                    "application/json; charset=utf-8",
                )
                return
            try:
                document = activation.status()
            except DeploymentActivationError as exc:
                self._send_bytes(
                    503,
                    _json_bytes({"error": exc.code}),
                    "application/json; charset=utf-8",
                )
                return
            self._send_bytes(
                200,
                _json_bytes(document),
                "application/json; charset=utf-8",
            )

        def _start_deployment_activation(self) -> None:
            activation = self.server.activation
            if activation is None:
                self._send_bytes(
                    404,
                    _json_bytes({"error": "deployment_activation_unavailable"}),
                    "application/json; charset=utf-8",
                )
                return
            if self.headers.get("origin") != f"http://{self.headers.get('host', '')}":
                self._send_bytes(
                    403,
                    _json_bytes({"error": "origin_mismatch"}),
                    "application/json; charset=utf-8",
                )
                return
            body = self._read_body(limit=4_096)
            if body is None:
                return
            try:
                document = json.loads(body)
                if (
                    not isinstance(document, dict)
                    or set(document) != {"candidate_id"}
                    or not isinstance(document["candidate_id"], str)
                ):
                    raise ValueError
                status = activation.activate(document["candidate_id"])
            except DeploymentActivationError as exc:
                self._send_bytes(
                    409,
                    _json_bytes({"error": exc.code}),
                    "application/json; charset=utf-8",
                )
                return
            except (TypeError, ValueError, json.JSONDecodeError):
                self._send_bytes(
                    400,
                    _json_bytes({"error": "invalid_activation_request"}),
                    "application/json; charset=utf-8",
                )
                return
            self._send_bytes(
                202,
                _json_bytes(status),
                "application/json; charset=utf-8",
            )

        def _unload_deployment_activation(self) -> None:
            activation = self.server.activation
            if activation is None:
                self._send_bytes(
                    404,
                    _json_bytes({"error": "deployment_activation_unavailable"}),
                    "application/json; charset=utf-8",
                )
                return
            if self.headers.get("origin") != f"http://{self.headers.get('host', '')}":
                self._send_bytes(
                    403,
                    _json_bytes({"error": "origin_mismatch"}),
                    "application/json; charset=utf-8",
                )
                return
            body = self._read_body(limit=4_096)
            if body is None:
                return
            try:
                document = json.loads(body)
                if (
                    not isinstance(document, dict)
                    or set(document) != {"candidate_id"}
                    or not isinstance(document["candidate_id"], str)
                ):
                    raise ValueError
                status = activation.unload(document["candidate_id"])
            except DeploymentActivationError as exc:
                self._send_bytes(
                    409,
                    _json_bytes({"error": exc.code}),
                    "application/json; charset=utf-8",
                )
                return
            except (TypeError, ValueError, json.JSONDecodeError):
                self._send_bytes(
                    400,
                    _json_bytes({"error": "invalid_activation_request"}),
                    "application/json; charset=utf-8",
                )
                return
            self._send_bytes(
                200,
                _json_bytes(status),
                "application/json; charset=utf-8",
            )

        def _model_capacity_refresh_status(self) -> None:
            refresh = self.server.capacity_refresh
            if refresh is None:
                self._send_bytes(
                    404,
                    _json_bytes({"error": "model_capacity_refresh_unavailable"}),
                    "application/json; charset=utf-8",
                )
                return
            self._send_bytes(
                200,
                _json_bytes(refresh.status()),
                "application/json; charset=utf-8",
            )

        def _start_model_capacity_refresh(self) -> None:
            refresh = self.server.capacity_refresh
            if refresh is None:
                self._send_bytes(
                    404,
                    _json_bytes({"error": "model_capacity_refresh_unavailable"}),
                    "application/json; charset=utf-8",
                )
                return
            if self.headers.get("origin") != f"http://{self.headers.get('host', '')}":
                self._send_bytes(
                    403,
                    _json_bytes({"error": "origin_mismatch"}),
                    "application/json; charset=utf-8",
                )
                return
            body = self._read_body(limit=256)
            if body is None:
                return
            try:
                document = json.loads(body)
                if not isinstance(document, dict) or document:
                    raise ValueError
                status = refresh.start()
            except ModelCapacityRefreshError as exc:
                self._send_bytes(
                    409,
                    _json_bytes({"error": exc.code}),
                    "application/json; charset=utf-8",
                )
                return
            except (TypeError, ValueError, json.JSONDecodeError):
                self._send_bytes(
                    400,
                    _json_bytes({"error": "invalid_capacity_refresh_request"}),
                    "application/json; charset=utf-8",
                )
                return
            self._send_bytes(
                202,
                _json_bytes(status),
                "application/json; charset=utf-8",
            )

        def _model_preparation_status(self) -> None:
            preparation = self.server.model_preparation
            if preparation is None:
                self._send_bytes(
                    404,
                    _json_bytes({"error": "model_preparation_unavailable"}),
                    "application/json; charset=utf-8",
                )
                return
            self._send_bytes(
                200,
                _json_bytes(preparation.status()),
                "application/json; charset=utf-8",
            )

        def _artifact_acquisition_ledger(self) -> None:
            store = self.server.artifact_acquisition_store
            if store is None:
                self._send_bytes(
                    404,
                    _json_bytes({"error": "artifact_acquisition_unavailable"}),
                    "application/json; charset=utf-8",
                )
                return
            self._send_bytes(
                200,
                _json_bytes(store.public_ledger()),
                "application/json; charset=utf-8",
            )

        def _governance_readiness(self) -> None:
            projection = self.server.governance_projection
            if projection is None:
                self._send_bytes(
                    404,
                    _json_bytes({"error": "governance_readiness_unavailable"}),
                    "application/json; charset=utf-8",
                )
                return
            self._send_bytes(
                200,
                _json_bytes(projection),
                "application/json; charset=utf-8",
            )

        def _runtime_evidence(self) -> None:
            registry = self.server.evidence_registry
            if registry is None:
                self._send_bytes(
                    404,
                    _json_bytes({"error": "runtime_evidence_unavailable"}),
                    "application/json; charset=utf-8",
                )
                return
            try:
                document = registry.runtime()
            except (TypeError, ValueError, RuntimeError):
                self._send_bytes(
                    503,
                    _json_bytes({"error": "runtime_evidence_unavailable"}),
                    "application/json; charset=utf-8",
                )
                return
            self._send_bytes(
                200,
                _json_bytes(document),
                "application/json; charset=utf-8",
            )

        def _historical_evidence(self, query: str) -> None:
            registry = self.server.evidence_registry
            if registry is None:
                self._send_bytes(
                    404,
                    _json_bytes({"error": "historical_evidence_unavailable"}),
                    "application/json; charset=utf-8",
                )
                return
            capability: str | None = None
            if query:
                parts = query.split("=")
                if len(parts) != 2 or parts[0] != "capability" or not parts[1]:
                    self._send_bytes(
                        400,
                        _json_bytes({"error": "evidence_filter_invalid"}),
                        "application/json; charset=utf-8",
                    )
                    return
                capability = unquote(parts[1])
            try:
                document = registry.history(capability=capability)
            except ValueError:
                self._send_bytes(
                    400,
                    _json_bytes({"error": "evidence_filter_invalid"}),
                    "application/json; charset=utf-8",
                )
                return
            self._send_bytes(
                200,
                _json_bytes(document),
                "application/json; charset=utf-8",
            )

        def _start_model_preparation(self) -> None:
            preparation = self.server.model_preparation
            if preparation is None:
                self._send_bytes(
                    404,
                    _json_bytes({"error": "model_preparation_unavailable"}),
                    "application/json; charset=utf-8",
                )
                return
            if self.headers.get("origin") != f"http://{self.headers.get('host', '')}":
                self._send_bytes(
                    403,
                    _json_bytes({"error": "origin_mismatch"}),
                    "application/json; charset=utf-8",
                )
                return
            body = self._read_body(limit=1_024)
            if body is None:
                return
            try:
                document = json.loads(body)
                if (
                    not isinstance(document, dict)
                    or set(document) != {"decision"}
                    or not isinstance(document["decision"], dict)
                ):
                    raise ValueError
                status = preparation.start(document["decision"])
            except ModelPreparationError as exc:
                self._send_bytes(
                    409,
                    _json_bytes({"error": exc.code}),
                    "application/json; charset=utf-8",
                )
                return
            except (TypeError, ValueError, json.JSONDecodeError):
                self._send_bytes(
                    400,
                    _json_bytes({"error": "invalid_model_preparation_request"}),
                    "application/json; charset=utf-8",
                )
                return
            self._send_bytes(
                202,
                _json_bytes(status),
                "application/json; charset=utf-8",
            )

        def _reacquire_model_candidate(self) -> None:
            preparation = self.server.model_preparation
            if preparation is None:
                self._send_bytes(
                    404,
                    _json_bytes({"error": "model_preparation_unavailable"}),
                    "application/json; charset=utf-8",
                )
                return
            if self.headers.get("origin") != f"http://{self.headers.get('host', '')}":
                self._send_bytes(
                    403,
                    _json_bytes({"error": "origin_mismatch"}),
                    "application/json; charset=utf-8",
                )
                return
            body = self._read_body(limit=1_536)
            if body is None:
                return
            try:
                document = json.loads(body)
                if (
                    not isinstance(document, dict)
                    or set(document) != {"candidate_id", "decision"}
                    or not isinstance(document["candidate_id"], str)
                    or not isinstance(document["decision"], dict)
                ):
                    raise ValueError
                status = preparation.reacquire(
                    document["decision"], document["candidate_id"]
                )
            except ModelPreparationError as exc:
                self._send_bytes(
                    409,
                    _json_bytes({"error": exc.code}),
                    "application/json; charset=utf-8",
                )
                return
            except (TypeError, ValueError, json.JSONDecodeError):
                self._send_bytes(
                    400,
                    _json_bytes({"error": "invalid_model_reacquisition_request"}),
                    "application/json; charset=utf-8",
                )
                return
            self._send_bytes(
                202,
                _json_bytes(status),
                "application/json; charset=utf-8",
            )

        def _m15_plan_comparison(self) -> None:
            source = getattr(self.server.route, "m15_plan_comparison", None)
            document = source() if callable(source) else None
            if document is None:
                self._send_bytes(
                    404,
                    _json_bytes({"error": "m15_plan_comparison_unavailable"}),
                    "application/json; charset=utf-8",
                )
                return
            self._send_bytes(
                200,
                _json_bytes(document),
                "application/json; charset=utf-8",
            )

        def _m16_runtime_status(self) -> None:
            source = getattr(self.server.route, "m16_runtime_status", None)
            document = source() if callable(source) else None
            if document is None:
                self._send_bytes(
                    404,
                    _json_bytes({"error": "m16_runtime_status_unavailable"}),
                    "application/json; charset=utf-8",
                )
                return
            self._send_bytes(
                200,
                _json_bytes(document),
                "application/json; charset=utf-8",
            )

        def _m17_model_operation(self) -> None:
            source = getattr(self.server.route, "m17_model_operation", None)
            document = source() if callable(source) else None
            if document is None:
                self._send_bytes(
                    404,
                    _json_bytes({"error": "model_operation_unavailable"}),
                    "application/json; charset=utf-8",
                )
                return
            self._send_bytes(
                200,
                _json_bytes(document),
                "application/json; charset=utf-8",
            )

        def _m17_swarm_evidence(self) -> None:
            source = getattr(self.server.route, "m17_swarm_evidence", None)
            if not callable(source):
                self._send_bytes(
                    404,
                    _json_bytes({"error": "swarm_evidence_unavailable"}),
                    "application/json; charset=utf-8",
                )
                return
            try:
                document = source()
            except RuntimeError as exc:
                self._send_bytes(
                    503,
                    _json_bytes({"error": str(exc)}),
                    "application/json; charset=utf-8",
                )
                return
            self._send_bytes(
                200,
                _json_bytes(document),
                "application/json; charset=utf-8",
            )

        def _select_deployment(self) -> None:
            select = getattr(self.server.route, "select", None)
            if not callable(select):
                self._send_bytes(
                    404,
                    _json_bytes({"error": "deployment_registry_unavailable"}),
                    "application/json; charset=utf-8",
                )
                return
            expected_origin = f"http://{self.headers.get('host', '')}"
            if self.headers.get("origin") != expected_origin:
                self._send_bytes(
                    403,
                    _json_bytes({"error": "origin_mismatch"}),
                    "application/json; charset=utf-8",
                )
                return
            body = self._read_body(limit=4_096)
            if body is None:
                return
            if not body:
                self._send_bytes(
                    400,
                    _json_bytes({"error": "invalid_request"}),
                    "application/json; charset=utf-8",
                )
                return
            try:
                document = json.loads(body)
                if set(document) != {"deployment_id"} or not isinstance(
                    document["deployment_id"], str
                ):
                    raise ValueError
                status = select(document["deployment_id"])
            except DeploymentSelectionError as exc:
                self._send_bytes(
                    409,
                    _json_bytes({"error": str(exc)}),
                    "application/json; charset=utf-8",
                )
                return
            except (TypeError, ValueError, json.JSONDecodeError):
                self._send_bytes(
                    400,
                    _json_bytes({"error": "invalid_request"}),
                    "application/json; charset=utf-8",
                )
                return
            self._send_bytes(
                200,
                _json_bytes(status),
                "application/json; charset=utf-8",
            )

        def _promote_candidate(self) -> None:
            promote = getattr(self.server.route, "promote_candidate", None)
            if not callable(promote):
                self._send_bytes(
                    404,
                    _json_bytes({"error": "candidate_registry_unavailable"}),
                    "application/json; charset=utf-8",
                )
                return
            if self.headers.get("origin") != f"http://{self.headers.get('host', '')}":
                self._send_bytes(
                    403,
                    _json_bytes({"error": "origin_mismatch"}),
                    "application/json; charset=utf-8",
                )
                return
            body = self._read_body(limit=256 * 1024)
            if body is None:
                return
            try:
                document = json.loads(body)
                status = promote(document)
            except DeploymentSelectionError as exc:
                self._send_bytes(
                    409,
                    _json_bytes({"error": str(exc)}),
                    "application/json; charset=utf-8",
                )
                return
            except (TypeError, ValueError, json.JSONDecodeError):
                self._send_bytes(
                    400,
                    _json_bytes({"error": "invalid_candidate_report"}),
                    "application/json; charset=utf-8",
                )
                return
            self._send_bytes(
                200, _json_bytes(status), "application/json; charset=utf-8"
            )

        def _canary_candidate(self) -> None:
            canary = getattr(self.server.route, "canary_candidate", None)
            if not callable(canary):
                self._send_bytes(
                    404,
                    _json_bytes({"error": "candidate_registry_unavailable"}),
                    "application/json; charset=utf-8",
                )
                return
            if self.headers.get("origin") != f"http://{self.headers.get('host', '')}":
                self._send_bytes(
                    403,
                    _json_bytes({"error": "origin_mismatch"}),
                    "application/json; charset=utf-8",
                )
                return
            body = self._read_body(limit=16_384)
            if body is None:
                return
            try:
                document = json.loads(body)
                if set(document) != {
                    "candidate_deployment_id",
                    "case_id",
                    "prompt",
                    "max_new_tokens",
                }:
                    raise ValueError
                result = canary(
                    document["candidate_deployment_id"],
                    case_id=document["case_id"],
                    prompt=document["prompt"],
                    max_new_tokens=document["max_new_tokens"],
                )
            except DeploymentSelectionError as exc:
                self._send_bytes(
                    409,
                    _json_bytes({"error": str(exc)}),
                    "application/json; charset=utf-8",
                )
                return
            except (TypeError, ValueError, json.JSONDecodeError):
                self._send_bytes(
                    400,
                    _json_bytes({"error": "invalid_candidate_canary"}),
                    "application/json; charset=utf-8",
                )
                return
            self._send_bytes(
                200, _json_bytes(result), "application/json; charset=utf-8"
            )

        def _rollback_candidate(self) -> None:
            rollback = getattr(self.server.route, "rollback_candidate", None)
            if not callable(rollback):
                self._send_bytes(
                    404,
                    _json_bytes({"error": "candidate_registry_unavailable"}),
                    "application/json; charset=utf-8",
                )
                return
            if self.headers.get("origin") != f"http://{self.headers.get('host', '')}":
                self._send_bytes(
                    403,
                    _json_bytes({"error": "origin_mismatch"}),
                    "application/json; charset=utf-8",
                )
                return
            body = self._read_body(limit=4_096)
            if body is None:
                return
            try:
                document = json.loads(body)
                if set(document) != {"candidate_deployment_id", "reason"}:
                    raise ValueError
                status = rollback(
                    document["candidate_deployment_id"], reason=document["reason"]
                )
            except DeploymentSelectionError as exc:
                self._send_bytes(
                    409,
                    _json_bytes({"error": str(exc)}),
                    "application/json; charset=utf-8",
                )
                return
            except (TypeError, ValueError, json.JSONDecodeError):
                self._send_bytes(
                    400,
                    _json_bytes({"error": "invalid_candidate_rollback"}),
                    "application/json; charset=utf-8",
                )
                return
            self._send_bytes(
                200, _json_bytes(status), "application/json; charset=utf-8"
            )

        def _asgi(self) -> None:
            parsed = urlsplit(self.path)
            body = self._read_body(limit=MAX_REQUEST_BODY_BYTES)
            if body is None:
                return
            headers = [
                (name.lower().encode("latin-1"), value.encode("latin-1"))
                for name, value in self.headers.items()
            ]
            incoming = True
            response_started = False

            async def receive() -> dict[str, Any]:
                nonlocal incoming
                if incoming:
                    incoming = False
                    return {"type": "http.request", "body": body, "more_body": False}
                await asyncio.Event().wait()
                return {"type": "http.disconnect"}

            async def send(message: Mapping[str, Any]) -> None:
                nonlocal response_started
                if message.get("type") == "http.response.start":
                    self.send_response(int(message["status"]))
                    for name, value in message.get("headers", ()):
                        self.send_header(
                            name.decode("latin-1"), value.decode("latin-1")
                        )
                    self._security_headers()
                    self.end_headers()
                    response_started = True
                    return
                if message.get("type") == "http.response.body":
                    if not response_started:
                        raise RuntimeError("asgi_response_not_started")
                    chunk = message.get("body", b"")
                    if chunk:
                        try:
                            self.wfile.write(chunk)
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError):
                            raise asyncio.CancelledError from None
                    if not message.get("more_body", False):
                        self.close_connection = True

            forwarded_proto = [
                value
                for name, value in headers
                if name == b"x-forwarded-proto"
            ]
            scheme = (
                "https"
                if getattr(self.server, "trusted_https_proxy", False)
                and forwarded_proto == [b"https"]
                else "http"
            )
            scope = {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": self.request_version.removeprefix("HTTP/"),
                "method": self.command,
                "scheme": scheme,
                "path": parsed.path,
                "raw_path": parsed.path.encode("utf-8"),
                "query_string": parsed.query.encode("ascii"),
                "headers": headers,
                "client": (self.client_address[0], self.client_address[1]),
                "server": self.server.server_address,
            }
            try:
                asyncio.run(self.server.app(scope, receive, send))
            except (BrokenPipeError, ConnectionResetError, asyncio.CancelledError):
                self.close_connection = True

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlsplit(self.path)
            if parsed.path == "/__mycelium/live-status" and not parsed.query:
                self._status()
            elif (
                parsed.path == "/__mycelium/planning/workload-comparison"
                and not parsed.query
            ):
                self._m15_plan_comparison()
            elif (
                parsed.path == "/__mycelium/runtime/admission-status"
                and not parsed.query
            ):
                self._m16_runtime_status()
            elif parsed.path == "/__mycelium/models/operation" and not parsed.query:
                self._m17_model_operation()
            elif (
                parsed.path == "/__mycelium/swarm/resource-observations"
                and not parsed.query
            ):
                self._m17_swarm_evidence()
            elif parsed.path == "/__mycelium/deployments" and not parsed.query:
                self._deployment_registry()
            elif (
                parsed.path == "/__mycelium/deployment-activation" and not parsed.query
            ):
                self._deployment_activation_status()
            elif (
                parsed.path == "/__mycelium/model-capacity-refresh" and not parsed.query
            ):
                self._model_capacity_refresh_status()
            elif parsed.path == "/__mycelium/model-preparation" and not parsed.query:
                self._model_preparation_status()
            elif (
                parsed.path == "/__mycelium/artifacts/acquisitions" and not parsed.query
            ):
                self._artifact_acquisition_ledger()
            elif parsed.path == "/__mycelium/governance-readiness" and not parsed.query:
                self._governance_readiness()
            elif parsed.path == "/__mycelium/evidence/runtime" and not parsed.query:
                self._runtime_evidence()
            elif parsed.path == "/__mycelium/evidence/history":
                self._historical_evidence(parsed.query)
            elif parsed.path.startswith("/__mycelium/"):
                self._send_bytes(
                    404,
                    _json_bytes({"error": "product_endpoint_unknown"}),
                    "application/json; charset=utf-8",
                )
            elif parsed.path.startswith("/api/"):
                self._asgi()
            else:
                self._serve_static(parsed.path)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlsplit(self.path)
            if parsed.path == "/__mycelium/deployments/select" and not parsed.query:
                self._select_deployment()
            elif (
                parsed.path == "/__mycelium/deployment-activation/start"
                and not parsed.query
            ):
                self._start_deployment_activation()
            elif (
                parsed.path == "/__mycelium/deployment-activation/unload"
                and not parsed.query
            ):
                self._unload_deployment_activation()
            elif (
                parsed.path == "/__mycelium/model-capacity-refresh/start"
                and not parsed.query
            ):
                self._start_model_capacity_refresh()
            elif (
                parsed.path == "/__mycelium/model-preparation/start"
                and not parsed.query
            ):
                self._start_model_preparation()
            elif (
                parsed.path == "/__mycelium/model-preparation/reacquire"
                and not parsed.query
            ):
                self._reacquire_model_candidate()
            elif parsed.path == "/__mycelium/candidates/canary" and not parsed.query:
                self._canary_candidate()
            elif parsed.path == "/__mycelium/candidates/promote" and not parsed.query:
                self._promote_candidate()
            elif parsed.path == "/__mycelium/candidates/rollback" and not parsed.query:
                self._rollback_candidate()
            else:
                self._asgi()

        def do_DELETE(self) -> None:  # noqa: N802
            self._asgi()

    return Handler


def create_server(
    *,
    app: Any,
    route: LiveRoute,
    static_root: Path,
    host: str,
    port: int,
    activation: PreparedDeploymentActivation | None = None,
    capacity_refresh: ModelCapacityRefresh | None = None,
    model_preparation: LocalModelPreparation | None = None,
    governance_projection: Mapping[str, Any] | None = None,
    evidence_registry: EvidenceProjectionRegistry | None = None,
    artifact_acquisition_store: ArtifactAcquisitionStore | None = None,
    trusted_https_proxy: bool = False,
) -> LiveHTTPServer:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("live_mvp_requires_loopback")
    static_root = Path(static_root)
    if not (static_root / "index.html").is_file():
        raise ValueError("live_ui_build_missing")
    server = LiveHTTPServer((host, port), _handler())
    server.app = app
    server.route = route
    server.static_root = static_root
    server.activation = activation
    server.capacity_refresh = capacity_refresh
    server.model_preparation = model_preparation
    server.governance_projection = governance_projection
    server.evidence_registry = evidence_registry
    server.artifact_acquisition_store = artifact_acquisition_store
    server.trusted_https_proxy = trusted_https_proxy
    return server


def _deployment_from_plan(plan: Path) -> Path:
    document = json.loads(Path(plan).read_text(encoding="utf-8"))
    return Path(document["controller"]["source_root"]) / "deployment"


def _placement_projection(deployment_dir: Path) -> Mapping[str, Any] | None:
    path = Path(deployment_dir) / "m13-placement-projection.json"
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("m13_placement_projection_unsafe")
    try:
        document = json.loads(path.read_text("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("m13_placement_projection_invalid") from exc
    return validate_m13_placement_projection(document)


def _topology_projection(deployment_dir: Path) -> Mapping[str, Any] | None:
    path = Path(deployment_dir) / "m14-topology-projection.json"
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("m14_topology_projection_unsafe")
    try:
        document = json.loads(path.read_text("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("m14_topology_projection_invalid") from exc
    try:
        return validate_m14_topology_projection(document)
    except ValueError as exc:
        raise ValueError("m14_topology_projection_invalid") from exc


def _workload_comparison(deployment_dir: Path) -> Mapping[str, Any] | None:
    path = Path(deployment_dir) / "m15-plan-comparison.json"
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("m15_plan_comparison_unsafe")
    try:
        document = json.loads(path.read_text("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("m15_plan_comparison_invalid") from exc
    try:
        return validate_m15_plan_comparison(document)
    except ValueError as exc:
        raise ValueError("m15_plan_comparison_invalid") from exc


def _m16_performance_budget(deployment_dir: Path) -> Mapping[str, Any] | None:
    path = Path(deployment_dir) / "m16-performance-budget.json"
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("m16_performance_budget_unsafe")
    try:
        document = json.loads(path.read_text("utf-8"))
        return validate_performance_budget_v3(document)
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        ValueError,
        RecursionError,
    ) as exc:
        raise ValueError("m16_performance_budget_invalid") from exc


def _m17_model_operation(
    deployment_dir: Path,
    *,
    explicit_path: Path | None = None,
) -> Mapping[str, Any] | None:
    path = (
        Path(explicit_path)
        if explicit_path is not None
        else Path(deployment_dir) / "m17-model-operation.json"
    )
    if not path.exists():
        if explicit_path is not None:
            raise ValueError("model_operation_missing")
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("model_operation_unsafe")
    try:
        document = json.loads(path.read_text("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("model_operation_invalid") from exc
    if (
        not isinstance(document, dict)
        or document.get("protocol") != "mycelium.model_operation.v1"
    ):
        raise ValueError("model_operation_invalid")
    return document


def _representation_authorization(path: Path | None) -> Mapping[str, Any] | None:
    if path is None:
        return None
    candidate = Path(path)
    if (
        candidate.is_symlink()
        or not candidate.is_file()
        or candidate.stat().st_size > 1024 * 1024
    ):
        raise ValueError("representation_authorization_unsafe")
    try:
        document = json.loads(candidate.read_text("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("representation_authorization_invalid") from exc
    if (
        not isinstance(document, dict)
        or document.get("protocol")
        not in {
            "mycelium.model_preparation_authorization.v1",
            "mycelium.model_preparation_authorization.v2",
        }
    ):
        raise ValueError("representation_authorization_invalid")
    return document


def _m18_replica_plan(deployment_dir: Path) -> Mapping[str, Any] | None:
    path = Path(deployment_dir) / "m18-replica-plan.json"
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("m18_replica_plan_unsafe")
    try:
        document = json.loads(path.read_text("utf-8"))
        return validate_replica_plan(document)
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        ValueError,
        RecursionError,
    ) as exc:
        raise ValueError("m18_replica_plan_invalid") from exc


def _m18_replica_runtime(deployment_dir: Path) -> Mapping[str, Any] | None:
    path = Path(deployment_dir) / "m18-replica-runtime.json"
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("m18_replica_runtime_unsafe")
    try:
        document = json.loads(path.read_text("utf-8"))
        return validate_replica_runtime(document)
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        ValueError,
        RecursionError,
    ) as exc:
        raise ValueError("m18_replica_runtime_invalid") from exc


def _m19_projection(
    deployment_dir: Path, filename: str, validator: Any, error_name: str
) -> Mapping[str, Any] | None:
    path = Path(deployment_dir) / filename
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError(f"{error_name}_unsafe")
    try:
        return validator(json.loads(path.read_text("utf-8")))
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        ValueError,
        RecursionError,
    ) as exc:
        raise ValueError(f"{error_name}_invalid") from exc


def _m19_evidence(deployment_dir: Path) -> tuple[Mapping[str, Any] | None, ...]:
    return (
        _m19_projection(
            deployment_dir, "m19-liveness.json", validate_liveness, "m19_liveness"
        ),
        _m19_projection(
            deployment_dir,
            "m19-recovery-plan.json",
            validate_recovery_plan,
            "m19_recovery_plan",
        ),
        _m19_projection(
            deployment_dir,
            "m19-recovery-runtime.json",
            validate_recovery_runtime,
            "m19_recovery_runtime",
        ),
    )


def _m20_evidence(deployment_dir: Path) -> tuple[Mapping[str, Any] | None, ...]:
    return (
        _m19_projection(
            deployment_dir,
            "m20-speculative-plan.json",
            validate_speculative_plan,
            "m20_speculative_plan",
        ),
        _m19_projection(
            deployment_dir,
            "m20-speculative-runtime.json",
            validate_speculative_runtime,
            "m20_speculative_runtime",
        ),
    )


def _m21_evidence(deployment_dir: Path) -> Mapping[str, Any] | None:
    return _m19_projection(
        deployment_dir,
        "m21-heterogeneous.json",
        validate_heterogeneous_evidence,
        "m21_heterogeneous",
    )


def _m22_evidence(deployment_dir: Path) -> Mapping[str, Any] | None:
    return _m19_projection(
        deployment_dir,
        "m22-release.json",
        validate_release_evidence,
        "m22_release",
    )


def _m23_evidence(deployment_dir: Path) -> Mapping[str, Any] | None:
    return _m19_projection(
        deployment_dir,
        "m23-kv-gate.json",
        validate_m23_kv_evidence,
        "m23_kv",
    )


def _historical_evidence(
    deployment_dir: Path, *, deployment_id: str
) -> tuple[Mapping[str, Any], ...]:
    """Load only records with intrinsic Unix observation time and measured provenance."""

    records: list[Mapping[str, Any]] = []
    replica_plan = _m18_replica_plan(deployment_dir)
    if replica_plan is not None:
        records.append(
            sealed_evidence_projection(
                record_id=f"replication-plan-{deployment_id}",
                capability="replicated_serving",
                authority="mycelium_m18_replication:build_replica_plan",
                generation=int(replica_plan["evidence"]["generation"]),
                observed_at_unix_ms=int(replica_plan["generated_at_unix_ms"]),
                payload=replica_plan,
            )
        )
    liveness, recovery_plan, _runtime = _m19_evidence(deployment_dir)
    if liveness is not None:
        records.append(
            sealed_evidence_projection(
                record_id=f"recovery-liveness-{deployment_id}",
                capability="scoped_recovery",
                authority="mycelium_m19_recovery:LivenessTracker",
                generation=int(liveness["binding"]["membership_generation"]),
                observed_at_unix_ms=int(liveness["generated_at_unix_ms"]),
                payload=liveness,
            )
        )
    if recovery_plan is not None:
        records.append(
            sealed_evidence_projection(
                record_id=f"recovery-plan-{deployment_id}",
                capability="scoped_recovery",
                authority="mycelium_m19_recovery:build_recovery_plan",
                generation=int(recovery_plan["binding"]["membership_generation"]),
                observed_at_unix_ms=int(recovery_plan["generated_at_unix_ms"]),
                payload=recovery_plan,
            )
        )
    kv_evidence = _m23_evidence(deployment_dir)
    if kv_evidence is not None:
        records.append(
            sealed_evidence_projection(
                record_id=f"stage-local-kv-{deployment_id}",
                capability="stage_local_kv",
                authority="scripts.run_m23_kv_gate:sealed_physical_gate",
                generation=0,
                observed_at_unix_ms=int(kv_evidence["generated_at_unix_ms"]),
                payload=kv_evidence,
            )
        )
    return tuple(records)


def _save_live_qualification(qualification: Any, path: Path) -> None:
    """Persist the freshly issued live-route qualification to a sealed file."""

    from mycelium_qualification.contracts import route_qualification_to_dict
    from mycelium_qualification.evidence import canonical_json_bytes

    document = route_qualification_to_dict(qualification)
    if path.exists() or path.is_symlink():
        raise ValueError("live_qualification_output_exists")
    body = canonical_json_bytes(document) + b"\n"
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())





def _explicit_historical_evidence(
    paths: Sequence[Path],
) -> tuple[Mapping[str, Any], ...]:
    """Load explicitly selected, intrinsically timestamped historical records."""

    records: list[Mapping[str, Any]] = []
    for raw_path in paths:
        path = Path(raw_path)
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size > 4 * 1024 * 1024
        ):
            raise ValueError("historical_evidence_file_unsafe")
        try:
            raw = json.loads(path.read_text("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
            raise ValueError("historical_evidence_file_invalid") from exc
        if not isinstance(raw, Mapping):
            raise ValueError("historical_evidence_file_invalid")
        if raw.get("protocol") == "mycelium.m23_heterogeneous_kv_gate.v1":
            document = validate_m23_kv_evidence(raw)
            records.append(
                sealed_evidence_projection(
                    record_id=f"stage-local-kv-{document['evidence_digest'][7:23]}",
                    capability="stage_local_kv",
                    authority="scripts.run_m23_kv_gate:sealed_physical_gate",
                    generation=0,
                    observed_at_unix_ms=int(document["generated_at_unix_ms"]),
                    payload=document,
                )
            )
            continue
        raise ValueError("historical_evidence_protocol_unsupported")
    return tuple(records)


def _qualify_open_route(route: Any) -> Any:
    """Renew authority by rerunning the exact physical startup challenge."""

    startup_prompt, startup_output = route.startup_challenge
    request_id = f"startup-{uuid.uuid4().hex}"
    try:
        try:
            result = route.infer(
                startup_prompt,
                max_new_tokens=len(startup_output),
                request_id=request_id,
                sink=_DiscardSink(),
            )
        except ControllerError as exc:
            safe_code = exc.remote_code or exc.code
            raise RuntimeError(f"startup_route_rejected:{safe_code}") from exc
        if result.token_ids != startup_output or route.counters().fatal is not None:
            raise RuntimeError("startup_challenge_failed")
        return issue_live_route_qualification(
            route.live_attestation(request_id=request_id),
            expected_prompt_token_ids=startup_prompt,
            expected_output_token_ids=startup_output,
        )
    finally:
        route.release_request(request_id)


def _qualified_runtime(
    operator_plan: Path,
    *,
    seed_state_root: Path,
    deployment_dir: Path | None = None,
    model_operation_file: Path | None = None,
    progress: Callable[[str], None] | None = None,
    force_relay: bool = False,
) -> QualifiedDeploymentRuntime:
    if progress is not None:
        progress("validating_plan")
    route = PhysicalLiveRoute.from_operator_plan(
        operator_plan,
        seed_state_root=seed_state_root,
        force_relay=force_relay,
    )
    try:
        if progress is not None:
            progress("opening_route")
        identity = route.open()
        _validate_route_identity(identity, route.execution_graph)
        if progress is not None:
            progress("qualifying_route")
        qualification = _qualify_open_route(route)
        # The physical command path revalidates the exact qualifier-owned
        # digest at every mutation boundary. Registry-backed serving must bind
        # that authority just like the single-deployment server does.
        route.set_deployment_qualification(qualification)
        selected_deployment_dir = deployment_dir or _deployment_from_plan(operator_plan)
        codec = prompt_codec_from_deployment(selected_deployment_dir)
        route.set_stop_token_ids(getattr(codec, "stop_token_ids", frozenset()))
        return QualifiedDeploymentRuntime(
            deployment_id=identity.deployment_id,
            model_id=identity.model_id,
            model_revision=identity.resolved_commit,
            quantization="int8-weight-only",
            qualified_at_unix_ms=qualification.issued_at_unix_ms,
            route=route,
            graph=route.execution_graph,
            codec=codec,
            qualification=qualification,
            placement_projection=_placement_projection(selected_deployment_dir),
            topology_projection=_topology_projection(selected_deployment_dir),
            workload_comparison=_workload_comparison(selected_deployment_dir),
            m16_performance_budget=_m16_performance_budget(selected_deployment_dir),
            model_operation=_m17_model_operation(
                selected_deployment_dir,
                explicit_path=model_operation_file,
            ),
            replica_plan=_m18_replica_plan(selected_deployment_dir),
            historical_evidence=_historical_evidence(
                selected_deployment_dir,
                deployment_id=identity.deployment_id,
            ),
        )
    except BaseException:
        route.close()
        raise


def _configured_deployment(plan: Path) -> tuple[str, str, str, int]:
    """Read one immutable graph identity before attempting physical startup."""

    config = load_operator_plan(plan)
    run_plan = config.controller.get("run_plan")
    nodes = run_plan.get("nodes") if isinstance(run_plan, Mapping) else None
    if not isinstance(nodes, list) or not nodes:
        raise ValueError("configured_deployment_graph_unavailable")
    first = nodes[0]
    configure = first.get("configure") if isinstance(first, Mapping) else None
    graph_document = (
        configure.get("graph") if isinstance(configure, Mapping) else None
    )
    graph = execution_graph_from_document(graph_document)
    return (
        graph.deployment_id,
        graph.model_id,
        graph.resolved_commit,
        len(graph.stages),
    )


def _startup_reason(exc: Exception) -> str:
    """Reduce a startup exception to one bounded non-sensitive public code."""

    code = getattr(exc, "remote_code", None) or getattr(exc, "code", None)
    if not isinstance(code, str) or not code:
        code = str(exc).partition(":")[0]
    if (
        not code
        or len(code) > 64
        or not code[0].islower()
        or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789_"
            for character in code
        )
    ):
        return "startup_route_unavailable"
    return code


_SERVE_DIAGNOSTIC_PROTOCOL = "mycelium.live_serve_diagnostic.v1"
_SERVE_DIAGNOSTIC_MAX_CAUSES = 8
_SERVE_DIAGNOSTIC_TAIL_CHARS = 16_000


def _exception_chain_document(exc: BaseException) -> list[dict[str, Any]]:
    """Bounded walk of an exception cause chain with node stderr evidence.

    A rejected node command carries the node's own stderr as a diagnostic
    (ControllerError.diagnostic) or detail (NodeProcessError.detail) on
    the exception; cleanup in finally blocks can later replace the
    original error (physical_cleanup_failed masking), so the serve log
    must walk the chain to reach the real reason.
    """
    chain: list[dict[str, Any]] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and len(chain) < _SERVE_DIAGNOSTIC_MAX_CAUSES:
        identity = id(current)
        if identity in seen:
            break
        seen.add(identity)
        entry: dict[str, Any] = {"type": type(current).__name__}
        code = getattr(current, "remote_code", None) or getattr(current, "code", None)
        if isinstance(code, str) and code:
            entry["code"] = code
        diagnostic = getattr(current, "diagnostic", None)
        if not isinstance(diagnostic, str) or not diagnostic:
            diagnostic = getattr(current, "detail", None)
        if isinstance(diagnostic, str) and diagnostic:
            entry["node_stderr_tail"] = diagnostic[: _SERVE_DIAGNOSTIC_TAIL_CHARS]
        chain.append(entry)
        current = current.__cause__ if current.__cause__ is not None else current.__context__
    return chain


def _emit_serve_diagnostic(exc: BaseException, *, stage: str) -> None:
    """Surface the node-side reason a physical route open was rejected.

    Printed BEFORE any failure-path cleanup can replace the exception,
    so the serve log keeps the real rejection (and the rejecting node's
    stderr tail) even when cleanup afterwards reports its own error.
    """
    document = {
        "protocol": _SERVE_DIAGNOSTIC_PROTOCOL,
        "stage": stage,
        "cause_chain": _exception_chain_document(exc),
        "emitted_at_unix_ms": int(time.time() * 1_000),
    }
    print(_json_bytes(document).decode("utf-8"), flush=True)


def run_registry_server(
    *,
    operator_plans: Sequence[Path],
    host: str,
    port: int,
    static_root: Path | None = None,
    registry_state: Path | None = None,
    seed_state_root: Path,
    model_operation_file: Path | None = None,
    seed_url: str | None = None,
    candidate_plan_root: Path | None = None,
    artifact_acquisition_root: Path | None = None,
    model_cache_root: Path | None = None,
    model_preparation_template_plan: Path | None = None,
    model_preparation_execution_topology_plan: Path | None = None,
    model_preparation_root: Path | None = None,
    model_preparation_temporary_root: Path | None = None,
    model_preparation_artifact_bytes_per_second: int | None = None,
    member_artifact_transport_plan: Path | None = None,
    member_model_inventory_files: Sequence[Path] = (),
    member_model_inventory_authorities_file: Path | None = None,
    representation_authorization_file: Path | None = None,
    model_capacity_live_observations_file: Path | None = None,
    historical_evidence_files: Sequence[Path] = (),
    public_origin: str | None = None,
    trusted_https_proxy: bool = False,
    trusted_proxy_capability: bytes | None = None,
    force_relay: bool = False,
) -> int:
    """Open, qualify, and serve one or more immutable deployments."""

    if not operator_plans:
        raise ValueError("deployment_registry_requires_one_or_more")
    runtimes: list[QualifiedDeploymentRuntime] = []
    unavailable: list[UnavailableDeployment] = []
    server: LiveHTTPServer | None = None
    registry: LiveDeploymentRegistry | None = None
    activation: PreparedDeploymentActivation | None = None
    capacity_refresh: ModelCapacityRefresh | None = None
    model_preparation: LocalModelPreparation | None = None
    artifact_acquisition_store: ArtifactAcquisitionStore | None = None
    startup_complete = False
    try:
        representation_authorization = _representation_authorization(
            representation_authorization_file
        )
        for plan in operator_plans:
            deployment_id, model_id, revision, topology_size = _configured_deployment(
                plan
            )
            try:
                runtimes.append(
                    _qualified_runtime(
                        plan,
                        seed_state_root=seed_state_root,
                        model_operation_file=model_operation_file,
                        force_relay=force_relay,
                    )
                )
            except Exception as exc:
                unavailable.append(
                    UnavailableDeployment(
                        deployment_id=deployment_id,
                        model_id=model_id,
                        model_revision=revision,
                        quantization="int8-weight-only",
                        topology_size=topology_size,
                        reason_code=_startup_reason(exc),
                        observed_at_unix_ms=int(time.time() * 1_000),
                    )
                )
        registry = LiveDeploymentRegistry(
            runtimes,
            unavailable_deployments=tuple(unavailable),
            state_path=registry_state,
            qualification_refresher=lambda runtime: _qualify_open_route(runtime.route),
        )
        if not runtimes and model_operation_file is not None:
            operation = _m17_model_operation(
                Path("."),
                explicit_path=model_operation_file,
            )
            if operation is not None:
                registry.set_m17_model_operation(operation)
        artifact_acquisition_store = ArtifactAcquisitionStore(
            artifact_acquisition_root
            or (Path(seed_state_root) / "artifact-acquisitions")
        )
        if candidate_plan_root is not None:
            activation_state_root = Path(seed_state_root) / "deployment-activation"
            activation_state_root.mkdir(mode=0o700, exist_ok=True)
            activation = PreparedDeploymentActivation(
                candidate_root=candidate_plan_root,
                state_root=activation_state_root,
                registry=registry,
                runtime_loader=lambda plan, report: _qualified_runtime(
                    plan,
                    seed_state_root=seed_state_root,
                    model_operation_file=model_operation_file,
                    progress=report,
                    force_relay=force_relay,
                ),
            )
        if model_cache_root is not None:
            capacity_refresh = ModelCapacityRefresh(
                evaluator=lambda progress: recompute_model_operation(
                    cache_root=model_cache_root,
                    live_observations=(
                        live_observations_document(
                            model_capacity_live_observations_file
                        )
                        if model_capacity_live_observations_file is not None
                        else registry.m17_swarm_evidence()
                    ),
                    evaluated_at_unix_ms=int(time.time() * 1_000),
                    member_inventory_files=member_model_inventory_files,
                    member_inventory_authorities_file=(
                        member_model_inventory_authorities_file
                    ),
                    representation_authorization=representation_authorization,
                    progress=progress,
                ),
                operation_sink=registry.set_m17_model_operation,
            )
        if model_preparation_template_plan is not None:
            if model_cache_root is None or candidate_plan_root is None:
                raise ValueError("model_preparation_requires_cache_and_candidate_roots")
            preparation_root = model_preparation_root or (
                Path(seed_state_root) / "model-preparation"
            )
            preparer = LocalCandidatePreparer(
                repo_root=ROOT,
                cache_root=model_cache_root,
                template_plan=model_preparation_template_plan,
                execution_topology_plan=model_preparation_execution_topology_plan,
                workspace_root=preparation_root,
                temporary_root=model_preparation_temporary_root,
                artifact_transfer_bytes_per_second=(
                    model_preparation_artifact_bytes_per_second
                ),
                candidate_root=candidate_plan_root,
                seed_state_root=seed_state_root,
                artifact_store=artifact_acquisition_store,
                member_stage_pack_acquirer=(
                    None
                    if member_artifact_transport_plan is None
                    else MemberArtifactTransport(member_artifact_transport_plan)
                ),
            )
            model_preparation = LocalModelPreparation(
                operation_source=registry.m17_model_operation,
                preparer=preparer,
                reacquirer=preparer.reacquire,
                on_candidate_published=activation.refresh
                if activation is not None
                else None,
            )
        stack = build_registry_stack(
            registry=registry,
            bearer_token=secrets.token_urlsafe(32),
            product_state_root=seed_state_root,
            seed_url=seed_url,
            public_origin=public_origin,
            trusted_https_proxy=trusted_https_proxy,
            trusted_proxy_capability=trusted_proxy_capability,
        )
        evidence_registry = EvidenceProjectionRegistry(
            runtime_source=registry.public_status,
            historical_records=tuple(
                record for runtime in runtimes for record in runtime.historical_evidence
            )
            + _explicit_historical_evidence(historical_evidence_files),
        )
        server = create_server(
            app=stack.app,
            route=registry,
            static_root=static_root or ROOT / "ui" / "web" / "dist",
            host=host,
            port=port,
            activation=activation,
            capacity_refresh=capacity_refresh,
            model_preparation=model_preparation,
            governance_projection=governance_readiness(ROOT),
            evidence_registry=evidence_registry,
            artifact_acquisition_store=artifact_acquisition_store,
            trusted_https_proxy=trusted_https_proxy,
        )
        startup_complete = True
        stop = threading.Event()

        def request_stop(_signum: int, _frame: Any) -> None:
            stop.set()

        prior = {
            signum: signal.signal(signum, request_stop)
            for signum in (signal.SIGINT, signal.SIGTERM)
        }
        server.timeout = 0.5
        print(
            _json_bytes(
                {
                    "protocol": "mycelium.live_registry_server_started.v1",
                    "url": f"http://{host}:{port}/",
                    "startup_qualification_complete": bool(runtimes),
                    "simulated": False,
                    "deployment_count": len(runtimes),
                    "unavailable_deployment_count": len(unavailable),
                }
            ).decode("utf-8"),
            flush=True,
        )
        try:
            while not stop.is_set():
                server.handle_request()
        finally:
            for signum, handler in prior.items():
                signal.signal(signum, handler)
        return 0
    finally:
        if model_preparation is not None:
            model_preparation.close()
        if capacity_refresh is not None:
            capacity_refresh.close()
        if activation is not None:
            activation.close()
        if server is not None:
            server.server_close()
        if registry is not None:
            registry.close()
        else:
            for runtime in runtimes:
                runtime.route.close()
        if not startup_complete:
            for runtime in runtimes:
                try:
                    runtime.route.cleanup()
                except BaseException:
                    pass


def run_physical_server(
    *,
    operator_plan: Path,
    host: str,
    port: int,
    deployment_dir: Path | None = None,
    model_operation_file: Path | None = None,
    static_root: Path | None = None,
    seed_state_root: Path,
    seed_url: str | None = None,
    model_cache_root: Path | None = None,
    artifact_acquisition_root: Path | None = None,
    member_model_inventory_files: Sequence[Path] = (),
    member_model_inventory_authorities_file: Path | None = None,
    representation_authorization_file: Path | None = None,
    model_capacity_live_observations_file: Path | None = None,
    historical_evidence_files: Sequence[Path] = (),
    a4_evidence: Mapping[str, Any] | None = None,
    save_live_qualification: Path | None = None,
    public_origin: str | None = None,
    trusted_https_proxy: bool = False,
    trusted_proxy_capability: bytes | None = None,
    force_relay: bool = False,
) -> int:
    route = PhysicalLiveRoute.from_operator_plan(
        operator_plan,
        seed_state_root=seed_state_root,
        force_relay=force_relay,
    )
    stack: LiveStack | None = None
    server: LiveHTTPServer | None = None
    startup_complete = False
    capacity_refresh: ModelCapacityRefresh | None = None
    artifact_acquisition_store: ArtifactAcquisitionStore | None = None
    try:
        representation_authorization = _representation_authorization(
            representation_authorization_file
        )
        try:
            identity = route.open()
        except ControllerError as exc:
            safe_code = exc.remote_code or exc.code
            _emit_serve_diagnostic(exc, stage="route_open")
            raise RuntimeError(f"startup_route_open_rejected:{safe_code}") from exc
        except Exception as exc:
            # A node-process rejection (NodeProcessError) is NOT a
            # ControllerError: surface its node stderr before re-raising
            # with the same public shape as the controller path.
            _emit_serve_diagnostic(exc, stage="route_open")
            raise RuntimeError(
                f"startup_route_open_rejected:{_startup_reason(exc)}"
            ) from exc
        _validate_route_identity(identity, route.execution_graph)

        qualification = _qualify_open_route(route)
        route.set_deployment_qualification(qualification)
        if save_live_qualification is not None:
            _save_live_qualification(qualification, save_live_qualification)
        if a4_evidence is not None:
            graph = route.execution_graph
            a4_document = build_a4_qualification(
                **a4_evidence,
                qualification_digest=qualification_digest(qualification),
                graph_digest=getattr(graph, "graph_digest", None),
                manifest_digest=getattr(graph, "manifest_digest", None),
            )
            route.set_a4_qualification(a4_document)

        selected_deployment_dir = deployment_dir or _deployment_from_plan(operator_plan)
        route.set_public_projections(
            placement=_placement_projection(selected_deployment_dir),
            topology=_topology_projection(selected_deployment_dir),
            workload_comparison=_workload_comparison(selected_deployment_dir),
        )
        route.set_m17_model_operation(
            _m17_model_operation(
                selected_deployment_dir,
                explicit_path=model_operation_file,
            )
        )
        stack = build_live_stack(
            route=route,
            deployment_dir=selected_deployment_dir,
            execution_graph=route.execution_graph,
            bearer_token=secrets.token_urlsafe(32),
            product_state_root=seed_state_root,
            seed_url=seed_url,
            public_origin=public_origin,
            trusted_https_proxy=trusted_https_proxy,
            trusted_proxy_capability=trusted_proxy_capability,
        )
        stack.health.publish(qualification)
        artifact_acquisition_store = ArtifactAcquisitionStore(
            artifact_acquisition_root
            or (Path(seed_state_root) / "artifact-acquisitions")
        )
        if model_cache_root is not None:
            capacity_refresh = ModelCapacityRefresh(
                evaluator=lambda progress: recompute_model_operation(
                    cache_root=model_cache_root,
                    live_observations=(
                        live_observations_document(
                            model_capacity_live_observations_file
                        )
                        if model_capacity_live_observations_file is not None
                        else route.m17_swarm_evidence()
                    ),
                    evaluated_at_unix_ms=int(time.time() * 1_000),
                    member_inventory_files=member_model_inventory_files,
                    member_inventory_authorities_file=(
                        member_model_inventory_authorities_file
                    ),
                    representation_authorization=representation_authorization,
                    progress=progress,
                ),
                operation_sink=route.set_m17_model_operation,
            )
        server = create_server(
            app=stack.app,
            route=route,
            static_root=static_root or ROOT / "ui" / "web" / "dist",
            host=host,
            port=port,
            capacity_refresh=capacity_refresh,
            artifact_acquisition_store=artifact_acquisition_store,
            trusted_https_proxy=trusted_https_proxy,
            evidence_registry=EvidenceProjectionRegistry(
                runtime_source=route.public_status,
                historical_records=_historical_evidence(
                    selected_deployment_dir,
                    deployment_id=identity.deployment_id,
                )
                + _explicit_historical_evidence(historical_evidence_files),
            ),
        )
        startup_complete = True

        stop = threading.Event()

        def request_stop(_signum: int, _frame: Any) -> None:
            stop.set()

        prior = {
            signum: signal.signal(signum, request_stop)
            for signum in (signal.SIGINT, signal.SIGTERM)
        }
        server.timeout = 0.5
        print(
            _json_bytes(
                {
                    "protocol": "mycelium.live_server_started.v1",
                    "url": f"http://{host}:{port}/",
                    "route_ready": qualification.route_ready,
                    "simulated": False,
                    "startup_counters": _counter_document(route.counters()),
                }
            ).decode("utf-8"),
            flush=True,
        )
        try:
            while not stop.is_set():
                server.handle_request()
        finally:
            for signum, handler in prior.items():
                signal.signal(signum, handler)
        return 0
    finally:
        if capacity_refresh is not None:
            capacity_refresh.close()
        if stack is not None:
            stack.health.drop()
        if server is not None:
            server.server_close()
        route.close()
        if not startup_complete:
            try:
                route.cleanup()
            except Exception as cleanup_exc:
                # The failure-path cleanup must never mask the real
                # startup rejection: keep the original exception in
                # flight and record the cleanup failure in the serve
                # log instead.
                _emit_serve_diagnostic(cleanup_exc, stage="route_cleanup")


class _DiscardSink:
    def emit(self, _token_index: int, _token_id: int) -> None:
        return


def _a4_evidence_from_args(args: argparse.Namespace) -> Mapping[str, Any] | None:
    """Load owner-supplied A4 gate artifacts; None when no flag was supplied."""

    if not any(
        (
            args.a4_positive_observation,
            args.a4_negative_data_plane_observation,
            args.a4_negative_qualification_observation,
            args.a4_negative_shutdown_observation,
        )
    ):
        return None
    flags = (
        args.a4_positive_observation,
        args.a4_negative_data_plane_observation,
        args.a4_negative_qualification_observation,
        args.a4_negative_shutdown_observation,
    )
    if any(flag is None for flag in flags):
        raise SystemExit("--a4-* evidence flags require all four artifact kinds")
    return load_a4_evidence_files(
        positive=tuple(args.a4_positive_observation),
        data_plane=tuple(args.a4_negative_data_plane_observation),
        qualification=args.a4_negative_qualification_observation,
        shutdown=args.a4_negative_shutdown_observation,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3.14 -m mycelium_demo serve --mode live"
    )
    parser.add_argument("--operator-plan", type=Path, required=True, action="append")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--deployment-dir", type=Path)
    parser.add_argument("--model-operation-file", type=Path)
    parser.add_argument(
        "--model-cache-root",
        type=Path,
        help="read-only local Hugging Face cache used by explicit capacity rechecks",
    )
    parser.add_argument(
        "--model-capacity-live-observations-file",
        type=Path,
        help="owner-private fresh signed swarm generation used by capacity refresh",
    )
    parser.add_argument("--static-root", type=Path)
    parser.add_argument("--registry-state", type=Path)
    parser.add_argument("--historical-evidence-file", type=Path, action="append")
    parser.add_argument("--seed-state-root", type=Path, required=True)
    parser.add_argument(
        "--candidate-plan-root",
        type=Path,
        help="private directory of operator-prepared deployment plans",
    )
    parser.add_argument(
        "--artifact-acquisition-root",
        type=Path,
        help="owner-private assignment artifact store (may be on another volume)",
    )
    parser.add_argument(
        "--model-preparation-template-plan",
        type=Path,
        help="private physical plan supplying the current peer/runtime topology",
    )
    parser.add_argument(
        "--model-preparation-execution-topology-plan",
        type=Path,
        help="enrolled execution bindings ordered by the fresh capacity planner",
    )
    parser.add_argument(
        "--model-preparation-root",
        type=Path,
        help="owner-only workspace retaining built local model candidates",
    )
    parser.add_argument(
        "--model-preparation-temporary-root",
        type=Path,
        help="owner-private workspace for large ephemeral conversion files",
    )
    parser.add_argument(
        "--model-preparation-artifact-bytes-per-second",
        type=int,
        help="bounded preparation transfer rate enforced by grants and recipients",
    )
    parser.add_argument(
        "--member-artifact-transport-plan",
        type=Path,
        help="owner-private signed-source and recipient execution bindings",
    )
    parser.add_argument(
        "--member-model-inventory-file",
        type=Path,
        action="append",
        help="current signed member inventory; repeat for multiple members",
    )
    parser.add_argument(
        "--member-model-inventory-authorities-file",
        type=Path,
        help="owner-private current member generation and key bindings",
    )
    parser.add_argument(
        "--representation-authorization-file",
        type=Path,
        help="owner-approved immutable serving representation to revalidate",
    )
    parser.add_argument(
        "--seed-url",
        help="network URL advertised by the live durable seed; enables owner enrollment",
    )
    parser.add_argument(
        "--a4-positive-observation",
        type=Path,
        action="append",
        help="sealed A4 positive product observation; repeat for multiple runs",
    )
    parser.add_argument(
        "--a4-negative-data-plane-observation",
        type=Path,
        action="append",
        help="sealed A4 negative data-plane observation; repeat for multiple runs",
    )
    parser.add_argument(
        "--a4-negative-qualification-observation",
        type=Path,
        help="sealed A4 negative qualification observation (stale-qualification 409)",
    )
    parser.add_argument(
        "--a4-negative-shutdown-observation",
        type=Path,
        help="sealed A4 negative shutdown observation (bounded SIGTERM)",
    )
    parser.add_argument(
        "--save-live-qualification",
        type=Path,
        help="if set, persist the freshly issued live-route qualification to this path",
    )
    parser.add_argument(
        "--public-origin",
        help="exact external HTTPS origin when the loopback listener is behind a trusted proxy",
    )
    parser.add_argument(
        "--trusted-https-proxy",
        action="store_true",
        help="trust an authenticated HTTPS loopback proxy capability",
    )
    parser.add_argument("--trusted-proxy-capability-file", type=Path)
    parser.add_argument(
        "--a8-force-relay",
        action="store_true",
        help="operator-only physical qualification control; path evidence stays observed",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.port <= 65_535:
        raise SystemExit("port must be from 1 through 65535")
    if not (
        args.trusted_https_proxy
        == (args.public_origin is not None)
        == (args.trusted_proxy_capability_file is not None)
    ):
        raise SystemExit(
            "--trusted-https-proxy, --public-origin, and "
            "--trusted-proxy-capability-file require each other"
        )
    trusted_proxy_capability = None
    if args.trusted_proxy_capability_file is not None:
        try:
            trusted_proxy_capability = _load_trusted_proxy_capability(
                args.trusted_proxy_capability_file
            )
        except ValueError as exc:
            raise SystemExit("invalid trusted proxy capability file") from exc
    if len(args.operator_plan) == 1 and args.candidate_plan_root is None:
        if args.registry_state is not None:
            raise SystemExit(
                "--registry-state requires multiple --operator-plan values"
            )
        return run_physical_server(
            operator_plan=args.operator_plan[0],
            host=args.host,
            port=args.port,
            deployment_dir=args.deployment_dir,
            model_operation_file=args.model_operation_file,
            static_root=args.static_root,
            seed_state_root=args.seed_state_root,
            seed_url=args.seed_url,
            model_cache_root=args.model_cache_root,
            artifact_acquisition_root=args.artifact_acquisition_root,
            member_model_inventory_files=tuple(args.member_model_inventory_file or ()),
            member_model_inventory_authorities_file=(
                args.member_model_inventory_authorities_file
            ),
            representation_authorization_file=(args.representation_authorization_file),
            model_capacity_live_observations_file=(
                args.model_capacity_live_observations_file
            ),
            historical_evidence_files=tuple(args.historical_evidence_file or ()),
            save_live_qualification=args.save_live_qualification,
            a4_evidence=_a4_evidence_from_args(args),
            public_origin=args.public_origin,
            trusted_https_proxy=args.trusted_https_proxy,
            trusted_proxy_capability=trusted_proxy_capability,
            force_relay=args.a8_force_relay,
        )
    if args.deployment_dir is not None:
        raise SystemExit("--deployment-dir is valid only with one --operator-plan")
    return run_registry_server(
        operator_plans=args.operator_plan,
        host=args.host,
        port=args.port,
        static_root=args.static_root,
        registry_state=args.registry_state,
        seed_state_root=args.seed_state_root,
        model_operation_file=args.model_operation_file,
        seed_url=args.seed_url,
        candidate_plan_root=args.candidate_plan_root,
        artifact_acquisition_root=args.artifact_acquisition_root,
        model_cache_root=args.model_cache_root,
        model_preparation_template_plan=args.model_preparation_template_plan,
        model_preparation_execution_topology_plan=(
            args.model_preparation_execution_topology_plan
        ),
        model_preparation_root=args.model_preparation_root,
        model_preparation_temporary_root=args.model_preparation_temporary_root,
        model_preparation_artifact_bytes_per_second=(
            args.model_preparation_artifact_bytes_per_second
        ),
        member_artifact_transport_plan=args.member_artifact_transport_plan,
        member_model_inventory_files=tuple(args.member_model_inventory_file or ()),
        member_model_inventory_authorities_file=(
            args.member_model_inventory_authorities_file
        ),
        representation_authorization_file=args.representation_authorization_file,
        model_capacity_live_observations_file=(
            args.model_capacity_live_observations_file
        ),
        historical_evidence_files=tuple(args.historical_evidence_file or ()),
        public_origin=args.public_origin,
        trusted_https_proxy=args.trusted_https_proxy,
        trusted_proxy_capability=trusted_proxy_capability,
        force_relay=args.a8_force_relay,
    )


__all__ = [
    "LiveStack",
    "build_live_stack",
    "build_registry_stack",
    "create_server",
    "main",
    "run_physical_server",
    "run_registry_server",
]
