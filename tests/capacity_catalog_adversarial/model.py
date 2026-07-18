"""Small independent state machine for CapacityProfileCatalog behavior."""
from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum


AUTHORITY_FLAGS = (False, False, False)


class AdversarialValue(str, Enum):
    TRUE = "boolean-true"
    NAN = "nan"
    POSITIVE_INFINITY = "positive-infinity"
    NEGATIVE_INFINITY = "negative-infinity"
    OVERSIZED_INTEGER = "oversized-integer"


def materialize_value(value: object) -> object:
    if value is AdversarialValue.TRUE:
        return True
    if value is AdversarialValue.NAN:
        return math.nan
    if value is AdversarialValue.POSITIVE_INFINITY:
        return math.inf
    if value is AdversarialValue.NEGATIVE_INFINITY:
        return -math.inf
    if value is AdversarialValue.OVERSIZED_INTEGER:
        return 10**10_000
    return value


@dataclass(frozen=True, slots=True)
class SlotIdentity:
    model_digest: str
    quantization: str
    backend: str
    runtime_build: str
    hardware_class: str
    power_mode: str
    context_bucket: str
    kv_mode: str


@dataclass(frozen=True, slots=True)
class ProfileFixture:
    profile_id: str
    profile_digest: str
    source_evidence_digest: str
    slot: SlotIdentity
    canonical_profile_bytes: bytes


@dataclass(frozen=True, slots=True)
class Operation:
    kind: str
    profile_id: str
    now: object
    ttl: object | None = None
    allow_replacement: object = False
    expected_current_profile_id: str | None = None
    lookup_profile_id: str | None = None

    @classmethod
    def insert(
        cls,
        profile_id: str,
        *,
        now: object,
        ttl: object,
        allow_replacement: object = False,
        expected_current_profile_id: str | None = None,
    ) -> Operation:
        return cls(
            kind="insert",
            profile_id=profile_id,
            now=now,
            ttl=ttl,
            allow_replacement=allow_replacement,
            expected_current_profile_id=expected_current_profile_id,
        )

    @classmethod
    def resolve(
        cls,
        profile_id: str,
        *,
        now: object,
        lookup_profile_id: str | None = None,
    ) -> Operation:
        return cls(
            kind="resolve",
            profile_id=profile_id,
            now=now,
            lookup_profile_id=lookup_profile_id,
        )


@dataclass(frozen=True, slots=True)
class Observation:
    operation: str
    code: str
    entry_count: int
    state: str | None = None
    slot: SlotIdentity | None = None
    profile_digest: str | None = None
    source_evidence_digest: str | None = None
    canonical_profile_bytes: bytes | None = None
    inserted_at: float | None = None
    expires_at: float | None = None
    deprecated_at: float | None = None
    replaced_by_profile_digest: str | None = None
    authority_flags: tuple[bool, bool, bool] = AUTHORITY_FLAGS


@dataclass(frozen=True, slots=True)
class _Entry:
    fixture: ProfileFixture
    inserted_at: float
    expires_at: float
    deprecated_at: float | None = None
    replaced_by_profile_digest: str | None = None


class _ModelError(ValueError):
    pass


