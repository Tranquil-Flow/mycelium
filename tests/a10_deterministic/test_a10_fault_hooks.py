# SPDX-License-Identifier: AGPL-3.0-only
"""A10 runtime-batch fault hooks: the nine negatives the product API cannot drive.

``run_a10_physical_gate.py negatives`` executes cancellation isolation, queue
overflow and slow consumer through the ordinary product API.  The other nine of the
twelve required runtime-batch negatives (specification section 10) need a fault
injected inside the runtime-batch path: timeout, incompatible member, stale prefix,
partial member failure, shared backend failure, wrong result count, runtime exit,
shutdown and verifier rollback.

This suite pins, one failure mode per test:

  test_hooks_are_opt_in_and_refuse_without_the_exact_fleet_grant
      A hook that can be built without the exact A10 fleet grant.
  test_unarmed_hooks_are_a_pure_pass_through
      A wrapped port that changes behaviour while no fault is armed.
  test_a_fault_is_single_shot_and_arming_is_closed
      A fault that fires twice, arms twice at once, or accepts an unknown kind.
  test_production_modules_never_import_the_fault_hooks
      Any production module (other than the gate runner) able to enable a fault.
  test_each_negative_fails_closed_within_its_scope_with_zero_residue
      A negative that is recorded as passed without its fault firing, without the
      victims failing closed, without peers surviving, or with leftover resources.
  test_the_judge_rejects_a_tampered_observation
      A judge that would pass a leaked result, a leaked resource, a poisoned peer or
      a fault that never fired.
"""

from __future__ import annotations

from tests.a10_deterministic import ensure_worktree_imports

ensure_worktree_imports()

import re
import subprocess
from pathlib import Path

import pytest

from mycelium_a10_verify_control import EXACT_FLEET_GRANT
from mycelium_router.a10_fault_hooks import (
   FAULT_KINDS,
   A10FaultHookRefused,
   A10FaultHooks,
   A10InjectedFault,
   FaultInjectingDispatchPort,
)
from mycelium_router.a10_fault_negatives import (
   CPU_SURFACE,
   ReferenceDispatchPort,
   judge,
   observe_fault,
   run_fault_negative,
)
from mycelium_router.a10_qualification import REQUIRED_RUNTIME_BATCH_NEGATIVES

REPO = Path(__file__).resolve().parents[2]
FROZEN_PACKAGE_SOURCE = "f905d54a5e0b9816729aa587d2423a6d30675e73"
PRODUCT_DRIVEN = ("cancellation_isolation", "queue_overflow", "slow_consumer")

EXPECTED_ERROR = {
   "timeout": ("runtime_dispatch_failed", "backend_timeout"),
   "shared_backend_failure": ("runtime_dispatch_failed", "shared_backend_failure"),
   "runtime_exit": ("runtime_dispatch_failed", "runtime_exit"),
   "shutdown": ("runtime_dispatch_failed", "runtime_shutdown"),
   "wrong_result_count": ("member_identity_mismatch_fails_closed", None),
   "stale_prefix": ("invalid_kv_position", None),
   "partial_member_failure": (None, None),
   "incompatible_member": (None, None),
   "verifier_rollback": ("verifier_transaction_failure", None),
}


# ---------------------------------------------------------------------------
# The inventory
# ---------------------------------------------------------------------------


def test_fault_kinds_are_exactly_the_nine_negatives_the_product_api_cannot_drive():
   assert len(FAULT_KINDS) == 9
   assert set(FAULT_KINDS).isdisjoint(PRODUCT_DRIVEN)
   assert set(FAULT_KINDS) | set(PRODUCT_DRIVEN) == set(REQUIRED_RUNTIME_BATCH_NEGATIVES)


# ---------------------------------------------------------------------------
# The hooks are opt-in
# ---------------------------------------------------------------------------


def test_the_mirrored_grant_equals_the_product_grant():
   from mycelium_router import a10_fault_hooks

   assert a10_fault_hooks.EXACT_FLEET_GRANT == EXACT_FLEET_GRANT


@pytest.mark.parametrize(
   "grant", [None, "", "FLEET GRANTED TO A10 ", "fleet granted to a10", "FLEET GRANTED TO A6", 1, b"x"]
)
def test_hooks_are_opt_in_and_refuse_without_the_exact_fleet_grant(grant):
   with pytest.raises(A10FaultHookRefused) as refused:
      A10FaultHooks(grant=grant)
   assert str(refused.value) == "explicit_fleet_grant_required"


