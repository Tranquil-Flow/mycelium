# SPDX-License-Identifier: AGPL-3.0-or-later
"""``mycelium.stage_capabilities.v1`` - the frozen PV29 stage contract.

Design record: ``docs/design/PV29-STAGE-CONTRACT.md``.  This module freezes four
things and nothing else:

* the closed stage-backend registry (``llama_stage`` is registered but fenced),
* the closed per-stage quantization vocabulary and the lossy-stage refusal,
* the closed, canonically serialised :class:`StageCapabilities` record,
* the route-wide wire dtype negotiation rule (plan R-W1/R-W2).

Claim boundary: a declaration here is a contract, not execution evidence.  A
capability record is never qualification, eligibility, or route readiness, and
registering ``llama_stage`` does not make it selectable.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
import importlib.util
import re
from types import MappingProxyType
from typing import Any

from .primitives import (
    MAX_LIST_ITEMS,
    RUNTIME_BACKEND_STAGES,
    PlatformNeutralContractError,
    bounded_int,
    canonical_bytes,
    closed_fields,
    digest,
    nullable_enum,
    nullable_text,
    record_digest,
    require,
    segment,
    text,
)

PROTOCOL = "mycelium.stage_capabilities.v1"

#: Stage-capable backends.  ``llama_http`` is deliberately absent: it is a
#: whole-model replica path and cannot run a layer range (plan section 1.2).
STAGE_BACKENDS = frozenset({"mlx", "numpy", "llama_stage", "mobile-rust-qwen2"})
#: Registered but not selectable until the backend's runtime module exists.
FENCED_STAGE_BACKENDS = frozenset({"llama_stage"})
#: Module whose presence unfences a backend (an import spec lookup only; the
#: module is never imported by this contract).
_FENCED_RUNTIME_MODULES = {"llama_stage": "mycelium_router.llama_stage_runtime"}

#: Frozen per-stage quantization vocabulary (plan section 2.4).
STAGE_QUANTIZATIONS = (
    "none",
    "int8-weight-only",
    "gguf-f16",
    "gguf-q8_0",
    "gguf-q4_k_m",
)
#: Lossy classes (plan gate Q1).  Refused unless the owner explicitly opts in
#: (owner question Q3, default pending owner: lossless or Q8_0/int8 only).
LOSSY_STAGE_QUANTIZATIONS = frozenset({"gguf-q4_k_m"})
#: Name of the explicit, default-off, owner opt-in for lossy stages.
LOSSY_OPT_IN_FLAG = "allow_lossy_stage_quantization"

#: Weight format -> quantization id it realises.
WEIGHT_FORMAT_QUANTIZATION = {
    "safetensors-bf16": "none",
    "int8-rowwise": "int8-weight-only",
    "gguf-f16": "gguf-f16",
    "gguf-q8_0": "gguf-q8_0",
    "gguf-q4_k_m": "gguf-q4_k_m",
}
WEIGHT_FORMATS = frozenset(WEIGHT_FORMAT_QUANTIZATION)

WIRE_DTYPES = frozenset({"float32", "float16", "bfloat16"})
#: What a route may use unless a later, separately gated decision (PV28) widens
#: it.  float32 is what every registered model uses today.
DEFAULT_ENABLED_WIRE_DTYPES = ("float32",)
ROUTE_WIRE_FALLBACK_DTYPE = "float32"

ARCHITECTURES = frozenset({"gpt2", "qwen2", "qwen3", "qwen3_5", "qwen3_5_moe"})
DECODE_MODES = frozenset({"stage_local_kv", "complete_context_replay"})
#: The only KV checkpoint format each backend may declare (``None`` always ok).
KV_CHECKPOINT_FORMATS = {
    "numpy": "numpy-kv-v1",
    "mlx": "mlx-kv-v1",
    "llama_stage": "llama-seq-v1",
    "mobile-rust-qwen2": None,
}
#: Backends whose compute path is float32 only (NumPy lacks bfloat16; the phone
#: runtime shares the NumPy contract).
_FLOAT32_ONLY_BACKENDS = frozenset({"numpy", "mobile-rust-qwen2"})

_DEVICE_RE = re.compile(r"^(cpu|metal|(cuda|vulkan):[0-9]{1,3})$")
_MAX_CTX = 1 << 30

FIELDS = frozenset(
    {
        "protocol",
        "backend",
        "backend_version",
        "build_digest",
        "device",
        "device_name",
        "architectures",
        "weight_formats",
        "wire_dtypes_in",
        "wire_dtypes_out",
        "decode_modes",
        "kv_checkpoint_format",
        "batching",
        "max_ctx",
        "memory_footprint_estimator",
        "deterministic",
    }
)


def require_stage_backend(backend: Any) -> str:
    """Return ``backend`` iff it is a registered stage backend, else refuse."""

    require(
        isinstance(backend, str)
        and backend in STAGE_BACKENDS
        and (backend == "mobile-rust-qwen2" or backend in RUNTIME_BACKEND_STAGES),
        "stage_backend_unregistered",
    )
    return backend


def stage_backend_selectable(
    backend: Any, *, runtime_present: Collection[str] | None = None
) -> bool:
    """True when ``backend`` may be selected for a stage on this host.

    Unfenced backends are selectable.  A fenced backend is selectable only when
    its runtime is present: either named in ``runtime_present`` or, by default,
    its runtime module exists.
    """

    require_stage_backend(backend)
    if backend not in FENCED_STAGE_BACKENDS:
        return True
    if runtime_present is not None:
        return backend in runtime_present
    module = _FENCED_RUNTIME_MODULES[backend]
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def admit_stage_quantization(quantization: Any, **options: bool) -> str:
    """Admit one quantization id to a route, refusing unknown and lossy ids.

    ``allow_lossy_stage_quantization`` is the only option, keyword-only, strictly
    boolean and default off (owner question Q3).
    """

    allow_lossy = options.pop(LOSSY_OPT_IN_FLAG, False)
    require(not options, "stage_quantization_option_invalid")
    require(type(allow_lossy) is bool, "stage_quantization_option_invalid")
    require(
        isinstance(quantization, str) and quantization in STAGE_QUANTIZATIONS,
        "stage_quantization_unregistered",
    )
    require(
        allow_lossy or quantization not in LOSSY_STAGE_QUANTIZATIONS,
        "lossy_stage_quantization_refused",
    )
    return quantization


def _flag(value: Any, code: str) -> bool:
    require(type(value) is bool, code)
    return value


def _canonical_set(
    value: Any, allowed: Iterable[str], code: str, *, nonempty: bool = True
) -> tuple[str, ...]:
    """A list of distinct registered names in strictly ascending order."""

    require(isinstance(value, (list, tuple)), code)
    require(len(value) <= MAX_LIST_ITEMS and (bool(value) or not nonempty), code)
    allowed_set = frozenset(allowed)
    for item in value:
        require(isinstance(item, str) and item in allowed_set, code)
    require(all(a < b for a, b in zip(value, value[1:])), code)
    return tuple(value)


@dataclass(frozen=True, slots=True)
class StageCapabilities:
    """One stage backend's closed capability declaration."""

    backend: str
    backend_version: str
    build_digest: str
    device: str
    device_name: str
    architectures: tuple[str, ...]
    weight_formats: tuple[str, ...]
    wire_dtypes_in: tuple[str, ...]
    wire_dtypes_out: tuple[str, ...]
    decode_modes: tuple[str, ...]
    kv_checkpoint_format: str | None
    batching: bool
    max_ctx: int
    memory_footprint_estimator: str | None
    deterministic: bool

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "StageCapabilities":
        record = closed_fields(value, FIELDS, "stage_capabilities_invalid")
        require(record["protocol"] == PROTOCOL, "stage_capabilities_protocol_invalid")
        backend = require_stage_backend(record["backend"])
        wire_dtypes = {}
        for direction in ("wire_dtypes_in", "wire_dtypes_out"):
            allowed = (
                {"float32"} if backend in _FLOAT32_ONLY_BACKENDS else WIRE_DTYPES
            )
            wire_dtypes[direction] = _canonical_set(
                record[direction], allowed, "stage_capabilities_wire_dtypes_invalid"
            )
        kv_format = nullable_enum(
            record["kv_checkpoint_format"],
            (KV_CHECKPOINT_FORMATS[backend],) if KV_CHECKPOINT_FORMATS[backend] else (),
            "stage_capabilities_kv_format_invalid",
        )
        device = record["device"]
        require(
            isinstance(device, str) and _DEVICE_RE.fullmatch(device) is not None,
            "stage_capabilities_device_invalid",
        )
        return cls(
            backend=backend,
            backend_version=text(
                record["backend_version"],
                "stage_capabilities_backend_version_invalid",
                maximum=64,
            ),
            build_digest=digest(
                record["build_digest"], "stage_capabilities_build_digest_invalid"
            ),
            device=device,
            device_name=text(
                record["device_name"],
                "stage_capabilities_device_name_invalid",
                maximum=128,
            ),
            architectures=_canonical_set(
                record["architectures"],
                ARCHITECTURES,
                "stage_capabilities_architectures_invalid",
            ),
            weight_formats=_canonical_set(
                record["weight_formats"],
                WEIGHT_FORMATS,
                "stage_capabilities_weight_formats_invalid",
            ),
            wire_dtypes_in=wire_dtypes["wire_dtypes_in"],
            wire_dtypes_out=wire_dtypes["wire_dtypes_out"],
            decode_modes=_canonical_set(
                record["decode_modes"],
                DECODE_MODES,
                "stage_capabilities_decode_modes_invalid",
            ),
            kv_checkpoint_format=kv_format,
            batching=_flag(record["batching"], "stage_capabilities_batching_invalid"),
            max_ctx=bounded_int(
                record["max_ctx"],
                "stage_capabilities_max_ctx_invalid",
                minimum=1,
                maximum=_MAX_CTX,
            ),
            memory_footprint_estimator=(
                None
                if record["memory_footprint_estimator"] is None
                else segment(
                    record["memory_footprint_estimator"],
                    "stage_capabilities_estimator_invalid",
                )
            ),
            deterministic=_flag(
                record["deterministic"], "stage_capabilities_deterministic_invalid"
            ),
        )

    def to_mapping(self) -> dict[str, Any]:
        """A fresh, JSON-plain mapping (lists, never shared state)."""

        return {
            "protocol": PROTOCOL,
            "backend": self.backend,
            "backend_version": self.backend_version,
            "build_digest": self.build_digest,
            "device": self.device,
            "device_name": self.device_name,
            "architectures": list(self.architectures),
            "weight_formats": list(self.weight_formats),
            "wire_dtypes_in": list(self.wire_dtypes_in),
            "wire_dtypes_out": list(self.wire_dtypes_out),
            "decode_modes": list(self.decode_modes),
            "kv_checkpoint_format": self.kv_checkpoint_format,
            "batching": self.batching,
            "max_ctx": self.max_ctx,
            "memory_footprint_estimator": self.memory_footprint_estimator,
            "deterministic": self.deterministic,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_bytes(self.to_mapping(), "stage_capabilities_invalid")

    def digest(self) -> str:
        return record_digest(self.to_mapping(), "stage_capabilities_invalid")

    @property
    def quantizations(self) -> tuple[str, ...]:
        """Quantization ids realised by the declared weight formats."""

        return tuple(WEIGHT_FORMAT_QUANTIZATION[item] for item in self.weight_formats)

    def admitted_quantizations(self, **options: bool) -> tuple[str, ...]:
        """The declared quantizations a route may admit under the lossy policy."""

        allow_lossy = options.pop(LOSSY_OPT_IN_FLAG, False)
        require(not options, "stage_quantization_option_invalid")
        result = []
        for quantization in self.quantizations:
            try:
                result.append(
                    admit_stage_quantization(
                        quantization, **{LOSSY_OPT_IN_FLAG: allow_lossy}
                    )
                )
            except PlatformNeutralContractError as error:
                if error.code != "lossy_stage_quantization_refused":
                    raise
        return tuple(result)


#: Static wire dtype ceiling per declared backend, for callers that only know the
#: backend id (the route compiler); a full record's ``wire_dtypes_*`` may only
#: narrow this.  Matches the per-backend rule enforced in ``from_mapping``.
DECLARED_BACKEND_WIRE_DTYPES = MappingProxyType(
    {
        backend: (
            frozenset({"float32"})
            if backend in _FLOAT32_ONLY_BACKENDS
            else frozenset(WIRE_DTYPES)
        )
        for backend in STAGE_BACKENDS
    }
)


def _negotiate(
    entries: Sequence[tuple[str, frozenset[str]]],
    *,
    requested: str | None,
    enabled: Iterable[str],
    runtime_present: Collection[str] | None,
) -> str:
    for backend, _common in entries:
        if not stage_backend_selectable(backend, runtime_present=runtime_present):
            raise PlatformNeutralContractError("fenced_stage_backend_unavailable")
    try:
        enabled_set = frozenset(enabled)
    except TypeError:
        raise PlatformNeutralContractError("wire_dtype_not_enabled") from None
    dtype = ROUTE_WIRE_FALLBACK_DTYPE if requested is None else requested
    require(
        isinstance(dtype, str) and dtype in WIRE_DTYPES and dtype in enabled_set,
        "wire_dtype_not_enabled",
    )
    mixed = len({backend for backend, _common in entries}) > 1
    require(
        dtype == ROUTE_WIRE_FALLBACK_DTYPE or not mixed,
        "mixed_backend_route_requires_float32",
    )
    require(
        all(dtype in common for _backend, common in entries),
        "wire_dtype_negotiation_failed",
    )
    return dtype


def negotiate_route_wire_dtype(
    capabilities: Sequence[StageCapabilities],
    *,
    requested: str | None = None,
    enabled: Iterable[str] = DEFAULT_ENABLED_WIRE_DTYPES,
    runtime_present: Collection[str] | None = None,
) -> str:
    """The one wire dtype a route uses for every hop (plan R-W1, R-W2).

    * The result is a member of every stage's ``wire_dtypes_in`` and
      ``wire_dtypes_out``; there is never a per-hop or silent coercion.
    * ``float32`` is the default and the only value in ``enabled`` unless a
      separately gated decision (PV28) widens it.  There is no automatic
      downgrade: an unsatisfiable request is refused.
    * A route that mixes backends uses ``float32`` only.
    * A fenced backend cannot join a route unless its runtime is present.
    """

    require(
        isinstance(capabilities, (list, tuple))
        and bool(capabilities)
        and all(isinstance(item, StageCapabilities) for item in capabilities),
        "route_stage_capabilities_invalid",
    )
    return _negotiate(
        [
            (item.backend, frozenset(item.wire_dtypes_in) & frozenset(item.wire_dtypes_out))
            for item in capabilities
        ],
        requested=requested,
        enabled=enabled,
        runtime_present=runtime_present,
    )


def negotiate_route_wire_dtype_for_backends(
    backends: Sequence[str],
    *,
    requested: str | None = None,
    enabled: Iterable[str] = DEFAULT_ENABLED_WIRE_DTYPES,
    runtime_present: Collection[str] | None = None,
) -> str:
    """:func:`negotiate_route_wire_dtype` over declared backend ids only.

    Uses each backend's static ceiling (:data:`DECLARED_BACKEND_WIRE_DTYPES`);
    the route compiler calls this where it fixes the route's hop dtype.
    """

    require(
        isinstance(backends, (list, tuple)) and bool(backends),
        "route_stage_capabilities_invalid",
    )
    for backend in backends:
        require_stage_backend(backend)
    return _negotiate(
        [(backend, DECLARED_BACKEND_WIRE_DTYPES[backend]) for backend in backends],
        requested=requested,
        enabled=enabled,
        runtime_present=runtime_present,
    )


__all__ = [
    "ARCHITECTURES",
    "DECLARED_BACKEND_WIRE_DTYPES",
    "DECODE_MODES",
    "DEFAULT_ENABLED_WIRE_DTYPES",
    "FENCED_STAGE_BACKENDS",
    "KV_CHECKPOINT_FORMATS",
    "LOSSY_OPT_IN_FLAG",
    "LOSSY_STAGE_QUANTIZATIONS",
    "PROTOCOL",
    "ROUTE_WIRE_FALLBACK_DTYPE",
    "STAGE_BACKENDS",
    "STAGE_QUANTIZATIONS",
    "StageCapabilities",
    "WEIGHT_FORMATS",
    "WEIGHT_FORMAT_QUANTIZATION",
    "WIRE_DTYPES",
    "admit_stage_quantization",
    "negotiate_route_wire_dtype",
    "negotiate_route_wire_dtype_for_backends",
    "require_stage_backend",
    "stage_backend_selectable",
]
