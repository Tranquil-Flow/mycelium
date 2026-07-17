"""Versioned data-plane payloads owned by the Router boundary.

The activation format is deliberately tensor-library neutral.  It carries only
canonical dtype/shape metadata and little-endian element bytes; it never uses
pickle, JSON numbers, or an MLX-specific object representation on the wire.
"""

from dataclasses import dataclass
import struct
from typing import Iterator


_TOKEN_MAGIC = b"MYTK"
_TOKEN_VERSION = 1
_TOKEN_HEADER = struct.Struct(">4sBI")
_TOKEN = struct.Struct(">I")
_MAX_TOKEN_IDS = 1_048_576

_ACTIVATION_MAGIC = b"MYAC"
_ACTIVATION_VERSION = 1
_ACTIVATION_HEADER = struct.Struct(">4sBBBBQ")
_ACTIVATION_DIMENSION = struct.Struct(">I")
_ACTIVATION_LITTLE_ENDIAN = 1
_MAX_ACTIVATION_RANK = 8
_MAX_ACTIVATION_BYTES = 268_435_456
_DTYPE_CODES = {"float16": 1, "bfloat16": 2, "float32": 3}
_DTYPES_BY_CODE = {code: dtype for dtype, code in _DTYPE_CODES.items()}
_DTYPE_BYTES = {"float16": 2, "bfloat16": 2, "float32": 4}


class PayloadError(ValueError):
   def __init__(self, code: str, detail: str = ""):
      self.code = code
      self.detail = detail
      super().__init__(code if not detail else f"{code}: {detail}")


@dataclass(frozen=True)
class ActivationPayload:
   """Decoded ``mycelium.activation.v1`` tensor envelope.

   ``data`` is the contiguous row-major element representation in little-endian
   order.  Iteration is supported so callers can also unpack the contract as
   ``dtype, shape, data = decode_activation(...)``.
   """

   dtype: str
   shape: tuple[int, ...]
   data: bytes

   def __iter__(self) -> Iterator[object]:
      yield self.dtype
      yield self.shape
      yield self.data


# A descriptive compatibility name for callers that refer to the wire object as
# an envelope rather than a payload.
ActivationEnvelope = ActivationPayload


def encode_token_ids(token_ids: tuple[int, ...]) -> bytes:
   if not isinstance(token_ids, tuple):
      raise PayloadError("token_ids_must_be_tuple")
   if len(token_ids) > _MAX_TOKEN_IDS:
      raise PayloadError("too_many_token_ids")
   encoded = bytearray(
      _TOKEN_HEADER.pack(_TOKEN_MAGIC, _TOKEN_VERSION, len(token_ids))
   )
   for token_id in token_ids:
      if (
         not isinstance(token_id, int)
         or isinstance(token_id, bool)
         or token_id < 0
         or token_id > 0xFFFFFFFF
      ):
         raise PayloadError("invalid_token_id", str(token_id))
      encoded.extend(_TOKEN.pack(token_id))
   return bytes(encoded)


def decode_token_ids(payload: bytes) -> tuple[int, ...]:
   if not isinstance(payload, bytes):
      raise PayloadError("token_payload_must_be_bytes")
   if len(payload) < _TOKEN_HEADER.size:
      raise PayloadError("truncated_token_payload")
   magic, version, count = _TOKEN_HEADER.unpack(payload[: _TOKEN_HEADER.size])
   if magic != _TOKEN_MAGIC:
      raise PayloadError("invalid_token_payload_magic")
   if version != _TOKEN_VERSION:
      raise PayloadError("unknown_token_payload_version", str(version))
   if count > _MAX_TOKEN_IDS:
      raise PayloadError("too_many_token_ids")
   expected_length = _TOKEN_HEADER.size + count * _TOKEN.size
   if len(payload) != expected_length:
      raise PayloadError("token_payload_length_mismatch")
   return tuple(
      _TOKEN.unpack_from(payload, _TOKEN_HEADER.size + index * _TOKEN.size)[0]
      for index in range(count)
   )


def _activation_fields(
   activation: object,
   *,
   dtype: str | None,
   shape: tuple[int, ...] | None,
   data: bytes | None,
) -> ActivationPayload:
   if isinstance(activation, ActivationPayload):
      if any(value is not None for value in (dtype, shape, data)):
         raise PayloadError("ambiguous_activation_payload")
      return activation
   if activation is not None:
      if any(value is not None for value in (dtype, shape, data)):
         raise PayloadError("ambiguous_activation_payload")
      try:
         array_dtype = str(getattr(activation, "dtype"))
         array_shape = tuple(getattr(activation, "shape"))
         array_data = bytes(activation)
      except (AttributeError, TypeError, ValueError) as exc:
         raise PayloadError("invalid_activation_value") from exc
      dtype = array_dtype.removeprefix("mlx.core.")
      shape = array_shape
      data = array_data
   if dtype is None or shape is None or data is None:
      raise PayloadError("activation_fields_required")
   return ActivationPayload(dtype=dtype, shape=shape, data=data)


