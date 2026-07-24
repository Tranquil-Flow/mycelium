# SPDX-License-Identifier: AGPL-3.0-or-later
"""Seed coordinator for signed membership, leases, and assignment control."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
import hashlib
import math
import re
import threading
import time
from typing import Any
import uuid

from mycelium_invite import (
    InviteError,
    SqliteInviteRegistry,
    mint_invite_bundle,
    verify_invite,
)
from mycelium_membership import (
    ASSIGNMENT_OFFER_PROTOCOL,
    ASSIGNMENT_RESULT_PROTOCOL,
    CAPABILITY_REPORT_PROTOCOL,
    DRAIN_ACK_PROTOCOL,
    HEARTBEAT_PROTOCOL,
    JOIN_ACCEPTANCE_PROTOCOL,
    LEASE_RENEWAL_PROTOCOL,
    LINK_PROBE_REPORT_PROTOCOL,
    MAX_MESSAGE_TTL_SECONDS,
    MembershipContractError,
    peer_runtime_is_activation_eligible,
    sign_membership_message,
    verify_join_request,
    verify_membership_message,
)
from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_qualification.signing import Ed25519EvidenceSigner

from .placement import MemberRecord, PlacementDecision, PlacementError, PlacementSource
from .state import SeedStateError, SqliteSeedState


_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MAX_REPLAY_IDS = 4096
SEED_SIGNED_ENVELOPE_PROTOCOL = "mycelium.seed.signed_envelope.v1"
SEED_IDENTITY_PROTOCOL = "mycelium.seed.identity.v1"
SEED_RECEIPT_PROTOCOL = "mycelium.seed.receipt.v1"
_MEMBER_PROTOCOLS = frozenset(
    {
        CAPABILITY_REPORT_PROTOCOL,
        LINK_PROBE_REPORT_PROTOCOL,
        HEARTBEAT_PROTOCOL,
        ASSIGNMENT_RESULT_PROTOCOL,
        DRAIN_ACK_PROTOCOL,
    }
)


class SeedCoordinatorError(RuntimeError):
    """Stable seed-side relational or lifecycle error."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _segment(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _SEGMENT_RE.fullmatch(value) is None:
        raise ValueError(f"{field_name} is invalid")
    return value


def _now(value: Any) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise SeedCoordinatorError("seed_clock_invalid")
    return float(value)


@dataclass
class _Member:
    node_id: str
    endpoint_id: str
    endpoint_addrs: tuple[str, ...]
    peer_class: str
    runtime_capability: dict[str, Any]
    verification_key_digest: str
    incarnation: str
    generation: int
    lease_expires_at: float
    last_heartbeat_sequence: int = 0
    last_liveness_at: float = 0.0
    next_heartbeat_due_at: float = 0.0
    last_activity_receipt_at: float | None = None
    active_requests: int = 0
    lifecycle_state: str = "NEW"
    seen_ids: dict[str, float] = field(default_factory=dict)
    latest_messages: dict[str, dict[str, Any]] = field(default_factory=dict)

    def projection(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "endpoint_id": self.endpoint_id,
            "endpoint_addrs": list(self.endpoint_addrs),
            "peer_class": self.peer_class,
            "runtime_capability": dict(self.runtime_capability),
            "activation_eligible": peer_runtime_is_activation_eligible(
                self.peer_class,
                self.runtime_capability,
            ),
            "verification_key_digest": self.verification_key_digest,
            "incarnation": self.incarnation,
            "generation": self.generation,
            "lease_expires_at": self.lease_expires_at,
            "last_heartbeat_sequence": self.last_heartbeat_sequence,
            "last_liveness_at": self.last_liveness_at,
            "next_heartbeat_due_at": self.next_heartbeat_due_at,
            "last_activity_receipt_at": self.last_activity_receipt_at,
            "active_requests": self.active_requests,
            "lifecycle_state": self.lifecycle_state,
        }


