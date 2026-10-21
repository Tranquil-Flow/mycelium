# SPDX-License-Identifier: AGPL-3.0-only
"""A10 `bounded_queue_qos_aging` deterministic negatives (frozen invariant set).

Frozen invariants covered: finite member/byte/active-batch limits; overflow rejects
before runtime dispatch with a bounded retry hint; the interactive deadline guard
limits collection; aged eligible batch work cannot starve; ties and exclusion
reasons are deterministic; accepted members continue; queue and reservation state
returns to baseline.  Nothing physical is executed or claimed.
"""

from __future__ import annotations

from tests.a10_deterministic import ensure_worktree_imports

# Resolve imports inside this lane's worktree even when another test module
# bound ``mycelium_router`` to the standing checkout first (see __init__).
ensure_worktree_imports()

import pytest

from mycelium_router import a10_runtime_batch as scaffold
from mycelium_router.a10_batch_admission import RuntimeBatchAdmission
from tests.a10_deterministic.doubles import (
   BatchPort,
   compatibility_key,
   member,
   policy,
)

COLLECT_AT = 0.01


def _admission(port, **policy_overrides) -> RuntimeBatchAdmission:
   return RuntimeBatchAdmission(
      policy=policy(**policy_overrides),
      port=port,
      incarnation="incarnation:1",
      stage_ref="stage-000",
   )


def _isolated(member_ref: str, placement: str, **overrides):
   """One member with its own compatibility identity (so it collects alone)."""

   values = {
      "compatibility_key": compatibility_key(placement_id=placement),
   }
   values.update(overrides)
   return member(member_ref, **values)


def _code(exc: pytest.ExceptionInfo) -> str:
   return exc.value.code


# ---------------------------------------------------------------------------
# Bounds are finite and coded
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
   "overrides,code",
   [
      ({"maximum_members": 0}, "invalid_batch_member_limit"),
      ({"maximum_queued_members": 0}, "invalid_queue_member_limit"),
      ({"maximum_queued_members": float("nan")}, "invalid_queue_member_limit"),
      ({"maximum_queued_bytes": 0}, "invalid_queue_byte_limit"),
      ({"maximum_active_batches": 0}, "invalid_active_batch_limit"),
      ({"maximum_consecutive_interactive_selections": -1}, "invalid_aging_bound"),
      ({"collection_window_seconds": float("inf")}, "invalid_collection_window"),
      ({"deadline_guard_seconds": -0.1}, "invalid_deadline_guard"),
      ({"maximum_retry_after_seconds": 0.0}, "invalid_retry_hint_bound"),
      ({"maximum_member_event_bytes": 0}, "invalid_member_event_bound"),
      ({"gateway_buffer_bytes": -1}, "invalid_gateway_buffer_bound"),
      ({"detachment_policy": "ignore"}, "invalid_detachment_policy"),
   ],
)
def test_policy_limits_are_finite_and_coded(overrides, code):
   with pytest.raises(scaffold.A10ContractError) as raised:
      policy(**overrides)
   assert _code(raised) == code


# ---------------------------------------------------------------------------
# Overflow rejects before runtime dispatch, with a bounded retry hint
# ---------------------------------------------------------------------------


def test_member_overflow_rejects_before_runtime_dispatch():
   port = BatchPort()
   admission = _admission(port, maximum_queued_members=2)
   receipt = admission.submit(
      (member("member:a"), member("member:b"), member("member:c")), now=0.0
   )

   assert receipt.accepted == ("member:a", "member:b")
   assert receipt.refusals == (("member:c", "queue_member_limit"),)
   assert 0.0 < receipt.retry_after_seconds <= 0.05
   # Rejection happened before any runtime dispatch.
   assert port.calls == []

   collection = admission.collect(now=COLLECT_AT)
   assert collection.candidate_member_refs == ("member:a", "member:b")
   assert "member:c" not in collection.candidate_member_refs


def test_byte_overflow_rejects_with_bounded_retry_hint():
   port = BatchPort()
   admission = _admission(port, maximum_queued_bytes=100)
   receipt = admission.submit(
      (
         member("member:a", encoded_bytes=64),
         member("member:b", encoded_bytes=64),
      ),
      now=0.0,
   )

   assert receipt.accepted == ("member:a",)
   assert receipt.refusals == (("member:b", "queue_byte_limit"),)
   assert 0.0 < receipt.retry_after_seconds <= 0.05


def test_accepted_members_continue_and_queue_returns_to_baseline():
   port = BatchPort()
   admission = _admission(port, maximum_queued_members=2)
   baseline = admission.resource_usage()
   admission.submit(
      (member("member:a"), member("member:b"), member("member:c")), now=0.0
   )

   collection = admission.collect(now=COLLECT_AT)
   outcome = admission.dispatch(collection, now=COLLECT_AT + 0.001)

   assert outcome.observation.actual_member_count == 2
   assert len(port.calls) == 1
   assert admission.resource_usage() == baseline


# ---------------------------------------------------------------------------
# Active-batch limit and deadline guard
# ---------------------------------------------------------------------------