def test_unarmed_hooks_are_a_pure_pass_through():
   hooks = A10FaultHooks(grant=EXACT_FLEET_GRANT)
   assert hooks.armed() is None
   bare = ReferenceDispatchPort(prefixes={"a": 3, "b": 3})
   wrapped = FaultInjectingDispatchPort(ReferenceDispatchPort(prefixes={"a": 3, "b": 3}), hooks)
   request = _request(("a", "b"))

   assert wrapped.dispatch_mode() == bare.dispatch_mode()
   assert wrapped.execute_batch(request) == bare.execute_batch(request)
   assert hooks.fired() == ()
   assert hooks.status()["qualification_claim"] is False
   assert hooks.status()["physical_evidence"] is False


def test_a_fault_is_single_shot_and_arming_is_closed():
   hooks = A10FaultHooks(grant=EXACT_FLEET_GRANT)
   with pytest.raises(A10FaultHookRefused) as unknown:
      hooks.arm("cancellation_isolation")
   assert str(unknown.value) == "unknown_fault_kind"

   hooks.arm("shared_backend_failure")
   with pytest.raises(A10FaultHookRefused) as twice:
      hooks.arm("timeout")
   assert str(twice.value) == "fault_already_armed"

   port = FaultInjectingDispatchPort(ReferenceDispatchPort(prefixes={"a": 3, "b": 3}), hooks)
   request = _request(("a", "b"))
   with pytest.raises(A10InjectedFault) as raised:
      port.execute_batch(request)
   assert raised.value.code == "shared_backend_failure"
   assert hooks.fired() == ("shared_backend_failure",)
   assert hooks.armed() is None
   # The fault is spent: the very next invocation is a normal one.
   assert port.execute_batch(request).backend_invocations == 1


def _request(member_refs):
   from mycelium_router.a10_batch_admission import RuntimeBatchRequest

   return RuntimeBatchRequest(
      batch_ref="batch:t",
      phase="DECODE",
      compatibility_key="sha256:" + "0" * 64,
      member_refs=tuple(member_refs),
      proposed_member_count=len(member_refs),
   )


def _fault_hook_importers(repo):
   allowed = {
      repo / "mycelium_router" / "a10_fault_hooks.py",
      repo / "mycelium_router" / "a10_fault_negatives.py",
      repo / "scripts" / "run_a10_physical_gate.py",
   }
   pattern = re.compile(r"\ba10_fault_(hooks|negatives)\b")
   offenders = []
   for path in repo.rglob("*.py"):
      relative = path.relative_to(repo)
      if relative.parts[0] in {"tests", "runs", "node_modules", ".build-native"} or ".venv" in relative.parts:
         continue
      if path in allowed:
         continue
      # Packaging copies the allowlisted modules into compiler-sources. They
      # remain allowed only while their shipped bytes equal the source bytes;
      # a modified copy or any new production importer still fails closed.
      if (len(relative.parts) >= 5 and relative.parts[0] == "build"
            and relative.parts[2] == "compiler-sources"):
         source = repo.joinpath(*relative.parts[3:])
         if source in allowed and path.read_bytes() == source.read_bytes():
            continue
         # The L2 train exercises the frozen F package build beside newer
         # source. Only this exact candidate's original allowlisted runner
         # is an additional valid compiler-sources copy; arbitrary edits and
         # production importers remain offenders.
         if repo == REPO and source == repo / "scripts" / "run_a10_physical_gate.py":
            frozen = subprocess.check_output(
               ["git", "-C", str(repo), "show", f"{FROZEN_PACKAGE_SOURCE}:scripts/run_a10_physical_gate.py"]
            )
            if path.read_bytes() == frozen:
               continue
      if pattern.search(path.read_text("utf-8", "replace")):
         offenders.append(str(relative))
   return offenders


def test_production_modules_never_import_the_fault_hooks():
   assert _fault_hook_importers(REPO) == []


def test_packaged_fault_hooks_are_allowed_only_as_exact_source_copies(tmp_path):
   source = tmp_path / "mycelium_router" / "a10_fault_hooks.py"
   shipped = tmp_path / "build" / "preview" / "compiler-sources" / "mycelium_router" / source.name
   rogue = tmp_path / "mycelium_router" / "normal_runtime.py"
   for path in (source, shipped, rogue):
      path.parent.mkdir(parents=True, exist_ok=True)
   source.write_text("from mycelium_router.a10_fault_negatives import fault\n")
   shipped.write_bytes(source.read_bytes())
   assert _fault_hook_importers(tmp_path) == []
   shipped.write_text("from mycelium_router.a10_fault_negatives import changed\n")
   assert _fault_hook_importers(tmp_path) == [str(shipped.relative_to(tmp_path))]
   shipped.write_bytes(source.read_bytes())
   rogue.write_text("from mycelium_router.a10_fault_hooks import fault\n")
   assert _fault_hook_importers(tmp_path) == [str(rogue.relative_to(tmp_path))]


