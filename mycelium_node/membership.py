# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stateful signed membership session for one physical node agent."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import hashlib
import math
import re
import threading
import time
from typing import Any
import uuid

from mycelium_membership import (
    ASSIGNMENT_OFFER_PROTOCOL,
    ASSIGNMENT_RESULT_PROTOCOL,
    CAPABILITY_REPORT_PROTOCOL,
    DRAIN_ACK_PROTOCOL,
    HEARTBEAT_PROTOCOL,
    JOIN_ACCEPTANCE_PROTOCOL,
    JOIN_REQUEST_PROTOCOL,
    LEASE_RENEWAL_PROTOCOL,
    LINK_PROBE_REPORT_PROTOCOL,
    MAX_MESSAGE_TTL_SECONDS,
    MembershipContractError,
    sign_membership_message,
    validate_peer_runtime_capability,
    verify_membership_message,
)
from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_qualification.signing import Ed25519EvidenceSigner
from mycelium_router.transports.iroh import (
    DELIVERY_SEMANTICS,
    DeliveryReceipt,
)
from mycelium_router.wire import ROUTER_WIRE_PROTOCOL


_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_REPLAY_IDS = 4096
_LIFECYCLE_STATES = frozenset(
    {"NEW", "CONFIGURED", "RUNNING", "DRAINING", "STOPPING", "STOPPED"}
)


