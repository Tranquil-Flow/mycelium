# SPDX-License-Identifier: AGPL-3.0-only
"""A10 machine-source boundary suite.

GREEN with the scaffold; collection error without it (the modules are new).  It is NOT
evidence that any A10 behaviour works.  It asserts exactly three things:

1. the gate reports itself as ``design_only`` and claims none of the thirteen closure
   requirements of ``scripts/mycelium_completion_audit.py``;
2. the entry points that are still absent refuse with an explicit coded exception and
   never return a value -- the honest not-implemented boundary.  The four deterministic
   execution entry points (``execute_observed_batch``, ``observe_pipeline_overlap``,
   ``verify_candidate_positions``, ``sequential_parity_reference``) are implemented in
   ``mycelium_router.a10_runtime_execution`` and ``mycelium_router.a10_verifier_execution``
   and are therefore no longer listed as boundaries; their behaviour is covered by
   ``tests/a10_deterministic/test_runtime_batch_execution.py`` and
   ``tests/a10_deterministic/test_target_verification_execution.py``;
3. shape validation is real, coded, and distinguishable from the refusal, and an
   unsupported generation policy is rejected at admission rather than silently
   coerced to greedy.

Cross-artifact freshness is asserted, not assumed: the declared contract protocols must
equal the six protocols named by the A10 specification, and the inventory constants must
equal the frozen ``tests/a10_acceptance/scenarios.v1.json`` values.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
from pathlib import Path

import pytest

from mycelium_router import a10_runtime_batch as rrb
from mycelium_router import a10_target_verification as atv


ROOT = Path(__file__).resolve().parents[2]
INVENTORY = ROOT / "tests" / "a10_acceptance" / "scenarios.v1.json"
SPECIFICATION = (
   ROOT
   / "docs"
   / "superpowers"
   / "specs"
   / "2026-08-18-mycelium-a10-runtime-batching-overlap-target-verification.md"
)
AUDIT_SCRIPT = ROOT / "scripts" / "mycelium_completion_audit.py"
PROTOCOL_PATTERN = re.compile(r"`(mycelium\.[a-z0-9_]+\.[a-z0-9_]+)`")


def _load_audit_module():
   spec = importlib.util.spec_from_file_location(
      "a10_boundary_completion_audit",
      AUDIT_SCRIPT,
   )
   assert spec is not None and spec.loader is not None
   module = importlib.util.module_from_spec(spec)
   spec.loader.exec_module(module)
   return module


AUDIT = _load_audit_module()


class StubPort:
   """Minimal object satisfying both declared scaffold port protocols."""

   def execute(self, item):
      return None

   def execute_batch(self, batch):
      return ()

   def cancel(self, path_id):
      return None

   def close(self, *, reason="stub"):
      return None

   def kv_snapshot(self):
      return {}


DIGEST = "sha256:" + "a" * 64


def _key() -> "rrb.RuntimeBatchKey":
   from mycelium_router.contracts import RuntimeBatchKey

   return RuntimeBatchKey(
      deployment_id="deployment-1",
      deployment_epoch=7,
      model_commit="commit-abc",
      manifest_digest="manifest-abc",
      placement_id="placement-a",
      assignment_id="assignment-a",
      stage_signature="stage-signature-a",
      load_proof_digest="load-proof-a",
      runtime_backend="numpy",
      phase="DECODE",
      hidden_size=8,
      activation_bytes=4,
      token_span=1,
   )


def _proposal(**overrides):
   values = {
      "compatibility_key": _key(),
      "member_refs": ("member:one", "member:two"),
      "qos_classes": ("interactive", "batch"),
      "maximum_members": 8,
      "maximum_bytes": 1 << 20,
      "proposed_at": 0.0,
      "collection_deadline_at": 0.002,
   }
   values.update(overrides)
   return rrb.RuntimeBatchProposal(**values)


def _verification_request(**overrides):
   values = {
      "request_ref": "verification:one",
      "target_kv_ref": "target-kv:one",
      "runtime_incarnation": "incarnation:one",
      "committed_prefix_position": 16,
      "committed_prefix_digest": DIGEST,
      "candidate_position_count": 4,
      "candidate_bytes": 4096,
      "generation_policy": "deterministic_greedy",
      "transaction_mode": "rollback_to_prefix",
      "deadline_at": 1.0,
      "cancellation_generation": 0,
      "idempotency_digest": DIGEST,
   }
   values.update(overrides)
   return atv.TargetVerificationRequest(**values)


def _interval(**overrides):
   values = {
      "interval_ref": "interval:one",
      "request_ref": "request:one",
      "microbatch_ref": "microbatch:one",
      "stage_id": "stage-000",
      "placement_id": "placement-000",
      "started_at": 0.0,
      "finished_at": 1.0,
      "clock_uncertainty": 0.01,
   }
   values.update(overrides)
   return rrb.StageComputeInterval(**values)


def _trace(**overrides):
   values = {
      "trace_ref": "trace:one",
      "intervals": (_interval(),),
      "causal_edges": (),
   }
   values.update(overrides)
   return rrb.PipelineOverlapTrace(**values)


BOUNDARY_CALLS = {
   "qualify_runtime_batch": lambda: rrb.qualify_runtime_batch(
      rrb.QualificationRequest(
         owner_private_root="private:root",
         benchmark_manifest_digest=DIGEST,
         acceptance_inventory_digest=DIGEST,
         baseline_windows=16,
         candidate_windows=16,
         valid_pairs=12,
      )
   ),
   "qualify_target_verifier": lambda: atv.qualify_target_verifier(
      atv.TargetVerificationQualificationRequest(
         owner_private_root="private:root",
         runtime_digest=DIGEST,
         width_classes=atv.QUALIFIED_WIDTH_CLASSES,
         prefix_length_buckets=2,
         operations=atv.MINIMUM_QUALIFIED_OPERATIONS,
         sequential_parity_proof_digest=DIGEST,
      )
   ),
}

BOUNDARIES = rrb.scaffold_status().boundaries


# ---------------------------------------------------------------------------
# 1. The scaffold's own honest account of itself
# ---------------------------------------------------------------------------


def test_scaffold_state_and_claim_boundary_are_honest():
   status = rrb.scaffold_status()
   assert status.gate_id == "A10"
   assert status.protocol == "mycelium.a10_scaffold.v1"
   assert status.state == "design_only"
   assert status.state in AUDIT.STATES
   assert "no closure requirement earned" in status.claim_boundary


def test_scaffold_claims_none_of_the_thirteen_closure_requirements():
   status = rrb.scaffold_status()
   canonical = set(AUDIT.REQUIREMENTS)
   assert len(canonical) == 13
   assert status.earned_closure_requirements == frozenset()
   assert not (status.earned_closure_requirements & canonical)
   assert rrb.EARNED_CLOSURE_REQUIREMENTS == frozenset()


def test_declared_contract_protocols_match_the_specification():
   declared = set(rrb.DECLARED_CONTRACT_PROTOCOLS)
   in_spec = set(PROTOCOL_PATTERN.findall(SPECIFICATION.read_text(encoding="utf-8")))
   assert len(in_spec) == 6
   assert declared == in_spec


def test_inventory_constants_and_digest_bind_the_frozen_inventory():
   inventory_bytes = INVENTORY.read_bytes()
   inventory = json.loads(inventory_bytes)
   digest = "sha256:" + hashlib.sha256(inventory_bytes).hexdigest()
   assert rrb.ACCEPTANCE_INVENTORY_DIGEST == digest
   assert rrb.ACCEPTANCE_INVENTORY == "tests/a10_acceptance/scenarios.v1.json"

   requirements = inventory["runtime_batch_requirements"]
   assert (
      rrb.MEMBERSHIP_AUTHORITY_STAGE_RUNTIME
      == requirements["final_membership_authority"]
   )
   assert (
      rrb.MINIMUM_PHYSICAL_BATCH_MEMBERS
      == requirements["minimum_physical_batch_members"]
   )
   assert (
      rrb.BACKEND_INVOCATIONS_PER_OBSERVED_BATCH
      == requirements["backend_invocations_per_observed_batch"]
   )
   assert requirements["sequential_loop_alone_qualifies"] is False
   assert requirements["candidate_membership_alone_qualifies"] is False
   assert (
      rrb.MEASURED_PAIRED_WINDOWS
      == inventory["benchmark_requirements"]["measured_windows"]
   )

   verifier = inventory["target_verifier_requirements"]
   assert list(atv.QUALIFIED_GENERATION_POLICIES) == [verifier["generation_policy"]]
   assert list(atv.TRANSACTION_MODES) == verifier["transaction_modes"]
   assert list(atv.QUALIFIED_WIDTH_CLASSES) == verifier["qualified_width_classes"]
   assert atv.MINIMUM_QUALIFIED_OPERATIONS == verifier["minimum_operations"]
   assert (
      atv.MINIMUM_PREFIX_LENGTH_BUCKETS
      == verifier["minimum_prefix_length_buckets"]
   )


# ---------------------------------------------------------------------------
# 2. Every boundary refuses explicitly and returns nothing
# ---------------------------------------------------------------------------


def test_boundary_table_is_complete_and_has_no_extra_entries():
   assert set(BOUNDARY_CALLS) == {name for name, _ in BOUNDARIES}
   codes = [code for _, code in BOUNDARIES]
   assert len(set(codes)) == len(codes)


@pytest.mark.parametrize("name,code", BOUNDARIES, ids=[name for name, _ in BOUNDARIES])
def test_every_boundary_refuses_with_its_coded_exception(name, code):
   call = BOUNDARY_CALLS[name]
   with pytest.raises(rrb.A10NotImplementedError) as raised:
      returned = call()
      pytest.fail(f"{name} returned {returned!r} instead of refusing")
   assert isinstance(raised.value, ValueError)
   assert str(raised.value) == f"not_implemented:{code}"
   assert raised.value.code == f"not_implemented:{code}"


def test_a_coded_contract_refusal_is_distinguishable_from_the_boundary():
   with pytest.raises(rrb.A10ContractError) as invalid:
      rrb.execute_observed_batch(object(), object())
   assert not isinstance(invalid.value, rrb.A10NotImplementedError)
   assert invalid.value.code == "invalid_runtime_batch_proposal_argument"

   # A port that cannot dispatch is rejected before any member is offered.
   with pytest.raises(rrb.A10ContractError) as port_rejected:
      rrb.execute_observed_batch(StubPort(), _proposal())
   assert not isinstance(port_rejected.value, rrb.A10NotImplementedError)
   assert port_rejected.value.code == "invalid_runtime_batch_port"


def test_a_port_that_cannot_execute_is_rejected_before_the_refusal():
   with pytest.raises(rrb.A10ContractError) as raised:
      rrb.execute_observed_batch(object(), _proposal())
   assert raised.value.code == "invalid_runtime_batch_port"


# ---------------------------------------------------------------------------
# 3. Validation is real, coded and honest about what it does not cover
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
   "overrides,code",
   [
      ({"compatibility_key": None}, "missing_batch_compatibility_identity"),
      ({"member_refs": ()}, "invalid_batch_member_refs"),
      (
         {"member_refs": ("member:one", "member:one")},
         "duplicate_batch_member_ref",
      ),
      ({"maximum_members": 0}, "invalid_batch_member_bound"),
      ({"maximum_members": 1}, "excess_batch_member_count"),
      ({"maximum_bytes": rrb.SCAFFOLD_BYTE_CEILING + 1}, "invalid_batch_byte_bound"),
      ({"qos_classes": ("interactive",)}, "invalid_batch_qos_count"),
      ({"qos_classes": ("priority", "batch")}, "invalid_batch_qos_class"),
      ({"proposed_at": float("nan")}, "invalid_batch_proposed_at"),
      ({"collection_deadline_at": -1.0}, "invalid_batch_collection_deadline"),
   ],
)
def test_proposal_rejections_are_coded(overrides, code):
   with pytest.raises(rrb.A10ContractError) as raised:
      _proposal(**overrides)
   assert raised.value.code == code


@pytest.mark.parametrize(
   "overrides,code",
   [
      ({"generation_policy": "sampling"}, "unsupported_generation_policy"),
      ({"transaction_mode": "commit_anything"}, "invalid_transaction_mode"),
      ({"candidate_position_count": 0}, "invalid_candidate_position_count"),
      (
         {"candidate_position_count": atv.SCAFFOLD_MAXIMUM_VERIFICATION_POSITIONS + 1},
         "invalid_candidate_position_count",
      ),
      ({"candidate_bytes": 0}, "invalid_candidate_bytes"),
      ({"committed_prefix_position": -1}, "invalid_committed_prefix_position"),
      ({"committed_prefix_digest": "sha256:nothex"}, "invalid_committed_prefix_digest"),
      ({"idempotency_digest": ""}, "invalid_idempotency_digest"),
      ({"deadline_at": float("inf")}, "invalid_verification_deadline"),
      ({"cancellation_generation": -1}, "invalid_cancellation_generation"),
      ({"runtime_incarnation": " "}, "invalid_runtime_incarnation"),
   ],
)
def test_verification_request_rejections_are_coded(overrides, code):
   with pytest.raises(rrb.A10ContractError) as raised:
      _verification_request(**overrides)
   assert raised.value.code == code


def test_unsupported_generation_policy_is_never_coerced_to_greedy():
   with pytest.raises(rrb.A10ContractError) as raised:
      _verification_request(generation_policy="sampling")
   assert raised.value.code == "unsupported_generation_policy"
   assert atv.QUALIFIED_GENERATION_POLICIES == frozenset({"deterministic_greedy"})

   # The qualified policy is admitted at shape level, but a port that cannot open a
   # target-KV transaction is still refused before any candidate is evaluated, so
   # admission is never mistaken for execution.
   request = _verification_request(generation_policy="deterministic_greedy")
   with pytest.raises(rrb.A10ContractError) as refused:
      atv.verify_candidate_positions(StubPort(), request)
   assert refused.value.code == "invalid_target_verification_port"


@pytest.mark.parametrize(
   "overrides,code",
   [
      ({"membership_authority": "queue_proposal"}, "invalid_membership_authority"),
      ({"backend_invocations": 0}, "invalid_backend_invocation_count"),
      ({"actual_member_count": 3}, "invalid_actual_member_count"),
      (
         {"member_outcomes": (("member:one", "completed"),)},
         "invalid_member_outcome_count",
      ),
      (
         {"member_outcomes": (("member:one", "completed"), ("member:one", "completed"))},
         "duplicate_member_outcome",
      ),
      ({"executed_at": -1.0}, "invalid_observation_intervals"),
      ({"backend_invocation_id": ""}, "invalid_backend_invocation_id"),
   ],
)
def test_observation_rejections_are_coded(overrides, code):
   values = {
      "batch_ref": "batch:one",
      "phase": "DECODE",
      "stage": "stage-000",
      "backend_invocation_id": "invocation:one",
      "backend_invocations": 1,
      "membership_authority": "stage_runtime",
      "proposed_member_count": 2,
      "actual_member_count": 2,
      "member_outcomes": (("member:one", "completed"), ("member:two", "completed")),
      "collected_at": 0.0,
      "executed_at": 1.0,
      "cleanup": "released",
   }
   values.update(overrides)
   with pytest.raises(rrb.A10ContractError) as raised:
      rrb.RuntimeBatchObservation(**values)
   assert raised.value.code == code


def test_observed_batch_predicate_never_over_claims():
   real = rrb.RuntimeBatchObservation(
      batch_ref="batch:one",
      phase="DECODE",
      stage="stage-000",
      backend_invocation_id="invocation:one",
      backend_invocations=1,
      membership_authority="stage_runtime",
      proposed_member_count=2,
      actual_member_count=2,
      member_outcomes=(("member:one", "completed"), ("member:two", "completed")),
      collected_at=0.0,
      executed_at=1.0,
      cleanup="released",
   )
   assert real.claims_runtime_batch() is True

   split = rrb.RuntimeBatchObservation(
      batch_ref="batch:one",
      phase="DECODE",
      stage="stage-000",
      backend_invocation_id="invocation:one",
      backend_invocations=2,
      membership_authority="stage_runtime",
      proposed_member_count=2,
      actual_member_count=2,
      member_outcomes=(("member:one", "completed"), ("member:two", "completed")),
      collected_at=0.0,
      executed_at=1.0,
      cleanup="released",
   )
   assert split.claims_runtime_batch() is False

   singleton = rrb.RuntimeBatchObservation(
      batch_ref="batch:two",
      phase="DECODE",
      stage="stage-000",
      backend_invocation_id="invocation:two",
      backend_invocations=1,
      membership_authority="stage_runtime",
      proposed_member_count=1,
      actual_member_count=1,
      member_outcomes=(("member:one", "completed"),),
      collected_at=0.0,
      executed_at=1.0,
      cleanup="released",
   )
   assert singleton.claims_runtime_batch() is False


@pytest.mark.parametrize(
   "factory,overrides,code",
   [
      (_interval, {"finished_at": -1.0}, "reversed_interval"),
      (_interval, {"clock_uncertainty": -0.1}, "invalid_interval_uncertainty"),
      (_trace, {"intervals": ()}, "invalid_trace_intervals"),
      (_trace, {"causal_edges": (("interval:one", "interval:two"),)}, "unknown_causal_endpoint"),
      (_trace, {"causal_edges": (("interval:one", "interval:one"),)}, "unknown_causal_endpoint"),
   ],
)
def test_trace_rejections_are_coded(factory, overrides, code):
   with pytest.raises(rrb.A10ContractError) as raised:
      factory(**overrides)
   assert raised.value.code == code


def test_duplicate_interval_ref_is_rejected():
   with pytest.raises(rrb.A10ContractError) as raised:
      _trace(intervals=(_interval(), _interval(request_ref="request:two")))
   assert raised.value.code == "duplicate_interval_ref"


@pytest.mark.parametrize(
   "overrides,code",
   [
      ({"baseline_windows": 16, "candidate_windows": 16}, None),
      ({"valid_pairs": 16}, None),
      # v1 three-pair shape is not the benchmark protocol v2 schedule
      ({"baseline_windows": 3, "candidate_windows": 3}, "invalid_benchmark_window_count"),
      # the v2 schedule is exactly sixteen pairs; twelve is only the valid floor
      ({"baseline_windows": 12, "candidate_windows": 12}, "invalid_benchmark_window_count"),
      ({"baseline_windows": 16, "candidate_windows": 15}, "invalid_benchmark_window_count"),
      ({"baseline_windows": 17, "candidate_windows": 17}, "invalid_benchmark_window_count"),
      ({"valid_pairs": 11}, "invalid_benchmark_valid_pair_count"),
      ({"valid_pairs": 17}, "invalid_benchmark_valid_pair_count"),
      ({"baseline_windows": 2, "candidate_windows": 3}, "invalid_benchmark_window_count"),
      ({"baseline_windows": 0}, "invalid_benchmark_window_count"),
      ({"benchmark_manifest_digest": "sha256:zz"}, "invalid_benchmark_manifest_digest"),
      ({"acceptance_inventory_digest": ""}, "invalid_acceptance_inventory_digest"),
   ],
)
def test_qualification_request_rejections_are_coded(overrides, code):
   values = {
      "owner_private_root": "private:root",
      "benchmark_manifest_digest": DIGEST,
      "acceptance_inventory_digest": DIGEST,
      "baseline_windows": 16,
      "candidate_windows": 16,
      "valid_pairs": 12,
   }
   values.update(overrides)
   if code is None:
      request = rrb.QualificationRequest(**values)
      assert request.baseline_windows + request.candidate_windows == 32
      return
   with pytest.raises(rrb.A10ContractError) as raised:
      rrb.QualificationRequest(**values)
   assert raised.value.code == code


@pytest.mark.parametrize(
   "overrides,code",
   [
      ({"commit_state": "rolled_back", "committed_position": 16}, None),
      ({"commit_state": "committed"}, None),
      ({"committed_position": 21}, "invalid_commit_position"),
      ({"commit_state": "committed_with_correction"}, "invalid_commit_position"),
      (
         {"commit_state": "rolled_back", "committed_position": 17},
         "invalid_commit_position",
      ),
      (
         {
            "commit_state": "committed_with_correction",
            "accepted_prefix_count": 4,
            "correction_count": 1,
            "committed_position": 21,
         },
         "invalid_commit_position",
      ),
      ({"commit_state": "partial"}, "invalid_commit_state"),
      ({"verified_count": 5}, "invalid_verified_count"),
      ({"accepted_prefix_count": 5}, "invalid_accepted_prefix_count"),
   ],
)
def test_outcome_commit_arithmetic_is_closed(overrides, code):
   values = {
      "request_ref": "verification:one",
      "prefix_position": 16,
      "proposed_position_count": 4,
      "verified_count": 4,
      "accepted_prefix_count": 4,
      "correction_count": 0,
      "committed_position": 20,
      "commit_state": "committed",
      "cleanup": "released",
   }
   values.update(overrides)
   if code is None:
      outcome = atv.TargetVerificationOutcome(**values)
      assert outcome.commit_state == values["commit_state"]
      assert outcome.committed_position == values["committed_position"]
      return
   with pytest.raises(rrb.A10ContractError) as raised:
      atv.TargetVerificationOutcome(**values)
   assert raised.value.code == code


def test_verifier_capability_cannot_be_advertised_unqualified():
   values = {
      "runtime_digest": DIGEST,
      "model_digest": DIGEST,
      "representation_digest": DIGEST,
      "runtime_incarnation": "incarnation:one",
      "maximum_positions": 4,
      "maximum_bytes": 4096,
      "generation_policy": "deterministic_greedy",
      "transaction_modes": atv.TRANSACTION_MODES,
      "qualification_binding": DIGEST,
   }
   capability = atv.TargetVerificationCapability(**values)
   assert capability.qualification_binding == DIGEST

   for binding in ("", "qualified", "sha256:short"):
      with pytest.raises(rrb.A10ContractError) as raised:
         atv.TargetVerificationCapability(**{**values, "qualification_binding": binding})
      assert raised.value.code == "unqualified_target_verification_capability"


@pytest.mark.parametrize(
   "overrides,code",
   [
      (
         {"operations": atv.MINIMUM_QUALIFIED_OPERATIONS - 1},
         "insufficient_qualification_operations",
      ),
      ({"prefix_length_buckets": 1}, "insufficient_prefix_length_buckets"),
      ({"width_classes": ("one",)}, "invalid_qualified_width_classes"),
      (
         {"sequential_parity_proof_digest": "not-a-digest"},
         "missing_sequential_parity_proof",
      ),
      ({"runtime_digest": "sha256:x"}, "invalid_runtime_digest"),
   ],
)
def test_verifier_qualification_rejections_are_coded(overrides, code):
   values = {
      "owner_private_root": "private:root",
      "runtime_digest": DIGEST,
      "width_classes": atv.QUALIFIED_WIDTH_CLASSES,
      "prefix_length_buckets": 2,
      "operations": atv.MINIMUM_QUALIFIED_OPERATIONS,
      "sequential_parity_proof_digest": DIGEST,
   }
   values.update(overrides)
   with pytest.raises(rrb.A10ContractError) as raised:
      atv.TargetVerificationQualificationRequest(**values)
   assert raised.value.code == code
