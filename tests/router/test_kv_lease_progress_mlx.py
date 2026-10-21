# SPDX-License-Identifier: AGPL-3.0-only
"""The KV lease renewal rule on the MLX stage runtime (node-0's backend on a Mac).

Same contract as ``test_kv_lease_progress`` (NumPy): an applied continuation of
the owning path renews that path's local KV deadline by one bounded window;
abandoned work still expires; replays, refusals and other paths never renew.
Both the per-item path and the A10 one-invocation batch path are covered.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from mycelium_router.a10_batch_admission import (
   BatchExecutionReport,
   RuntimeBatchRequest,
   compatibility_digest,
)
from mycelium_router.contracts import RouterConfig
from mycelium_router.mlx_runtime import RUNTIME_BATCH_DISPATCH_MODE, MLXRuntimePort
from mycelium_router.payloads import encode_token_ids
from test_router_mlx_runtime import (  # noqa: F401 - fixture re-export
   _work_item,
   runtime_case,
)


_WINDOW = RouterConfig().reservation_lease_seconds
_LEASE = 130.0  # issued 30 s before the tests' t=100


class _Clock:
   def __init__(self, now: float) -> None:
      self.now = now

   def __call__(self) -> float:
      return self.now


@pytest.fixture()
def port_and_clock(runtime_case):
   clock = _Clock(100.0)
   placement = runtime_case.graph.stages[0].placements[0].placement_id
   port = MLXRuntimePort(
      runtime_case.assignments[0]["node_id"],
      runtime_case.graph,
      {placement: runtime_case.loaded[0]},
      clock=clock,
   )
   return port, clock


def _prefill(case, *, path: str = "path-renew", lease: float = _LEASE):
   return _work_item(
      case,
      0,
      encode_token_ids((1, 2)),
      phase="PREFILL",
      token_span=2,
      request_id=f"request-{path}",
      path_id=path,
      token_index=-1,
      position=0,
      lease_expires_at=lease,
   )


def _decode(case, step: int, *, path: str = "path-renew", lease: float = _LEASE):
   return _work_item(
      case,
      0,
      encode_token_ids((1 + step % 3,)),
      phase="DECODE",
      token_span=1,
      request_id=f"request-{path}",
      path_id=path,
      token_index=step,
      position=1 + step,
      lease_expires_at=lease,
   )


def test_decode_steps_renew_past_the_issued_lease_and_abandonment_expires(
   runtime_case, port_and_clock
) -> None:
   port, clock = port_and_clock
   assert port.execute(_prefill(runtime_case)).success
   for step in range(1, 8):
      clock.now += 6.0  # 7 steps over 42 s: past the issued 130.0
      result = port.execute(_decode(runtime_case, step))
      assert result.success, (step, clock.now, result.failure_reason)
   last = clock.now
   assert last > _LEASE
   clock.now = last + _WINDOW - 0.001
   assert port.expire_leases() == ()
   clock.now = last + _WINDOW
   assert port.expire_leases() == ("path-renew",)
   assert port.kv_snapshot()["release_counts"]["lease_expired"] == 1


def test_replays_refusals_and_other_paths_do_not_renew_on_mlx(
   runtime_case, port_and_clock
) -> None:
   port, clock = port_and_clock
   assert port.execute(_prefill(runtime_case)).success
   clock.now = 110.0
   step_one = _decode(runtime_case, 1)
   assert port.execute(step_one).success  # renews to 140.0
   clock.now = 139.0
   assert port.execute(step_one).success  # a replay: not progress
   mismatched = _decode(runtime_case, 2, lease=_LEASE + 500.0)
   assert port.execute(mismatched).failure_reason == "kv_lease_mismatch"
   misplaced = replace(_decode(runtime_case, 2), position=9)
   assert port.execute(misplaced).failure_reason == "kv_position_mismatch"
   assert port.execute(_prefill(runtime_case, path="path-other", lease=500.0)).success
   clock.now = 140.0
   assert "path-renew" in port.expire_leases()
   assert port.execute(_decode(runtime_case, 2)).failure_reason == "kv_lease_expired"


def test_a_batched_decode_group_renews_every_member(runtime_case, port_and_clock) -> None:
   port, clock = port_and_clock
   assert port.execute(_prefill(runtime_case, path="path-a")).success
   assert port.execute(_prefill(runtime_case, path="path-b")).success
   clock.now = 125.0
   members = (_decode(runtime_case, 1, path="path-a"), _decode(runtime_case, 1, path="path-b"))
   refs = ("member-0", "member-1")
   for item, ref in zip(members, refs):
      port.offer_batch_member(item, member_ref=ref)
   report = port.execute_batch(
      RuntimeBatchRequest(
         batch_ref="batch:kv-lease-renewal",
         phase="DECODE",
         compatibility_key=compatibility_digest(members[0].batch_key),
         member_refs=refs,
         proposed_member_count=2,
      )
   )
   assert isinstance(report, BatchExecutionReport)
   assert report.dispatch_mode == RUNTIME_BATCH_DISPATCH_MODE
   assert [execution.result.success for execution in report.executed] == [True, True]
   clock.now = 125.0 + _WINDOW - 0.001
   assert port.expire_leases() == ()
   clock.now = 125.0 + _WINDOW
   assert port.expire_leases() == ("path-a", "path-b")
