# SPDX-License-Identifier: AGPL-3.0-only
"""Qualify and serve the persistent physical route on loopback."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import hmac
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import secrets
import signal
import stat
import tempfile
import threading
import time
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import unquote, urlsplit
import uuid

from mycelium_deadlines import CANCEL_AND_CLEANUP_BUDGET_SECONDS
from mycelium_demo.product_stack import build_loopback_product_stack
from mycelium_a6_m16 import M16RecoveryAuthority
from mycelium_a6_recovery import ReplayCheckpoint
from mycelium_evidence import EvidenceProjectionRegistry, sealed_evidence_projection
from mycelium_governance import governance_readiness
from mycelium_host_platform import StopRequest
from mycelium_product_spine import (
    ProductEvidenceApplication, ProductEvidenceStateError, ProductProjector,
    ProductSourceUnavailable,
)
from mycelium_layer_planner.public_projection import validate_m13_placement_projection
from mycelium_layer_planner.workload_intelligence import validate_m15_plan_comparison
from mycelium_topology_evidence import validate_m14_topology_projection
from mycelium_qualification import (
    issue_live_route_qualification,
    route_qualification_to_dict,
)
from mycelium_qualification.contracts import RouteQualificationV1
from mycelium_request_gateway.asgi import MAX_REQUEST_BODY_BYTES
from mycelium_request_gateway.contracts import (
    AdmissionError,
    qualification_digest,
    safe_qualification_projection,
)
from mycelium_m16_runtime import build_live_m16_runtime
from mycelium_router.contracts import PathManifest
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

from .activation import DeploymentActivationError, PreparedDeploymentActivation
from .a7_authority import install_route_authority
from .a6_recovery import LiveA6RecoveryQualifier, tokenizer_artifact_digest
from .artifact_provisioner import ArtifactAcquisitionStore
from .codec import prompt_codec_from_deployment
from .health import LIVE_QUALIFICATION_REFRESH_AFTER_MS, RouteHealthSource
from .model_capacity import (
    ModelCapacityRefresh,
    ModelCapacityRefreshError,
    live_observations_document,
    recompute_model_operation,
)
from .local_preparer import LocalCandidatePreparer
from .member_transport import MemberArtifactTransport
from .preparation import LocalModelPreparation, ModelPreparationError
from .representation import native_deployment_projection
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
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class _A7StagedAuthority:
    stage_id: str
    entry_node_id: str
    source_node_id: str
    standby_node_id: str


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


def _a7_entry_command_only(*_args: Any, **_kwargs: Any) -> Any:
    raise RuntimeError("a7_entry_command_dispatch_only")


def _a7_route_authority_target(route: Any) -> Any | None:
    current = route
    seen: set[int] = set()
    for _ in range(8):
        if current is None or id(current) in seen:
            return None
        seen.add(id(current))
        try:
            setter = getattr(current, "set_peer_exit_successor_authority", None)
        except Exception:
            setter = None
        if callable(setter):
            return current
        try:
            current = getattr(current, "_route", None)
        except Exception:
            return None
    return None


def _a7_route_graph(route: Any) -> Any | None:
    try:
        current_deployment = getattr(route, "current_deployment", None)
    except Exception:
        current_deployment = None
    if callable(current_deployment):
        try:
            graph = current_deployment()
        except Exception:
            graph = None
        if graph is not None:
            return graph
    for name in ("execution_graph", "_graph"):
        try:
            graph = getattr(route, name, None)
        except Exception:
            graph = None
        if graph is not None:
            return graph
    return None


def _a7_route_id(route: Any) -> str:
    graph = _a7_route_graph(route)
    deployment_id = getattr(graph, "deployment_id", None)
    if isinstance(deployment_id, str) and deployment_id:
        return deployment_id
    return hex(id(route))


def _a7_stage_log_value(stage_id: str) -> str:
    text = str(stage_id)
    for prefix in ("stage-", "stage_"):
        if text.startswith(prefix) and text[len(prefix):].isdigit():
            return str(int(text[len(prefix):]))
    return text


A7_ATTESTED_CONFIG_FIELD = "a7_node_config"


def _a7_attested_node_configs(route: Any, peers: Mapping[str, Any]) -> dict[str, Any] | None:
    """The A7 node configs each live node attested in its signed ``configured`` observation.

    Detection is derived from the route's own verified observations, never from
    node-0's disk: a remote source's staging root is not readable here.  The
    route records an observation only after verifying its signature; the
    latest ``configured`` observation per peer is the current node process.
    An attestation from a node that is not a route peer fails closed (None).
    """

    try:
        envelopes = list(getattr(route, "_signed_observations", None) or ())
    except Exception:
        return None
    peer_nodes = {
        getattr(peer, "node_id", None) for peer in peers.values()
    }
    configured: dict[str, Mapping[str, Any]] = {}
    for envelope in envelopes:
        observation = envelope.get("observation") if isinstance(envelope, Mapping) else None
        if not isinstance(observation, Mapping) or observation.get("event") != "configured":
            continue
        node_id = observation.get("node_id")
        details = observation.get("details")
        if not isinstance(node_id, str) or not isinstance(details, Mapping):
            continue
        configured[node_id] = details
    attested: dict[str, Any] = {}
    for node_id, details in configured.items():
        if A7_ATTESTED_CONFIG_FIELD not in details:
            continue
        if node_id not in peer_nodes or not isinstance(details[A7_ATTESTED_CONFIG_FIELD], Mapping):
            return None
        attested[node_id] = json.loads(json.dumps(details[A7_ATTESTED_CONFIG_FIELD]))
    return attested


def _a7_staged_authority(route: Any) -> _A7StagedAuthority | None:
    graph = _a7_route_graph(route)
    if graph is None or not isinstance(getattr(graph, "deployment_id", None), str):
        return None
    try:
        peers = getattr(route, "_peers", None)
    except Exception:
        return None
    if not isinstance(peers, Mapping):
        return None

    from mycelium_a7_node_wiring import validate_against_graph, validate_node_config

    placement_stage: dict[str, str] = {}
    for stage in getattr(graph, "stages", ()):
        stage_id = getattr(stage, "stage_id", None)
        if not isinstance(stage_id, str):
            continue
        for placement in getattr(stage, "placements", ()):
            placement_id = getattr(placement, "placement_id", None)
            if isinstance(placement_id, str):
                placement_stage[placement_id] = stage_id

    attested = _a7_attested_node_configs(route, peers)
    if attested is None:
        return None
    entries: list[tuple[str, str, str, str, str, str]] = []
    sources_by_stage: dict[str, set[tuple[str, str, str, str]]] = {}
    standbys_by_stage: dict[str, set[tuple[str, str]]] = {}
    for node_id, document in sorted(attested.items()):
        try:
            config = validate_node_config(
                document,
                deployment_id=graph.deployment_id,
                node_id=node_id,
            )
            validate_against_graph(config, graph, node_id=node_id)
        except Exception:
            return None
        source = config["source"]
        if source is not None:
            stage_id = source["stage_id"]
            sources_by_stage.setdefault(stage_id, set()).add(
                (
                    node_id,
                    source["source_placement_id"],
                    source["standby_node_id"],
                    source["standby_placement_id"],
                )
            )
        standby = config["standby"]
        if standby is not None:
            stage_id = placement_stage.get(standby["placement_id"])
            if stage_id is not None:
                standbys_by_stage.setdefault(stage_id, set()).add(
                    (node_id, standby["placement_id"])
                )
        entry = config["entry"]
        if entry is not None:
            for coverage in entry["coverage"]:
                entries.append(
                    (
                        coverage["stage_id"],
                        node_id,
                        coverage["source_node_id"],
                        coverage["standby_node_id"],
                        coverage["source_placement_id"],
                        coverage["standby_placement_id"],
                    )
                )
    if not attested:
        return None

    for (
        stage_id,
        entry_node_id,
        source_node_id,
        standby_node_id,
        source_placement_id,
        standby_placement_id,
    ) in entries:
        if (
            (source_node_id, source_placement_id, standby_node_id, standby_placement_id)
            in sources_by_stage.get(stage_id, set())
            and (standby_node_id, standby_placement_id)
            in standbys_by_stage.get(stage_id, set())
        ):
            return _A7StagedAuthority(
                stage_id=stage_id,
                entry_node_id=entry_node_id,
                source_node_id=source_node_id,
                standby_node_id=standby_node_id,
            )
    return None


class LiveObservatoryApplication:
    """Minimal read-only Observatory projection of the qualifier-owned record."""

    def __init__(self, health: Any) -> None:
        self._health = health
        self._lock = threading.Lock()
        # Survive backend restarts without replaying a generation below the
        # browser's fail-closed high-water mark.
        self._opened_at_unix_ms = int(time.time() * 1_000)
        self._generation = self._opened_at_unix_ms

    def generation_floor(self) -> int:
        """The wall-clock generation floor the durable publisher must not go below."""

        return max(self._generation, int(time.time() * 1_000))

    def projection_bundle(self) -> dict[str, Any]:
        """One deterministic live bundle for the durable gateway publisher.

        The durable publisher owns generations, so this bundle stays stable
        while the qualification is unchanged: a gateway read republishes only
        on a real change, never once per read.
        """

        qualification = self._health.current()
        observed_at = self._opened_at_unix_ms
        if qualification is not None:
            observed_at = max(observed_at, int(qualification.issued_at_unix_ms))
        return self._bundle(
            source_cursor=observed_at, observed_at_unix_ms=observed_at
        )

    def _bundle(self, *, source_cursor: int, observed_at_unix_ms: int) -> dict[str, Any]:
        qualification = self._health.current()
        observed_at = observed_at_unix_ms
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
            "snapshot": {
                "protocol": "mycelium.observatory.request_projection.v1",
                "source_cursor": source_cursor,
                "observed_at_unix_ms": observed_at,
                "qualification": projection,
                "sessions": [],
            },
            "incidents": [],
            "provisioning": {
                "protocol": "mycelium.observatory.event_adapter_status.v1",
                "route_ready": route_ready,
                "source_cursor": source_cursor,
                "buffered_sessions": 0,
                "quarantine_capacity": 256,
                "dropped_quarantine_count": 0,
            },
        }

    def _envelope(self) -> dict[str, Any]:
        with self._lock:
            self._generation += 1
            generation = self._generation
        return {
            "protocol": "mycelium.observatory_stream.v1",
            "generation": generation,
            "bundle": self._bundle(
                source_cursor=generation,
                observed_at_unix_ms=int(time.time() * 1_000),
            ),
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
        health: Any,
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
    router: LiveRouterPort | None = None
    observatory: Any | None = None

    def close_router(self) -> None:
        """Stop the router's dispatch and worker threads (idempotent).

        Call before closing the route: closing cancels owned requests through it.
        """

        if self.router is not None:
            self.router.close()


COLD_ROUTE_RESTART_ERROR_CODE = "cold_route_restart_in_progress"
COLD_ROUTE_RESTART_EXHAUSTED_CODE = "cold_route_restart_budget_exhausted"
COLD_ROUTE_RESTART_FAILED_CODE = "cold_route_restart_failed"
#: How long ``close()`` waits for a background restart to unwind before it
#: closes the replacement itself; the product's cancel-and-cleanup budget.
COLD_ROUTE_RESTART_CLOSE_JOIN_SECONDS = float(CANCEL_AND_CLEANUP_BUDGET_SECONDS)


class RouteUnavailableError(RuntimeError):
    """Bounded public error: the route owner has no route to serve.

    Its ``code`` is ``route_not_open`` before the owner opens, ``route_closed``
    after it closes, and a ``ColdRouteRestartError`` code while it restarts.
    """

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ColdRouteRestartError(RouteUnavailableError):
    """Bounded public error raised for requests that hit a cold restart seam."""


class ColdRouteRestartOwner:
    """Own one replaceable physical route and cold-restart it with fresh authority.

    The owner is both a ``LiveRoute`` proxy and a qualification source.  It never
    replays the request that discovered a dead route: that request receives the
    stable retryable error code, while the owner constructs, opens, qualifies,
    and publishes a replacement route for subsequent requests.
    """

    is_simulated = False
    acts_as_qualification_source = True

    def __init__(
        self,
        *,
        route_factory: Callable[[], LiveRoute],
        open_and_qualify: Callable[[Any, int], tuple[RouteIdentity, Any]],
        restart_budget: int = 1,
    ) -> None:
        if not callable(route_factory) or not callable(open_and_qualify):
            raise ValueError("cold_route_restart_factory_required")
        if (
            type(restart_budget) is not int
            or isinstance(restart_budget, bool)
            or restart_budget < 0
            or restart_budget > 8
        ):
            raise ValueError("cold_route_restart_budget_invalid")
        self._route_factory = route_factory
        self._open_and_qualify = open_and_qualify
        self._restart_budget = restart_budget
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._current: LiveRoute | None = None
        self._startup_failed_route: LiveRoute | None = None
        # The replacement a cold restart is opening; close() aborts it.
        self._replacing: LiveRoute | None = None
        self._qualification: Any | None = None
        self._qualification_refresh: Callable[[LiveRoute], RouteQualificationV1] | None = None
        self._qualification_refresh_allowed: Callable[[], bool] = lambda: False
        self._qualification_clock_ms: Callable[[], int] = lambda: int(time.time() * 1_000)
        self._qualification_refreshed: Callable[[LiveRoute, RouteQualificationV1], None] = lambda _route, _record: None
        self._qualification_refresh_runner: Callable[[Callable[[], RouteQualificationV1 | None]], Any] = lambda operation: operation()
        self._generation = 0
        self._restart_attempts = 0
        self._state = "new"
        self._last_error_code: str | None = None
        self._last_restart_reason: str | None = None
        self._closed = False
        self._restart_threads: list[threading.Thread] = []
        self._dropped = False
        self._request_routes: dict[str, LiveRoute] = {}
        self._settled_request_authority: dict[str, Any] = {}
        self._stop_token_ids: tuple[int, ...] | None = None
        self._m16_runtime_source: Callable[[], Mapping[str, Any] | None] | None = None
        self._recovery_guard = False
        self._m17_model_operation_set = False
        self._m17_model_operation: Mapping[str, Any] | None = None

    def attach_recovery_guard(self) -> None:
        """Mark this owner as serving under the A6 product fault control.

        Then the idle cold restart never runs on a background thread: a
        detached restart may still be opening nodes when the serve shuts down,
        which the A6 post-teardown census cannot verify. Without it (A5) the
        restart stays in the background so a failed terminal is not delayed.
        The outstanding-recovery gate applies in both modes.
        """

        with self._lock:
            self._recovery_guard = True

    def open(self) -> RouteIdentity:
        with self._lock:
            if self._closed:
                raise RuntimeError("route_closed")
            if self._current is not None or self._generation != 0:
                raise RuntimeError("route_already_open")
            self._state = "starting"
            self._last_error_code = None
        route = self._route_factory()
        try:
            identity, qualification = self._open_and_prepare(route, generation=1)
        except BaseException:
            with self._lock:
                self._startup_failed_route = route
                self._qualification = None
                self._state = "failed"
                self._last_error_code = COLD_ROUTE_RESTART_FAILED_CODE
                self._changed.notify_all()
            raise
        with self._lock:
            if self._closed:
                self._startup_failed_route = route
                raise RuntimeError("route_closed")
            self._current = route
            self._generation = 1
            self._qualification = qualification
            self._state = "ready"
            self._last_error_code = None
            self._last_restart_reason = None
            self._changed.notify_all()
        return identity

    def publish(self, qualification: Any) -> None:
        with self._lock:
            if self._dropped or self._state != "ready":
                return
            self._qualification = qualification

    def drop(self) -> None:
        with self._lock:
            self._dropped = True
            self._qualification = None
            self._changed.notify_all()

    def configure_qualification_refresh(
        self,
        *,
        refresh: Callable[[LiveRoute], RouteQualificationV1],
        refresh_allowed: Callable[[], bool],
        clock_unix_ms: Callable[[], int] | None = None,
        on_refresh: Callable[[LiveRoute, RouteQualificationV1], None] | None = None,
        run_when_idle: Callable[[Callable[[], RouteQualificationV1 | None]], None] | None = None,
    ) -> None:
        """Enable genuine idle renewal on this physical route owner."""
        with self._lock:
            self._qualification_refresh = refresh
            self._qualification_refresh_allowed = refresh_allowed
            if clock_unix_ms is not None:
                self._qualification_clock_ms = clock_unix_ms
            if on_refresh is not None:
                self._qualification_refreshed = on_refresh
            if run_when_idle is not None:
                self._qualification_refresh_runner = run_when_idle

    @staticmethod
    def _same_renewal_identity(old: RouteQualificationV1, new: RouteQualificationV1) -> bool:
        # path_manifest_digest includes the one-time challenge request_id;
        # stage_bindings include observation digests. Compare the durable
        # placement/process identity, not their per-challenge evidence bytes.
        fields = (
            "deployment_id", "deployment_epoch", "topology_version", "model_id",
            "resolved_commit", "manifest_digest", "execution_graph_digest",
        )
        stages = (
            "stage_id", "placement_id", "assignment_id", "node_id",
            "stage_signature", "load_proof_digest", "endpoint_id", "process_id",
            "process_host_id", "tensor_scope_digest", "reservation_id",
        )
        return (
            all(getattr(old, name) == getattr(new, name) for name in fields)
            and len(old.stage_bindings) == len(new.stage_bindings)
            and all(
                all(getattr(a, name) == getattr(b, name) for name in stages)
                for a, b in zip(old.stage_bindings, new.stage_bindings)
            )
        )

    def current(self) -> Any | None:
        # Consult the router outside the owner lock; admission itself reads
        # current() while holding the router's lock.
        with self._lock:
            candidate = self._qualification
            refresh = self._qualification_refresh
            allowed = self._qualification_refresh_allowed
            clock = self._qualification_clock_ms
        now = clock() if refresh is not None and isinstance(candidate, RouteQualificationV1) else None
        due = (
            now is not None and candidate is not None
            and now - candidate.issued_at_unix_ms >= LIVE_QUALIFICATION_REFRESH_AFTER_MS
        )
        if due and allowed():
            def renew_if_current() -> RouteQualificationV1 | None:
                with self._lock:
                    route = self._current
                    qualification = self._qualification
                    if (
                        self._dropped or self._state != "ready" or route is None
                        or qualification is not candidate or refresh is None
                        or not isinstance(qualification, RouteQualificationV1)
                        or not self._route_alive(route)
                    ):
                        return
                    try:
                        renewed = refresh(route)
                        if (
                            not isinstance(renewed, RouteQualificationV1)
                            or renewed.route_ready is not True
                            or renewed.issued_at_unix_ms <= qualification.issued_at_unix_ms
                            or renewed.issued_at_unix_ms > clock()
                            or not self._same_renewal_identity(qualification, renewed)
                            or not self._route_alive(route)
                        ):
                            raise ValueError("qualification_renewal_invalid")
                        self._qualification_refreshed(route, renewed)
                        self._qualification = renewed
                        return renewed
                    except Exception:
                        # Retain the true timestamp for retry and expiry.
                        return None

            # Production runner holds the admission publication lock for the
            # full challenge. Never acquire that router lock under owner lock.
            self._qualification_refresh_runner(renew_if_current)
        with self._lock:
            route = self._current
            qualification = self._qualification
            if (
                self._dropped or self._state != "ready" or route is None
                or qualification is None
            ):
                return None
            if now is not None and clock() - qualification.issued_at_unix_ms > 60 * 60_000:
                return None
        try:
            return qualification if route.is_alive() else None
        except BaseException:
            return None

    def current_for_request(self, request_id: str) -> Any | None:
        """Authority for one request already running on the current route.

        A route that lost a peer is no longer current for admission, but it
        is kept open (not cold-restarted) for the requests mapped to it, which
        A4 scopes the loss to and M16 recovery may continue on surviving
        placements. Those requests keep the qualification they were admitted
        under until they are released; nothing else is authorized by it.
        """

        with self._lock:
            if self._dropped or self._closed:
                return None
            settled = self._settled_request_authority.get(request_id)
            mapped = self._request_routes.get(request_id)
        if settled is not None and mapped is None:
            # Released: the terminal is sealed against the authority the
            # request ran under, even if its route was replaced since.
            return settled
        current = self.current()
        if current is not None:
            return current
        with self._lock:
            route = self._current
            qualification = self._qualification
            if (
                self._state != "ready"
                or route is None or qualification is None
                or self._request_routes.get(request_id) is not route
            ):
                return None
        return qualification if self._route_survives_peer_loss(route) else None

    def peek_current(self) -> Any | None:
        """Non-refreshing authority snapshot for HTTP status/projection reads."""
        with self._lock:
            route = self._current
            qualification = self._qualification
            if (
                self._dropped or self._state != "ready" or route is None
                or qualification is None
            ):
                return None
            if (
                self._qualification_refresh is not None
                and isinstance(qualification, RouteQualificationV1)
                and self._qualification_clock_ms() - qualification.issued_at_unix_ms > 60 * 60_000
            ):
                return None
        try:
            return qualification if route.is_alive() else None
        except BaseException:
            return None

    @property
    def execution_graph(self) -> Any:
        route = self._current_route_or_raise()
        return route.execution_graph  # type: ignore[attr-defined]

    @property
    def startup_challenge(self) -> Any:
        route = self._current_route_or_raise()
        return route.startup_challenge  # type: ignore[attr-defined]

    def infer(
        self,
        token_ids: Sequence[int],
        *,
        max_new_tokens: int,
        request_id: str,
        sink: Any,
        cancel_requested: Callable[[], bool] | None = None,
        selected_placement_ids: Sequence[str] | None = None,
        route_identity: Mapping[str, Any] | None = None,
        locked_path_manifest: Mapping[str, Any] | None = None,
        command_identity: Mapping[str, Any] | None = None,
        authorize_cleanup: Callable[[float], float] | None = None,
        publish_cleanup_receipt: Callable[[Mapping[str, Any]], None] | None = None,
        _stop_token_ids_override: frozenset[int] | None = None,
        speculation_preference: str | None = None,
    ) -> Any:
        route, generation = self._ready_route_or_raise()
        if not self._route_alive(route) and not self._route_survives_peer_loss(route):
            self._restart_from(route, generation, reason="route_not_alive")
            raise ColdRouteRestartError(COLD_ROUTE_RESTART_ERROR_CODE)
        # A route that lost a peer but is neither fatal nor wholly dead still
        # serves a request whose placements exclude the lost peer (an M16
        # successor). The route itself refuses a path through a dead peer with
        # ``selected_route_not_open``, which cold-restarts below.
        # Qualification challenges must reproduce the exact controller-owned
        # output, including tokens that are terminal for the product codec;
        # forward the request-local override only when one is requested so
        # routes without stop filtering keep their plain signature.
        extra: dict[str, Any] = {}
        if _stop_token_ids_override is not None:
            extra["_stop_token_ids_override"] = _stop_token_ids_override
        # A11: an explicit preference is a plain passthrough, forwarded only
        # when stated; the route validates it and the entry node admits or
        # refuses it (a refusal is an ordinary target-only request).
        if speculation_preference is not None:
            extra["speculation_preference"] = speculation_preference
        with self._lock:
            self._request_routes[request_id] = route
        try:
            return route.infer(
                token_ids,
                max_new_tokens=max_new_tokens,
                request_id=request_id,
                sink=sink,
                cancel_requested=cancel_requested,
                selected_placement_ids=selected_placement_ids,
                route_identity=route_identity,
                locked_path_manifest=locked_path_manifest,
                command_identity=command_identity,
                authorize_cleanup=authorize_cleanup,
                publish_cleanup_receipt=publish_cleanup_receipt,
                **extra,
            )
        except BaseException as exc:
            if self._recovery_owns_failure(route, request_id):
                # The route recorded a request-scoped, non-fatal incident for
                # this request: M16 recovery owns it. A cold restart here would
                # retire the incident with the route and strand the request.
                raise
            if self._route_survives_peer_loss(route) and self._route_busy_elsewhere(
                route, request_id
            ):
                # Another request (an M16 source awaiting its successor) still
                # runs on this degraded route; it is replaced once released.
                raise
            if self._restartable_failure(route, exc):
                try:
                    self._restart_from(route, generation, reason=self._error_code(exc))
                except ColdRouteRestartError as restart_exc:
                    raise restart_exc from exc
                raise ColdRouteRestartError(COLD_ROUTE_RESTART_ERROR_CODE) from exc
            raise

    def a10_target_verify(
        self,
        node_id: str,
        *,
        operation: Mapping[str, Any],
        deadline_monotonic_s: float,
    ) -> Any:
        """Forward one A10 verification operation to the ready route only."""

        with self._lock:
            route = self._current
            ready = self._state == "ready" and route is not None
        if not ready:
            raise RuntimeError("route_not_open")
        verify = getattr(route, "a10_target_verify", None)
        if not callable(verify):
            raise RuntimeError("a10_verify_route_unsupported")
        return verify(
            node_id,
            operation=operation,
            deadline_monotonic_s=deadline_monotonic_s,
        )

    def a10_batch_policy(
        self,
        node_id: str,
        *,
        policy: str,
        deadline_monotonic_s: float,
    ) -> Any:
        """Forward one A10 batch-policy switch to the ready route only.

        A replacement route starts with its nodes' configured policy; the paired
        benchmark applies the policy again before every window.
        """

        with self._lock:
            route = self._current
            ready = self._state == "ready" and route is not None
        if not ready:
            raise RuntimeError("route_not_open")
        switch = getattr(route, "a10_batch_policy", None)
        if not callable(switch):
            raise RuntimeError("a10_batch_policy_route_unsupported")
        return switch(
            node_id,
            policy=policy,
            deadline_monotonic_s=deadline_monotonic_s,
        )

    def route_for_request(self, request_id: str) -> LiveRoute | None:
        """The route that executed ``request_id`` until it is released.

        It may be a route already retired by a cold restart: its in-memory
        cleanup evidence remains the only proof for that request.
        """

        with self._lock:
            return self._request_routes.get(request_id)

    def release_request(self, request_id: str) -> None:
        with self._lock:
            route = self._request_routes.pop(request_id, None)
            self._settle_request_authority_locked(request_id, route)
        if route is not None:
            route.release_request(request_id)
        self._restart_if_idle_after_peer_loss(released_request_id=request_id)

    def release_qualification_probe(self, probe_id: str) -> None:
        """Release an A6 successor-qualification probe; never restart the route.

        The probe runs on the degraded route while the recovery that asked for
        it is still to run its successor on that same route, so releasing the
        probe must not start the idle cold restart. Only the final release of a
        real request does.
        """

        with self._lock:
            route = self._request_routes.pop(probe_id, None)
        if route is not None:
            route.release_request(probe_id)

    def _settle_request_authority_locked(
        self, request_id: str, route: LiveRoute | None
    ) -> None:
        """Remember the authority a released request ran under (bounded).

        Releasing the last request on a degraded route cold-restarts it, which
        publishes the replacement's qualification before the gateway seals the
        released request's terminal. The request was produced entirely under
        its own route's qualification; only that request may still read it.
        """

        settled = self._settled_request_authority
        settled.pop(request_id, None)
        if route is not None and route is self._current and self._qualification is not None:
            settled[request_id] = self._qualification
            while len(settled) > 256:
                settled.pop(next(iter(settled)))

    def release_request_scoped(
        self,
        request_id: str,
        *,
        request_attempt: int,
        path_id: str,
        path_attempt: int,
        path_digest: str,
        cleanup_owner_id: str,
    ) -> None:
        """Forward an exact scoped release to the route that ran the request."""

        with self._lock:
            route = self._request_routes.get(request_id) or self._current
            self._settle_request_authority_locked(
                request_id, self._request_routes.get(request_id))
        if route is None:
            return
        scoped = getattr(route, "release_request_scoped", None)
        if callable(scoped):
            scoped(
                request_id,
                request_attempt=request_attempt,
                path_id=path_id,
                path_attempt=path_attempt,
                path_digest=path_digest,
                cleanup_owner_id=cleanup_owner_id,
            )
        else:
            route.release_request(request_id)
        with self._lock:
            if self._request_routes.get(request_id) is route:
                self._request_routes.pop(request_id, None)
        self._restart_if_idle_after_peer_loss(released_request_id=request_id)

    def release_attempt_scoped(
        self,
        request_id: str,
        *,
        request_attempt: int,
        path_id: str,
        path_attempt: int,
        path_digest: str,
        cleanup_owner_id: str,
    ) -> None:
        """Retire one proven attempt's route state; the request stays bound.

        Unlike ``release_request_scoped`` the request is not released: an A6
        successor attempt of the same request is about to run on this route,
        so the route stays mapped to the request and no idle cold restart may
        replace it underneath the successor.
        """

        with self._lock:
            route = self._request_routes.get(request_id) or self._current
        if route is None:
            raise RuntimeError("route_not_open")
        scoped = getattr(route, "release_request_scoped", None)
        if not callable(scoped):
            return  # a route without per-attempt state has nothing to retire
        scoped(
            request_id,
            request_attempt=request_attempt,
            path_id=path_id,
            path_attempt=path_attempt,
            path_digest=path_digest,
            cleanup_owner_id=cleanup_owner_id,
        )

    def _recovery_outstanding(self, released_request_id: str | None) -> bool:
        """True while the M16 runtime still runs a request that may need this route.

        A request in recovery is not mapped to its route between the source
        attempt's release and its successor's start; the runtime is the
        authority for it. The successor reuses the source's request id, so the
        released id counts as outstanding until the runtime lists it terminal
        (or no longer lists it). An unreadable runtime never blocks a restart.
        """

        with self._lock:
            source = self._m16_runtime_source
        if source is None:
            return False
        try:
            document = source()
            active = document["queue"]["active_request_ids"]  # type: ignore[index]
            if any(item != released_request_id for item in active):
                return True
            if released_request_id is None:
                return False
            if released_request_id in active:
                records = document.get("requests") or ()  # type: ignore[union-attr]
                for record in records:
                    if record.get("request_id") == released_request_id:
                        return record.get("terminal_state") is None
                return True
            return False
        except BaseException:
            return False

    def _restart_if_idle_after_peer_loss(
        self, released_request_id: str | None = None
    ) -> None:
        """Cold-restart a degraded route once no request still runs on it.

        Recovery keeps a route that lost a peer open for the successor of the
        request that observed the loss. Once that request is released the
        route is replaced, so it does not stay unqualified indefinitely. The
        restart never starts while the recovery runtime still has another
        request in flight: that request's successor needs this route.
        """

        with self._lock:
            route = self._current
            generation = self._generation
            if (
                self._closed
                or self._state != "ready"
                or route is None
                or any(item is route for item in self._request_routes.values())
            ):
                return
        if self._route_alive(route):
            return
        outstanding = self._recovery_outstanding(released_request_id)
        with self._lock:
            inline = self._recovery_guard
        _LOGGER.info(
            "cold restart decision released_request_id=%s recovery_outstanding=%s "
            "inline=%s generation=%s thread=%s",
            released_request_id, outstanding, inline, generation,
            threading.current_thread().name,
        )
        if outstanding:
            return
        if inline:
            # A6 fault control mounted: restart on the releasing thread, only
            # now that no recovery needs the degraded route.
            try:
                self._restart_from(route, generation, reason="route_not_alive")
            except ColdRouteRestartError:
                pass
            return

        def restart() -> None:
            try:
                self._restart_from(route, generation, reason="route_not_alive")
            except ColdRouteRestartError:
                # State and status already name the outcome; the released
                # request has its terminal and must not be failed by the
                # replacement.
                pass

        # The gateway releases the backend before it publishes the request's
        # terminal, so reopening and requalifying the route here would hold the
        # failed terminal back for the whole restart (minutes for a real
        # model). Until the replacement is ready the route is not current and
        # admission is refused honestly.
        thread = threading.Thread(
            target=restart, name="cold-route-restart", daemon=True
        )
        with self._lock:
            if self._closed:
                return
            self._restart_threads = [
                t for t in self._restart_threads if t.is_alive()
            ] + [thread]
        thread.start()

    def counters(self) -> RouteCounters:
        with self._lock:
            route = self._current
            state = self._state
            error = self._last_error_code
        if route is not None and state == "ready":
            return route.counters()
        return RouteCounters(
            frames_sent=0,
            frames_received=0,
            applied_operation_count=0,
            fatal=error,
        )

    def public_status(self) -> Mapping[str, Any]:
        with self._lock:
            route = self._current
            state = self._state
            generation = self._generation
            attempts = self._restart_attempts
            budget = self._restart_budget
            error = self._last_error_code
            reason = self._last_restart_reason
            qualification = self._qualification
        route_alive = False
        document: dict[str, Any]
        if route is not None and state == "ready":
            try:
                document = dict(route.public_status())
            except BaseException:
                document = {"protocol": "mycelium.live_route_status.v1"}
            route_alive = self._route_alive(route)
        else:
            document = {"protocol": "mycelium.live_route_status.v1"}
        route_ready = bool(state == "ready" and route_alive and self.peek_current() is not None)
        document["route_alive"] = route_alive
        document["route_ready"] = route_ready
        document["route_restarting"] = state == "restarting"
        document["restart_failed"] = state == "failed"
        document["cold_route_restart"] = {
            "protocol": "mycelium.cold_route_restart_status.v1",
            "state": state,
            "generation": generation,
            "attempts": attempts,
            "budget": budget,
            "budget_exhausted": state == "failed" and error == COLD_ROUTE_RESTART_EXHAUSTED_CODE,
            "last_error_code": error,
            "last_restart_reason": reason,
        }
        return document

    def is_alive(self) -> bool:
        with self._lock:
            return not self._closed and self._state != "new"

    def close(self) -> None:
        routes: list[LiveRoute] = []
        with self._lock:
            self._closed = True
            self._state = "closed"
            if self._current is not None:
                routes.append(self._current)
                self._current = None
            if self._startup_failed_route is not None:
                routes.append(self._startup_failed_route)
            self._qualification = None
            replacing = self._replacing
            self._changed.notify_all()
        if replacing is not None:
            # A cold restart is opening a replacement in a request thread. Do
            # not wait out its open and qualification: abort it without the
            # route's lock (its open holds it), so the opener fails at once and
            # the restart retires the replacement (see _restart_from).
            abort = getattr(replacing, "abort_open", None)
            if callable(abort):
                try:
                    abort()
                except BaseException:  # pragma: no cover - never mask shutdown
                    pass
        for route in self._unique_routes(routes):
            route.close()
        with self._lock:
            restart_threads = tuple(self._restart_threads)
        deadline = time.monotonic() + COLD_ROUTE_RESTART_CLOSE_JOIN_SECONDS
        stuck = False
        for thread in restart_threads:
            if thread is not threading.current_thread():
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
                stuck = stuck or thread.is_alive()
        if stuck:
            # The restart did not unwind within the budget (its open ignores
            # the abort): close the replacement here so no node it started
            # outlives this call. The restart thread retires it again when it
            # finally unwinds; closing a route twice is harmless.
            with self._lock:
                leftover = self._replacing
            if leftover is not None:
                abort = getattr(leftover, "abort_open", None)
                if callable(abort):
                    try:
                        abort()  # re-snapshot sessions started since the first abort
                    except BaseException:  # pragma: no cover
                        pass
                try:
                    self._retire_route(leftover, cleanup=False)
                except BaseException:  # pragma: no cover - never mask shutdown
                    pass
            # Return only once the restart thread unwound (its replacement is
            # then retired and its node processes reaped), bounded.
            reap_deadline = time.monotonic() + COLD_ROUTE_RESTART_CLOSE_JOIN_SECONDS
            for thread in restart_threads:
                if thread is not threading.current_thread():
                    thread.join(timeout=max(0.0, reap_deadline - time.monotonic()))

    def cleanup(self) -> None:
        with self._lock:
            route = self._startup_failed_route
            self._startup_failed_route = None
        if route is not None:
            route.cleanup()

    def set_stop_token_ids(self, token_ids: Sequence[int]) -> None:
        cached = tuple(token_ids)
        with self._lock:
            self._stop_token_ids = cached
            route = self._current
        if route is not None:
            configure = getattr(route, "set_stop_token_ids", None)
            if callable(configure):
                configure(cached)

    def set_m16_runtime_source(
        self, source: Callable[[], Mapping[str, Any] | None]
    ) -> None:
        if not callable(source):
            raise ValueError("m16_runtime_source_invalid")
        with self._lock:
            self._m16_runtime_source = source
            route = self._current
        if route is not None:
            configure = getattr(route, "set_m16_runtime_source", None)
            if callable(configure):
                configure(source)

    def set_m17_model_operation(self, document: Mapping[str, Any] | None) -> None:
        with self._lock:
            self._m17_model_operation_set = True
            self._m17_model_operation = None if document is None else dict(document)
            route = self._current
        if route is not None:
            configure = getattr(route, "set_m17_model_operation", None)
            if callable(configure):
                configure(document)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        route = self._current_route_or_raise()
        return getattr(route, name)

    def _open_and_prepare(
        self,
        route: LiveRoute,
        *,
        generation: int,
    ) -> tuple[RouteIdentity, Any]:
        identity, qualification = self._open_and_qualify(route, generation)
        with self._lock:
            closed = self._closed
        if closed:
            raise RouteUnavailableError("route_closed")
        self._apply_cached_configuration(route)
        return identity, qualification

    def _apply_cached_configuration(self, route: LiveRoute) -> None:
        with self._lock:
            stop_token_ids = self._stop_token_ids
            runtime_source = self._m16_runtime_source
            model_operation_set = self._m17_model_operation_set
            model_operation = self._m17_model_operation
        if stop_token_ids is not None:
            configure_stop = getattr(route, "set_stop_token_ids", None)
            if callable(configure_stop):
                configure_stop(stop_token_ids)
        if runtime_source is not None:
            configure_runtime = getattr(route, "set_m16_runtime_source", None)
            if callable(configure_runtime):
                configure_runtime(runtime_source)
        if model_operation_set:
            configure_model = getattr(route, "set_m17_model_operation", None)
            if callable(configure_model):
                configure_model(model_operation)
        self._install_a7_route_authority_if_staged(route)

    def _install_a7_route_authority_if_staged(self, route: LiveRoute) -> None:
        target = _a7_route_authority_target(route)
        if target is None:
            return
        staged = _a7_staged_authority(target)
        if staged is None:
            return
        install_route_authority(
            target,
            node_id_for_command=staged.entry_node_id,
            accept_source_lost=_a7_entry_command_only,
            cut_over_lost_source=_a7_entry_command_only,
        )
        _LOGGER.info(
            "a7 authority installed on route id=%s stage=%s standby_node=%s entry_node=%s source_node=%s",
            _a7_route_id(target),
            _a7_stage_log_value(staged.stage_id),
            staged.standby_node_id,
            staged.entry_node_id,
            staged.source_node_id,
        )

    def _ready_route_or_raise(self) -> tuple[LiveRoute, int]:
        with self._lock:
            if self._state == "restarting":
                raise ColdRouteRestartError(COLD_ROUTE_RESTART_ERROR_CODE)
            if self._state == "failed":
                raise ColdRouteRestartError(
                    self._last_error_code or COLD_ROUTE_RESTART_FAILED_CODE
                )
            if self._closed:
                raise RouteUnavailableError("route_closed")
            if self._state != "ready" or self._current is None:
                raise RouteUnavailableError("route_not_open")
            return self._current, self._generation

    def _current_route_or_raise(self) -> LiveRoute:
        route, _generation = self._ready_route_or_raise()
        return route

    def _restart_from(self, route: LiveRoute, generation: int, *, reason: str) -> None:
        with self._lock:
            if self._state == "restarting":
                raise ColdRouteRestartError(COLD_ROUTE_RESTART_ERROR_CODE)
            if self._state == "failed":
                raise ColdRouteRestartError(
                    self._last_error_code or COLD_ROUTE_RESTART_FAILED_CODE
                )
            if self._current is not route or self._generation != generation:
                raise ColdRouteRestartError(COLD_ROUTE_RESTART_ERROR_CODE)
            if self._restart_attempts >= self._restart_budget:
                self._state = "failed"
                self._last_error_code = COLD_ROUTE_RESTART_EXHAUSTED_CODE
                self._last_restart_reason = reason[:128]
                self._qualification = None
                self._current = None
                self._changed.notify_all()
                retire_route = route
                exhausted = True
                next_generation = generation
            else:
                self._state = "restarting"
                self._last_error_code = COLD_ROUTE_RESTART_ERROR_CODE
                self._last_restart_reason = reason[:128]
                self._qualification = None
                self._current = None
                self._restart_attempts += 1
                self._changed.notify_all()
                retire_route = route
                exhausted = False
                next_generation = generation + 1
        try:
            # A replacement reopens the same digest-bound staging; only a route
            # that will not be replaced may delete it.
            self._retire_route(retire_route, cleanup=exhausted)
        except BaseException as exc:
            self._mark_restart_failed(str(getattr(exc, "code", exc)) or type(exc).__name__)
            raise ColdRouteRestartError(COLD_ROUTE_RESTART_FAILED_CODE) from exc
        if exhausted:
            raise ColdRouteRestartError(COLD_ROUTE_RESTART_EXHAUSTED_CODE)
        replacement: LiveRoute | None = None
        try:
            with self._lock:
                closed_before_factory = self._closed
            if closed_before_factory:
                # Shut down while the old route retired: build nothing.
                raise ColdRouteRestartError(COLD_ROUTE_RESTART_FAILED_CODE)
            replacement = self._route_factory()
            with self._lock:
                if self._closed:
                    raise ColdRouteRestartError(COLD_ROUTE_RESTART_FAILED_CODE)
                self._replacing = replacement
            identity, qualification = self._open_and_prepare(
                replacement,
                generation=next_generation,
            )
            _ = identity  # Identity is validated by the opener; retained via route state.
        except BaseException as exc:
            with self._lock:
                self._replacing = None
                closed = self._closed
            if replacement is not None:
                try:
                    # Closed by the serve: close it as an ordinary shutdown
                    # does (the staging stays); otherwise retire it fully.
                    self._retire_route(replacement, cleanup=not closed)
                except BaseException:
                    pass
            self._mark_restart_failed(self._error_code(exc))
            raise ColdRouteRestartError(COLD_ROUTE_RESTART_FAILED_CODE) from exc
        with self._lock:
            self._replacing = None
            if self._closed:
                retire_closed = True
            else:
                retire_closed = False
                self._current = replacement
                self._generation = next_generation
                self._qualification = qualification
                self._state = "ready"
                self._last_error_code = None
                self._last_restart_reason = None
                self._changed.notify_all()
        if retire_closed:
            # The owner was closed while this replacement opened. close() has
            # already run, so nothing else would ever close it: its node
            # processes (and their non-daemon reader threads) would keep the
            # serve process alive after shutdown.
            try:
                self._retire_route(replacement, cleanup=False)
            except BaseException:
                pass
            raise ColdRouteRestartError(COLD_ROUTE_RESTART_FAILED_CODE)

    def _mark_restart_failed(self, reason: str) -> None:
        with self._lock:
            if self._closed:
                # Shutdown is not a restart failure; keep the closed state.
                return
            self._state = "failed"
            self._last_error_code = COLD_ROUTE_RESTART_FAILED_CODE
            self._last_restart_reason = reason[:128]
            self._qualification = None
            self._current = None
            self._changed.notify_all()

    @staticmethod
    def _retire_route(route: LiveRoute, *, cleanup: bool = True) -> None:
        close_error: BaseException | None = None
        try:
            route.close()
        except BaseException as exc:  # pragma: no cover - defensive masking guard
            close_error = exc
        if cleanup:
            try:
                route.cleanup()
            except BaseException as exc:
                if close_error is None:
                    close_error = exc
        if close_error is not None:
            raise RuntimeError("cold_route_restart_retire_failed") from close_error

    @staticmethod
    def _route_alive(route: LiveRoute) -> bool:
        try:
            return bool(route.is_alive())
        except BaseException:
            return False

    @staticmethod
    def _route_survives_peer_loss(route: LiveRoute) -> bool:
        """True when a route lost peers yet is neither fatal nor wholly dead."""

        try:
            if route.counters().fatal is not None:
                return False
            status = route.a4_liveness_status()  # type: ignore[attr-defined]
        except BaseException:
            return False
        if not isinstance(status, Mapping) or status.get("deployment_fatal_reason") is not None:
            return False
        subjects = status.get("subjects")
        if not isinstance(subjects, (list, tuple)):
            return False
        return any(
            isinstance(subject, Mapping)
            and subject.get("state") not in {"failed", "quarantined"}
            for subject in subjects
        )

    def _route_busy_elsewhere(self, route: LiveRoute, request_id: str) -> bool:
        with self._lock:
            return any(
                other is route and other_id != request_id
                for other_id, other in self._request_routes.items()
            )

    def _recovery_owns_failure(self, route: LiveRoute, request_id: str) -> bool:
        """A request-scoped, non-fatal incident on a surviving route is M16's."""

        if not self._route_survives_peer_loss(route):
            return False
        try:
            incidents = route.a4_scoped_runtime_incidents()  # type: ignore[attr-defined]
        except BaseException:
            return False
        if not isinstance(incidents, (list, tuple)):
            return False
        return any(
            isinstance(incident, Mapping)
            and incident.get("request_id") == request_id
            and incident.get("fatal_accepted") is not True
            for incident in incidents
        )

    def _restartable_failure(self, route: LiveRoute, error: BaseException) -> bool:
        code = self._error_code(error)
        return (not self._route_alive(route)) or code in {
            "route_not_open",
            "selected_route_not_open",
        }

    @staticmethod
    def _error_code(error: BaseException) -> str:
        code = getattr(error, "remote_code", None) or getattr(error, "code", None)
        if not isinstance(code, str) or not code:
            code = str(error).partition(":")[0]
        if (
            not code
            or len(code) > 64
            or not code[0].islower()
            or any(
                character not in "abcdefghijklmnopqrstuvwxyz0123456789_"
                for character in code
            )
        ):
            return "runtime_error"
        return code

    @staticmethod
    def _unique_routes(routes: Sequence[LiveRoute]) -> tuple[LiveRoute, ...]:
        seen: set[int] = set()
        unique: list[LiveRoute] = []
        for route in routes:
            marker = id(route)
            if marker not in seen:
                seen.add(marker)
                unique.append(route)
        return tuple(unique)


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


