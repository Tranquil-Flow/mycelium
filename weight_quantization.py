# SPDX-License-Identifier: AGPL-3.0-only
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
    # The staging codes and row scales (one per output row, of each expert
    # for packed ``[E, out, in]`` experts) live until the packed copy exists.
    resident_bytes = elements + (elements // shape[-1]) * 4
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


#: Persistent packed int8 layout (MLX ``quantized_matmul`` / ``gather_qmm``):
#: unsigned 8-bit affine codes, group scale 1 and group bias -128, so every
#: code dequantizes to exactly its signed int8 value.
PACKED_INT8_GROUP_SIZE = 128
PACKED_INT8_BITS = 8
#: Bytes per element of the float32 group scales plus group biases.
_PACKED_INT8_GROUP_PARAMETER_BYTES = 2 * 4


def packed_int8_packable(shape: tuple[int, ...]) -> bool:
    """Whether a row-wise int8 tensor of ``shape`` takes the packed layout."""

    return (
        len(shape) in {2, 3}
        and all(extent >= 1 for extent in shape)
        and shape[-1] % PACKED_INT8_GROUP_SIZE == 0
    )


def int8_rowwise_resident_bytes(shape: tuple[int, ...]) -> int:
    """Resident bytes of one row-wise int8 tensor as the MLX loader keeps it.

    Exactly one representation is resident: the int8 codes (packed four per
    ``uint32`` when :func:`packed_int8_packable`, else plain ``int8``), one
    float32 scale per output row, and -- for the packed layout only -- the
    float32 group scales and biases (one each per 128 codes).
    """

    elements = 1
    for extent in shape:
        elements *= extent
    rows = elements // shape[-1]
    total = elements + 4 * rows
    if packed_int8_packable(shape):
        total += (
            _PACKED_INT8_GROUP_PARAMETER_BYTES * elements // PACKED_INT8_GROUP_SIZE
        )
    return total


def _mlx() -> Any:
    import mlx.core as mx

    return mx


class PackedInt8RowwiseWeight(Int8RowwiseWeight):
    """A row-wise int8 weight held ONLY in the persistent packed layout.

    ``packed`` is ``[..., out, in // 4]`` ``uint32`` (offset-binary codes,
    ``code = value + 128``), ``scales`` the row scales, ``group_scales`` /
    ``group_biases`` the ``[..., out, in // 128]`` float32 kernel parameters.
    No signed int8 copy is resident: :attr:`values` re-derives the exact
    signed codes on every access (a transient for digests, oracles and the
    per-call fallback), so a stage keeps one representation per weight.
    """

    __slots__ = ("packed", "group_scales", "group_biases")

    def __init__(
        self, *, scales: Any, packed: Any, group_scales: Any, group_biases: Any
    ) -> None:
        object.__setattr__(self, "scales", scales)
        object.__setattr__(self, "packed", packed)
        object.__setattr__(self, "group_scales", group_scales)
        object.__setattr__(self, "group_biases", group_biases)

    @property
    def values(self) -> Any:  # type: ignore[override]
        """The exact signed int8 codes, derived (never cached) from ``packed``."""

        return unpack_int8_codes(self.packed)

    @property
    def shape(self) -> Any:
        extents = tuple(int(extent) for extent in self.packed.shape)
        return (*extents[:-1], extents[-1] * 4)

    @property
    def dtype(self) -> Any:
        return _mlx().int8

    def gather_rows(self, indices: Any) -> Any:
        """Signed int8 rows ``values[indices]`` without deriving the whole matrix."""

        return unpack_int8_codes(self.packed[indices])

    def host_values(self) -> Any:
        """The signed codes as a NumPy ``int8`` array (one host transient).

        Reads the packed buffer in place, so a load-time digest never allocates
        an MLX copy of the codes.
        """

        import numpy as np

        words = np.asarray(memoryview(self.packed))
        return np.bitwise_xor(words.view(np.uint8), np.uint8(0x80)).view(np.int8)

    def float32_values(self) -> Any:
        """``values.astype(float32)``, bit for bit, straight from the packed codes.

        ``code * 1 + (-128)`` is exact in float32, so the per-call float path
        needs no signed int8 intermediate next to its float32 transient.
        """

        mx = _mlx()
        return mx.dequantize(
            self.packed,
            self.group_scales,
            self.group_biases,
            group_size=PACKED_INT8_GROUP_SIZE,
            bits=PACKED_INT8_BITS,
        )

    def __repr__(self) -> str:
        return f"PackedInt8RowwiseWeight(shape={self.shape})"

    def __eq__(self, other: object) -> bool:
        return self is other

    __hash__ = object.__hash__


def unpack_int8_codes(packed: Any) -> Any:
    """Signed int8 codes from offset-binary ``uint32`` words (exact inverse)."""

    mx = _mlx()
    codes = packed.view(mx.uint8)
    return mx.bitwise_xor(codes, mx.array(0x80, dtype=mx.uint8)).view(mx.int8)


def pack_int8_codes(values: Any) -> Any:
    """Offset-binary ``uint32`` words of signed int8 ``values`` (last axis / 4)."""

    mx = _mlx()
    codes = mx.bitwise_xor(values.view(mx.uint8), mx.array(0x80, dtype=mx.uint8))
    return codes.view(mx.uint32)


def packed_int8_weight(packed: Any, scales: Any) -> PackedInt8RowwiseWeight:
    """Wrap evaluated packed codes with their row scales and group parameters."""

    mx = _mlx()
    groups_shape = (
        *tuple(int(extent) for extent in packed.shape[:-1]),
        int(packed.shape[-1]) * 4 // PACKED_INT8_GROUP_SIZE,
    )
    group_scales = mx.ones(groups_shape, dtype=mx.float32)
    group_biases = mx.full(groups_shape, -128.0, dtype=mx.float32)
    mx.eval(packed, scales, group_scales, group_biases)
    return PackedInt8RowwiseWeight(
        scales=scales,
        packed=packed,
        group_scales=group_scales,
        group_biases=group_biases,
    )


def int8_float32_values(weight: Int8RowwiseWeight) -> Any:
    """The exact float32 image of an MLX row-wise int8 weight's codes."""

    float32_values = getattr(weight, "float32_values", None)
    if float32_values is not None:
        return float32_values()
    return weight.values.astype(_mlx().float32)


def int8_rows(weight: Int8RowwiseWeight, indices: Any) -> Any:
    """Signed int8 rows of ``weight`` (embedding gather) in either layout."""

    gather_rows = getattr(weight, "gather_rows", None)
    if gather_rows is not None:
        return gather_rows(indices)
    return weight.values[indices]


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
    "PACKED_INT8_BITS",
    "PACKED_INT8_GROUP_SIZE",
    "PackedInt8RowwiseWeight",
    "ROWWISE_INT8_CONVERSION_CHUNK_FLOAT_BYTES",
    "ROWWISE_INT8_CONVERSION_TEMPORARY_COPIES",
    "int8_float32_values",
    "int8_rowwise_resident_bytes",
    "int8_rows",
    "pack_int8_codes",
    "packed_int8_packable",
    "packed_int8_weight",
    "rowwise_int8_kind",
    "rowwise_int8_streaming_transient_bytes",
    "unpack_int8_codes",
]
