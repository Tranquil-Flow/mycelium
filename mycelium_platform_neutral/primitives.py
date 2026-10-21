# SPDX-License-Identifier: AGPL-3.0-only
"""Fail-closed primitives shared by the A9 platform-neutral capability contracts.

Every A9 contract is authored (spec §4) as canonical, size/count bounded, and
rejecting unknown fields, invalid nullability, duplicate identifiers, non-finite
numbers, stale generations, unsupported protocol versions, and mismatched
digests.  These helpers are the single place that implements those rules so the
nine contract modules stay declarative.

Nothing here claims qualification, eligibility, placement, or readiness: this
package is deterministic authoring code for the A9 frozen contracts.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
import re
from types import MappingProxyType
from typing import Any

from mycelium_qualification.evidence import (
    EvidenceValidationError,
    canonical_json_bytes,
    canonical_json_loads,
    sha256_bytes,
)


SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

#: One canonical contract record may not exceed this many bytes.
MAX_RECORD_BYTES = 64 * 1024
#: Bounded list/collection sizes for every contract.
MAX_LIST_ITEMS = 64
MAX_STRING_CHARS = 256
MAX_COUNT = 1 << 40
MAX_SECONDS = 1.0e9

#: Registered runtime backend identifiers and their declared implementation
#: stage (A9 frozen decision ``runtime_backend_registry``).  The registry is
#: closed: an unregistered identifier is rejected, never defaulted.  A future
#: backend enters the product by gaining a registry entry, not by appearing in
#: a declaration.  ``unknown`` is the registered sentinel for an undeclared
#: backend; it stays visible and ineligible.
RUNTIME_BACKEND_STAGES = MappingProxyType(
    {
        "mlx": "implemented",
        "numpy": "implemented",
        "llama_http": "implemented",
        "openrouter_http": "implemented",
        # The A9 native accelerated Windows x86_64 provider (source tranche in
        # ``mycelium_platform_neutral/windows_provider.py``).  Registration is a
        # declared runtime identity, never qualification: the physical Windows
        # device blocker ``windows_x86_64_provider_not_physically_qualified``
        # stays open, and an adapter or registry entry is not execution evidence.
        "windows_native_provider": "implemented",
        "unknown": "unknown",
    }
)
#: Backends that are portable correctness/reference identities only.  They can
#: never mint accelerated eligibility (spec §3.2, §10 negative battery).
CORRECTNESS_ONLY_BACKENDS = frozenset({"numpy", "unknown"})

#: The nine frozen A9 contracts (spec §4), by protocol.
PEER_PROFILE_PROTOCOL = "mycelium.membership.peer_profile.v2"
CAPABILITY_REPORT_PROTOCOL = "mycelium.membership.capability_report.v2"
CLASS_QUALIFICATION_PROTOCOL = "mycelium.peer_class_qualification.v1"
ELIGIBILITY_PROTOCOL = "mycelium.peer_eligibility.v1"
MIGRATION_PROTOCOL = "mycelium.membership_contract_migration.v1"
ACCELERATION_PROFILE_PROTOCOL = "mycelium.runtime_acceleration_profile.v1"
STAGE_OBSERVATION_PROTOCOL = "mycelium.stage_performance_observation.v1"
CODEC_QUALIFICATION_PROTOCOL = "mycelium.activation_codec_qualification.v1"
EFFICIENCY_QUALIFICATION_PROTOCOL = "mycelium.runtime_efficiency_qualification.v1"

FROZEN_A9_PROTOCOLS = (
    PEER_PROFILE_PROTOCOL,
    CAPABILITY_REPORT_PROTOCOL,
    CLASS_QUALIFICATION_PROTOCOL,
    ELIGIBILITY_PROTOCOL,
    MIGRATION_PROTOCOL,
    ACCELERATION_PROFILE_PROTOCOL,
    STAGE_OBSERVATION_PROTOCOL,
    CODEC_QUALIFICATION_PROTOCOL,
    EFFICIENCY_QUALIFICATION_PROTOCOL,
)

IDENTITY_KINDS = frozenset(
    {"native_peer", "browser_peer", "artifact_source", "probe_only"}
)
PLATFORM_FAMILIES = frozenset(
    {"macos", "linux", "windows", "android", "ios", "ipados", "browser", "unknown"}
)
ARCHITECTURES = frozenset({"arm64", "x86_64", "wasm32", "unknown"})
TRANSPORT_FAMILIES = frozenset({"iroh", "https", "browser_https", "unknown"})
LIFECYCLE_AVAILABILITY = frozenset(
    {"foreground_only", "foreground_and_background", "service_managed", "unknown"}
)
EVIDENCE_SOURCE_KINDS = frozenset(
    {"native_observed", "synthetic_conformance", "operator_declared", "unknown"}
)
ELIGIBILITY_STATES = frozenset({"eligible", "ineligible", "unknown"})
#: The six-rung fail-closed ladder (spec §5).  ``signed_member`` is the base
#: membership rung and carries no qualification contract.
ELIGIBILITY_ROLES = (
    "signed_member",
    "probe_contributor",
    "artifact_source",
    "speculative_draft_worker",
    "qualified_model_stage",
    "performance_ready_model_stage",
)
QUALIFIABLE_ROLES = ELIGIBILITY_ROLES[1:]


class PlatformNeutralContractError(ValueError):
    """Fail-closed A9 contract error carrying one stable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def require(condition: bool, code: str) -> None:
    if not condition:
        raise PlatformNeutralContractError(code)


