# SPDX-License-Identifier: AGPL-3.0-only
"""Authenticated, encrypted A7 shadow-checkpoint channel and signed standby acks.

A7 specification section 5 requires KV checkpoints to travel on a dedicated,
bounded, authenticated, encrypted channel (never the activation frame path, never
public evidence), and every standby acknowledgement to be signed and bound to the
stored digest and the standby's current incarnation.

* The standby holds an X25519 checkpoint key for one membership incarnation and
  publishes its public half in a record signed by its Ed25519 node signer.  The
  source seals to that key only after verifying the record against the standby's
  pinned verification key.
* Sealing is ephemeral-static X25519, HKDF-SHA256 and ChaCha20-Poly1305.  The
  checkpoint binding (request, attempt, stage, chain position, committed watermark,
  payload digest, standby placement and incarnation, expiry) is the AEAD
  associated data, so a sealed checkpoint cannot be replayed under another
  binding, to another standby, or for another incarnation.
* The standby opens it, checks the plaintext digest against the binding, and
  signs the acknowledgement statement; the coordinator verifies the signature with
  the standby's pinned key before the route watermark algebra may consume it.

Bindings and acknowledgements carry digests, counts and positions only; raw KV,
prompts and tokens stay inside the ciphertext.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import struct
import time
from collections.abc import Callable, Mapping
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from mycelium_qualification.signing import Ed25519EvidenceSigner

STANDBY_KEY_PROTOCOL = "mycelium.a7_standby_checkpoint_key.v1"
SEALED_CHECKPOINT_PROTOCOL = "mycelium.a7_sealed_checkpoint.v1"
SIGNED_ACK_PROTOCOL = "mycelium.a7_signed_checkpoint_ack.v1"
SEALED_MAGIC = b"MYCSKV1\x00"
_HEADER_LENGTH = struct.Struct("<I")
_MAX_HEADER = 16 * 1024
_HKDF_INFO = b"mycelium.a7.checkpoint.v1"
_DIGEST_FIELDS = (
   "request_key",
   "stage_id",
   "committed_prefix_digest",
   "checkpoint_digest",
   "standby_placement_id",
   "standby_membership_incarnation",
   "source_placement_id",
)
BINDING_FIELDS = frozenset(
   {
      "request_key",
      "request_attempt",
      "stage_id",
      "checkpoint_sequence",
      "predecessor_digest",
      "committed_token_count",
      "committed_prefix_digest",
      "kv_position",
      "checkpoint_digest",
      "byte_size",
      "standby_placement_id",
      "standby_membership_incarnation",
      "source_placement_id",
      "expires_at_unix_ms",
   }
)
_KEY_FIELDS = frozenset(
   {"protocol", "standby_placement_id", "standby_membership_incarnation", "x25519_public_key", "signature"}
)


class CheckpointChannelError(ValueError):
   """A channel operation was refused; ``code`` is a stable, value-free reason."""

   def __init__(self, code: str) -> None:
      super().__init__(code)
      self.code = code


def _refuse(code: str) -> None:
   raise CheckpointChannelError(code)


def _canonical(value: Any) -> bytes:
   return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def payload_digest(payload: bytes) -> str:
   return "sha256:" + hashlib.sha256(bytes(payload)).hexdigest()


def _is_digest(value: Any) -> bool:
   return (
      isinstance(value, str)
      and value.startswith("sha256:")
      and len(value) == 71
      and all(character in "0123456789abcdef" for character in value[7:])
   )


def _non_negative(value: Any) -> bool:
   return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def validate_binding(binding: Any) -> dict[str, Any]:
   if not isinstance(binding, Mapping) or frozenset(binding) != BINDING_FIELDS:
      _refuse("a7_checkpoint_binding_invalid")
   document = dict(binding)
   for field in _DIGEST_FIELDS:
      if not _is_digest(document[field]):
         _refuse("a7_checkpoint_binding_invalid")
   for field in ("request_attempt", "checkpoint_sequence", "committed_token_count", "kv_position", "byte_size", "expires_at_unix_ms"):
      if not _non_negative(document[field]):
         _refuse("a7_checkpoint_binding_invalid")
   if document["request_attempt"] < 1 or document["byte_size"] < 1 or document["expires_at_unix_ms"] < 1:
      _refuse("a7_checkpoint_binding_invalid")
   predecessor = document["predecessor_digest"]
   if (document["checkpoint_sequence"] == 0) != (predecessor is None):
      _refuse("a7_checkpoint_binding_invalid")
   if predecessor is not None and not _is_digest(predecessor):
      _refuse("a7_checkpoint_binding_invalid")
   return document


def _signed_statement(protocol: str, body: Mapping[str, Any]) -> dict[str, Any]:
   return {"protocol": protocol, **body}


class StandbyCheckpointKey:
   """One standby incarnation's private X25519 checkpoint key (never serialized)."""

   def __init__(
      self,
      *,
      signer: Ed25519EvidenceSigner,
      standby_placement_id: str,
      standby_membership_incarnation: str,
      x25519_private_bytes: bytes | None = None,
   ) -> None:
      if not _is_digest(standby_placement_id) or not _is_digest(standby_membership_incarnation):
         _refuse("a7_standby_key_identity_invalid")
      if x25519_private_bytes is None:
         self._private = X25519PrivateKey.generate()
      else:
         # A staged, owner-only standby key: its public record can be pinned on
         # the source and entry nodes before the standby process starts.
         if not isinstance(x25519_private_bytes, bytes) or len(x25519_private_bytes) != 32:
            _refuse("a7_standby_key_material_invalid")
         self._private = X25519PrivateKey.from_private_bytes(x25519_private_bytes)
      self.standby_placement_id = standby_placement_id
      self.standby_membership_incarnation = standby_membership_incarnation
      body = {
         "standby_placement_id": standby_placement_id,
         "standby_membership_incarnation": standby_membership_incarnation,
         "x25519_public_key": base64.b64encode(self._private.public_key().public_bytes_raw()).decode("ascii"),
      }
      self._record = {
         "protocol": STANDBY_KEY_PROTOCOL,
         **body,
         "signature": signer.sign(_signed_statement(STANDBY_KEY_PROTOCOL, body)),
      }

   def public_record(self) -> dict[str, Any]:
      return json.loads(_canonical(self._record))

   def _shared(self, ephemeral_public: bytes) -> bytes:
      try:
         return self._private.exchange(X25519PublicKey.from_public_bytes(ephemeral_public))
      except ValueError:
         _refuse("a7_sealed_checkpoint_key_invalid")


