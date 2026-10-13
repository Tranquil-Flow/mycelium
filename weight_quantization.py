"""Small backend-neutral value object for symmetric row-wise int8 weights."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


ROWWISE_INT8_CONVERSION_CHUNK_FLOAT_BYTES = 32 * 1024 * 1024
ROWWISE_INT8_CONVERSION_TEMPORARY_COPIES = 10


def rowwise_int8_streaming_transient_bytes(
    shape: tuple[int, ...],
    *,
    quantized_matrix: bool,
) -> int:
    """Conservative transient bound for the chunked row-wise converter."""

    elements = 1
    for extent in shape:
        elements *= extent
    float_bytes = elements * 4
    if not quantized_matrix:
        return float_bytes * 3
    resident_bytes = elements + shape[0] * 4
    chunk_bytes = min(float_bytes, ROWWISE_INT8_CONVERSION_CHUNK_FLOAT_BYTES)
    return (
        resident_bytes
        + chunk_bytes * ROWWISE_INT8_CONVERSION_TEMPORARY_COPIES
    )


@dataclass(frozen=True, slots=True)
class Int8RowwiseWeight:
    """An output-row quantized matrix and one positive scale per row.

    For packed MoE experts ``values`` is ``[E, out, in]`` and ``scales`` is
    ``[E, out]``: still one scale per output row, of each expert.
    """

    values: Any
    scales: Any

    @property
    def shape(self) -> Any:
        return self.values.shape

    @property
    def dtype(self) -> Any:
        return self.values.dtype


#: Packed 3-D MoE expert tensors (``[E, out, in]``, no ``.weight`` suffix):
#: quantized per expert and per output row, scales ``[E, out]``.
PACKED_EXPERT_TENSOR_SUFFIXES = (".mlp.experts.gate_up_proj", ".mlp.experts.down_proj")
#: MoE routing matrices kept in float32 under int8-weight-only: the router
#: decides which experts run, so its logits are never computed from int8 codes.
FLOAT32_ROUTING_TENSOR_SUFFIXES = (".mlp.gate.weight", ".mlp.shared_expert_gate.weight")


def rowwise_int8_kind(key: str, ndim: int) -> str | None:
    """How int8-weight-only stores tensor ``key`` of rank ``ndim``.

    ``"matrix"``: a 2-D ``.weight`` matrix, one scale per output row (the
    incumbent rule).  ``"experts"``: a packed 3-D expert tensor, one scale per
    expert and output row.  ``None``: kept in the runtime dtype (norms, conv,
    biases, MoE routing matrices).
    """

    if key.endswith(FLOAT32_ROUTING_TENSOR_SUFFIXES):
        return None
    if ndim == 2 and key.endswith(".weight"):
        return "matrix"
    if ndim == 3 and key.endswith(PACKED_EXPERT_TENSOR_SUFFIXES):
        return "experts"
    return None


__all__ = [
    "FLOAT32_ROUTING_TENSOR_SUFFIXES",
    "Int8RowwiseWeight",
    "PACKED_EXPERT_TENSOR_SUFFIXES",
    "ROWWISE_INT8_CONVERSION_CHUNK_FLOAT_BYTES",
    "ROWWISE_INT8_CONVERSION_TEMPORARY_COPIES",
    "rowwise_int8_kind",
    "rowwise_int8_streaming_transient_bytes",
]
