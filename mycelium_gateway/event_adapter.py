"""Read-only qualification/request event projection for the Observatory.

The adapter validates frozen producer contracts, discards private request content, and
publishes only bounded metadata through the existing GET/SSE Observatory publisher.
It has no request, Router, qualification, cancellation, or promotion authority.
"""
from __future__ import annotations

from collections import deque
from copy import deepcopy
from dataclasses import dataclass, replace
import ipaddress
import json
import re
import threading
from typing import Any, Mapping, Optional

from mycelium_qualification.contracts import (
    ROUTE_QUALIFICATION_PROTOCOL,
    QualificationContractError,
    RouteQualificationV1,
    route_qualification_from_dict,
)
from mycelium_qualification.evidence import is_sha256_ref
from mycelium_request_gateway.contracts import (
    REQUEST_EVENT_PROTOCOL,
    AdmissionError,
    StreamEvent,
    is_safe_error_code,
    is_valid_request_id,
    qualification_binding,
)

from .observatory import CoherentSnapshotPublisher, MAX_SAFE_GENERATION, Publication


OBSERVATORY_EVENT_PROJECTION_PROTOCOL = "mycelium.observatory.request_projection.v1"
OBSERVATORY_EVENT_STATUS_PROTOCOL = "mycelium.observatory.event_adapter_status.v1"

_PUBLIC_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:~-]{0,127}$")
_HOST_PORT_RE = re.compile(r"^(?:[A-Za-z0-9.-]+):[0-9]{1,5}$")
_CREDENTIAL_RE = re.compile(
    r"(?:\bbearer\s+|\bsk-[a-z0-9_-]{12,}|\bgh[pousr]_[a-z0-9]{20,}|"
    r"\bgithub_pat_[a-z0-9_]{20,}|-----BEGIN[ A-Z0-9_-]{0,48}PRIVATE KEY-----)",
    re.IGNORECASE,
)
_KNOWN_PROTOCOLS = frozenset({ROUTE_QUALIFICATION_PROTOCOL, REQUEST_EVENT_PROTOCOL})


class EventAdapterStateError(RuntimeError):
    """Persisted state is not an exact event-adapter projection."""


class _WireError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ApplyOutcome:
    """Stable result containing metadata only; never retains an input payload."""

    applied: bool
    reason: str | None
    publication: Publication | None


@dataclass(frozen=True, slots=True)
class _QualificationState:
    projection: dict[str, Any]

    @property
    def binding(self) -> dict[str, Any]:
        return self.projection["binding"]


@dataclass(frozen=True, slots=True)
class _SessionState:
    request_id: str
    state: str
    last_kind: str
    last_sequence: int
    event_count: int
    token_count: int
    terminal: bool
    qualification_id: str
    deployment_id: str
    deployment_epoch: int
    path_manifest_digest: str
    started_at_unix_ms: int
    updated_at_unix_ms: int
    quarantine_reason: str | None = None

    def public_projection(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "state": self.state,
            "last_sequence": self.last_sequence,
            "event_count": self.event_count,
            "token_count": self.token_count,
            "terminal": self.terminal,
            "qualification_id": self.qualification_id,
            "started_at_unix_ms": self.started_at_unix_ms,
            "updated_at_unix_ms": self.updated_at_unix_ms,
            "quarantine_reason": self.quarantine_reason,
        }


@dataclass(frozen=True, slots=True)
class _Checkpoint:
    source_cursor: int
    observed_at_unix_ms: int
    qualification: _QualificationState | None
    sessions: dict[str, _SessionState]
    incidents: tuple[dict[str, Any], ...]
    dropped_quarantine_count: int


def _positive_integer(name: str, value: object, *, maximum: int | None = None) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
        or (maximum is not None and value > maximum)
    ):
        qualifier = " positive integer" if maximum is None else f" integer in [1, {maximum}]"
        raise ValueError(f"{name} must be a{qualifier}")
    return value


def _nonnegative_safe_integer(name: str, value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > MAX_SAFE_GENERATION
    ):
        raise ValueError(f"{name} must be a non-negative safe integer")
    return value


def _public_identifier(value: object) -> bool:
    if not isinstance(value, str) or _PUBLIC_IDENTIFIER_RE.fullmatch(value) is None:
        return False
    lowered = value.lower()
    if (
        "/" in value
        or "\\" in value
        or "://" in value
        or _CREDENTIAL_RE.search(value)
        or _HOST_PORT_RE.fullmatch(value) is not None
        or lowered == "localhost"
        or lowered.endswith((".localhost", ".local", ".internal", ".lan"))
    ):
        return False
    candidate = value[1:-1] if value.startswith("[") and value.endswith("]") else value
    try:
        ipaddress.ip_address(candidate.split("%", 1)[0])
    except ValueError:
        return True
    return False


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _WireError("duplicate object field")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise _WireError("non-finite JSON number")


