# SPDX-License-Identifier: AGPL-3.0-only
"""``mycelium.activation_codec_qualification.v1`` — exact reduced-precision codec.

Spec §3.4, §4.8.  Float16/bfloat16 activation transport is qualified for the
exact adjacent runtime/edge builds and dtype only after shape/dtype/finite-value,
output-token parity, cancellation, replay, corruption, and byte-reduction gates
pass.  Negotiation is generation-bound and fail-closed: an unsupported,
withdrawn, expired, or generation-mismatched codec uses an independently
qualified dtype or fails — it is never silently coerced (frozen decision
``qualified_activation_precision``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .class_qualification import ExactClass
from .primitives import (
    CODEC_QUALIFICATION_PROTOCOL,
    TRANSPORT_FAMILIES,
    bounded_int,
    canonical_bytes,
    closed_fields,
    digest,
    enum_value,
    finite,
    record_digest,
    require,
    require_distinct,
    segment,
    text,
)

PROTOCOL = CODEC_QUALIFICATION_PROTOCOL

DTYPES = frozenset({"float16", "bfloat16"})
WITHDRAWAL_STATES = frozenset({"current", "withdrawn"})
EDGE_FIELDS = frozenset({"runtime_build_digest", "transport_family", "device_class"})
SHAPE_FIELDS = frozenset({"hidden_size", "max_sequence", "batch"})
PARITY_FIELDS = frozenset({"output_token_parity", "sample_count", "parity_digest"})
VALIDATION_FIELDS = frozenset(
    {
        "shape_checked",
        "dtype_checked",
        "finite_values_checked",
        "byte_reduction_measured",
    }
)
REDUCTION_FIELDS = frozenset({"bytes_before", "bytes_after", "samples", "counter_digest"})
WITHDRAWAL_FIELDS = frozenset({"state", "reason"})
FALLBACK_FIELDS = frozenset({"dtype", "qualification_digest"})
#: Every negative class the codec gate must have rejected (spec §9).
REQUIRED_NEGATIVES = (
    "unsupported_dtype",
    "corrupted_frame",
    "non_finite_value",
    "wrong_shape",
    "wrong_dtype",
    "replayed_frame",
    "generation_mismatch",
)
NEGATIVE_FIELDS = frozenset({"rejected", "evidence_digest"})

FIELDS = frozenset(
    {
        "protocol",
        "qualification_id",
        "dtype",
        "sender",
        "receiver",
        "generation_binding",
        "shapes",
        "parity",
        "validation",
        "byte_reduction",
        "negatives",
        "withdrawal",
        "fallback",
        "issued_at",
        "expires_at",
    }
)


@dataclass(frozen=True, slots=True)
class CodecEdge:
    runtime_build_digest: str
    transport_family: str
    device_class: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], code: str) -> "CodecEdge":
        record = closed_fields(value, EDGE_FIELDS, code)
        return cls(
            runtime_build_digest=digest(record["runtime_build_digest"], code),
            transport_family=enum_value(record["transport_family"], TRANSPORT_FAMILIES, code),
            device_class=text(record["device_class"], code),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "runtime_build_digest": self.runtime_build_digest,
            "transport_family": self.transport_family,
            "device_class": self.device_class,
        }


@dataclass(frozen=True, slots=True)
class ActivationCodecQualification:
    qualification_id: str
    dtype: str
    sender: CodecEdge
    receiver: CodecEdge
    generation_binding: int
    hidden_size: int
    max_sequence: int
    batch: int
    output_token_parity: bool
    sample_count: int
    parity_digest: str
    shape_checked: bool
    dtype_checked: bool
    finite_values_checked: bool
    byte_reduction_measured: bool
    bytes_before: int
    bytes_after: int
    reduction_samples: int
    counter_digest: str
    negatives: Mapping[str, str]
    withdrawal_state: str
    withdrawal_reason: str | None
    fallback_dtype: str | None
    fallback_qualification_digest: str | None
    issued_at: float
    expires_at: float

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ActivationCodecQualification":
        record = closed_fields(value, FIELDS, "codec_qualification_invalid")
        require(record["protocol"] == PROTOCOL, "codec_qualification_protocol_invalid")
        shapes = closed_fields(record["shapes"], SHAPE_FIELDS, "codec_shapes_invalid")
        parity = closed_fields(record["parity"], PARITY_FIELDS, "codec_parity_invalid")
        validation = closed_fields(
            record["validation"], VALIDATION_FIELDS, "codec_validation_invalid"
        )
        reduction = closed_fields(
            record["byte_reduction"], REDUCTION_FIELDS, "codec_reduction_invalid"
        )
        withdrawal = closed_fields(
            record["withdrawal"], WITHDRAWAL_FIELDS, "codec_withdrawal_invalid"
        )
        fallback = closed_fields(
            record["fallback"], FALLBACK_FIELDS, "codec_fallback_invalid"
        )
        negatives = closed_fields(
            record["negatives"], frozenset(REQUIRED_NEGATIVES), "codec_negatives_invalid"
        )
        negative_digests: dict[str, str] = {}
        for name in REQUIRED_NEGATIVES:
            entry = closed_fields(negatives[name], NEGATIVE_FIELDS, "codec_negative_invalid")
            require(
                entry["rejected"] is True,
                f"codec_negative_not_rejected:{name}",
            )
            negative_digests[name] = digest(entry["evidence_digest"], "codec_negative_digest_invalid")
        require_distinct(list(negative_digests.values()), "codec_negative_digest_duplicate")

        state = enum_value(
            withdrawal["state"], WITHDRAWAL_STATES, "codec_withdrawal_state_invalid"
        )
        reason = (
            None
            if withdrawal["reason"] is None
            else text(withdrawal["reason"], "codec_withdrawal_reason_invalid")
        )
        if state == "withdrawn":
            require(reason is not None, "codec_withdrawal_requires_reason")
        else:
            require(reason is None, "codec_current_must_not_carry_withdrawal_reason")

        fallback_dtype = (
            None
            if fallback["dtype"] is None
            else enum_value(fallback["dtype"], DTYPES, "codec_fallback_dtype_invalid")
        )
        fallback_digest = (
            None
            if fallback["qualification_digest"] is None
            else digest(
                fallback["qualification_digest"], "codec_fallback_digest_invalid"
            )
        )
        require(
            (fallback_dtype is None) == (fallback_digest is None),
            "codec_fallback_requires_independent_qualification",
        )

        positive = bounded_int(
            reduction["bytes_before"], "codec_bytes_before_invalid", minimum=1, maximum=1 << 60
        )
        reduced = bounded_int(
            reduction["bytes_after"], "codec_bytes_after_invalid", minimum=0, maximum=1 << 60
        )
        require(
            validation["byte_reduction_measured"] is True,
            "codec_byte_reduction_not_measured",
        )
        require(reduced < positive, "codec_byte_reduction_not_present")
        require(
            parity["output_token_parity"] is True,
            "codec_output_token_parity_required",
        )
        for field in ("shape_checked", "dtype_checked", "finite_values_checked"):
            require(validation[field] is True, f"codec_validation_missing:{field}")
        issued_at = finite(record["issued_at"], "codec_issued_at_invalid", minimum=0.0)
        expires_at = finite(record["expires_at"], "codec_expires_at_invalid", minimum=0.0)
        require(expires_at > issued_at, "codec_validity_invalid")

        return cls(
            qualification_id=segment(
                record["qualification_id"], "codec_qualification_id_invalid"
            ),
            dtype=enum_value(record["dtype"], DTYPES, "codec_dtype_invalid"),
            sender=CodecEdge.from_mapping(record["sender"], "codec_sender_invalid"),
            receiver=CodecEdge.from_mapping(record["receiver"], "codec_receiver_invalid"),
            generation_binding=bounded_int(
                record["generation_binding"],
                "codec_generation_invalid",
                minimum=1,
                maximum=1 << 40,
            ),
            hidden_size=bounded_int(
                shapes["hidden_size"], "codec_hidden_size_invalid", minimum=1, maximum=1 << 30
            ),
            max_sequence=bounded_int(
                shapes["max_sequence"], "codec_max_sequence_invalid", minimum=1, maximum=1 << 30
            ),
            batch=bounded_int(shapes["batch"], "codec_batch_invalid", minimum=1, maximum=1 << 20),
            output_token_parity=require_bool(
                parity["output_token_parity"], "codec_parity_invalid"
            ),
            sample_count=bounded_int(
                parity["sample_count"], "codec_parity_samples_invalid", minimum=1, maximum=1 << 32
            ),
            parity_digest=digest(parity["parity_digest"], "codec_parity_digest_invalid"),
            shape_checked=require_bool(validation["shape_checked"], "codec_validation_invalid"),
            dtype_checked=require_bool(validation["dtype_checked"], "codec_validation_invalid"),
            finite_values_checked=require_bool(
                validation["finite_values_checked"], "codec_validation_invalid"
            ),
            byte_reduction_measured=require_bool(
                validation["byte_reduction_measured"], "codec_validation_invalid"
            ),
            bytes_before=positive,
            bytes_after=reduced,
            reduction_samples=bounded_int(
                reduction["samples"], "codec_reduction_samples_invalid", minimum=1, maximum=1 << 32
            ),
            counter_digest=digest(reduction["counter_digest"], "codec_counter_digest_invalid"),
            negatives=dict(negative_digests),
            withdrawal_state=state,
            withdrawal_reason=reason,
            fallback_dtype=fallback_dtype,
            fallback_qualification_digest=fallback_digest,
            issued_at=issued_at,
            expires_at=expires_at,
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "protocol": PROTOCOL,
            "qualification_id": self.qualification_id,
            "dtype": self.dtype,
            "sender": self.sender.to_mapping(),
            "receiver": self.receiver.to_mapping(),
            "generation_binding": self.generation_binding,
            "shapes": {
                "hidden_size": self.hidden_size,
                "max_sequence": self.max_sequence,
                "batch": self.batch,
            },
            "parity": {
                "output_token_parity": self.output_token_parity,
                "sample_count": self.sample_count,
                "parity_digest": self.parity_digest,
            },
            "validation": {
                "shape_checked": self.shape_checked,
                "dtype_checked": self.dtype_checked,
                "finite_values_checked": self.finite_values_checked,
                "byte_reduction_measured": self.byte_reduction_measured,
            },
            "byte_reduction": {
                "bytes_before": self.bytes_before,
                "bytes_after": self.bytes_after,
                "samples": self.reduction_samples,
                "counter_digest": self.counter_digest,
            },
            "negatives": {
                name: {"rejected": True, "evidence_digest": value}
                for name, value in self.negatives.items()
            },
            "withdrawal": {"state": self.withdrawal_state, "reason": self.withdrawal_reason},
            "fallback": {
                "dtype": self.fallback_dtype,
                "qualification_digest": self.fallback_qualification_digest,
            },
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_bytes(self.to_mapping(), "codec_qualification_noncanonical")

    @property
    def digest(self) -> str:
        return record_digest(self.to_mapping(), "codec_qualification_noncanonical")

    def is_current(self, *, now: float, generation: int) -> bool:
        return (
            self.withdrawal_state == "current"
            and now <= self.expires_at
            and self.generation_binding == generation
        )

    def covers_edge(self, sender: ExactClass, receiver: ExactClass) -> bool:
        return (
            self.sender.runtime_build_digest == sender.build_digest
            and self.receiver.runtime_build_digest == receiver.build_digest
        )


def negotiate_activation_codec(
    qualifications: tuple[ActivationCodecQualification, ...],
    *,
    required_dtype: str,
    generation: int,
    now: float,
) -> ActivationCodecQualification:
    """Generation-bound, fail-closed negotiation: a fallback or a failure, never a coercion."""

    enum_value(required_dtype, DTYPES, "codec_dtype_invalid")
    bounded_int(generation, "codec_generation_invalid", minimum=1)
    finite(now, "codec_now_invalid", minimum=0.0)
    for candidate in qualifications:
        if candidate.dtype != required_dtype:
            continue
        if candidate.is_current(now=now, generation=generation):
            return candidate
    for candidate in qualifications:
        if (
            candidate.dtype != required_dtype
            and candidate.is_current(now=now, generation=generation)
        ):
            return candidate
    raise CodecUnavailable("activation_codec_unavailable")


class CodecUnavailable(RuntimeError):
    """No independently qualified activation dtype is current; never coerce silently."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def require_bool(value: Any, code: str) -> bool:
    require(isinstance(value, bool), code)
    return value
