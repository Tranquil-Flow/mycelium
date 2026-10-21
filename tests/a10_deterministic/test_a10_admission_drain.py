# SPDX-License-Identifier: AGPL-3.0-only
"""A queue whose every member was cancelled before collection drains cleanly.

Failure mode this suite catches: ``RuntimeBatchAdmission.collect`` indexing the
queue after the pre-collection cancellations had already emptied it, so a shutdown
that cancels everything still queued raised ``IndexError`` instead of returning an
empty collection and leaving zero queue residue.
"""

from __future__ import annotations

from tests.a10_deterministic import ensure_worktree_imports

ensure_worktree_imports()

from mycelium_router.a10_batch_admission import RuntimeBatchAdmission
from tests.a10_deterministic.doubles import BatchPort, member, policy


def _admission() -> RuntimeBatchAdmission:
   return RuntimeBatchAdmission(
      policy=policy(), port=BatchPort(), incarnation="incarnation:1", stage_ref="stage-000"
   )


def test_collecting_after_every_queued_member_was_cancelled_returns_an_empty_collection():
   admission = _admission()
   admission.submit((member("member:a"), member("member:b")), now=0.0)
   for ref in ("member:a", "member:b"):
      admission.cancel(ref, at="pre_collection", now=0.001)

   collection = admission.collect(now=0.002)

   assert collection.selected == ()
   assert collection.reason == "no_eligible_members"
   assert sorted(ref for ref, _ in collection.excluded) == ["member:a", "member:b"]
   usage = admission.resource_usage()
   assert (usage.queued_members, usage.queued_bytes, usage.active_batches, usage.pending_reservations) == (0, 0, 0, 0)
   assert admission.queue_depth() == 0
