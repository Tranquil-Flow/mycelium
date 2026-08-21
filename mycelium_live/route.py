"""Lifecycle seam for a physical inference route that outlives one request."""

from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import threading
import time
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.parse import quote

from mycelium_membership.contracts import (
    peer_runtime_is_activation_eligible,
    sign_membership_message,
)
from mycelium_invite import mint_invite_bundle
from mycelium_node.process import (
    PhysicalNodeProcess,
    PrivateDirectoryLease,
    private_directory_lease,
)
from mycelium_seed.authority import (
    PRODUCT_PSEUDONYM_KEY_FILE,
    SeedAuthorityError,
    derive_product_pseudonym_salt,
    load_bound_seed_signer,
    load_product_pseudonym_salt,
)
from mycelium_seed.state import SeedStateError, SqliteSeedState
from mycelium_seed.operator import SeedOperatorError, revoke_seed_member
from mycelium_qualification.signing import Ed25519EvidenceSigner
from mycelium_router.contracts import ExecutionGraph
from mycelium_router.serialization import execution_graph_to_dict
from physical_inference_node import execution_graph_from_document
from physical_inference_qualification import (
    NODE_COMMAND_TIMEOUT_SECONDS,
    NODE_SESSION_TIMEOUT_SECONDS,
    PeerIdentity,
    QualificationController,
    _peer_process_argv,
)
from mycelium_live.liveness import (
    LivenessState,
    LivenessSubject,
    ObservationSource,
    SubjectKind,
    TrafficAwareLivenessDetector,
)


_IDLE_KEEPALIVE_PROBE_DEADLINE_SECONDS = 0.5


class AffectedPeerQuarantined(RuntimeError):
    """Admission refused: a participating peer is liveness-quarantined.

    Raised before any node command is issued, so no command-ledger terminal
    CAS is owed — the router port publishes a bounded failed terminal
    directly instead of the cleanup-unproven nonterminal shape.
    """


