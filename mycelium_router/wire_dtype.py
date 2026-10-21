# SPDX-License-Identifier: AGPL-3.0-or-later
"""Exact conversion between compute arrays and ``MYAC`` activation element bytes.

This is the single place a NumPy-backed stage converts floats to and from the
wire.  Rules (PV29 R-W2/R-W3, ``docs/design/PV29-STAGE-CONTRACT.md``):

* bfloat16 on the wire is raw little-endian u16 bits: encode is
  round-to-nearest-even from float32 (bitwise equal to the platform-neutral
  ``activation_transport.encode_bfloat16``), decode is exact widening;
* float16 and float32 are IEEE little-endian;
* never silent: non-finite values are refused, and a finite value beyond the
  target format's range is refused instead of saturating or becoming ``inf``.

The compute dtype of a NumPy stage is always float32; decoding always returns
float32.  This module changes no wire framing (see ``payloads.py``).
"""

from __future__ import annotations

from typing import Any

import numpy as np

FP16_MAX = float(np.finfo(np.float16).max)  # 65504.0
FP32_MAX = float(np.finfo(np.float32).max)
#: Largest finite bfloat16 (bits 0x7F7F).
BF16_MAX = float(np.array([0x7F7F0000], dtype=np.uint32).view(np.float32)[0])

_ELEMENT_BYTES = {"float16": 2, "bfloat16": 2, "float32": 4}
_LIMITS = {
   "float16": (FP16_MAX, "activation_float16_overflow"),
   "bfloat16": (BF16_MAX, "activation_bfloat16_overflow"),
   "float32": (FP32_MAX, "activation_float32_overflow"),
}


class WireDtypeError(ValueError):
   """A bounded wire conversion refusal carrying one stable code."""

   def __init__(self, code: str, detail: str = "") -> None:
      self.code = code
      self.detail = detail
      super().__init__(code if not detail else f"{code}: {detail}")


def _require_dtype(dtype: Any) -> str:
   if not isinstance(dtype, str) or dtype not in _ELEMENT_BYTES:
      raise WireDtypeError("unsupported_wire_dtype", repr(dtype))
   return dtype


def encode_wire_array(array: Any, dtype: str) -> bytes:
   """Wire element bytes (C order, little-endian) of a float array."""

   _require_dtype(dtype)
   values = np.asarray(array)
   if values.dtype.kind != "f":
      raise WireDtypeError("unsupported_activation_array", str(values.dtype))
   if not np.isfinite(values).all():
      raise WireDtypeError("nonfinite_activation")
   limit, code = _LIMITS[dtype]
   if values.size and float(np.abs(values).max()) > limit:
      raise WireDtypeError(code)
   as_float32 = np.ascontiguousarray(values, dtype=np.float32)
   if dtype == "float32":
      return as_float32.astype("<f4", copy=False).tobytes()
   if dtype == "float16":
      return as_float32.astype("<f2").tobytes()
   bits = as_float32.view(np.uint32)
   rounded = (bits + np.uint32(0x7FFF) + ((bits >> np.uint32(16)) & np.uint32(1))) >> np.uint32(16)
   return rounded.astype("<u2").tobytes()


def decode_wire_array(data: bytes, dtype: str, shape: tuple[int, ...]) -> np.ndarray:
   """float32 array of the given shape from wire element bytes (exact widening)."""

   _require_dtype(dtype)
   if not isinstance(data, bytes) or not isinstance(shape, tuple):
      raise WireDtypeError("activation_data_length_mismatch")
   count = 1
   for dimension in shape:
      if not isinstance(dimension, int) or isinstance(dimension, bool) or dimension < 0:
         raise WireDtypeError("activation_data_length_mismatch")
      count *= dimension
   if len(data) != count * _ELEMENT_BYTES[dtype]:
      raise WireDtypeError("activation_data_length_mismatch")
   if dtype == "float32":
      flat = np.frombuffer(data, dtype="<f4")
      if not flat.dtype.isnative:
         flat = flat.astype(np.float32)
   elif dtype == "float16":
      flat = np.frombuffer(data, dtype="<f2").astype(np.float32)
   else:
      raw = np.frombuffer(data, dtype="<u2").astype(np.uint32)
      flat = (raw << np.uint32(16)).view(np.float32)
   return flat.reshape(shape)


__all__ = [
   "BF16_MAX",
   "FP16_MAX",
   "FP32_MAX",
   "WireDtypeError",
   "decode_wire_array",
   "encode_wire_array",
]