def verify_standby_key_record(
   record: Any,
   *,
   verify_signature: Callable[[bytes, dict[str, Any]], bool],
   standby_placement_id: str,
   standby_membership_incarnation: str,
) -> bytes:
   """Return the standby's X25519 public key after verifying its signed record."""

   if not isinstance(record, Mapping) or frozenset(record) != _KEY_FIELDS or record["protocol"] != STANDBY_KEY_PROTOCOL:
      _refuse("a7_standby_key_record_invalid")
   if record["standby_placement_id"] != standby_placement_id:
      _refuse("a7_standby_key_placement_mismatch")
   if record["standby_membership_incarnation"] != standby_membership_incarnation:
      _refuse("a7_standby_key_incarnation_mismatch")
   body = {field: record[field] for field in ("standby_placement_id", "standby_membership_incarnation", "x25519_public_key")}
   signature = record["signature"]
   if not isinstance(signature, dict) or not verify_signature(_canonical(_signed_statement(STANDBY_KEY_PROTOCOL, body)), signature):
      _refuse("a7_standby_key_signature_invalid")
   try:
      public = base64.b64decode(record["x25519_public_key"], validate=True)
   except (ValueError, TypeError):
      _refuse("a7_standby_key_record_invalid")
   if len(public) != 32:
      _refuse("a7_standby_key_record_invalid")
   return public


def _cipher(shared: bytes, ephemeral_public: bytes, recipient_public: bytes) -> ChaCha20Poly1305:
   key = HKDF(
      algorithm=hashes.SHA256(),
      length=32,
      salt=ephemeral_public + recipient_public,
      info=_HKDF_INFO,
   ).derive(shared)
   return ChaCha20Poly1305(key)


