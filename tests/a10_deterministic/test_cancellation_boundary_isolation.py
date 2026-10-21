# SPDX-License-Identifier: AGPL-3.0-only
"""A10 `cancellation_boundary_isolation` deterministic negatives.

Frozen invariants covered: cancellation is checked before collection and before
backend launch; a member cancelled before launch is removed; a post-launch cancelled
result is discarded; only the cancelled member's KV rolls back; remaining members
receive exactly one result; cancelled-member cleanup is exact; the deployment and
unrelated batches remain usable.  The named cancellation points are the contract's
own five (implementation contract resolution 8).
"""

from __future__ import annotations

from tests.a10_deterministic import ensure_worktree_imports

# Resolve imports inside this lane's worktree even when another test module
# bound ``mycelium_router`` to the standing checkout first (see __init__).
ensure_worktree_imports()

import pytest

from mycelium_router import a10_runtime_batch as scaffold
from mycelium_router.a10_batch_admission import RuntimeBatchAdmission
from mycelium_router.a10_member_isolation import CANCELLATION_POINTS, MemberKvRegistry
from tests.a10_deterministic.doubles import BatchPort, compatibility_key, member, policy

COLLECT_AT = 0.01
DISPATCH_AT = 0.011


def _admission(port, **policy_overrides) -> RuntimeBatchAdmission:
   return RuntimeBatchAdmission(
      policy=policy(**policy_overrides),
      port=port,
      incarnation="incarnation:1",
      stage_ref="stage-000",
   )


def _three_member_admission(port) -> RuntimeBatchAdmission:
   admission = _admission(port)
   receipt = admission.submit(
      (
         member("member:a"),
         member("member:b"),
         member("member:c"),
      ),
      now=0.0,
   )
   assert receipt.accepted == ("member:a", "member:b", "member:c")
   return admission


def _code(exc: pytest.ExceptionInfo) -> str:
   return exc.value.code


def test_cancellation_points_are_named():
   assert CANCELLATION_POINTS == (
      "pre_collection",
      "pre_launch",
      "post_return",
      "pre_forward",
      "command_cancellation",
   )
   admission = _admission(BatchPort())
   with pytest.raises(scaffold.A10ContractError) as raised:
      admission.cancel("member:a", at="mid_flight", now=0.0)
   assert _code(raised) == "unknown_cancellation_point"


def test_pre_collection_cancellation_removes_the_member():
   port = BatchPort()
   admission = _three_member_admission(port)
   removal = admission.cancel("member:b", at="pre_collection", now=0.001)

   collection = admission.collect(now=COLLECT_AT)

   assert collection.candidate_member_refs == ("member:a", "member:c")
   assert collection.excluded == (("member:b", "cancelled_before_collection"),)
   assert removal.removal == "removed_before_collection"


def test_pre_launch_cancellation_removes_the_member_before_execution():
   port = BatchPort()
   admission = _three_member_admission(port)
   removal = admission.cancel("member:b", at="pre_launch", now=0.005)
   collection = admission.collect(now=COLLECT_AT)

   outcome = admission.dispatch(collection, now=DISPATCH_AT)

   assert removal.removal == "removed_before_launch"
   assert port.calls[0].member_refs == ("member:a", "member:c")
   assert outcome.observation.proposed_member_count == 3
   assert outcome.observation.actual_member_count == 2
   assert ("member:b", "cancelled") in outcome.member_outcomes


def test_command_cancellation_blocks_the_operation():
   port = BatchPort()
   admission = _admission(port)
   admission.submit((member("member:a"), member("member:b")), now=0.0)
   for member_ref in ("member:a", "member:b"):
      admission.cancel(member_ref, at="command_cancellation", now=0.001)
   collection = admission.collect(now=COLLECT_AT)

   with pytest.raises(scaffold.A10ContractError) as raised:
      admission.dispatch(collection, now=DISPATCH_AT)
   assert _code(raised) == "no_launchable_members"
   assert port.calls == []
   assert admission.observations() == ()


