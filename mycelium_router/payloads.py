"""Versioned data-plane payloads owned by the Router boundary."""

import struct


_TOKEN_MAGIC = b"MYTK"
_TOKEN_VERSION = 1
_HEADER = struct.Struct(">4sBI")
_TOKEN = struct.Struct(">I")
_MAX_TOKEN_IDS = 1_048_576


class PayloadError(ValueError):
   def __init__(self, code: str, detail: str = ""):
      self.code = code
      self.detail = detail
      super().__init__(code if not detail else f"{code}: {detail}")


def encode_token_ids(token_ids: tuple[int, ...]) -> bytes:
   if not isinstance(token_ids, tuple):
      raise PayloadError("token_ids_must_be_tuple")
   if len(token_ids) > _MAX_TOKEN_IDS:
      raise PayloadError("too_many_token_ids")
   encoded = bytearray(_HEADER.pack(_TOKEN_MAGIC, _TOKEN_VERSION, len(token_ids)))
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
   if len(payload) < _HEADER.size:
      raise PayloadError("truncated_token_payload")
   magic, version, count = _HEADER.unpack(payload[: _HEADER.size])
   if magic != _TOKEN_MAGIC:
      raise PayloadError("invalid_token_payload_magic")
   if version != _TOKEN_VERSION:
      raise PayloadError("unknown_token_payload_version", str(version))
   if count > _MAX_TOKEN_IDS:
      raise PayloadError("too_many_token_ids")
   expected_length = _HEADER.size + count * _TOKEN.size
   if len(payload) != expected_length:
      raise PayloadError("token_payload_length_mismatch")
   return tuple(
      _TOKEN.unpack_from(payload, _HEADER.size + index * _TOKEN.size)[0]
      for index in range(count)
   )