def seal_checkpoint(
   payload: bytes,
   *,
   binding: Mapping[str, Any],
   standby_public_key: bytes,
   max_bytes: int,
) -> bytes:
   """Seal one checkpoint payload to the verified standby key under its binding."""

   document = validate_binding(binding)
   if not isinstance(payload, (bytes, bytearray)) or not payload:
      _refuse("a7_checkpoint_payload_invalid")
   if len(payload) > max_bytes:
      _refuse("a7_checkpoint_payload_too_large")
   if document["checkpoint_digest"] != payload_digest(payload) or document["byte_size"] != len(payload):
      _refuse("a7_checkpoint_binding_payload_mismatch")
   ephemeral = X25519PrivateKey.generate()
   ephemeral_public = ephemeral.public_key().public_bytes_raw()
   try:
      shared = ephemeral.exchange(X25519PublicKey.from_public_bytes(standby_public_key))
   except ValueError:
      _refuse("a7_sealed_checkpoint_key_invalid")
   nonce = os.urandom(12)
   header = {
      "protocol": SEALED_CHECKPOINT_PROTOCOL,
      "binding": document,
      "ephemeral_public_key": base64.b64encode(ephemeral_public).decode("ascii"),
      "nonce": base64.b64encode(nonce).decode("ascii"),
   }
   encoded = _canonical(header)
   ciphertext = _cipher(shared, ephemeral_public, standby_public_key).encrypt(nonce, bytes(payload), encoded)
   return SEALED_MAGIC + _HEADER_LENGTH.pack(len(encoded)) + encoded + ciphertext


def _sealed_header(sealed: bytes, *, max_bytes: int) -> tuple[dict[str, Any], bytes, int, int]:
   """Parse the sealed header; its raw bytes are the AEAD associated data."""

   if not isinstance(sealed, (bytes, bytearray)) or len(sealed) > max_bytes + _MAX_HEADER + 64:
      _refuse("a7_sealed_checkpoint_too_large")
   prefix = len(SEALED_MAGIC) + _HEADER_LENGTH.size
   if len(sealed) < prefix or bytes(sealed[: len(SEALED_MAGIC)]) != SEALED_MAGIC:
      _refuse("a7_sealed_checkpoint_invalid")
   (length,) = _HEADER_LENGTH.unpack_from(sealed, len(SEALED_MAGIC))
   if length > _MAX_HEADER or prefix + length > len(sealed):
      _refuse("a7_sealed_checkpoint_invalid")
   raw = bytes(sealed[prefix : prefix + length])
   try:
      header = json.loads(raw.decode("utf-8"))
   except (UnicodeDecodeError, ValueError):
      _refuse("a7_sealed_checkpoint_invalid")
   if (
      not isinstance(header, dict)
      or frozenset(header) != {"protocol", "binding", "ephemeral_public_key", "nonce"}
      or header["protocol"] != SEALED_CHECKPOINT_PROTOCOL
      or _canonical(header) != raw
   ):
      _refuse("a7_sealed_checkpoint_invalid")
   return header, raw, prefix, length


def sealed_checkpoint_binding(sealed: bytes, *, max_bytes: int) -> dict[str, Any]:
   """The binding a sealed checkpoint names, read before it is opened.

   Not authenticated by itself: ``open_checkpoint`` authenticates exactly these
   header bytes as associated data, so a checkpoint that opens stores this
   binding and no other. Use it to refuse a shipment before storing anything.
   """

   header, _raw, _prefix, _length = _sealed_header(sealed, max_bytes=max_bytes)
   return validate_binding(header["binding"])