# --------------------------------------------------------------------------- #
# Live product servers: the real Observatory gateway and the OpenAI shim
# --------------------------------------------------------------------------- #
LIVE_OPENAI_SHIM_DEFAULT_MODEL_ID = "mycelium-live-route"
LIVE_OPENAI_SHIM_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
OBSERVATORY_STATE_DIRNAME = "observatory"
OBSERVATORY_STATE_FILENAME = "coherent-snapshot.json"


def _observatory_state_path(product_state_root: Path | None) -> Path:
    """Durable Observatory state lives under the operator's own product state root."""

    if product_state_root is None:
        # No private product state root was configured: keep the state in a
        # per-process private directory instead of a shared, guessable path.
        base = Path(tempfile.mkdtemp(prefix="mycelium-observatory-"))
    else:
        base = Path(product_state_root)
    return base / OBSERVATORY_STATE_DIRNAME / OBSERVATORY_STATE_FILENAME


def _load_live_observatory_gateway(*, health: Any, state_path: Path) -> Any:
    """Build the real Observatory gateway, or fail loudly with the import error.

    The patchable seam is deliberate: ``tests/live/test_live_servers_reachability.py``
    proves the live composition is RED (fails closed) when the gateway module
    cannot be imported from it.
    """

    from mycelium_gateway.live_projection import (
        build_live_projection_gateway,
        loopback_upstream_read_policy,
    )

    projection = LiveObservatoryApplication(health)
    return build_live_projection_gateway(
        state_path=state_path,
        projection_source=projection.projection_bundle,
        generation_floor=projection.generation_floor,
        read_policy=loopback_upstream_read_policy(),
    )


