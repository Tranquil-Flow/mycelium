# SPDX-License-Identifier: AGPL-3.0-only
"""A7: sealed shadow-checkpoint channel and signed standby acknowledgements."""

from __future__ import annotations

import hashlib

import pytest

import mycelium_a7_kv_successor as a7
from mycelium_a7_checkpoint_channel import (
   CheckpointChannelError,
   StandbyCheckpointKey,
   open_checkpoint,
   payload_digest,
   seal_checkpoint,
   sign_checkpoint_ack,
   verify_signed_checkpoint_ack,
   verify_standby_key_record,
)
from mycelium_qualification.signing import build_ed25519_verifier, generate_ed25519_signer

_NOW = 1_800_000_000_000
_MAX = 1 << 20


def _digest(seed: str) -> str:
   return "sha256:" + hashlib.sha256(seed.encode()).hexdigest()


PLACEMENT = _digest("placement:standby")
INCARNATION = _digest("incarnation:standby:1")
PAYLOAD = b"exact-kv-bytes" * 64


def _binding(**overrides):
   binding = {
      "request_key": _digest("request"),
      "request_attempt": 1,
      "stage_id": _digest("stage:1"),
      "checkpoint_sequence": 0,
      "predecessor_digest": None,
      "committed_token_count": 5,
      "committed_prefix_digest": _digest("prefix:5"),
      "kv_position": 5,
      "checkpoint_digest": payload_digest(PAYLOAD),
      "byte_size": len(PAYLOAD),
      "standby_placement_id": PLACEMENT,
      "standby_membership_incarnation": INCARNATION,
      "source_placement_id": _digest("placement:source"),
      "expires_at_unix_ms": _NOW + 60_000,
   }
   binding.update(overrides)
   return binding


@pytest.fixture()
def standby():
   signer = generate_ed25519_signer(endpoint_id="standby-endpoint")
   key = StandbyCheckpointKey(signer=signer, standby_placement_id=PLACEMENT, standby_membership_incarnation=INCARNATION)
   verifier = build_ed25519_verifier([signer.public_key_record()])
   public = verify_standby_key_record(key.public_record(), verify_signature=verifier, standby_placement_id=PLACEMENT, standby_membership_incarnation=INCARNATION)
   return signer, key, verifier, public


def _code(callable_, *args, **kwargs):
   with pytest.raises(CheckpointChannelError) as refused:
      callable_(*args, **kwargs)
   return refused.value.code


def test_sealed_checkpoint_round_trips_only_for_its_standby(standby):
   _signer, key, _verifier, public = standby
   sealed = seal_checkpoint(PAYLOAD, binding=_binding(), standby_public_key=public, max_bytes=_MAX)
   assert PAYLOAD not in sealed
   binding, payload = open_checkpoint(sealed, standby_key=key, max_bytes=_MAX, now_unix_ms=_NOW)
   assert payload == PAYLOAD and binding == _binding()

   other_signer = generate_ed25519_signer(endpoint_id="other")
   other = StandbyCheckpointKey(signer=other_signer, standby_placement_id=PLACEMENT, standby_membership_incarnation=INCARNATION)
   assert _code(open_checkpoint, sealed, standby_key=other, max_bytes=_MAX, now_unix_ms=_NOW) == "a7_sealed_checkpoint_authentication_failed"
   restarted = StandbyCheckpointKey(signer=other_signer, standby_placement_id=PLACEMENT, standby_membership_incarnation=_digest("incarnation:standby:2"))
   assert _code(open_checkpoint, sealed, standby_key=restarted, max_bytes=_MAX, now_unix_ms=_NOW) == "a7_sealed_checkpoint_stale_incarnation"
   assert _code(open_checkpoint, sealed, standby_key=key, max_bytes=_MAX, now_unix_ms=_NOW + 60_000) == "a7_sealed_checkpoint_expired"


def test_tampering_with_binding_or_ciphertext_fails_authentication(standby):
   _signer, key, _verifier, public = standby
   sealed = seal_checkpoint(PAYLOAD, binding=_binding(), standby_public_key=public, max_bytes=_MAX)
   flipped = bytearray(sealed)
   flipped[-1] ^= 1
   assert _code(open_checkpoint, bytes(flipped), standby_key=key, max_bytes=_MAX, now_unix_ms=_NOW) == "a7_sealed_checkpoint_authentication_failed"
   rebound = sealed.replace(b'"committed_token_count":5', b'"committed_token_count":6')
   assert _code(open_checkpoint, rebound, standby_key=key, max_bytes=_MAX, now_unix_ms=_NOW) == "a7_sealed_checkpoint_authentication_failed"