def _parse_json_integer(value: str) -> int:
    parsed = int(value)
    if abs(parsed) > MAX_SAFE_GENERATION:
        raise _WireError("JSON integer is outside the safe range")
    return parsed


def _decode_document(payload: bytes) -> dict[str, Any]:
    try:
        text = payload.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
            parse_int=_parse_json_integer,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, _WireError, RecursionError) as exc:
        raise _WireError("invalid JSON event") from exc
    if not isinstance(value, dict):
        raise _WireError("event must be an object")
    return value


def _safe_protocol(value: object) -> str:
    return value if isinstance(value, str) and value in _KNOWN_PROTOCOLS else "unknown"


def _qualification_projection(record: RouteQualificationV1) -> dict[str, Any]:
    identifiers = (
        record.qualification_id,
        record.deployment_id,
        record.model_id,
        record.resolved_commit,
    )
    if not all(_public_identifier(value) for value in identifiers):
        raise QualificationContractError("unsafe_public_identifier")
    if not all(is_safe_error_code(code) for code in record.reason_codes):
        raise QualificationContractError("unsafe_reason_code")
    binding = qualification_binding(record).to_dict()
    return {
        "protocol": ROUTE_QUALIFICATION_PROTOCOL,
        "qualification_id": record.qualification_id,
        "issued_at_unix_ms": record.issued_at_unix_ms,
        "evidence_class": record.evidence_class,
        "route_ready": False,
        "reason_codes": list(record.reason_codes),
        "binding": binding,
    }


def _exact_keys(value: object, keys: set[str]) -> bool:
    return isinstance(value, dict) and set(value) == keys


