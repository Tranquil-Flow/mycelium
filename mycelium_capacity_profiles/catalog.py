from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace
from enum import Enum

from .compiler import CapacityProfile
from .document import parse_capacity_profile_bytes


_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class CapacityProfileCatalogState(str, Enum):
    MISSING = "missing"
    CURRENT = "current"
    STALE = "stale"
    DEPRECATED = "deprecated"


class CatalogInsertAction(str, Enum):
    ADDED = "added"
    REPLAYED = "replayed"
    REPLACED = "replaced"


def _finite_number(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        normalized = float(value)
    except OverflowError as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(normalized):
        raise ValueError(f"{name} must be a finite number")
    return normalized


def _require_digest(name: str, value: object) -> str:
    if type(value) is not str or not _DIGEST_RE.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase sha256 digest")
    return value


def _require_slot_string(name: str, value: object) -> str:
    if type(value) is not str or not value or len(value) > 256:
        raise ValueError(f"{name} must be a non-empty bounded string")
    return value


@dataclass(frozen=True)
class CapacityProfileCatalogPolicy:
    max_entries: int
    max_ttl: float

    def __post_init__(self) -> None:
        if type(self.max_entries) is not int or self.max_entries <= 0:
            raise ValueError("max_entries must be a positive exact integer")
        max_ttl = _finite_number("max_ttl", self.max_ttl)
        if max_ttl <= 0:
            raise ValueError("max_ttl must be positive")


@dataclass(frozen=True)
class CapacityProfileSlot:
    model_digest: str
    quantization: str
    backend: str
    runtime_build: str
    hardware_class: str
    power_mode: str
    context_bucket: str
    kv_mode: str

    def __post_init__(self) -> None:
        _require_digest("model_digest", self.model_digest)
        for name in (
            "quantization",
            "backend",
            "runtime_build",
            "hardware_class",
            "power_mode",
            "context_bucket",
            "kv_mode",
        ):
            _require_slot_string(name, getattr(self, name))

    @classmethod
    def from_profile(cls, profile: CapacityProfile) -> CapacityProfileSlot:
        key = profile.key
        return cls(
            model_digest=key.model_digest,
            quantization=key.quantization,
            backend=key.backend,
            runtime_build=key.runtime_build,
            hardware_class=key.hardware_class,
            power_mode=key.power_mode,
            context_bucket=key.context_bucket,
            kv_mode=key.kv_mode,
        )


@dataclass(frozen=True)
class CatalogLookup:
    state: CapacityProfileCatalogState
    slot: CapacityProfileSlot
    profile_digest: str | None = None
    source_evidence_digest: str | None = None
    profile: CapacityProfile | None = None
    canonical_profile_bytes: bytes | None = None
    inserted_at: float | None = None
    expires_at: float | None = None
    deprecated_at: float | None = None
    replaced_by_profile_digest: str | None = None
    route_ready: bool = field(default=False, init=False)
    release_ready: bool = field(default=False, init=False)
    qualification_evaluated: bool = field(default=False, init=False)


@dataclass(frozen=True)
class CatalogInsertResult:
    action: CatalogInsertAction
    state: CapacityProfileCatalogState
    slot: CapacityProfileSlot
    profile_digest: str
    source_evidence_digest: str
    inserted_at: float
    expires_at: float
    route_ready: bool = field(default=False, init=False)
    release_ready: bool = field(default=False, init=False)
    qualification_evaluated: bool = field(default=False, init=False)


@dataclass(frozen=True)
class _StoredEntry:
    slot: CapacityProfileSlot
    profile: CapacityProfile
    canonical_profile_bytes: bytes
    inserted_at: float
    expires_at: float
    deprecated_at: float | None = None
    replaced_by_profile_digest: str | None = None

    @property
    def profile_digest(self) -> str:
        return self.profile.profile_digest

    @property
    def source_evidence_digest(self) -> str:
        return self.profile.key.source_evidence_digest


class CapacityProfileCatalog:
    """Bounded process-local storage with no activation authority."""

    def __init__(self, policy: CapacityProfileCatalogPolicy) -> None:
        if type(policy) is not CapacityProfileCatalogPolicy:
            raise ValueError("catalog policy must be an immutable capacity profile policy")
        self._policy = policy
        self._entries_by_digest: dict[str, _StoredEntry] = {}
        self._current_by_slot: dict[CapacityProfileSlot, str] = {}
        self._last_now: float | None = None

    @property
    def policy(self) -> CapacityProfileCatalogPolicy:
        return self._policy

    @property
    def entry_count(self) -> int:
        return len(self._entries_by_digest)

    def _observe_now(self, now: object) -> float:
        normalized = _finite_number("monotonic time", now)
        if normalized < 0:
            raise ValueError("monotonic time must be non-negative")
        if self._last_now is not None and normalized < self._last_now:
            raise ValueError("caller monotonic time must not move backward")
        self._last_now = normalized
        return normalized

    def _validate_ttl(self, ttl: object) -> float:
        normalized = _finite_number("capacity profile TTL", ttl)
        if normalized <= 0 or normalized > self._policy.max_ttl:
            raise ValueError("capacity profile TTL must be positive and within policy")
        return normalized

    def _require_capacity(self) -> None:
        if self.entry_count >= self._policy.max_entries:
            raise ValueError("capacity profile catalog capacity exhausted")

    @staticmethod
    def _entry_state(
        entry: _StoredEntry,
        now: float,
    ) -> CapacityProfileCatalogState:
        if entry.deprecated_at is not None:
            return CapacityProfileCatalogState.DEPRECATED
        if now >= entry.expires_at:
            return CapacityProfileCatalogState.STALE
        return CapacityProfileCatalogState.CURRENT

    @classmethod
    def _lookup_from_entry(cls, entry: _StoredEntry, now: float) -> CatalogLookup:
        return CatalogLookup(
            state=cls._entry_state(entry, now),
            slot=entry.slot,
            profile_digest=entry.profile_digest,
            source_evidence_digest=entry.source_evidence_digest,
            profile=entry.profile,
            canonical_profile_bytes=entry.canonical_profile_bytes,
            inserted_at=entry.inserted_at,
            expires_at=entry.expires_at,
            deprecated_at=entry.deprecated_at,
            replaced_by_profile_digest=entry.replaced_by_profile_digest,
        )

    @classmethod
    def _insert_result(
        cls,
        action: CatalogInsertAction,
        entry: _StoredEntry,
        now: float,
    ) -> CatalogInsertResult:
        return CatalogInsertResult(
            action=action,
            state=cls._entry_state(entry, now),
            slot=entry.slot,
            profile_digest=entry.profile_digest,
            source_evidence_digest=entry.source_evidence_digest,
            inserted_at=entry.inserted_at,
            expires_at=entry.expires_at,
        )

    def insert(
        self,
        payload: bytes,
        *,
        now: float,
        ttl: float,
        allow_replacement: object = False,
        expected_current_digest: object | None = None,
    ) -> CatalogInsertResult:
        profile = parse_capacity_profile_bytes(payload)
        normalized_now = self._observe_now(now)
        normalized_ttl = self._validate_ttl(ttl)
        expires_at = normalized_now + normalized_ttl
        if not math.isfinite(expires_at):
            raise ValueError("capacity profile expiry must be finite")

        slot = CapacityProfileSlot.from_profile(profile)
        profile_digest = profile.profile_digest
        existing = self._entries_by_digest.get(profile_digest)
        if existing is not None:
            if existing.canonical_profile_bytes != payload or existing.slot != slot:
                raise ValueError("capacity profile digest collision")
            return self._insert_result(
                CatalogInsertAction.REPLAYED,
                existing,
                normalized_now,
            )

        current_digest = self._current_by_slot.get(slot)
        if current_digest is None:
            self._require_capacity()
            entry = _StoredEntry(
                slot=slot,
                profile=profile,
                canonical_profile_bytes=payload,
                inserted_at=normalized_now,
                expires_at=expires_at,
            )
            self._entries_by_digest[profile_digest] = entry
            self._current_by_slot[slot] = profile_digest
            return self._insert_result(
                CatalogInsertAction.ADDED,
                entry,
                normalized_now,
            )

        current = self._entries_by_digest[current_digest]
        if allow_replacement is not True:
            raise ValueError("capacity profile replacement requires explicit replacement authorization")
        if type(expected_current_digest) is not str or expected_current_digest != current_digest:
            raise ValueError("capacity profile replacement compare-and-swap failed")
        if current.source_evidence_digest == profile.key.source_evidence_digest:
            raise ValueError("replacement requires a different verified source-evidence digest")
        self._require_capacity()

        replacement = _StoredEntry(
            slot=slot,
            profile=profile,
            canonical_profile_bytes=payload,
            inserted_at=normalized_now,
            expires_at=expires_at,
        )
        deprecated = replace(
            current,
            deprecated_at=normalized_now,
            replaced_by_profile_digest=profile_digest,
        )
        self._entries_by_digest[current_digest] = deprecated
        self._entries_by_digest[profile_digest] = replacement
        self._current_by_slot[slot] = profile_digest
        return self._insert_result(
            CatalogInsertAction.REPLACED,
            replacement,
            normalized_now,
        )

    def resolve(
        self,
        slot: CapacityProfileSlot,
        *,
        now: float,
        profile_digest: str | None = None,
    ) -> CatalogLookup:
        if type(slot) is not CapacityProfileSlot:
            raise ValueError("capacity profile lookup requires an operational slot")
        normalized_now = self._observe_now(now)

        if profile_digest is None:
            selected_digest = self._current_by_slot.get(slot)
        else:
            selected_digest = _require_digest("profile_digest", profile_digest)

        if selected_digest is None:
            return CatalogLookup(
                state=CapacityProfileCatalogState.MISSING,
                slot=slot,
            )
        entry = self._entries_by_digest.get(selected_digest)
        if entry is None or entry.slot != slot:
            return CatalogLookup(
                state=CapacityProfileCatalogState.MISSING,
                slot=slot,
            )
        return self._lookup_from_entry(entry, normalized_now)


__all__ = [
    "CapacityProfileCatalog",
    "CapacityProfileCatalogPolicy",
    "CapacityProfileCatalogState",
    "CapacityProfileSlot",
    "CatalogInsertAction",
    "CatalogInsertResult",
    "CatalogLookup",
]