def test_active_batch_limit_is_finite_and_refuses_further_collection():
   port = BatchPort()
   admission = _admission(port, maximum_active_batches=1)
   admission.submit((member("member:a"), member("member:b")), now=0.0)
   first = admission.collect(now=COLLECT_AT)
   assert first.candidate_member_refs == ("member:a", "member:b")

   admission.submit((_isolated("member:c", "placement-c"),), now=COLLECT_AT)
   blocked = admission.collect(now=COLLECT_AT + 0.01)

   assert blocked.candidate_member_refs == ()
   assert blocked.reason == "active_batch_limit"
   assert port.calls == []

   admission.dispatch(first, now=COLLECT_AT + 0.001)
   after = admission.collect(now=COLLECT_AT + 0.02)
   assert after.candidate_member_refs == ("member:c",)


def test_interactive_deadline_guard_limits_collection():
   port = BatchPort()
   admission = _admission(
      port,
      collection_window_seconds=0.5,
      deadline_guard_seconds=0.001,
   )
   admission.submit(
      (
         member(
            "member:urgent",
            qos_class="batch",
            enqueued_at=0.0,
            deadline_at=0.0014,
         ),
      ),
      now=0.0,
   )
   # The collection window has not expired, but the deadline guard does.
   collection = admission.collect(now=0.0005)
   assert collection.candidate_member_refs == ("member:urgent",)
   assert collection.reason == "deadline_guard"

   patient = _admission(
      BatchPort(),
      collection_window_seconds=0.5,
      deadline_guard_seconds=0.001,
   )
   patient.submit(
      (member("member:patient", qos_class="batch", enqueued_at=0.0, deadline_at=9.0),),
      now=0.0,
   )
   waiting = patient.collect(now=0.0005)
   assert waiting.candidate_member_refs == ()
   assert waiting.reason == "collecting"
   assert waiting.excluded == (("member:patient", "collection_window_open"),)


# ---------------------------------------------------------------------------
# Aging: eligible batch work cannot starve
# ---------------------------------------------------------------------------


def test_aged_eligible_batch_work_cannot_starve():
   port = BatchPort()
   admission = _admission(
      port,
      maximum_consecutive_interactive_selections=2,
      collection_window_seconds=0.002,
   )
   admission.submit(
      (
         _isolated("member:batch-aged", "placement-batch", qos_class="batch"),
         _isolated("member:i1", "placement-i1", qos_class="interactive"),
         _isolated("member:i2", "placement-i2", qos_class="interactive"),
      ),
      now=0.0,
   )

   selected: list[str] = []
   reasons: list[str] = []
   now = COLLECT_AT
   for _ in range(3):
      collection = admission.collect(now=now)
      selected.extend(collection.candidate_member_refs)
      reasons.append(collection.reason)
      admission.dispatch(collection, now=now + 0.001)
      now += 1.0

   assert selected[0] == "member:i1"
   assert selected[1] == "member:i2"
   # The lower-priority batch member is scheduled by the aging bound, not starved.
   assert "member:batch-aged" in selected
   assert reasons[0] == "interactive_immediate"
   assert reasons[1] == "interactive_immediate"
   assert reasons[2] == "aged_batch_selected"


def test_aging_bound_is_a_bound_not_a_hint():
   port = BatchPort()
   admission = _admission(port, maximum_consecutive_interactive_selections=0)
   admission.submit(
      (
         _isolated("member:batch-aged", "placement-batch", qos_class="batch"),
         _isolated("member:i1", "placement-i1", qos_class="interactive"),
      ),
      now=0.0,
   )

   first = admission.collect(now=COLLECT_AT)
   # With a zero bound the aged eligible batch member wins the very first collect.
   assert first.candidate_member_refs == ("member:batch-aged",)
   assert first.reason == "aged_batch_selected"


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_ties_and_exclusion_reasons_are_deterministic():
   port = BatchPort()
   admission = _admission(port, maximum_members=2, maximum_batch_bytes=200)
   admission.submit(
      (
         member("member:a", encoded_bytes=64, enqueued_at=0.0),
         member("member:b", encoded_bytes=64, enqueued_at=0.0),
         member("member:c", encoded_bytes=64, enqueued_at=0.0),
      ),
      now=0.0,
   )

   first = admission.collect(now=COLLECT_AT)
   assert first.candidate_member_refs == ("member:a", "member:b")
   assert first.excluded == (("member:c", "batch_member_limit"),)

   replay = _admission(BatchPort(), maximum_members=2, maximum_batch_bytes=200)
   replay.submit(
      (
         member("member:a", encoded_bytes=64, enqueued_at=0.0),
         member("member:b", encoded_bytes=64, enqueued_at=0.0),
         member("member:c", encoded_bytes=64, enqueued_at=0.0),
      ),
      now=0.0,
   )
   second = replay.collect(now=COLLECT_AT)
   assert second.candidate_member_refs == first.candidate_member_refs
   assert second.excluded == first.excluded
   assert second.reason == first.reason