class ObservatoryEventAdapter:
    """Bounded, deterministic, read-only adapter over frozen producer events."""

    def __init__(
        self,
        publisher: CoherentSnapshotPublisher,
        *,
        max_event_bytes: int = 256 * 1024,
        max_sessions: int = 64,
        quarantine_capacity: int = 16,
        max_qualification_age_ms: int = 300_000,
        max_session_idle_ms: int = 300_000,
    ) -> None:
        if not isinstance(publisher, CoherentSnapshotPublisher):
            raise TypeError("publisher must be a CoherentSnapshotPublisher")
        self._publisher = publisher
        self._max_event_bytes = _positive_integer(
            "max_event_bytes", max_event_bytes, maximum=2 * 1024 * 1024
        )
        self._max_sessions = _positive_integer(
            "max_sessions", max_sessions, maximum=256
        )
        self._quarantine_capacity = _positive_integer(
            "quarantine_capacity", quarantine_capacity, maximum=256
        )
        self._max_qualification_age_ms = _positive_integer(
            "max_qualification_age_ms", max_qualification_age_ms
        )
        self._max_session_idle_ms = _positive_integer(
            "max_session_idle_ms", max_session_idle_ms
        )
        self._mutex = threading.RLock()
        self._source_cursor = -1
        self._observed_at_unix_ms = 0
        self._qualification: _QualificationState | None = None
        self._sessions: dict[str, _SessionState] = {}
        self._incidents: deque[dict[str, Any]] = deque(maxlen=quarantine_capacity)
        self._dropped_quarantine_count = 0
        self._restore()

    @property
    def source_cursor(self) -> int:
        with self._mutex:
            return self._source_cursor

    def current_publication(self) -> Publication | None:
        return self._publisher.current_publication()

    def current_envelope(self) -> dict[str, Any] | None:
        return self._publisher.current_envelope()

    def snapshot_json(self) -> bytes | None:
        return self._publisher.snapshot_json()

    def subscribe(self, *, last_event_id: Optional[int] = None):
        return self._publisher.subscribe(last_event_id=last_event_id)

    def current_projection(self) -> dict[str, Any] | None:
        envelope = self.current_envelope()
        return None if envelope is None else envelope["bundle"]

    def _checkpoint(self) -> _Checkpoint:
        return _Checkpoint(
            source_cursor=self._source_cursor,
            observed_at_unix_ms=self._observed_at_unix_ms,
            qualification=deepcopy(self._qualification),
            sessions=dict(self._sessions),
            incidents=tuple(deepcopy(list(self._incidents))),
            dropped_quarantine_count=self._dropped_quarantine_count,
        )

    def _rollback(self, checkpoint: _Checkpoint) -> None:
        self._source_cursor = checkpoint.source_cursor
        self._observed_at_unix_ms = checkpoint.observed_at_unix_ms
        self._qualification = checkpoint.qualification
        self._sessions = dict(checkpoint.sessions)
        self._incidents = deque(
            deepcopy(checkpoint.incidents), maxlen=self._quarantine_capacity
        )
        self._dropped_quarantine_count = checkpoint.dropped_quarantine_count

    def _bundle(self) -> dict[str, Any]:
        return {
            "snapshot": {
                "protocol": OBSERVATORY_EVENT_PROJECTION_PROTOCOL,
                "source_cursor": self._source_cursor,
                "observed_at_unix_ms": self._observed_at_unix_ms,
                "qualification": (
                    None
                    if self._qualification is None
                    else deepcopy(self._qualification.projection)
                ),
                "sessions": [
                    self._sessions[request_id].public_projection()
                    for request_id in sorted(self._sessions)
                ],
            },
            "incidents": deepcopy(list(self._incidents)),
            "provisioning": {
                "protocol": OBSERVATORY_EVENT_STATUS_PROTOCOL,
                "route_ready": False,
                "source_cursor": self._source_cursor,
                "buffered_sessions": len(self._sessions),
                "quarantine_capacity": self._quarantine_capacity,
                "dropped_quarantine_count": self._dropped_quarantine_count,
            },
        }

    def _publish(self) -> Publication:
        return self._publisher.publish(self._bundle())

    def _append_incident(self, source_cursor: int, protocol: str, reason: str) -> None:
        incident = {
            "protocol": protocol,
            "source_cursor": source_cursor,
            "reason": reason,
        }
        if self._incidents and self._incidents[-1] == incident:
            return
        if len(self._incidents) == self._quarantine_capacity:
            self._dropped_quarantine_count += 1
        self._incidents.append(incident)

    def _reject(
        self,
        *,
        source_cursor: int,
        observed_at_unix_ms: int,
        protocol: str,
        reason: str,
        consume_cursor: bool = True,
    ) -> ApplyOutcome:
        if consume_cursor:
            self._source_cursor = source_cursor
            self._observed_at_unix_ms = max(
                self._observed_at_unix_ms, observed_at_unix_ms
            )
        self._append_incident(source_cursor, protocol, reason)
        return ApplyOutcome(applied=False, reason=reason, publication=self._publish())

    def _consume_duplicate(
        self, source_cursor: int, observed_at_unix_ms: int, reason: str
    ) -> ApplyOutcome:
        self._source_cursor = source_cursor
        self._observed_at_unix_ms = max(
            self._observed_at_unix_ms, observed_at_unix_ms
        )
        return ApplyOutcome(applied=False, reason=reason, publication=self._publish())

    def apply(
        self,
        source_cursor: int,
        payload: bytes,
        *,
        observed_at_unix_ms: int,
    ) -> ApplyOutcome:
        """Consume one source cursor once and publish metadata-only state."""
        cursor = _nonnegative_safe_integer("source_cursor", source_cursor)
        observed_at = _nonnegative_safe_integer(
            "observed_at_unix_ms", observed_at_unix_ms
        )
        with self._mutex:
            if cursor <= self._source_cursor:
                return ApplyOutcome(False, "duplicate_cursor", None)
            checkpoint = self._checkpoint()
            try:
                if cursor != self._source_cursor + 1:
                    return self._reject(
                        source_cursor=cursor,
                        observed_at_unix_ms=observed_at,
                        protocol="unknown",
                        reason="source_cursor_gap",
                        consume_cursor=False,
                    )
                if observed_at < self._observed_at_unix_ms:
                    return self._reject(
                        source_cursor=cursor,
                        observed_at_unix_ms=observed_at,
                        protocol="unknown",
                        reason="stale_observation_time",
                    )
                if not isinstance(payload, bytes):
                    return self._reject(
                        source_cursor=cursor,
                        observed_at_unix_ms=observed_at,
                        protocol="unknown",
                        reason="invalid_event_payload",
                    )
                if len(payload) > self._max_event_bytes:
                    return self._reject(
                        source_cursor=cursor,
                        observed_at_unix_ms=observed_at,
                        protocol="unknown",
                        reason="event_too_large",
                    )
                try:
                    document = _decode_document(payload)
                except _WireError:
                    return self._reject(
                        source_cursor=cursor,
                        observed_at_unix_ms=observed_at,
                        protocol="unknown",
                        reason="invalid_json",
                    )
                protocol = document.get("protocol")
                if protocol == ROUTE_QUALIFICATION_PROTOCOL:
                    return self._apply_qualification(cursor, observed_at, document)
                if protocol == REQUEST_EVENT_PROTOCOL:
                    return self._apply_request_event(cursor, observed_at, document)
                return self._reject(
                    source_cursor=cursor,
                    observed_at_unix_ms=observed_at,
                    protocol=_safe_protocol(protocol),
                    reason="unsupported_protocol",
                )
            except BaseException:
                self._rollback(checkpoint)
                raise

    def _apply_qualification(
        self, source_cursor: int, observed_at_unix_ms: int, document: dict[str, Any]
    ) -> ApplyOutcome:
        try:
            record = route_qualification_from_dict(document)
            projection = _qualification_projection(record)
        except (QualificationContractError, AdmissionError, TypeError, ValueError):
            return self._reject(
                source_cursor=source_cursor,
                observed_at_unix_ms=observed_at_unix_ms,
                protocol=ROUTE_QUALIFICATION_PROTOCOL,
                reason="invalid_qualification_event",
            )
        if record.issued_at_unix_ms > observed_at_unix_ms:
            return self._reject(
                source_cursor=source_cursor,
                observed_at_unix_ms=observed_at_unix_ms,
                protocol=ROUTE_QUALIFICATION_PROTOCOL,
                reason="qualification_from_future",
            )
        if observed_at_unix_ms - record.issued_at_unix_ms > self._max_qualification_age_ms:
            return self._reject(
                source_cursor=source_cursor,
                observed_at_unix_ms=observed_at_unix_ms,
                protocol=ROUTE_QUALIFICATION_PROTOCOL,
                reason="stale_qualification",
            )

        incoming = _QualificationState(projection=projection)
        current = self._qualification
        if current is not None:
            incoming_binding = incoming.binding
            current_binding = current.binding
            if (
                incoming_binding["deployment_id"] == current_binding["deployment_id"]
                and incoming_binding["deployment_epoch"]
                < current_binding["deployment_epoch"]
            ):
                return self._reject(
                    source_cursor=source_cursor,
                    observed_at_unix_ms=observed_at_unix_ms,
                    protocol=ROUTE_QUALIFICATION_PROTOCOL,
                    reason="stale_deployment_epoch",
                )
            incoming_issued = projection["issued_at_unix_ms"]
            current_issued = current.projection["issued_at_unix_ms"]
            if incoming_issued < current_issued:
                return self._reject(
                    source_cursor=source_cursor,
                    observed_at_unix_ms=observed_at_unix_ms,
                    protocol=ROUTE_QUALIFICATION_PROTOCOL,
                    reason="stale_qualification",
                )
            incoming_digest = incoming_binding["qualification_digest"]
            current_digest = current_binding["qualification_digest"]
            if incoming_digest == current_digest:
                return self._consume_duplicate(
                    source_cursor, observed_at_unix_ms, "duplicate_qualification"
                )
            if incoming_issued == current_issued:
                return self._reject(
                    source_cursor=source_cursor,
                    observed_at_unix_ms=observed_at_unix_ms,
                    protocol=ROUTE_QUALIFICATION_PROTOCOL,
                    reason="qualification_conflict",
                )
            stale_reason = self._binding_change_reason(current_binding, incoming_binding)
            if stale_reason is not None:
                self._quarantine_active_sessions(stale_reason, observed_at_unix_ms)

        self._qualification = incoming
        self._source_cursor = source_cursor
        self._observed_at_unix_ms = observed_at_unix_ms
        return ApplyOutcome(True, None, self._publish())

    @staticmethod
    def _binding_change_reason(
        current: Mapping[str, Any], incoming: Mapping[str, Any]
    ) -> str | None:
        if current["deployment_id"] != incoming["deployment_id"]:
            return "deployment_changed"
        if current["deployment_epoch"] != incoming["deployment_epoch"]:
            return "deployment_epoch_changed"
        if current["path_manifest_digest"] != incoming["path_manifest_digest"]:
            return "path_changed"
        if current["qualification_digest"] != incoming["qualification_digest"]:
            return "qualification_changed"
        return None

    def _quarantine_active_sessions(self, reason: str, observed_at_unix_ms: int) -> None:
        for request_id, session in tuple(self._sessions.items()):
            if session.terminal or session.quarantine_reason is not None:
                continue
            self._sessions[request_id] = replace(
                session,
                state="quarantined",
                updated_at_unix_ms=observed_at_unix_ms,
                quarantine_reason=reason,
            )

    def _apply_request_event(
        self, source_cursor: int, observed_at_unix_ms: int, document: dict[str, Any]
    ) -> ApplyOutcome:
        try:
            event = StreamEvent.from_dict(document)
        except (AdmissionError, TypeError, ValueError):
            return self._reject(
                source_cursor=source_cursor,
                observed_at_unix_ms=observed_at_unix_ms,
                protocol=REQUEST_EVENT_PROTOCOL,
                reason="invalid_request_event",
            )
        if not is_valid_request_id(event.request_id) or not _public_identifier(event.request_id):
            return self._reject(
                source_cursor=source_cursor,
                observed_at_unix_ms=observed_at_unix_ms,
                protocol=REQUEST_EVENT_PROTOCOL,
                reason="invalid_request_event",
            )

        session = self._sessions.get(event.request_id)
        if session is None:
            if event.kind != "accepted" or event.sequence != 0:
                return self._reject(
                    source_cursor=source_cursor,
                    observed_at_unix_ms=observed_at_unix_ms,
                    protocol=REQUEST_EVENT_PROTOCOL,
                    reason="cross_session_event",
                )
            return self._start_session(source_cursor, observed_at_unix_ms, event)

        if session.quarantine_reason is not None:
            return self._reject(
                source_cursor=source_cursor,
                observed_at_unix_ms=observed_at_unix_ms,
                protocol=REQUEST_EVENT_PROTOCOL,
                reason="stale_session",
            )
        if event.sequence < session.last_sequence or (
            event.sequence == session.last_sequence and event.kind == session.last_kind
        ):
            return self._consume_duplicate(
                source_cursor, observed_at_unix_ms, "duplicate_session_event"
            )
        if event.sequence == session.last_sequence:
            return self._reject(
                source_cursor=source_cursor,
                observed_at_unix_ms=observed_at_unix_ms,
                protocol=REQUEST_EVENT_PROTOCOL,
                reason="session_sequence_conflict",
            )
        if session.terminal:
            return self._reject(
                source_cursor=source_cursor,
                observed_at_unix_ms=observed_at_unix_ms,
                protocol=REQUEST_EVENT_PROTOCOL,
                reason="event_after_terminal",
            )
        if observed_at_unix_ms - session.updated_at_unix_ms > self._max_session_idle_ms:
            self._sessions[event.request_id] = replace(
                session,
                state="quarantined",
                updated_at_unix_ms=observed_at_unix_ms,
                quarantine_reason="stale_session",
            )
            return self._reject(
                source_cursor=source_cursor,
                observed_at_unix_ms=observed_at_unix_ms,
                protocol=REQUEST_EVENT_PROTOCOL,
                reason="stale_session",
            )
        qualification = self._qualification
        if (
            qualification is None
            or observed_at_unix_ms - qualification.projection["issued_at_unix_ms"]
            > self._max_qualification_age_ms
        ):
            self._sessions[event.request_id] = replace(
                session,
                state="quarantined",
                updated_at_unix_ms=observed_at_unix_ms,
                quarantine_reason="stale_qualification",
            )
            return self._reject(
                source_cursor=source_cursor,
                observed_at_unix_ms=observed_at_unix_ms,
                protocol=REQUEST_EVENT_PROTOCOL,
                reason="stale_qualification",
            )
        if event.sequence != session.last_sequence + 1:
            return self._reject(
                source_cursor=source_cursor,
                observed_at_unix_ms=observed_at_unix_ms,
                protocol=REQUEST_EVENT_PROTOCOL,
                reason="session_sequence_gap",
            )
        if not self._session_matches_current_qualification(session):
            self._sessions[event.request_id] = replace(
                session,
                state="quarantined",
                updated_at_unix_ms=observed_at_unix_ms,
                quarantine_reason="stale_session_binding",
            )
            return self._reject(
                source_cursor=source_cursor,
                observed_at_unix_ms=observed_at_unix_ms,
                protocol=REQUEST_EVENT_PROTOCOL,
                reason="stale_session",
            )
        if event.kind == "accepted":
            return self._reject(
                source_cursor=source_cursor,
                observed_at_unix_ms=observed_at_unix_ms,
                protocol=REQUEST_EVENT_PROTOCOL,
                reason="duplicate_acceptance",
            )
        if event.kind == "token" and event.token_index != session.token_count:
            return self._reject(
                source_cursor=source_cursor,
                observed_at_unix_ms=observed_at_unix_ms,
                protocol=REQUEST_EVENT_PROTOCOL,
                reason="token_sequence_gap",
            )

        state = "streaming" if event.kind == "token" else event.kind
        updated = replace(
            session,
            state=state,
            last_kind=event.kind,
            last_sequence=event.sequence,
            event_count=session.event_count + 1,
            token_count=session.token_count + (1 if event.kind == "token" else 0),
            terminal=event.terminal,
            updated_at_unix_ms=observed_at_unix_ms,
        )
        self._sessions[event.request_id] = updated
        self._source_cursor = source_cursor
        self._observed_at_unix_ms = observed_at_unix_ms
        return ApplyOutcome(True, None, self._publish())

    def _start_session(
        self, source_cursor: int, observed_at_unix_ms: int, event: StreamEvent
    ) -> ApplyOutcome:
        qualification = self._qualification
        if qualification is None:
            return self._reject(
                source_cursor=source_cursor,
                observed_at_unix_ms=observed_at_unix_ms,
                protocol=REQUEST_EVENT_PROTOCOL,
                reason="qualification_unavailable",
            )
        issued_at = qualification.projection["issued_at_unix_ms"]
        if observed_at_unix_ms - issued_at > self._max_qualification_age_ms:
            return self._reject(
                source_cursor=source_cursor,
                observed_at_unix_ms=observed_at_unix_ms,
                protocol=REQUEST_EVENT_PROTOCOL,
                reason="stale_qualification",
            )
        self._evict_oldest_inactive_session()
        if len(self._sessions) >= self._max_sessions:
            return self._reject(
                source_cursor=source_cursor,
                observed_at_unix_ms=observed_at_unix_ms,
                protocol=REQUEST_EVENT_PROTOCOL,
                reason="session_capacity",
            )
        binding = qualification.binding
        self._sessions[event.request_id] = _SessionState(
            request_id=event.request_id,
            state="accepted",
            last_kind="accepted",
            last_sequence=0,
            event_count=1,
            token_count=0,
            terminal=False,
            qualification_id=qualification.projection["qualification_id"],
            deployment_id=binding["deployment_id"],
            deployment_epoch=binding["deployment_epoch"],
            path_manifest_digest=binding["path_manifest_digest"],
            started_at_unix_ms=observed_at_unix_ms,
            updated_at_unix_ms=observed_at_unix_ms,
        )
        self._source_cursor = source_cursor
        self._observed_at_unix_ms = observed_at_unix_ms
        return ApplyOutcome(True, None, self._publish())

    def _evict_oldest_inactive_session(self) -> None:
        if len(self._sessions) < self._max_sessions:
            return
        candidates = [
            session
            for session in self._sessions.values()
            if session.terminal or session.quarantine_reason is not None
        ]
        if not candidates:
            return
        oldest = min(
            candidates,
            key=lambda item: (item.updated_at_unix_ms, item.request_id),
        )
        self._sessions.pop(oldest.request_id, None)

    def _session_matches_current_qualification(self, session: _SessionState) -> bool:
        qualification = self._qualification
        if qualification is None:
            return False
        binding = qualification.binding
        return (
            session.qualification_id == qualification.projection["qualification_id"]
            and session.deployment_id == binding["deployment_id"]
            and session.deployment_epoch == binding["deployment_epoch"]
            and session.path_manifest_digest == binding["path_manifest_digest"]
        )

    def _restore(self) -> None:
        publication = self._publisher.current_publication()
        if publication is None:
            return
        try:
            envelope = publication.envelope()
            bundle = envelope["bundle"]
            if not _exact_keys(bundle, {"snapshot", "incidents", "provisioning"}):
                raise EventAdapterStateError("invalid adapter bundle")
            snapshot = bundle["snapshot"]
            status = bundle["provisioning"]
            if not _exact_keys(
                snapshot,
                {
                    "protocol",
                    "source_cursor",
                    "observed_at_unix_ms",
                    "qualification",
                    "sessions",
                },
            ) or snapshot["protocol"] != OBSERVATORY_EVENT_PROJECTION_PROTOCOL:
                raise EventAdapterStateError("invalid adapter snapshot")
            if not _exact_keys(
                status,
                {
                    "protocol",
                    "route_ready",
                    "source_cursor",
                    "buffered_sessions",
                    "quarantine_capacity",
                    "dropped_quarantine_count",
                },
            ) or status["protocol"] != OBSERVATORY_EVENT_STATUS_PROTOCOL:
                raise EventAdapterStateError("invalid adapter status")
            source_cursor = snapshot["source_cursor"]
            observed_at = snapshot["observed_at_unix_ms"]
            if (
                isinstance(source_cursor, bool)
                or not isinstance(source_cursor, int)
                or source_cursor < -1
                or source_cursor > MAX_SAFE_GENERATION
            ):
                raise EventAdapterStateError("invalid source cursor")
            _nonnegative_safe_integer("observed_at_unix_ms", observed_at)
            if (
                status["route_ready"] is not False
                or status["source_cursor"] != source_cursor
                or isinstance(status["buffered_sessions"], bool)
                or not isinstance(status["buffered_sessions"], int)
                or status["buffered_sessions"] < 0
                or status["buffered_sessions"] > 256
                or isinstance(status["quarantine_capacity"], bool)
                or not isinstance(status["quarantine_capacity"], int)
                or not 1 <= status["quarantine_capacity"] <= 256
                or isinstance(status["dropped_quarantine_count"], bool)
                or not isinstance(status["dropped_quarantine_count"], int)
                or status["dropped_quarantine_count"] < 0
            ):
                raise EventAdapterStateError("invalid adapter status")
            qualification = self._restore_qualification(snapshot["qualification"])
            sessions = self._restore_sessions(snapshot["sessions"], qualification)
            restored_incidents = self._restore_incidents(bundle["incidents"])
            if (
                status["buffered_sessions"] != len(sessions)
                or len(restored_incidents) > status["quarantine_capacity"]
                or any(
                    session.updated_at_unix_ms > observed_at
                    for session in sessions.values()
                )
                or (
                    qualification is not None
                    and qualification.projection["issued_at_unix_ms"] > observed_at
                )
            ):
                raise EventAdapterStateError("incoherent persisted adapter state")
        except (KeyError, TypeError, ValueError, EventAdapterStateError) as exc:
            if isinstance(exc, EventAdapterStateError):
                raise
            raise EventAdapterStateError("persisted adapter state is invalid") from exc

        self._source_cursor = source_cursor
        self._observed_at_unix_ms = observed_at
        self._qualification = qualification
        self._sessions = sessions
        if len(restored_incidents) > self._quarantine_capacity:
            restored_incidents = restored_incidents[-self._quarantine_capacity :]
        self._incidents = deque(restored_incidents, maxlen=self._quarantine_capacity)
        self._dropped_quarantine_count = status["dropped_quarantine_count"]

    @staticmethod
    def _restore_qualification(value: object) -> _QualificationState | None:
        if value is None:
            return None
        expected = {
            "protocol",
            "qualification_id",
            "issued_at_unix_ms",
            "evidence_class",
            "route_ready",
            "reason_codes",
            "binding",
        }
        if not _exact_keys(value, expected):
            raise EventAdapterStateError("invalid qualification projection")
        projection = value
        assert isinstance(projection, dict)
        binding_keys = {
            "qualification_id",
            "qualification_digest",
            "deployment_id",
            "deployment_epoch",
            "topology_version",
            "model_id",
            "resolved_commit",
            "manifest_digest",
            "path_manifest_digest",
            "stage_load_proof_digests",
        }
        binding = projection["binding"]
        if (
            projection["protocol"] != ROUTE_QUALIFICATION_PROTOCOL
            or projection["route_ready"] is not False
            or not _exact_keys(binding, binding_keys)
            or not all(
                _public_identifier(projection[name])
                for name in ("qualification_id",)
            )
            or not isinstance(projection["issued_at_unix_ms"], int)
            or isinstance(projection["issued_at_unix_ms"], bool)
            or projection["issued_at_unix_ms"] < 0
            or projection["evidence_class"]
            not in {"physical_qualification", "synthetic_test_fixture"}
            or not isinstance(projection["reason_codes"], list)
            or not 1 <= len(projection["reason_codes"]) <= 64
            or len(projection["reason_codes"])
            != len(set(projection["reason_codes"]))
            or not all(is_safe_error_code(code) for code in projection["reason_codes"])
        ):
            raise EventAdapterStateError("invalid qualification projection")
        assert isinstance(binding, dict)
        if (
            binding["qualification_id"] != projection["qualification_id"]
            or not all(
                _public_identifier(binding[name])
                for name in (
                    "qualification_id",
                    "deployment_id",
                    "model_id",
                    "resolved_commit",
                )
            )
            or not all(
                is_sha256_ref(binding[name])
                for name in (
                    "qualification_digest",
                    "manifest_digest",
                    "path_manifest_digest",
                )
            )
            or any(
                isinstance(binding[name], bool)
                or not isinstance(binding[name], int)
                or binding[name] < 0
                for name in ("deployment_epoch", "topology_version")
            )
            or not isinstance(binding["stage_load_proof_digests"], list)
            or len(binding["stage_load_proof_digests"]) > 256
            or binding["stage_load_proof_digests"]
            != sorted(set(binding["stage_load_proof_digests"]))
            or not all(
                is_sha256_ref(item) for item in binding["stage_load_proof_digests"]
            )
        ):
            raise EventAdapterStateError("invalid qualification binding")
        return _QualificationState(projection=deepcopy(projection))

    @staticmethod
    def _restore_sessions(
        value: object, qualification: _QualificationState | None
    ) -> dict[str, _SessionState]:
        if not isinstance(value, list):
            raise EventAdapterStateError("invalid session projection")
        sessions: dict[str, _SessionState] = {}
        expected = {
            "request_id",
            "state",
            "last_sequence",
            "event_count",
            "token_count",
            "terminal",
            "qualification_id",
            "started_at_unix_ms",
            "updated_at_unix_ms",
            "quarantine_reason",
        }
        for document in value:
            if not _exact_keys(document, expected) or qualification is None:
                raise EventAdapterStateError("invalid session projection")
            assert isinstance(document, dict)
            request_id = document["request_id"]
            state = document["state"]
            terminal_state = state in {"completed", "cancelled", "failed"}
            quarantined = state == "quarantined"
            if (
                len(value) > 256
                or not is_valid_request_id(request_id)
                or not _public_identifier(request_id)
                or request_id in sessions
                or not _public_identifier(document["qualification_id"])
                or (
                    not terminal_state
                    and not quarantined
                    and document["qualification_id"]
                    != qualification.projection["qualification_id"]
                )
                or not isinstance(document["last_sequence"], int)
                or isinstance(document["last_sequence"], bool)
                or document["last_sequence"] < 0
                or not isinstance(document["event_count"], int)
                or isinstance(document["event_count"], bool)
                or document["event_count"] != document["last_sequence"] + 1
                or not isinstance(document["token_count"], int)
                or isinstance(document["token_count"], bool)
                or not 0 <= document["token_count"] < document["event_count"]
                or type(document["terminal"]) is not bool
                or document["terminal"] != terminal_state
                or state
                not in {"accepted", "streaming", "completed", "cancelled", "failed", "quarantined"}
                or (
                    state == "accepted"
                    and (
                        document["event_count"] != 1
                        or document["token_count"] != 0
                    )
                )
                or (state == "streaming" and document["token_count"] < 1)
                or not isinstance(document["started_at_unix_ms"], int)
                or isinstance(document["started_at_unix_ms"], bool)
                or document["started_at_unix_ms"] < 0
                or not isinstance(document["updated_at_unix_ms"], int)
                or isinstance(document["updated_at_unix_ms"], bool)
                or document["updated_at_unix_ms"] < document["started_at_unix_ms"]
                or quarantined != (document["quarantine_reason"] is not None)
                or (
                    document["quarantine_reason"] is not None
                    and not is_safe_error_code(document["quarantine_reason"])
                )
            ):
                raise EventAdapterStateError("invalid session projection")
            binding = qualification.binding
            last_kind = "token" if state == "streaming" else state
            sessions[request_id] = _SessionState(
                request_id=request_id,
                state=state,
                last_kind=last_kind,
                last_sequence=document["last_sequence"],
                event_count=document["event_count"],
                token_count=document["token_count"],
                terminal=document["terminal"],
                qualification_id=document["qualification_id"],
                deployment_id=binding["deployment_id"],
                deployment_epoch=binding["deployment_epoch"],
                path_manifest_digest=binding["path_manifest_digest"],
                started_at_unix_ms=document["started_at_unix_ms"],
                updated_at_unix_ms=document["updated_at_unix_ms"],
                quarantine_reason=document["quarantine_reason"],
            )
        if list(sessions) != sorted(sessions):
            raise EventAdapterStateError("session projection is not deterministic")
        return sessions

    @staticmethod
    def _restore_incidents(value: object) -> list[dict[str, Any]]:
        if not isinstance(value, list) or len(value) > 256:
            raise EventAdapterStateError("invalid incident projection")
        incidents: list[dict[str, Any]] = []
        for document in value:
            if (
                not _exact_keys(document, {"protocol", "source_cursor", "reason"})
                or document["protocol"] not in {*_KNOWN_PROTOCOLS, "unknown"}
                or not isinstance(document["source_cursor"], int)
                or isinstance(document["source_cursor"], bool)
                or document["source_cursor"] < 0
                or not is_safe_error_code(document["reason"])
            ):
                raise EventAdapterStateError("invalid incident projection")
            incidents.append(dict(document))
        return incidents
