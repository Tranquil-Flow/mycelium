# SPDX-License-Identifier: AGPL-3.0-only
"""Efficient int8 weight execution — portable source kernels with real ledgers.

Spec §3.4, frozen decisions ``persistent_int8_execution`` and
``accelerated_windows_backend``.

Scope of this module, stated so it cannot be read as more than it is:

* It is a *portable reference host* implementation (``numpy``) of the int8 weight
  execution contract: bounded persistent packed state created at load/warmup,
  integer accumulation that never recasts or expands a whole weight matrix at
  decode time, a monotonic materialization ledger, and a negative control that
  detects the rejected per-linear whole-weight cast.
* It is **not** an accelerated backend.  ``KERNEL_HOST_BACKEND`` is registered at
  ``implemented`` only, and ``numpy`` is a correctness-only identity (spec §3.2):
  it can never satisfy ``accelerated_windows_backend``, which stays blocked on a
  physically qualified native Windows x86_64 provider.  That device blocker is
  raised explicitly by :func:`accelerated_windows_kernel_required` and reported by
  the benchmark harness instead of being papered over.

Ledger semantics (the part the acceptance gate reads):

``full_weight_materializations``
    Monotonic count of conversions whose single extent covered the *entire*
    packed weight matrix.  Warm decode must leave it unchanged (at zero for a
    weight that does not fit the workspace budget in one slab).
``bounded_slab_conversions``
    Monotonic count of bounded weight-slab conversions, each of which fits inside
    the declared ``workspace_bytes`` budget.
``peak_weight_workspace_bytes`` / ``peak_activation_workspace_bytes``
    Monotonic peaks of the two transient budgets, reported separately so a
    token-proportional activation buffer is never confused with weight state.

Integer accumulation is exact: activations are dynamically quantized per row to
int8, products are accumulated in int32, and the result is scaled once on output.
The dequantized result is therefore bit-identical to
:func:`quantized_reference_linear`, which is asserted in the acceptance tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np

#: Registered backend identity of this kernel host.  ``numpy`` is registered at
#: ``implemented`` and is a portable correctness/reference identity only.
KERNEL_HOST_BACKEND = "numpy"
#: Backends that can never mint an acceleration claim (spec §3.2, §10).
CORRECTNESS_ONLY_HOSTS = frozenset({"numpy", "unknown"})

#: Default transient budget derived from the packed weight during one decode step.
#: 256 KiB is a realistic kernel tile: for the decoder shapes measured here it is
#: well below one whole weight matrix, so a decode never converts the whole weight
#: in a single call.
DEFAULT_WORKSPACE_BYTES = 256 * 1024
#: int32 is the accumulator width for int8 x int8 products.
ACCUMULATOR_BYTES = 4
ACTIVATION_MAX = 127

#: Closed set of boundary/refusal codes this module can raise.  Every raise site
#: uses a member; the acceptance tests assert membership rather than docstrings.
BOUNDARY_CODES = frozenset(
    {
        "weight_representation_unsupported",
        "weight_scale_invalid",
        "workspace_budget_invalid",
        "activation_width_mismatch",
        "activation_not_finite",
        "warm_decode_not_proven",
        "materializing_shape_rejected",
        "windows_x86_64_provider_not_physically_qualified",
        "accelerated_provider_required",
        "benchmark_must_not_claim_qualification",
    }
)

#: Acceptance-inventory coverage for this tranche.  Every entry is a negative
#: statement on purpose: source invariants are not physical execution.
ACCEPTANCE_COVERAGE = {
    "int8_decode_materialization_rejection": (
        "not_earned: physical_negative acceptance case requires the materialization "
        "and bounded-workspace challenge window on a physically qualified native "
        "Windows x86_64 provider; this tranche proves the invariants and the "
        "detector on a portable reference host only"
    ),
    "numpy_reference_not_performance_eligible": (
        "not_earned: the reference host is correctness-only by construction; the "
        "performance-eligible decision belongs to the runtime-efficiency contract "
        "and needs executed acceleration evidence"
    ),
}


class Int8KernelUnavailable(RuntimeError):
    """A bounded refusal: the requested int8 execution path is not available.

    Distinct from :class:`mycelium_platform_neutral.primitives.PlatformNeutralContractError`
    (a contract-shape violation) so a boundary refusal can never be mistaken for
    a malformed record.
    """

    def __init__(self, code: str) -> None:
        if code not in BOUNDARY_CODES:
            raise ValueError(f"unregistered_int8_kernel_code:{code}")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class MaterializationLedger:
    """Immutable snapshot of the monotonic materialization counters."""

    full_weight_materializations: int
    bounded_slab_conversions: int
    peak_weight_workspace_bytes: int
    peak_activation_workspace_bytes: int
    decode_calls: int
    warmup_complete: bool


def backend_is_correctness_only(backend_id: str) -> bool:
    return backend_id in CORRECTNESS_ONLY_HOSTS


def accelerated_windows_kernel_required() -> None:
    """The A9 accelerated Windows device blocker, raised rather than described."""

    raise Int8KernelUnavailable("windows_x86_64_provider_not_physically_qualified")


def _require_finite(activations: np.ndarray) -> None:
    if not bool(np.isfinite(activations).all()):
        raise Int8KernelUnavailable("activation_not_finite")


def quantize_activations(activations: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Dynamic per-row symmetric int8 quantization of one activation block.

    Returns ``(int8_values, per_row_scale)``.  A zero row keeps scale ``1.0`` so an
    all-zero activation never divides by zero and never changes the result.
    """

    block = np.ascontiguousarray(activations, dtype=np.float32)
    magnitude = np.abs(block).max(axis=1) / float(ACTIVATION_MAX)
    scale = np.where(magnitude == 0.0, np.float32(1.0), magnitude).astype(np.float32)
    scaled = np.rint(block / scale[:, None])
    quantized = np.clip(scaled, -ACTIVATION_MAX, ACTIVATION_MAX).astype(np.int8)
    return quantized, scale


