"""Canonical attempt-scoped Router idempotency keys and payload identity."""

from hashlib import sha256
import struct


def _length_prefixed(value: bytes) -> bytes:
   return len(value).to_bytes(8, "big") + value


def _canonical_payload(value: object, active: set[int]) -> bytes:
   if value is None:
      return b"n"
   if isinstance(value, bool):
      return b"b1" if value else b"b0"
   if isinstance(value, int):
      return b"i" + _length_prefixed(str(value).encode("ascii"))
   if isinstance(value, float):
      return b"f" + struct.pack(">d", value)
   if isinstance(value, str):
      return b"s" + _length_prefixed(value.encode("utf-8"))
   if isinstance(value, bytes):
      return b"y" + _length_prefixed(value)
   if isinstance(value, (bytearray, memoryview)):
      payload = bytes(value)
      return b"y" + _length_prefixed(payload)

   marker = id(value)
   if marker in active:
      raise TypeError("recursive_payload")
   active.add(marker)
   try:
      if isinstance(value, tuple):
         items = b"".join(
            _length_prefixed(_canonical_payload(item, active)) for item in value
         )
         return b"t" + len(value).to_bytes(8, "big") + items
      if isinstance(value, list):
         items = b"".join(
            _length_prefixed(_canonical_payload(item, active)) for item in value
         )
         return b"l" + len(value).to_bytes(8, "big") + items
      if isinstance(value, dict):
         pairs = sorted(
            (
               _canonical_payload(key, active),
               _canonical_payload(item, active),
            )
            for key, item in value.items()
         )
         encoded = b"".join(
            _length_prefixed(key) + _length_prefixed(item)
            for key, item in pairs
         )
         return b"d" + len(pairs).to_bytes(8, "big") + encoded
      raise TypeError("unsupported_payload_type")
   finally:
      active.remove(marker)


def _snapshot_payload(value: object, active: set[int]) -> object:
   if value is None or isinstance(value, (bool, int, float, str, bytes)):
      return value
   if isinstance(value, (bytearray, memoryview)):
      return bytes(value)

   marker = id(value)
   if marker in active:
      raise TypeError("recursive_payload")
   active.add(marker)
   try:
      if isinstance(value, tuple):
         return tuple(_snapshot_payload(item, active) for item in value)
      if isinstance(value, list):
         return [_snapshot_payload(item, active) for item in value]
      if isinstance(value, dict):
         return {
            _snapshot_payload(key, active): _snapshot_payload(item, active)
            for key, item in value.items()
         }
      raise TypeError("unsupported_payload_type")
   finally:
      active.remove(marker)


def snapshot_payload(payload: object) -> object:
   """Detach accepted payload values from caller-owned mutable containers."""

   return _snapshot_payload(payload, set())


def payload_fingerprint(payload: object) -> str:
   """Return a value-stable digest without retaining or logging payload bytes."""

   return sha256(_canonical_payload(payload, set())).hexdigest()


def hop_idempotency_key(
   *,
   request_id: str,
   path_id: str,
   path_attempt: int,
   phase: str,
   token_index: int,
   hop_index: int,
) -> str:
   return (
      f"{request_id}:{path_id}:{path_attempt}:"
      f"{phase}:{token_index}:{hop_index}"
   )
