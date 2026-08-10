"""Lifecycle seam for a physical inference route that outlives one request."""
from __future__ import annotations

from collections import deque
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
from mycelium_node.process import PrivateDirectoryLease, private_directory_lease
from mycelium_seed.authority import (
    PRODUCT_PSEUDONYM_KEY_FILE,
    SeedAuthorityError,
    derive_product_pseudonym_salt,
    load_bound_seed_signer,
    load_product_pseudonym_salt,
)
from mycelium_seed.state import SeedStateError, SqliteSeedState
from mycelium_qualification.signing import Ed25519EvidenceSigner
from mycelium_router.contracts import ExecutionGraph
from mycelium_router.serialization import execution_graph_to_dict
from physical_inference_node import execution_graph_from_document
from physical_inference_qualification import (
    NODE_COMMAND_TIMEOUT_SECONDS,
    NODE_SESSION_TIMEOUT_SECONDS,
    NodeProcessSession,
    PeerIdentity,
    QualificationController,
    _peer_process_argv,
)


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
        if authority_generation < 1 or str(authority_generation) != binding[
            "authority_generation"
        ]:
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
                pseudonym_salt = load_product_pseudonym_salt(
                    lease.path / "identity"
                )
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
                        "frames_received": after.frames_received - before.frames_received,
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
    for envelope in offers:
        message = envelope.get("message") if isinstance(envelope, Mapping) else None
        recipient = (
            message.get("recipient_node_id") if isinstance(message, Mapping) else None
        )
        if not isinstance(recipient, str) or not recipient or recipient in recipients:
            raise ValueError("membership_snapshot_invalid")
        recipients.append(recipient)
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
                "endpoint_id": other["endpoint_id"],
                "deployment_epoch": message["deployment_epoch"],
                "membership_generation": other["generation"],
                "valid_from": now,
                "valid_until": valid_until,
            }
            for node_id, other in sorted(route_members.items())
            if node_id != recipient
        ]
        refreshed_offers.append(
            sign_membership_message(signer=signer, message=message)
        )
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
        session_factory=NodeProcessSession,
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
        self._request_inputs: dict[str, tuple[int, ...]] = {}
        self._request_outputs: dict[str, tuple[int, ...]] = {}
        self._request_limits: dict[str, int] = {}
        self._recent_inferences: deque[dict[str, Any]] = deque(maxlen=64)
        self._incidents: deque[dict[str, Any]] = deque(maxlen=64)
        self._incident_sequence = 0
        self._stop_token_ids: frozenset[int] = frozenset()
        self._command_sequence = 0
        self._open = False
        self._closed = False
        self._fatal: str | None = None
        self._lock = threading.RLock()

    def _record_incident(
        self,
        *,
        state: str,
        reason: str,
        request_id: str | None,
    ) -> None:
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
            peers = tuple(
                PeerIdentity(**item) for item in controller_document["peers"]
            )
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
        self._command_sequence += 1
        return f"{node_id}-{operation}-{self._command_sequence}"

    @property
    def execution_graph(self) -> ExecutionGraph:
        """Return the graph bound into every configured process."""
        return self._graph

    def set_stop_token_ids(self, token_ids: Sequence[int]) -> None:
        """Stop future generations before emitting a model terminator token."""

        if any(
            not isinstance(token_id, int)
            or isinstance(token_id, bool)
            or token_id < 0
            for token_id in token_ids
        ):
            raise ValueError("invalid_stop_token_ids")
        self._stop_token_ids = frozenset(token_ids)

    def set_public_projections(
        self,
        *,
        placement: Mapping[str, Any] | None,
        topology: Mapping[str, Any] | None,
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
        return _peer_process_argv(peer, command)

    def _verify_observation(
        self,
        node_id: str,
        response: Mapping[str, Any],
        *,
        event: str,
        require_known_key: bool = True,
    ) -> dict[str, Any]:
        return self._controller._verified_observation(
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
        snapshots: dict[str, dict[str, Any]] = {}
        for node_id in sorted(self._sessions):
            response = self._sessions[node_id].send(
                command_id=self._command_id(node_id, "snapshot"),
                command="snapshot",
                payload={},
            )
            observation = self._verify_observation(
                node_id, response, event="snapshot"
            )
            snapshots[node_id] = observation
            fatal = observation["details"].get("transport_fatal_error")
            if fatal is not None:
                self._fatal = str(fatal.get("code", "transport_fatal"))
        self._last_snapshots = snapshots

    def _cancellation_cleanup_complete(self) -> bool:
        if set(self._last_snapshots) != set(self._sessions):
            return False
        for observation in self._last_snapshots.values():
            details = observation.get("details")
            if not isinstance(details, Mapping):
                return False
            runtime = details.get("runtime")
            if (
                not isinstance(runtime, Mapping)
                or runtime.get("active_state_count") != 0
                or details.get("transport_pending_delivery_count") != 0
                or details.get("transport_cancellation_cleanup_complete") is not True
            ):
                return False
        return True

    def _wait_for_cancellation_cleanup(self) -> None:
        deadline = time.monotonic() + 5.0
        while True:
            self._snapshot_all()
            if self._cancellation_cleanup_complete():
                return
            if time.monotonic() >= deadline:
                raise RuntimeError("route_cancellation_cleanup_timeout")
            time.sleep(0.02)

    def infer(
        self,
        token_ids: Sequence[int],
        *,
        max_new_tokens: int,
        request_id: str,
        sink: TokenSink,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> InferenceResult:
        with self._lock:
            if not self.is_alive():
                raise RuntimeError("route_not_open")
            if (
                not isinstance(max_new_tokens, int)
                or isinstance(max_new_tokens, bool)
                or max_new_tokens < 1
                or not isinstance(request_id, str)
                or not request_id
            ):
                raise ValueError("invalid_inference_request")
            entry_node_id = self._plan["entry_node_id"]
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
            try:
                started_response = self._sessions[entry_node_id].send(
                    command_id=self._command_id(entry_node_id, "infer-start"),
                    command="infer_start",
                    payload={"request": request},
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
                cancelled_for_request = (
                    cancel_requested is not None
                    and cancel_requested()
                    and status == "DECODING"
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
                    decoded_response = self._sessions[entry_node_id].send(
                        command_id=self._command_id(entry_node_id, "infer-decode"),
                        command="infer_decode",
                        payload={"request_id": request_id, "count": 1},
                    )
                    decoded = self._verify_observation(
                        entry_node_id,
                        decoded_response,
                        event="inference_decoded",
                    )
                    next_output = self._output_tokens(decoded)
                    status = decoded["details"].get("status")
                    cancelled_for_request = (
                        cancel_requested is not None
                        and cancel_requested()
                        and status == "DECODING"
                    )
                    if len(next_output) <= len(output):
                        if status == "COMPLETED":
                            break
                        raise RuntimeError("route_decode_stalled")
                    output = truncate_at_stop(next_output)
                    if not cancelled_for_request:
                        emit_new_tokens(output)
                cancelled_for_stop = stopped and status == "DECODING"
                if cancelled_for_stop or cancelled_for_request:
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
                output = output[:max_new_tokens]
                if cancelled_for_stop or cancelled_for_request:
                    self._wait_for_cancellation_cleanup()
                else:
                    self._snapshot_all()
                if self._fatal is not None:
                    raise RuntimeError(self._fatal)
                if cancelled_for_request:
                    raise InferenceCancelled("inference_cancelled")
            except InferenceCancelled:
                raise
            except BaseException as exc:
                self._fatal = getattr(
                    exc,
                    "remote_code",
                    getattr(exc, "code", str(exc) or type(exc).__name__),
                )
                self._record_incident(
                    state="route_failed_closed",
                    reason=str(self._fatal),
                    request_id=request_id,
                )
                raise

            self._request_outputs[request_id] = output
            self._request_inputs[request_id] = tuple(token_ids)
            self._request_limits[request_id] = max_new_tokens
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

    def release_request(self, request_id: str) -> None:
        """Forget prompt/output token material after the gateway releases a request."""

        with self._lock:
            self._request_inputs.pop(request_id, None)
            self._request_outputs.pop(request_id, None)
            self._request_limits.pop(request_id, None)

    def _peer_counters(self) -> dict[str, dict[str, int]]:
        counters: dict[str, dict[str, int]] = {}
        for node_id in sorted(self._peers):
            observation = self._last_snapshots.get(node_id, {})
            details = observation.get("details", {})
            transport = details.get("transport", {})
            runtime = details.get("runtime", {})
            counters[node_id] = {
                "frames_sent": int(transport.get("remote_frames_sent", 0)),
                "frames_received": int(
                    transport.get("remote_frames_received", 0)
                ),
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

    def public_status(self) -> Mapping[str, Any]:
        """Return a bounded, prompt-free projection of the running route."""

        with self._lock:
            lost_process = next(
                (
                    node_id
                    for node_id, session in sorted(self._sessions.items())
                    if session.returncode is not None
                ),
                None,
            )
            if self._open and not self._closed and lost_process is not None:
                self._fatal = self._fatal or "route_peer_process_lost"
                self._record_incident(
                    state="route_failed_closed",
                    reason=self._fatal,
                    request_id=None,
                )
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
                        "end_layer_exclusive": (
                            stage.layer_range.end_layer_exclusive
                        ),
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
                runtime = details.get("runtime", {})
                mode = runtime.get("mode")
                if isinstance(mode, str):
                    decode_modes.add(mode)
                release_counts = runtime.get("release_counts", {})
                peers.append(
                    {
                        "node_id": node_id,
                        "placements": placements_by_node.get(node_id, []),
                        **peer_counters[node_id],
                        "decode_mode": mode if isinstance(mode, str) else None,
                        "active_kv_state_count": int(
                            runtime.get("active_state_count", 0)
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
                    next(iter(decode_modes))
                    if len(decode_modes) == 1
                    else "mixed"
                ),
                "counters": {
                    "frames_sent": aggregate.frames_sent,
                    "frames_received": aggregate.frames_received,
                    "applied_operation_count": (
                        aggregate.applied_operation_count
                    ),
                    "fatal": aggregate.fatal,
                },
                "stages": stages,
                "peers": peers,
                "recent_inferences": list(self._recent_inferences),
                "incidents": list(self._incidents),
            }
            if self._placement_projection is not None:
                status["placement"] = json.loads(
                    json.dumps(self._placement_projection)
                )
            if self._topology_projection is not None:
                status["topology"] = json.loads(json.dumps(self._topology_projection))
            return status

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
            placement.assignment_id: (stage.stage_id, placement.load_proof_digest)
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
        return {
            "protocol": "mycelium.live_route_attestation.v1",
            "captured_at_unix_ms": int(time.time() * 1_000),
            "run_id": self._plan["run_id"],
            "entry_node_id": self._plan["entry_node_id"],
            "request_id": request_id,
            "prompt_token_ids": list(self._request_inputs[request_id]),
            "max_new_tokens": self._request_limits[request_id],
            "execution_graph": execution_graph_to_dict(self._graph),
            "output_token_ids": list(self._request_outputs[request_id]),
            "signed_observations": json.loads(
                json.dumps(self._signed_observations)
            ),
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
            if not isinstance(process_id, int) or isinstance(process_id, bool) or process_id <= 0:
                raise RuntimeError("peer_process_identity_unavailable")
            return process_id

    def is_alive(self) -> bool:
        return (
            self._open
            and not self._closed
            and self._fatal is None
            and bool(self._sessions)
            and all(session.returncode is None for session in self._sessions.values())
        )

    def close(self) -> None:
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
