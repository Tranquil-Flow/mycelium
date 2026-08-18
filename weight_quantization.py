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
    """An output-row quantized matrix and one positive scale per row."""

    values: Any
    scales: Any

    @property
    def shape(self) -> Any:
        return self.values.shape

    @property
    def dtype(self) -> Any:
        return self.values.dtype


__all__ = [
    "Int8RowwiseWeight",
    "ROWWISE_INT8_CONVERSION_CHUNK_FLOAT_BYTES",
    "ROWWISE_INT8_CONVERSION_TEMPORARY_COPIES",
    "rowwise_int8_streaming_transient_bytes",
]