def _validate_activation(activation: ActivationPayload) -> int:
   if activation.dtype not in _DTYPE_CODES:
      raise PayloadError("unsupported_activation_dtype", str(activation.dtype))
   if not isinstance(activation.shape, tuple):
      raise PayloadError("activation_shape_must_be_tuple")
   rank = len(activation.shape)
   if rank <= 0 or rank > _MAX_ACTIVATION_RANK:
      raise PayloadError("invalid_activation_rank", str(rank))
   element_count = 1
   for dimension in activation.shape:
      if (
         not isinstance(dimension, int)
         or isinstance(dimension, bool)
         or dimension <= 0
         or dimension > 0xFFFFFFFF
      ):
         raise PayloadError("invalid_activation_dimension", str(dimension))
      element_count *= dimension
      if element_count * _DTYPE_BYTES[activation.dtype] > _MAX_ACTIVATION_BYTES:
         raise PayloadError("activation_payload_too_large")
   if not isinstance(activation.data, bytes):
      raise PayloadError("activation_data_must_be_bytes")
   expected_bytes = element_count * _DTYPE_BYTES[activation.dtype]
   if len(activation.data) != expected_bytes:
      raise PayloadError("activation_data_length_mismatch")
   return expected_bytes


def encode_activation(
   activation: object = None,
   *,
   dtype: str | None = None,
   shape: tuple[int, ...] | None = None,
   data: bytes | None = None,
) -> bytes:
   """Encode an activation envelope or an array-like object.

   Array-like values must expose ``dtype`` and ``shape`` and support ``bytes``;
   this includes ``mlx.core.array`` without making the payload contract depend
   on MLX.  Explicit callers can instead pass ``dtype``, ``shape``, and ``data``.
   """

   envelope = _activation_fields(
      activation,
      dtype=dtype,
      shape=shape,
      data=data,
   )
   data_length = _validate_activation(envelope)
   encoded = bytearray(
      _ACTIVATION_HEADER.pack(
         _ACTIVATION_MAGIC,
         _ACTIVATION_VERSION,
         _DTYPE_CODES[envelope.dtype],
         len(envelope.shape),
         _ACTIVATION_LITTLE_ENDIAN,
         data_length,
      )
   )
   for dimension in envelope.shape:
      encoded.extend(_ACTIVATION_DIMENSION.pack(dimension))
   encoded.extend(envelope.data)
   return bytes(encoded)


def decode_activation(payload: bytes) -> ActivationPayload:
   """Decode and fully validate one activation payload, failing closed."""

   if not isinstance(payload, bytes):
      raise PayloadError("activation_payload_must_be_bytes")
   if len(payload) < _ACTIVATION_HEADER.size:
      raise PayloadError("truncated_activation_payload")
   magic, version, dtype_code, rank, byte_order, data_length = (
      _ACTIVATION_HEADER.unpack_from(payload)
   )
   if magic != _ACTIVATION_MAGIC:
      raise PayloadError("invalid_activation_payload_magic")
   if version != _ACTIVATION_VERSION:
      raise PayloadError("unknown_activation_payload_version", str(version))
   if dtype_code not in _DTYPES_BY_CODE:
      raise PayloadError("unknown_activation_dtype_code", str(dtype_code))
   if byte_order != _ACTIVATION_LITTLE_ENDIAN:
      raise PayloadError("unsupported_activation_byte_order", str(byte_order))
   if rank <= 0 or rank > _MAX_ACTIVATION_RANK:
      raise PayloadError("invalid_activation_rank", str(rank))
   metadata_length = _ACTIVATION_HEADER.size + rank * _ACTIVATION_DIMENSION.size
   if len(payload) < metadata_length:
      raise PayloadError("truncated_activation_shape")
   if data_length > _MAX_ACTIVATION_BYTES:
      raise PayloadError("activation_payload_too_large")
   if len(payload) != metadata_length + data_length:
      raise PayloadError("activation_payload_length_mismatch")
   shape = tuple(
      _ACTIVATION_DIMENSION.unpack_from(
         payload,
         _ACTIVATION_HEADER.size + index * _ACTIVATION_DIMENSION.size,
      )[0]
      for index in range(rank)
   )
   envelope = ActivationPayload(
      dtype=_DTYPES_BY_CODE[dtype_code],
      shape=shape,
      data=payload[metadata_length:],
   )
   _validate_activation(envelope)
   return envelope


# Explicit aliases make the public wire-contract names discoverable while the
# shorter names remain symmetrical with encode_token_ids/decode_token_ids.
encode_activation_payload = encode_activation
decode_activation_payload = decode_activation
