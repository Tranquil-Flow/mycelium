# SPDX-License-Identifier: AGPL-3.0-only
"""A12 native Android operation qualification from runtime-owned observations.

The native core seals each ``mycelium.mobile_runtime_observation.v1`` inside a
member-signed envelope (``native/mobile_core/src/evidence.rs``
``sign_observation``); ``observe_mobile_runtime`` accepts only such an envelope,
signed by the exact enrolled member key, native, runtime-owned, fresh and for this
platform and build.  ``qualify_mobile_operation`` re-verifies every envelope and
decides the exact role from them plus the owner-private physical gate bundle.

Envelopes here are signed with the same Ed25519 evidence-signing format the native
core implements; the Rust side of the format has its own unit tests.
"""

from __future__ import annotations

import copy
import hashlib

import pytest

from mycelium_mobile import a12_activation as a12
from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_qualification.signing import generate_ed25519_signer

BUILD = "sha256:" + "b" * 64
FAMILY = "family:" + "a1" * 16
NOW = 1_800_000_000_000.0
STAGE_METRICS = {
   "thermal_max_celsius": 38.5,
   "battery_level_percent": 81.0,
   "charging": 1.0,
   "memory_available_bytes": 3.2e9,
   "lifecycle_member": 1.0,
   "relay_online": 1.0,
   "stage_loaded": 1.0,
   "frames_executed_window": 42.0,
}
CURRENT = {"signed_membership": "eligible", "capability_eligibility": "eligible"}


def _observation(generation, *, metrics=None, observed_at=NOW - 1_000.0, duration=120_000.0, **overrides):
   document = {
      "protocol": "mycelium.mobile_runtime_observation.v1",
      "platform_family": "android",
      "runtime_build_digest": BUILD,
      "profile_generation": 2,
      "evidence_generation": generation,
      "source_kind": "native_device_observation",
      "observed_at_unix_ms": observed_at,
      "measurement_duration_ms": duration,
      "sample_count": 3,
      "authority": "mycelium_node_app",
      "metrics": dict(STAGE_METRICS if metrics is None else metrics),
   }
   document.update(overrides)
   return document


def _envelope(signer, document):
   return {
      "protocol": a12.MEMBER_ENVELOPE_PROTOCOL,
      "message": document,
      "signature": signer.sign(document),
      "verification_key": signer.public_key_record(),
   }


@pytest.fixture()
def member():
   return generate_ed25519_signer(endpoint_id="member-endpoint-1")


def _digest(value):
   return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _gate(member, observations, *, roles=("qualified_model_stage",), **overrides):
   body = {
      "protocol": a12.ACTIVATION_QUALIFICATION_PROTOCOL,
      "platform_family": "android",
      "roles": list(roles),
      "member_key_digest": member.verification_key_digest,
      "runtime_build_digest": BUILD,
      "family_ref": FAMILY,
      "execution_path": "signed_native_application",
      "positives": {
         case: {"outcome": "passed"}
         for case in a12.REQUIRED_MOBILE_POSITIVES["qualified_model_stage"]
      },
      "negatives": {case: {"outcome": "failed_closed"} for case in a12.REQUIRED_MOBILE_NEGATIVES},
      "adb_used_during_gate": False,
      "provenance": "live",
      "observation_digests": [_digest(document) for document in observations],
   }
   body.update(overrides)
   return {**body, "gate_digest": _digest(body)}


def _qualify(member, envelopes, gate, *, role="qualified_model_stage", prior=None):
   return a12.qualify_mobile_operation(
      member_ref="member:1",
      role=role,
      platform_family="android",
      runtime_build_digest=BUILD,
      prior_rung_decisions=prior or {**CURRENT, "artifact_readiness": "eligible"},
      observation_envelopes=envelopes,
      physical_gate=gate,
      member_key_digest=member.verification_key_digest,
      family_ref=FAMILY,
      now_unix_ms=NOW,
   )


def _window(member, count=5):
   documents = [_observation(generation) for generation in range(1, count + 1)]
   return documents, [_envelope(member, document) for document in documents]


def test_a_sealed_native_observation_is_accepted_and_digested(member):
   document = _observation(1)
   record = a12.observe_mobile_runtime(
      member_ref="member:1",
      platform_family="android",
      runtime_build_digest=BUILD,
      observation_envelope=_envelope(member, document),
      expected_member_key_digest=member.verification_key_digest,
      now_unix_ms=NOW,
   )
   assert record["observation"] == document
   assert record["observation_digest"] == _digest(document)
   assert record["member_key_digest"] == member.verification_key_digest