def _build_live_observatory_gateway(
    *, health: Any, product_state_root: Path | None
) -> Any:
    return _load_live_observatory_gateway(
        health=health, state_path=_observatory_state_path(product_state_root)
    )


def _load_live_openai_shim_serve_in_thread() -> Any:
    """Return ``mycelium_openai_shim.server.serve_in_thread`` or raise."""

    from mycelium_openai_shim.server import serve_in_thread

    return serve_in_thread


class LiveOpenAIShim:
    """The OpenAI-compatible shim co-listener launched beside the live server."""

    def __init__(
        self, *, server: Any, thread: threading.Thread, model_id: str
    ) -> None:
        self.server = server
        self.thread = thread
        self.model_id = model_id

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def status(self) -> Mapping[str, Any]:
        return {
            "base_url": self.base_url,
            "model_id": self.model_id,
            "endpoints": (
                f"GET {self.base_url}/v1/models",
                f"POST {self.base_url}/v1/chat/completions",
            ),
        }

    def close(self) -> None:
        try:
            self.server.shutdown()
        except BaseException:  # pragma: no cover - shutdown is best effort
            pass
        stop = getattr(self.server, "stop", None)
        if callable(stop):
            stop(self.thread)
            return
        self.server.server_close()
        if self.thread.is_alive():
            self.thread.join(timeout=5.0)