def test_seal_refuses_a_binding_that_does_not_match_the_payload(standby):
   _signer, _key, _verifier, public = standby
   assert _code(seal_checkpoint, PAYLOAD + b"x", binding=_binding(), standby_public_key=public, max_bytes=_MAX) == "a7_checkpoint_binding_payload_mismatch"
   assert _code(seal_checkpoint, PAYLOAD, binding=_binding(checkpoint_sequence=1), standby_public_key=public, max_bytes=_MAX) == "a7_checkpoint_binding_invalid"
   assert _code(seal_checkpoint, PAYLOAD, binding=_binding(), standby_public_key=public, max_bytes=len(PAYLOAD) - 1) == "a7_checkpoint_payload_too_large"


def test_standby_key_record_must_be_signed_by_the_pinned_standby(standby):
   _signer, key, _verifier, _public = standby
   impostor = generate_ed25519_signer(endpoint_id="impostor")
   impostor_verifier = build_ed25519_verifier([impostor.public_key_record()])
   assert _code(verify_standby_key_record, key.public_record(), verify_signature=impostor_verifier, standby_placement_id=PLACEMENT, standby_membership_incarnation=INCARNATION) == "a7_standby_key_signature_invalid"
   swapped = {**key.public_record(), "x25519_public_key": StandbyCheckpointKey(signer=impostor, standby_placement_id=PLACEMENT, standby_membership_incarnation=INCARNATION).public_record()["x25519_public_key"]}
   assert _code(verify_standby_key_record, swapped, verify_signature=standby[2], standby_placement_id=PLACEMENT, standby_membership_incarnation=INCARNATION) == "a7_standby_key_signature_invalid"
   assert _code(verify_standby_key_record, key.public_record(), verify_signature=standby[2], standby_placement_id=_digest("other"), standby_membership_incarnation=INCARNATION) == "a7_standby_key_placement_mismatch"


def _ack_for(binding, **overrides):
   document = {
      "protocol": a7.CHECKPOINT_ACK_PROTOCOL,
      "ack_id": _digest("ack:0"),
      "request_id": binding["request_key"],
      "session_id": _digest("session"),
      "request_attempt": binding["request_attempt"],
      "stage_id": binding["stage_id"],
      "standby_placement_id": binding["standby_placement_id"],
      "standby_membership_incarnation": binding["standby_membership_incarnation"],
      "checkpoint_sequence": binding["checkpoint_sequence"],
      "checkpoint_digest": binding["checkpoint_digest"],
      "predecessor_digest": binding["predecessor_digest"],
      "committed_token_count": binding["committed_token_count"],
      "committed_prefix_digest": binding["committed_prefix_digest"],
      "stored_checkpoint_digest_verified": True,
      "stored_checkpoint_digest": binding["checkpoint_digest"],
      "resource_reservation_generation": 1,
      "acknowledgement_authority": "standby_runtime",
      "ack_authority_generation": 1,
   }
   document.update(overrides)
   document["evidence_digest"] = a7.binding_digest(document)
   return document


def test_signed_ack_verifies_only_for_its_signer_and_binding(standby):
   signer, _key, verifier, _public = standby
   binding = _binding()
   signed = sign_checkpoint_ack(signer, _ack_for(binding))
   record = verify_signed_checkpoint_ack(signed, verify_signature=verifier, binding=binding)
   assert record["checkpoint_digest"] == binding["checkpoint_digest"]

   forger = generate_ed25519_signer(endpoint_id="forger")
   forged = sign_checkpoint_ack(forger, _ack_for(binding))
   assert _code(verify_signed_checkpoint_ack, forged, verify_signature=verifier, binding=binding) == "a7_signed_ack_signature_invalid"
   other_count = sign_checkpoint_ack(signer, _ack_for(binding, committed_token_count=4))
   assert _code(verify_signed_checkpoint_ack, other_count, verify_signature=verifier, binding=binding) == "a7_signed_ack_binding_mismatch"
   stale = sign_checkpoint_ack(signer, _ack_for(binding, standby_membership_incarnation=_digest("incarnation:old")))
   assert _code(verify_signed_checkpoint_ack, stale, verify_signature=verifier, binding=binding) == "a7_signed_ack_binding_mismatch"
   tampered = {**signed, "ack": {**signed["ack"], "ack_authority_generation": 2}}
   assert _code(verify_signed_checkpoint_ack, tampered, verify_signature=verifier, binding=binding) == "a7_signed_ack_invalid"
