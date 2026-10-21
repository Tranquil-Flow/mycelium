# SPDX-License-Identifier: AGPL-3.0-only
"""A7 standby placement qualification (spec sections 3 and 4).

A STANDBY placement is recovery capacity for exactly one source placement's
stage.  Membership, artifact presence, a load or a received shadow checkpoint
never make it qualified.  Only this qualifier decides, from:

* the exact stage KV identity each runtime port reports for its own bound
  placement (model and immutable revision, manifest, deployment and epoch,
  half-open layer range, component roles, backend/build/device/dtype/
  quantization/architecture and decode mode) -- any difference or unknown
  field is incompatible, never assumed;
* both placements' graph records (stage, assignment-local load proofs, stage
  signature and backend);
* an executed next-position parity challenge: the source and the standby each
  decode the same next position from the same exact KV and their output bytes
  must be identical; and
* the standby's declared checkpoint/KV reservation against its measured free
  memory.

The record is closed, digest-bound and privacy-reduced (identities are
digested; mismatches are reported by field name only).  ``qualified`` is
``True`` only when every rung passes; a record is current only before its
expiry.  It feeds the cutover predicate's ``qualifier_current`` input.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

from mycelium_router.kv_checkpoint import stage_kv_identity

STANDBY_QUALIFICATION_PROTOCOL = "mycelium.a7_standby_qualification.v1"
PARITY_CHALLENGE_PROTOCOL = "mycelium.a7_standby_parity_challenge.v1"
MAX_QUALIFICATION_TTL_MS = 24 * 60 * 60 * 1000

_IDENTITY_FIELDS = (
   "model_id",
   "resolved_commit",
   "manifest_digest",
   "deployment_id",
   "deployment_epoch",
   "layer_range",
   "component_roles",
   "decode_mode",
   "runtime.backend",
   "runtime.backend_version",
   "runtime.device",
   "runtime.dtype",
   "runtime.quantization",
   "runtime.architecture",
   "placement.stage_id",
   "placement.stage_signature",
   "placement.runtime_backend",
)

_RECORD_FIELDS = frozenset(
   {
      "protocol",
      "qualification_id",
      "stage_id",
      "source_placement_id",
      "standby_placement_id",
      "standby_node_id",
      "source_identity_digest",
      "standby_identity_digest",
      "source_load_proof_digest",
      "standby_load_proof_digest",
      "compatible",
      "mismatched_fields",
      "unknown_fields",
      "parity_challenge",
      "reserved_kv_bytes",
      "available_bytes",
      "resource_ok",
      "issued_at_unix_ms",
      "expires_at_unix_ms",
      "qualified",
      "reason",
      "evidence_digest",
   }
)
_CHALLENGE_FIELDS = frozenset(
   {
      "protocol",
      "challenge_id",
      "next_position",
      "source_output_digest",
      "standby_output_digest",
      "executed",
      "passed",
   }
)
REASONS = (
   "qualified",
   "identity_incompatible",
   "parity_challenge_missing",
   "parity_challenge_failed",
   "resource_insufficient",
   "load_proof_missing",
)


class StandbyQualificationError(ValueError):
   def __init__(self, code: str) -> None:
      super().__init__(code)
      self.code = code


def _canonical(value: Any) -> bytes:
   return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(value: Any) -> str:
   data = value if isinstance(value, bytes) else _canonical(value)
   return "sha256:" + hashlib.sha256(data).hexdigest()


_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


def _flatten(identity: Mapping[str, Any], placement: Any, stage_id: str) -> dict[str, Any]:
   runtime = identity.get("runtime_identity") or {}
   flat: dict[str, Any] = {
      field: identity.get(field)
      for field in _IDENTITY_FIELDS
      if not field.startswith(("runtime.", "placement."))
   }
   for field in ("backend", "backend_version", "device", "dtype", "quantization", "architecture"):
      flat[f"runtime.{field}"] = runtime.get(field) or None
   flat["placement.stage_id"] = stage_id
   flat["placement.stage_signature"] = getattr(placement, "stage_signature", None) or None
   flat["placement.runtime_backend"] = getattr(placement, "runtime_backend", None) or None
   return flat


def port_placement_identity(port: Any, placement_id: str) -> dict[str, Any]:
   """The exact, flattened KV identity a runtime port reports for one bound placement."""

   binding = port._bound.get(placement_id)
   if binding is None:
      raise StandbyQualificationError("standby_placement_unbound")
   stage, placement, _loaded, _runtime = binding
   identity = stage_kv_identity(port, placement_id)
   flat = _flatten(identity, placement, stage.stage_id)
   flat["load_proof_digest"] = placement.load_proof_digest or None
   flat["placement_id"] = placement.placement_id
   flat["node_id"] = placement.node_id
   flat["lifecycle_state"] = placement.lifecycle_state
   return flat


def parity_challenge_result(
   *,
   challenge_id: str,
   next_position: int,
   source_output: bytes,
   standby_output: bytes,
) -> dict[str, Any]:
   """Digest one executed next-position challenge (raw outputs never leave here)."""

   if not isinstance(challenge_id, str) or not challenge_id:
      raise StandbyQualificationError("parity_challenge_invalid")
   if type(next_position) is not int or next_position < 1:
      raise StandbyQualificationError("parity_challenge_invalid")
   if not isinstance(source_output, bytes) or not isinstance(standby_output, bytes):
      raise StandbyQualificationError("parity_challenge_invalid")
   source_digest = _sha(source_output)
   standby_digest = _sha(standby_output)
   return {
      "protocol": PARITY_CHALLENGE_PROTOCOL,
      "challenge_id": _sha(challenge_id.encode("utf-8")),
      "next_position": next_position,
      "source_output_digest": source_digest,
      "standby_output_digest": standby_digest,
      "executed": True,
      "passed": source_digest == standby_digest,
   }


_CROSS_HOST_FIELDS = frozenset(
   {
      "protocol",
      "challenge_id",
      "source_identity_digest",
      "standby_identity_digest",
      "source_output_digest",
      "standby_output_digest",
      "candidate_commit",
      "measured_at_unix_ms",
      "evidence_digest",
   }
)


def physical_parity_challenge(
   *,
   challenge_id: str,
   source_identity: Mapping[str, Any],
   standby_identity: Mapping[str, Any],
   source_output_digest: str,
   standby_output_digest: str,
   candidate_commit: str,
   measured_at_unix_ms: int,
) -> dict[str, Any]:
   """Closed cross-host parity record from one measured source/standby pair.

   Refuses (fail closed) when the two placement identities differ in any
   KV identity field, when either output digest is malformed, or when the
   outputs differ: a failing parity is never sealed as a challenge.  The
   record is digest-bound so it cannot be re-issued against another
   qualification.
   """

   if not isinstance(challenge_id, str) or not challenge_id:
      raise StandbyQualificationError("physical_parity_invalid")
   if not isinstance(source_identity, Mapping) or not isinstance(standby_identity, Mapping):
      raise StandbyQualificationError("physical_parity_identity_invalid")
   if not isinstance(source_output_digest, str) or not _DIGEST_RE.fullmatch(source_output_digest):
      raise StandbyQualificationError("physical_parity_output_digest_invalid")
   if not isinstance(standby_output_digest, str) or not _DIGEST_RE.fullmatch(standby_output_digest):
      raise StandbyQualificationError("physical_parity_output_digest_invalid")
   if source_output_digest != standby_output_digest:
      raise StandbyQualificationError("physical_parity_outputs_differ")
   if not isinstance(candidate_commit, str) or not _COMMIT_RE.fullmatch(candidate_commit):
      raise StandbyQualificationError("physical_parity_commit_invalid")
   if type(measured_at_unix_ms) is not int or measured_at_unix_ms < 1:
      raise StandbyQualificationError("physical_parity_clock_invalid")
   left = {key: source_identity.get(key) for key in _IDENTITY_FIELDS}
   right = {key: standby_identity.get(key) for key in _IDENTITY_FIELDS}
   if left != right:
      mismatched = sorted(
         key for key in _IDENTITY_FIELDS if left.get(key) != right.get(key)
      )
      raise StandbyQualificationError("physical_parity_identity_mismatch")
   record = {
      "protocol": PARITY_CHALLENGE_PROTOCOL,
      "challenge_id": _sha(challenge_id.encode("utf-8")),
      "source_identity_digest": _sha(left),
      "standby_identity_digest": _sha(right),
      "source_output_digest": source_output_digest,
      "standby_output_digest": standby_output_digest,
      "candidate_commit": candidate_commit,
      "measured_at_unix_ms": measured_at_unix_ms,
   }
   record["evidence_digest"] = _sha(record)
   return record


def validate_physical_parity_challenge(document: Any) -> dict[str, Any]:
   """Closed-shape, digest-bound validation of a cross-host parity record."""

   if not isinstance(document, Mapping) or set(document) != _CROSS_HOST_FIELDS:
      raise StandbyQualificationError("physical_parity_invalid")
   record = dict(document)
   if record["protocol"] != PARITY_CHALLENGE_PROTOCOL:
      raise StandbyQualificationError("physical_parity_invalid")
   body = {key: value for key, value in record.items() if key != "evidence_digest"}
   if record["evidence_digest"] != _sha(body):
      raise StandbyQualificationError("physical_parity_digest_mismatch")
   if record["source_output_digest"] != record["standby_output_digest"]:
      raise StandbyQualificationError("physical_parity_outputs_differ")
   if record["source_identity_digest"] != record["standby_identity_digest"]:
      raise StandbyQualificationError("physical_parity_identity_mismatch")
   return record


def qualify_standby(
   *,
   source_identity: Mapping[str, Any],
   standby_identity: Mapping[str, Any],
   parity_challenge: Mapping[str, Any] | None,
   reserved_kv_bytes: int,
   available_bytes: int | None,
   issued_at_unix_ms: int,
   ttl_ms: int,
) -> dict[str, Any]:
   """Decide one standby's recovery readiness for one source placement."""

   if type(reserved_kv_bytes) is not int or reserved_kv_bytes < 1:
      raise StandbyQualificationError("standby_reservation_invalid")
   if available_bytes is not None and (type(available_bytes) is not int or available_bytes < 0):
      raise StandbyQualificationError("standby_reservation_invalid")
   if type(issued_at_unix_ms) is not int or issued_at_unix_ms < 1:
      raise StandbyQualificationError("standby_qualification_clock_invalid")
   if type(ttl_ms) is not int or not 1 <= ttl_ms <= MAX_QUALIFICATION_TTL_MS:
      raise StandbyQualificationError("standby_qualification_ttl_invalid")
   if standby_identity.get("lifecycle_state") != "STANDBY":
      raise StandbyQualificationError("standby_placement_not_standby")
   if source_identity.get("lifecycle_state") != "ACTIVE":
      raise StandbyQualificationError("source_placement_not_active")
   if standby_identity.get("placement_id") == source_identity.get("placement_id"):
      raise StandbyQualificationError("standby_is_source")

   mismatched: list[str] = []
   unknown: list[str] = []
   for field in _IDENTITY_FIELDS:
      left = source_identity.get(field)
      right = standby_identity.get(field)
      if left is None or right is None:
         unknown.append(field)
      elif left != right:
         mismatched.append(field)
   compatible = not mismatched and not unknown
   challenge = None if parity_challenge is None else _validate_challenge(parity_challenge)
   resource_ok = available_bytes is not None and reserved_kv_bytes <= available_bytes
   proofs = bool(source_identity.get("load_proof_digest")) and bool(standby_identity.get("load_proof_digest"))
   if not proofs:
      reason = "load_proof_missing"
   elif not compatible:
      reason = "identity_incompatible"
   elif challenge is None:
      reason = "parity_challenge_missing"
   elif not challenge["passed"]:
      reason = "parity_challenge_failed"
   elif not resource_ok:
      reason = "resource_insufficient"
   else:
      reason = "qualified"
   identity_core = {key: source_identity.get(key) for key in _IDENTITY_FIELDS}
   standby_core = {key: standby_identity.get(key) for key in _IDENTITY_FIELDS}
   record = {
      "protocol": STANDBY_QUALIFICATION_PROTOCOL,
      "stage_id": str(standby_identity.get("placement.stage_id") or ""),
      "source_placement_id": _sha(str(source_identity.get("placement_id")).encode("utf-8")),
      "standby_placement_id": _sha(str(standby_identity.get("placement_id")).encode("utf-8")),
      "standby_node_id": _sha(str(standby_identity.get("node_id")).encode("utf-8")),
      "source_identity_digest": _sha(identity_core),
      "standby_identity_digest": _sha(standby_core),
      "source_load_proof_digest": _sha(str(source_identity.get("load_proof_digest")).encode("utf-8")),
      "standby_load_proof_digest": _sha(str(standby_identity.get("load_proof_digest")).encode("utf-8")),
      "compatible": compatible,
      "mismatched_fields": sorted(mismatched),
      "unknown_fields": sorted(unknown),
      "parity_challenge": challenge,
      "reserved_kv_bytes": reserved_kv_bytes,
      "available_bytes": available_bytes,
      "resource_ok": resource_ok,
      "issued_at_unix_ms": issued_at_unix_ms,
      "expires_at_unix_ms": issued_at_unix_ms + ttl_ms,
      "qualified": reason == "qualified",
      "reason": reason,
   }
   record["qualification_id"] = _sha({"kind": "qualification", **record})
   record["evidence_digest"] = _sha(record)
   return validate_standby_qualification(record)