def start_live_openai_shim(
    *,
    route: LiveRoute,
    host: str = "127.0.0.1",
    port: int = 0,
    model_id: str | None = None,
) -> LiveOpenAIShim:
    """Launch the OpenAI-compatible shim on loopback beside the live route.

    The shim is a real HTTP listener driving ``route.infer``; an unimportable
    ``mycelium_openai_shim`` fails live bring-up loudly instead of silently
    serving without the OpenAI-compatible surface.
    """

    if host not in LIVE_OPENAI_SHIM_HOSTS:
        raise ValueError("live_openai_shim_requires_loopback")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65_535:
        raise ValueError("live_openai_shim_port_invalid")
    serve_in_thread = _load_live_openai_shim_serve_in_thread()
    from mycelium_openai_shim.translator import default_detokenizer, default_tokenizer

    resolved_model_id = str(
        model_id or getattr(route, "model_id", None) or LIVE_OPENAI_SHIM_DEFAULT_MODEL_ID
    )
    server, thread = serve_in_thread(
        live_route=route,
        host=host,
        port=port,
        tokenizer=default_tokenizer(),
        detokenizer=default_detokenizer(),
        model_id=resolved_model_id,
    )
    return LiveOpenAIShim(server=server, thread=thread, model_id=resolved_model_id)


def _follow_replica_reader(route: Any, name: str) -> Callable[[], Any] | None:
    """A replica reader that reads the route that serves now, or ``None``.

    A cold-restart owner swaps in a new route and closes the old one, so a
    method bound at build time would keep reading the retired route's replica
    qualifications and losses. Look the reader up again on every call. While
    the owner restarts (or after its restart failed, or once it closed) there
    is no route to read: refuse the request with the owner's bounded code,
    which the request gateway reports as the failed terminal's code.
    """

    if not callable(getattr(route, name, None)):
        return None

    def read() -> Any:
        try:
            reader = getattr(route, name)
        except RouteUnavailableError as exc:
            raise AdmissionError(_route_unavailable_code(exc)) from None
        return reader()

    return read


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
    recovery_qualification_source: (
        Callable[
            [str, ReplayCheckpoint, Mapping[str, Any], PathManifest],
            tuple[Mapping[str, Any], Mapping[str, Any]],
        ]
        | None
    ) = None,
    recovery_tokenizer_digest: str | None = None,
    recovery_a4_atomic_commit: str | None = None,
    coarse_region_declaration_file: Path | None = None,
    persist_transport_history: bool = False,
) -> LiveStack:
    """Compose the production browser and request gateways around a live route."""
    transport_binding = _transport_history_binding(
        execution_graph,
        product_state_root=product_state_root,
        persist=persist_transport_history,
    )
    if (recovery_qualification_source is None) != (
        recovery_tokenizer_digest is None
    ):
        raise ValueError("a6_recovery_configuration_incomplete")
    if recovery_qualification_source is not None and product_state_root is None:
        raise ValueError("a6_recovery_checkpoint_root_required")
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
    # The qualifier-owned current-record source is created first: the router
    # port's idle recovery projection (polled by the browser before the first
    # admission) must read the same authority the request gateway captures
    # from, so the two never disagree about the current qualification.
    health = (
        route
        if getattr(route, "acts_as_qualification_source", False) is True
        else RouteHealthSource(
            route=route,
            refresh=lambda: _qualify_open_route(route),
            refresh_allowed=lambda: router.is_idle(),
        )
    )
    router = LiveRouterPort(
        route=route,
        execution_graph=execution_graph,
        runtime_coordinator=coordinator,
        recovery_authority=(
            None
            if recovery_qualification_source is None
            else M16RecoveryAuthority(coordinator)
        ),
        recovery_qualification_source=recovery_qualification_source,
        recovery_tokenizer_digest=recovery_tokenizer_digest,
        recovery_a4_atomic_commit=recovery_a4_atomic_commit,
        qualification_source=health,
    )
    configure_runtime_source = getattr(route, "set_m16_runtime_source", None)
    if callable(configure_runtime_source):
        configure_runtime_source(router.runtime_status)

    def product_app_factory(recovery_source: Callable[[], Mapping[str, Any]]) -> Any | None:
        return _optional_product_application(
            route,
            health,
            recovery_source=recovery_source,
            state_root=product_state_root,
            coarse_region_declaration_file=coarse_region_declaration_file,
            transport_binding=transport_binding,
        )

    checkpoint_root = _prepare_product_checkpoint_root(product_state_root)
    observatory = _build_live_observatory_gateway(
        health=health, product_state_root=product_state_root
    )
    app = build_loopback_product_stack(
        qualification_source=health,
        router=router,
        codec=codec,
        observatory_app=observatory.app,
        product_app_factory=product_app_factory,
        swarm_coordinator=LiveSwarmCoordinator(route, health, seed_url=seed_url),
        request_bearer_token=bearer_token,
        replica_qualifications_factory=_follow_replica_reader(
            route, "replica_track_qualifications"
        ),
        replica_loss_placement_ids_factory=_follow_replica_reader(
            route, "replica_loss_placement_ids"
        ),
        public_origin=public_origin,
        trusted_https_proxy=trusted_https_proxy,
        trusted_proxy_capability=trusted_proxy_capability,
        replay_checkpoint_root=checkpoint_root,
    )
    return LiveStack(
        app=app,
        health=health,
        route=route,
        router=router,
        observatory=observatory,
    )


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

    checkpoint_root = _prepare_product_checkpoint_root(product_state_root)
    observatory = _build_live_observatory_gateway(
        health=registry, product_state_root=product_state_root
    )
    app = build_loopback_product_stack(
        qualification_source=registry,
        router=registry,
        codec=registry,
        observatory_app=observatory.app,
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
        replay_checkpoint_root=checkpoint_root,
    )
    return LiveStack(
        app=app, health=registry, route=registry, observatory=observatory
    )


def _prepare_product_checkpoint_root(state_root: Path | None) -> Path | None:
    """Prepare fresh managed directories before the checkpoint gateway.

    Existing operator directories are validated, never repaired or chmodded.
    """
    if state_root is None:
        return None
    root = Path(state_root)
    checkpoint = root / "private" / "a6-replay-checkpoints"
    for path in (root, root / "private", checkpoint):
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise ProductEvidenceStateError("product_evidence_state_root_invalid") from exc
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise ProductEvidenceStateError("product_evidence_state_root_invalid") from exc
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise ProductEvidenceStateError("product_evidence_state_root_invalid")
    return checkpoint


def _transport_history_binding(
    execution_graph: Any,
    *,
    product_state_root: Path | None,
    persist: bool,
) -> str | None:
    """Bind the persisted direct/relay ledger to the served deployment (opt-in)."""

    if not persist:
        return None
    if product_state_root is None:
        raise ValueError("a14_transport_history_state_root_required")
    deployment_id = getattr(execution_graph, "deployment_id", None)
    if not isinstance(deployment_id, str) or not deployment_id:
        raise ValueError("a14_transport_history_deployment_unavailable")
    return deployment_id


def _a9_overlaid(reader: Callable[[], Any]) -> Callable[[], Any]:
    """Identity unless the grant-gated A9 publisher wrote to the passive ledger."""
    from mycelium_a9_authority_ledger import overlay_membership

    def read() -> Any:
        return overlay_membership(reader())

    return read


def _product_application(
    route: Any,
    qualification_source: Any,
    *,
    recovery_source: Callable[[], Mapping[str, Any]] | None = None,
    state_root: Path | None = None,
    coarse_region_declaration_file: Path | None = None,
    transport_binding: str | None = None,
) -> Any:
    membership = getattr(route, "product_membership_records", None)
    salt = getattr(route, "product_pseudonym_salt", None)
    public_status = getattr(route, "public_status", None)
    assignments = getattr(route, "product_assignment_records", None)
    internet_native = getattr(route, "product_internet_native_snapshot", None)
    recovery_projection = getattr(route, "recovery_public_projection", None)
    if not all(callable(value) for value in (membership, salt, public_status)):
        raise ValueError("product_evidence_source_unavailable")

    def _follow(name: str) -> Callable[[], Any]:
        # A cold-restart owner swaps in a new route and closes the old one, so a
        # method bound at build time would keep reading the closed route. Look the
        # reader up again on every call to follow the route that currently serves.
        # While the owner restarts (or after its restart failed) there is no route
        # to read: name that bounded state for this source instead of failing it.
        def read() -> Any:
            try:
                reader = getattr(route, name)
            except ColdRouteRestartError as exc:
                raise ProductSourceUnavailable(exc.code) from None
            return reader()

        return read

    def qualification_document() -> Mapping[str, Any] | None:
        record = qualification_source.current()
        return None if record is None else route_qualification_to_dict(record)

    def internet_native_document() -> Mapping[str, Any]:
        if not callable(internet_native):
            raise ValueError("product_internet_native_source_unavailable")
        document = _follow("product_internet_native_snapshot")()
        if not isinstance(document, Mapping):
            raise ValueError("product_internet_native_source_invalid")
        return document

    projector = ProductProjector(pseudonym_salt=salt())
    coarse_region_source = None
    if coarse_region_declaration_file is not None:
        from mycelium_product_spine.region_declarations import (
            OperatorRegionEvidenceSource,
        )

        coarse_region_source = OperatorRegionEvidenceSource(
            coarse_region_declaration_file,
            member_entity_id=projector._pseudonym,
        )
    return ProductEvidenceApplication(
        projector=projector,
        coarse_region_source=coarse_region_source,
        membership_source=_a9_overlaid(_follow("product_membership_records")),
        assignment_source=(
            _follow("product_assignment_records") if callable(assignments) else None
        ),
        route_source=_follow("public_status"),
        qualification_source=qualification_document,
        internet_native_source=(
            internet_native_document if callable(internet_native) else None
        ),
        recovery_source=(
            recovery_source
            if recovery_source is not None
            else _follow("recovery_public_projection")
            if callable(recovery_projection)
            else None
        ),
        state_root=state_root,
        transport_binding=transport_binding,
        a7_source=_a7_product_source(route),
    )


def _a7_product_source(route: Any) -> Callable[[], Mapping[str, Any] | None]:
    """The A7 workspace evidence reader; it answers ``None`` while A7 is not staged."""

    from mycelium_a7_evidence_control import RouteA7Evidence

    return RouteA7Evidence(route).product


def _optional_product_application(
    route: Any,
    qualification_source: Any,
    *,
    recovery_source: Callable[[], Mapping[str, Any]] | None = None,
    state_root: Path | None = None,
    coarse_region_declaration_file: Path | None = None,
    transport_binding: str | None = None,
) -> Any | None:
    required = (
        getattr(route, "product_membership_records", None),
        getattr(route, "product_pseudonym_salt", None),
        getattr(route, "public_status", None),
    )
    if not all(callable(value) for value in required):
        return None
    return _product_application(
        route, qualification_source, recovery_source=recovery_source,
        state_root=state_root,
        coarse_region_declaration_file=coarse_region_declaration_file,
        transport_binding=transport_binding,
    )


class LiveHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    app: Any
    route: LiveRoute
    static_root: Path
    # Explicit API-only serving: static routes fail closed with a safe JSON
    # error instead of serving UI files.  Default (False) keeps UI mode.
    api_only: bool
    activation: PreparedDeploymentActivation | None
    capacity_refresh: ModelCapacityRefresh | None
    model_preparation: LocalModelPreparation | None
    governance_projection: Mapping[str, Any] | None
    evidence_registry: EvidenceProjectionRegistry | None
    artifact_acquisition_store: ArtifactAcquisitionStore | None
    trusted_https_proxy: bool


def _route_unavailable_code(exc: RouteUnavailableError) -> str:
    """The bounded public code for a request that found no route to read.

    While the cold-restart owner restarts (or after its restart failed), and
    before it opens or after it closes, it has no route, and every reader
    through it raises ``RouteUnavailableError``. The code is validated the way
    the product snapshot validates its reason codes
    (``ProductSourceUnavailable``).
    """

    try:
        return ProductSourceUnavailable(exc.code).reason_code
    except ValueError:
        return (
            COLD_ROUTE_RESTART_FAILED_CODE
            if isinstance(exc, ColdRouteRestartError)
            else "route_not_open"
        )


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
            if getattr(self.server, "api_only", False):
                self._send_bytes(
                    404,
                    _json_bytes({"error": "static_content_disabled"}),
                    "application/json; charset=utf-8",
                )
                return
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

        def _route_unavailable(self, status: int, exc: RouteUnavailableError) -> None:
            # A route reader looked up through an owner with no route (it is
            # restarting, not open yet, or closed) raises; a getattr default
            # does not catch it. Answer the owner's bounded code instead of
            # letting the handler thread drop the connection.
            self._send_bytes(
                status,
                _json_bytes({"error": _route_unavailable_code(exc)}),
                "application/json; charset=utf-8",
            )

        def _deployment_registry(self) -> None:
            try:
                registry_status = getattr(self.server.route, "registry_status", None)
                if not callable(registry_status):
                    self._send_bytes(
                        404,
                        _json_bytes({"error": "deployment_registry_unavailable"}),
                        "application/json; charset=utf-8",
                    )
                    return
                document = registry_status()
            except RouteUnavailableError as exc:
                self._route_unavailable(503, exc)
                return
            self._send_bytes(
                200,
                _json_bytes(document),
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
            try:
                source = getattr(self.server.route, "m15_plan_comparison", None)
                document = source() if callable(source) else None
            except RouteUnavailableError as exc:
                self._route_unavailable(503, exc)
                return
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
            try:
                source = getattr(self.server.route, "m16_runtime_status", None)
                document = source() if callable(source) else None
            except RouteUnavailableError as exc:
                self._route_unavailable(503, exc)
                return
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
            try:
                source = getattr(self.server.route, "m17_model_operation", None)
                document = source() if callable(source) else None
            except RouteUnavailableError as exc:
                self._route_unavailable(503, exc)
                return
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
            try:
                source = getattr(self.server.route, "m17_swarm_evidence", None)
            except RouteUnavailableError as exc:
                self._route_unavailable(503, exc)
                return
            if not callable(source):
                self._send_bytes(
                    404,
                    _json_bytes({"error": "swarm_evidence_unavailable"}),
                    "application/json; charset=utf-8",
                )
                return
            try:
                document = source()
            except RouteUnavailableError as exc:
                self._route_unavailable(503, exc)
                return
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
            try:
                select = getattr(self.server.route, "select", None)
            except RouteUnavailableError as exc:
                self._route_unavailable(409, exc)
                return
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
            except RouteUnavailableError as exc:
                self._route_unavailable(409, exc)
                return
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

        def _install_replica_qualification(self) -> None:
            """Install (or clear) the live replica-qualification set at runtime.

            A5-owned operator surface, no restart, no request-path contract
            change. ``{"documents": []}`` clears the set — rotation then
            degenerates to the incumbent A4 default path (baseline mode).
            ``{"documents": [<validated replica_qualification.v1>...]}``
            restores candidate tracks. Every document is re-validated against
            the closed A5 contract before the route method installs it; the
            route method validates again fail-closed.
            """

            try:
                install = getattr(
                    self.server.route, "set_replica_track_qualification", None
                )
            except RouteUnavailableError as exc:
                self._route_unavailable(409, exc)
                return
            if not callable(install):
                self._send_bytes(
                    404,
                    _json_bytes({"error": "replica_qualification_unavailable"}),
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
            operator_token = self.server.replica_operator_token
            authorization = self.headers.get("authorization", "")
            if (
                not isinstance(operator_token, str)
                or len(operator_token) < 32
                or not hmac.compare_digest(
                    authorization,
                    f"Bearer {operator_token}",
                )
            ):
                self._send_bytes(
                    403,
                    _json_bytes({"error": "operator_authorization_required"}),
                    "application/json; charset=utf-8",
                )
                return
            body = self._read_body(limit=64 * 1024)
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
                from mycelium_replica_contracts import (
                    validate_replica_qualification,
                )

                document = json.loads(body)
                if set(document) != {"documents"} or not isinstance(
                    document["documents"], list
                ):
                    raise ValueError
                validated = [
                    validate_replica_qualification(item)
                    for item in document["documents"]
                ]
                install(validated)
            except RouteUnavailableError as exc:
                self._route_unavailable(409, exc)
                return
            except (TypeError, ValueError, json.JSONDecodeError):
                self._send_bytes(
                    400,
                    _json_bytes({"error": "invalid_replica_qualification"}),
                    "application/json; charset=utf-8",
                )
                return
            self._send_bytes(
                200,
                _json_bytes({"installed": len(validated)}),
                "application/json; charset=utf-8",
            )

        def _promote_candidate(self) -> None:
            try:
                promote = getattr(self.server.route, "promote_candidate", None)
            except RouteUnavailableError as exc:
                self._route_unavailable(409, exc)
                return
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
            except RouteUnavailableError as exc:
                self._route_unavailable(409, exc)
                return
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
            try:
                canary = getattr(self.server.route, "canary_candidate", None)
            except RouteUnavailableError as exc:
                self._route_unavailable(409, exc)
                return
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
            except RouteUnavailableError as exc:
                self._route_unavailable(409, exc)
                return
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
            try:
                rollback = getattr(self.server.route, "rollback_candidate", None)
            except RouteUnavailableError as exc:
                self._route_unavailable(409, exc)
                return
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
            except RouteUnavailableError as exc:
                self._route_unavailable(409, exc)
                return
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
            elif (
                parsed.path == "/__mycelium/replica-qualification/install"
                and not parsed.query
            ):
                self._install_replica_qualification()
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
    replica_operator_token: str | None = None,
    trusted_https_proxy: bool = False,
    api_only: bool = False,
) -> LiveHTTPServer:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("live_mvp_requires_loopback")
    if type(api_only) is not bool:
        raise ValueError("api_only_invalid")
    static_root = Path(static_root)
    if not api_only and not (static_root / "index.html").is_file():
        raise ValueError("live_ui_build_missing")
    server = LiveHTTPServer((host, port), _handler())
    server.app = app
    server.route = route
    server.static_root = static_root
    server.api_only = api_only
    server.activation = activation
    server.capacity_refresh = capacity_refresh
    server.model_preparation = model_preparation
    server.governance_projection = governance_projection
    server.evidence_registry = evidence_registry
    server.artifact_acquisition_store = artifact_acquisition_store
    if replica_operator_token is None:
        replica_operator_token = os.environ.get("MYCELIUM_A5_OPERATOR_TOKEN")
    server.replica_operator_token = replica_operator_token
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


def _read_replica_qualification_file(path: Path) -> Mapping[str, Any]:
    """Read and validate one closed replica-qualification artifact.

    Mirrors the A4 evidence reader's safety surface: no symlinks, bounded
    size, JSON-only, closed protocol, validated against the A5 closed
    contract. Anything else fails closed and aborts serve startup.
    """

    from mycelium_replica_contracts import validate_replica_qualification

    candidate = Path(path)
    if (
        candidate.is_symlink()
        or not candidate.is_file()
        or candidate.stat().st_size > 4 * 1024 * 1024
    ):
        raise ValueError("replica_qualification_unsafe")
    try:
        document = json.loads(candidate.read_text("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("replica_qualification_invalid") from exc
    return validate_replica_qualification(document)


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


def _qualify_open_route(route: Any, *, on_challenge_event=None) -> Any:
    """Renew authority by rerunning the exact physical startup challenge."""

    startup_prompt, startup_output = route.startup_challenge
    request_id = f"startup-{uuid.uuid4().hex}"
    def report(kind: str, index: int) -> None:
        if on_challenge_event is not None:
            try:
                on_challenge_event(route.execution_graph.deployment_id,
                                   request_id, kind, index, len(startup_output))
            except Exception:
                # Telemetry cannot change inference or request release.
                pass
    try:
        report("begin", -1)  # Registration, not progress.
        try:
            exact_challenge = getattr(route, "infer_startup_challenge", None)
            if callable(exact_challenge):
                result = exact_challenge(
                    request_id=request_id,
                    sink=_DiscardSink(lambda index: report("token", index)),
                )
            else:
                result = route.infer(
                    startup_prompt,
                    max_new_tokens=len(startup_output),
                    request_id=request_id,
                    sink=_DiscardSink(lambda index: report("token", index)),
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
            **({"phone_bindings": route.phone_qualification_bindings()}
               if callable(getattr(route, "phone_qualification_bindings", None)) else {}),
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
    on_challenge_event=None,
    on_startup_failure=None,
    force_relay: bool = False,
    session_factory=None,
    external_session_factory=None,
) -> QualifiedDeploymentRuntime:
    deployment = native_deployment_projection(operator_plan)
    if progress is not None:
        progress("validating_plan")
    route = PhysicalLiveRoute.from_operator_plan(
        operator_plan,
        seed_state_root=seed_state_root,
        force_relay=force_relay,
        **({"session_factory": session_factory} if session_factory is not None else {}),
        **({"external_session_factory": external_session_factory} if external_session_factory is not None else {}),
    )
    try:
        if progress is not None:
            progress("opening_route")
        identity = route.open()
        _validate_route_identity(identity, route.execution_graph)
        if (
            identity.deployment_id != deployment.deployment_id
            or identity.model_id != deployment.model_id
            or identity.resolved_commit != deployment.model_revision
        ):
            raise RuntimeError("startup_representation_identity_mismatch")
        if progress is not None:
            progress("qualifying_route")
        # Pass the observer only when one was supplied: existing callers and
        # test doubles of _qualify_open_route take just the route.
        qualification = (_qualify_open_route(route) if on_challenge_event is None
                         else _qualify_open_route(route, on_challenge_event=on_challenge_event))
        # The physical command path revalidates the exact qualifier-owned
        # digest at every mutation boundary. Registry-backed serving must bind
        # that authority just like the single-deployment server does.
        route.set_deployment_qualification(
            qualification,
            quantization=deployment.quantization,
        )
        selected_deployment_dir = deployment_dir or _deployment_from_plan(operator_plan)
        codec = prompt_codec_from_deployment(selected_deployment_dir)
        route.set_stop_token_ids(getattr(codec, "stop_token_ids", frozenset()))
        qualify_token_path = getattr(route, "qualify_token_path", None)
        if callable(qualify_token_path):
            qualify_token_path()
        return QualifiedDeploymentRuntime(
            deployment_id=identity.deployment_id,
            model_id=identity.model_id,
            model_revision=identity.resolved_commit,
            quantization=deployment.quantization,
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
    except BaseException as exc:
        # A remote stop/close can take its full bounded command deadline.
        # Report the original refusal first; this is not cleanup evidence.
        if on_startup_failure is not None and isinstance(exc, Exception):
            try:
                on_startup_failure(deployment.deployment_id, _startup_reason(exc))
            except Exception:
                pass  # An observer cannot mask or turn a refusal into readiness.
        route.close()
        raise


def _startup_reason(exc: Exception) -> str:
    """Reduce a startup exception to one bounded non-sensitive public code."""

    code = getattr(exc, "remote_code", None) or getattr(exc, "code", None)
    if not code and isinstance(exc.__cause__, ControllerError):
        code = exc.__cause__.remote_code or exc.__cause__.code
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


def _route_node_stderr_tails(route: Any) -> dict[str, str]:
    """Each route node's bounded stderr tail (nodes with no stderr omitted).

    A startup failure whose exception carries no node diagnostic (a node
    error code alone, or a stalled decode) is otherwise undiagnosable:
    the node processes keep their stderr only in memory.
    """
    reader = getattr(route, "diagnostic_stderr", None)
    graph = getattr(route, "execution_graph", None)
    if not callable(reader) or graph is None:
        return {}
    node_ids = sorted(
        {
            placement.node_id
            for stage in getattr(graph, "stages", ())
            for placement in getattr(stage, "placements", ())
            if isinstance(getattr(placement, "node_id", None), str)
        }
    )
    tails: dict[str, str] = {}
    for node_id in node_ids:
        try:
            tail = reader(node_id)
        except Exception:
            continue
        if isinstance(tail, str) and tail.strip():
            tails[node_id] = tail[-_SERVE_DIAGNOSTIC_TAIL_CHARS:]
    return tails


def _emit_serve_diagnostic(
    exc: BaseException,
    *,
    stage: str,
    route: Any = None,
    evidence_root: Path | None = None,
) -> None:
    """Surface the node-side reason a physical route open was rejected.

    Printed BEFORE any failure-path cleanup can replace the exception,
    so the serve log keeps the real rejection (and the rejecting node's
    stderr tail) even when cleanup afterwards reports its own error.
    With ``route``, every node's bounded stderr tail is printed too.  With
    ``route`` and ``evidence_root`` every node is also asked for its evidence
    bundle, and the bundles and redacted tails are written below
    ``evidence_root`` before teardown (``_write_route_evidence``).
    """
    document = {
        "protocol": _SERVE_DIAGNOSTIC_PROTOCOL,
        "stage": stage,
        "cause_chain": _exception_chain_document(exc),
        "emitted_at_unix_ms": int(time.time() * 1_000),
    }
    tails = {} if route is None else _route_node_stderr_tails(route)
    if tails:
        document["node_stderr_tails"] = tails
    print(_json_bytes(document).decode("utf-8"), flush=True)
    if route is not None and evidence_root is not None:
        try:
            _write_route_evidence(route, stage=stage, root=Path(evidence_root), tails=tails)
        except Exception as error:  # noqa: BLE001 - diagnostics never mask exc
            print(
                _json_bytes(
                    {
                        "protocol": _SERVE_EVIDENCE_PROTOCOL,
                        "stage": stage,
                        "error": type(error).__name__[:64],
                    }
                ).decode("utf-8"),
                flush=True,
            )


_SERVE_EVIDENCE_PROTOCOL = "mycelium.live_serve_evidence.v1"
#: One node's bundle request; nodes are asked in parallel.
_SERVE_EVIDENCE_TIMEOUT_SECONDS = 20.0


def _route_node_ids(route: Any) -> list[str]:
    graph = getattr(route, "execution_graph", None)
    return sorted(
        {
            placement.node_id
            for stage in getattr(graph, "stages", ())
            for placement in getattr(stage, "placements", ())
            if isinstance(getattr(placement, "node_id", None), str)
        }
    )


def _write_private(path: Path, data: bytes) -> None:
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)


def _write_route_evidence(
    route: Any, *, stage: str, root: Path, tails: Mapping[str, str]
) -> Path:
    """Persist every route node's evidence for a failed startup.

    ``<root>/<stage>-<unix ms>/`` (0700) holds, per node, the redacted
    parent-side stderr tail (``<node id>.stderr-tail.txt``) and the bundle the
    node returned (``<node id>.evidence.json``), plus ``index.json``.  Nodes
    are asked in parallel with a bounded timeout, so one wedged node cannot
    delay the others or the teardown by more than that bound.
    """

    from physical_inference_node import redact_evidence_text

    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = root / f"{stage}-{int(time.time() * 1_000)}"
    directory.mkdir(mode=0o700)
    os.chmod(directory, 0o700)
    node_ids = _route_node_ids(route)
    reader = getattr(route, "diagnostic_evidence", None)
    results: dict[str, Any] = {}

    def collect(node_id: str) -> None:
        try:
            results[node_id] = reader(
                node_id,
                reason=stage,
                timeout_seconds=_SERVE_EVIDENCE_TIMEOUT_SECONDS,
            )
        except Exception as error:  # noqa: BLE001
            results[node_id] = error

    workers = []
    if callable(reader):
        for node_id in node_ids:
            worker = threading.Thread(
                target=collect,
                args=(node_id,),
                name=f"mycelium-serve-evidence-{node_id}",
                daemon=True,
            )
            worker.start()
            workers.append(worker)
    give_up_at = time.monotonic() + _SERVE_EVIDENCE_TIMEOUT_SECONDS + 5.0
    for worker in workers:
        worker.join(max(0.0, give_up_at - time.monotonic()))
    index: dict[str, Any] = {
        "protocol": _SERVE_EVIDENCE_PROTOCOL,
        "stage": stage,
        "nodes": {},
    }
    files: list[str] = []
    for node_id in node_ids:
        safe = "".join(
            character if character.isalnum() or character in "-_" else "_"
            for character in node_id
        )[:64]
        entry: dict[str, Any] = {"stderr_tail": None, "evidence": None}
        tail = tails.get(node_id)
        if isinstance(tail, str) and tail:
            name = f"{safe}.stderr-tail.txt"
            _write_private(directory / name, redact_evidence_text(tail).encode("utf-8"))
            entry["stderr_tail"] = name
            files.append(name)
        result = results.get(node_id)
        if isinstance(result, Mapping):
            name = f"{safe}.evidence.json"
            _write_private(directory / name, _json_bytes(dict(result)))
            entry["evidence"] = name
            node_file = result.get("file")
            entry["node_file"] = node_file if isinstance(node_file, str) else None
            files.append(name)
        elif isinstance(result, BaseException):
            entry["evidence_error"] = type(result).__name__[:64]
        elif node_id in results:
            entry["evidence_error"] = "no_evidence"
        else:
            entry["evidence_error"] = "timeout"
        index["nodes"][node_id] = entry
    _write_private(directory / "index.json", _json_bytes(index))
    print(
        _json_bytes(
            {
                "protocol": _SERVE_EVIDENCE_PROTOCOL,
                "stage": stage,
                "directory": str(directory),
                "files": sorted(files + ["index.json"]),
            }
        ).decode("utf-8"),
        flush=True,
    )
    return directory


def _record_unavailable_deployment(
    deployment: Any, exc: Exception, *, unavailable: list[UnavailableDeployment]
) -> None:
    """Record a plan whose runtime could not be built, and SAY WHY.

    Run 63r3 (2026-09-16): the registry could not build a runtime for its only deployment, and
    the bounded reason code computed here was then DROPPED - the persisted registry state
    carried health "unavailable" with no cause, so that failure had to be root-caused by
    elimination instead of read off disk. The stage diagnostic is emitted BEFORE the append so
    the serve log keeps the rejecting node's own stderr on the registry path, exactly as the
    single-server path already does via stage="route_open".
    """
    _emit_serve_diagnostic(exc, stage="qualified_runtime")
    unavailable.append(
        UnavailableDeployment(
            deployment_id=deployment.deployment_id,
            model_id=deployment.model_id,
            model_revision=deployment.model_revision,
            quantization=deployment.quantization,
            topology_size=deployment.topology_size,
            reason_code=_startup_reason(exc),
            observed_at_unix_ms=int(time.time() * 1_000),
        )
    )


def run_registry_server(
    *,
    operator_plans: Sequence[Path],
    host: str,
    port: int,
    static_root: Path | None = None,
    registry_state: Path | None = None,
    seed_state_root: Path,
    product_state_root: Path | None = None,
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
    session_factory=None,
    external_session_factory=None,
    stop_event: threading.Event | None = None,
    on_started=None,
    on_challenge_event=None,
    on_startup_failure=None,
    api_only: bool = False,
    governance_root: Path | None = ROOT,
) -> int:
    """Open, qualify, and serve one or more immutable deployments.

    An explicitly supplied session provider owns managed target communication;
    operator-plan bytes can never supply executable Python or credentials.
    """

    if stop_event is not None and not isinstance(stop_event, threading.Event):
        raise ValueError("registry_stop_event_invalid")
    if on_started is not None and not callable(on_started):
        raise ValueError("registry_start_observer_invalid")
    if on_challenge_event is not None and not callable(on_challenge_event):
        raise ValueError("registry_challenge_observer_invalid")
    if on_startup_failure is not None and not callable(on_startup_failure):
        raise ValueError("registry_failure_observer_invalid")
    if type(api_only) is not bool:
        raise ValueError("api_only_invalid")
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
            deployment = native_deployment_projection(plan)
            try:
                runtimes.append(
                    _qualified_runtime(
                        plan,
                        seed_state_root=seed_state_root,
                        model_operation_file=model_operation_file,
                        force_relay=force_relay,
                        on_challenge_event=on_challenge_event,
                        **({"on_startup_failure": on_startup_failure} if on_startup_failure is not None else {}),
                        **({"session_factory": session_factory} if session_factory is not None else {}),
                        **({"external_session_factory": external_session_factory} if external_session_factory is not None else {}),
                    )
                )
            except Exception as exc:
                _record_unavailable_deployment(deployment, exc, unavailable=unavailable)
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
                    **({"session_factory": session_factory} if session_factory is not None else {}),
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
            product_state_root=product_state_root or seed_state_root,
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
            governance_projection=(None if governance_root is None else governance_readiness(governance_root)),
            evidence_registry=evidence_registry,
            artifact_acquisition_store=artifact_acquisition_store,
            trusted_https_proxy=trusted_https_proxy,
            api_only=api_only,
        )
        startup_complete = True
        # The signal handler below runs on the main thread between bytecodes:
        # its stop flag must take no lock (threading.Event.set() does).
        stop = stop_event if stop_event is not None else StopRequest()

        def request_stop(_signum: int, _frame: Any) -> None:
            stop.set()

        # Embedded applications own cancellation, not process-global signals.
        prior = {} if stop_event is not None else {
            signum: signal.signal(signum, request_stop)
            for signum in (signal.SIGINT, signal.SIGTERM)
        }
        server.timeout = 0.5
        observed = {
            "protocol": "mycelium.live_registry_server_started.v1",
            "url": f"http://{host}:{server.server_address[1]}/",
            "startup_qualification_complete": bool(runtimes),
            "simulated": False,
            "api_only": api_only,
            "deployment_count": len(runtimes),
            "unavailable_deployment_count": len(unavailable),
        }
        try:
            if on_started is not None:
                on_started(dict(observed))
            print(_json_bytes(observed).decode("utf-8"), flush=True)
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


def _sealed_a4_atomic_commit() -> str:
    path = ROOT / "docs" / "handover" / "a4-completion-record.v1.json"
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1_048_576:
        raise RuntimeError("a4_completion_record_unavailable")
    try:
        document = json.loads(path.read_text("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise RuntimeError("a4_completion_record_invalid") from exc
    if not isinstance(document, Mapping) or document.get("state") != "complete":
        raise RuntimeError("a4_completion_record_invalid")
    commit = document.get("atomic_commit")
    if (
        not isinstance(commit, str)
        or len(commit) != 40
        or any(character not in "0123456789abcdef" for character in commit)
    ):
        raise RuntimeError("a4_completion_record_invalid")
    return commit


def _build_physical_product_stack(
    *,
    route: LiveRoute,
    deployment_dir: Path,
    execution_graph: Any,
    bearer_token: str,
    product_state_root: Path,
    seed_url: str | None,
    public_origin: str | None = None,
    trusted_https_proxy: bool = False,
    trusted_proxy_capability: bytes | None = None,
    coarse_region_declaration_file: Path | None = None,
    persist_transport_history: bool = False,
) -> LiveStack:
    tokenizer_digest = tokenizer_artifact_digest(deployment_dir)
    qualifier = LiveA6RecoveryQualifier(
        graph=execution_graph,
        deployment_dir=deployment_dir,
        tokenizer_digest=tokenizer_digest,
        route=route,
    )
    return build_live_stack(
        route=route,
        deployment_dir=deployment_dir,
        execution_graph=execution_graph,
        bearer_token=bearer_token,
        product_state_root=product_state_root,
        seed_url=seed_url,
        recovery_qualification_source=qualifier,
        recovery_tokenizer_digest=tokenizer_digest,
        recovery_a4_atomic_commit=_sealed_a4_atomic_commit(),
        public_origin=public_origin,
        trusted_https_proxy=trusted_https_proxy,
        trusted_proxy_capability=trusted_proxy_capability,
        coarse_region_declaration_file=coarse_region_declaration_file,
        persist_transport_history=persist_transport_history,
    )


def run_physical_server(
    *,
    operator_plan: Path,
    host: str,
    port: int,
    deployment_dir: Path | None = None,
    model_operation_file: Path | None = None,
    static_root: Path | None = None,
    seed_state_root: Path,
    product_state_root: Path | None = None,
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
    replica_qualification_files: Sequence[Path] = (),
    public_origin: str | None = None,
    trusted_https_proxy: bool = False,
    trusted_proxy_capability: bytes | None = None,
    force_relay: bool = False,
    a6_fault_control_port: int | None = None,
    coarse_region_declaration_file: Path | None = None,
    a10_verify_control_port: int | None = None,
    a10_batch_policy_control_port: int | None = None,
    persist_transport_history: bool = False,
    a7_evidence_control_port: int | None = None,
    a11_evidence_control_port: int | None = None,
    a11_draft_loss_control_port: int | None = None,
    a9_authority_fault_control_port: int | None = None,
) -> int:
    deployment = native_deployment_projection(operator_plan)
    selected_deployment_dir: Path | None = None
    # A failed startup keeps each node's evidence bundle here (A14 class D).
    evidence_root = (
        None if product_state_root is None else Path(product_state_root) / "diagnostics"
    )

    def route_factory() -> LiveRoute:
        return PhysicalLiveRoute.from_operator_plan(
            operator_plan,
            seed_state_root=seed_state_root,
            force_relay=force_relay,
        )

    def open_and_qualify(concrete: Any, generation: int) -> tuple[RouteIdentity, Any]:
        nonlocal selected_deployment_dir
        try:
            identity = concrete.open()
        except ControllerError as exc:
            safe_code = exc.remote_code or exc.code
            _emit_serve_diagnostic(
                exc, stage="route_open", route=concrete, evidence_root=evidence_root
            )
            raise RuntimeError(f"startup_route_open_rejected:{safe_code}") from exc
        except Exception as exc:
            # A node-process rejection (NodeProcessError) is NOT a
            # ControllerError: surface its node stderr before re-raising
            # with the same public shape as the controller path.
            _emit_serve_diagnostic(
                exc, stage="route_open", route=concrete, evidence_root=evidence_root
            )
            raise RuntimeError(
                f"startup_route_open_rejected:{_startup_reason(exc)}"
            ) from exc
        _validate_route_identity(identity, concrete.execution_graph)
        if (
            identity.deployment_id != deployment.deployment_id
            or identity.model_id != deployment.model_id
            or identity.resolved_commit != deployment.model_revision
        ):
            raise RuntimeError("startup_representation_identity_mismatch")

        try:
            qualification = _qualify_open_route(concrete)
        except Exception as exc:
            # The startup challenge failed inside an open route: print every
            # node's stderr tail before cleanup closes the route.
            _emit_serve_diagnostic(
                exc, stage="route_qualify", route=concrete, evidence_root=evidence_root
            )
            raise
        concrete.set_deployment_qualification(
            qualification,
            quantization=deployment.quantization,
        )
        if generation == 1 and save_live_qualification is not None:
            _save_live_qualification(qualification, save_live_qualification)
        if a4_evidence is not None:
            graph = concrete.execution_graph
            a4_document = build_a4_qualification(
                **a4_evidence,
                qualification_digest=qualification_digest(qualification),
                graph_digest=getattr(graph, "graph_digest", None),
                manifest_digest=getattr(graph, "manifest_digest", None),
            )
            concrete.set_a4_qualification(a4_document)

        if replica_qualification_files:
            replica_documents = [
                _read_replica_qualification_file(path)
                for path in replica_qualification_files
            ]
            concrete.set_replica_track_qualification(replica_documents)

        concrete_deployment_dir = deployment_dir or _deployment_from_plan(operator_plan)
        selected_deployment_dir = concrete_deployment_dir
        concrete.set_public_projections(
            placement=_placement_projection(concrete_deployment_dir),
            topology=_topology_projection(concrete_deployment_dir),
            workload_comparison=_workload_comparison(concrete_deployment_dir),
        )
        concrete.set_m17_model_operation(
            _m17_model_operation(
                concrete_deployment_dir,
                explicit_path=model_operation_file,
            )
        )
        return identity, qualification

    route = ColdRouteRestartOwner(
        route_factory=route_factory,
        open_and_qualify=open_and_qualify,
        restart_budget=1,
    )
    stack: LiveStack | None = None
    server: LiveHTTPServer | None = None
    openai_shim: LiveOpenAIShim | None = None
    startup_complete = False
    capacity_refresh: ModelCapacityRefresh | None = None
    artifact_acquisition_store: ArtifactAcquisitionStore | None = None
    a6_fault_control: Any | None = None
    a10_verify_control: Any | None = None
    a10_batch_policy_control: Any | None = None
    a7_evidence_control: Any | None = None
    a11_evidence_control: Any | None = None
    a11_draft_loss_control: Any | None = None
    a9_authority_fault_control: Any | None = None
    try:
        representation_authorization = _representation_authorization(
            representation_authorization_file
        )
        identity = route.open()
        qualification = route.current()
        if qualification is None:
            raise RuntimeError("startup_route_qualification_unavailable")
        active_deployment_dir = selected_deployment_dir
        if active_deployment_dir is None:
            raise RuntimeError("startup_deployment_dir_unavailable")
        stack = _build_physical_product_stack(
            route=route,
            deployment_dir=active_deployment_dir,
            execution_graph=route.execution_graph,
            bearer_token=secrets.token_urlsafe(32),
            product_state_root=product_state_root or seed_state_root,
            seed_url=seed_url,
            public_origin=public_origin,
            trusted_https_proxy=trusted_https_proxy,
            trusted_proxy_capability=trusted_proxy_capability,
            coarse_region_declaration_file=coarse_region_declaration_file,
            persist_transport_history=persist_transport_history,
        )
        stack.health.publish(qualification)
        if stack.router is None:
            raise RuntimeError("physical_qualification_router_unavailable")
        route.configure_qualification_refresh(
            refresh=_qualify_open_route,
            refresh_allowed=stack.router.is_idle,
            run_when_idle=stack.router.run_qualification_refresh_when_idle,
            on_refresh=lambda concrete, record: getattr(
                concrete, "set_deployment_qualification"
            )(record, quantization=deployment.quantization),
        )
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
                    active_deployment_dir,
                    deployment_id=identity.deployment_id,
                )
                + _explicit_historical_evidence(historical_evidence_files),
            ),
        )
        openai_shim = start_live_openai_shim(
            route=route,
            model_id=getattr(deployment, "model_id", None),
        )
        # Set from the SIGINT/SIGTERM handler: must take no lock.
        stop = StopRequest()
        if a6_fault_control_port is not None:
            a6_fault_control = _mount_a6_product_fault_control(
                stack=stack,
                route=route,
                product_port=port,
                control_port=a6_fault_control_port,
                public_origin=public_origin,
                trusted_https_proxy=trusted_https_proxy,
                request_stop=stop.set,
                stop_requested=stop.is_set,
                operator_plan=operator_plan,
            )
        if a10_verify_control_port is not None:
            a10_verify_control = _mount_a10_verify_control(
                stack=stack,
                product_port=port,
                control_port=a10_verify_control_port,
                public_origin=public_origin,
                trusted_https_proxy=trusted_https_proxy,
            )
        if a10_batch_policy_control_port is not None:
            a10_batch_policy_control = _mount_a10_batch_policy_control(
                stack=stack,
                product_port=port,
                control_port=a10_batch_policy_control_port,
                public_origin=public_origin,
                trusted_https_proxy=trusted_https_proxy,
            )
        if a7_evidence_control_port is not None:
            a7_evidence_control = _mount_a7_evidence_control(
                stack=stack,
                route=route,
                product_port=port,
                control_port=a7_evidence_control_port,
                public_origin=public_origin,
                trusted_https_proxy=trusted_https_proxy,
            )
        if a11_evidence_control_port is not None:
            a11_evidence_control = _mount_a11_evidence_control(
                route=route,
                product_state_root=product_state_root,
                product_port=port,
                control_port=a11_evidence_control_port,
                public_origin=public_origin,
                trusted_https_proxy=trusted_https_proxy,
            )
        if a11_draft_loss_control_port is not None:
            a11_draft_loss_control = _mount_a11_draft_loss_control(
                route=route, product_state_root=product_state_root,
                product_port=port, control_port=a11_draft_loss_control_port,
                public_origin=public_origin, trusted_https_proxy=trusted_https_proxy,
            )
        if a9_authority_fault_control_port is not None:
            a9_authority_fault_control = _mount_a9_authority_fault_control(
                route=route, product_state_root=product_state_root,
                product_port=port, control_port=a9_authority_fault_control_port,
                public_origin=public_origin, trusted_https_proxy=trusted_https_proxy,
            )
        startup_complete = True

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
                    "openai_shim": openai_shim.status(),
                    **(
                        {}
                        if a6_fault_control is None
                        else {"a6_fault_control": a6_fault_control.status()}
                    ),
                    **(
                        {}
                        if a10_verify_control is None
                        else {"a10_verify_control": a10_verify_control.status()}
                    ),
                    **(
                        {}
                        if a10_batch_policy_control is None
                        else {
                            "a10_batch_policy_control": (
                                a10_batch_policy_control.status()
                            )
                        }
                    ),
                    **(
                        {}
                        if a7_evidence_control is None
                        else {"a7_evidence_control": a7_evidence_control.status()}
                    ),
                    **(
                        {}
                        if a11_evidence_control is None
                        else {"a11_evidence_control": a11_evidence_control.status()}
                    ),
                    **(
                        {} if a11_draft_loss_control is None
                        else {"a11_draft_loss_control": a11_draft_loss_control.status()}
                    ),
                    **(
                        {} if a9_authority_fault_control is None
                        else {"a9_authority_fault_control": a9_authority_fault_control.status()}
                    ),
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
        a6_processes = (
            () if a6_fault_control is None else _a6_session_processes(route)
        )
        if a11_evidence_control is not None:
            a11_evidence_control.close()
        if a11_draft_loss_control is not None:
            a11_draft_loss_control.close()
        if a9_authority_fault_control is not None:
            a9_authority_fault_control.close()
        if a7_evidence_control is not None:
            a7_evidence_control.close()
        if a10_batch_policy_control is not None:
            a10_batch_policy_control.close()
        if a10_verify_control is not None:
            a10_verify_control.close()
        if a6_fault_control is not None:
            a6_fault_control.close()
            print(
                _json_bytes(a6_fault_control.teardown_witness()).decode("utf-8"),
                flush=True,
            )
        if openai_shim is not None:
            openai_shim.close()
        if capacity_refresh is not None:
            capacity_refresh.close()
        if stack is not None:
            stack.health.drop()
        if server is not None:
            server.server_close()
        try:
            if stack is not None:
                stack.close_router()
        finally:
            route.close()
        if a6_fault_control is not None and stack is not None:
            a6_fault_control.finalize(
                product_port=port,
                processes=a6_processes,
                router=stack.router,
            )
        if not startup_complete:
            try:
                route.cleanup()
            except Exception as cleanup_exc:
                # The failure-path cleanup must never mask the real
                # startup rejection: keep the original exception in
                # flight and record the cleanup failure in the serve
                # log instead.
                _emit_serve_diagnostic(cleanup_exc, stage="route_cleanup")


def _mount_a10_verify_control(
    *,
    stack: Any,
    product_port: int,
    control_port: int,
    public_origin: str | None,
    trusted_https_proxy: bool,
) -> Any:
    """Mount the grant-gated A10 verification control on its own loopback listener.

    Every request is handed to the product dispatcher (the live router port),
    which admits the operation exclusively and accepts only the node's record.
    """

    from mycelium_a10_verify_control import (
        A10VerifyControl,
        A10VerifyControlRefused,
        grant_from_environment,
    )

    dispatch = getattr(getattr(stack, "router", None), "a10_target_verify", None)
    grant = grant_from_environment()
    try:
        if grant is not None and not callable(dispatch):
            raise RuntimeError("startup_a10_verify_control_dispatcher_unavailable")
        control = A10VerifyControl(
            grant=grant,
            dispatch=dispatch,  # type: ignore[arg-type]
            product_port=product_port,
            port=control_port,
            public_origin=public_origin,
            trusted_https_proxy=trusted_https_proxy,
        )
        control.start()
    except A10VerifyControlRefused as exc:
        raise RuntimeError(f"startup_a10_verify_control_refused:{exc}") from exc
    except OSError as exc:
        raise RuntimeError("startup_a10_verify_control_bind_failed") from exc
    return control


def _mount_a10_batch_policy_control(
    *,
    stack: Any,
    product_port: int,
    control_port: int,
    public_origin: str | None,
    trusted_https_proxy: bool,
) -> Any:
    """Mount the grant-gated A10 batch-policy control on its own loopback listener.

    Every request is handed to the product dispatcher (the live router port),
    which applies the policy to every node of the deployment and accepts only
    each node's own receipt.
    """

    from mycelium_a10_batch_policy_control import (
        A10BatchPolicyControl,
        A10BatchPolicyControlRefused,
        grant_from_environment,
    )

    dispatch = getattr(getattr(stack, "router", None), "a10_batch_policy", None)
    grant = grant_from_environment()
    try:
        if grant is not None and not callable(dispatch):
            raise RuntimeError("startup_a10_batch_policy_control_dispatcher_unavailable")
        control = A10BatchPolicyControl(
            grant=grant,
            dispatch=dispatch,  # type: ignore[arg-type]
            product_port=product_port,
            port=control_port,
            public_origin=public_origin,
            trusted_https_proxy=trusted_https_proxy,
        )
        control.start()
    except A10BatchPolicyControlRefused as exc:
        raise RuntimeError(f"startup_a10_batch_policy_control_refused:{exc}") from exc
    except OSError as exc:
        raise RuntimeError("startup_a10_batch_policy_control_bind_failed") from exc
    return control


def _mount_a7_evidence_control(
    *,
    stack: Any,
    route: Any,
    product_port: int,
    control_port: int,
    public_origin: str | None,
    trusted_https_proxy: bool,
    token_dir: Path | None = None,
) -> Any:
    """Mount the grant-gated, read-only A7 evidence door on its own loopback listener.

    The door reads only the entry's signed ``a7_snapshot`` observation, the
    route's request-scoped incidents and resource counters; it never mutates
    the route, the router or a node.  Its per-lifetime secret token lives only
    in an owner-only file inside ``token_dir`` (a fresh private directory by
    default); the path is published in the serve's ``live_server_started`` line
    so the measurement client can read the token, and the token itself is never
    printed, logged or returned by any status.
    """

    from mycelium_a7_evidence_control import (
        A7EvidenceControl,
        A7EvidenceControlRefused,
        RouteA7Evidence,
        grant_from_environment,
    )

    directory = Path(tempfile.mkdtemp(prefix="mycelium-a7-evidence-", dir=token_dir)) if token_dir else Path(
        tempfile.mkdtemp(prefix="mycelium-a7-evidence-")
    )
    # The identity of the directory mkdtemp just made, so teardown can prove it
    # is still ours before removing anything.  Without it, a teardown that
    # removes a *replaced* directory would destroy another life's token - the
    # recursive removal destroyed whatever was standing there.
    directory_identity = _directory_identity(directory)
    try:
        control = A7EvidenceControl(
            grant=grant_from_environment(),
            source=RouteA7Evidence(route, router=getattr(stack, "router", None)),
            token_file=directory / "a7-evidence.token",
            product_port=product_port,
            port=control_port,
            public_origin=public_origin,
            trusted_https_proxy=trusted_https_proxy,
        )
        control.start()
    except A7EvidenceControlRefused as exc:
        _remove_own_empty_directory(directory, directory_identity)
        raise RuntimeError(f"startup_a7_evidence_control_refused:{exc}") from exc
    except OSError as exc:
        _remove_own_empty_directory(directory, directory_identity)
        raise RuntimeError("startup_a7_evidence_control_bind_failed") from exc
    except BaseException:
        # A thread or listener that died mid-start must not leave either behind.
        _remove_own_empty_directory(directory, directory_identity)
        raise
    # The token directory exists only for this lifetime; the door removes its
    # own token file, and this removes the now-empty private directory with it.
    control.close = _closing_with(control.close, directory, directory_identity)
    return control


def _mount_a11_evidence_control(
    *,
    route: Any,
    product_state_root: Path | None,
    product_port: int,
    control_port: int,
    public_origin: str | None,
    trusted_https_proxy: bool,
) -> Any:
    """Mount the grant-gated, read-only A11 evidence door on its own loopback listener.

    The door sends every live node only the read-only ``a11_snapshot`` command
    over the route's own signed command channel and returns the verified signed
    envelopes; it never mutates the route, the router or a node.  Its
    per-lifetime token is written owner-only into the product state root and
    removed when the door closes.
    """

    if product_state_root is None:
        raise RuntimeError("startup_a11_evidence_control_refused:product_state_root_required")

    from mycelium_a11_evidence_control import (
        A11EvidenceControl,
        A11EvidenceControlRefused,
        RouteA11Evidence,
        grant_from_environment,
    )

    try:
        control = A11EvidenceControl(
            grant=grant_from_environment(),
            source=RouteA11Evidence(route),
            token_file=Path(product_state_root) / "a11-evidence.token",
            product_port=product_port,
            port=control_port,
            public_origin=public_origin,
            trusted_https_proxy=trusted_https_proxy,
        )
        control.start()
    except A11EvidenceControlRefused as exc:
        raise RuntimeError(f"startup_a11_evidence_control_refused:{exc}") from exc
    except OSError as exc:
        raise RuntimeError("startup_a11_evidence_control_bind_failed") from exc
    return control


def _mount_a11_draft_loss_control(
    *, route: Any, product_state_root: Path | None, product_port: int,
    control_port: int, public_origin: str | None, trusted_https_proxy: bool,
) -> Any:
    """Explicit A11-only direct-loopback fault mount, absent on normal serves."""
    from mycelium_a11_draft_loss_control import DraftLossControl, RouteDraftLoss, DraftLossRefused

    if product_state_root is None:
        raise DraftLossRefused("a11_draft_loss_product_state_root_required")
    control = DraftLossControl(
        grant=os.environ.get("MYCELIUM_A11_FLEET_GRANT"),
        source=RouteDraftLoss(route),
        token_file=Path(product_state_root) / "a11-draft-loss.token",
        product_port=product_port, port=control_port,
        public_origin=public_origin, trusted_https_proxy=trusted_https_proxy,
    )
    control.start()
    return control


def _mount_a9_authority_fault_control(
    *, route: Any, product_state_root: Path | None, product_port: int,
    control_port: int, public_origin: str | None, trusted_https_proxy: bool,
) -> Any:
    """Explicit A9-only direct-loopback fault mount, absent on normal serves."""
    from mycelium_a9_authority_fault_control import (
        AuthorityFaultControl, AuthorityFaultRefused, RouteAuthorityFault,
    )
    from mycelium_a9_authority_ledger import GRANT_ENV

    if product_state_root is None:
        raise AuthorityFaultRefused("a9_authority_fault_product_state_root_required")
    control = AuthorityFaultControl(
        grant=os.environ.get(GRANT_ENV),
        source=RouteAuthorityFault(route),
        token_file=Path(product_state_root) / "a9-authority-fault.token",
        product_port=product_port, port=control_port,
        public_origin=public_origin, trusted_https_proxy=trusted_https_proxy,
    )
    control.start()
    return control


def _directory_identity(directory: Path) -> tuple[int, int] | None:
    """``(st_dev, st_ino)`` of the private directory this mount created."""

    try:
        metadata = directory.lstat()
    except OSError:
        return None
    return (metadata.st_dev, metadata.st_ino)


def _remove_own_empty_directory(directory: Path, identity: tuple[int, int] | None) -> bool:
    """Remove ``directory`` only while it is still our empty private directory.

    Never recursive: this directory holds one token file that the door itself
    unlinks by identity, so anything left in it is something else's and is left
    standing.  A replaced, re-pointed or no-longer-empty directory is refused.
    """

    if identity is None:
        return False
    try:
        metadata = directory.lstat()
    except OSError:
        return False
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or (metadata.st_dev, metadata.st_ino) != identity
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        return False
    try:
        directory.rmdir()
    except OSError:
        return False
    return True


def _closing_with(close: Any, directory: Path, identity: tuple[int, int] | None) -> Any:
    def _close() -> None:
        try:
            close()
        finally:
            _remove_own_empty_directory(directory, identity)

    return _close



A6_FAULT_CONTROL_TEARDOWN_PROTOCOL = "mycelium.live_server_a6_fault_control_stopped.v1"


class _A6FaultControlMount:
    """The mounted receiver plus the product's source-owned A6 observations."""

    def __init__(self, control: Any, recorder: Any | None, authority: Any | None) -> None:
        self.control = control
        self.recorder = recorder
        self.authority = authority

    @property
    def port(self) -> int:
        return self.control.port

    @property
    def executed_case_ids(self) -> tuple[str, ...]:
        return self.control.executed_case_ids

    def status(self) -> dict[str, Any]:
        status = dict(self.control.status())
        if self.recorder is not None:
            status["observations"] = self.recorder.status()
        return status

    def close(self) -> None:
        self.control.close()

    def teardown_witness(self) -> dict[str, Any]:
        """The closed receiver's own lifetime and successful executions.

        Only a receiver that listened and has since closed can report; the
        case ids are the receiver's 200-completed execution order, so a
        refused or repeated request never appears.
        """

        status = self.control.status()
        if status["mounted"] or status["lifetime_id"] is None:
            raise RuntimeError("a6_fault_control_teardown_before_close")
        return {
            "protocol": A6_FAULT_CONTROL_TEARDOWN_PROTOCOL,
            "lifetime_id": status["lifetime_id"],
            "executed_case_ids": list(status["executed_case_ids"]),
            "torn_down": True,
        }

    def finalize(self, *, product_port: int, processes: Sequence[Any], router: Any) -> None:
        """After the listener, route and peers closed: settle and write cleanup.

        Case observations are settled at every shutdown. The cleanup
        observation is measured only by the run's final shutdown, the one the
        frozen last case (``shutdown_during_recovery``) triggers; an earlier
        serve lifetime of the same run writes none.
        """

        if self.recorder is None or self.authority is None:
            return
        from mycelium_a6_case_observations import write_cleanup_observation

        self.recorder.join(timeout=60.0)
        if "shutdown_during_recovery" not in self.control.executed_case_ids:
            return
        try:
            write_cleanup_observation(
                authority=self.authority,
                product_port=product_port,
                physical_process_count=sum(
                    1 for process in processes if process.returncode is None
                ),
                reservation_count=_a6_reservation_count(router),
            )
        except Exception as exc:  # never mask the serve exit; no file is written
            _emit_serve_diagnostic(exc, stage="a6_cleanup_observation")


def _a6_session_processes(route: Any) -> tuple[Any, ...]:
    """The node processes this serve launched, captured before route close."""

    try:
        concrete = route._current_route_or_raise()
    except Exception:
        return ()
    sessions = getattr(concrete, "_sessions", None)
    if not isinstance(sessions, Mapping):
        return ()
    return tuple(
        process
        for process in (getattr(session, "_process", None) for session in sessions.values())
        if process is not None
    )


def _a6_reservation_count(router: Any) -> int:
    coordinator = getattr(router, "_coordinator", None)
    if coordinator is None:
        raise RuntimeError("a6_reservation_count_unavailable")
    return sum(int(item["reservation_count"]) for item in coordinator.status()["requests"])


def _a6_product_fault_seams(
    *,
    stack: LiveStack,
    route: Any,
    request_stop: Callable[[], None],
    stop_requested: Callable[[], bool] | None = None,
    grant: str | None = None,
    target_sink: list[Any] | None = None,
) -> Mapping[str, Any]:
    """Bind every A6 case to real peer/path faults on this product stack."""

    from mycelium_a6_product_fault_seams import (
        LiveProductFaultTarget,
        build_product_case_seams,
        request_gateway_service,
    )

    router = stack.router
    service = request_gateway_service(stack.app)
    if router is None or service is None or grant is None:
        raise RuntimeError("startup_a6_fault_control_product_unavailable")
    target = LiveProductFaultTarget(
        router=router,
        route=route,
        service=service,
        qualifier=getattr(router, "_recovery_qualification_source", None),
        request_stop=request_stop,
        stop_requested=stop_requested,
        grant=grant,
    )
    if target_sink is not None:
        target_sink.append(target)
    return build_product_case_seams(target)


def _mount_a6_product_fault_control(
    *,
    stack: LiveStack,
    route: Any,
    product_port: int,
    control_port: int,
    public_origin: str | None,
    trusted_https_proxy: bool,
    request_stop: Callable[[], None],
    stop_requested: Callable[[], bool] | None = None,
    operator_plan: Path | None = None,
) -> Any:
    """Mount the grant-gated A6 receiver on its own direct loopback listener.

    With a physical-runner operator plan the product also records the
    source-owned case observations and the cleanup observation the A6 gate
    reads below that plan's evidence directory.
    """

    from mycelium_a6_product_fault_control import (
        ProductFaultControl,
        ProductFaultControlRefused,
        grant_from_environment,
    )

    attach_guard = getattr(route, "attach_recovery_guard", None)
    if callable(attach_guard):
        attach_guard()
    grant = grant_from_environment()
    targets: list[Any] = []
    authority = None
    if grant is not None and operator_plan is not None:
        from mycelium_a6_case_observations import ObservationRefused, observation_authority

        try:
            authority = observation_authority(operator_plan=operator_plan, repo_root=ROOT)
        except ObservationRefused as exc:
            raise RuntimeError(f"startup_a6_fault_control_refused:{exc}") from exc
    try:
        control = ProductFaultControl(
            grant=grant,
            seams=(
                {}
                if grant is None
                else _a6_product_fault_seams(
                    stack=stack,
                    route=route,
                    request_stop=request_stop,
                    stop_requested=stop_requested,
                    grant=grant,
                    target_sink=targets,
                )
            ),
            product_port=product_port,
            port=control_port,
            public_origin=public_origin,
            trusted_https_proxy=trusted_https_proxy,
        )
        control.start()
    except ProductFaultControlRefused as exc:
        raise RuntimeError(f"startup_a6_fault_control_refused:{exc}") from exc
    except OSError as exc:
        raise RuntimeError("startup_a6_fault_control_bind_failed") from exc
    recorder = None
    if authority is not None and targets:
        from mycelium_a6_case_observations import CaseObservationRecorder

        recorder = CaseObservationRecorder(authority=authority, target=targets[0])
        targets[0].add_listener(recorder.observe)
    return _A6FaultControlMount(control, recorder, authority)


class _DiscardSink:
    def __init__(self, on_verified_index=None):
        self._on_verified_index = on_verified_index

    def emit(self, _token_index: int, _token_id: int) -> None:
        if self._on_verified_index is not None:
            try:
                self._on_verified_index(_token_index)
            except Exception:
                pass


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
        "--product-state-root",
        type=Path,
        help="private product-event journal root; defaults to the seed state root",
    )
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
        "--replica-qualification",
        type=Path,
        action="append",
        help="sealed A5 replica-track qualification; repeat for each qualified track",
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
    parser.add_argument(
        "--coarse-region-declaration-file",
        type=Path,
        help=(
            "owner-private mycelium.operator_region_declaration.v1 file; publishes "
            "owner-declared coarse regions to the live route explorer"
        ),
    )
    parser.add_argument(
        "--persist-transport-history",
        action="store_true",
        help=(
            "operator-only A14 control: keep the direct/relay ledger in the product "
            "state root, bound to the served deployment, across planned restarts"
        ),
    )
    parser.add_argument(
        "--a6-fault-control-port",
        type=int,
        help=(
            "mount the grant-gated A6 controlled-fault receiver on this direct "
            "127.0.0.1 port (0 = ephemeral); requires MYCELIUM_A6_FLEET_GRANT"
        ),
    )
    parser.add_argument(
        "--a10-verify-control-port",
        type=int,
        help=(
            "mount the grant-gated A10 target-verification control on this direct "
            "127.0.0.1 port (0 = ephemeral); requires MYCELIUM_A10_FLEET_GRANT"
        ),
    )
    parser.add_argument(
        "--a10-batch-policy-control-port",
        type=int,
        help=(
            "mount the grant-gated A10 batch-policy control (baseline/candidate) on "
            "this direct 127.0.0.1 port (0 = ephemeral); requires "
            "MYCELIUM_A10_FLEET_GRANT"
        ),
    )
    parser.add_argument(
        "--a7-evidence-control-port",
        type=int,
        help=(
            "mount the grant-gated, read-only A7 evidence door on this direct "
            "127.0.0.1 port (0 = ephemeral); requires MYCELIUM_A7_FLEET_GRANT"
        ),
    )
    parser.add_argument(
        "--a11-evidence-control-port",
        type=int,
        help=(
            "mount the grant-gated, read-only A11 evidence door on this direct "
            "127.0.0.1 port (0 = ephemeral); requires MYCELIUM_A11_FLEET_GRANT"
        ),
    )
    parser.add_argument("--a11-draft-loss-control-port", type=int,
                        help="grant-gated A11 request-scoped draft loss on direct loopback")
    parser.add_argument("--a9-authority-fault-control-port", type=int,
                        help="grant-gated A9 authority fault publisher on direct loopback; "
                             "requires MYCELIUM_A9_FLEET_GRANT")
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
    if args.a6_fault_control_port is not None and not (
        args.a6_fault_control_port == 0
        or 1_024 <= args.a6_fault_control_port <= 65_535
    ):
        raise SystemExit("--a6-fault-control-port must be 0 or from 1024 through 65535")
    if args.a10_verify_control_port is not None and not (
        args.a10_verify_control_port == 0
        or 1_024 <= args.a10_verify_control_port <= 65_535
    ):
        raise SystemExit("--a10-verify-control-port must be 0 or from 1024 through 65535")
    if args.a10_batch_policy_control_port is not None and not (
        args.a10_batch_policy_control_port == 0
        or 1_024 <= args.a10_batch_policy_control_port <= 65_535
    ):
        raise SystemExit(
            "--a10-batch-policy-control-port must be 0 or from 1024 through 65535"
        )
    if args.a7_evidence_control_port is not None and not (
        args.a7_evidence_control_port == 0
        or 1_024 <= args.a7_evidence_control_port <= 65_535
    ):
        raise SystemExit("--a7-evidence-control-port must be 0 or from 1024 through 65535")
    if args.a11_evidence_control_port is not None and not (
        args.a11_evidence_control_port == 0
        or 1_024 <= args.a11_evidence_control_port <= 65_535
    ):
        raise SystemExit("--a11-evidence-control-port must be 0 or from 1024 through 65535")
    if args.a11_draft_loss_control_port is not None and not (
        args.a11_draft_loss_control_port == 0
        or 1_024 <= args.a11_draft_loss_control_port <= 65_535
    ):
        raise SystemExit("--a11-draft-loss-control-port must be 0 or from 1024 through 65535")
    if args.a9_authority_fault_control_port is not None and not (
        args.a9_authority_fault_control_port == 0
        or 1_024 <= args.a9_authority_fault_control_port <= 65_535
    ):
        raise SystemExit("--a9-authority-fault-control-port must be 0 or from 1024 through 65535")
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
            product_state_root=args.product_state_root,
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
            replica_qualification_files=tuple(args.replica_qualification or ()),
            public_origin=args.public_origin,
            trusted_https_proxy=args.trusted_https_proxy,
            trusted_proxy_capability=trusted_proxy_capability,
            force_relay=args.a8_force_relay,
            **(
                {}
                if args.a6_fault_control_port is None
                else {"a6_fault_control_port": args.a6_fault_control_port}
            ),
            **(
                {}
                if args.a10_verify_control_port is None
                else {"a10_verify_control_port": args.a10_verify_control_port}
            ),
            **(
                {}
                if args.a10_batch_policy_control_port is None
                else {
                    "a10_batch_policy_control_port": args.a10_batch_policy_control_port
                }
            ),
            **(
                {}
                if args.coarse_region_declaration_file is None
                else {
                    "coarse_region_declaration_file": args.coarse_region_declaration_file
                }
            ),
            **(
                {"persist_transport_history": True}
                if args.persist_transport_history
                else {}
            ),
            **(
                {}
                if args.a7_evidence_control_port is None
                else {"a7_evidence_control_port": args.a7_evidence_control_port}
            ),
            **(
                {}
                if args.a11_evidence_control_port is None
                else {"a11_evidence_control_port": args.a11_evidence_control_port}
            ),
            **(
                {} if args.a11_draft_loss_control_port is None
                else {"a11_draft_loss_control_port": args.a11_draft_loss_control_port}
            ),
            **(
                {} if args.a9_authority_fault_control_port is None
                else {"a9_authority_fault_control_port": args.a9_authority_fault_control_port}
            ),
        )
    if args.persist_transport_history:
        raise SystemExit("--persist-transport-history is valid only with one --operator-plan")
    if args.coarse_region_declaration_file is not None:
        raise SystemExit(
            "--coarse-region-declaration-file is valid only with one --operator-plan"
        )
    if args.a6_fault_control_port is not None:
        raise SystemExit("--a6-fault-control-port is valid only with one --operator-plan")
    if args.a10_verify_control_port is not None:
        raise SystemExit("--a10-verify-control-port is valid only with one --operator-plan")
    if args.a10_batch_policy_control_port is not None:
        raise SystemExit(
            "--a10-batch-policy-control-port is valid only with one --operator-plan"
        )
    if args.a7_evidence_control_port is not None:
        raise SystemExit("--a7-evidence-control-port is valid only with one --operator-plan")
    if args.a11_evidence_control_port is not None:
        raise SystemExit("--a11-evidence-control-port is valid only with one --operator-plan")
    if args.a11_draft_loss_control_port is not None:
        raise SystemExit("--a11-draft-loss-control-port is valid only with one --operator-plan")
    if args.a9_authority_fault_control_port is not None:
        raise SystemExit("--a9-authority-fault-control-port is valid only with one --operator-plan")
    if args.deployment_dir is not None:
        raise SystemExit("--deployment-dir is valid only with one --operator-plan")
    return run_registry_server(
        operator_plans=args.operator_plan,
        host=args.host,
        port=args.port,
        static_root=args.static_root,
        registry_state=args.registry_state,
        seed_state_root=args.seed_state_root,
        product_state_root=args.product_state_root,
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
    "COLD_ROUTE_RESTART_ERROR_CODE",
    "COLD_ROUTE_RESTART_EXHAUSTED_CODE",
    "COLD_ROUTE_RESTART_FAILED_CODE",
    "ColdRouteRestartError",
    "ColdRouteRestartOwner",
    "LiveStack",
    "RouteUnavailableError",
    "build_live_stack",
    "build_registry_stack",
    "create_server",
    "main",
    "run_physical_server",
    "run_registry_server",
]