class CatalogReferenceModel:
    """Independent bounded map model; deliberately imports no production package."""

    def __init__(self, *, max_entries: int, max_ttl: float) -> None:
        self.max_entries = max_entries
        self.max_ttl = max_ttl
        self._entries: dict[str, _Entry] = {}
        self._current_by_slot: dict[SlotIdentity, str] = {}
        self._last_now: float | None = None

    @property
    def entry_count(self) -> int:
        return len(self._entries)

    @staticmethod
    def _finite_number(value: object, error_code: str) -> float:
        value = materialize_value(value)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _ModelError(error_code)
        try:
            normalized = float(value)
        except OverflowError as exc:
            raise _ModelError(error_code) from exc
        if not math.isfinite(normalized):
            raise _ModelError(error_code)
        return normalized

    def _observe_now(self, value: object) -> float:
        normalized = self._finite_number(value, "invalid_time")
        if normalized < 0:
            raise _ModelError("invalid_time")
        if self._last_now is not None and normalized < self._last_now:
            raise _ModelError("backward_time")
        # The catalog contract treats any valid caller time as a fail-closed
        # watermark observation, even when a later operation check rejects.
        self._last_now = normalized
        return normalized

    def _ttl(self, value: object) -> float:
        normalized = self._finite_number(value, "invalid_ttl")
        if normalized <= 0 or normalized > self.max_ttl:
            raise _ModelError("invalid_ttl")
        return normalized

    @staticmethod
    def _state(entry: _Entry, now: float) -> str:
        if entry.deprecated_at is not None:
            return "deprecated"
        if now >= entry.expires_at:
            return "stale"
        return "current"

    def _entry_observation(
        self,
        *,
        operation: str,
        code: str,
        state: str,
        entry: _Entry,
    ) -> Observation:
        fixture = entry.fixture
        expose_lineage = operation == "resolve"
        return Observation(
            operation=operation,
            code=code,
            entry_count=self.entry_count,
            state=state,
            slot=fixture.slot,
            profile_digest=fixture.profile_digest,
            source_evidence_digest=fixture.source_evidence_digest,
            canonical_profile_bytes=(
                fixture.canonical_profile_bytes if expose_lineage else None
            ),
            inserted_at=entry.inserted_at,
            expires_at=entry.expires_at,
            deprecated_at=entry.deprecated_at if expose_lineage else None,
            replaced_by_profile_digest=(
                entry.replaced_by_profile_digest if expose_lineage else None
            ),
        )

    def _insert(
        self,
        operation: Operation,
        fixtures: Mapping[str, ProfileFixture],
    ) -> Observation:
        fixture = fixtures[operation.profile_id]
        now = self._observe_now(operation.now)
        ttl = self._ttl(operation.ttl)
        expires_at = now + ttl
        if not math.isfinite(expires_at):
            raise _ModelError("nonfinite_expiry")

        existing = self._entries.get(fixture.profile_digest)
        if existing is not None:
            if (
                existing.fixture.canonical_profile_bytes
                != fixture.canonical_profile_bytes
                or existing.fixture.slot != fixture.slot
            ):
                raise _ModelError("digest_collision")
            return self._entry_observation(
                operation="insert",
                code="replayed",
                state=self._state(existing, now),
                entry=existing,
            )

        current_digest = self._current_by_slot.get(fixture.slot)
        if current_digest is None:
            if self.entry_count >= self.max_entries:
                raise _ModelError("capacity_exhausted")
            entry = _Entry(fixture=fixture, inserted_at=now, expires_at=expires_at)
            self._entries[fixture.profile_digest] = entry
            self._current_by_slot[fixture.slot] = fixture.profile_digest
            return self._entry_observation(
                operation="insert",
                code="added",
                state="current",
                entry=entry,
            )

        current = self._entries[current_digest]
        if operation.allow_replacement is not True:
            raise _ModelError("replacement_not_authorized")
        expected_digest = (
            None
            if operation.expected_current_profile_id is None
            else fixtures[operation.expected_current_profile_id].profile_digest
        )
        if expected_digest != current_digest:
            raise _ModelError("cas_failed")
        if current.fixture.source_evidence_digest == fixture.source_evidence_digest:
            raise _ModelError("source_evidence_reused")
        if self.entry_count >= self.max_entries:
            raise _ModelError("capacity_exhausted")

        replacement = _Entry(
            fixture=fixture,
            inserted_at=now,
            expires_at=expires_at,
        )
        self._entries[current_digest] = replace(
            current,
            deprecated_at=now,
            replaced_by_profile_digest=fixture.profile_digest,
        )
        self._entries[fixture.profile_digest] = replacement
        self._current_by_slot[fixture.slot] = fixture.profile_digest
        return self._entry_observation(
            operation="insert",
            code="replaced",
            state="current",
            entry=replacement,
        )

    def _resolve(
        self,
        operation: Operation,
        fixtures: Mapping[str, ProfileFixture],
    ) -> Observation:
        slot = fixtures[operation.profile_id].slot
        now = self._observe_now(operation.now)
        if operation.lookup_profile_id is None:
            selected_digest = self._current_by_slot.get(slot)
        else:
            selected_digest = fixtures[operation.lookup_profile_id].profile_digest
        entry = None if selected_digest is None else self._entries.get(selected_digest)
        if entry is None or entry.fixture.slot != slot:
            return Observation(
                operation="resolve",
                code="missing",
                entry_count=self.entry_count,
                state="missing",
                slot=slot,
            )
        return self._entry_observation(
            operation="resolve",
            code=self._state(entry, now),
            state=self._state(entry, now),
            entry=entry,
        )

    def apply(
        self,
        operation: Operation,
        fixtures: Mapping[str, ProfileFixture],
    ) -> Observation:
        try:
            if operation.kind == "insert":
                return self._insert(operation, fixtures)
            if operation.kind == "resolve":
                return self._resolve(operation, fixtures)
            raise _ModelError("invalid_operation")
        except _ModelError as exc:
            return Observation(
                operation=operation.kind,
                code=str(exc),
                entry_count=self.entry_count,
            )


def run_reference_trace(
    model: CatalogReferenceModel,
    trace: Sequence[Operation],
    fixtures: Mapping[str, ProfileFixture],
) -> tuple[Observation, ...]:
    return tuple(model.apply(operation, fixtures) for operation in trace)


def minimize_trace(
    trace: Sequence[Operation],
    failure: Callable[[tuple[Operation, ...]], bool],
) -> tuple[Operation, ...]:
    candidate = tuple(trace)
    if not failure(candidate):
        raise ValueError("trace_does_not_fail")
    changed = True
    while changed:
        changed = False
        for index in range(len(candidate)):
            reduced = candidate[:index] + candidate[index + 1 :]
            if failure(reduced):
                candidate = reduced
                changed = True
                break
    return candidate