class SeedCoordinator:
    """Authorize joins and broker signed control messages without route authority."""

    def __init__(
        self,
        *,
        swarm_id: str,
        seed_node_id: str,
        seed_url: str | None,
        signer: Ed25519EvidenceSigner,
        invite_registry: SqliteInviteRegistry,
        incarnation: str,
        placement_source: PlacementSource | None = None,
        state: SqliteSeedState | None = None,
        clock: Callable[[], float] = time.time,
        id_source: Callable[[], str] = lambda: str(uuid.uuid4()),
        lease_seconds: float = 300.0,
        message_ttl_seconds: float = 60.0,
        keepalive_interval_seconds: float = 30.0,
        evidence_freshness_seconds: float = 90.0,
    ) -> None:
        self.swarm_id = _segment(swarm_id, "swarm_id")
        self.seed_node_id = _segment(seed_node_id, "seed_node_id")
        self.incarnation = _segment(incarnation, "incarnation")
        if seed_url is not None and (
            not isinstance(seed_url, str)
            or not seed_url
            or seed_url != seed_url.strip()
        ):
            raise ValueError("seed_url is invalid")
        if not isinstance(signer, Ed25519EvidenceSigner):
            raise ValueError("signer is invalid")
        if not isinstance(invite_registry, SqliteInviteRegistry):
            raise ValueError("invite_registry is invalid")
        if state is not None and not isinstance(state, SqliteSeedState):
            raise ValueError("state is invalid")
        if placement_source is not None and not callable(
            getattr(placement_source, "compile", None)
        ):
            raise ValueError("placement_source is invalid")
        if state is None:
            state = SqliteSeedState(invite_registry.database)
        if state.database.resolve() != invite_registry.database.resolve():
            raise ValueError("state and invite_registry must share one database")
        if not callable(clock) or not callable(id_source):
            raise ValueError("clock and id_source must be callable")
        for value, name in (
            (lease_seconds, "lease_seconds"),
            (message_ttl_seconds, "message_ttl_seconds"),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or value <= 0
            ):
                raise ValueError(f"{name} is invalid")
        if float(lease_seconds) > MAX_MESSAGE_TTL_SECONDS:
            raise ValueError("lease_seconds is invalid")
        self.seed_url = seed_url
        self.signer = signer
        self._invite_registry = invite_registry
        self._clock = clock
        self._id_source = id_source
        self._lease_seconds = float(lease_seconds)
        self._message_ttl_seconds = float(message_ttl_seconds)
        for name, value in (
            ("keepalive_interval_seconds", keepalive_interval_seconds),
            ("evidence_freshness_seconds", evidence_freshness_seconds),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) <= 0.0
            ):
                raise ValueError(f"{name} is invalid")
        if float(evidence_freshness_seconds) <= float(keepalive_interval_seconds):
            raise ValueError("evidence_freshness_seconds is invalid")
        self._keepalive_interval_seconds = float(keepalive_interval_seconds)
        self._evidence_freshness_seconds = float(evidence_freshness_seconds)
        self._lock = threading.RLock()
        self._members: dict[str, _Member] = {}
        self._emitted_ids: set[str] = set()
        self._assignments: dict[str, dict[str, Any]] = {}
        self._placement_source = placement_source
        self._state = state
        if state is not None:
            try:
                state.bind_identity(
                    swarm_id=self.swarm_id,
                    seed_node_id=self.seed_node_id,
                    seed_key_digest=self.signer.verification_key_digest,
                )
                for record in state.load_members():
                    member = _Member(
                        node_id=_segment(record["node_id"], "node_id"),
                        endpoint_id=_segment(record["endpoint_id"], "endpoint_id"),
                        endpoint_addrs=tuple(record["endpoint_addrs"]),
                        peer_class=record["peer_class"],
                        runtime_capability=dict(record["runtime_capability"]),
                        verification_key_digest=record["verification_key_digest"],
                        incarnation=_segment(record["incarnation"], "incarnation"),
                        generation=int(record["generation"]),
                        lease_expires_at=float(record["lease_expires_at"]),
                        last_heartbeat_sequence=int(
                            record["last_heartbeat_sequence"]
                        ),
                        last_liveness_at=float(record["last_liveness_at"]),
                        next_heartbeat_due_at=float(record["next_heartbeat_due_at"]),
                        last_activity_receipt_at=(
                            None
                            if record["last_activity_receipt_at"] is None
                            else float(record["last_activity_receipt_at"])
                        ),
                        active_requests=int(record["active_requests"]),
                        lifecycle_state=record["lifecycle_state"],
                    )
                    self._members[member.node_id] = member
                self._assignments = {
                    record["assignment_id"]: record
                    for record in state.load_assignments()
                }
            except (SeedStateError, TypeError, ValueError) as exc:
                code = getattr(exc, "code", "seed_state_corrupt")
                raise SeedCoordinatorError(code) from exc

    def _persist(self, method: str, *args: Any, **kwargs: Any) -> None:
        if self._state is None:
            return
        try:
            getattr(self._state, method)(*args, **kwargs)
        except SeedStateError as exc:
            raise SeedCoordinatorError(exc.code) from exc

    def bind_seed_url(self, seed_url: str) -> str:
        if not isinstance(seed_url, str) or not seed_url or seed_url != seed_url.strip():
            raise ValueError("seed_url is invalid")
        with self._lock:
            if self.seed_url is not None and self.seed_url != seed_url:
                raise SeedCoordinatorError("seed_url_already_bound")
            self.seed_url = seed_url
            return seed_url

    def _require_seed_url(self) -> str:
        if self.seed_url is None:
            raise SeedCoordinatorError("seed_url_unbound")
        return self.seed_url

    def _now(self) -> float:
        return _now(self._clock())

    def _new_message_id(self) -> str:
        message_id = _segment(self._id_source(), "message_id")
        if message_id in self._emitted_ids:
            raise SeedCoordinatorError("seed_message_id_reused")
        self._persist("reserve_seed_message", message_id)
        self._emitted_ids.add(message_id)
        return message_id

    def mint_invite(self, *, nonce: str, ttl_seconds: int) -> dict[str, Any]:
        with self._lock:
            seed_url = self._require_seed_url()
            return mint_invite_bundle(
                signer=self.signer,
                swarm_id=self.swarm_id,
                seed_url=seed_url,
                ttl_seconds=ttl_seconds,
                nonce=nonce,
                issued_at=self._now(),
            )

    def _signed_seed_statement(
        self,
        protocol: str,
        fields: Mapping[str, Any],
    ) -> dict[str, Any]:
        now = self._now()
        statement = {
            "protocol": protocol,
            "swarm_id": self.swarm_id,
            "seed_node_id": self.seed_node_id,
            "seed_endpoint_id": self.signer.endpoint_id,
            "seed_url": self._require_seed_url(),
            "issued_at": now,
            "expires_at": now + self._message_ttl_seconds,
            **dict(fields),
        }
        return {
            "protocol": SEED_SIGNED_ENVELOPE_PROTOCOL,
            "statement": statement,
            "signature": self.signer.sign(statement),
            "verification_key": self.signer.public_key_record(),
        }

    def identity_envelope(self) -> dict[str, Any]:
        with self._lock:
            return self._signed_seed_statement(SEED_IDENTITY_PROTOCOL, {})

    def receipt_envelope(self, message_id: str) -> dict[str, Any]:
        message_id = _segment(message_id, "message_id")
        with self._lock:
            return self._signed_seed_statement(
                SEED_RECEIPT_PROTOCOL,
                {"accepted_message_id": message_id},
            )

    def accept_join(
        self,
        *,
        invite_token: str,
        join_envelope: Mapping[str, Any],
    ) -> dict[str, Any]:
        with self._lock:
            invite_token_digest: str | None = None
            request_envelope_digest: str | None = None
            retry_nonce: str | None = None
            if isinstance(invite_token, str) and isinstance(join_envelope, Mapping):
                try:
                    candidate = join_envelope.get("message")
                    if isinstance(candidate, Mapping):
                        nonce = candidate.get("invite_nonce")
                        if isinstance(nonce, str):
                            retry_nonce = nonce
                    invite_token_digest = hashlib.sha256(
                        invite_token.encode("utf-8")
                    ).hexdigest()
                    request_envelope_digest = hashlib.sha256(
                        canonical_json_bytes(dict(join_envelope))
                    ).hexdigest()
                except (TypeError, ValueError, UnicodeError):
                    pass
            if (
                retry_nonce is not None
                and invite_token_digest is not None
                and request_envelope_digest is not None
            ):
                try:
                    committed = self._state.load_join_acceptance(
                        nonce=retry_nonce,
                        invite_token_digest=invite_token_digest,
                        request_envelope_digest=request_envelope_digest,
                    )
                except SeedStateError as exc:
                    raise SeedCoordinatorError(exc.code) from exc
                if committed is not None:
                    return committed

            now = self._now()
            seed_url = self._require_seed_url()
            invite = verify_invite(
                invite_token,
                verifier_key_records=[self.signer.public_key_record()],
                now=now,
            )
            request = verify_join_request(join_envelope, now=now)
            if (
                invite["swarm_id"] != self.swarm_id
                or invite["seed_url"] != seed_url
                or request["swarm_id"] != self.swarm_id
                or request["recipient_node_id"] != self.seed_node_id
                or request["invite_nonce"] != invite["nonce"]
            ):
                raise SeedCoordinatorError("seed_join_mismatch")
            record = join_envelope.get("verification_key")
            if not isinstance(record, Mapping):
                raise SeedCoordinatorError("seed_join_key_invalid")
            key_digest = record.get("verification_key_digest")
            if not isinstance(key_digest, str) or not key_digest:
                raise SeedCoordinatorError("seed_join_key_invalid")
            node_id = request["sender_node_id"]
            previous = self._members.get(node_id)
            if any(
                other.node_id != node_id
                and (
                    other.endpoint_id == request["sender_endpoint_id"]
                    or other.verification_key_digest == key_digest
                )
                for other in self._members.values()
            ):
                raise SeedCoordinatorError("seed_member_identity_reused")
            if previous is not None and previous.verification_key_digest != key_digest:
                raise SeedCoordinatorError("seed_node_key_conflict")
            if (
                previous is not None
                and previous.endpoint_id != request["sender_endpoint_id"]
            ):
                raise SeedCoordinatorError("seed_node_endpoint_conflict")

            generation = 1 if previous is None else previous.generation + 1
            lease_expires_at = now + self._lease_seconds
            endpoint = request["endpoint_addr"]
            member = _Member(
                node_id=node_id,
                endpoint_id=request["sender_endpoint_id"],
                endpoint_addrs=tuple(endpoint["addrs"]),
                peer_class=request["peer_class"],
                runtime_capability=dict(request["runtime_capability"]),
                verification_key_digest=key_digest,
                incarnation=request["incarnation"],
                generation=generation,
                lease_expires_at=lease_expires_at,
                last_liveness_at=now,
                next_heartbeat_due_at=now + self._keepalive_interval_seconds,
                active_requests=0,
                lifecycle_state="NEW",
            )
            message_id = _segment(self._id_source(), "message_id")
            if message_id in self._emitted_ids:
                raise SeedCoordinatorError("seed_message_id_reused")
            message = {
                "protocol": JOIN_ACCEPTANCE_PROTOCOL,
                "message_id": message_id,
                "swarm_id": self.swarm_id,
                "sender_node_id": self.seed_node_id,
                "sender_endpoint_id": self.signer.endpoint_id,
                "recipient_node_id": node_id,
                "incarnation": self.incarnation,
                "generation": generation,
                "issued_at": now,
                "expires_at": now + self._message_ttl_seconds,
                "request_message_id": request["message_id"],
                "accepted_node_id": node_id,
                "accepted_incarnation": request["incarnation"],
                "membership_generation": generation,
                "lease_expires_at": lease_expires_at,
            }
            acceptance = sign_membership_message(signer=self.signer, message=message)
            assert invite_token_digest is not None
            assert request_envelope_digest is not None
            try:
                committed = self._state.commit_join(
                    nonce=invite["nonce"],
                    consumed_at=now,
                    invite_expires_at=float(invite["expires_at"]),
                    invite_token_digest=invite_token_digest,
                    request_envelope_digest=request_envelope_digest,
                    request_message_id=request["message_id"],
                    message_id=message_id,
                    member=member.projection(),
                    acceptance=acceptance,
                )
            except SeedStateError as exc:
                if exc.code == "seed_join_invite_replayed":
                    raise InviteError("invite_replayed") from exc
                raise SeedCoordinatorError(exc.code) from exc
            self._emitted_ids.add(message_id)
            self._members[node_id] = member
            return committed

    def _liveness_projection(
        self,
        member: _Member,
        *,
        now: float,
    ) -> dict[str, Any]:
        age = max(0.0, now - member.last_liveness_at)
        overdue_age = max(0.0, now - member.next_heartbeat_due_at)
        stale = now > member.next_heartbeat_due_at
        missed_keepalives = (
            0
            if not stale
            else 1 + int(overdue_age // self._keepalive_interval_seconds)
        )
        evidence_expired = age > self._evidence_freshness_seconds
        if not stale:
            status = "active_fresh" if member.active_requests > 0 else "idle_fresh"
            dead = False
            reason = None
        elif member.active_requests > 0:
            status = "active_decode_transport_failure"
            dead = False
            reason = "in_flight_liveness_lost"
        else:
            dead = missed_keepalives >= 2 and evidence_expired
            status = "dead" if dead else "liveness_stale"
            reason = (
                "idle_keepalive_and_evidence_expired"
                if dead
                else "idle_keepalive_missed"
            )
        return {
            "liveness_status": status,
            "liveness_dead": dead,
            "liveness_reason": reason,
            "liveness_age_seconds": age,
            "heartbeat_overdue_seconds": overdue_age,
            "missed_keepalives": missed_keepalives,
            "evidence_freshness_expired": evidence_expired,
        }

    def member(self, node_id: str) -> dict[str, Any]:
        node_id = _segment(node_id, "node_id")
        with self._lock:
            member = self._members.get(node_id)
            if member is None:
                raise SeedCoordinatorError("seed_member_unknown")
            projection = member.projection()
            projection.update(self._liveness_projection(member, now=self._now()))
            return projection

    def members(self, *, peer_class: str | None = None) -> tuple[dict[str, Any], ...]:
        """Return detached, stable projections for transport adapters."""

        if peer_class is not None:
            peer_class = _segment(peer_class, "peer_class")
        with self._lock:
            now = self._now()
            projections = []
            for member in sorted(
                self._members.values(),
                key=lambda item: item.node_id,
            ):
                if peer_class is not None and member.peer_class != peer_class:
                    continue
                projection = member.projection()
                projection.update(self._liveness_projection(member, now=now))
                projections.append(projection)
            return tuple(projections)

    @contextmanager
    def member_authority_guard(
        self,
        *,
        node_id: str,
        expected_generation: int,
        expected_peer_class: str,
        eligible_lifecycle_states: frozenset[str],
    ) -> Iterator[dict[str, Any]]:
        """Hold seed authority stable while an adapter commits accepted work."""

        node_id = _segment(node_id, "node_id")
        expected_peer_class = _segment(expected_peer_class, "expected_peer_class")
        if (
            isinstance(expected_generation, bool)
            or not isinstance(expected_generation, int)
            or expected_generation < 1
        ):
            raise ValueError("expected_generation is invalid")
        if (
            not isinstance(eligible_lifecycle_states, frozenset)
            or not eligible_lifecycle_states
        ):
            raise ValueError("eligible_lifecycle_states is invalid")
        try:
            normalized_lifecycle_states = frozenset(
                _segment(state, "eligible_lifecycle_state")
                for state in eligible_lifecycle_states
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("eligible_lifecycle_states is invalid") from exc
        with self._lock:
            member = self._members.get(node_id)
            if member is None:
                raise SeedCoordinatorError("seed_member_unknown")
            durable_guard = (
                nullcontext(member.projection())
                if self._state is None
                else self._state.member_authority_guard(node_id=node_id)
            )
            try:
                # Lock order is adapter -> coordinator -> durable state. The
                # transaction never calls back into either outer lock owner.
                with durable_guard as persisted:
                    identity_fields = (
                        "node_id",
                        "endpoint_id",
                        "verification_key_digest",
                        "incarnation",
                    )
                    if any(
                        persisted[field] != getattr(member, field)
                        for field in identity_fields
                    ):
                        raise SeedCoordinatorError("seed_state_member_stale")
                    if persisted["generation"] != member.generation:
                        raise SeedCoordinatorError("seed_state_member_stale")
                    if persisted["peer_class"] != member.peer_class:
                        raise SeedCoordinatorError("seed_state_member_stale")
                    if persisted["lease_expires_at"] != member.lease_expires_at:
                        raise SeedCoordinatorError("seed_state_member_stale")
                    if persisted["lifecycle_state"] != member.lifecycle_state:
                        raise SeedCoordinatorError("seed_state_member_stale")
                    if member.generation != expected_generation:
                        raise SeedCoordinatorError(
                            "seed_member_generation_stale"
                        )
                    if member.peer_class != expected_peer_class:
                        raise SeedCoordinatorError(
                            "seed_member_peer_class_mismatch"
                        )
                    now = self._now()
                    if now >= member.lease_expires_at:
                        raise SeedCoordinatorError("seed_member_lease_expired")
                    if (
                        member.lifecycle_state
                        not in normalized_lifecycle_states
                    ):
                        raise SeedCoordinatorError(
                            "seed_member_lifecycle_ineligible"
                        )
                    projection = member.projection()
                    projection.update(
                        self._liveness_projection(member, now=now)
                    )
                    yield projection
            except SeedStateError as exc:
                raise SeedCoordinatorError(exc.code) from exc

    def advance_member_generation(
        self,
        *,
        node_id: str,
        expected_generation: int,
        lifecycle_state: str,
    ) -> dict[str, Any]:
        """Durably fence one adapter member without defining a second trust plane."""

        node_id = _segment(node_id, "node_id")
        if (
            isinstance(expected_generation, bool)
            or not isinstance(expected_generation, int)
            or expected_generation < 1
        ):
            raise ValueError("expected_generation is invalid")
        if lifecycle_state not in {"STOPPING", "STOPPED"}:
            raise ValueError("lifecycle_state is invalid")
        with self._lock:
            member = self._members.get(node_id)
            if member is None:
                raise SeedCoordinatorError("seed_member_unknown")
            self._ensure_current_member(member)
            if member.generation != expected_generation:
                raise SeedCoordinatorError("seed_member_generation_stale")
            now = self._now()
            persisted = member.projection()
            persisted.update(
                generation=member.generation + 1,
                last_liveness_at=now,
                next_heartbeat_due_at=now,
                active_requests=0,
                lifecycle_state=lifecycle_state,
            )
            self._persist("save_member", persisted)
            member.generation += 1
            member.last_liveness_at = now
            member.next_heartbeat_due_at = now
            member.active_requests = 0
            member.lifecycle_state = lifecycle_state
            projection = member.projection()
            projection.update(self._liveness_projection(member, now=now))
            return projection

    def compile_placement(self) -> PlacementDecision:
        """Compile placement intent from a stable public membership snapshot."""

        source = self._placement_source
        if source is None:
            raise SeedCoordinatorError("seed_placement_source_unconfigured")
        with self._lock:
            members = tuple(
                MemberRecord(
                    node_id=member.node_id,
                    endpoint_id=member.endpoint_id,
                    peer_class=member.peer_class,
                    runtime_capability=member.runtime_capability,
                    generation=member.generation,
                    lease_expires_at=member.lease_expires_at,
                    activation_eligible=peer_runtime_is_activation_eligible(
                        member.peer_class,
                        member.runtime_capability,
                    ),
                )
                for member in sorted(
                    self._members.values(),
                    key=lambda item: item.node_id,
                )
            )
        try:
            decision = source.compile(members)
        except PlacementError as exc:
            raise SeedCoordinatorError(exc.code) from exc
        if not isinstance(decision, PlacementDecision):
            raise SeedCoordinatorError("seed_placement_decision_invalid")
        return decision

    def _ensure_current_member(self, member: _Member) -> None:
        if self._state is None:
            return
        try:
            current = self._state.member_is_current(
                node_id=member.node_id,
                endpoint_id=member.endpoint_id,
                verification_key_digest=member.verification_key_digest,
                incarnation=member.incarnation,
                generation=member.generation,
            )
        except SeedStateError as exc:
            raise SeedCoordinatorError(exc.code) from exc
        if not current:
            raise SeedCoordinatorError("seed_state_member_stale")

    def _ensure_not_replayed(
        self,
        member: _Member,
        message_id: str,
    ) -> None:
        if self._state is not None:
            try:
                seen = self._state.member_message_seen(
                    node_id=member.node_id,
                    generation=member.generation,
                    message_id=message_id,
                )
            except SeedStateError as exc:
                raise SeedCoordinatorError(exc.code) from exc
            if seen:
                raise SeedCoordinatorError("seed_message_replayed")
            return
        if message_id in member.seen_ids:
            raise SeedCoordinatorError("seed_message_replayed")

    def _remember(
        self,
        member: _Member,
        message: Mapping[str, Any],
        now: float,
    ) -> None:
        if self._state is not None:
            self._persist(
                "remember_member_message",
                node_id=member.node_id,
                generation=member.generation,
                message_id=message["message_id"],
                expires_at=float(message["expires_at"]),
                now=now,
                capacity=_MAX_REPLAY_IDS,
            )
            return
        for message_id, expires_at in tuple(member.seen_ids.items()):
            if expires_at < now:
                del member.seen_ids[message_id]
        message_id = message["message_id"]
        if message_id in member.seen_ids:
            raise SeedCoordinatorError("seed_message_replayed")
        if len(member.seen_ids) >= _MAX_REPLAY_IDS:
            raise SeedCoordinatorError("seed_replay_window_full")
        member.seen_ids[message_id] = float(message["expires_at"])

    def _validate_committed_heartbeat_renewal(
        self,
        envelope: Mapping[str, Any],
        *,
        member: _Member,
        heartbeat_message_id: str,
        now: float,
    ) -> dict[str, Any]:
        message = verify_membership_message(
            envelope,
            now=now,
            expected_key_digest=self.signer.verification_key_digest,
            expected_protocol=LEASE_RENEWAL_PROTOCOL,
            expected_swarm_id=self.swarm_id,
            expected_sender_node_id=self.seed_node_id,
            expected_sender_endpoint_id=self.signer.endpoint_id,
            expected_recipient_node_id=member.node_id,
        )
        if (
            message["incarnation"] != self.incarnation
            or message["generation"] != member.generation
            or message["heartbeat_message_id"] != heartbeat_message_id
            or message["member_incarnation"] != member.incarnation
            or message["membership_generation"] != member.generation
        ):
            raise SeedCoordinatorError("seed_state_corrupt")
        return message

    def _apply_committed_heartbeat(
        self,
        member: _Member,
        *,
        heartbeat: Mapping[str, Any],
        renewal: Mapping[str, Any],
        renewal_message: Mapping[str, Any],
    ) -> None:
        sequence = int(heartbeat["heartbeat_sequence"])
        if sequence >= member.last_heartbeat_sequence:
            committed_at = float(renewal_message["issued_at"])
            heartbeat_intervals = (
                2 if heartbeat["liveness_source"] == "activation_receipt" else 1
            )
            member.last_heartbeat_sequence = sequence
            member.lease_expires_at = max(
                member.lease_expires_at,
                float(renewal_message["lease_expires_at"]),
            )
            member.last_liveness_at = committed_at
            member.next_heartbeat_due_at = (
                committed_at
                + heartbeat_intervals * self._keepalive_interval_seconds
            )
            member.active_requests = int(heartbeat["active_requests"])
            member.lifecycle_state = heartbeat["lifecycle_state"]
            if heartbeat["liveness_source"] == "activation_receipt":
                member.last_activity_receipt_at = committed_at
        member.latest_messages[LEASE_RENEWAL_PROTOCOL] = dict(renewal)
        member.latest_messages[HEARTBEAT_PROTOCOL] = dict(heartbeat)

    def receive_member_message(
        self,
        envelope: Mapping[str, Any],
        *,
        expected_protocol: str,
    ) -> dict[str, Any]:
        if expected_protocol not in _MEMBER_PROTOCOLS:
            raise ValueError("expected_protocol is invalid")
        try:
            untrusted_message = envelope["message"]
            node_id = untrusted_message["sender_node_id"]
        except (KeyError, TypeError) as exc:
            raise SeedCoordinatorError("seed_message_malformed") from exc
        if not isinstance(node_id, str):
            raise SeedCoordinatorError("seed_message_malformed")
        with self._lock:
            member = self._members.get(node_id)
            if member is None:
                raise SeedCoordinatorError("seed_member_unknown")
            now = self._now()
            if (
                expected_protocol != HEARTBEAT_PROTOCOL
                and now >= member.lease_expires_at
            ):
                raise SeedCoordinatorError("seed_member_lease_expired")

            verification_now = now
            if expected_protocol == HEARTBEAT_PROTOCOL:
                try:
                    request_expiry = float(untrusted_message["expires_at"])
                    if math.isfinite(request_expiry):
                        verification_now = min(now, request_expiry)
                except (KeyError, TypeError, ValueError):
                    pass
            message = verify_membership_message(
                envelope,
                now=verification_now,
                expected_key_digest=member.verification_key_digest,
                expected_protocol=expected_protocol,
            )
            if (
                message["swarm_id"] != self.swarm_id
                or message["sender_node_id"] != member.node_id
                or message["sender_endpoint_id"] != member.endpoint_id
                or message["incarnation"] != member.incarnation
                or message["generation"] != member.generation
                or message["recipient_node_id"] != self.seed_node_id
            ):
                raise SeedCoordinatorError("seed_message_mismatch")
            self._ensure_current_member(member)

            if expected_protocol == HEARTBEAT_PROTOCOL:
                request_digest = hashlib.sha256(
                    canonical_json_bytes(dict(envelope))
                ).hexdigest()
                try:
                    committed = self._state.load_heartbeat_renewal(
                        node_id=member.node_id,
                        endpoint_id=member.endpoint_id,
                        verification_key_digest=member.verification_key_digest,
                        incarnation=member.incarnation,
                        generation=member.generation,
                        heartbeat_message_id=message["message_id"],
                        request_envelope_digest=request_digest,
                    )
                except SeedStateError as exc:
                    raise SeedCoordinatorError(exc.code) from exc
                if committed is not None:
                    renewal_message = self._validate_committed_heartbeat_renewal(
                        committed,
                        member=member,
                        heartbeat_message_id=message["message_id"],
                        now=now,
                    )
                    self._apply_committed_heartbeat(
                        member,
                        heartbeat=message,
                        renewal=committed,
                        renewal_message=renewal_message,
                    )
                    return message
                if now > float(message["expires_at"]):
                    raise MembershipContractError("membership_message_expired")
                if now >= member.lease_expires_at:
                    raise SeedCoordinatorError("seed_member_lease_expired")

                sequence = int(message["heartbeat_sequence"])
                if sequence <= member.last_heartbeat_sequence:
                    raise SeedCoordinatorError("seed_heartbeat_sequence_stale")
                renewed_until = max(
                    member.lease_expires_at,
                    now + self._lease_seconds,
                )
                if renewed_until <= member.lease_expires_at:
                    renewed_until = math.nextafter(
                        member.lease_expires_at,
                        math.inf,
                    )
                renewal_message_id = _segment(self._id_source(), "message_id")
                if renewal_message_id in self._emitted_ids:
                    raise SeedCoordinatorError("seed_message_id_reused")
                renewal_message = {
                    "protocol": LEASE_RENEWAL_PROTOCOL,
                    "message_id": renewal_message_id,
                    "swarm_id": self.swarm_id,
                    "sender_node_id": self.seed_node_id,
                    "sender_endpoint_id": self.signer.endpoint_id,
                    "recipient_node_id": member.node_id,
                    "incarnation": self.incarnation,
                    "generation": member.generation,
                    "issued_at": now,
                    "expires_at": renewed_until,
                    "heartbeat_message_id": message["message_id"],
                    "member_incarnation": member.incarnation,
                    "membership_generation": member.generation,
                    "lease_expires_at": renewed_until,
                }
                renewal = sign_membership_message(
                    signer=self.signer,
                    message=renewal_message,
                )
                persisted_member = member.projection()
                persisted_member["last_heartbeat_sequence"] = sequence
                persisted_member["lease_expires_at"] = renewed_until
                persisted_member["last_liveness_at"] = now
                heartbeat_intervals = (
                    2
                    if message["liveness_source"] == "activation_receipt"
                    else 1
                )
                next_heartbeat_due_at = (
                    now + heartbeat_intervals * self._keepalive_interval_seconds
                )
                persisted_member["next_heartbeat_due_at"] = next_heartbeat_due_at
                persisted_member["active_requests"] = message["active_requests"]
                persisted_member["lifecycle_state"] = message["lifecycle_state"]
                if message["liveness_source"] == "activation_receipt":
                    persisted_member["last_activity_receipt_at"] = now
                try:
                    committed = self._state.commit_heartbeat_renewal(
                        request_envelope_digest=request_digest,
                        heartbeat_message_id=message["message_id"],
                        heartbeat_sequence=sequence,
                        heartbeat_expires_at=float(message["expires_at"]),
                        renewal_message_id=renewal_message_id,
                        member=persisted_member,
                        renewal=renewal,
                        now=now,
                        capacity=_MAX_REPLAY_IDS,
                    )
                except SeedStateError as exc:
                    raise SeedCoordinatorError(exc.code) from exc
                committed_message = self._validate_committed_heartbeat_renewal(
                    committed,
                    member=member,
                    heartbeat_message_id=message["message_id"],
                    now=now,
                )
                self._emitted_ids.add(committed_message["message_id"])
            elif expected_protocol == ASSIGNMENT_RESULT_PROTOCOL:
                self._ensure_not_replayed(member, message["message_id"])
                assignment = self._assignments.get(message["assignment_id"])
                if (
                    assignment is None
                    or assignment["node_id"] != node_id
                    or assignment["deployment_id"] != message["deployment_id"]
                    or assignment["deployment_epoch"] != message["deployment_epoch"]
                    or assignment["membership_generation"] != member.generation
                ):
                    raise SeedCoordinatorError("seed_assignment_result_mismatch")
                if assignment["accepted"] is not None:
                    raise SeedCoordinatorError(
                        "seed_assignment_result_already_recorded"
                    )
                updated_assignment = {
                    **assignment,
                    "accepted": message["accepted"],
                    "result_code": message["result_code"],
                    "load_proof_digest": message["load_proof_digest"],
                    "runtime_endpoint": message["runtime_endpoint"],
                }
                self._persist("save_assignment_result", updated_assignment)
            else:
                self._ensure_not_replayed(member, message["message_id"])
            if expected_protocol != HEARTBEAT_PROTOCOL:
                self._remember(member, message, now)
            if expected_protocol == HEARTBEAT_PROTOCOL:
                self._apply_committed_heartbeat(
                    member,
                    heartbeat=message,
                    renewal=committed,
                    renewal_message=committed_message,
                )
            elif expected_protocol == ASSIGNMENT_RESULT_PROTOCOL:
                assignment.update(updated_assignment)
            member.latest_messages[expected_protocol] = dict(message)
            return message

    def lease_renewal(
        self,
        *,
        node_id: str,
        heartbeat_message_id: str,
    ) -> dict[str, Any]:
        node_id = _segment(node_id, "node_id")
        heartbeat_message_id = _segment(
            heartbeat_message_id,
            "heartbeat_message_id",
        )
        with self._lock:
            member = self._members.get(node_id)
            if member is None:
                raise SeedCoordinatorError("seed_member_unknown")
            self._ensure_current_member(member)
            envelope = member.latest_messages.get(LEASE_RENEWAL_PROTOCOL)
            if (
                envelope is None
                or envelope.get("message", {}).get("heartbeat_message_id")
                != heartbeat_message_id
            ):
                try:
                    envelope = self._state.find_heartbeat_renewal(
                        node_id=member.node_id,
                        generation=member.generation,
                        heartbeat_message_id=heartbeat_message_id,
                    )
                except SeedStateError as exc:
                    raise SeedCoordinatorError(exc.code) from exc
            if envelope is None:
                raise SeedCoordinatorError("seed_lease_renewal_unknown")
            self._validate_committed_heartbeat_renewal(
                envelope,
                member=member,
                heartbeat_message_id=heartbeat_message_id,
                now=self._now(),
            )
            member.latest_messages[LEASE_RENEWAL_PROTOCOL] = dict(envelope)
            return dict(envelope)

    def assignment_offer(
        self,
        *,
        node_id: str,
        deployment_id: str,
        deployment_epoch: int,
        assignment_id: str,
        assignment_digest: str,
        stage_pack_digest: str,
        graph_digest: str,
        load_generation: int,
        peer_node_ids: Sequence[str],
        placement_provenance: str,
    ) -> dict[str, Any]:
        node_id = _segment(node_id, "node_id")
        assignment_id = _segment(assignment_id, "assignment_id")
        with self._lock:
            member = self._members.get(node_id)
            if member is None:
                raise SeedCoordinatorError("seed_member_unknown")
            now = self._now()
            if now >= member.lease_expires_at:
                raise SeedCoordinatorError("seed_member_lease_expired")
            self._ensure_current_member(member)
            liveness = self._liveness_projection(member, now=now)
            if liveness["liveness_dead"]:
                raise SeedCoordinatorError("seed_member_liveness_dead")
            if liveness["liveness_status"] not in {"idle_fresh", "active_fresh"}:
                raise SeedCoordinatorError("seed_member_liveness_stale")
            if not peer_runtime_is_activation_eligible(
                member.peer_class,
                member.runtime_capability,
            ):
                raise SeedCoordinatorError("seed_member_activation_ineligible")
            if (
                isinstance(peer_node_ids, (str, bytes))
                or not isinstance(peer_node_ids, Sequence)
                or len(peer_node_ids) > 256
            ):
                raise ValueError("peer_node_ids is invalid")
            normalized_peer_ids = [
                _segment(peer_node_id, "peer_node_id")
                for peer_node_id in peer_node_ids
            ]
            if (
                len(set(normalized_peer_ids)) != len(normalized_peer_ids)
                or node_id in normalized_peer_ids
            ):
                raise ValueError("peer_node_ids is invalid")
            peers: list[_Member] = []
            for peer_node_id in sorted(normalized_peer_ids):
                peer = self._members.get(peer_node_id)
                if peer is None:
                    raise SeedCoordinatorError("seed_peer_unknown")
                if now >= peer.lease_expires_at:
                    raise SeedCoordinatorError("seed_peer_lease_expired")
                self._ensure_current_member(peer)
                peer_liveness = self._liveness_projection(peer, now=now)
                if peer_liveness["liveness_dead"]:
                    raise SeedCoordinatorError("seed_peer_liveness_dead")
                if peer_liveness["liveness_status"] not in {
                    "idle_fresh",
                    "active_fresh",
                }:
                    raise SeedCoordinatorError("seed_peer_liveness_stale")
                if not peer_runtime_is_activation_eligible(
                    peer.peer_class,
                    peer.runtime_capability,
                ):
                    raise SeedCoordinatorError("seed_peer_activation_ineligible")
                peers.append(peer)
            if assignment_id in self._assignments:
                raise SeedCoordinatorError("seed_assignment_exists")
            expires_at = min(
                now + self._message_ttl_seconds,
                member.lease_expires_at,
                *(peer.lease_expires_at for peer in peers),
            )
            message = {
                "protocol": ASSIGNMENT_OFFER_PROTOCOL,
                "message_id": self._new_message_id(),
                "swarm_id": self.swarm_id,
                "sender_node_id": self.seed_node_id,
                "sender_endpoint_id": self.signer.endpoint_id,
                "recipient_node_id": node_id,
                "incarnation": self.incarnation,
                "generation": member.generation,
                "issued_at": now,
                "expires_at": expires_at,
                "deployment_id": deployment_id,
                "deployment_epoch": deployment_epoch,
                "assignment_id": assignment_id,
                "assignment_digest": assignment_digest,
                "stage_pack_digest": stage_pack_digest,
                "graph_digest": graph_digest,
                "load_generation": load_generation,
                "placement_provenance": placement_provenance,
                "peer_endpoint_records": [
                    {
                        "node_id": peer.node_id,
                        "endpoint_id": peer.endpoint_id,
                        "deployment_epoch": deployment_epoch,
                        "membership_generation": peer.generation,
                        "valid_from": now,
                        "valid_until": expires_at,
                    }
                    for peer in peers
                ],
            }
            envelope = sign_membership_message(signer=self.signer, message=message)
            assignment = {
                "node_id": node_id,
                "deployment_id": deployment_id,
                "deployment_epoch": deployment_epoch,
                "membership_generation": member.generation,
                "assignment_id": assignment_id,
                "accepted": None,
                "result_code": None,
                "load_proof_digest": None,
                "runtime_endpoint": None,
            }
            self._persist("save_assignment", assignment)
            self._assignments[assignment_id] = assignment
            return envelope

    def assignment_status(self, assignment_id: str) -> dict[str, Any]:
        assignment_id = _segment(assignment_id, "assignment_id")
        with self._lock:
            assignment = self._assignments.get(assignment_id)
            if assignment is None:
                raise SeedCoordinatorError("seed_assignment_unknown")
            return dict(assignment)