def closed_fields(
    value: Any, fields: frozenset[str], code: str
) -> Mapping[str, Any]:
    """Return ``value`` only when it carries exactly ``fields`` (no more, no less)."""

    require(isinstance(value, Mapping), code)
    try:
        present = set(value)
    except TypeError:
        raise PlatformNeutralContractError(code) from None
    require(present == set(fields), code)
    return value


def segment(value: Any, code: str) -> str:
    require(isinstance(value, str) and SEGMENT_RE.fullmatch(value) is not None, code)
    return value


def digest(value: Any, code: str) -> str:
    require(isinstance(value, str) and DIGEST_RE.fullmatch(value) is not None, code)
    return value


def text(value: Any, code: str, *, maximum: int = MAX_STRING_CHARS) -> str:
    require(
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and len(value) <= maximum,
        code,
    )
    return value


def nullable_text(value: Any, code: str, *, maximum: int = MAX_STRING_CHARS) -> str | None:
    if value is None:
        return None
    return text(value, code, maximum=maximum)


def enum_value(value: Any, allowed: Iterable[str], code: str) -> str:
    try:
        permitted = value in allowed
    except TypeError:
        raise PlatformNeutralContractError(code) from None
    require(permitted, code)
    return value


def nullable_enum(value: Any, allowed: Iterable[str], code: str) -> str | None:
    if value is None:
        return None
    return enum_value(value, allowed, code)


def bounded_int(value: Any, code: str, *, minimum: int, maximum: int = MAX_COUNT) -> int:
    require(
        isinstance(value, int)
        and not isinstance(value, bool)
        and minimum <= value <= maximum,
        code,
    )
    return value


def nullable_bounded_int(
    value: Any, code: str, *, minimum: int, maximum: int = MAX_COUNT
) -> int | None:
    if value is None:
        return None
    return bounded_int(value, code, minimum=minimum, maximum=maximum)


def finite(
    value: Any,
    code: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    require(
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and _is_finite(value),
        code,
    )
    number = float(value)
    if minimum is not None:
        require(number >= minimum, code)
    if maximum is not None:
        require(number <= maximum, code)
    return number


def nullable_finite(
    value: Any,
    code: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float | None:
    if value is None:
        return None
    return finite(value, code, minimum=minimum, maximum=maximum)


def nullable_bool(value: Any, code: str) -> bool | None:
    if value is None:
        return None
    require(isinstance(value, bool), code)
    return value


def bounded_list(value: Any, code: str, *, maximum: int = MAX_LIST_ITEMS) -> list[Any]:
    require(isinstance(value, (list, tuple)) and 0 <= len(value) <= maximum, code)
    return list(value)


def unique_names(
    value: Any, code: str, *, maximum: int = MAX_LIST_ITEMS, nullable: bool = False
) -> tuple[str, ...]:
    """A bounded, order-preserving, duplicate-free sequence of segments/nullables."""

    items = bounded_list(value, code, maximum=maximum)
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        item = nullable_text(item, code) if nullable else text(item, code)
        if item is None:
            continue
        require(item not in seen, code)
        seen.add(item)
        result.append(item)
    return tuple(result)


def canonical_bytes(value: Any, code: str) -> bytes:
    """Canonical JSON bytes for one record, bounded and finite-value only."""

    try:
        content = canonical_json_bytes(value)
    except EvidenceValidationError as exc:
        raise PlatformNeutralContractError(code) from exc
    require(len(content) <= MAX_RECORD_BYTES, code)
    return content


def record_digest(value: Any, code: str) -> str:
    return sha256_bytes(canonical_bytes(value, code))


def detached(record: Mapping[str, Any], code: str) -> dict[str, Any]:
    """Canonical detached copy; rejects duplicate JSON keys and non-canonical input."""

    content = canonical_bytes(record, code)
    try:
        value = canonical_json_loads(content, path=code)
    except EvidenceValidationError as exc:
        raise PlatformNeutralContractError(code) from exc
    require(isinstance(value, dict), code)
    return value


def _is_finite(value: Any) -> bool:
    try:
        return float(value) == float(value) and abs(float(value)) != float("inf")
    except (TypeError, ValueError, OverflowError):
        return False


def require_distinct(values: Sequence[str], code: str) -> None:
    require(len(set(values)) == len(values), code)