class NodeMembershipError(RuntimeError):
    """Stable relational or lifecycle failure for a node membership session."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _segment(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SEGMENT_RE.fullmatch(value):
        raise ValueError(f"{field} is invalid")
    return value


def _finite_now(value: Any) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise NodeMembershipError("membership_clock_invalid")
    return float(value)


def validate_heartbeat_shape(
    *,
    lifecycle_state: Any,
    active_requests: Any,
    route_ready: Any,
    liveness_source: Any,
    activity_receipt_digest: Any,
    activity_peer_node_id: Any,
) -> None:
    """Validate an unsigned, side-effect-free liveness message shape."""

    if (
        not isinstance(lifecycle_state, str)
        or lifecycle_state not in _LIFECYCLE_STATES
    ):
        raise ValueError("heartbeat lifecycle state is invalid")
    if (
        not isinstance(active_requests, int)
        or isinstance(active_requests, bool)
        or active_requests < 0
    ):
        raise ValueError("heartbeat active requests is invalid")
    if route_ready is not False:
        raise ValueError("heartbeat route readiness is invalid")
    if (
        type(liveness_source) is not str
        or liveness_source not in {"scheduled_heartbeat", "activation_receipt"}
    ):
        raise ValueError("heartbeat liveness source is invalid")
    if liveness_source == "scheduled_heartbeat":
        valid_activity = (
            activity_receipt_digest is None
            and activity_peer_node_id is None
        )
    else:
        valid_activity = (
            isinstance(activity_receipt_digest, str)
            and _DIGEST_RE.fullmatch(activity_receipt_digest) is not None
            and isinstance(activity_peer_node_id, str)
            and _SEGMENT_RE.fullmatch(activity_peer_node_id) is not None
        )
    if not valid_activity:
        raise ValueError("heartbeat activity shape is invalid")


class NodeMembershipSession:
    """One node's join, lease, replay, and signed telemetry state."""

    def __init__(
        self,
        *,
        node_id: str,
        swarm_id: str,
        seed_node_id: str,
        signer: Ed25519EvidenceSigner,
        incarnation: str,
        software_version: str,
        peer_class: str,
        runtime_capability: Mapping[str, Any],
        clock: Callable[[], float] = time.time,
        id_source: Callable[[], str] = lambda: str(uuid.uuid4()),
        message_ttl_seconds: float = 60.0,
    ) -> None:
        self.node_id = _segment(node_id, "node_id")
        self.swarm_id = _segment(swarm_id, "swarm_id")
        self.seed_node_id = _segment(seed_node_id, "seed_node_id")
        self.incarnation = _segment(incarnation, "incarnation")
        if not isinstance(signer, Ed25519EvidenceSigner):
            raise ValueError("signer is invalid")
        if not isinstance(software_version, str) or not software_version.strip():
            raise ValueError("software_version is invalid")
        if not callable(clock) or not callable(id_source):
            raise ValueError("clock and id_source must be callable")
        if (
            isinstance(message_ttl_seconds, bool)
            or not isinstance(message_ttl_seconds, (int, float))
            or not math.isfinite(float(message_ttl_seconds))
            or message_ttl_seconds <= 0
            or message_ttl_seconds > MAX_MESSAGE_TTL_SECONDS
        ):
            raise ValueError("message_ttl_seconds is invalid")
        try:
            validated_runtime = validate_peer_runtime_capability(
                peer_class,
                runtime_capability,
            )
        except MembershipContractError as exc:
            raise ValueError(exc.code) from exc
        self.signer = signer
        self.software_version = software_version.strip()
        self.peer_class = peer_class
        self.runtime_capability = validated_runtime
        self._clock = clock
        self._id_source = id_source
        self._ttl = float(message_ttl_seconds)
        self._lock = threading.RLock()
        self._pending_join_message_id: str | None = None
        self._generation: int | None = None
        self._seed_key_digest: str | None = None
        self._seed_endpoint_id: str | None = None
        self._lease_expires_at: float | None = None
        self._heartbeat_sequence = 0
        self._seen_ids: dict[str, float] = {}
        self._emitted_ids: set[str] = set()
        self._deployment_epochs: dict[str, int] = {}
        self._assignment_offers: dict[str, dict[str, Any]] = {}
        self._peer_generations: dict[str, int] = {}
        self._peer_endpoints: dict[str, str] = {}
        self._probe_sequences: dict[tuple[str, str], int] = {}
        self._pending_liveness: dict[str, tuple[str, float]] = {}
        self._activity_receipts: dict[str, float] = {}
        self._suppress_next_heartbeat = False

    @property
    def joined(self) -> bool:
        with self._lock:
            return self._generation is not None

    @property
    def generation(self) -> int | None:
        with self._lock:
            return self._generation

    @property
    def seed_endpoint_id(self) -> str | None:
        with self._lock:
            return self._seed_endpoint_id

    @property
    def seed_key_digest(self) -> str | None:
        with self._lock:
            return self._seed_key_digest

    def _now(self) -> float:
        return _finite_now(self._clock())

    def _new_message_id(self) -> str:
        value = _segment(self._id_source(), "message_id")
        if value in self._emitted_ids:
            raise NodeMembershipError("membership_message_id_reused")
        self._emitted_ids.add(value)
        return value

    def _remember_incoming(
        self,
        message_id: str,
        *,
        expires_at: float,
        now: float,
    ) -> None:
        for expired_id, expiry in tuple(self._seen_ids.items()):
            if expiry < now:
                del self._seen_ids[expired_id]
        if message_id in self._seen_ids:
            raise NodeMembershipError("membership_message_replayed")
        if len(self._seen_ids) >= _MAX_REPLAY_IDS:
            raise NodeMembershipError("membership_replay_window_full")
        self._seen_ids[message_id] = expires_at

    def _post_join_common(
        self,
        protocol: str,
        *,
        recipient_node_id: str | None = None,
    ) -> dict[str, Any]:
        if (
            self._generation is None
            or self._seed_key_digest is None
            or self._seed_endpoint_id is None
            or self._lease_expires_at is None
        ):
            raise NodeMembershipError("membership_not_joined")
        now = self._now()
        if now >= self._lease_expires_at:
            raise NodeMembershipError("membership_lease_expired")
        return {
            "protocol": protocol,
            "message_id": self._new_message_id(),
            "swarm_id": self.swarm_id,
            "sender_node_id": self.node_id,
            "sender_endpoint_id": self.signer.endpoint_id,
            "recipient_node_id": (
                self.seed_node_id
                if recipient_node_id is None
                else _segment(recipient_node_id, "recipient_node_id")
            ),
            "incarnation": self.incarnation,
            "generation": self._generation,
            "issued_at": now,
            "expires_at": min(now + self._ttl, self._lease_expires_at),
        }

    def join_request(
        self,
        *,
        invite_nonce: str,
        endpoint_addrs: Sequence[str],
    ) -> dict[str, Any]:
        with self._lock:
            if self._generation is not None:
                raise NodeMembershipError("membership_already_joined")
            if self._pending_join_message_id is not None:
                raise NodeMembershipError("join_request_already_pending")
            now = self._now()
            message = {
                "protocol": JOIN_REQUEST_PROTOCOL,
                "message_id": self._new_message_id(),
                "swarm_id": self.swarm_id,
                "sender_node_id": self.node_id,
                "sender_endpoint_id": self.signer.endpoint_id,
                "recipient_node_id": self.seed_node_id,
                "incarnation": self.incarnation,
                "generation": 0,
                "issued_at": now,
                "expires_at": now + self._ttl,
                "invite_nonce": invite_nonce,
                "software_version": self.software_version,
                "peer_class": self.peer_class,
                "runtime_capability": dict(self.runtime_capability),
                "endpoint_addr": {
                    "id": self.signer.endpoint_id,
                    "addrs": list(endpoint_addrs),
                },
            }
            envelope = sign_membership_message(signer=self.signer, message=message)
            self._pending_join_message_id = message["message_id"]
            return envelope

    def accept_join(
        self,
        envelope: Mapping[str, Any],
        *,
        seed_key_digest: str,
    ) -> dict[str, Any]:
        with self._lock:
            now = self._now()
            message = verify_membership_message(
                envelope,
                now=now,
                expected_key_digest=seed_key_digest,
                expected_protocol=JOIN_ACCEPTANCE_PROTOCOL,
            )
            if message["message_id"] in self._seen_ids:
                raise NodeMembershipError("membership_message_replayed")
            if self._generation is not None:
                raise NodeMembershipError("membership_already_joined")
            expected = (
                self._pending_join_message_id is not None
                and message["swarm_id"] == self.swarm_id
                and message["sender_node_id"] == self.seed_node_id
                and message["recipient_node_id"] == self.node_id
                and message["request_message_id"] == self._pending_join_message_id
                and message["accepted_node_id"] == self.node_id
                and message["accepted_incarnation"] == self.incarnation
            )
            if not expected:
                raise NodeMembershipError("join_acceptance_mismatch")
            self._remember_incoming(
                message["message_id"],
                expires_at=float(message["expires_at"]),
                now=now,
            )
            if float(message["lease_expires_at"]) <= now:
                raise NodeMembershipError("membership_lease_expired")
            self._generation = int(message["membership_generation"])
            self._seed_key_digest = seed_key_digest
            self._seed_endpoint_id = message["sender_endpoint_id"]
            self._lease_expires_at = float(message["lease_expires_at"])
            self._pending_join_message_id = None
            return message

    def _verify_seed_message(
        self,
        envelope: Mapping[str, Any],
        *,
        expected_protocol: str,
    ) -> dict[str, Any]:
        if (
            self._seed_key_digest is None
            or self._generation is None
            or self._lease_expires_at is None
        ):
            raise NodeMembershipError("membership_not_joined")
        now = self._now()
        if now >= self._lease_expires_at:
            raise NodeMembershipError("membership_lease_expired")
        message = verify_membership_message(
            envelope,
            now=now,
            expected_key_digest=self._seed_key_digest,
            expected_protocol=expected_protocol,
        )
        if (
            message["swarm_id"] != self.swarm_id
            or message["sender_node_id"] != self.seed_node_id
            or message["sender_endpoint_id"] != self._seed_endpoint_id
            or message["recipient_node_id"] != self.node_id
            or message["generation"] != self._generation
        ):
            raise NodeMembershipError("membership_message_mismatch")
        self._remember_incoming(
            message["message_id"],
            expires_at=float(message["expires_at"]),
            now=now,
        )
        return message

    def accept_lease_renewal(
        self,
        envelope: Mapping[str, Any],
        *,
        heartbeat_message_id: str,
    ) -> dict[str, Any]:
        with self._lock:
            heartbeat_message_id = _segment(
                heartbeat_message_id,
                "heartbeat_message_id",
            )
            message = self._verify_seed_message(
                envelope,
                expected_protocol=LEASE_RENEWAL_PROTOCOL,
            )
            if (
                message["heartbeat_message_id"] != heartbeat_message_id
                or message["member_incarnation"] != self.incarnation
                or message["membership_generation"] != self._generation
            ):
                raise NodeMembershipError("membership_lease_renewal_mismatch")
            pending_liveness = self._pending_liveness.get(heartbeat_message_id)
            if pending_liveness is None:
                raise NodeMembershipError("membership_lease_renewal_unknown")
            renewed_until = float(message["lease_expires_at"])
            if (
                self._lease_expires_at is None
                or renewed_until < self._lease_expires_at
                or renewed_until <= self._now()
            ):
                raise NodeMembershipError("membership_lease_renewal_stale")
            self._lease_expires_at = renewed_until
            source, _expires_at = self._pending_liveness.pop(heartbeat_message_id)
            if source == "activation_receipt":
                self._suppress_next_heartbeat = True
            return message

    def accept_assignment_offer(self, envelope: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            message = self._verify_seed_message(
                envelope,
                expected_protocol=ASSIGNMENT_OFFER_PROTOCOL,
            )
            deployment_id = message["deployment_id"]
            epoch = int(message["deployment_epoch"])
            previous = self._deployment_epochs.get(deployment_id)
            if previous is not None and epoch <= previous:
                raise NodeMembershipError("assignment_epoch_stale")
            assignment_id = message["assignment_id"]
            previous_offer = self._assignment_offers.get(assignment_id)
            if previous_offer is not None and (
                previous_offer["deployment_id"] != deployment_id
                or previous_offer["deployment_epoch"] != epoch
            ):
                raise NodeMembershipError("assignment_id_conflict")
            peer_records = {
                record["node_id"]: dict(record)
                for record in message["peer_endpoint_records"]
            }
            for peer_node_id, record in peer_records.items():
                generation = int(record["membership_generation"])
                known_generation = self._peer_generations.get(peer_node_id)
                if known_generation is not None and generation < known_generation:
                    raise NodeMembershipError("assignment_peer_generation_stale")
                if (
                    known_generation == generation
                    and self._peer_endpoints.get(peer_node_id) != record["endpoint_id"]
                ):
                    raise NodeMembershipError("assignment_peer_endpoint_conflict")
            self._deployment_epochs[deployment_id] = epoch
            self._assignment_offers[assignment_id] = {
                "deployment_id": deployment_id,
                "deployment_epoch": epoch,
                "placement_provenance": message["placement_provenance"],
                "peer_endpoint_records": peer_records,
            }
            for peer_node_id, record in peer_records.items():
                self._peer_generations[peer_node_id] = int(
                    record["membership_generation"]
                )
                self._peer_endpoints[peer_node_id] = record["endpoint_id"]
            return message

    def resolve_peer_endpoint(
        self,
        *,
        assignment_id: str,
        peer_node_id: str,
        operator_expected_endpoint_id: str | None = None,
        now: float | None = None,
    ) -> str:
        """Resolve transport identity only from a current seed-signed assignment record."""

        with self._lock:
            assignment_id = _segment(assignment_id, "assignment_id")
            peer_node_id = _segment(peer_node_id, "peer_node_id")
            if operator_expected_endpoint_id is not None:
                raise NodeMembershipError("assignment_operator_endpoint_forbidden")
            offer = self._assignment_offers.get(assignment_id)
            if offer is None:
                raise NodeMembershipError("assignment_offer_unknown")
            if (
                self._deployment_epochs.get(offer["deployment_id"])
                != offer["deployment_epoch"]
            ):
                raise NodeMembershipError("assignment_offer_superseded")
            record = offer["peer_endpoint_records"].get(peer_node_id)
            if record is None:
                raise NodeMembershipError("assignment_peer_unauthorized")
            current = self._now() if now is None else _finite_now(now)
            if not float(record["valid_from"]) <= current < float(record["valid_until"]):
                raise NodeMembershipError("assignment_peer_record_expired")
            if self._peer_generations.get(peer_node_id) != int(
                record["membership_generation"]
            ):
                raise NodeMembershipError("assignment_peer_generation_revoked")
            if self._peer_endpoints.get(peer_node_id) != record["endpoint_id"]:
                raise NodeMembershipError("assignment_peer_endpoint_revoked")
            return str(record["endpoint_id"])

    def assignment_result(
        self,
        *,
        assignment_id: str,
        accepted: bool,
        result_code: str,
        load_proof_digest: str | None,
        runtime_endpoint: str | None,
    ) -> dict[str, Any]:
        with self._lock:
            assignment_id = _segment(assignment_id, "assignment_id")
            identity = self._assignment_offers.get(assignment_id)
            if identity is None:
                raise NodeMembershipError("assignment_offer_unknown")
            deployment_id = identity["deployment_id"]
            deployment_epoch = identity["deployment_epoch"]
            message = {
                **self._post_join_common(ASSIGNMENT_RESULT_PROTOCOL),
                "deployment_id": deployment_id,
                "deployment_epoch": deployment_epoch,
                "assignment_id": assignment_id,
                "accepted": accepted,
                "result_code": result_code,
                "load_proof_digest": load_proof_digest,
                "runtime_endpoint": runtime_endpoint,
            }
            return sign_membership_message(signer=self.signer, message=message)

    def link_probe_report(
        self,
        *,
        target_node_id: str,
        target_endpoint_id: str,
        reachable: bool,
        rtt_ms: float,
        goodput_bytes_per_second: float,
    ) -> dict[str, Any]:
        with self._lock:
            target_node_id = _segment(target_node_id, "target_node_id")
            target_endpoint_id = _segment(target_endpoint_id, "target_endpoint_id")
            key = (target_node_id, target_endpoint_id)
            sequence = self._probe_sequences.get(key, 0) + 1
            message = {
                **self._post_join_common(
                    LINK_PROBE_REPORT_PROTOCOL,
                    recipient_node_id=target_node_id,
                ),
                "target_node_id": target_node_id,
                "target_endpoint_id": target_endpoint_id,
                "reachable": reachable,
                "rtt_ms": rtt_ms,
                "goodput_bytes_per_second": goodput_bytes_per_second,
                "probe_sequence": sequence,
            }
            envelope = sign_membership_message(signer=self.signer, message=message)
            self._probe_sequences[key] = sequence
            return envelope

    def drain_acknowledgement(
        self,
        *,
        drain_id: str,
        last_request_id: str | None,
        completed_at: float | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            message = self._post_join_common(DRAIN_ACK_PROTOCOL)
            completed = message["issued_at"] if completed_at is None else completed_at
            message.update(
                {
                    "drain_id": drain_id,
                    "active_requests": 0,
                    "last_request_id": last_request_id,
                    "completed_at": completed,
                }
            )
            return sign_membership_message(signer=self.signer, message=message)

    def capability_report(
        self,
        *,
        platform: str,
        architecture: str,
        memory_bytes: int,
        available_storage_bytes: int,
        backends: Sequence[str],
        precisions: Sequence[str],
    ) -> dict[str, Any]:
        with self._lock:
            message = {
                **self._post_join_common(CAPABILITY_REPORT_PROTOCOL),
                "platform": platform,
                "architecture": architecture,
                "memory_bytes": memory_bytes,
                "available_storage_bytes": available_storage_bytes,
                "backends": list(backends),
                "precisions": list(precisions),
            }
            return sign_membership_message(signer=self.signer, message=message)

    def _emit_liveness(
        self,
        *,
        lifecycle_state: str,
        active_requests: int,
        liveness_source: str,
        activity_receipt_digest: str | None,
        activity_peer_node_id: str | None,
    ) -> dict[str, Any]:
        validate_heartbeat_shape(
            lifecycle_state=lifecycle_state,
            active_requests=active_requests,
            route_ready=False,
            liveness_source=liveness_source,
            activity_receipt_digest=activity_receipt_digest,
            activity_peer_node_id=activity_peer_node_id,
        )
        now = self._now()
        self._pending_liveness = {
            message_id: pending
            for message_id, pending in self._pending_liveness.items()
            if pending[1] > now
        }
        self._activity_receipts = {
            digest: expires_at
            for digest, expires_at in self._activity_receipts.items()
            if expires_at > now
        }
        self._heartbeat_sequence += 1
        try:
            message = {
                **self._post_join_common(HEARTBEAT_PROTOCOL),
                "heartbeat_sequence": self._heartbeat_sequence,
                "lifecycle_state": lifecycle_state,
                "route_ready": False,
                "active_requests": active_requests,
                "liveness_source": liveness_source,
                "activity_receipt_digest": activity_receipt_digest,
                "activity_peer_node_id": activity_peer_node_id,
            }
            envelope = sign_membership_message(signer=self.signer, message=message)
            self._pending_liveness[message["message_id"]] = (
                liveness_source,
                float(message["expires_at"]),
            )
            return envelope
        except BaseException:
            self._heartbeat_sequence -= 1
            raise

    def heartbeat(
        self,
        *,
        lifecycle_state: str,
        active_requests: int,
        force: bool = False,
    ) -> dict[str, Any] | None:
        with self._lock:
            if not isinstance(force, bool):
                raise ValueError("force is invalid")
            if self._suppress_next_heartbeat and not force:
                self._suppress_next_heartbeat = False
                return None
            return self._emit_liveness(
                lifecycle_state=lifecycle_state,
                active_requests=active_requests,
                liveness_source="scheduled_heartbeat",
                activity_receipt_digest=None,
                activity_peer_node_id=None,
            )

    def activation_receipt(
        self,
        *,
        assignment_id: str,
        peer_node_id: str,
        receipt: DeliveryReceipt,
        lifecycle_state: str,
        active_requests: int,
    ) -> dict[str, Any]:
        with self._lock:
            assignment_id = _segment(assignment_id, "assignment_id")
            peer_node_id = _segment(peer_node_id, "peer_node_id")
            if not isinstance(receipt, DeliveryReceipt):
                raise NodeMembershipError("assignment_receipt_type_invalid")
            expected_endpoint_id = self.resolve_peer_endpoint(
                assignment_id=assignment_id,
                peer_node_id=peer_node_id,
            )
            offer = self._assignment_offers[assignment_id]
            record = offer["peer_endpoint_records"][peer_node_id]
            if (
                receipt.peer_endpoint_id != expected_endpoint_id
                or receipt.peer_generation != record["membership_generation"]
                or receipt.semantics != DELIVERY_SEMANTICS
                or receipt.router_protocol != ROUTER_WIRE_PROTOCOL
                or not isinstance(receipt.message_id, bytes)
                or len(receipt.message_id) != 32
            ):
                raise NodeMembershipError("assignment_receipt_peer_mismatch")
            receipt_document = {
                "message_id": receipt.message_id.hex(),
                "peer_endpoint_id": receipt.peer_endpoint_id,
                "peer_generation": receipt.peer_generation,
                "semantics": receipt.semantics,
                "router_protocol": receipt.router_protocol,
            }
            digest = "sha256:" + hashlib.sha256(
                canonical_json_bytes(receipt_document)
            ).hexdigest()
            now = self._now()
            if self._activity_receipts.get(digest, 0.0) > now:
                raise NodeMembershipError("assignment_receipt_replayed")
            envelope = self._emit_liveness(
                lifecycle_state=lifecycle_state,
                active_requests=active_requests,
                liveness_source="activation_receipt",
                activity_receipt_digest=digest,
                activity_peer_node_id=peer_node_id,
            )
            self._activity_receipts[digest] = float(
                envelope["message"]["expires_at"]
            )
            return envelope