def quantized_reference_linear(
    activations: np.ndarray, weight: np.ndarray, scales: np.ndarray
) -> np.ndarray:
    """The exact integer reference the kernel must reproduce bit for bit.

    One int32 accumulation over the whole matrix, scaled once.  Because integer
    arithmetic is exact and associative, the chunked kernel output equals this
    array bitwise; a mismatch means the kernel changed the arithmetic.
    """

    rows = np.ascontiguousarray(activations, dtype=np.float32)
    quantized, act_scale = quantize_activations(rows)
    accumulators = quantized.astype(np.int32) @ np.ascontiguousarray(weight, dtype=np.int8).astype(np.int32).T
    return (accumulators.astype(np.float32) * (act_scale[:, None] * scales[None, :])).astype(np.float32)


class Int8LinearKernel:
    """Bounded persistent int8 weight state for one linear operation.

    ``workspace_bytes`` is the maximum transient byte budget the kernel may
    derive from the packed weight during a single decode step.  The kernel splits
    the weight into slab rows that fit that budget; a single slab that happens to
    cover the whole matrix is *reported* as a full-weight materialization instead
    of being hidden.
    """

    def __init__(
        self,
        weight: np.ndarray,
        scales: np.ndarray,
        *,
        workspace_bytes: int = DEFAULT_WORKSPACE_BYTES,
        boundary: str = "load",
    ) -> None:
        values = np.asarray(weight)
        if values.dtype != np.int8 or values.ndim != 2 or 0 in values.shape:
            raise Int8KernelUnavailable("weight_representation_unsupported")
        scale_values = np.asarray(scales, dtype=np.float32)
        if (
            scale_values.shape != (values.shape[0],)
            or not bool(np.isfinite(scale_values).all())
            or bool((scale_values <= 0.0).any())
        ):
            raise Int8KernelUnavailable("weight_scale_invalid")
        if not isinstance(workspace_bytes, int) or isinstance(workspace_bytes, bool):
            raise Int8KernelUnavailable("workspace_budget_invalid")
        per_row_bytes = values.shape[1] * ACCUMULATOR_BYTES
        if workspace_bytes < min(per_row_bytes, values.size * ACCUMULATOR_BYTES):
            raise Int8KernelUnavailable("workspace_budget_invalid")
        if boundary not in ("load", "warmup"):
            raise Int8KernelUnavailable("weight_representation_unsupported")

        # Persistent packed state: created once, at the declared boundary, kept int8.
        self._values = np.ascontiguousarray(values, dtype=np.int8)
        self._scales = np.ascontiguousarray(scale_values, dtype=np.float32)
        self._workspace_bytes = workspace_bytes
        self._boundary = boundary
        self._out_features, self._in_features = self._values.shape
        self._packed_state_bytes = int(self._values.nbytes + self._scales.nbytes)
        self._slab_rows = max(
            1, min(self._out_features, workspace_bytes // per_row_bytes)
        )
        self._full_weight_materializations = 0
        self._bounded_slab_conversions = 0
        self._peak_weight_workspace_bytes = 0
        self._peak_activation_workspace_bytes = 0
        self._decode_calls = 0
        self._warmup_complete = False

    # -- packed state ----------------------------------------------------
    @property
    def packed_state_bytes(self) -> int:
        return self._packed_state_bytes

    @property
    def packed_values_dtype(self) -> np.dtype:
        return self._values.dtype

    @property
    def boundary(self) -> str:
        return self._boundary

    @property
    def slab_rows(self) -> int:
        """Output rows converted from the packed weight per bounded slab."""

        return self._slab_rows

    def packed_state_is_stable(self) -> bool:
        return self._packed_state_bytes == int(self._values.nbytes + self._scales.nbytes)

    # -- execution -------------------------------------------------------
    def warmup(self, activations: np.ndarray) -> None:
        """Declared warmup boundary: one decode pass, then the counters are read."""

        self.linear(activations)
        self._warmup_complete = True

    def linear(self, activations: np.ndarray) -> np.ndarray:
        block = np.asarray(activations)
        if block.ndim == 1:
            block = block.reshape(1, -1)
        if block.ndim != 2 or block.shape[1] != self._in_features:
            raise Int8KernelUnavailable("activation_width_mismatch")
        rows = np.ascontiguousarray(block, dtype=np.float32)
        _require_finite(rows)

        quantized, act_scale = quantize_activations(rows)
        act_workspace = int(quantized.astype(np.int32).nbytes)
        self._peak_activation_workspace_bytes = max(
            self._peak_activation_workspace_bytes, act_workspace
        )
        accumulators = quantized.astype(np.int32)
        out = np.empty((rows.shape[0], self._out_features), dtype=np.float32)
        for start in range(0, self._out_features, self._slab_rows):
            stop = min(start + self._slab_rows, self._out_features)
            slab = self._values[start:stop]
            slab_bytes = int(slab.size * ACCUMULATOR_BYTES)
            self._peak_weight_workspace_bytes = max(
                self._peak_weight_workspace_bytes, slab_bytes
            )
            if stop - start == self._out_features:
                self._full_weight_materializations += 1
            else:
                self._bounded_slab_conversions += 1
            products = accumulators @ slab.astype(np.int32).T
            factor = act_scale[:, None] * self._scales[start:stop][None, :]
            out[:, start:stop] = (products.astype(np.float32) * factor).astype(np.float32)
        self._decode_calls += 1
        return out

    # -- ledger ----------------------------------------------------------
    def ledger(self) -> MaterializationLedger:
        return MaterializationLedger(
            full_weight_materializations=self._full_weight_materializations,
            bounded_slab_conversions=self._bounded_slab_conversions,
            peak_weight_workspace_bytes=self._peak_weight_workspace_bytes,
            peak_activation_workspace_bytes=self._peak_activation_workspace_bytes,
            decode_calls=self._decode_calls,
            warmup_complete=self._warmup_complete,
        )

    def assert_warm_decode(self) -> None:
        """Refuse to call a decode window warm unless it is actually warm."""

        if (
            not self._warmup_complete
            or self._full_weight_materializations != 0
            or not self.packed_state_is_stable()
        ):
            raise Int8KernelUnavailable("warm_decode_not_proven")


class PerCallFullWeightCastLinear:
    """Negative control: the rejected per-linear whole-weight ``int8 -> float32`` cast.

    This exists so the ledger's detector is measured against a real instance of
    the shortcut the spec rejects (``persistent_int8_execution``), and so the
    benchmark can price that shortcut honestly.  It must never be registered as
    an accelerated or performance-ready backend.
    """

    def __init__(self, weight: np.ndarray, scales: np.ndarray) -> None:
        self._values = np.ascontiguousarray(np.asarray(weight), dtype=np.int8)
        self._scales = np.ascontiguousarray(np.asarray(scales), dtype=np.float32)
        self._full_weight_materializations = 0
        self._peak_weight_workspace_bytes = 0
        self._warmup_complete = False
        self._decode_calls = 0

    def linear(self, activations: np.ndarray) -> np.ndarray:
        block = np.ascontiguousarray(np.asarray(activations), dtype=np.float32)
        materialized = self._values.astype(np.float32) * self._scales[:, None]
        self._full_weight_materializations += 1
        self._peak_weight_workspace_bytes = max(
            self._peak_weight_workspace_bytes, int(materialized.nbytes)
        )
        self._decode_calls += 1
        return (block @ materialized.T).astype(np.float32)

    def ledger(self) -> MaterializationLedger:
        return MaterializationLedger(
            full_weight_materializations=self._full_weight_materializations,
            bounded_slab_conversions=0,
            peak_weight_workspace_bytes=self._peak_weight_workspace_bytes,
            peak_activation_workspace_bytes=0,
            decode_calls=self._decode_calls,
            warmup_complete=self._warmup_complete,
        )

    def assert_warm_decode(self) -> None:
        raise Int8KernelUnavailable("warm_decode_not_proven")


def rowwise_int8_profile(weight: np.ndarray, scales: np.ndarray) -> Mapping[str, Any]:
    """Bounded, privacy-reduced description of a packed weight (no values)."""

    values = np.asarray(weight)
    if values.dtype != np.int8 or values.ndim != 2:
        raise Int8KernelUnavailable("weight_representation_unsupported")
    return {
        "out_features": int(values.shape[0]),
        "in_features": int(values.shape[1]),
        "packed_state_bytes": int(values.nbytes + np.asarray(scales).nbytes),
        "float32_equivalent_bytes": int(values.size * 4),
        "storage_reduction_ratio": float(4.0),
    }


def reference_host_report(workspace_bytes: Sequence[int] = ()) -> Mapping[str, Any]:
    """Explicit, honest statement of what this kernel host is and is not."""

    return {
        "backend_id": KERNEL_HOST_BACKEND,
        "correctness_only": backend_is_correctness_only(KERNEL_HOST_BACKEND),
        "accelerated": False,
        "qualification_claim": False,
        "windows_x86_64_qualified": False,
        "blocker": "windows_x86_64_provider_not_physically_qualified",
        "workspace_budget_bytes": list(workspace_bytes)
        or [DEFAULT_WORKSPACE_BYTES],
    }


__all__ = [
    "ACCUMULATOR_BYTES",
    "ACCEPTANCE_COVERAGE",
    "ACTIVATION_MAX",
    "BOUNDARY_CODES",
    "CORRECTNESS_ONLY_HOSTS",
    "DEFAULT_WORKSPACE_BYTES",
    "KERNEL_HOST_BACKEND",
    "Int8KernelUnavailable",
    "Int8LinearKernel",
    "MaterializationLedger",
    "PerCallFullWeightCastLinear",
    "accelerated_windows_kernel_required",
    "backend_is_correctness_only",
    "quantize_activations",
    "quantized_reference_linear",
    "reference_host_report",
    "rowwise_int8_profile",
]