# ---------------------------------------------------------------------------
# Each negative, executed on CPU
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", FAULT_KINDS)
def test_each_negative_fails_closed_within_its_scope_with_zero_residue(kind):
   observation = observe_fault(kind, grant=EXACT_FLEET_GRANT)
   error, cause = EXPECTED_ERROR[kind]
   assert observation.fault == kind
   assert observation.fired is True, "the injected fault actually ran"
   assert observation.error_code == error
   assert observation.cause_code == cause
   assert observation.residue_fields == {}, "nothing is left behind"
   assert observation.peer_completed_once is True, "an unrelated placement is not poisoned"

   result = run_fault_negative(kind, grant=EXACT_FLEET_GRANT)
   assert result["outcome"] == "failed_closed_within_scope"
   assert result["residue"] == 0
   assert result["fault"] == kind
   assert result["surface"] == CPU_SURFACE
   assert result["physical_pending"] is True
   assert result["reasons"] == []


def test_the_victims_and_survivors_of_each_dispatch_fault_are_attributed_exactly():
   partial = observe_fault("partial_member_failure", grant=EXACT_FLEET_GRANT)
   assert partial.failed == ("g1",)
   assert partial.forwarded == ("g0", "g2")
   assert partial.prefixes_after["g1"] == partial.prefixes_before["g1"]
   assert partial.prefixes_after["g0"] == partial.prefixes_before["g0"] + 1

   for kind in ("timeout", "shared_backend_failure", "wrong_result_count", "stale_prefix", "runtime_exit", "shutdown"):
      observation = observe_fault(kind, grant=EXACT_FLEET_GRANT)
      assert observation.forwarded == (), kind
      assert observation.prefixes_after == observation.prefixes_before, kind

   incompatible = observe_fault("incompatible_member", grant=EXACT_FLEET_GRANT)
   assert incompatible.excluded == (("g2", "incompatible_compatibility_key"),)
   assert incompatible.forwarded == ("g0", "g1", "g2"), "served once each, the foreign member alone"
   assert incompatible.batch_sizes == (2, 1)

   for kind in ("runtime_exit", "shutdown"):
      observation = observe_fault(kind, grant=EXACT_FLEET_GRANT)
      assert observation.same_placement_followup == "failed_closed", "a dead runtime serves nothing"

   rollback = observe_fault("verifier_rollback", grant=EXACT_FLEET_GRANT)
   assert rollback.prefixes_after == rollback.prefixes_before
   assert rollback.verifier_open_transactions == 0
   assert rollback.verifier_commit_calls == 0
   assert rollback.verifier_rollback_calls == 1


def test_no_fault_leaks_into_a_fault_free_run():
   observation = observe_fault(None, grant=EXACT_FLEET_GRANT)
   assert observation.fired is False
   assert observation.error_code is None
   assert observation.forwarded == ("g0", "g1", "g2")
   assert observation.residue_fields == {}


# ---------------------------------------------------------------------------
# The judge is not a tautology
# ---------------------------------------------------------------------------


def _tampered(kind, **changes):
   import dataclasses

   observation = observe_fault(kind, grant=EXACT_FLEET_GRANT)
   return dataclasses.replace(observation, **changes)


@pytest.mark.parametrize(
   "kind, changes, reason",
   [
      ("shared_backend_failure", {"fired": False}, "fault_not_fired"),
      ("shared_backend_failure", {"forwarded": ("g0",)}, "uncommitted_result_forwarded"),
      ("shared_backend_failure", {"residue_fields": {"pending_reservations": 3}}, "residue_nonzero"),
      ("shared_backend_failure", {"peer_completed_once": False}, "peer_placement_poisoned"),
      ("shared_backend_failure", {"error_code": None}, "unexpected_error_code"),
      ("shared_backend_failure", {"cause_code": "backend_timeout"}, "unexpected_cause_code"),
      ("stale_prefix", {"prefixes_after": {"g0": 4, "g1": 3, "g2": 3}}, "prefix_not_restored"),
      ("partial_member_failure", {"failed": ("g0", "g1")}, "failure_exceeds_scope"),
      ("partial_member_failure", {"forwarded": ("g0",)}, "survivor_not_forwarded_once"),
      ("incompatible_member", {"excluded": ()}, "incompatible_member_not_excluded"),
      ("incompatible_member", {"batch_sizes": (3,)}, "incompatible_member_batched"),
      ("runtime_exit", {"same_placement_followup": "completed"}, "dead_runtime_served_work"),
      ("verifier_rollback", {"verifier_open_transactions": 1}, "verifier_transaction_left_open"),
      ("verifier_rollback", {"verifier_commit_calls": 1}, "verifier_committed_after_failure"),
   ],
)
def test_the_judge_rejects_a_tampered_observation(kind, changes, reason):
   passed, reasons = judge(_tampered(kind, **changes))
   assert passed is False
   assert reason in reasons


def test_the_judge_passes_the_untampered_observation():
   for kind in FAULT_KINDS:
      assert judge(observe_fault(kind, grant=EXACT_FLEET_GRANT)) == (True, [])
