# SPDX-License-Identifier: AGPL-3.0-only
"""Native accelerated Windows x86_64 provider adapter — A9 source tranche.

Spec §3.2/§3.4, frozen decisions ``accelerated_windows_backend`` and
``persistent_int8_execution``.

What this module is: the platform-neutral *interface* half of the native
accelerated Windows x86_64 provider, with the Windows-native specifics behind it:

* :func:`detect_windows_dispatch` — the Windows CPU/dispatch observation.  It
  refuses every host that is not an observed ``Windows`` + x86_64
  (``AMD64``/``x86_64``) pair, so acceleration is never inferred from a platform
  string, a device brand, or this source file existing.
* :func:`bind_provider_library` — the win32 library-binding seam.  The binding is
  digest-pinned to the exact declared provider build, refuses symlinked or absent
  binaries, and refuses to pretend a provider is bound when the running host has
  no win32 loader (which is exactly what happens on the macOS authoring host).
* :class:`WindowsNativeProviderAdapter` — the typed adapter that executes int8
  weights through the *landed* int8 kernel seam
  (:mod:`mycelium_platform_neutral.int8_execution`), so provider state,
  workspace accounting and the materialization ledger have exactly one
  implementation, and emits the frozen
  ``mycelium.runtime_acceleration_profile.v1`` shape for the exact provider build.

What this module is NOT: an acceleration claim.  ``BACKEND_ID`` is registered in
:data:`mycelium_platform_neutral.primitives.RUNTIME_BACKEND_STAGES` as a declared
runtime identity at ``implemented`` — registration is an identity, never
qualification — and the physical acceptance case
``windows_accelerated_physical_positive`` stays unearned:
:meth:`WindowsNativeProviderAdapter.assert_physical_qualification` raises the A9
device blocker ``windows_x86_64_provider_not_physically_qualified`` instead of
returning a qualification.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
import math
from pathlib import Path
import platform
import sys
from typing import Any

import numpy as np

from .acceleration_profile import REQUIRED_OPERATIONS, AccelerationProfile
from .activation_transport import SUPPORTED_DTYPES
from .int8_execution import (
    DEFAULT_WORKSPACE_BYTES,
    Int8KernelUnavailable,
    Int8LinearKernel,
    MaterializationLedger,
)
from .primitives import ACCELERATION_PROFILE_PROTOCOL, DIGEST_RE, RUNTIME_BACKEND_STAGES

PROTOCOL = "mycelium.a9_windows_native_provider_adapter.v1"

CLAIM_BOUNDARY = (
    "source-only Windows x86_64 provider adapter: no provider build was installed, "
    "loaded, dispatched or measured on Windows hardware, and adapter presence is "
    "never acceleration or qualification evidence"
)

#: The stable, vendor-neutral runtime identity of the Windows provider.
BACKEND_ID = "windows_native_provider"
PLATFORM_FAMILY = "windows"
DEVICE_CLASS = "windows_x86_64"
#: Provider implementation families the A9 specification allows for the Windows
#: gate.  The family is frozen only after physical evidence passes; naming one
#: here is a declaration, not a decision.
SUPPORTED_PROVIDER_FAMILIES: tuple[str, ...] = (
    "directml",
    "onnxruntime",
    "onednn",
    "native_x86_64_dispatch",
)
WINDOWS_SYSTEM_NAMES: frozenset[str] = frozenset({"Windows"})
X86_64_MACHINE_NAMES: frozenset[str] = frozenset({"AMD64", "amd64", "x86_64"})
#: Observed but unadmitted Windows architectures: A9 freezes x86_64 only.
OTHER_WINDOWS_MACHINE_NAMES: frozenset[str] = frozenset(
    {"ARM64", "arm64", "arm64ec", "i686", "x86", "unknown"}
)
PERSISTENT_STATE_BOUNDARIES: frozenset[str] = frozenset({"load", "warmup"})

DECLARED_TARGET: dict[str, str] = {
    "device_label": "mycelium-win-1",
    "claim_level": "managed_native_host",
    "physical_execution": "not_executed",
}

#: Closed set of codes this adapter can raise.  The int8 seam's own codes are
#: included because the adapter re-raises them through its single exception type.
ADAPTER_CODES: frozenset[str] = frozenset(
    {
        "unsupported_platform",
        "unsupported_architecture",
        "platform_observation_invalid",
        "windows_provider_declaration_invalid",
        "provider_family_unregistered",
        "provider_build_digest_invalid",
        "provider_activation_precision_unsupported",
        "provider_materialization_policy_rejected",
        "provider_per_decode_cast_rejected",
        "provider_stage_local_kv_required",
        "provider_lifecycle_support_required",
        "provider_workspace_budget_invalid",
        "provider_validity_invalid",
        "provider_not_bound",
        "provider_library_absent",
        "provider_library_digest_mismatch",
        "provider_library_binding_unavailable_on_host",
        "provider_library_load_failed",
        "windows_x86_64_provider_not_physically_qualified",
        # Re-raised int8 seam refusals.
        "weight_representation_unsupported",
        "weight_scale_invalid",
        "workspace_budget_invalid",
        "activation_width_mismatch",
        "activation_not_finite",
        "warm_decode_not_proven",
    }
)

#: Acceptance-inventory coverage.  Every entry is a negative statement: this
#: tranche implements source and boundaries, not physical execution.
ACCEPTANCE_COVERAGE = {
    "windows_accelerated_physical_positive": (
        "not_earned: physical_positive acceptance case requires the exact provider "
        "build, device and driver identity, zero warm decode-time full-weight "
        "materialization, component timings and the frozen single-request floor "
        "observed on a native Windows x86_64 member; this tranche proves the "
        "adapter seam, dispatch refusal and materialization invariants on a "
        "portable host only"
    ),
    "numpy_reference_not_performance_eligible": (
        "not_earned: physical/deterministic negative requires the performance-ready "
        "decision to be refused for a portable runtime on the ordinary product path; "
        "the adapter here never mints that decision at all"
    ),
    "int8_decode_materialization_rejection": (
        "not_earned: physical_negative acceptance case requires the materialization "
        "and bounded-workspace challenge window on a physically qualified native "
        "Windows x86_64 provider; the ledger detector is shared with the landed int8 "
        "seam and is not a device observation"
    ),
}

DECLARATION_FIELDS: frozenset[str] = frozenset(
    {
        "provider_family",
        "build_digest",
        "driver_runtime_identity",
        "driver_runtime_version_digest",
        "activation_precisions",
        "persistent_state_boundary",
        "decode_time_full_weight_materializations",
        "per_decode_full_weight_cast",
        "stage_local_kv",
        "cancellation_supported",
        "cleanup_supported",
        "workspace_bounded_bytes",
        "issued_at",
        "expires_at",
    }
)


class WindowsProviderUnavailable(RuntimeError):
    """A bounded refusal: the requested Windows provider path is not available."""

    def __init__(self, code: str) -> None:
        if code not in ADAPTER_CODES:
            raise ValueError(f"unregistered_windows_provider_code:{code}")
        self.code = code
        super().__init__(code)


def _refuse(code: str) -> None:
    raise WindowsProviderUnavailable(code)


def _digest(value: Any) -> bool:
    return isinstance(value, str) and DIGEST_RE.fullmatch(value) is not None


def _finite(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def windows_provider_declaration() -> dict[str, Any]:
    """The declared Windows provider boundary of this tranche, with no run claim."""

    return {
        "protocol": PROTOCOL,
        "platform_family": PLATFORM_FAMILY,
        "device_class": DEVICE_CLASS,
        "backend_id": BACKEND_ID,
        "registry_stage": RUNTIME_BACKEND_STAGES[BACKEND_ID],
        "registry_entry_is_not_qualification": True,
        "supported_provider_families": list(SUPPORTED_PROVIDER_FAMILIES),
        "claim_level": DECLARED_TARGET["claim_level"],
        "target_device_label": DECLARED_TARGET["device_label"],
        "physical_execution": DECLARED_TARGET["physical_execution"],
        "claim_boundary": CLAIM_BOUNDARY,
    }


def detect_windows_dispatch(
    *, system: object = None, machine: object = None
) -> dict[str, Any]:
    """Observe Windows x86_64 dispatch, or fail closed.

    Caller-supplied observations only.  A caller that passes nothing gets this
    host's values, which is why the adapter cannot be driven on macOS by
    accident: the observation is refused before any provider work happens.
    """

    observed_system = platform.system() if system is None else system
    observed_machine = platform.machine() if machine is None else machine
    if not isinstance(observed_system, str) or not isinstance(observed_machine, str):
        _refuse("platform_observation_invalid")
    if observed_system not in WINDOWS_SYSTEM_NAMES:
        _refuse("unsupported_platform")
    if observed_machine not in X86_64_MACHINE_NAMES:
        _refuse("unsupported_architecture")
    return {
        "protocol": PROTOCOL,
        "system": observed_system,
        "machine": observed_machine,
        "platform_family": PLATFORM_FAMILY,
        "architecture": "x86_64",
        "device_class": DEVICE_CLASS,
        # Windows-native specific: the CPU dispatch path this provider build uses.
        "dispatch": "native_x86_64",
        "physical_execution": DECLARED_TARGET["physical_execution"],
    }


def _default_win32_loader(location: str) -> object:
    """The real win32 library binding.  Only reachable on an actual win32 host."""

    import ctypes

    return ctypes.WinDLL(location)  # type: ignore[attr-defined]


def bind_provider_library(
    *,
    library_path: object,
    expected_digest: object,
    library_loader: Any = None,
    host_platform: str | None = None,
) -> dict[str, Any]:
    """Digest-pin one provider binary to the exact declared build.

    ``library_loader`` exists so the win32 binding seam is exercisable without a
    Windows host; when it is absent on a non-win32 host the binding is refused
    rather than simulated.
    """

    if not _digest(expected_digest):
        _refuse("provider_build_digest_invalid")
    path = Path(library_path) if isinstance(library_path, (str, Path)) else None
    if path is None or path.is_symlink() or not path.is_file():
        _refuse("provider_library_absent")
    try:
        raw = path.read_bytes()
    except OSError:
        _refuse("provider_library_absent")
    if "sha256:" + hashlib.sha256(raw).hexdigest() != expected_digest:
        _refuse("provider_library_digest_mismatch")
    host = sys.platform if host_platform is None else host_platform
    if not isinstance(host, str):
        _refuse("provider_library_binding_unavailable_on_host")
    loader = library_loader
    if loader is None:
        if host != "win32":
            _refuse("provider_library_binding_unavailable_on_host")
        loader = _default_win32_loader
    if not callable(loader):
        _refuse("provider_library_load_failed")
    try:
        handle = loader(str(path))
    except Exception as exc:  # noqa: BLE001 - external loader boundary
        raise WindowsProviderUnavailable("provider_library_load_failed") from exc
    if handle is None:
        _refuse("provider_library_load_failed")
    return {
        "protocol": PROTOCOL,
        "bound": True,
        "library_name": path.name,
        "library_digest": expected_digest,
        "library_bytes": len(raw),
        "host_platform": host,
        "win32_binding": host == "win32",
        "library_handle_obtained": True,
        "physical_execution": DECLARED_TARGET["physical_execution"],
    }


def validate_windows_provider_declaration(value: object) -> dict[str, Any]:
    """Closed-shape validation of one declared Windows provider build."""

    if not isinstance(value, Mapping) or set(value) != DECLARATION_FIELDS:
        _refuse("windows_provider_declaration_invalid")
    document = dict(value)
    if document["provider_family"] not in SUPPORTED_PROVIDER_FAMILIES:
        _refuse("provider_family_unregistered")
    if not _digest(document["build_digest"]) or not _digest(
        document["driver_runtime_version_digest"]
    ):
        _refuse("provider_build_digest_invalid")
    if not isinstance(document["driver_runtime_identity"], str) or not document[
        "driver_runtime_identity"
    ]:
        _refuse("windows_provider_declaration_invalid")
    precisions = document["activation_precisions"]
    if (
        not isinstance(precisions, (list, tuple))
        or not precisions
        or any(item not in SUPPORTED_DTYPES for item in precisions)
        or len(set(precisions)) != len(precisions)
    ):
        _refuse("provider_activation_precision_unsupported")
    if document["persistent_state_boundary"] not in PERSISTENT_STATE_BOUNDARIES:
        _refuse("provider_materialization_policy_rejected")
    materializations = document["decode_time_full_weight_materializations"]
    if (
        isinstance(materializations, bool)
        or not isinstance(materializations, int)
        or materializations != 0
    ):
        _refuse("provider_materialization_policy_rejected")
    if document["per_decode_full_weight_cast"] is not False:
        _refuse("provider_per_decode_cast_rejected")
    if document["stage_local_kv"] is not True:
        _refuse("provider_stage_local_kv_required")
    if document["cancellation_supported"] is not True or (
        document["cleanup_supported"] is not True
    ):
        _refuse("provider_lifecycle_support_required")
    workspace = document["workspace_bounded_bytes"]
    if isinstance(workspace, bool) or not isinstance(workspace, int) or workspace < 1:
        _refuse("provider_workspace_budget_invalid")
    if not _finite(document["issued_at"]) or not _finite(document["expires_at"]):
        _refuse("provider_validity_invalid")
    if float(document["expires_at"]) <= float(document["issued_at"]):
        _refuse("provider_validity_invalid")
    document["activation_precisions"] = list(precisions)
    return document


class WindowsNativeProviderAdapter:
    """The Windows x86_64 provider behind the platform-neutral interface.

    Execution is delegated to the landed int8 kernel seam, so this adapter adds
    no second kernel: it adds the Windows dispatch requirement, the digest-pinned
    provider binding, the provider-policy refusals, and the frozen
    acceleration-profile projection.  It never reports an executed acceleration.
    """

    def __init__(
        self,
        *,
        declaration: object,
        dispatch: object,
        library: object,
        weight: np.ndarray,
        scales: np.ndarray,
    ) -> None:
        self._declaration = validate_windows_provider_declaration(declaration)
        if not isinstance(dispatch, Mapping):
            _refuse("platform_observation_invalid")
        if dispatch.get("platform_family") != PLATFORM_FAMILY:
            _refuse("unsupported_platform")
        if dispatch.get("device_class") != DEVICE_CLASS:
            _refuse("unsupported_architecture")
        self._dispatch = dict(dispatch)
        if not isinstance(library, Mapping) or library.get("bound") is not True:
            _refuse("provider_not_bound")
        if library.get("library_digest") != self._declaration["build_digest"]:
            _refuse("provider_library_digest_mismatch")
        self._library = dict(library)
        try:
            self._kernel = Int8LinearKernel(
                weight,
                scales,
                workspace_bytes=self._declaration["workspace_bounded_bytes"],
                boundary=self._declaration["persistent_state_boundary"],
            )
        except Int8KernelUnavailable as error:
            raise WindowsProviderUnavailable(error.code) from None

    # -- platform-neutral provider interface ------------------------------
    @property
    def provider_family(self) -> str:
        return self._declaration["provider_family"]

    @property
    def build_digest(self) -> str:
        return self._declaration["build_digest"]

    @property
    def packed_state_bytes(self) -> int:
        return self._kernel.packed_state_bytes

    @property
    def activation_precisions(self) -> tuple[str, ...]:
        return tuple(self._declaration["activation_precisions"])

    def packed_state_is_stable(self) -> bool:
        return self._kernel.packed_state_is_stable()

    def warmup(self, activations: np.ndarray) -> None:
        self._kernel.warmup(activations)

    def linear(self, activations: np.ndarray) -> np.ndarray:
        try:
            return self._kernel.linear(activations)
        except Int8KernelUnavailable as error:
            raise WindowsProviderUnavailable(error.code) from None

    def ledger(self) -> MaterializationLedger:
        return self._kernel.ledger()

    def assert_warm_decode(self) -> None:
        try:
            self._kernel.assert_warm_decode()
        except Int8KernelUnavailable as error:
            raise WindowsProviderUnavailable(error.code) from None

    # -- reports ----------------------------------------------------------
    def dispatch_report(self) -> dict[str, Any]:
        return {
            **self._dispatch,
            "device_class": DEVICE_CLASS,
            "physical_execution": DECLARED_TARGET["physical_execution"],
        }

    def provider_report(self) -> dict[str, Any]:
        return {
            "protocol": PROTOCOL,
            "backend_id": BACKEND_ID,
            "registry_stage": RUNTIME_BACKEND_STAGES[BACKEND_ID],
            "provider_family": self.provider_family,
            "build_digest": self.build_digest,
            "device_class": DEVICE_CLASS,
            "bound_library_digest": self._library["library_digest"],
            "activation_precisions": list(self.activation_precisions),
            "physical_execution": DECLARED_TARGET["physical_execution"],
            "accelerated": False,
            "qualified": False,
            "blocker": "windows_x86_64_provider_not_physically_qualified",
        }

    def acceleration_profile(self) -> dict[str, Any]:
        """The exact ``mycelium.runtime_acceleration_profile.v1`` shape.

        Capability declaration only: the returned record carries a zero
        decode-time materialization policy and the real bounded packed-state
        size, and it is never a qualification (spec §2, §5).
        """

        return AccelerationProfile.from_mapping(
            {
                "protocol": ACCELERATION_PROFILE_PROTOCOL,
                "profile_id": f"windows-provider-{self.provider_family}",
                "backend_id": BACKEND_ID,
                "registry_stage": RUNTIME_BACKEND_STAGES[BACKEND_ID],
                "implementation_family": self.provider_family,
                "execution_provider": self.provider_family,
                "build_digest": self.build_digest,
                "device_class": DEVICE_CLASS,
                "driver_runtime": {
                    "identity": self._declaration["driver_runtime_identity"],
                    "version_digest": self._declaration["driver_runtime_version_digest"],
                },
                "supported_operations": sorted(REQUIRED_OPERATIONS),
                "kernels": ["int8_gemv", "int8_gemm"],
                "precision_policy": {
                    "weight_precision": "int8",
                    "activation_precisions": list(self.activation_precisions),
                    "per_decode_full_weight_cast": False,
                },
                "materialization_policy": {
                    "persistent_state_boundary": self._declaration[
                        "persistent_state_boundary"
                    ],
                    "decode_time_full_weight_materializations": 0,
                    "packed_state_bounded_bytes": self.packed_state_bytes,
                },
                "stage_local_kv": True,
                "workspace_bounded_bytes": self._declaration["workspace_bounded_bytes"],
                "cancellation_supported": True,
                "cleanup_supported": True,
                "issued_at": float(self._declaration["issued_at"]),
                "expires_at": float(self._declaration["expires_at"]),
            }
        ).to_mapping()

    def physical_qualification_report(self) -> dict[str, Any]:
        """Explicit, honest statement of what has *not* been executed."""

        return {
            "protocol": PROTOCOL,
            "backend_id": BACKEND_ID,
            "device_class": DEVICE_CLASS,
            "provider_family": self.provider_family,
            "build_digest": self.build_digest,
            "physical_execution": DECLARED_TARGET["physical_execution"],
            "accelerated": False,
            "qualified": False,
            "blocker": "windows_x86_64_provider_not_physically_qualified",
            "claim_boundary": CLAIM_BOUNDARY,
        }

    def assert_physical_qualification(self) -> None:
        """Refuse: only a physically executed Windows run can qualify this build."""

        _refuse("windows_x86_64_provider_not_physically_qualified")


def support_declaration_families() -> Sequence[str]:
    """The declared provider families, for registry-driven UI projection."""

    return tuple(SUPPORTED_PROVIDER_FAMILIES)


__all__ = [
    "ACCEPTANCE_COVERAGE",
    "ADAPTER_CODES",
    "BACKEND_ID",
    "CLAIM_BOUNDARY",
    "DECLARATION_FIELDS",
    "DECLARED_TARGET",
    "DEVICE_CLASS",
    "OTHER_WINDOWS_MACHINE_NAMES",
    "PERSISTENT_STATE_BOUNDARIES",
    "PLATFORM_FAMILY",
    "PROTOCOL",
    "SUPPORTED_PROVIDER_FAMILIES",
    "WindowsNativeProviderAdapter",
    "WindowsProviderUnavailable",
    "bind_provider_library",
    "detect_windows_dispatch",
    "support_declaration_families",
    "validate_windows_provider_declaration",
    "windows_provider_declaration",
]