def test_a_tampered_observation_is_refused(member):
   envelope = _envelope(member, _observation(1))
   envelope["message"]["sample_count"] = 9
   with pytest.raises(a12.A12ContractError) as refused:
      a12.observe_mobile_runtime(
         member_ref="member:1",
         platform_family="android",
         runtime_build_digest=BUILD,
         observation_envelope=envelope,
         expected_member_key_digest=member.verification_key_digest,
         now_unix_ms=NOW,
      )
   assert refused.value.code == "member_profile_signature_invalid"


@pytest.mark.parametrize(
   ("document", "expected_key", "code"),
   [
      (_observation(1, source_kind="synthetic_conformance"), None, "non_native_source_cannot_qualify"),
      (_observation(1, authority="operator_console"), None, "mobile_observation_not_runtime_owned"),
      (_observation(1, runtime_build_digest="sha256:" + "c" * 64), None, "mobile_observation_build_mismatch"),
      (_observation(1, observed_at=NOW - 30 * 3_600_000.0), None, "mobile_observation_stale"),
      (_observation(1, observed_at=NOW + 3_600_000.0), None, "mobile_observation_time_invalid"),
      (_observation(1), "sha256:" + "9" * 64, "mobile_observation_signer_not_member"),
   ],
)
def test_non_native_foreign_stale_or_unbound_observations_fail_closed(member, document, expected_key, code):
   with pytest.raises(a12.A12ContractError) as refused:
      a12.observe_mobile_runtime(
         member_ref="member:1",
         platform_family="android",
         runtime_build_digest=BUILD,
         observation_envelope=_envelope(member, document),
         expected_member_key_digest=expected_key or member.verification_key_digest,
         now_unix_ms=NOW,
      )
   assert refused.value.code == code


def test_apple_mobile_is_deferred_and_the_absent_seams_still_refuse(member):
   with pytest.raises(a12.A12ContractError) as deferred:
      a12.observe_mobile_runtime(
         member_ref="member:1",
         platform_family="ios",
         runtime_build_digest=BUILD,
         observation_envelope=_envelope(member, _observation(1, platform_family="ios")),
         expected_member_key_digest=member.verification_key_digest,
         now_unix_ms=NOW,
      )
   assert deferred.value.code == a12.APPLE_DEFERRED_CODE
   with pytest.raises(a12.A12NotImplementedError):
      a12.observe_mobile_runtime(member_ref="member:1", platform_family="android", runtime_build_digest=BUILD)
   with pytest.raises(a12.A12NotImplementedError):
      a12.qualify_mobile_operation(
         member_ref="member:1",
         role="qualified_model_stage",
         platform_family="android",
         runtime_build_digest=BUILD,
         prior_rung_decisions={**CURRENT, "artifact_readiness": "eligible"},
      )


def test_live_native_evidence_qualifies_exactly_one_role(member):
   documents, envelopes = _window(member)
   record = _qualify(member, envelopes, _gate(member, documents))

   assert record["decision"] == "eligible", record["blockers"]
   assert record["protocol"] == a12.ROLE_QUALIFICATION_PROTOCOL
   assert record["role"] == "qualified_model_stage"
   assert record["claim_scope"] == "family_bound"
   assert record["route_ready"] is False
   assert record["grants"] == [a12.RUNG_GRANTS["operation_qualification"]]
   assert "another_role_qualification" in record["cannot_grant"]
   assert record["observed_window_ms"] == 600_000.0


def test_an_artifact_free_probe_qualifies_without_stage_families(member):
   metrics = {key: value for key, value in STAGE_METRICS.items() if key not in ("stage_loaded", "frames_executed_window")}
   documents = [_observation(generation, metrics=metrics) for generation in range(1, 6)]
   envelopes = [_envelope(member, document) for document in documents]
   gate = _gate(
      member,
      documents,
      roles=("probe_contributor",),
      positives={case: {"outcome": "passed"} for case in a12.REQUIRED_MOBILE_POSITIVES["probe_contributor"]},
   )
   record = _qualify(
      member,
      envelopes,
      gate,
      role="probe_contributor",
      prior={**CURRENT, "artifact_readiness": "not_applicable"},
   )
   assert record["decision"] == "eligible", record["blockers"]