def _validate_challenge(challenge: Mapping[str, Any]) -> dict[str, Any]:
   if not isinstance(challenge, Mapping) or set(challenge) != _CHALLENGE_FIELDS:
      raise StandbyQualificationError("parity_challenge_invalid")
   result = dict(challenge)
   if result["protocol"] != PARITY_CHALLENGE_PROTOCOL or result["executed"] is not True:
      raise StandbyQualificationError("parity_challenge_invalid")
   if type(result["passed"]) is not bool or result["passed"] != (
      result["source_output_digest"] == result["standby_output_digest"]
   ):
      raise StandbyQualificationError("parity_challenge_invalid")
   if type(result["next_position"]) is not int or result["next_position"] < 1:
      raise StandbyQualificationError("parity_challenge_invalid")
   return result


def validate_standby_qualification(document: Mapping[str, Any]) -> dict[str, Any]:
   """Closed-shape, digest-bound validation; inconsistent claims fail closed."""

   if not isinstance(document, Mapping) or set(document) != _RECORD_FIELDS:
      raise StandbyQualificationError("standby_qualification_invalid")
   record = dict(document)
   if record["protocol"] != STANDBY_QUALIFICATION_PROTOCOL or record["reason"] not in REASONS:
      raise StandbyQualificationError("standby_qualification_invalid")
   body = {key: value for key, value in record.items() if key != "evidence_digest"}
   if record["evidence_digest"] != _sha(body):
      raise StandbyQualificationError("standby_qualification_digest_mismatch")
   if record["parity_challenge"] is not None:
      _validate_challenge(record["parity_challenge"])
   if record["expires_at_unix_ms"] <= record["issued_at_unix_ms"]:
      raise StandbyQualificationError("standby_qualification_invalid")
   earned = (
      record["compatible"] is True
      and not record["mismatched_fields"]
      and not record["unknown_fields"]
      and record["parity_challenge"] is not None
      and record["parity_challenge"]["passed"] is True
      and record["resource_ok"] is True
   )
   if record["qualified"] is not (earned and record["reason"] == "qualified"):
      raise StandbyQualificationError("standby_qualification_unearned")
   return record


def standby_qualification_is_current(document: Mapping[str, Any], *, now_unix_ms: int) -> bool:
   """True only for a valid, earned, unexpired qualification."""

   try:
      record = validate_standby_qualification(document)
   except StandbyQualificationError:
      return False
   return record["qualified"] is True and record["issued_at_unix_ms"] <= now_unix_ms < record["expires_at_unix_ms"]
