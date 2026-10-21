# SPDX-License-Identifier: AGPL-3.0-only
"""A10 `slow_consumer_isolation` deterministic negatives.

Frozen invariants covered: the bounded gateway buffer fills without unbounded
growth; the slow member is detached from future scheduling; the declared policy
cancels or terminates only the slow member; batch peers continue and receive one
result; stage/transport/SSE/replay/evidence buffers remain bounded; request-owned
resources return to baseline; private output never crosses the member boundary.
"""

from __future__ import annotations

from tests.a10_deterministic import ensure_worktree_imports

# Resolve imports inside this lane's worktree even when another test module
# bound ``mycelium_router`` to the standing checkout first (see __init__).
ensure_worktree_imports()

import pytest

from mycelium_router import a10_runtime_batch as scaffold
from mycelium_router.a10_batch_admission import RuntimeBatchAdmission
from tests.a10_deterministic.doubles import BatchPort, member, policy

COLLECT_AT = 0.01
DISPATCH_AT = 0.011


def _admission(port, **policy_overrides) -> RuntimeBatchAdmission:
   return RuntimeBatchAdmission(
      policy=policy(**policy_overrides),
      port=port,
      incarnation="incarnation:1",
      stage_ref="stage-000",
   )


def _code(exc: pytest.ExceptionInfo) -> str:
   return exc.value.code


def test_bounded_gateway_buffer_fills_without_unbounded_growth():
   port = BatchPort(events={"member:a": (8, 4, 4), "member:b": (2,)})
   admission = _admission(port, gateway_buffer_bytes=8)
   admission.submit((member("member:a"), member("member:b")), now=0.0)
   collection = admission.collect(now=COLLECT_AT)

   outcome = admission.dispatch(collection, now=DISPATCH_AT)

   assert outcome.detached == (("member:a", "gateway_buffer_exhausted"),)
   assert admission.buffer_high_water() == 8
   assert admission.buffer_high_water() <= 8


def test_declared_policy_terminates_only_the_slow_member():
   port = BatchPort(events={"member:a": (8, 8), "member:b": (2,)})
   admission = _admission(port, gateway_buffer_bytes=8, detachment_policy="terminate")
   admission.submit((member("member:a"), member("member:b")), now=0.0)
   collection = admission.collect(now=COLLECT_AT)

   outcome = admission.dispatch(collection, now=DISPATCH_AT)

   assert outcome.observation.member_outcomes == (
      ("member:a", "cancelled"),
      ("member:b", "completed"),
   )
   assert outcome.detached == (("member:a", "gateway_buffer_exhausted"),)


def test_batch_peers_continue_and_receive_exactly_one_result():
   port = BatchPort(events={"member:a": (8, 4), "member:b": (3,)})
   admission = _admission(port, gateway_buffer_bytes=8)
   admission.submit((member("member:a"), member("member:b")), now=0.0)
   collection = admission.collect(now=COLLECT_AT)

   outcome = admission.dispatch(collection, now=DISPATCH_AT)

   assert outcome.forwarded == ("member:b",)
   assert [record.forward_count for record in outcome.members] == [0, 1]
   assert len(port.calls) == 1
   assert outcome.observation.backend_invocations == 1


def test_private_output_does_not_cross_the_member_boundary():
   port = BatchPort(events={"member:a": (16, 4), "member:b": (3, 3)})
   admission = _admission(port, gateway_buffer_bytes=8)
   admission.submit((member("member:a"), member("member:b")), now=0.0)
   collection = admission.collect(now=COLLECT_AT)

   outcome = admission.dispatch(collection, now=DISPATCH_AT)

   feeds = dict(outcome.event_feeds)
   assert feeds["member:b"] == (3, 3)
   assert feeds["member:a"] == ()
   assert 16 not in feeds["member:b"]


def test_slow_member_is_detached_from_future_scheduling():
   port = BatchPort(events={"member:a": (8, 4)})
   admission = _admission(port, gateway_buffer_bytes=8)
   admission.submit((member("member:a"),), now=0.0)
   admission.dispatch(admission.collect(now=COLLECT_AT), now=DISPATCH_AT)

   receipt = admission.submit(
      (member("member:a"), member("member:b")), now=DISPATCH_AT + 0.001
   )

   assert receipt.accepted == ("member:b",)
   assert ("member:a", "detached_member_not_schedulable") in receipt.refusals


def test_oversized_member_event_fails_closed():
   port = BatchPort(events={"member:a": (8_192,),"member:b": (2,)})
   admission = _admission(port, maximum_member_event_bytes=4_096)
   admission.submit((member("member:a"), member("member:b")), now=0.0)
   collection = admission.collect(now=COLLECT_AT)

   with pytest.raises(scaffold.A10ContractError) as raised:
      admission.dispatch(collection, now=DISPATCH_AT)
   assert _code(raised) == "unbounded_member_event_fails_closed"


def test_request_owned_resources_return_to_baseline():
   port = BatchPort(events={"member:a": (8, 4), "member:b": (2,)})
   admission = _admission(port, gateway_buffer_bytes=8)
   baseline = admission.resource_usage()
   admission.submit((member("member:a"), member("member:b")), now=0.0)

   outcome = admission.dispatch(admission.collect(now=COLLECT_AT), now=DISPATCH_AT)

   assert outcome.observation.backend_invocations == 1
   assert admission.resource_usage() == baseline
   assert admission.buffer_high_water() <= 8
   assert admission.buffered_event_bytes() == 0