class _ConcurrentNodeSession:
    """Adapt the command-demultiplexing process boundary to route evidence envelopes."""

    def __init__(
        self,
        *,
        argv: tuple[str, ...],
        node_id: str,
        run_id: str,
        deployment_id: str,
        timeout_seconds: float,
    ) -> None:
        self._process = PhysicalNodeProcess(
            command=argv,
            node_id=node_id,
            run_id=run_id,
            deployment_id=deployment_id,
            response_timeout_seconds=timeout_seconds,
        )
        self.node_id = node_id

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    @property
    def stderr(self) -> bytes:
        return self._process.stderr_tail.encode("utf-8", errors="replace")

    def send(
        self,
        *,
        command_id: str,
        command: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        result = self._process.command(
            command,
            payload,
            terminate_on_timeout=False,
            command_id=command_id,
        )
        return {
            "protocol": "mycelium.physical_node_control.v1",
            "command_id": command_id,
            "node_id": self.node_id,
            "ok": True,
            "route_ready": False,
            "result": result,
        }

    def send_before(
        self,
        *,
        command_id: str,
        command: str,
        payload: Mapping[str, Any],
        deadline_monotonic_s: float,
    ) -> dict[str, Any]:
        remaining = deadline_monotonic_s - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("command_deadline_exceeded")
        result = self._process.command(
            command,
            payload,
            timeout_seconds=remaining,
            terminate_on_timeout=False,
            command_id=command_id,
        )
        return {
            "protocol": "mycelium.physical_node_control.v1",
            "command_id": command_id,
            "node_id": self.node_id,
            "ok": True,
            "route_ready": False,
            "result": result,
        }

    def close(self) -> None:
        self._process.close()

    def interrupt_command(self, command_id: str, *, code: str) -> bool:
        return self._process.interrupt_command(command_id, code=code)


class TokenSink(Protocol):
    def emit(self, token_index: int, token_id: int) -> None: ...


@dataclass(frozen=True, slots=True)
class RouteCounters:
    frames_sent: int
    frames_received: int
    applied_operation_count: int
    fatal: str | None = None


@dataclass(frozen=True, slots=True)
class InferenceResult:
    request_id: str
    token_ids: tuple[int, ...]


class InferenceCancelled(RuntimeError):
    """The caller cancelled after admission and physical cleanup completed."""


def _request_is_cancelled(
    status: object,
    cancel_requested: Callable[[], bool] | None,
) -> bool:
    """Treat the physical terminal as authoritative across callback races."""

    return status == "CANCELLED" or (
        status == "DECODING"
        and cancel_requested is not None
        and cancel_requested()
    )


@dataclass(frozen=True, slots=True)
class RouteIdentity:
    deployment_id: str
    model_id: str
    resolved_commit: str
    endpoint_ids: tuple[str, ...]


class LiveSeedStateError(RuntimeError):
    """Stable fail-closed error while loading the live membership authority."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(slots=True)
class LiveSeedAuthority:
    """Descriptor-bound, load-only view of one durable seed identity and database."""

    state_root: PrivateDirectoryLease
    signer: Ed25519EvidenceSigner
    database: Path
    swarm_id: str
    seed_node_id: str
    members: tuple[dict[str, Any], ...]
    authority_generation: int
    pseudonym_salt: bytes
    rotation_status: str | None
    rotation_observed_at: float | None

    def close(self) -> None:
        self.state_root.close()

    def current_members(self) -> tuple[dict[str, Any], ...]:
        self.state_root.revalidate()
        return tuple(
            {
                **member,
                "authority_generation": self.authority_generation,
                "rotation_status": self.rotation_status,
                "rotation_observed_at": self.rotation_observed_at,
            }
            for member in _read_live_seed_members(self.database)
        )

    def product_pseudonym_salt(self) -> bytes:
        """Derive one stable secret salt without exposing coordinator key bytes."""

        return self.pseudonym_salt


def _read_live_seed_members(database: Path) -> tuple[dict[str, Any], ...]:
    uri = f"file:{quote(str(database), safe='/')}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only = ON")
            member_rows = connection.execute(
                """
                SELECT node_id, endpoint_id, endpoint_addrs_json,
                       peer_class, runtime_capability_json,
                       verification_key_digest, incarnation, generation,
                       lease_expires_at, last_heartbeat_sequence,
                       last_liveness_at, next_heartbeat_due_at,
                       last_activity_receipt_at, active_requests,
                       lifecycle_state
                FROM seed_members
                ORDER BY node_id
                """
            ).fetchall()
        finally:
            connection.close()
        members = []
        for row in member_rows:
            member = SqliteSeedState._decode_member_row(row)
            # Activation eligibility is a derived policy decision, not durable
            # member-controlled state.  Keep the database schema free of that
            # claim while ensuring every live consumer evaluates the exact same
            # peer-class/runtime-capability rule used during route construction.
            member["activation_eligible"] = peer_runtime_is_activation_eligible(
                member["peer_class"],
                member["runtime_capability"],
            )
            members.append(member)
        return tuple(members)
    except (SeedStateError, sqlite3.Error) as exc:
        raise LiveSeedStateError("live_seed_database_corrupt") from exc


def _load_live_seed_authority(
    *,
    seed_state_root: Path,
    plan_membership_snapshot: Mapping[str, Any],
) -> LiveSeedAuthority:
    """Load and bind an existing seed root without creating or rewriting it."""

    lease: PrivateDirectoryLease | None = None
    try:
        lease = private_directory_lease(seed_state_root, create=False)
        if not lease.exists:
            raise LiveSeedStateError("live_seed_state_missing")
        lease.revalidate()
        database = lease.path / "state.sqlite3"
        try:
            metadata = database.lstat()
        except FileNotFoundError as exc:
            raise LiveSeedStateError("live_seed_database_missing") from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
        ):
            raise LiveSeedStateError("live_seed_database_path_invalid")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise LiveSeedStateError("live_seed_database_permissions_invalid")

        uri = f"file:{quote(str(database), safe='/')}?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=5.0)
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("PRAGMA query_only = ON")
                quick_check = connection.execute("PRAGMA quick_check").fetchone()
                if quick_check is None or quick_check[0] != "ok":
                    raise LiveSeedStateError("live_seed_database_corrupt")
                rows = connection.execute(
                    "SELECT key, value FROM seed_metadata "
                    "WHERE key IN ('swarm_id', 'seed_node_id', 'seed_key_digest', "
                    "'authority_generation')"
                ).fetchall()
                rotation_row = connection.execute(
                    "SELECT status, initiated_at FROM seed_authority_rotations "
                    "ORDER BY authority_generation DESC LIMIT 1"
                ).fetchone()
            finally:
                connection.close()
        except LiveSeedStateError:
            raise
        except sqlite3.Error as exc:
            raise LiveSeedStateError("live_seed_database_corrupt") from exc
        binding = {
            row["key"]: row["value"]
            for row in rows
            if isinstance(row["key"], str) and isinstance(row["value"], str)
        }
        if set(binding) != {
            "swarm_id",
            "seed_node_id",
            "seed_key_digest",
            "authority_generation",
        }:
            raise LiveSeedStateError("live_seed_database_identity_missing")
        try:
            authority_generation = int(binding["authority_generation"])
        except ValueError as exc:
            raise LiveSeedStateError("live_seed_database_identity_missing") from exc
        if (
            authority_generation < 1
            or str(authority_generation) != binding["authority_generation"]
        ):
            raise LiveSeedStateError("live_seed_database_identity_missing")
        try:
            signer = load_bound_seed_signer(
                lease.path / "identity",
                expected_digest=binding["seed_key_digest"],
            )
        except SeedAuthorityError as exc:
            mapping = {
                "seed_authority_key_missing": "live_seed_identity_missing",
                "node_identity_permissions_invalid": (
                    "live_seed_identity_permissions_invalid"
                ),
                "node_identity_invalid": "live_seed_identity_invalid",
                "seed_authority_key_mismatch": "live_seed_database_identity_mismatch",
            }
            raise LiveSeedStateError(
                mapping.get(exc.code, "live_seed_identity_path_invalid")
            ) from exc
        pseudonym_path = lease.path / "identity" / PRODUCT_PSEUDONYM_KEY_FILE
        if pseudonym_path.exists() or pseudonym_path.is_symlink():
            try:
                pseudonym_salt = load_product_pseudonym_salt(lease.path / "identity")
            except SeedAuthorityError as exc:
                raise LiveSeedStateError("live_seed_product_identity_invalid") from exc
        elif authority_generation == 1:
            pseudonym_salt = derive_product_pseudonym_salt(
                signer,
                swarm_id=binding["swarm_id"],
            )
        else:
            raise LiveSeedStateError("live_seed_product_identity_missing")
        plan_digest = plan_membership_snapshot.get("seed_key_digest")
        if plan_digest != signer.verification_key_digest:
            raise LiveSeedStateError("live_seed_plan_identity_mismatch")
        plan_swarm_id = plan_membership_snapshot.get("swarm_id")
        if plan_swarm_id != binding["swarm_id"]:
            raise LiveSeedStateError("live_seed_plan_swarm_mismatch")
        members = _read_live_seed_members(database)
        rotation_status = None
        rotation_observed_at = None
        if rotation_row is not None:
            rotation_status = str(rotation_row["status"]).lower()
            rotation_observed_at = float(rotation_row["initiated_at"])
        lease.revalidate()
        return LiveSeedAuthority(
            state_root=lease,
            signer=signer,
            database=database,
            swarm_id=binding["swarm_id"],
            seed_node_id=binding["seed_node_id"],
            members=members,
            authority_generation=authority_generation,
            pseudonym_salt=pseudonym_salt,
            rotation_status=rotation_status,
            rotation_observed_at=rotation_observed_at,
        )
    except LiveSeedStateError:
        if lease is not None:
            lease.close()
        raise
    except (OSError, ValueError) as exc:
        if lease is not None:
            lease.close()
        raise LiveSeedStateError("live_seed_state_invalid") from exc


class LiveRoute(Protocol):
    def open(self) -> RouteIdentity: ...

    def infer(
        self,
        token_ids: Sequence[int],
        *,
        max_new_tokens: int,
        request_id: str,
        sink: TokenSink,
        cancel_requested: Callable[[], bool] | None = None,
        selected_placement_ids: Sequence[str] | None = None,
        route_identity: Mapping[str, Any] | None = None,
        locked_path_manifest: Mapping[str, Any] | None = None,
        command_identity: Mapping[str, Any] | None = None,
        authorize_cleanup: Callable[[float], float] | None = None,
    ) -> InferenceResult: ...

    def release_request(self, request_id: str) -> None: ...

    def counters(self) -> RouteCounters: ...

    def public_status(self) -> Mapping[str, Any]: ...

    def is_alive(self) -> bool: ...

    def close(self) -> None: ...

    def cleanup(self) -> None: ...


class FakeLiveRoute:
    """Deterministic test double. Never valid as a product path."""

    is_simulated = True

    def __init__(self, *, scripted_tokens: Sequence[int]) -> None:
        self._scripted = tuple(scripted_tokens)
        self._open = False
        self._closed = False
        self._frames_sent = 0
        self._frames_received = 0
        self._operations = 0
        self._recent_inferences: deque[dict[str, Any]] = deque(maxlen=64)
        self._incidents: deque[dict[str, Any]] = deque(maxlen=64)
        self._placement_projection: dict[str, Any] | None = None
        self._topology_projection: dict[str, Any] | None = None
        self._model_operation: dict[str, Any] | None = None
        self._m16_runtime_source: Callable[[], Mapping[str, Any] | None] | None = None

    def open(self) -> RouteIdentity:
        if self._closed:
            raise RuntimeError("route_closed")
        self._open = True
        return RouteIdentity(
            deployment_id="12345678-1234-5678-9234-abcdefabcdef",
            model_id="microsoft/DialoGPT-small",
            resolved_commit="49c537161a457d5256512f9d2d38a87d81ae0f0e",
            endpoint_ids=("fake-endpoint-0", "fake-endpoint-1"),
        )

    def infer(
        self,
        token_ids: Sequence[int],
        *,
        max_new_tokens: int,
        request_id: str,
        sink: TokenSink,
        cancel_requested: Callable[[], bool] | None = None,
        selected_placement_ids: Sequence[str] | None = None,
        route_identity: Mapping[str, Any] | None = None,
        locked_path_manifest: Mapping[str, Any] | None = None,
        command_identity: Mapping[str, Any] | None = None,
        authorize_cleanup: Callable[[float], float] | None = None,
    ) -> InferenceResult:
        if not self.is_alive():
            raise RuntimeError("route_not_open")
        before = self.counters()
        started_at = time.monotonic()
        emitted = self._scripted[:max_new_tokens]
        for index, token_id in enumerate(emitted):
            if cancel_requested is not None and cancel_requested():
                raise InferenceCancelled("inference_cancelled")
            self._frames_sent += 1
            self._frames_received += 1
            self._operations += 1
            sink.emit(index, token_id)
        completed_at = time.monotonic()
        after = self.counters()
        self._recent_inferences.append(
            {
                "context_tokens": len(token_ids),
                "output_tokens": len(emitted),
                "prefill_ms": 0.0,
                "ttft_ms": 0.0 if emitted else None,
                "tpot_ms": 0.0 if len(emitted) > 1 else None,
                "total_ms": (completed_at - started_at) * 1_000.0,
                "peer_counter_deltas": [
                    {
                        "node_id": "fake-node",
                        "frames_sent": after.frames_sent - before.frames_sent,
                        "frames_received": after.frames_received
                        - before.frames_received,
                        "applied_operation_count": (
                            after.applied_operation_count
                            - before.applied_operation_count
                        ),
                    }
                ],
            }
        )
        return InferenceResult(request_id=request_id, token_ids=emitted)

    def release_request(self, request_id: str) -> None:
        return

    def counters(self) -> RouteCounters:
        return RouteCounters(
            frames_sent=self._frames_sent,
            frames_received=self._frames_received,
            applied_operation_count=self._operations,
        )

    def public_status(self) -> Mapping[str, Any]:
        return {
            "protocol": "mycelium.live_route_status.v1",
            "route_alive": self.is_alive(),
            "simulated": True,
            "route_identity_digest": None,
            "deployment_id": "12345678-1234-5678-9234-abcdefabcdef",
            "model_id": "microsoft/DialoGPT-small",
            "topology_version": 1,
            "decode_mode": "complete_context_replay",
            "counters": {
                "frames_sent": self._frames_sent,
                "frames_received": self._frames_received,
                "applied_operation_count": self._operations,
                "fatal": None,
            },
            "stages": [],
            "peers": [],
            "recent_inferences": list(self._recent_inferences),
            "incidents": list(self._incidents),
        }

    def membership_status(self, *, qualification: Any | None) -> Mapping[str, Any]:
        state = "qualified" if qualification is not None else "assigned"
        return {
            "protocol": "mycelium.product_ui.swarm.v1",
            "native_nodes": [
                {
                    "member_id": node_id,
                    "capability": "native_inference_node",
                    "membership_state": state,
                    "connectivity": "unknown",
                    "endpoint_id": None,
                }
                for node_id in ("node-a", "node-b")
            ],
            "browser_workers": [],
        }

    def product_membership_records(self) -> tuple[dict[str, Any], ...]:
        now = time.time()
        return tuple(
            {
                "node_id": node_id,
                "peer_class": "mac_mlx_iroh",
                "runtime_capability": {
                    "runtime_backend": "mlx",
                    "transport": "iroh",
                    "activation_protocol": "mycelium.router_wire.v1",
                },
                "activation_eligible": True,
                "generation": 1,
                "incarnation": "fixture-incarnation",
                "lease_expires_at": now + 300.0,
                "last_liveness_at": now,
                "lifecycle_state": "RUNNING",
            }
            for node_id in ("node-a", "node-b")
        )

    @staticmethod
    def product_pseudonym_salt() -> bytes:
        return hashlib.sha256(b"mycelium-fake-route-product-salt").digest()

    def is_alive(self) -> bool:
        return self._open and not self._closed

    def close(self) -> None:
        self._open = False
        self._closed = True

    def cleanup(self) -> None:
        return


def _refresh_membership_snapshot(
    snapshot: Mapping[str, Any],
    *,
    now: float,
    signer: Ed25519EvidenceSigner,
    seed_node_id: str,
    members: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Reissue assignment offers from current coordinator membership facts."""

    offers = snapshot.get("assignment_offers")
    if not isinstance(offers, list) or not offers:
        raise ValueError("membership_snapshot_invalid")
    member_by_node: dict[str, Mapping[str, Any]] = {}
    for member in members:
        node_id = member.get("node_id") if isinstance(member, Mapping) else None
        if not isinstance(node_id, str) or not node_id or node_id in member_by_node:
            raise ValueError("membership_member_invalid")
        member_by_node[node_id] = member
    recipients = []
    activation_endpoint_by_node: dict[str, str] = {}
    for envelope in offers:
        message = envelope.get("message") if isinstance(envelope, Mapping) else None
        recipient = (
            message.get("recipient_node_id") if isinstance(message, Mapping) else None
        )
        if not isinstance(recipient, str) or not recipient or recipient in recipients:
            raise ValueError("membership_snapshot_invalid")
        recipients.append(recipient)
        records = message.get("peer_endpoint_records")
        if not isinstance(records, list):
            raise ValueError("membership_snapshot_invalid")
        for record in records:
            if not isinstance(record, Mapping):
                raise ValueError("membership_snapshot_invalid")
            node_id = record.get("node_id")
            endpoint_id = record.get("endpoint_id")
            if (
                not isinstance(node_id, str)
                or not node_id
                or not isinstance(endpoint_id, str)
                or not endpoint_id
            ):
                raise ValueError("membership_activation_endpoint_invalid")
            prior = activation_endpoint_by_node.setdefault(node_id, endpoint_id)
            if prior != endpoint_id:
                raise ValueError("membership_activation_endpoint_conflict")
    route_members: dict[str, Mapping[str, Any]] = {}
    for recipient in recipients:
        member = member_by_node.get(recipient)
        if member is None:
            raise ValueError("membership_member_missing")
        try:
            generation = member["generation"]
            lease_expires_at = float(member["lease_expires_at"])
            endpoint_id = member["endpoint_id"]
            capable = peer_runtime_is_activation_eligible(
                member["peer_class"],
                member["runtime_capability"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("membership_member_invalid") from exc
        if (
            not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation < 1
            or not isinstance(endpoint_id, str)
            or not endpoint_id
        ):
            raise ValueError("membership_member_invalid")
        if lease_expires_at <= now:
            raise ValueError("membership_member_lease_expired")
        if not capable:
            raise ValueError("membership_member_activation_ineligible")
        route_members[recipient] = member
    if len(route_members) > 1 and set(activation_endpoint_by_node) != set(route_members):
        raise ValueError("membership_activation_endpoint_missing")
    valid_until = min(
        now + 3_600.0,
        *(float(member["lease_expires_at"]) for member in route_members.values()),
    )
    refreshed_offers = []
    for index, envelope in enumerate(offers):
        if not isinstance(envelope, Mapping) or not isinstance(
            envelope.get("message"), Mapping
        ):
            raise ValueError("membership_snapshot_invalid")
        message = json.loads(json.dumps(envelope["message"]))
        recipient = message["recipient_node_id"]
        member = route_members[recipient]
        message["message_id"] = f"live-route-offer-{index}-{int(now * 1_000)}"
        message["sender_node_id"] = seed_node_id
        message["sender_endpoint_id"] = signer.endpoint_id
        message["incarnation"] = f"live-seed-{int(now * 1_000)}"
        message["generation"] = member["generation"]
        message["issued_at"] = now
        message["expires_at"] = valid_until
        message["peer_endpoint_records"] = [
            {
                "node_id": node_id,
                # Membership endpoint identity authenticates the current member and
                # generation. It is not the planner-authorized activation-plane Iroh
                # identity. Configure separately proves possession of that key.
                "endpoint_id": activation_endpoint_by_node[node_id],
                "deployment_epoch": message["deployment_epoch"],
                "membership_generation": other["generation"],
                "valid_from": now,
                "valid_until": valid_until,
            }
            for node_id, other in sorted(route_members.items())
            if node_id != recipient
        ]
        refreshed_offers.append(sign_membership_message(signer=signer, message=message))
    return {
        **dict(snapshot),
        "seed_key_digest": signer.verification_key_digest,
        "assignment_offers": refreshed_offers,
    }


class PhysicalLiveRoute:
    """Persistent multi-host route composed from the physical node protocol."""

    is_simulated = False

    def __init__(
        self,
        *,
        controller: QualificationController,
        endpoints: Mapping[str, Mapping[str, Any]],
        run_plan: Mapping[str, Any],
        session_factory=_ConcurrentNodeSession,
        seed_authority: LiveSeedAuthority | None = None,
        membership_snapshot: Mapping[str, Any] | None = None,
    ) -> None:
        self._controller = controller
        self._peers = {peer.node_id: peer for peer in controller.peers}
        self._endpoints = {
            node_id: dict(endpoint) for node_id, endpoint in endpoints.items()
        }
        self._plan = dict(run_plan)
        self._plans_by_node = {
            item["node_id"]: dict(item) for item in self._plan["nodes"]
        }
        self._graph = execution_graph_from_document(
            self._plan["nodes"][0]["configure"]["graph"]
        )
        self._session_factory = session_factory
        self._seed_authority = seed_authority
        self._membership_snapshot = (
            None
            if membership_snapshot is None
            else json.loads(json.dumps(dict(membership_snapshot)))
        )
        self._sessions: dict[str, Any] = {}
        self._identities: dict[str, dict[str, Any]] = {}
        self._verification_keys: dict[str, dict[str, Any]] = {}
        self._endpoint_addresses: dict[str, dict[str, Any]] = {}
        self._signed_observations: list[dict[str, Any]] = []
        self._last_snapshots: dict[str, dict[str, Any]] = {}
        self._last_health: dict[str, dict[str, Any]] = {}
        self._request_inputs: dict[str, tuple[int, ...]] = {}
        self._request_outputs: dict[str, tuple[int, ...]] = {}
        self._request_limits: dict[str, int] = {}
        self._request_entry_nodes: dict[str, str] = {}
        self._recent_inferences: deque[dict[str, Any]] = deque(maxlen=64)
        self._incidents: deque[dict[str, Any]] = deque(maxlen=64)
        self._incident_sequence = 0
        self._stop_token_ids: frozenset[int] = frozenset()
        self._placement_projection: dict[str, Any] | None = None
        self._topology_projection: dict[str, Any] | None = None
        self._workload_comparison: dict[str, Any] | None = None
        self._model_operation: dict[str, Any] | None = None
        self._deployment_qualification: Any | None = None
        self._deployment_quantization = "int8-weight-only"
        self._command_sequence = 0
        self._open = False
        self._closed = False
        self._fatal: str | None = None
        self._m16_runtime_source: Callable[[], Mapping[str, Any] | None] | None = None
        self._lock = threading.RLock()
        self._request_locks: dict[str, threading.RLock] = {}
        self._active_route_requests: dict[str, dict[str, Any]] = {}
        self._pending_publisher_generations: dict[str, tuple[int, int, dict[str, Any]]] = {}
        self._request_cleanup_receipts: dict[str, dict[str, Any]] = {}
        self._scoped_runtime_incidents: deque[dict[str, Any]] = deque(maxlen=64)
        self._scoped_runtime_incident_sequence = 0
        self._a4_qualification: dict[str, Any] | None = None
        self._liveness = TrafficAwareLivenessDetector()
        self._liveness_subjects: dict[str, LivenessSubject] = {}
        self._liveness_edge_subjects: dict[str, LivenessSubject] = {}
        self._last_transport_event_sequence: dict[str, int] = {}
        self._liveness_monitor_stop = threading.Event()
        self._liveness_monitor_thread: threading.Thread | None = None

    def _record_incident(
        self,
        *,
        state: str,
        reason: str,
        request_id: str | None,
    ) -> None:
        with self._lock:
            last = self._incidents[-1] if self._incidents else None
            if (
                last is not None
                and last["state"] == state
                and last["reason"] == reason
                and last["request_id"] == request_id
            ):
                return
            self._incident_sequence += 1
            self._incidents.append(
                {
                    "protocol": "mycelium.live_route_incident.v1",
                    "incident_id": f"route-incident-{self._incident_sequence}",
                    "deployment_id": self._plan["deployment_id"],
                    "request_id": request_id,
                    "state": state,
                    "reason": reason[:128],
                    "observed_at_unix_ms": int(time.time() * 1_000),
                }
            )

    def _record_scoped_runtime_incident(
        self,
        *,
        request_id: str,
        subject_id: str,
        scope: str,
        reason: str,
        fatal_requested: bool = False,
        fatal_accepted: bool = False,
    ) -> None:
        """Record one privacy-reduced incident against the exact active identity."""

        from mycelium_live.a4_contracts import validate_scoped_runtime_incident

        with self._lock:
            active = self._active_route_requests.get(request_id)
            if active is None:
                return
            self._scoped_runtime_incident_sequence += 1
            document = validate_scoped_runtime_incident(
                {
                    "protocol": "mycelium.scoped_runtime_incident.v1",
                    "incident_id": (
                        f"scoped-runtime-{self._scoped_runtime_incident_sequence}"
                    ),
                    "deployment_id": active["deployment_id"],
                    "deployment_epoch": active["deployment_epoch"],
                    "qualification_digest": active["qualification_digest"],
                    "request_id": request_id,
                    "request_attempt": active["request_attempt"],
                    "path_id": active["path_id"],
                    "path_attempt": active["path_attempt"],
                    "path_digest": active["path_digest"],
                    "topology_generation": active["topology_generation"],
                    "command_id": active["command_id"],
                    "cancellation_generation": active[
                        "cancellation_generation"
                    ],
                    "publisher_generation": active["publisher_generation"],
                    "cleanup_owner_id": (
                        f"physical-live-route:{active['deployment_id']}"
                    ),
                    "subject_id": subject_id,
                    "scope": scope,
                    "reason": reason[:128],
                    "fatal_requested": fatal_requested,
                    "fatal_accepted": fatal_accepted,
                    "observed_at_monotonic_ms": int(time.monotonic() * 1_000),
                }
            )
            self._scoped_runtime_incidents.append(document)

    def a4_scoped_runtime_incidents(self) -> tuple[Mapping[str, Any], ...]:
        """Return detached, bounded A4 incident evidence without prompt material."""

        with self._lock:
            return tuple(
                json.loads(json.dumps(item))
                for item in self._scoped_runtime_incidents
            )

    def _record_active_runtime_failure(
        self,
        *,
        request_id: str,
        node_id: str,
        reason: str,
        observed_at_monotonic_s: float,
    ) -> None:
        """Route a verified command failure through scoped liveness, never fatal."""

        subject = self._liveness_subjects.get(node_id)
        if subject is None:
            return
        observed_at_ms = int(observed_at_monotonic_s * 1_000)
        transport_failure = reason in {
            "ack_failed",
            "delivery_deadline_exceeded",
            "delivery_not_confirmed",
            "delivery_cancelled",
            "path_cancelled",
            "peer_rotated",
            "sidecar_queue_full",
            "transport_not_running",
        } or reason.startswith(("sidecar_", "transport_"))
        if transport_failure:
            self._liveness.record_active_failure(
                subject,
                failure_started_at_ms=observed_at_ms,
                observed_at_ms=observed_at_ms,
                scope="request",
                affected_track_ids=(request_id,),
                verified=True,
            )
        else:
            self._liveness.record_worker_exception(
                subject,
                request_id=request_id,
                observed_at_ms=observed_at_ms,
            )
        self._record_scoped_runtime_incident(
            request_id=request_id,
            subject_id=node_id,
            scope="request",
            reason=reason,
        )

    @classmethod
    def from_operator_plan(
        cls,
        plan_path: Path,
        *,
        seed_state_root: Path,
    ) -> "PhysicalLiveRoute":
        document = json.loads(Path(plan_path).read_text(encoding="utf-8"))
        controller_document = document["controller"]
        authority = _load_live_seed_authority(
            seed_state_root=seed_state_root,
            plan_membership_snapshot=controller_document["membership_snapshot"],
        )
        now = time.time()
        try:
            membership_snapshot = _refresh_membership_snapshot(
                controller_document["membership_snapshot"],
                now=now,
                signer=authority.signer,
                seed_node_id=authority.seed_node_id,
                members=authority.members,
            )
            peers = tuple(PeerIdentity(**item) for item in controller_document["peers"])
            controller = QualificationController(
                mode=controller_document["mode"],
                peers=peers,
                source_root=Path(controller_document["source_root"]),
                transfer_manifest=controller_document["transfer_manifest"],
                node_transfer_manifests=controller_document.get(
                    "node_transfer_manifests"
                ),
                membership_snapshot=membership_snapshot,
                now=now,
                run_plan=controller_document["run_plan"],
            )
            controller._validate_physical_distinctness()
            endpoints = controller._validate_membership()
            run_plan = controller._validate_run_plan()
            return cls(
                controller=controller,
                endpoints=endpoints,
                run_plan=run_plan,
                seed_authority=authority,
                membership_snapshot=membership_snapshot,
            )
        except BaseException:
            authority.close()
            raise

    def _command_id(self, node_id: str, operation: str) -> str:
        with self._lock:
            self._command_sequence += 1
            return f"{node_id}-{operation}-{self._command_sequence}"

    def _send_request_command(
        self,
        *,
        request_id: str,
        node_id: str,
        command_id: str,
        command: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        with self._lock:
            active = self._active_route_requests.get(request_id)
            if active is None or active.get("terminal") is True:
                raise RuntimeError("request_control_identity_unavailable")
            inflight = active.setdefault("inflight_commands", {})
            if node_id in inflight:
                raise RuntimeError("request_node_command_already_inflight")
            inflight[node_id] = command_id
        try:
            return self._sessions[node_id].send(
                command_id=command_id,
                command=command,
                payload=payload,
            )
        finally:
            with self._lock:
                active = self._active_route_requests.get(request_id)
                if active is not None:
                    inflight = active.get("inflight_commands")
                    if (
                        isinstance(inflight, dict)
                        and inflight.get(node_id) == command_id
                    ):
                        inflight.pop(node_id, None)

    def _request_execution_lock(self, request_id: str) -> threading.RLock:
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("invalid_inference_request")
        with self._lock:
            locks = getattr(self, "_request_locks", None)
            if locks is None:
                locks = {}
                self._request_locks = locks
            return locks.setdefault(request_id, threading.RLock())

    @property
    def execution_graph(self) -> ExecutionGraph:
        """Return the graph bound into every configured process."""
        return self._graph

    def set_stop_token_ids(self, token_ids: Sequence[int]) -> None:
        """Stop future generations before emitting a model terminator token."""

        if any(
            not isinstance(token_id, int) or isinstance(token_id, bool) or token_id < 0
            for token_id in token_ids
        ):
            raise ValueError("invalid_stop_token_ids")
        self._stop_token_ids = frozenset(token_ids)

    def set_public_projections(
        self,
        *,
        placement: Mapping[str, Any] | None,
        topology: Mapping[str, Any] | None,
        workload_comparison: Mapping[str, Any] | None = None,
    ) -> None:
        """Attach validated deployment projections to single-route status."""

        with self._lock:
            if self._closed:
                raise RuntimeError("route_closed")
            self._placement_projection = (
                None if placement is None else json.loads(json.dumps(dict(placement)))
            )
            self._topology_projection = (
                None if topology is None else json.loads(json.dumps(dict(topology)))
            )
            self._workload_comparison = (
                None
                if workload_comparison is None
                else json.loads(json.dumps(dict(workload_comparison)))
            )

    def m15_plan_comparison(self) -> Mapping[str, Any] | None:
        """Return detached workload-planner intent, never route authority."""

        with self._lock:
            comparison = getattr(self, "_workload_comparison", None)
            return None if comparison is None else json.loads(json.dumps(comparison))

    def set_m17_model_operation(self, document: Mapping[str, Any] | None) -> None:
        """Attach the privacy-reduced catalog/feasibility projection."""

        with self._lock:
            if self._closed:
                raise RuntimeError("route_closed")
            self._model_operation = (
                None if document is None else json.loads(json.dumps(dict(document)))
            )

    def set_deployment_qualification(
        self,
        qualification: Any,
        *,
        quantization: str = "int8-weight-only",
    ) -> None:
        """Expose one qualified deployment through the same read model as a registry."""

        if qualification.deployment_id != self._graph.deployment_id:
            raise ValueError("qualification_deployment_mismatch")
        if qualification.model_id != self._graph.model_id:
            raise ValueError("qualification_model_mismatch")
        if not isinstance(quantization, str) or not quantization:
            raise ValueError("qualification_quantization_invalid")
        with self._lock:
            if self._closed:
                raise RuntimeError("route_closed")
            self._deployment_qualification = qualification
            self._deployment_quantization = quantization

    def registry_status(self) -> Mapping[str, Any]:
        """Describe a single-route server without pretending another model is selectable."""

        with self._lock:
            qualification = self._deployment_qualification
            if qualification is None:
                raise RuntimeError("deployment_qualification_unavailable")
            qualified = self.is_alive() and qualification.route_ready is True
            return {
                "protocol": "mycelium.live_deployment_registry.v1",
                "selected_deployment_id": self._graph.deployment_id,
                "switching_allowed": False,
                "deployments": [
                    {
                        "deployment_id": self._graph.deployment_id,
                        "model_id": self._graph.model_id,
                        "model_revision": self._graph.resolved_commit,
                        "quantization": self._deployment_quantization,
                        "topology_size": len(self._graph.stages),
                        "health": "qualified" if qualified else "unavailable",
                        "qualified_at_unix_ms": qualification.issued_at_unix_ms,
                        "qualification_id": qualification.qualification_id,
                    }
                ],
            }

    def m17_model_operation(self) -> Mapping[str, Any] | None:
        with self._lock:
            document = getattr(self, "_model_operation", None)
            if document is None:
                return None
            if self._deployment_qualification is None:
                return json.loads(json.dumps(document))
            from mycelium_model_catalog import enrich_model_operation_lifecycle

            return enrich_model_operation_lifecycle(document, self.registry_status())

    def set_m18_replica_plan(self, document: Mapping[str, Any] | None) -> None:
        """Attach validated Planner-owned M18 replica intent."""

        from mycelium_m18_replication import validate_replica_plan

        with self._lock:
            if self._closed:
                raise RuntimeError("route_closed")
            self._replica_plan = (
                None if document is None else validate_replica_plan(document)
            )

    def m18_replica_plan(self) -> Mapping[str, Any] | None:
        with self._lock:
            document = getattr(self, "_replica_plan", None)
            return None if document is None else json.loads(json.dumps(document))

    def set_m18_replica_runtime_source(
        self, source: Callable[[], Mapping[str, Any] | None]
    ) -> None:
        if not callable(source):
            raise ValueError("m18_replica_runtime_source_invalid")
        with self._lock:
            self._m18_replica_runtime_source = source

    def m18_replica_runtime(self) -> Mapping[str, Any] | None:
        from mycelium_m18_replication import validate_replica_runtime

        source = getattr(self, "_m18_replica_runtime_source", None)
        document = None if source is None else source()
        return None if document is None else validate_replica_runtime(document)

    def set_m19_recovery_evidence(
        self,
        *,
        liveness: Mapping[str, Any] | None,
        plan: Mapping[str, Any] | None,
        runtime: Mapping[str, Any] | None,
    ) -> None:
        """Attach privacy-reduced M19 evidence; none of it grants route authority."""

        from mycelium_m19_recovery import (
            validate_liveness,
            validate_recovery_plan,
            validate_recovery_runtime,
        )

        with self._lock:
            if self._closed:
                raise RuntimeError("route_closed")
            self._m19_liveness = None if liveness is None else validate_liveness(liveness)
            self._m19_recovery_plan = None if plan is None else validate_recovery_plan(plan)
            self._m19_recovery_runtime = (
                None if runtime is None else validate_recovery_runtime(runtime)
            )

    def m19_liveness(self) -> Mapping[str, Any] | None:
        with self._lock:
            document = getattr(self, "_m19_liveness", None)
            return None if document is None else json.loads(json.dumps(document))

    def m19_recovery_plan(self) -> Mapping[str, Any] | None:
        with self._lock:
            document = getattr(self, "_m19_recovery_plan", None)
            return None if document is None else json.loads(json.dumps(document))

    def m19_recovery_runtime(self) -> Mapping[str, Any] | None:
        with self._lock:
            document = getattr(self, "_m19_recovery_runtime", None)
            return None if document is None else json.loads(json.dumps(document))

    def set_m20_speculative_evidence(
        self,
        *,
        plan: Mapping[str, Any] | None,
        runtime: Mapping[str, Any] | None,
    ) -> None:
        """Attach M20 evidence without changing target-only route authority."""

        from mycelium_m20_speculation import (
            validate_speculative_plan,
            validate_speculative_runtime,
        )

        with self._lock:
            if self._closed:
                raise RuntimeError("route_closed")
            self._m20_speculative_plan = (
                None if plan is None else validate_speculative_plan(plan)
            )
            self._m20_speculative_runtime = (
                None if runtime is None else validate_speculative_runtime(runtime)
            )

    def m20_speculative_plan(self) -> Mapping[str, Any] | None:
        with self._lock:
            document = getattr(self, "_m20_speculative_plan", None)
            return None if document is None else json.loads(json.dumps(document))

    def m20_speculative_runtime(self) -> Mapping[str, Any] | None:
        with self._lock:
            document = getattr(self, "_m20_speculative_runtime", None)
            return None if document is None else json.loads(json.dumps(document))

    def set_m21_heterogeneous_evidence(
        self, document: Mapping[str, Any] | None
    ) -> None:
        """Attach privacy-reduced M21 membership and physical-route evidence."""

        from mycelium_m21_heterogeneous import validate_heterogeneous_evidence

        with self._lock:
            if self._closed:
                raise RuntimeError("route_closed")
            self._m21_heterogeneous = (
                None
                if document is None
                else validate_heterogeneous_evidence(document)
            )

    def m21_heterogeneous(self) -> Mapping[str, Any] | None:
        with self._lock:
            document = getattr(self, "_m21_heterogeneous", None)
            return None if document is None else json.loads(json.dumps(document))

    def set_m22_release_evidence(self, document: Mapping[str, Any] | None) -> None:
        """Attach the privacy-reduced M22 release-closure projection."""

        from mycelium_m22_release import validate_release_evidence

        with self._lock:
            if self._closed:
                raise RuntimeError("route_closed")
            self._m22_release = (
                None if document is None else validate_release_evidence(document)
            )

    def m22_release(self) -> Mapping[str, Any] | None:
        with self._lock:
            document = getattr(self, "_m22_release", None)
            return None if document is None else json.loads(json.dumps(document))

    def set_m23_kv_evidence(self, document: Mapping[str, Any] | None) -> None:
        """Attach the sealed privacy-reduced M23 heterogeneous KV gate."""

        from mycelium_m23_kv import validate_m23_kv_evidence

        with self._lock:
            if self._closed:
                raise RuntimeError("route_closed")
            self._m23_kv = (
                None if document is None else validate_m23_kv_evidence(document)
            )

    def m23_kv(self) -> Mapping[str, Any] | None:
        with self._lock:
            document = getattr(self, "_m23_kv", None)
            return None if document is None else json.loads(json.dumps(document))

    def m17_swarm_evidence(self) -> Mapping[str, Any]:
        """Capture one fresh set of independently signed node resource observations."""

        with self._lock:
            if self._closed or not self._open or not self.is_alive():
                raise RuntimeError("swarm_evidence_unavailable")
            before = len(self._signed_observations)
            self._snapshot_all()
            captured = self._signed_observations[before:]
            by_node: dict[str, dict[str, Any]] = {}
            for signed in captured:
                observation = signed.get("observation")
                if (
                    isinstance(observation, Mapping)
                    and observation.get("event") == "snapshot"
                    and isinstance(observation.get("node_id"), str)
                ):
                    by_node[str(observation["node_id"])] = signed
            if set(by_node) != set(self._sessions):
                raise RuntimeError("swarm_evidence_incomplete")
            return {
                "protocol": "mycelium.live_swarm_resource_observations.v1",
                "captured_at_unix_ms": int(time.time() * 1_000),
                "deployment_id": self._graph.deployment_id,
                "model_id": self._graph.model_id,
                "resolved_commit": self._graph.resolved_commit,
                "placement": json.loads(json.dumps(self._placement_projection)),
                "topology": json.loads(json.dumps(self._topology_projection)),
                "signed_snapshots": [by_node[node_id] for node_id in sorted(by_node)],
                "route_ready": False,
            }

    def set_m16_runtime_source(
        self,
        source: Callable[[], Mapping[str, Any] | None],
    ) -> None:
        if not callable(source):
            raise ValueError("m16_runtime_source_invalid")
        with self._lock:
            self._m16_runtime_source = source

    def m16_runtime_status(self) -> Mapping[str, Any] | None:
        # The coordinator owns its own lock. Reading the immutable source
        # reference must remain available while physical inference holds the
        # route lock, otherwise queue/admission UI would disappear precisely
        # while it is most useful.
        source = self._m16_runtime_source
        document = None if source is None else source()
        return None if document is None else json.loads(json.dumps(document))

    @property
    def startup_challenge(self) -> tuple[tuple[int, ...], tuple[int, ...]]:
        """Return the prompt/output pair authenticated by the controller plan."""

        return (
            tuple(self._plan["request"]["prompt_token_ids"]),
            tuple(self._plan["expected_token_ids"]),
        )

    def _node_command(self, node_id: str) -> tuple[str, ...]:
        peer = self._peers[node_id]
        node_plan = self._plans_by_node[node_id]
        node_script = f"{peer.staging_root}/physical_inference_node.py"
        command = (
            node_plan["python_executable"],
            "-B",
            node_script,
            "--run-id",
            self._plan["run_id"],
            "--deployment-id",
            self._plan["deployment_id"],
            "--node-id",
            node_id,
            "--artifact-root",
            peer.staging_root,
            "--socket-root",
            node_plan["socket_root"],
            "--sidecar-binary",
            node_plan["sidecar_binary"],
            "--endpoint-secret-file",
            node_plan["endpoint_secret_file"],
            "--command-timeout",
            str(int(NODE_COMMAND_TIMEOUT_SECONDS)),
        )
        decode_mode = self._plan.get("decode_mode")
        if decode_mode is not None:
            command += ("--decode-mode", decode_mode)
        return _peer_process_argv(peer, command)

    def _verify_observation(
        self,
        node_id: str,
        response: Mapping[str, Any],
        *,
        event: str,
        require_known_key: bool = True,
    ) -> dict[str, Any]:
        observation = self._controller._verified_observation(
            response,
            event=event,
            peer=self._peers[node_id],
            process_id=self._identities[node_id]["process_id"],
            run_id=self._plan["run_id"],
            deployment_id=self._plan["deployment_id"],
            endpoint_id=self._endpoints[node_id]["endpoint_id"],
            expected_verification_key=(
                self._verification_keys.get(node_id) if require_known_key else None
            ),
            signed_observation_sink=self._signed_observations,
        )
        subject = self._liveness_subjects.get(node_id)
        if subject is not None:
            self._liveness.observe_receipt(
                subject,
                observed_at_ms=int(time.monotonic() * 1_000),
                source=ObservationSource.APPLICATION_RECEIPT,
                signed=True,
            )
        return observation

    def open(self) -> RouteIdentity:
        with self._lock:
            if self._closed:
                raise RuntimeError("route_closed")
            if self._open:
                raise RuntimeError("route_already_open")
            try:
                for node_id in sorted(self._peers):
                    session = self._session_factory(
                        argv=self._node_command(node_id),
                        node_id=node_id,
                        run_id=self._plan["run_id"],
                        deployment_id=self._plan["deployment_id"],
                        timeout_seconds=NODE_SESSION_TIMEOUT_SECONDS,
                    )
                    self._sessions[node_id] = session
                    hello = session.send(
                        command_id=self._command_id(node_id, "hello"),
                        command="hello",
                        payload={},
                    )
                    self._identities[node_id] = self._controller._hello_identity(
                        hello,
                        peer=self._peers[node_id],
                        run_id=self._plan["run_id"],
                        deployment_id=self._plan["deployment_id"],
                    )
                    subject = LivenessSubject(
                        subject_id=node_id,
                        kind=SubjectKind.PEER,
                        membership_generation=int(
                            self._endpoints[node_id]["membership_generation"]
                        ),
                    )
                    self._liveness_subjects[node_id] = subject
                    self._liveness.register_subject(
                        subject,
                        observed_at_ms=int(time.monotonic() * 1_000),
                    )

                for node_id in sorted(self._sessions):
                    response = self._sessions[node_id].send(
                        command_id=self._command_id(node_id, "configure"),
                        command="configure",
                        payload=self._plans_by_node[node_id]["configure"],
                    )
                    observation = self._verify_observation(
                        node_id,
                        response,
                        event="configured",
                        require_known_key=False,
                    )
                    endpoint_address = observation["details"].get("endpoint_addr")
                    if (
                        not isinstance(endpoint_address, Mapping)
                        or endpoint_address.get("id")
                        != self._endpoints[node_id]["endpoint_id"]
                    ):
                        raise RuntimeError("node_endpoint_address_invalid")
                    self._endpoint_addresses[node_id] = dict(endpoint_address)
                    self._verification_keys[node_id] = dict(
                        response["result"]["verification_key"]
                    )

                # The run-plan order is the selected physical cycle. Sorting node
                # ids here silently replaced an evidence-driven M14 route with a
                # canonical route whenever the optimizer chose a different cycle.
                ordered = [item["node_id"] for item in self._plan["nodes"]]
                if len(ordered) != len(set(ordered)) or set(ordered) != set(
                    self._sessions
                ):
                    raise RuntimeError("route_plan_node_order_invalid")
                successors = {
                    node_id: ordered[(index + 1) % len(ordered)]
                    for index, node_id in enumerate(ordered)
                }
                for node_id in ordered:
                    remote_node_id = successors[node_id]
                    additional_node_ids = [
                        candidate
                        for candidate in ordered
                        if candidate not in {node_id, remote_node_id}
                    ]

                    def peer_document(candidate: str) -> dict[str, Any]:
                        return {
                            "node_id": candidate,
                            "endpoint_id": self._endpoints[candidate]["endpoint_id"],
                            "endpoint_addr": self._endpoint_addresses[candidate],
                            "generation": self._endpoints[candidate][
                                "membership_generation"
                            ],
                        }

                    response = self._sessions[node_id].send(
                        command_id=self._command_id(node_id, "start"),
                        command="start",
                        payload={
                            "peer": peer_document(remote_node_id),
                            "peers": [
                                peer_document(candidate)
                                for candidate in additional_node_ids
                            ],
                            "local_generation": self._endpoints[node_id][
                                "membership_generation"
                            ],
                        },
                    )
                    self._verify_observation(node_id, response, event="started")
                self._open = True
                self._liveness_monitor_stop.clear()
                self._liveness_monitor_thread = threading.Thread(
                    target=self._active_liveness_monitor_loop,
                    name="mycelium-active-liveness-monitor",
                    daemon=True,
                )
                self._liveness_monitor_thread.start()
            except BaseException as exc:
                self._fatal = getattr(exc, "code", type(exc).__name__)
                self._record_incident(
                    state="route_failed_closed",
                    reason=str(self._fatal),
                    request_id=None,
                )
                self.close()
                raise

            graph = self._graph
            return RouteIdentity(
                deployment_id=graph.deployment_id,
                model_id=graph.model_id,
                resolved_commit=graph.resolved_commit,
                endpoint_ids=tuple(
                    self._endpoints[node_id]["endpoint_id"]
                    for node_id in sorted(self._endpoints)
                ),
            )

    @staticmethod
    def _output_tokens(observation: Mapping[str, Any]) -> tuple[int, ...]:
        output = observation.get("details", {}).get("output")
        if not isinstance(output, Mapping) or not isinstance(
            output.get("token_ids"), list
        ):
            raise RuntimeError("node_inference_output_invalid")
        tokens = tuple(output["token_ids"])
        if not all(
            isinstance(token, int) and not isinstance(token, bool) and token >= 0
            for token in tokens
        ):
            raise RuntimeError("node_inference_output_invalid")
        return tokens

    def _snapshot_all(self) -> None:
        self._snapshot_nodes(frozenset(self._sessions))

    def _snapshot_nodes(
        self,
        node_ids: frozenset[str],
        *,
        deadline_monotonic_s: float | None = None,
    ) -> None:
        snapshots: dict[str, dict[str, Any]] = {}
        if not node_ids or not node_ids <= set(self._sessions):
            raise ValueError("snapshot_nodes_invalid")
        for node_id in sorted(node_ids):
            if deadline_monotonic_s is None:
                response = self._sessions[node_id].send(
                    command_id=self._command_id(node_id, "snapshot"),
                    command="snapshot",
                    payload={},
                )
            else:
                response = self._send_before(
                    node_id,
                    command_id=self._command_id(node_id, "snapshot"),
                    command="snapshot",
                    payload={},
                    deadline_monotonic_s=deadline_monotonic_s,
                )
            observation = self._verify_observation(node_id, response, event="snapshot")
            snapshots[node_id] = observation
            self._ingest_transport_scoped_events(node_id, observation)
        with self._lock:
            self._last_snapshots.update(snapshots)

    def _monitor_active_liveness_once(self) -> None:
        """Observe active data paths and interrupt only their failed tracks."""

        observed_at_s = time.monotonic()
        with self._lock:
            if not self._open or self._closed:
                return
            active_node_ids = frozenset(
                node_id
                for active in self._active_route_requests.values()
                if not active.get("terminal")
                for node_id in active.get("participating_node_ids", ())
            )
        if not active_node_ids:
            return
        def health_active_node(node_id: str) -> bool:
            try:
                response = self._send_before(
                    node_id,
                    command_id=self._command_id(node_id, "health"),
                    command="health",
                    payload={},
                    deadline_monotonic_s=observed_at_s + 0.5,
                )
                observation = self._verify_observation(
                    node_id,
                    response,
                    event="health",
                )
                with self._lock:
                    self._last_health[node_id] = observation
            except BaseException:
                return False
            return True

        with ThreadPoolExecutor(
            max_workers=len(active_node_ids),
            thread_name_prefix="a4-liveness-snapshot",
        ) as executor:
            health_results = tuple(
                executor.map(health_active_node, sorted(active_node_ids))
            )
        if not any(health_results):
            self._handle_lost_peer_processes()
            return
        with self._lock:
            failed_nodes: dict[str, str] = {}
            for node_id in active_node_ids:
                details = self._last_health.get(node_id, {}).get("details")
                sidecar = (
                    details.get("sidecar_process")
                    if isinstance(details, Mapping)
                    else None
                )
                if (
                    isinstance(sidecar, Mapping)
                    and sidecar.get("started") is True
                    and sidecar.get("alive") is False
                ):
                    failed_nodes[node_id] = "sidecar_process_exited"
                    continue
                fatal = (
                    details.get("transport_fatal_error")
                    if isinstance(details, Mapping)
                    else None
                )
                if isinstance(fatal, Mapping):
                    code = fatal.get("code")
                    failed_nodes[node_id] = (
                        code if isinstance(code, str) and code else "transport_failed"
                    )
            affected_by_node = {
                node_id: tuple(
                    sorted(
                        request_id
                        for request_id, active in self._active_route_requests.items()
                        if not active.get("terminal")
                        and not active.get("cancellation_requested")
                        and node_id in active.get("participating_node_ids", ())
                    )
                )
                for node_id in failed_nodes
            }
        for node_id, reason in sorted(failed_nodes.items()):
            affected_tracks = affected_by_node[node_id]
            if not affected_tracks:
                continue
            subject = self._liveness_subjects.get(node_id)
            if subject is not None:
                failure_started_at_ms = int(observed_at_s * 1_000)
                subject_snapshot = self._liveness.subject_snapshot(subject)
                observed_at_ms = max(
                    int(time.monotonic() * 1_000),
                    (
                        subject_snapshot.last_observed_ms + 1
                        if subject_snapshot is not None
                        else failure_started_at_ms
                    ),
                )
                try:
                    self._liveness.record_active_failure(
                        subject,
                        failure_started_at_ms=failure_started_at_ms,
                        observed_at_ms=observed_at_ms,
                        scope="request",
                        affected_track_ids=affected_tracks,
                        verified=True,
                    )
                except BaseException:
                    # Evidence projection must never suppress bounded safety work.
                    pass
            interruption_deadline = observed_at_s + 2.0
            for request_id in affected_tracks:
                try:
                    self._record_scoped_runtime_incident(
                        request_id=request_id,
                        subject_id=node_id,
                        scope="request",
                        reason=reason,
                    )
                except BaseException:
                    # Preserve cancellation even if incident validation rejects.
                    pass
                try:
                    self.cancel_request(
                        request_id,
                        deadline_monotonic_s=interruption_deadline,
                    )
                except BaseException:
                    # Inference owner still performs the same deadline-bound
                    # cleanup path. Keep this monitor available for other peers.
                    pass

    def _active_liveness_monitor_loop(self) -> None:
        while not self._liveness_monitor_stop.wait(0.05):
            try:
                self._monitor_active_liveness_once()
                self._monitor_idle_keepalives_once()
            except BaseException:
                if self._liveness_monitor_stop.is_set():
                    return
                self._handle_lost_peer_processes()

    def _monotonic_ms(self) -> int:
        """Injectable monotonic clock (milliseconds) for liveness observation."""

        return int(time.monotonic() * 1_000)

    def _monitor_idle_keepalives_once(self) -> None:
        """Probe each peer whose keepalive is due; a silent peer accrues misses.

        The detector's traffic-aware design expects an explicit idle probe:
        a verified response is a fresh application receipt (it extends the
        keepalive due horizon), while a probe that times out or fails is one
        recorded keepalive miss.  Suspect after the first miss, quarantine
        after QUARANTINE_MISSES with QUARANTINE_STALE_MS elapsed.  This runs
        even when no requests are active, which is what makes an idle-stalled
        participating peer observable at all.
        """

        observed_at_ms = self._monotonic_ms()
        with self._lock:
            if not self._open or self._closed:
                return
            subjects = tuple(sorted(self._liveness_subjects.items()))
        for node_id, subject in subjects:
            decision = self._liveness.keepalive_due(
                subject,
                observed_at_ms=observed_at_ms,
            )
            if not decision.accepted or not decision.due:
                continue
            if self._idle_probe_peer(node_id):
                continue
            self._liveness.record_keepalive_miss(
                subject,
                observed_at_ms=self._monotonic_ms(),
            )

    def _idle_probe_peer(self, node_id: str) -> bool:
        """Send one bounded health probe; True when the peer answered fresh.

        A verified response refreshes the subject via the ordinary receipt
        path in _verify_observation.  Any failure (deadline, rejection, lost
        process) counts as a silent peer for this keepalive window.
        """

        try:
            response = self._send_before(
                node_id,
                command_id=self._command_id(node_id, "keepalive"),
                command="health",
                payload={},
                deadline_monotonic_s=(
                    time.monotonic() + _IDLE_KEEPALIVE_PROBE_DEADLINE_SECONDS
                ),
            )
            observation = self._verify_observation(
                node_id,
                response,
                event="health",
            )
            with self._lock:
                self._last_health[node_id] = observation
            return True
        except BaseException:
            return False

    def _peer_subject_is_quarantined(self, node_id: str) -> bool:
        subjects = getattr(self, "_liveness_subjects", None)
        subject = subjects.get(node_id) if isinstance(subjects, dict) else None
        if subject is None:
            return False
        snapshot = self._liveness.subject_snapshot(subject)
        return snapshot is not None and snapshot.state is LivenessState.QUARANTINED

    def _reject_if_affected_peer_quarantined(
        self,
        participating_node_ids: frozenset[str],
    ) -> None:
        """Fail closed: admission touching a quarantined peer is refused.

        The detector's quarantine incident carries the
        ``remove_from_affected_admission`` action; this is its enforcement
        point for new requests whose participating nodes include the
        quarantined peer.
        """

        quarantined = tuple(
            sorted(
                node_id
                for node_id in participating_node_ids
                if self._peer_subject_is_quarantined(node_id)
            )
        )
        if quarantined:
            raise AffectedPeerQuarantined("affected_peer_quarantined")

    def _ingest_transport_scoped_events(
        self,
        node_id: str,
        observation: Mapping[str, Any],
    ) -> None:
        details = observation.get("details")
        transport = details.get("transport") if isinstance(details, Mapping) else None
        events = transport.get("scoped_events") if isinstance(transport, Mapping) else None
        if not isinstance(events, list):
            return
        last_sequence = self._last_transport_event_sequence.get(node_id, 0)
        for event in events:
            if not isinstance(event, Mapping):
                continue
            sequence = event.get("sequence")
            request_id = event.get("request_id")
            path_id = event.get("path_id")
            path_attempt = event.get("path_attempt")
            peer_node_id = event.get("peer_node_id")
            peer_generation = event.get("peer_generation")
            event_kind = event.get("event")
            if (
                type(sequence) is not int
                or sequence <= last_sequence
                or not isinstance(request_id, str)
                or not request_id
                or not isinstance(path_id, str)
                or not path_id
                or type(path_attempt) is not int
                or path_attempt < 0
                or not isinstance(peer_node_id, str)
                or not peer_node_id
                or type(peer_generation) is not int
                or peer_generation < 1
                or event_kind not in {"receipt", "failure"}
            ):
                continue
            edge_id = f"{node_id}->{peer_node_id}"
            subject = self._liveness_edge_subjects.get(edge_id)
            if (
                subject is None
                or subject.membership_generation != peer_generation
            ):
                observed_at_ms = int(time.monotonic() * 1_000)
                subject = LivenessSubject(
                    subject_id=edge_id,
                    kind=SubjectKind.EDGE,
                    membership_generation=peer_generation,
                )
                registered = self._liveness.register_subject(
                    subject,
                    observed_at_ms=max(0, observed_at_ms - 1),
                )
                if not registered.accepted:
                    continue
                self._liveness_edge_subjects[edge_id] = subject
            observed_at_ms = int(time.monotonic() * 1_000)
            if event_kind == "receipt":
                self._liveness.observe_receipt(
                    subject,
                    observed_at_ms=observed_at_ms,
                    source=ObservationSource.APPLICATION_RECEIPT,
                    signed=True,
                )
            else:
                self._liveness.record_active_failure(
                    subject,
                    failure_started_at_ms=observed_at_ms,
                    observed_at_ms=observed_at_ms,
                    scope="edge",
                    affected_track_ids=(request_id,),
                    verified=True,
                )
                with self._lock:
                    active = self._active_route_requests.get(request_id)
                    exact_active_identity = bool(
                        active is not None
                        and active.get("path_id") == path_id
                        and active.get("path_attempt") == path_attempt
                    )
                if exact_active_identity:
                    self._record_scoped_runtime_incident(
                        request_id=request_id,
                        subject_id=edge_id,
                        scope="edge",
                        reason=(
                            event.get("code")
                            if isinstance(event.get("code"), str)
                            and event.get("code")
                            else "transport_failure"
                        ),
                    )
            last_sequence = sequence
        self._last_transport_event_sequence[node_id] = last_sequence

    def _send_before(
        self,
        node_id: str,
        *,
        command_id: str,
        command: str,
        payload: Mapping[str, Any],
        deadline_monotonic_s: float,
    ) -> dict[str, Any]:
        sender = getattr(self._sessions[node_id], "send_before", None)
        if callable(sender):
            return sender(
                command_id=command_id,
                command=command,
                payload=payload,
                deadline_monotonic_s=deadline_monotonic_s,
            )
        if time.monotonic() >= deadline_monotonic_s:
            raise TimeoutError("command_deadline_exceeded")
        return self._sessions[node_id].send(
            command_id=command_id,
            command=command,
            payload=payload,
        )

    def _snapshot_nodes_before(
        self,
        node_ids: frozenset[str],
        *,
        deadline_monotonic_s: float,
        cleanup_subject: Mapping[str, Any] | None = None,
    ) -> None:
        def snapshot_node(node_id: str) -> tuple[str, dict[str, Any]]:
            # A peer whose transport is fatally failed can never answer a
            # fresh snapshot within the request-scoped cleanup deadline.
            # Record the existing last health observation (or a synthetic
            # fatal projection) so cleanup_complete can prove the affected
            # track and the gateway can publish a real terminal event.
            last_health = getattr(self, "_last_health", None)
            if isinstance(last_health, Mapping):
                with self._lock:
                    health = last_health.get(node_id)
                if isinstance(health, Mapping):
                    fatal = (
                        health.get("details", {}).get("transport_fatal_error")
                        if isinstance(health.get("details"), Mapping)
                        else None
                    )
                    if isinstance(fatal, Mapping):
                        return node_id, dict(health)
            response = self._send_before(
                node_id,
                command_id=self._command_id(node_id, "snapshot"),
                command="snapshot",
                payload=(
                    {}
                    if cleanup_subject is None
                    else {"cleanup_subject": dict(cleanup_subject)}
                ),
                deadline_monotonic_s=deadline_monotonic_s,
            )
            observation = self._verify_observation(
                node_id,
                response,
                event="snapshot",
            )
            self._ingest_transport_scoped_events(node_id, observation)
            return node_id, observation

        # Every proof command shares the owner's original absolute deadline.
        # Parallel fanout prevents one slower peer from consuming another peer's
        # proof budget while retaining one independently verified receipt per node.
        with ThreadPoolExecutor(
            max_workers=len(node_ids),
            thread_name_prefix="a4-cleanup-snapshot",
        ) as executor:
            results = tuple(
                executor.map(snapshot_node, sorted(node_ids))
            )
        snapshots = dict(results)
        with self._lock:
            self._last_snapshots.update(snapshots)

    def _cancellation_cleanup_complete(
        self,
        participating_node_ids: frozenset[str] | None = None,
        *,
        cleanup_subject: Mapping[str, Any] | None = None,
    ) -> bool:
        required = (
            frozenset(self._sessions)
            if participating_node_ids is None
            else participating_node_ids
        )
        if not required or not required <= set(self._last_snapshots):
            return False
        for node_id in required:
            observation = self._last_snapshots[node_id]
            details = observation.get("details")
            if not isinstance(details, Mapping):
                return False
            # A peer whose transport is fatally failed cannot produce a
            # cleanup receipt; the route's monitor and gateway already
            # consider the request cancelled. Treat that peer as
            # authoritative proof that no further scoped resource remains.
            fatal = details.get("transport_fatal_error")
            if isinstance(fatal, Mapping):
                continue
            if cleanup_subject is None:
                runtime = details.get("runtime")
                if (
                    not isinstance(runtime, Mapping)
                    or runtime.get("active_state_count") != 0
                    or details.get("transport_pending_delivery_count") != 0
                    or details.get("transport_cancellation_cleanup_complete") is not True
                ):
                    return False
            else:
                receipt = details.get("request_cleanup")
                if (
                    not isinstance(receipt, Mapping)
                    or any(
                        receipt.get(field) != expected
                        for field, expected in cleanup_subject.items()
                    )
                    or receipt.get("complete") is not True
                ):
                    return False
        return True

    def _wait_for_cancellation_cleanup(
        self,
        participating_node_ids: frozenset[str],
        *,
        deadline_monotonic_s: float,
        cleanup_subject: Mapping[str, Any],
    ) -> None:
        request_id = cleanup_subject.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise RuntimeError("route_cleanup_subject_invalid")
        with self._lock:
            active = self._active_route_requests.get(request_id)
            fanout_complete = (
                None
                if active is None
                else active.get("cancellation_fanout_complete")
            )
            cancellation_requested = bool(
                active is not None
                and active.get("cancellation_requested") is True
            )
        if cancellation_requested:
            if not isinstance(fanout_complete, threading.Event):
                raise RuntimeError("route_cancellation_fanout_untracked")
            remaining = deadline_monotonic_s - time.monotonic()
            if remaining <= 0 or not fanout_complete.wait(timeout=remaining):
                raise RuntimeError("route_cancellation_fanout_timeout")
        while True:
            self._snapshot_nodes_before(
                participating_node_ids,
                deadline_monotonic_s=deadline_monotonic_s,
                cleanup_subject=cleanup_subject,
            )
            if self._cancellation_cleanup_complete(
                participating_node_ids,
                cleanup_subject=cleanup_subject,
            ):
                return
            if time.monotonic() >= deadline_monotonic_s:
                raise RuntimeError("route_cancellation_cleanup_timeout")
            time.sleep(0.02)

    def _store_request_cleanup_receipt(
        self,
        *,
        request_id: str,
        cleanup_subject: Mapping[str, Any],
        participating_node_ids: frozenset[str],
        deployment_id: str,
    ) -> None:
        receipt = {
            **cleanup_subject,
            "cleanup_owner_id": f"physical-live-route:{deployment_id}",
            "node_ids": sorted(participating_node_ids),
            "completed_at_monotonic_ms": int(time.monotonic() * 1_000),
        }
        receipt["receipt_digest"] = "sha256:" + hashlib.sha256(
            json.dumps(
                receipt,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        with self._lock:
            self._request_cleanup_receipts[request_id] = receipt

    def _request_control_identity(self, request_id: str) -> dict[str, Any]:
        with self._lock:
            active = self._active_route_requests.get(request_id)
            if active is None:
                raise RuntimeError("request_control_identity_unavailable")
            return {
                field: active[field]
                for field in (
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
                )
            }

    def _request_cleanup_subject(self, request_id: str) -> dict[str, Any]:
        return {
            "request_id": request_id,
            **self._request_control_identity(request_id),
        }

    def cancel_request(
        self,
        request_id: str,
        *,
        deadline_monotonic_s: float | None = None,
    ) -> bool:
        """Interrupt one physical request without terminating its shared node."""

        with self._lock:
            active = self._active_route_requests.get(request_id)
            if active is None or active.get("terminal") is True:
                return False
            if active.get("cancellation_requested") is True:
                return False
            active["cancellation_requested"] = True
            active["cancellation_generation"] += 1
            active["cancellation_started_at"] = time.monotonic()
            fanout_complete = threading.Event()
            active["cancellation_fanout_complete"] = fanout_complete
            if deadline_monotonic_s is None:
                deadline_monotonic_s = active["cancellation_started_at"] + 2.0
            if (
                not isinstance(deadline_monotonic_s, (int, float))
                or isinstance(deadline_monotonic_s, bool)
                or deadline_monotonic_s <= active["cancellation_started_at"]
                or deadline_monotonic_s
                > active["cancellation_started_at"] + 2.0
            ):
                raise ValueError("invalid_cancellation_deadline")
            active["cancellation_deadline"] = float(deadline_monotonic_s)
            participating_node_ids = frozenset(active["participating_node_ids"])
            inflight_commands = dict(active.get("inflight_commands", {}))
            deadline = float(active["cancellation_deadline"])
            remaining_ms = max(1, int((deadline - time.monotonic()) * 1_000))
            payload = {
                "request_id": request_id,
                "request_attempt": active["request_attempt"],
                "deployment_id": active["deployment_id"],
                "deployment_epoch": active["deployment_epoch"],
                "qualification_digest": active["qualification_digest"],
                "command_id": active["command_id"],
                "publisher_generation": active["publisher_generation"],
                "absolute_deadline_ms": active["absolute_deadline_ms"],
                "path_id": active["path_id"],
                "path_attempt": active["path_attempt"],
                "path_digest": active["path_digest"],
                "topology_generation": active["topology_generation"],
                "cancellation_generation": active["cancellation_generation"],
                "deadline_budget_ms": min(2_000, remaining_ms),
            }
        # Wake only this request's correlated control waiters. The node process
        # remains shared and alive; any later response is retired by command ID.
        for node_id, command_id in sorted(inflight_commands.items()):
            session = self._sessions.get(node_id)
            interrupt = getattr(session, "interrupt_command", None)
            if callable(interrupt):
                try:
                    interrupt(command_id, code="request_cancelled")
                except BaseException:
                    pass
        # Every participating node owns request/path-scoped runtime or transport
        # state.  Propagate the same owner-issued cancellation generation and
        # absolute deadline to each node; a relay-level path cancellation alone
        # cannot advance the remote node's command-control generation.
        def cancel_node(node_id: str) -> tuple[str, dict[str, Any] | None]:
            # A peer whose transport is already fatally failed cannot answer
            # any further command. Treat the live monitor's last fatal
            # projection as an authoritative cancellation projection so the
            # fanout does not consume the request's only 2,000 ms budget
            # waiting on a transport that is already declared dead.
            last_health = getattr(self, "_last_health", None)
            if isinstance(last_health, Mapping):
                with self._lock:
                    observed = last_health.get(node_id)
                if isinstance(observed, Mapping):
                    fatal = (
                        observed.get("details", {}).get(
                            "transport_fatal_error"
                        )
                        if isinstance(observed.get("details"), Mapping)
                        else None
                    )
                    if isinstance(fatal, Mapping):
                        return node_id, dict(observed)
            response = self._send_before(
                node_id,
                command_id=self._command_id(node_id, "infer-cancel"),
                command="infer_cancel",
                payload=payload,
                deadline_monotonic_s=deadline,
            )
            observation = self._verify_observation(
                node_id,
                response,
                event="inference_cancelled",
            )
            return node_id, observation

        try:
            with ThreadPoolExecutor(
                max_workers=len(participating_node_ids),
                thread_name_prefix="a4-cancel-fanout",
            ) as executor:
                tuple(executor.map(cancel_node, sorted(participating_node_ids)))
        finally:
            # A lost peer may reject its fanout leaf while surviving peers still
            # consume the same generation-fenced interruption.  Always release
            # the barrier after every leaf resolved or failed; leaving it unset
            # hides the real peer failure behind a second artificial timeout.
            fanout_complete.set()
        return True

    def update_publisher_generation(
        self,
        request_id: str,
        *,
        expected_generation: int,
        new_generation: int,
        route_identity: Mapping[str, Any],
    ) -> bool:
        """CAS a gateway-owned publisher generation onto one physical request."""

        if (
            type(expected_generation) is not int
            or expected_generation < 1
            or type(new_generation) is not int
            or new_generation != expected_generation + 1
        ):
            return False
        identity = dict(route_identity)
        expected_fields = {
            "request_id",
            "request_attempt",
            "path_id",
            "path_attempt",
            "path_manifest_digest",
            "deployment_id",
            "deployment_epoch",
            "topology_generation",
        }
        if set(identity) != expected_fields or identity["request_id"] != request_id:
            return False
        with self._lock:
            active = self._active_route_requests.get(request_id)
            if active is None:
                prior = self._pending_publisher_generations.get(request_id)
                candidate = (expected_generation, new_generation, identity)
                if prior is not None and prior != candidate:
                    return False
                self._pending_publisher_generations[request_id] = candidate
                return True
            if (
                active["request_attempt"] != identity["request_attempt"]
                or active["path_id"] != identity["path_id"]
                or active["path_attempt"] != identity["path_attempt"]
                or active["path_digest"] != identity["path_manifest_digest"]
                or active["deployment_id"] != identity["deployment_id"]
                or active["deployment_epoch"] != identity["deployment_epoch"]
                or active["topology_generation"] != identity["topology_generation"]
                or active["publisher_generation"] != expected_generation
                or active.get("terminal") is True
            ):
                return False
            active["publisher_generation"] = new_generation
            control_bound = active.get("control_bound") is True
            participating = frozenset(active["participating_node_ids"])
        if control_bound:
            self._propagate_publisher_generation(request_id, participating)
        return True

    def _propagate_publisher_generation(
        self,
        request_id: str,
        participating_node_ids: frozenset[str],
    ) -> None:
        control = self._request_control_identity(request_id)
        for node_id in sorted(participating_node_ids):
            response = self._sessions[node_id].send(
                command_id=self._command_id(node_id, "update-request-control"),
                command="update_request_control",
                payload={"request_id": request_id, "control": control},
            )
            self._verify_observation(
                node_id,
                response,
                event="request_control_updated",
            )

    def infer(
        self,
        token_ids: Sequence[int],
        *,
        max_new_tokens: int,
        request_id: str,
        sink: TokenSink,
        cancel_requested: Callable[[], bool] | None = None,
        selected_placement_ids: Sequence[str] | None = None,
        route_identity: Mapping[str, Any] | None = None,
        locked_path_manifest: Mapping[str, Any] | None = None,
        command_identity: Mapping[str, Any] | None = None,
        authorize_cleanup: Callable[[float], float] | None = None,
    ) -> InferenceResult:
        # This lock is request-scoped. No route-global mutex spans physical
        # prefill, decode, cancellation, cleanup, or browser streaming.
        with self._request_execution_lock(request_id):
            if (
                getattr(self, "_closed", False)
                or not getattr(self, "_open", True)
                or self._fatal is not None
            ):
                raise RuntimeError("route_not_open")
            if (
                not isinstance(max_new_tokens, int)
                or isinstance(max_new_tokens, bool)
                or max_new_tokens < 1
                or not isinstance(request_id, str)
                or not request_id
            ):
                raise ValueError("invalid_inference_request")
            graph = getattr(self, "_graph", None)
            if graph is None:
                explicit_selection = False
                if selected_placement_ids is not None:
                    raise ValueError("invalid_selected_placement_ids")
                entry_node_id = self._plan["entry_node_id"]
                excluded_placement_ids: list[str] = []
                participating_node_ids = frozenset(self._sessions)
            else:
                explicit_selection = selected_placement_ids is not None
                selected = (
                    tuple(selected_placement_ids)
                    if selected_placement_ids is not None
                    else tuple(
                        stage.placements[0].placement_id for stage in graph.stages
                    )
                )
                if len(selected) != len(graph.stages) or len(set(selected)) != len(
                    selected
                ):
                    raise ValueError("invalid_selected_placement_ids")
                selected_placements = []
                for stage, placement_id in zip(graph.stages, selected, strict=True):
                    matches = [
                        placement
                        for placement in stage.placements
                        if placement.placement_id == placement_id
                    ]
                    if len(matches) != 1:
                        raise ValueError("invalid_selected_placement_ids")
                    selected_placements.append(matches[0])
                entry_node_id = selected_placements[0].node_id
                participating_node_ids = frozenset(
                    placement.node_id for placement in selected_placements
                )
                excluded_placement_ids = [
                    placement.placement_id
                    for stage in graph.stages
                    for placement in stage.placements
                    if placement.placement_id not in selected
                ]
            if any(
                getattr(self._sessions[node_id], "returncode", None) is not None
                for node_id in participating_node_ids
            ):
                raise RuntimeError("selected_route_not_open")
            self._reject_if_affected_peer_quarantined(participating_node_ids)
            before_peers = self._peer_counters()
            started_at = time.monotonic()
            prefill_completed_at: float | None = None
            token_times: list[float] = []
            request = {
                **self._plan["request"],
                "request_id": request_id,
                "prompt_token_ids": list(token_ids),
                "max_new_tokens": max_new_tokens,
                "expected_new_tokens": max_new_tokens,
            }
            if route_identity is None:
                # Legacy qualification helpers do not use the product coordinator.
                # Product requests are required to pass the M16-owned identity.
                path_identity = {
                    "request_id": request_id,
                    "request_attempt": 1,
                    "path_id": f"legacy:{request_id}",
                    "path_attempt": 0,
                    "path_manifest_digest": "sha256:" + hashlib.sha256(
                        request_id.encode("utf-8")
                    ).hexdigest(),
                    "deployment_id": (
                        graph.deployment_id
                        if graph is not None
                        else self._plan.get("deployment_id", "legacy-route")
                    ),
                    "deployment_epoch": graph.deployment_epoch if graph is not None else 0,
                    "topology_generation": (
                        graph.topology_version if graph is not None else 0
                    ),
                }
            else:
                path_identity = dict(route_identity)
                expected = {
                    "request_id",
                    "request_attempt",
                    "path_id",
                    "path_attempt",
                    "path_manifest_digest",
                    "deployment_id",
                    "deployment_epoch",
                    "topology_generation",
                }
                if set(path_identity) != expected or path_identity["request_id"] != request_id:
                    raise ValueError("invalid_route_identity")
                if graph is not None and (
                    path_identity["deployment_id"] != graph.deployment_id
                    or path_identity["deployment_epoch"] != graph.deployment_epoch
                    or path_identity["topology_generation"] != graph.topology_version
                ):
                    raise ValueError("stale_route_identity")
            if route_identity is not None:
                from mycelium_router.serialization import path_manifest_from_dict

                if not isinstance(locked_path_manifest, Mapping):
                    raise ValueError("locked_path_manifest_required")
                locked_manifest = path_manifest_from_dict(dict(locked_path_manifest))
                from mycelium_router.validation import validate_manifest

                if graph is None:
                    raise ValueError("locked_path_graph_unavailable")
                validate_manifest(locked_manifest, graph)
                serialized_digest = "sha256:" + hashlib.sha256(
                    json.dumps(
                        dict(locked_path_manifest),
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                if (
                    locked_manifest.request_id != request_id
                    or locked_manifest.path_id != path_identity["path_id"]
                    or locked_manifest.path_attempt != path_identity["path_attempt"]
                    or serialized_digest != path_identity["path_manifest_digest"]
                ):
                    raise ValueError("locked_path_identity_mismatch")
            elif locked_path_manifest is not None:
                raise ValueError("unexpected_locked_path_manifest")
            path_digest = str(path_identity["path_manifest_digest"])
            qualification = getattr(self, "_deployment_qualification", None)
            if route_identity is not None:
                if qualification is None or getattr(qualification, "route_ready", False) is not True:
                    raise RuntimeError("deployment_qualification_unavailable")
                from mycelium_request_gateway.contracts import qualification_digest

                current_qualification_digest = qualification_digest(qualification)
            else:
                current_qualification_digest = "sha256:" + hashlib.sha256(
                    b"legacy-qualification-helper"
                ).hexdigest()
            if route_identity is not None:
                expected_command_fields = {
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
                    "publisher_generation",
                    "absolute_deadline_ms",
                    "cancellation_generation",
                }
                if not isinstance(command_identity, Mapping):
                    raise ValueError("command_identity_required")
                command_control = dict(command_identity)
                if (
                    set(command_control) != expected_command_fields
                    or command_control["request_id"] != request_id
                    or command_control["deployment_id"] != path_identity["deployment_id"]
                    or command_control["deployment_epoch"] != path_identity["deployment_epoch"]
                    or command_control["qualification_digest"]
                    != current_qualification_digest
                    or command_control["request_attempt"]
                    != path_identity["request_attempt"]
                    or command_control["path_id"] != path_identity["path_id"]
                    or command_control["path_attempt"] != path_identity["path_attempt"]
                    or command_control["path_digest"] != path_digest
                    or command_control["topology_generation"]
                    != path_identity["topology_generation"]
                    or not isinstance(command_control["command_id"], str)
                    or not command_control["command_id"]
                    or type(command_control["publisher_generation"]) is not int
                    or command_control["publisher_generation"] < 1
                    or type(command_control["absolute_deadline_ms"]) is not int
                    or command_control["absolute_deadline_ms"]
                    <= int(time.monotonic() * 1_000)
                    or command_control["cancellation_generation"] != 0
                ):
                    raise ValueError("invalid_command_identity")
                with self._lock:
                    pending_publisher = self._pending_publisher_generations.pop(
                        request_id,
                        None,
                    )
                if pending_publisher is not None:
                    expected_publisher, new_publisher, pending_identity = (
                        pending_publisher
                    )
                    if (
                        pending_identity != path_identity
                        or command_control["publisher_generation"]
                        != expected_publisher
                        or new_publisher != expected_publisher + 1
                    ):
                        raise ValueError("stale_publisher_generation")
                    command_control["publisher_generation"] = new_publisher
            elif command_identity is not None:
                raise ValueError("unexpected_command_identity")
            if route_identity is not None and authorize_cleanup is None:
                raise ValueError("cleanup_authority_required")
            if route_identity is None and authorize_cleanup is not None:
                raise ValueError("unexpected_cleanup_authority")
            with self._lock:
                active_requests = getattr(self, "_active_route_requests", None)
                if active_requests is None:
                    active_requests = {}
                    self._active_route_requests = active_requests
                self._active_route_requests[request_id] = {
                    "request_attempt": path_identity["request_attempt"],
                    "path_id": path_identity["path_id"],
                    "path_attempt": path_identity["path_attempt"],
                    "path_digest": path_digest,
                    "deployment_id": path_identity["deployment_id"],
                    "deployment_epoch": path_identity["deployment_epoch"],
                    "qualification_digest": current_qualification_digest,
                    "topology_generation": path_identity["topology_generation"],
                    "command_id": (
                        command_control["command_id"]
                        if route_identity is not None
                        else f"legacy:{request_id}"
                    ),
                    "publisher_generation": (
                        command_control["publisher_generation"]
                        if route_identity is not None
                        else 1
                    ),
                    "absolute_deadline_ms": (
                        command_control["absolute_deadline_ms"]
                        if route_identity is not None
                        else int(time.monotonic() * 1_000) + 3_600_000
                    ),
                    "entry_node_id": entry_node_id,
                    "participating_node_ids": participating_node_ids,
                    "cancellation_generation": 0,
                    "cancellation_requested": False,
                    "cancellation_started_at": None,
                    "cancellation_deadline": None,
                    "cancellation_fanout_complete": None,
                    "terminal": False,
                    "control_bound": False,
                }
            cleanup_subject = self._request_cleanup_subject(request_id)
            try:
                if route_identity is not None:
                    bind_control = self._request_control_identity(request_id)
                    for node_id in sorted(participating_node_ids):
                        bound_response = self._send_request_command(
                            request_id=request_id,
                            node_id=node_id,
                            command_id=self._command_id(
                                node_id,
                                "bind-request-control",
                            ),
                            command="bind_request_control",
                            payload={
                                "request_id": request_id,
                                "control": bind_control,
                            },
                        )
                        self._verify_observation(
                            node_id,
                            bound_response,
                            event="request_control_bound",
                        )
                    with self._lock:
                        active = self._active_route_requests[request_id]
                        active["control_bound"] = True
                        publisher_changed_during_bind = (
                            active["publisher_generation"]
                            != bind_control["publisher_generation"]
                        )
                    if publisher_changed_during_bind:
                        self._propagate_publisher_generation(
                            request_id,
                            participating_node_ids,
                        )
                start_payload: dict[str, Any] = {"request": request}
                if route_identity is not None:
                    start_payload["control"] = {
                        "request_attempt": path_identity["request_attempt"],
                        "path_id": path_identity["path_id"],
                        "path_attempt": path_identity["path_attempt"],
                        "path_digest": path_digest,
                        "deployment_id": path_identity["deployment_id"],
                        "deployment_epoch": path_identity["deployment_epoch"],
                        "qualification_digest": current_qualification_digest,
                        "topology_generation": path_identity["topology_generation"],
                        "cancellation_generation": 0,
                        "command_id": command_control["command_id"],
                        "publisher_generation": command_control[
                            "publisher_generation"
                        ],
                        "absolute_deadline_ms": command_control[
                            "absolute_deadline_ms"
                        ],
                    }
                if locked_path_manifest is not None:
                    start_payload["path_manifest"] = dict(locked_path_manifest)
                if excluded_placement_ids:
                    start_payload["excluded_placement_ids"] = excluded_placement_ids
                started_response = self._send_request_command(
                    request_id=request_id,
                    node_id=entry_node_id,
                    command_id=self._command_id(entry_node_id, "infer-start"),
                    command="infer_start",
                    payload=start_payload,
                )
                observation = self._verify_observation(
                    entry_node_id,
                    started_response,
                    event="inference_started",
                )
                prefill_completed_at = time.monotonic()
                output = self._output_tokens(observation)
                status = observation["details"].get("status")
                emitted = 0
                stopped = False

                def truncate_at_stop(tokens: tuple[int, ...]) -> tuple[int, ...]:
                    nonlocal stopped
                    for index, token_id in enumerate(tokens):
                        if token_id in self._stop_token_ids:
                            stopped = True
                            return tokens[:index]
                    return tokens

                output = truncate_at_stop(output)
                cancelled_for_request = _request_is_cancelled(
                    status,
                    cancel_requested,
                )

                def emit_new_tokens(tokens: tuple[int, ...]) -> None:
                    nonlocal emitted
                    for index in range(emitted, len(tokens)):
                        sink.emit(index, tokens[index])
                        token_times.append(time.monotonic())
                    emitted = len(tokens)

                if not cancelled_for_request:
                    emit_new_tokens(output)
                while (
                    len(output) < max_new_tokens
                    and status == "DECODING"
                    and not stopped
                    and not cancelled_for_request
                ):
                    decode_payload: dict[str, Any] = {
                        "request_id": request_id,
                        "count": 1,
                    }
                    if route_identity is not None:
                        decode_payload["control"] = self._request_control_identity(
                            request_id
                        )
                    decoded_response = self._send_request_command(
                        request_id=request_id,
                        node_id=entry_node_id,
                        command_id=self._command_id(entry_node_id, "infer-decode"),
                        command="infer_decode",
                        payload=decode_payload,
                    )
                    decoded = self._verify_observation(
                        entry_node_id,
                        decoded_response,
                        event="inference_decoded",
                    )
                    next_output = self._output_tokens(decoded)
                    status = decoded["details"].get("status")
                    cancelled_for_request = _request_is_cancelled(
                        status,
                        cancel_requested,
                    )
                    if status == "CANCELLED":
                        break
                    if len(next_output) <= len(output):
                        if status == "COMPLETED":
                            break
                        raise RuntimeError("route_decode_stalled")
                    output = truncate_at_stop(next_output)
                    if not cancelled_for_request:
                        emit_new_tokens(output)
                cancelled_for_stop = stopped and status == "DECODING"
                if cancelled_for_stop:
                    if route_identity is None:
                        cancelled_response = self._sessions[entry_node_id].send(
                            command_id=self._command_id(entry_node_id, "infer-cancel"),
                            command="infer_cancel",
                            payload={"request_id": request_id},
                        )
                        self._verify_observation(
                            entry_node_id,
                            cancelled_response,
                            event="inference_cancelled",
                        )
                    elif not self.cancel_request(request_id):
                        raise RuntimeError("route_cancellation_rejected")
                output = output[:max_new_tokens]
                if cancelled_for_stop or cancelled_for_request:
                    with self._lock:
                        cancellation_deadline = self._active_route_requests[
                            request_id
                        ].get("cancellation_deadline")
                    if cancellation_deadline is None:
                        cancellation_deadline = time.monotonic() + 2.0
                    if authorize_cleanup is not None:
                        cancellation_deadline = authorize_cleanup(
                            float(cancellation_deadline)
                        )
                    cleanup_subject = self._request_cleanup_subject(request_id)
                    self._wait_for_cancellation_cleanup(
                        participating_node_ids,
                        deadline_monotonic_s=float(cancellation_deadline),
                        cleanup_subject=cleanup_subject,
                    )
                elif route_identity is not None:
                    # A completed request still owns Router path registration,
                    # stage-local KV, transport forwarding state, and capacity
                    # reservations until an exact generation-fenced teardown is
                    # issued. Observing snapshots cannot cause that cleanup. Use
                    # the same request-scoped cooperative interrupt operation as
                    # cancellation, but retain the already-determined completed
                    # terminal outcome after cleanup is proven.
                    cleanup_deadline = time.monotonic() + 2.0
                    assert authorize_cleanup is not None
                    cleanup_deadline = authorize_cleanup(cleanup_deadline)
                    if not self.cancel_request(
                        request_id,
                        deadline_monotonic_s=cleanup_deadline,
                    ):
                        raise RuntimeError("route_completion_cleanup_rejected")
                    cleanup_subject = self._request_cleanup_subject(request_id)
                    self._wait_for_cancellation_cleanup(
                        participating_node_ids,
                        deadline_monotonic_s=cleanup_deadline,
                        cleanup_subject=cleanup_subject,
                    )
                elif explicit_selection:
                    self._snapshot_nodes(participating_node_ids)
                else:
                    self._snapshot_all()
                if route_identity is not None:
                    self._store_request_cleanup_receipt(
                        request_id=request_id,
                        cleanup_subject=cleanup_subject,
                        participating_node_ids=participating_node_ids,
                        deployment_id=path_identity["deployment_id"],
                    )
                if self._fatal is not None:
                    raise RuntimeError(self._fatal)
                if cancelled_for_request:
                    raise InferenceCancelled("inference_cancelled")
            except BaseException as exc:
                failure_observed_at = time.monotonic()
                remote_reason = getattr(exc, "remote_code", None)
                reason = str(
                    remote_reason
                    if isinstance(remote_reason, str) and remote_reason
                    else getattr(exc, "code", str(exc) or type(exc).__name__)
                )
                with self._lock:
                    active_request = self._active_route_requests.get(request_id)
                    cancellation_requested = bool(
                        active_request is not None
                        and active_request.get("cancellation_requested") is True
                    )
                if (
                    cancellation_requested
                    and reason == "request_cancelled"
                ):
                    # The cooperative interrupt retired this request's inflight
                    # node command after the owner (or scoped liveness)
                    # requested cancellation.  This is the expected
                    # interruption outcome, not a runtime failure: complete
                    # the owner-scoped cleanup below and surface the request's
                    # terminal as cancelled.
                    exc = InferenceCancelled("inference_cancelled")
                if not isinstance(exc, InferenceCancelled):
                    self._record_active_runtime_failure(
                        request_id=request_id,
                        node_id=entry_node_id,
                        reason=reason,
                        observed_at_monotonic_s=failure_observed_at,
                    )
                if (
                    route_identity is not None
                    and self.request_cleanup_receipt(request_id) is None
                ):
                    with self._lock:
                        active = self._active_route_requests.get(request_id)
                        existing_deadline = (
                            None
                            if active is None
                            else active.get("cancellation_deadline")
                        )
                        cancellation_requested = bool(
                            active is not None
                            and active.get("cancellation_requested") is True
                        )
                    cleanup_deadline = (
                        float(existing_deadline)
                        if existing_deadline is not None
                        else failure_observed_at + 2.0
                    )
                    if not cancellation_requested:
                        try:
                            assert authorize_cleanup is not None
                            cleanup_deadline = authorize_cleanup(cleanup_deadline)
                            self.cancel_request(
                                request_id,
                                deadline_monotonic_s=cleanup_deadline,
                            )
                        except BaseException:
                            # A failed interrupt command is not cleanup evidence.
                            # Exact per-node snapshots below remain authoritative.
                            pass
                    cleanup_subject = self._request_cleanup_subject(request_id)
                    try:
                        self._wait_for_cancellation_cleanup(
                            participating_node_ids,
                            deadline_monotonic_s=cleanup_deadline,
                            cleanup_subject=cleanup_subject,
                        )
                    except BaseException as cleanup_error:
                        self._record_incident(
                            state="request_cleanup_unproven",
                            reason=str(
                                getattr(
                                    cleanup_error,
                                    "code",
                                    str(cleanup_error)
                                    or type(cleanup_error).__name__,
                                )
                            ),
                            request_id=request_id,
                        )
                        raise RuntimeError(
                            "request_terminal_blocked_cleanup_unproven"
                        ) from exc
                    self._store_request_cleanup_receipt(
                        request_id=request_id,
                        cleanup_subject=cleanup_subject,
                        participating_node_ids=participating_node_ids,
                        deployment_id=path_identity["deployment_id"],
                    )
                if isinstance(exc, InferenceCancelled):
                    raise exc
                self._record_incident(
                    state="request_failed_closed",
                    reason=reason,
                    request_id=request_id,
                )
                raise

            with self._lock:
                active = self._active_route_requests.get(request_id)
                if active is not None:
                    active["terminal"] = True
            self._request_outputs[request_id] = output
            self._request_inputs[request_id] = tuple(token_ids)
            self._request_limits[request_id] = max_new_tokens
            self._request_entry_nodes[request_id] = entry_node_id
            completed_at = time.monotonic()
            after_peers = self._peer_counters()
            first_token_at = token_times[0] if token_times else None
            tpot_ms = None
            if len(token_times) > 1:
                tpot_ms = (
                    (token_times[-1] - token_times[0])
                    * 1_000.0
                    / (len(token_times) - 1)
                )
            self._recent_inferences.append(
                {
                    "context_tokens": len(token_ids),
                    "output_tokens": len(output),
                    "prefill_ms": (
                        None
                        if prefill_completed_at is None
                        else (prefill_completed_at - started_at) * 1_000.0
                    ),
                    "ttft_ms": (
                        None
                        if first_token_at is None
                        else (first_token_at - started_at) * 1_000.0
                    ),
                    "tpot_ms": tpot_ms,
                    "total_ms": (completed_at - started_at) * 1_000.0,
                    "peer_counter_deltas": [
                        {
                            "node_id": node_id,
                            "frames_sent": (
                                after_peers[node_id]["frames_sent"]
                                - before_peers[node_id]["frames_sent"]
                            ),
                            "frames_received": (
                                after_peers[node_id]["frames_received"]
                                - before_peers[node_id]["frames_received"]
                            ),
                            "applied_operation_count": (
                                after_peers[node_id]["applied_operation_count"]
                                - before_peers[node_id]["applied_operation_count"]
                            ),
                        }
                        for node_id in sorted(after_peers)
                    ],
                }
            )
            return InferenceResult(request_id=request_id, token_ids=output)

    def qualify_replica_concurrency(
        self,
        requests: Sequence[Sequence[int]],
        *,
        max_new_tokens: int,
        request_id_prefix: str,
    ) -> Mapping[str, Any]:
        """Run two overlapping requests and expose their immutable Router tracks.

        This is a qualification seam, not the browser request gateway. It keeps
        both requests admitted at once, advances decode round-robin, and returns
        privacy-reduced path and timing evidence for the M18 physical gate.
        """

        with self._lock:
            if not self.is_alive():
                raise RuntimeError("route_not_open")
            if (
                len(requests) != 2
                or not isinstance(max_new_tokens, int)
                or isinstance(max_new_tokens, bool)
                or max_new_tokens < 2
                or not isinstance(request_id_prefix, str)
                or not request_id_prefix
            ):
                raise ValueError("invalid_replica_qualification_request")
            normalized = tuple(tuple(tokens) for tokens in requests)
            if any(
                not tokens
                or any(
                    not isinstance(token, int) or isinstance(token, bool) or token < 0
                    for token in tokens
                )
                for tokens in normalized
            ):
                raise ValueError("invalid_replica_qualification_request")

            if (
                len(self._graph.stages) == 1
                and len(self._graph.stages[0].placements) >= 2
            ):
                entry_node_ids = tuple(
                    placement.node_id
                    for placement in self._graph.stages[0].placements[:2]
                )
            else:
                entry_node_ids = (self._plan["entry_node_id"],) * 2
            before_peers = self._peer_counters()
            started_at = time.monotonic()
            admission_barrier = threading.Barrier(2)

            def execute_track(
                index: int, token_ids: tuple[int, ...], entry_node_id: str
            ) -> dict[str, Any]:
                request_id = f"{request_id_prefix}-{index}"
                request = {
                    **self._plan["request"],
                    "request_id": request_id,
                    "prompt_token_ids": list(token_ids),
                    "max_new_tokens": max_new_tokens,
                    "expected_new_tokens": max_new_tokens,
                }
                payload: dict[str, Any] = {"request": request}
                if len(self._graph.stages) == 1:
                    local_placements = tuple(
                        placement
                        for placement in self._graph.stages[0].placements
                        if placement.node_id == entry_node_id
                    )
                    if len(local_placements) != 1:
                        raise RuntimeError("replica_entry_placement_invalid")
                    selected_placement_id = local_placements[0].placement_id
                    payload["excluded_placement_ids"] = [
                        placement.placement_id
                        for placement in self._graph.stages[0].placements
                        if placement.placement_id != selected_placement_id
                    ]
                command_started_offset_ms = (
                    time.monotonic() - started_at
                ) * 1_000.0
                try:
                    try:
                        response = self._sessions[entry_node_id].send(
                            command_id=self._command_id(
                                entry_node_id, "replica-infer-start"
                            ),
                            command="infer_start",
                            payload=payload,
                        )
                        observation = self._verify_observation(
                            entry_node_id,
                            response,
                            event="inference_started",
                        )
                    except BaseException as exc:
                        remote_code = getattr(exc, "remote_code", None)
                        code = (
                            remote_code if isinstance(remote_code, str) else str(exc)
                        )
                        raise RuntimeError(
                            f"replica_admission_rejected:{entry_node_id}:{code}"
                        ) from exc
                    details = observation.get("details")
                    path = (
                        details.get("path") if isinstance(details, Mapping) else None
                    )
                    output = self._output_tokens(observation)
                    if (
                        not isinstance(path, Mapping)
                        or not isinstance(path.get("path_id"), str)
                        or not isinstance(path.get("path_attempt"), int)
                        or not isinstance(path.get("placement_ids"), list)
                        or not path["placement_ids"]
                        or any(
                            not isinstance(item, str) or not item
                            for item in path["placement_ids"]
                        )
                    ):
                        raise RuntimeError("replica_qualification_path_missing")
                    record = {
                        "request_id": request_id,
                        "initial_status": details.get("status"),
                        "status": details.get("status"),
                        "output": output,
                        "path_id": path["path_id"],
                        "path_attempt": path["path_attempt"],
                        "placement_ids": tuple(path["placement_ids"]),
                        "entry_node_id": entry_node_id,
                        "admitted_offset_ms": (
                            time.monotonic() - started_at
                        )
                        * 1_000.0,
                    }
                    admission_barrier.wait()
                    record["decode_started_offset_ms"] = (
                        time.monotonic() - started_at
                    ) * 1_000.0
                    while (
                        record["status"] == "DECODING"
                        and len(record["output"]) < max_new_tokens
                    ):
                        response = self._sessions[record["entry_node_id"]].send(
                        command_id=self._command_id(
                            record["entry_node_id"], "replica-infer-decode"
                        ),
                        command="infer_decode",
                        payload={"request_id": record["request_id"], "count": 1},
                    )
                        observation = self._verify_observation(
                            record["entry_node_id"],
                            response,
                            event="inference_decoded",
                        )
                        output = self._output_tokens(observation)
                        if len(output) <= len(record["output"]):
                            if observation["details"].get("status") == "COMPLETED":
                                record["status"] = "COMPLETED"
                                break
                            raise RuntimeError("replica_qualification_decode_stalled")
                        record["output"] = output
                        record["status"] = observation["details"].get("status")
                    record["completed_offset_ms"] = (
                        time.monotonic() - started_at
                    ) * 1_000.0
                    record["active_compute_elapsed_ms"] = (
                        record["admitted_offset_ms"]
                        - command_started_offset_ms
                        + record["completed_offset_ms"]
                        - record["decode_started_offset_ms"]
                    )
                    return record
                except BaseException:
                    admission_barrier.abort()
                    raise

            with ThreadPoolExecutor(
                max_workers=2, thread_name_prefix="m18-replica"
            ) as executor:
                futures = [
                    executor.submit(execute_track, index, token_ids, entry_node_id)
                    for index, (token_ids, entry_node_id) in enumerate(
                        zip(normalized, entry_node_ids, strict=True)
                    )
                ]
                records = [future.result() for future in futures]

            overlapping_at_admission = (
                all(record["initial_status"] == "DECODING" for record in records)
                and max(record["admitted_offset_ms"] for record in records)
                < min(record["completed_offset_ms"] for record in records)
            )

            self._snapshot_all()
            after_peers = self._peer_counters()
            completed_at = time.monotonic()
            tracks = [record["placement_ids"] for record in records]
            for record, tokens in zip(records, normalized, strict=True):
                request_id = record["request_id"]
                self._request_inputs[request_id] = tokens
                self._request_outputs[request_id] = tuple(record["output"])
                self._request_limits[request_id] = max_new_tokens
                self._request_entry_nodes[request_id] = record["entry_node_id"]
            return {
                "protocol": "mycelium.physical_replica_concurrency.v1",
                "deployment_id": self._graph.deployment_id,
                "request_count": 2,
                "overlapping_at_admission": overlapping_at_admission,
                "distinct_tracks": tracks[0] != tracks[1],
                "elapsed_ms": (completed_at - started_at) * 1_000.0,
                "requests": [
                    {
                        "request_id": record["request_id"],
                        "path_id": record["path_id"],
                        "path_attempt": record["path_attempt"],
                        "entry_node_id": record["entry_node_id"],
                        "placement_ids": list(record["placement_ids"]),
                        "prompt_token_count": len(tokens),
                        "output_token_count": len(record["output"]),
                        "output_digest": "sha256:"
                        + hashlib.sha256(
                            json.dumps(
                                list(record["output"]), separators=(",", ":")
                            ).encode("utf-8")
                        ).hexdigest(),
                        "admitted_offset_ms": record["admitted_offset_ms"],
                        "active_compute_elapsed_ms": record[
                            "active_compute_elapsed_ms"
                        ],
                    }
                    for record, tokens in zip(records, normalized, strict=True)
                ],
                "peer_counter_deltas": [
                    {
                        "node_id": node_id,
                        "frames_sent": after_peers[node_id]["frames_sent"]
                        - before_peers[node_id]["frames_sent"],
                        "frames_received": after_peers[node_id]["frames_received"]
                        - before_peers[node_id]["frames_received"],
                        "applied_operation_count": after_peers[node_id][
                            "applied_operation_count"
                        ]
                        - before_peers[node_id]["applied_operation_count"],
                    }
                    for node_id in sorted(after_peers)
                ],
                "route_ready": False,
            }

    def measure_replica_saturation(
        self,
        token_ids: Sequence[int],
        *,
        expected_output_token_ids: Sequence[int],
        request_counts_by_node: Mapping[str, int],
        request_id_prefix: str,
    ) -> Mapping[str, Any]:
        """Measure weighted steady-state throughput across complete local replicas."""

        with self._lock:
            if not self.is_alive() or len(self._graph.stages) != 1:
                raise RuntimeError("replica_saturation_unavailable")
            placements = self._graph.stages[0].placements
            placement_by_node = {placement.node_id: placement for placement in placements}
            if (
                len(placement_by_node) < 2
                or set(request_counts_by_node) != set(placement_by_node)
                or any(
                    not isinstance(count, int)
                    or isinstance(count, bool)
                    or count < 1
                    or count > 64
                    for count in request_counts_by_node.values()
                )
                or not request_id_prefix
            ):
                raise ValueError("invalid_replica_saturation_request")
            prompt = tuple(token_ids)
            expected_output = tuple(expected_output_token_ids)
            if not prompt or not expected_output:
                raise ValueError("invalid_replica_saturation_request")

            started_at = time.monotonic()
            start_barrier = threading.Barrier(len(placement_by_node))

            def execute_node(node_id: str) -> list[dict[str, Any]]:
                placement = placement_by_node[node_id]
                excluded = [
                    candidate.placement_id
                    for candidate in placements
                    if candidate.placement_id != placement.placement_id
                ]
                records: list[dict[str, Any]] = []
                try:
                    start_barrier.wait()
                    for ordinal in range(request_counts_by_node[node_id]):
                        request_id = f"{request_id_prefix}-{node_id}-{ordinal}"
                        request = {
                            **self._plan["request"],
                            "request_id": request_id,
                            "prompt_token_ids": list(prompt),
                            "max_new_tokens": len(expected_output),
                            "expected_new_tokens": len(expected_output),
                        }
                        request_started = time.monotonic()
                        response = self._sessions[node_id].send(
                            command_id=self._command_id(
                                node_id, "replica-saturation-start"
                            ),
                            command="infer_start",
                            payload={
                                "request": request,
                                "excluded_placement_ids": excluded,
                            },
                        )
                        observation = self._verify_observation(
                            node_id, response, event="inference_started"
                        )
                        details = observation.get("details")
                        path = (
                            details.get("path")
                            if isinstance(details, Mapping)
                            else None
                        )
                        output = self._output_tokens(observation)
                        status = (
                            details.get("status")
                            if isinstance(details, Mapping)
                            else None
                        )
                        if (
                            not isinstance(path, Mapping)
                            or path.get("placement_ids") != [placement.placement_id]
                        ):
                            raise RuntimeError("replica_saturation_path_invalid")
                        while status == "DECODING" and len(output) < len(
                            expected_output
                        ):
                            response = self._sessions[node_id].send(
                                command_id=self._command_id(
                                    node_id, "replica-saturation-decode"
                                ),
                                command="infer_decode",
                                payload={"request_id": request_id, "count": 1},
                            )
                            observation = self._verify_observation(
                                node_id, response, event="inference_decoded"
                            )
                            next_output = self._output_tokens(observation)
                            if len(next_output) <= len(output):
                                raise RuntimeError("replica_saturation_decode_stalled")
                            output = next_output
                            status = observation["details"].get("status")
                        if output != expected_output:
                            raise RuntimeError("replica_saturation_token_mismatch")
                        records.append(
                            {
                                "request_id": request_id,
                                "node_id": node_id,
                                "path_id": path["path_id"],
                                "path_attempt": path["path_attempt"],
                                "placement_ids": [placement.placement_id],
                                "elapsed_ms": (
                                    time.monotonic() - request_started
                                )
                                * 1_000.0,
                                "output_token_count": len(output),
                                "output_digest": "sha256:"
                                + hashlib.sha256(
                                    json.dumps(
                                        list(output), separators=(",", ":")
                                    ).encode("utf-8")
                                ).hexdigest(),
                            }
                        )
                    return records
                except BaseException:
                    start_barrier.abort()
                    raise

            with ThreadPoolExecutor(
                max_workers=len(placement_by_node),
                thread_name_prefix="m18-saturation",
            ) as executor:
                futures = {
                    node_id: executor.submit(execute_node, node_id)
                    for node_id in sorted(placement_by_node)
                }
                records_by_node = {
                    node_id: future.result() for node_id, future in futures.items()
                }
            self._snapshot_all()
            elapsed_ms = (time.monotonic() - started_at) * 1_000.0
            request_count = sum(request_counts_by_node.values())
            return {
                "protocol": "mycelium.physical_replica_saturation.v1",
                "deployment_id": self._graph.deployment_id,
                "request_count": request_count,
                "elapsed_ms": elapsed_ms,
                "throughput_rps": request_count * 1_000.0 / elapsed_ms,
                "request_counts_by_node": dict(sorted(request_counts_by_node.items())),
                "tracks": [
                    {
                        "node_id": node_id,
                        "placement_id": placement_by_node[node_id].placement_id,
                        "request_count": len(records),
                        "request_elapsed_ms": [
                            record["elapsed_ms"] for record in records
                        ],
                    }
                    for node_id, records in records_by_node.items()
                ],
                "requests": [
                    record
                    for node_id in sorted(records_by_node)
                    for record in records_by_node[node_id]
                ],
                "route_ready": False,
            }

    def release_request(self, request_id: str) -> None:
        """Forget prompt/output token material after the gateway releases a request."""

        with self._lock:
            self._request_inputs.pop(request_id, None)
            self._request_outputs.pop(request_id, None)
            self._request_limits.pop(request_id, None)
            self._request_entry_nodes.pop(request_id, None)
            self._request_locks.pop(request_id, None)
            self._active_route_requests.pop(request_id, None)
            getattr(self, "_pending_publisher_generations", {}).pop(
                request_id,
                None,
            )
            self._request_cleanup_receipts.pop(request_id, None)

    def request_cleanup_receipt_scoped(
        self,
        request_id: str,
        *,
        request_attempt: int,
        path_id: str,
        path_attempt: int,
        path_digest: str,
        cleanup_owner_id: str,
    ) -> Mapping[str, Any] | None:
        """Read a cleanup receipt only for the owner's exact immutable subject."""

        expected = {
            "request_id": request_id,
            "request_attempt": request_attempt,
            "path_id": path_id,
            "path_attempt": path_attempt,
            "path_digest": path_digest,
            "cleanup_owner_id": cleanup_owner_id,
        }
        with self._lock:
            receipt = self._request_cleanup_receipts.get(request_id)
            if receipt is None or any(
                receipt.get(field) != value for field, value in expected.items()
            ):
                return None
            return dict(receipt)

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
        """Release only after exact cleanup proof; stale attempts cannot erase state."""

        receipt = self.request_cleanup_receipt_scoped(
            request_id,
            request_attempt=request_attempt,
            path_id=path_id,
            path_attempt=path_attempt,
            path_digest=path_digest,
            cleanup_owner_id=cleanup_owner_id,
        )
        if receipt is None:
            raise RuntimeError("request_scoped_release_cleanup_unproven")
        with self._lock:
            active = self._active_route_requests.get(request_id)
            if active is None or any(
                active.get(field) != value
                for field, value in (
                    ("request_attempt", request_attempt),
                    ("path_id", path_id),
                    ("path_attempt", path_attempt),
                    ("path_digest", path_digest),
                )
            ):
                raise RuntimeError("request_scoped_release_identity_mismatch")
            self._request_inputs.pop(request_id, None)
            self._request_outputs.pop(request_id, None)
            self._request_limits.pop(request_id, None)
            self._request_entry_nodes.pop(request_id, None)
            self._request_locks.pop(request_id, None)
            self._active_route_requests.pop(request_id, None)
            getattr(self, "_pending_publisher_generations", {}).pop(
                request_id,
                None,
            )
            self._request_cleanup_receipts.pop(request_id, None)

    def request_cleanup_receipt(self, request_id: str) -> Mapping[str, Any] | None:
        with self._lock:
            receipt = self._request_cleanup_receipts.get(request_id)
            return None if receipt is None else dict(receipt)

    def _peer_counters(self) -> dict[str, dict[str, int]]:
        counters: dict[str, dict[str, int]] = {}
        for node_id in sorted(self._peers):
            observation = self._last_snapshots.get(node_id, {})
            details = observation.get("details", {})
            transport = details.get("transport", {})
            runtime = details.get("runtime", {})
            counters[node_id] = {
                "frames_sent": int(transport.get("remote_frames_sent", 0)),
                "frames_received": int(transport.get("remote_frames_received", 0)),
                "applied_operation_count": int(
                    runtime.get("applied_operation_count", 0)
                ),
            }
        return counters

    def counters(self) -> RouteCounters:
        frames_sent = 0
        frames_received = 0
        applied_operations = 0
        for observation in self._last_snapshots.values():
            details = observation.get("details", {})
            transport = details.get("transport")
            runtime = details.get("runtime")
            if isinstance(transport, Mapping):
                frames_sent += int(transport.get("remote_frames_sent", 0))
                frames_received += int(transport.get("remote_frames_received", 0))
            if isinstance(runtime, Mapping):
                applied_operations += int(runtime.get("applied_operation_count", 0))
        return RouteCounters(
            frames_sent=frames_sent,
            frames_received=frames_received,
            applied_operation_count=applied_operations,
            fatal=self._fatal,
        )

    def a4_liveness_status(self) -> Mapping[str, Any]:
        """Return the detector-owned privacy-reduced liveness projection."""

        from mycelium_live.a4_contracts import validate_traffic_liveness

        snapshots = self._liveness.snapshots()
        incidents = self._liveness.incidents()
        return validate_traffic_liveness({
            "protocol": "mycelium.traffic_liveness.v1",
            "deployment_id": self._graph.deployment_id,
            "generated_at_monotonic_ms": int(time.monotonic() * 1_000),
            "subjects": [
                {
                    "subject_id": item.identity.subject_id,
                    "kind": item.identity.kind.value,
                    "membership_generation": item.identity.membership_generation,
                    "state": item.state.value,
                    "last_fresh_ms": item.last_fresh_ms,
                    "last_observed_ms": item.last_observed_ms,
                    "next_keepalive_due_ms": item.next_keepalive_due_ms,
                    "consecutive_misses": item.consecutive_misses,
                    "last_source": item.last_source.value,
                }
                for item in snapshots
            ],
            "incidents": [
                {
                    "sequence": item.sequence,
                    "source": item.source.value,
                    "scope": item.scope,
                    "subject_id": item.subject.subject_id,
                    "membership_generation": item.subject.membership_generation,
                    "observed_at_ms": item.observed_at_ms,
                    "affected_track_ids": list(item.affected_track_ids),
                    "action": item.action,
                    "outcome": item.outcome,
                    "detection_latency_ms": item.detection_latency_ms,
                    "within_detection_budget": item.within_detection_budget,
                }
                for item in incidents
            ],
            "deployment_fatal_reason": self._liveness.deployment_fatal_reason,
        })

    def _handle_lost_peer_processes(self) -> None:
        """Interrupt only active requests whose physical participant exited."""

        observed_at_s = time.monotonic()
        with self._lock:
            if not self._open or self._closed:
                return
            lost = tuple(
                node_id
                for node_id, session in sorted(self._sessions.items())
                if session.returncode is not None
            )
        for node_id in lost:
            with self._lock:
                active_tracks = tuple(
                    request_id
                    for request_id, active in self._active_route_requests.items()
                    if active.get("terminal") is not True
                    and active.get("cancellation_requested") is not True
                    and node_id in active["participating_node_ids"]
                )
            subject = self._liveness_subjects.get(node_id)
            if subject is not None:
                snapshot = self._liveness.subject_snapshot(subject)
                if snapshot is not None and snapshot.state.value not in {
                    "failed",
                    "quarantined",
                }:
                    now_ms = int(observed_at_s * 1_000)
                    if active_tracks:
                        self._liveness.record_active_failure(
                            subject,
                            failure_started_at_ms=now_ms,
                            observed_at_ms=now_ms,
                            scope="peer",
                            affected_track_ids=active_tracks,
                            verified=True,
                        )
                    else:
                        self._liveness.record_nonparticipating_peer_exit(
                            subject,
                            observed_at_ms=now_ms,
                        )
            interruption_deadline = observed_at_s + 2.0
            for request_id in active_tracks:
                try:
                    self.cancel_request(
                        request_id,
                        deadline_monotonic_s=interruption_deadline,
                    )
                except BaseException:
                    # The request worker remains responsible for fail-closed
                    # terminal publication and exact cleanup proof.  A dead
                    # fanout leaf must not prevent interruption of live peers.
                    pass
            self._record_incident(
                state="peer_process_lost",
                reason="route_peer_process_lost",
                request_id=None,
            )

    def public_status(self) -> Mapping[str, Any]:
        """Return a bounded, prompt-free projection of the running route."""

        self._handle_lost_peer_processes()
        with self._lock:
            peer_counters = self._peer_counters()
            stages: list[dict[str, Any]] = []
            placements_by_node: dict[str, list[dict[str, Any]]] = {
                node_id: [] for node_id in self._peers
            }
            decode_modes: set[str] = set()
            for stage in self._graph.stages:
                for placement in stage.placements:
                    projected = {
                        "stage_id": stage.stage_id,
                        "placement_id": placement.placement_id,
                        "node_id": placement.node_id,
                        "runtime_backend": placement.runtime_backend,
                        "start_layer": stage.layer_range.start_layer,
                        "end_layer_exclusive": (stage.layer_range.end_layer_exclusive),
                        "component_roles": list(stage.component_roles),
                    }
                    stages.append(projected)
                    placements_by_node.setdefault(placement.node_id, []).append(
                        projected
                    )
            peers: list[dict[str, Any]] = []
            for node_id in sorted(self._peers):
                observation = self._last_snapshots.get(node_id, {})
                details = observation.get("details", {})
                health_observation = self._last_health.get(node_id, {})
                health_details = health_observation.get("details", {})
                sidecar_process = (
                    health_details.get("sidecar_process")
                    if isinstance(health_details, Mapping)
                    else None
                )
                runtime = details.get("runtime", {})
                interruptibility = details.get("interruptibility")
                resources = details.get("host_resources", {})
                mode = runtime.get("mode")
                if isinstance(mode, str):
                    decode_modes.add(mode)
                release_counts = runtime.get("release_counts", {})
                architecture = runtime.get("architecture")
                architecture_modes = (
                    resources.get("decode_modes_by_architecture", {})
                    if isinstance(resources, Mapping)
                    else {}
                )
                supported_modes = (
                    architecture_modes.get(architecture, [])
                    if isinstance(architecture_modes, Mapping)
                    and isinstance(architecture, str)
                    else []
                )
                peers.append(
                    {
                        "node_id": node_id,
                        "placements": placements_by_node.get(node_id, []),
                        **peer_counters[node_id],
                        "decode_mode": mode if isinstance(mode, str) else None,
                        "architecture": (
                            architecture if isinstance(architecture, str) else None
                        ),
                        "supported_decode_modes": (
                            [str(value) for value in supported_modes]
                            if isinstance(supported_modes, (list, tuple))
                            else []
                        ),
                        "data_plane_health_observed": bool(health_observation),
                        "sidecar_process_alive": (
                            sidecar_process.get("alive")
                            if isinstance(sidecar_process, Mapping)
                            and type(sidecar_process.get("alive")) is bool
                            else None
                        ),
                        "transport_running": (
                            health_details.get("transport_running")
                            if isinstance(health_details, Mapping)
                            and type(health_details.get("transport_running")) is bool
                            else None
                        ),
                        "transport_fatal": bool(
                            isinstance(health_details, Mapping)
                            and health_details.get("transport_fatal_error") is not None
                        ),
                        "active_kv_state_count": int(
                            runtime.get("active_state_count", 0)
                        ),
                        "active_kv_bytes": int(runtime.get("active_kv_bytes", 0)),
                        "peak_kv_bytes": int(runtime.get("peak_kv_bytes", 0)),
                        "prefill_operation_count": int(
                            runtime.get("prefill_operation_count", 0)
                        ),
                        "prefill_input_token_count": int(
                            runtime.get("prefill_input_token_count", 0)
                        ),
                        "decode_operation_count": int(
                            runtime.get("decode_operation_count", 0)
                        ),
                        "decode_input_token_count": int(
                            runtime.get("decode_input_token_count", 0)
                        ),
                        "activation_output_bytes": int(
                            runtime.get("activation_output_bytes", 0)
                        ),
                        "current_position": (
                            int(runtime["current_position"])
                            if type(runtime.get("current_position")) is int
                            else None
                        ),
                        "release_state": str(runtime.get("release_state", "unknown")),
                        "last_release_reason": (
                            str(runtime["last_release_reason"])
                            if isinstance(runtime.get("last_release_reason"), str)
                            else None
                        ),
                        "retained_result_count": int(
                            runtime.get("retained_result_count", 0)
                        ),
                        "release_counts": (
                            {
                                str(reason): int(count)
                                for reason, count in sorted(release_counts.items())
                            }
                            if isinstance(release_counts, Mapping)
                            else {}
                        ),
                        "interruptibility": (
                            dict(interruptibility)
                            if isinstance(interruptibility, Mapping)
                            else None
                        ),
                    }
                )
            identity_material = json.dumps(
                {
                    "deployment_id": self._plan["deployment_id"],
                    "topology_version": self._graph.topology_version,
                    "endpoints": [
                        self._endpoints[node_id]["endpoint_id"]
                        for node_id in sorted(self._endpoints)
                    ],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            aggregate = self.counters()
            status = {
                "protocol": "mycelium.live_route_status.v1",
                "route_alive": self.is_alive(),
                "simulated": False,
                "route_identity_digest": (
                    "sha256:" + hashlib.sha256(identity_material).hexdigest()
                ),
                "deployment_id": self._plan["deployment_id"],
                "model_id": self._graph.model_id,
                "topology_version": self._graph.topology_version,
                "decode_mode": (
                    next(iter(decode_modes)) if len(decode_modes) == 1 else "mixed"
                ),
                "counters": {
                    "frames_sent": aggregate.frames_sent,
                    "frames_received": aggregate.frames_received,
                    "applied_operation_count": (aggregate.applied_operation_count),
                    "fatal": aggregate.fatal,
                },
                "stages": stages,
                "peers": peers,
                "recent_inferences": list(self._recent_inferences),
                "incidents": list(self._incidents),
                "liveness": self.a4_liveness_status(),
                "concurrency_liveness_qualification": (
                    dict(self._a4_qualification)
                    if self._a4_qualification is not None
                    else self._unqualified_a4_status()
                ),
            }
            if self._placement_projection is not None:
                status["placement"] = json.loads(json.dumps(self._placement_projection))
            if self._topology_projection is not None:
                status["topology"] = json.loads(json.dumps(self._topology_projection))
            return status

    def _unqualified_a4_status(self) -> dict[str, Any]:
        return {
            "protocol": "mycelium.product_concurrency_liveness_qualification.v1",
            "deployment_id": self._graph.deployment_id,
            "qualification_digest": "sha256:"
            + hashlib.sha256(b"a4-physical-proof-unavailable").hexdigest(),
            "maximum_concurrent_requests": 4,
            "cancellation_and_cleanup_bound_ms": 2_000,
            "cooperative_interruption_proven": False,
            "request_scoped_cleanup_proven": False,
            "shared_process_termination_used": False,
            "publisher_generation_fencing_proven": False,
            "scoped_liveness_proven": False,
            "eligible": False,
            "evidence_digest": "sha256:"
            + hashlib.sha256(b"a4-physical-evidence-unavailable").hexdigest(),
        }

    def set_a4_qualification(self, document: Mapping[str, Any]) -> None:
        """Install only an externally sealed physical A4 qualification record."""

        from mycelium_live.a4_contracts import validate_product_qualification
        from mycelium_request_gateway.contracts import qualification_digest

        validated = validate_product_qualification(document)
        current = self._deployment_qualification
        if (
            validated["deployment_id"] != self._graph.deployment_id
            or validated["eligible"] is not True
            or current is None
            or validated["qualification_digest"] != qualification_digest(current)
        ):
            raise ValueError("a4_qualification_binding_mismatch")
        with self._lock:
            self._a4_qualification = dict(validated)

    def membership_status(self, *, qualification: Any | None) -> Mapping[str, Any]:
        """Project durable members independently from execution-graph placement."""

        authority = self._seed_authority
        if authority is None:
            raise LiveSeedStateError("live_seed_authority_unavailable")
        now = time.time()
        members = authority.current_members()
        placed = {
            placement.node_id
            for stage in self._graph.stages
            for placement in stage.placements
        }
        native_nodes: list[dict[str, Any]] = []
        browser_workers: list[dict[str, Any]] = []
        route_alive = self.is_alive()
        route_qualified = (
            qualification is not None
            and getattr(qualification, "route_ready", False) is True
            and route_alive
        )
        for member in members:
            live_lease = float(member["lease_expires_at"]) > now
            peer_class = member["peer_class"]
            if peer_class in {"browser_http", "pixel_http"}:
                browser_workers.append(
                    {
                        "peer_id": member["node_id"],
                        "capability": "synthetic_browser_probe",
                        "state": "ready" if live_lease else "stale",
                        "expires_at_unix_ms": int(
                            float(member["lease_expires_at"]) * 1_000
                        ),
                    }
                )
                continue
            if not live_lease:
                membership_state = "revoked"
                connectivity = "stale"
            elif member["node_id"] not in placed:
                membership_state = "reachable"
                connectivity = "unknown"
            elif route_qualified:
                membership_state = "qualified"
                connectivity = "unknown"
            elif route_alive:
                membership_state = "assigned"
                connectivity = "unknown"
            else:
                membership_state = "reachable"
                connectivity = "unknown"
            native_nodes.append(
                {
                    "member_id": member["node_id"],
                    "capability": "native_inference_node",
                    "membership_state": membership_state,
                    "connectivity": connectivity,
                    "endpoint_id": None,
                }
            )
        return {
            "protocol": "mycelium.product_ui.swarm.v1",
            "native_nodes": native_nodes,
            "browser_workers": browser_workers,
        }

    def mint_native_invite(
        self,
        *,
        seed_url: str,
        ttl_seconds: int,
        nonce: str,
    ) -> Mapping[str, Any]:
        """Mint a target-device bundle from the live durable seed authority."""

        authority = self._seed_authority
        if authority is None:
            raise LiveSeedStateError("live_seed_authority_unavailable")
        authority.state_root.revalidate()
        return mint_invite_bundle(
            signer=authority.signer,
            swarm_id=authority.swarm_id,
            seed_url=seed_url,
            ttl_seconds=ttl_seconds,
            nonce=nonce,
            issued_at=time.time(),
        )

    def revoke_native_member(self, member_id: str) -> Mapping[str, Any]:
        """Fence a standby member without silently invalidating an active route."""

        authority = self._seed_authority
        if authority is None:
            raise LiveSeedStateError("live_seed_authority_unavailable")
        placed = {
            placement.node_id
            for stage in self._graph.stages
            for placement in stage.placements
        }
        if member_id in placed and self.is_alive():
            raise LiveSeedStateError("member_in_active_route")
        member = next(
            (
                item
                for item in authority.current_members()
                if item["node_id"] == member_id
            ),
            None,
        )
        if member is None:
            raise LiveSeedStateError("member_unknown")
        try:
            return revoke_seed_member(
                authority.state_root.path,
                node_id=member_id,
                expected_generation=int(member["generation"]),
                reason="product-ui-owner-revocation",
            )
        except SeedOperatorError as exc:
            raise LiveSeedStateError(exc.code) from exc

    def product_membership_records(self) -> tuple[dict[str, Any], ...]:
        authority = self._seed_authority
        if authority is None:
            raise LiveSeedStateError("live_seed_authority_unavailable")
        return authority.current_members()

    def product_assignment_records(self) -> tuple[dict[str, Any], ...]:
        """Return privacy-reduced assignment/load bindings from the validated plan."""

        snapshot = self._membership_snapshot
        if snapshot is None:
            raise LiveSeedStateError("live_assignment_authority_unavailable")
        offers = snapshot.get("assignment_offers")
        if not isinstance(offers, list):
            raise LiveSeedStateError("live_assignment_authority_unavailable")
        placement_by_assignment = {
            placement.assignment_id: (
                placement.placement_id,
                placement.load_proof_digest,
            )
            for stage in self._graph.stages
            for placement in stage.placements
        }
        records: list[dict[str, Any]] = []
        for envelope in offers:
            message = envelope.get("message") if isinstance(envelope, Mapping) else None
            if not isinstance(message, Mapping):
                raise LiveSeedStateError("live_assignment_authority_unavailable")
            assignment_id = message.get("assignment_id")
            placement = placement_by_assignment.get(assignment_id)
            if placement is None:
                raise LiveSeedStateError("live_assignment_binding_mismatch")
            stage_id, load_proof_digest = placement
            records.append(
                {
                    "assignment_id": assignment_id,
                    "node_id": message.get("recipient_node_id"),
                    "stage_id": stage_id,
                    "membership_generation": message.get("generation"),
                    "load_generation": message.get("load_generation"),
                    "assignment_digest": message.get("assignment_digest"),
                    "stage_pack_digest": message.get("stage_pack_digest"),
                    "load_proof_digest": load_proof_digest,
                }
            )
        return tuple(records)

    def product_pseudonym_salt(self) -> bytes:
        authority = self._seed_authority
        if authority is None:
            raise LiveSeedStateError("live_seed_authority_unavailable")
        return authority.product_pseudonym_salt()

    def live_attestation(self, *, request_id: str) -> dict[str, Any]:
        if request_id not in self._request_outputs or not self.is_alive():
            raise RuntimeError("live_attestation_unavailable")
        selected: dict[tuple[str, str], dict[str, Any]] = {}
        request_observations: list[dict[str, Any]] = []
        for envelope in self._signed_observations:
            observation = envelope.get("observation")
            if not isinstance(observation, Mapping):
                continue
            node_id = observation.get("node_id")
            event = observation.get("event")
            details = observation.get("details")
            if not isinstance(node_id, str) or not isinstance(event, str):
                continue
            if event in {"configured", "started", "snapshot"}:
                selected[(node_id, event)] = envelope
            elif (
                event in {"inference_started", "inference_decoded"}
                and isinstance(details, Mapping)
                and details.get("request_id") == request_id
            ):
                request_observations.append(envelope)
        required = {
            (node_id, event)
            for node_id in self._sessions
            for event in ("configured", "started", "snapshot")
        }
        if set(selected) != required or not request_observations:
            raise RuntimeError("live_attestation_observation_window_incomplete")
        signed_observations = [
            *(
                selected[(node_id, event)]
                for event in ("configured", "started")
                for node_id in sorted(self._sessions)
            ),
            *request_observations,
            *(selected[(node_id, "snapshot")] for node_id in sorted(self._sessions)),
        ]
        return {
            "protocol": "mycelium.live_route_attestation.v1",
            "captured_at_unix_ms": int(time.time() * 1_000),
            "run_id": self._plan["run_id"],
            "entry_node_id": self._request_entry_nodes.get(
                request_id, self._plan["entry_node_id"]
            ),
            "request_id": request_id,
            "prompt_token_ids": list(self._request_inputs[request_id]),
            "max_new_tokens": self._request_limits[request_id],
            "execution_graph": execution_graph_to_dict(self._graph),
            "output_token_ids": list(self._request_outputs[request_id]),
            "signed_observations": json.loads(json.dumps(signed_observations)),
            "counters": {
                "frames_sent": self.counters().frames_sent,
                "frames_received": self.counters().frames_received,
                "applied_operation_count": self.counters().applied_operation_count,
                "fatal": self.counters().fatal,
            },
        }

    def process_id(self, node_id: str) -> int:
        """Expose a verified worker PID for bounded physical fault injection."""
        with self._lock:
            identity = self._identities.get(node_id)
            process_id = None if identity is None else identity.get("process_id")
            if (
                not isinstance(process_id, int)
                or isinstance(process_id, bool)
                or process_id <= 0
            ):
                raise RuntimeError("peer_process_identity_unavailable")
            return process_id

    def diagnostic_stderr(self, node_id: str) -> str:
        """Return a bounded worker traceback for a failed qualification run."""

        with self._lock:
            session = self._sessions.get(node_id)
            if session is None:
                return ""
            return session.stderr.decode("utf-8", errors="replace")[-4_096:]

    def is_alive(self) -> bool:
        return (
            self._open
            and not self._closed
            and self._fatal is None
            and bool(self._sessions)
            and all(session.returncode is None for session in self._sessions.values())
        )

    def close(self) -> None:
        self._liveness_monitor_stop.set()
        monitor = self._liveness_monitor_thread
        if monitor is not None and monitor is not threading.current_thread():
            monitor.join(timeout=1.0)
        with self._lock:
            if self._closed:
                return
            for node_id, session in tuple(self._sessions.items()):
                if session.returncode is None:
                    try:
                        session.send(
                            command_id=self._command_id(node_id, "stop"),
                            command="stop",
                            payload={},
                        )
                    except BaseException:
                        pass
                try:
                    session.close()
                except BaseException:
                    pass
            self._open = False
            self._closed = True
            if self._seed_authority is not None:
                try:
                    self._seed_authority.close()
                finally:
                    self._seed_authority = None

    def cleanup(self) -> None:
        """Remove only digest-bound staging roots after every process is closed."""
        with self._lock:
            if self._open or any(
                session.returncode is None for session in self._sessions.values()
            ):
                raise RuntimeError("route_cleanup_requires_closed_processes")
            try:
                self._controller.execute("cleanup")
            except BaseException as exc:
                self._fatal = getattr(exc, "code", type(exc).__name__)
                raise


__all__ = [
    "FakeLiveRoute",
    "InferenceCancelled",
    "InferenceResult",
    "LiveRoute",
    "LiveSeedAuthority",
    "LiveSeedStateError",
    "PhysicalLiveRoute",
    "RouteCounters",
    "RouteIdentity",
    "TokenSink",
]
