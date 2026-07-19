"""Strict privacy-preserving semantic contracts for the Observatory live lane."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Callable, Mapping, Optional, Union

from .observatory import CoherentSnapshotPublisher, MAX_SAFE_GENERATION, Publication


OBSERVATORY_SNAPSHOT_PROTOCOL = "mycelium.observatory.snapshot.v1"
OBSERVATORY_EVENT_PROTOCOL = "mycelium.observatory.event.v1"

_SNAPSHOT_KEYS = frozenset(
    {
        "protocol",
        "snapshot_id",
        "freshness",
        "binding",
        "claims",
        "conflicts",
        "route_challenge",
        "request_lifecycle",
        "provenance",
    }
)
_EVENT_KEYS = frozenset({"protocol", "generation", "snapshot"})
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
_CREDENTIAL_RE = re.compile(
    r"(?i)(?:\bbearer\s+|\bsk-[a-z0-9_-]{12,}|\bgh[pousr]_[a-z0-9]{20,}|"
    r"\bgithub_pat_[a-z0-9_]{20,}|-----BEGIN[ A-Z0-9_-]{0,48}PRIVATE KEY-----)"
)
_SCOPE_STATEMENT = {
    "deployment": "deployment_bound",
    "model": "model_bound",
    "route": "route_challenge_succeeded",
    "assignment": "assignment_ready",
    "request": "request_lifecycle_observed",
}
_SCOPE_PROVENANCE = {
    "deployment": ("gateway_projection", "mycelium_gateway"),
    "model": ("provisioning_audit", "mycelium_provisioning"),
    "route": ("route_challenge", "mycelium_router"),
    "assignment": ("provisioning_audit", "mycelium_provisioning"),
    "request": ("router_runtime", "mycelium_router"),
}
_PROVENANCE_PAIRS = frozenset(
    {
        ("gateway_projection", "mycelium_gateway"),
        ("provisioning_audit", "mycelium_provisioning"),
        ("route_challenge", "mycelium_router"),
        ("router_runtime", "mycelium_router"),
    }
)
_LIFECYCLE_STATES = frozenset({"admitting", "prefill", "locked", "decoding", "completed", "failed", "cancelled"})
_CONFLICT_REASONS = frozenset({"binding_mismatch", "value_mismatch", "freshness_overlap"})


class SemanticValidationError(ValueError):
    """A candidate is not an exact privacy-safe Observatory semantic document."""


class UnsupportedSemanticProtocolError(SemanticValidationError):
    """A semantic protocol major is absent or unsupported."""


class StaleSemanticSnapshotError(SemanticValidationError):
    """A structurally valid snapshot is not current enough to publish."""


class ObservatoryOwnerError(RuntimeError):
    """The single publication/SSE ownership lease cannot be acquired or used."""


@dataclass(frozen=True)
class ObservatoryQualification:
    live: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class SemanticPublication:
    generation: int
    snapshot: Mapping[str, Any]
    envelope_json: bytes

    @property
    def protocol(self) -> str:
        return OBSERVATORY_EVENT_PROTOCOL

    def envelope(self) -> dict[str, Any]:
        return json.loads(self.envelope_json)


def _object(value: Any, *, keys: frozenset[str], path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SemanticValidationError(f"{path} must be an object")
    if set(value) != keys:
        raise SemanticValidationError(f"{path} has unknown or missing fields")
    if any(not isinstance(key, str) or not key.isascii() for key in value):
        raise SemanticValidationError(f"{path} field names must be ASCII")
    return value


def _array(value: Any, *, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise SemanticValidationError(f"{path} must be an array")
    return value


def _reject_sensitive_text(value: str, *, path: str) -> None:
    if _CREDENTIAL_RE.search(value):
        raise SemanticValidationError(f"{path} contains prohibited credential-shaped material")
    if "/" in value or "\\" in value or "://" in value:
        raise SemanticValidationError(f"{path} must not contain an endpoint or path")
    try:
        ipaddress.ip_address(value.strip("[]"))
    except ValueError:
        return
    raise SemanticValidationError(f"{path} must not contain an IP address")


def _identifier(value: Any, *, path: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        raise SemanticValidationError(f"{path} must be a bounded public identifier")
    _reject_sensitive_text(value, path=path)
    return value


def _digest(value: Any, *, path: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise SemanticValidationError(f"{path} must be a lowercase sha256 digest")
    return value


def _integer(value: Any, *, path: str, minimum: int = 0) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > MAX_SAFE_GENERATION
    ):
        raise SemanticValidationError(f"{path} must be a safe integer >= {minimum}")
    return value


def _timestamp(value: Any, *, path: str) -> tuple[str, datetime]:
    if not isinstance(value, str) or _TIMESTAMP_RE.fullmatch(value) is None:
        raise SemanticValidationError(f"{path} must be an RFC3339 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise SemanticValidationError(f"{path} must be an RFC3339 UTC timestamp") from exc
    return value, parsed


def _freshness(value: Any, *, path: str) -> tuple[dict[str, str], datetime, datetime]:
    candidate = _object(
        value,
        keys=frozenset({"observed_at", "valid_until"}),
        path=path,
    )
    observed_text, observed = _timestamp(candidate["observed_at"], path=f"{path}.observed_at")
    valid_text, valid = _timestamp(candidate["valid_until"], path=f"{path}.valid_until")
    if observed >= valid:
        raise SemanticValidationError(f"{path} must have observed_at before valid_until")
    return {"observed_at": observed_text, "valid_until": valid_text}, observed, valid


def _bounded_freshness(
    value: Any,
    *,
    path: str,
    snapshot_observed: datetime,
    snapshot_valid: datetime,
) -> dict[str, str]:
    copied, observed, valid = _freshness(value, path=path)
    if observed < snapshot_observed or valid > snapshot_valid:
        raise SemanticValidationError(f"{path} must remain inside snapshot freshness")
    return copied


def _provenance(
    value: Any,
    *,
    path: str,
    required: Optional[tuple[str, str]] = None,
) -> dict[str, str]:
    candidate = _object(value, keys=frozenset({"kind", "producer"}), path=path)
    kind = _identifier(candidate["kind"], path=f"{path}.kind")
    producer = _identifier(candidate["producer"], path=f"{path}.producer")
    pair = (kind, producer)
    if pair not in _PROVENANCE_PAIRS or (required is not None and pair != required):
        raise SemanticValidationError(f"{path} has unsupported provenance")
    return {"kind": kind, "producer": producer}


def _binding(value: Any, *, path: str) -> dict[str, Any]:
    candidate = _object(
        value,
        keys=frozenset({"deployment", "model", "route"}),
        path=path,
    )
    deployment = _object(
        candidate["deployment"],
        keys=frozenset({"id", "epoch"}),
        path=f"{path}.deployment",
    )
    copied_deployment = {
        "id": _identifier(deployment["id"], path=f"{path}.deployment.id"),
        "epoch": _integer(deployment["epoch"], path=f"{path}.deployment.epoch", minimum=1),
    }

    model = _object(
        candidate["model"],
        keys=frozenset({"id", "revision", "manifest_digest", "num_layers"}),
        path=f"{path}.model",
    )
    copied_model = {
        "id": _identifier(model["id"], path=f"{path}.model.id"),
        "revision": _identifier(model["revision"], path=f"{path}.model.revision"),
        "manifest_digest": _digest(
            model["manifest_digest"], path=f"{path}.model.manifest_digest"
        ),
        "num_layers": _integer(model["num_layers"], path=f"{path}.model.num_layers", minimum=1),
    }

    route = _object(
        candidate["route"],
        keys=frozenset({"id", "generation", "digest", "assignments"}),
        path=f"{path}.route",
    )
    assignments = _array(route["assignments"], path=f"{path}.route.assignments")
    if not assignments:
        raise SemanticValidationError(f"{path}.route.assignments must not be empty")
    copied_assignments: list[dict[str, Any]] = []
    assignment_ids: set[str] = set()
    next_layer = 0
    for index, assignment_value in enumerate(assignments):
        assignment_path = f"{path}.route.assignments[{index}]"
        assignment = _object(
            assignment_value,
            keys=frozenset({"id", "peer_id", "start_layer", "end_layer_exclusive"}),
            path=assignment_path,
        )
        assignment_id = _identifier(assignment["id"], path=f"{assignment_path}.id")
        if assignment_id in assignment_ids:
            raise SemanticValidationError(f"{path}.route assignment ids must be unique")
        assignment_ids.add(assignment_id)
        start = _integer(assignment["start_layer"], path=f"{assignment_path}.start_layer")
        end = _integer(
            assignment["end_layer_exclusive"],
            path=f"{assignment_path}.end_layer_exclusive",
            minimum=1,
        )
        if start != next_layer or end <= start:
            raise SemanticValidationError(
                f"{path}.route assignments must provide ordered contiguous half-open coverage"
            )
        next_layer = end
        copied_assignments.append(
            {
                "id": assignment_id,
                "peer_id": _identifier(
                    assignment["peer_id"], path=f"{assignment_path}.peer_id"
                ),
                "start_layer": start,
                "end_layer_exclusive": end,
            }
        )
    if next_layer != copied_model["num_layers"]:
        raise SemanticValidationError(f"{path}.route assignments must cover every model layer")

    copied_route = {
        "id": _identifier(route["id"], path=f"{path}.route.id"),
        "generation": _integer(
            route["generation"], path=f"{path}.route.generation", minimum=1
        ),
        "digest": _digest(route["digest"], path=f"{path}.route.digest"),
        "assignments": copied_assignments,
    }
    return {"deployment": copied_deployment, "model": copied_model, "route": copied_route}


def _scope(
    value: Any,
    *,
    path: str,
    binding: Mapping[str, Any],
    request_id: str,
) -> dict[str, str]:
    candidate = _object(value, keys=frozenset({"kind", "id"}), path=path)
    kind = candidate["kind"]
    if not isinstance(kind, str) or kind not in _SCOPE_STATEMENT:
        raise SemanticValidationError(f"{path}.kind has unsupported claim scope")
    identifier = _identifier(candidate["id"], path=f"{path}.id")
    allowed_ids = {
        "deployment": {binding["deployment"]["id"]},
        "model": {binding["model"]["id"]},
        "route": {binding["route"]["id"]},
        "assignment": {item["id"] for item in binding["route"]["assignments"]},
        "request": {request_id},
    }
    if identifier not in allowed_ids[kind]:
        raise SemanticValidationError(f"{path}.id is outside the exact snapshot binding")
    return {"kind": kind, "id": identifier}


def _canonical_copy(document: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(
            json.dumps(
                document,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise SemanticValidationError("semantic document cannot be encoded as strict JSON") from exc


def decode_observatory_snapshot(value: Any) -> dict[str, Any]:
    """Decode one exact v1 public projection; unknown fields and majors fail closed."""
    if not isinstance(value, dict):
        raise SemanticValidationError("snapshot must be an object")
    protocol = value.get("protocol")
    if protocol != OBSERVATORY_SNAPSHOT_PROTOCOL:
        raise UnsupportedSemanticProtocolError("unsupported Observatory snapshot protocol")
    candidate = _object(value, keys=_SNAPSHOT_KEYS, path="snapshot")
    snapshot_id = _identifier(candidate["snapshot_id"], path="snapshot.snapshot_id")
    copied_freshness, snapshot_observed, snapshot_valid = _freshness(
        candidate["freshness"], path="snapshot.freshness"
    )
    copied_binding = _binding(candidate["binding"], path="snapshot.binding")
    copied_top_provenance = _provenance(
        candidate["provenance"],
        path="snapshot.provenance",
        required=("gateway_projection", "mycelium_gateway"),
    )

    lifecycle = _object(
        candidate["request_lifecycle"],
        keys=frozenset(
            {"request_id", "state", "path_attempt", "freshness", "binding", "provenance"}
        ),
        path="snapshot.request_lifecycle",
    )
    request_id = _identifier(
        lifecycle["request_id"], path="snapshot.request_lifecycle.request_id"
    )
    lifecycle_state = lifecycle["state"]
    if not isinstance(lifecycle_state, str) or lifecycle_state not in _LIFECYCLE_STATES:
        raise SemanticValidationError("snapshot.request_lifecycle.state is unsupported")
    copied_lifecycle_binding = _binding(
        lifecycle["binding"], path="snapshot.request_lifecycle.binding"
    )
    if copied_lifecycle_binding != copied_binding:
        raise SemanticValidationError("request lifecycle binding does not exactly match snapshot binding")
    copied_lifecycle = {
        "request_id": request_id,
        "state": lifecycle_state,
        "path_attempt": _integer(
            lifecycle["path_attempt"],
            path="snapshot.request_lifecycle.path_attempt",
            minimum=1,
        ),
        "freshness": _bounded_freshness(
            lifecycle["freshness"],
            path="snapshot.request_lifecycle.freshness",
            snapshot_observed=snapshot_observed,
            snapshot_valid=snapshot_valid,
        ),
        "binding": copied_lifecycle_binding,
        "provenance": _provenance(
            lifecycle["provenance"],
            path="snapshot.request_lifecycle.provenance",
            required=("router_runtime", "mycelium_router"),
        ),
    }

    challenge = _object(
        candidate["route_challenge"],
        keys=frozenset({"id", "status", "freshness", "binding", "provenance"}),
        path="snapshot.route_challenge",
    )
    challenge_status = challenge["status"]
    if challenge_status not in {"succeeded", "failed"}:
        raise SemanticValidationError("snapshot.route_challenge.status is unsupported")
    copied_challenge_binding = _binding(
        challenge["binding"], path="snapshot.route_challenge.binding"
    )
    if copied_challenge_binding != copied_binding:
        raise SemanticValidationError("route challenge binding does not exactly match snapshot binding")
    copied_challenge = {
        "id": _identifier(challenge["id"], path="snapshot.route_challenge.id"),
        "status": challenge_status,
        "freshness": _bounded_freshness(
            challenge["freshness"],
            path="snapshot.route_challenge.freshness",
            snapshot_observed=snapshot_observed,
            snapshot_valid=snapshot_valid,
        ),
        "binding": copied_challenge_binding,
        "provenance": _provenance(
            challenge["provenance"],
            path="snapshot.route_challenge.provenance",
            required=("route_challenge", "mycelium_router"),
        ),
    }

    claims = _array(candidate["claims"], path="snapshot.claims")
    copied_claims: list[dict[str, Any]] = []
    claim_ids: set[str] = set()
    claims_by_id: dict[str, dict[str, Any]] = {}
    claims_by_semantic_key: dict[tuple[str, str, str], set[str]] = {}
    for index, claim_value in enumerate(claims):
        claim_path = f"snapshot.claims[{index}]"
        claim = _object(
            claim_value,
            keys=frozenset(
                {"id", "scope", "statement", "value", "freshness", "provenance"}
            ),
            path=claim_path,
        )
        claim_id = _identifier(claim["id"], path=f"{claim_path}.id")
        if claim_id in claim_ids:
            raise SemanticValidationError("snapshot claim ids must be unique")
        claim_ids.add(claim_id)
        copied_scope = _scope(
            claim["scope"],
            path=f"{claim_path}.scope",
            binding=copied_binding,
            request_id=request_id,
        )
        statement = claim["statement"]
        if statement != _SCOPE_STATEMENT[copied_scope["kind"]]:
            raise SemanticValidationError(f"{claim_path}.statement does not match its scope")
        claim_value_name = claim["value"]
        if claim_value_name not in {"confirmed", "rejected", "unknown"}:
            raise SemanticValidationError(f"{claim_path}.value is unsupported")
        copied_claim = {
            "id": claim_id,
            "scope": copied_scope,
            "statement": statement,
            "value": claim_value_name,
            "freshness": _bounded_freshness(
                claim["freshness"],
                path=f"{claim_path}.freshness",
                snapshot_observed=snapshot_observed,
                snapshot_valid=snapshot_valid,
            ),
            "provenance": _provenance(
                claim["provenance"],
                path=f"{claim_path}.provenance",
                required=_SCOPE_PROVENANCE[copied_scope["kind"]],
            ),
        }
        copied_claims.append(copied_claim)
        claims_by_id[claim_id] = copied_claim
        semantic_key = (copied_scope["kind"], copied_scope["id"], statement)
        claims_by_semantic_key.setdefault(semantic_key, set()).add(claim_id)

    conflicts = _array(candidate["conflicts"], path="snapshot.conflicts")
    copied_conflicts: list[dict[str, Any]] = []
    reported_conflict_groups: set[frozenset[str]] = set()
    for index, conflict_value in enumerate(conflicts):
        conflict_path = f"snapshot.conflicts[{index}]"
        conflict = _object(
            conflict_value,
            keys=frozenset({"claim_ids", "scope", "reason"}),
            path=conflict_path,
        )
        conflict_claim_ids = _array(conflict["claim_ids"], path=f"{conflict_path}.claim_ids")
        copied_conflict_ids = [
            _identifier(item, path=f"{conflict_path}.claim_ids[{claim_index}]")
            for claim_index, item in enumerate(conflict_claim_ids)
        ]
        if (
            len(copied_conflict_ids) < 2
            or len(copied_conflict_ids) != len(set(copied_conflict_ids))
            or any(item not in claim_ids for item in copied_conflict_ids)
        ):
            raise SemanticValidationError(
                f"{conflict_path}.claim_ids must reference at least two unique claims"
            )
        reason = conflict["reason"]
        if reason not in _CONFLICT_REASONS:
            raise SemanticValidationError(f"{conflict_path}.reason is unsupported")
        copied_conflict_scope = _scope(
            conflict["scope"],
            path=f"{conflict_path}.scope",
            binding=copied_binding,
            request_id=request_id,
        )
        if any(
            claims_by_id[item]["scope"] != copied_conflict_scope
            for item in copied_conflict_ids
        ):
            raise SemanticValidationError(
                f"{conflict_path}.claim_ids must all match the exact conflict scope"
            )
        reported_conflict_groups.add(frozenset(copied_conflict_ids))
        copied_conflicts.append(
            {
                "claim_ids": copied_conflict_ids,
                "scope": copied_conflict_scope,
                "reason": reason,
            }
        )

    for semantic_claim_ids in claims_by_semantic_key.values():
        if (
            len(semantic_claim_ids) > 1
            and frozenset(semantic_claim_ids) not in reported_conflict_groups
        ):
            raise SemanticValidationError(
                "duplicate semantic claims must be represented by one exact conflict"
            )

    document = {
        "protocol": OBSERVATORY_SNAPSHOT_PROTOCOL,
        "snapshot_id": snapshot_id,
        "freshness": copied_freshness,
        "binding": copied_binding,
        "claims": copied_claims,
        "conflicts": copied_conflicts,
        "route_challenge": copied_challenge,
        "request_lifecycle": copied_lifecycle,
        "provenance": copied_top_provenance,
    }
    return _canonical_copy(document)


def decode_observatory_event(value: Any) -> dict[str, Any]:
    """Decode one complete v1 event containing one complete semantic snapshot."""
    if not isinstance(value, dict):
        raise SemanticValidationError("event must be an object")
    protocol = value.get("protocol")
    if protocol != OBSERVATORY_EVENT_PROTOCOL:
        raise UnsupportedSemanticProtocolError("unsupported Observatory event protocol")
    candidate = _object(value, keys=_EVENT_KEYS, path="event")
    generation = _integer(candidate["generation"], path="event.generation", minimum=1)
    return {
        "protocol": OBSERVATORY_EVENT_PROTOCOL,
        "generation": generation,
        "snapshot": decode_observatory_snapshot(candidate["snapshot"]),
    }


def _fresh_at(value: Mapping[str, str], now: datetime) -> bool:
    _, observed = _timestamp(value["observed_at"], path="freshness.observed_at")
    _, valid = _timestamp(value["valid_until"], path="freshness.valid_until")
    return observed <= now < valid


def qualify_observatory_snapshot(
    snapshot: Mapping[str, Any],
    *,
    now: Optional[datetime] = None,
) -> ObservatoryQualification:
    """Derive the truthful live gate; no producer-supplied live boolean is trusted."""
    decoded = decode_observatory_snapshot(dict(snapshot))
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    instant = instant.astimezone(timezone.utc)
    reasons: list[str] = []
    if not _fresh_at(decoded["freshness"], instant):
        reasons.append("snapshot_stale")
    if decoded["conflicts"]:
        reasons.append("conflicts_present")
    challenge = decoded["route_challenge"]
    if challenge["status"] != "succeeded":
        reasons.append("route_challenge_not_successful")
    if not _fresh_at(challenge["freshness"], instant):
        reasons.append("route_challenge_stale")
    lifecycle = decoded["request_lifecycle"]
    if lifecycle["state"] != "completed":
        reasons.append("request_lifecycle_not_completed")
    if not _fresh_at(lifecycle["freshness"], instant):
        reasons.append("request_lifecycle_stale")

    required = {
        ("deployment", decoded["binding"]["deployment"]["id"], "deployment_bound"),
        ("model", decoded["binding"]["model"]["id"], "model_bound"),
        ("route", decoded["binding"]["route"]["id"], "route_challenge_succeeded"),
        ("request", lifecycle["request_id"], "request_lifecycle_observed"),
    }
    required.update(
        ("assignment", assignment["id"], "assignment_ready")
        for assignment in decoded["binding"]["route"]["assignments"]
    )
    indexed = {
        (claim["scope"]["kind"], claim["scope"]["id"], claim["statement"]): claim
        for claim in decoded["claims"]
    }
    missing = required - set(indexed)
    if missing:
        reasons.append("required_claim_missing")
    required_claims = [indexed[key] for key in required if key in indexed]
    if any(claim["value"] != "confirmed" for claim in required_claims):
        reasons.append("required_claim_not_confirmed")
    if any(not _fresh_at(claim["freshness"], instant) for claim in required_claims):
        reasons.append("required_claim_stale")
    return ObservatoryQualification(live=not reasons, reasons=tuple(dict.fromkeys(reasons)))


def _event_bytes(generation: int, snapshot: Mapping[str, Any]) -> bytes:
    return json.dumps(
        {
            "protocol": OBSERVATORY_EVENT_PROTOCOL,
            "generation": generation,
            "snapshot": snapshot,
        },
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


class SemanticSnapshotSubscription:
    """Translate the internal coherent stream into public event-v1 publications."""

    def __init__(self, owner: "ObservatoryPublicationOwner", subscription: Any) -> None:
        self._owner = owner
        self._subscription = subscription
        self._replay = tuple(owner._semantic_publication(item) for item in subscription.replay)

    @property
    def replay(self) -> tuple[SemanticPublication, ...]:
        return self._replay

    @property
    def closed(self) -> bool:
        return bool(self._subscription.closed)

    @property
    def disconnect_reason(self) -> Optional[str]:
        return self._subscription.disconnect_reason

    def get_nowait(self) -> SemanticPublication:
        return self._owner._semantic_publication(self._subscription.get_nowait())

    def close(self) -> None:
        self._subscription.close()


class ObservatoryPublicationOwner:
    """Sole owner of semantic publication, durable generation, and SSE fan-out."""

    def __init__(
        self,
        state_path: Union[str, os.PathLike[str]],
        *,
        now: Optional[Callable[[], datetime]] = None,
        max_payload_bytes: int = 2 * 1024 * 1024,
        max_nesting: int = 32,
        replay_capacity: int = 32,
        max_subscribers: int = 64,
        subscriber_queue_size: int = 8,
    ) -> None:
        self._state_path = Path(state_path).expanduser().absolute()
        self._state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._closed = False
        self._lease_descriptor: Optional[int] = None
        self._acquire_owner_lease()
        try:
            self._publisher = CoherentSnapshotPublisher(
                self._state_path,
                max_payload_bytes=max_payload_bytes,
                max_nesting=max_nesting,
                replay_capacity=replay_capacity,
                max_subscribers=max_subscribers,
                subscriber_queue_size=subscriber_queue_size,
            )
            current = self._publisher.current_publication()
            if current is not None:
                self._semantic_publication(current)
        except Exception:
            self.close()
            raise

    def _acquire_owner_lease(self) -> None:
        lease_path = self._state_path.with_name(self._state_path.name + ".owner")
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor: Optional[int] = None
        try:
            descriptor = os.open(os.fspath(lease_path), flags, 0o600)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
                raise ObservatoryOwnerError("Observatory owner lease must be an owned regular file")
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            if descriptor is not None:
                os.close(descriptor)
            raise ObservatoryOwnerError("Observatory publication/SSE owner is already active") from exc
        except Exception:
            if descriptor is not None:
                os.close(descriptor)
            raise
        self._lease_descriptor = descriptor

    def _assert_open(self) -> None:
        if self._closed:
            raise ObservatoryOwnerError("Observatory publication/SSE owner is closed")

    def _semantic_publication(self, publication: Publication) -> SemanticPublication:
        internal = publication.envelope()
        snapshot = decode_observatory_snapshot(internal["bundle"]["snapshot"])
        return SemanticPublication(
            generation=publication.generation,
            snapshot=snapshot,
            envelope_json=_event_bytes(publication.generation, snapshot),
        )

    @property
    def subscriber_count(self) -> int:
        return self._publisher.subscriber_count

    def publish_snapshot(self, snapshot: Any) -> SemanticPublication:
        self._assert_open()
        decoded = decode_observatory_snapshot(snapshot)
        instant = self._now()
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ObservatoryOwnerError("owner clock must return a timezone-aware datetime")
        _, observed = _timestamp(decoded["freshness"]["observed_at"], path="freshness.observed_at")
        _, valid = _timestamp(decoded["freshness"]["valid_until"], path="freshness.valid_until")
        instant = instant.astimezone(timezone.utc)
        if not observed <= instant < valid:
            raise StaleSemanticSnapshotError("semantic snapshot is stale or not yet current")
        publication = self._publisher.publish(
            {
                "snapshot": decoded,
                "incidents": [],
                "provisioning": {"semantic_protocol": OBSERVATORY_SNAPSHOT_PROTOCOL},
            }
        )
        return self._semantic_publication(publication)

    def current_publication(self) -> Optional[SemanticPublication]:
        self._assert_open()
        current = self._publisher.current_publication()
        return None if current is None else self._semantic_publication(current)

    def snapshot_json(self) -> Optional[bytes]:
        current = self.current_publication()
        return None if current is None else current.envelope_json

    def subscribe(self, *, last_event_id: Optional[int] = None) -> SemanticSnapshotSubscription:
        self._assert_open()
        return SemanticSnapshotSubscription(
            self,
            self._publisher.subscribe(last_event_id=last_event_id),
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        descriptor = self._lease_descriptor
        self._lease_descriptor = None
        if descriptor is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def __enter__(self) -> "ObservatoryPublicationOwner":
        self._assert_open()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()