def open_checkpoint(
   sealed: bytes,
   *,
   standby_key: StandbyCheckpointKey,
   max_bytes: int,
   now_unix_ms: int | None = None,
) -> tuple[dict[str, Any], bytes]:
   """Authenticate and decrypt a sealed checkpoint addressed to this standby."""

   header, raw, prefix, length = _sealed_header(sealed, max_bytes=max_bytes)
   binding = validate_binding(header["binding"])
   if binding["standby_placement_id"] != standby_key.standby_placement_id:
      _refuse("a7_sealed_checkpoint_wrong_standby")
   if binding["standby_membership_incarnation"] != standby_key.standby_membership_incarnation:
      _refuse("a7_sealed_checkpoint_stale_incarnation")
   current = int(time.time() * 1000) if now_unix_ms is None else now_unix_ms
   if not math.isfinite(current) or current >= binding["expires_at_unix_ms"]:
      _refuse("a7_sealed_checkpoint_expired")
   try:
      ephemeral_public = base64.b64decode(header["ephemeral_public_key"], validate=True)
      nonce = base64.b64decode(header["nonce"], validate=True)
   except (ValueError, TypeError):
      _refuse("a7_sealed_checkpoint_invalid")
   if len(ephemeral_public) != 32 or len(nonce) != 12:
      _refuse("a7_sealed_checkpoint_invalid")
   recipient_public = standby_key._private.public_key().public_bytes_raw()
   try:
      payload = _cipher(standby_key._shared(ephemeral_public), ephemeral_public, recipient_public).decrypt(
         nonce, bytes(sealed[prefix + length :]), raw
      )
   except InvalidTag:
      _refuse("a7_sealed_checkpoint_authentication_failed")
   if len(payload) > max_bytes or len(payload) != binding["byte_size"] or payload_digest(payload) != binding["checkpoint_digest"]:
      _refuse("a7_sealed_checkpoint_digest_mismatch")
   return binding, payload


def signed_ack_statement(ack: Mapping[str, Any]) -> bytes:
   """The exact bytes a standby signs for one acknowledgement."""

   return _canonical(_signed_statement(SIGNED_ACK_PROTOCOL, {"ack": dict(ack)}))


def sign_checkpoint_ack(signer: Ed25519EvidenceSigner, ack: Mapping[str, Any]) -> dict[str, Any]:
   """Wrap a validated private checkpoint ack with the standby's node signature."""

   from mycelium_a7_kv_successor import validate_private_kv_checkpoint_ack

   record = validate_private_kv_checkpoint_ack(ack)
   return {
      "protocol": SIGNED_ACK_PROTOCOL,
      "ack": record,
      "signature": signer.sign(_signed_statement(SIGNED_ACK_PROTOCOL, {"ack": record})),
   }


def verify_signed_checkpoint_ack(
   signed: Any,
   *,
   verify_signature: Callable[[bytes, dict[str, Any]], bool],
   binding: Mapping[str, Any],
) -> dict[str, Any]:
   """Return the ack only if the standby signed it for exactly this binding."""

   from mycelium_a7_kv_successor import validate_private_kv_checkpoint_ack

   if not isinstance(signed, Mapping) or frozenset(signed) != {"protocol", "ack", "signature"} or signed["protocol"] != SIGNED_ACK_PROTOCOL:
      _refuse("a7_signed_ack_invalid")
   try:
      record = validate_private_kv_checkpoint_ack(signed["ack"])
   except ValueError:
      _refuse("a7_signed_ack_invalid")
   if not isinstance(signed["signature"], dict) or not verify_signature(
      _canonical(_signed_statement(SIGNED_ACK_PROTOCOL, {"ack": record})), signed["signature"]
   ):
      _refuse("a7_signed_ack_signature_invalid")
   expected = validate_binding(binding)
   pairs = {
      "request_id": "request_key",
      "request_attempt": "request_attempt",
      "stage_id": "stage_id",
      "checkpoint_sequence": "checkpoint_sequence",
      "checkpoint_digest": "checkpoint_digest",
      "predecessor_digest": "predecessor_digest",
      "committed_token_count": "committed_token_count",
      "committed_prefix_digest": "committed_prefix_digest",
      "standby_placement_id": "standby_placement_id",
      "standby_membership_incarnation": "standby_membership_incarnation",
   }
   for ack_field, binding_field in pairs.items():
      if record[ack_field] != expected[binding_field]:
         _refuse("a7_signed_ack_binding_mismatch")
   return record