def _without(mapping, key):
   return {name: value for name, value in mapping.items() if name != key}


@pytest.mark.parametrize(
   ("case", "blocker"),
   [
      ("unknown_thermal", "gate_family_unknown:thermal:thermal_max_celsius"),
      ("short_window", "sustained_observation_window_not_covered"),
      ("replayed_generation", "observation_generations_not_monotonic"),
      ("missing_negative", "physical_negative_not_passed:process_death"),
      ("violated_negative", "physical_negative_not_passed:revocation"),
      ("missing_positive", "physical_positive_not_passed:native_parity"),
      ("adb", "adb_used_during_gate"),
      ("fixture", "non_live_provenance_cannot_qualify"),
      ("unbound_observation", "observation_not_bound_to_physical_gate"),
      ("other_family", "physical_gate_bound_to_another_family"),
      ("stale_membership", "signed_membership_not_current"),
      ("no_artifact", "artifact_readiness_not_current"),
   ],
)
def test_every_gap_withholds_the_operation(member, case, blocker):
   documents, envelopes = _window(member)
   gate_overrides = {}
   prior = {**CURRENT, "artifact_readiness": "eligible"}
   if case == "unknown_thermal":
      documents[2] = _observation(3, metrics={**STAGE_METRICS, "thermal_max_celsius": "unknown"})
      envelopes[2] = _envelope(member, documents[2])
   elif case == "short_window":
      documents, envelopes = _window(member, count=2)
   elif case == "replayed_generation":
      documents[3] = _observation(2)
      envelopes[3] = _envelope(member, documents[3])
   elif case == "missing_negative":
      gate_overrides["negatives"] = {
         name: {"outcome": "failed_closed"} for name in a12.REQUIRED_MOBILE_NEGATIVES if name != "process_death"
      }
   elif case == "violated_negative":
      gate_overrides["negatives"] = {
         name: {"outcome": "violated" if name == "revocation" else "failed_closed"}
         for name in a12.REQUIRED_MOBILE_NEGATIVES
      }
   elif case == "missing_positive":
      gate_overrides["positives"] = {
         name: {"outcome": "not_executed" if name == "native_parity" else "passed"}
         for name in a12.REQUIRED_MOBILE_POSITIVES["qualified_model_stage"]
      }
   elif case == "adb":
      gate_overrides["adb_used_during_gate"] = True
   elif case == "fixture":
      gate_overrides["provenance"] = "fixture"
   elif case == "unbound_observation":
      gate_overrides["observation_digests"] = [_digest(documents[0])]
   elif case == "other_family":
      gate_overrides["family_ref"] = "family:" + "b2" * 16
   elif case == "stale_membership":
      prior["signed_membership"] = "stale"
   elif case == "no_artifact":
      prior["artifact_readiness"] = "withheld"
   gate = _gate(member, documents, **gate_overrides)
   try:
      record = _qualify(member, envelopes, gate, prior=prior)
   except a12.A12ContractError as refused:
      # A stale or withheld prior rung is refused by the rung-isolation guard
      # before any evidence is read: an equally fail-closed outcome.
      assert case in {"stale_membership", "no_artifact"}
      assert refused.code == "prior_rung_not_current"
      return
   assert record["decision"] == "withheld"
   assert blocker in record["blockers"]
   assert record["grants"] == []


def test_a_tampered_physical_gate_bundle_is_refused(member):
   documents, envelopes = _window(member)
   gate = _gate(member, documents)
   forged = copy.deepcopy(gate)
   forged["negatives"]["process_death"] = {"outcome": "failed_closed", "note": "edited"}
   forged["adb_used_during_gate"] = False
   forged["provenance"] = "live"
   forged["positives"]["native_parity"] = {"outcome": "passed"}
   forged["roles"] = ["qualified_model_stage", "probe_contributor"]
   with pytest.raises(a12.A12ContractError) as refused:
      _qualify(member, envelopes, forged)
   assert refused.value.code == "mobile_physical_gate_digest_mismatch"


def test_the_draft_role_still_needs_a_closed_a11_binding(member):
   documents, envelopes = _window(member)
   with pytest.raises(a12.A12NotImplementedError) as refused:
      _qualify(member, envelopes, _gate(member, documents, roles=("speculative_draft_worker",)), role="speculative_draft_worker")
   assert refused.value.code == "not_implemented:draft_worker_qualification_absent"