def test_post_launch_cancellation_discards_only_its_own_result():
   port = BatchPort(committed={"member:a": 8, "member:c": 8, "member:b": 8})
   admission = _three_member_admission(port)
   admission.cancel("member:b", at="post_return", now=0.005)
   collection = admission.collect(now=COLLECT_AT)

   outcome = admission.dispatch(collection, now=DISPATCH_AT)

   # The backend could not remove it after launch: bounded compute finished, the
   # result was discarded, and only that member is reported cancelled.
   assert port.calls[0].member_refs == ("member:a", "member:b", "member:c")
   assert outcome.observation.actual_member_count == 3
   assert outcome.observation.member_outcomes == (
      ("member:a", "completed"),
      ("member:b", "cancelled"),
      ("member:c", "completed"),
   )
   assert outcome.discarded == (("member:b", "result_discarded_after_return"),)
   assert outcome.forwarded == ("member:a", "member:c")


def test_pre_forward_cancellation_discards_the_result_before_forwarding():
   port = BatchPort()
   admission = _three_member_admission(port)
   admission.cancel("member:c", at="pre_forward", now=0.005)
   collection = admission.collect(now=COLLECT_AT)

   outcome = admission.dispatch(collection, now=DISPATCH_AT)

   assert outcome.observation.member_outcomes == (
      ("member:a", "completed"),
      ("member:b", "completed"),
      ("member:c", "cancelled"),
   )
   assert outcome.discarded == (("member:c", "result_discarded_before_forwarding"),)
   assert outcome.forwarded == ("member:a", "member:b")


def test_only_the_cancelled_member_kv_rolls_back():
   registry = MemberKvRegistry()
   port = BatchPort(committed={"member:a": 8, "member:b": 8, "member:c": 8})
   admission = RuntimeBatchAdmission(
      policy=policy(),
      port=port,
      incarnation="incarnation:1",
      stage_ref="stage-000",
      kv_registry=registry,
   )
   admission.submit(
      (
         member("member:a", committed_prefix=4),
         member("member:b", committed_prefix=4),
         member("member:c", committed_prefix=4),
      ),
      now=0.0,
   )
   admission.cancel("member:b", at="post_return", now=0.005)
   collection = admission.collect(now=COLLECT_AT)

   outcome = admission.dispatch(collection, now=DISPATCH_AT)

   assert outcome.forwarded == ("member:a", "member:c")
   # member:a and member:c advanced to the committed position they reported;
   # member:b was rolled back to its previously committed prefix (4) and its
   # peers keep theirs.
   assert dict(admission.kv_snapshot()) == {
      "member:a": (8, 8),
      "member:b": (4, 4),
      "member:c": (8, 8),
   }


def test_remaining_members_receive_exactly_one_result():
   port = BatchPort()
   admission = _three_member_admission(port)
   admission.cancel("member:b", at="post_return", now=0.005)
   collection = admission.collect(now=COLLECT_AT)

   outcome = admission.dispatch(collection, now=DISPATCH_AT)

   assert [record.member_ref for record in outcome.members] == [
      "member:a",
      "member:b",
      "member:c",
   ]
   assert outcome.forwarded == ("member:a", "member:c")
   assert [record.forward_count for record in outcome.members] == [1, 0, 1]
   assert {ref for ref, _ in outcome.results} == {"member:a", "member:b", "member:c"}
   assert len(outcome.results) == len(port.calls[0].member_refs)


def test_cancelled_member_cleanup_is_exact():
   port = BatchPort()
   admission = _admission(port)
   baseline = admission.resource_usage()
   admission.submit(
      (member("member:a"), member("member:b"), member("member:c")), now=0.0
   )
   admission.cancel("member:b", at="pre_launch", now=0.005)
   collection = admission.collect(now=COLLECT_AT)

   admission.dispatch(collection, now=DISPATCH_AT)

   assert admission.resource_usage() == baseline
   assert admission.queue_depth() == 0


def test_unrelated_batches_remain_usable_after_a_cancellation():
   port = BatchPort()
   admission = _three_member_admission(port)
   admission.cancel("member:b", at="post_return", now=0.005)
   first = admission.dispatch(admission.collect(now=COLLECT_AT), now=DISPATCH_AT)

   assert first.forwarded == ("member:a", "member:c")

   admission.submit(
      (
         member(
            "member:x",
            compatibility_key=compatibility_key(placement_id="placement-x"),
         ),
         member(
            "member:y",
            compatibility_key=compatibility_key(placement_id="placement-x"),
         ),
      ),
      now=DISPATCH_AT + 0.001,
   )
   second = admission.dispatch(
      admission.collect(now=DISPATCH_AT + 1.0), now=DISPATCH_AT + 1.001
   )
   assert second.observation.claims_runtime_batch() is True
   assert second.forwarded == ("member:x", "member:y")
   assert len(port.calls) == 2
