# SPDX-License-Identifier: AGPL-3.0-only
"""The node's ``a10_batch_policy`` switches the paired-benchmark mode, fail-closed.

``baseline`` caps the relay's runtime batches at one member and ``candidate``
restores the configured cap; nothing else about the node changes.  The end-to-end
cases drive the real in-process MLX stage runtime through the relay's own batch
scheduler, controller and runtime-batch seam, then read the runtime's own A10
publication through the gate's mode observer, so the gate's candidate/baseline
check sees a batch exactly when the candidate policy is in force.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from mycelium_router.contracts import RouterConfig
from mycelium_router.fakes import ManualClock
from mycelium_router.relay import RelayEngine
from mycelium_router.scheduler import HopScheduler
from physical_inference_node import (
   A10_BATCH_POLICY_RECEIPT_PROTOCOL,
   NODE_CONTROL_PROTOCOL,
   NodeCommandError,
   PhysicalNodeService,
)
from scripts.run_a10_physical_gate import mode_observer
from tests.a10_deterministic.test_a10_relay_wiring import _two_prefill_items
from test_router_mlx_runtime import _fresh_ports, runtime_case  # noqa: F401


def _relay(runtime) -> RelayEngine:
   return RelayEngine(
      node_id="node-a",
      runtime=runtime,
      transport=None,
      scheduler=HopScheduler(RouterConfig()),
      clock=ManualClock(),
      builder=None,
      device_states=None,
   )


def _service(relay, *, state="RUNNING", config=None) -> PhysicalNodeService:
   service = object.__new__(PhysicalNodeService)
   service.state = state
   service.router = None if relay is None else SimpleNamespace(relay=relay)
   service._router_config = config or RouterConfig()
   service._control_lock = threading.RLock()
   service.run_id = "run-a10"
   service.deployment_id = "deployment-a10"
   return service


def _command(payload) -> dict:
   return {"protocol": NODE_CONTROL_PROTOCOL, "command_id": "cmd-a10-policy", "run_id": "run-a10",
           "deployment_id": "deployment-a10", "command": "a10_batch_policy",
           "payload": payload}


def test_baseline_caps_runtime_batches_at_one_and_candidate_restores_the_configured_cap():
   relay = _relay(object())
   config = RouterConfig(maximum_runtime_batch_size=12)
   service = _service(relay, config=config)

   receipt = service.dispatch(_command({"policy": "baseline"}))
   assert receipt == {"protocol": A10_BATCH_POLICY_RECEIPT_PROTOCOL, "policy": "baseline",
                      "maximum_runtime_batch_size": 1, "collection_window_ms": 2.0}
   assert relay.batch_scheduler.config.maximum_runtime_batch_size == 1
   assert relay.batch_controller.config.maximum_runtime_batch_size == 1
   # Only the cap changes: every other router setting is the node's own.
   assert relay.batch_scheduler.config == RouterConfig(maximum_runtime_batch_size=1)

   receipt = service.dispatch(_command({"policy": "candidate"}))
   assert receipt["maximum_runtime_batch_size"] == 12
   assert relay.batch_scheduler.config is config
   assert relay.batch_controller.config is config


def test_candidate_with_a_window_sets_the_live_collection_window_and_echoes_it():
   relay = _relay(object())
   config = RouterConfig(maximum_runtime_batch_size=12)
   service = _service(relay, config=config)

   receipt = service.dispatch(_command({"policy": "candidate@40"}))
   assert receipt == {"protocol": A10_BATCH_POLICY_RECEIPT_PROTOCOL, "policy": "candidate@40",
                      "maximum_runtime_batch_size": 12, "collection_window_ms": 40.0}
   assert relay.batch_controller.config.live_hop_collection_window_seconds == 0.04
   assert relay.batch_scheduler.config == replace(
      config, live_hop_collection_window_seconds=0.04)
   assert service.dispatch(_command({"policy": "candidate@0"}))["collection_window_ms"] == 0.0
   assert service.dispatch(_command({"policy": "candidate@250"}))["collection_window_ms"] == 250.0
   # Plain candidate restores the node's configured window; baseline keeps it.
   assert service.dispatch(_command({"policy": "candidate"}))["collection_window_ms"] == 2.0
   assert relay.batch_controller.config is config
   service.dispatch(_command({"policy": "candidate@40"}))
   assert service.dispatch(_command({"policy": "baseline"}))["collection_window_ms"] == 2.0


@pytest.mark.parametrize(
   "policy",
   ["candidate@251", "candidate@1000", "candidate@-1", "candidate@040", "candidate@1.5",
    "candidate@", "baseline@40", "candidate@40 ", "CANDIDATE@40"],
)
def test_a_window_outside_the_bounds_or_grammar_is_refused_and_changes_nothing(policy):
   relay = _relay(object())
   before = relay.batch_controller.config
   with pytest.raises(NodeCommandError, match="invalid_a10_batch_policy"):
      _service(relay).dispatch(_command({"policy": policy}))
   assert relay.batch_controller.config is before


@pytest.mark.parametrize("state", ["CONFIGURED", "STOPPING", "STOPPED"])
def test_a_node_that_is_not_running_refuses(state):
   with pytest.raises(NodeCommandError, match="invalid_state_for_a10_batch_policy"):
      _service(_relay(object()), state=state).dispatch(_command({"policy": "baseline"}))


def test_a_running_node_without_a_router_refuses():
   with pytest.raises(NodeCommandError, match="invalid_state_for_a10_batch_policy"):
      _service(None).dispatch(_command({"policy": "baseline"}))


@pytest.mark.parametrize(
   ("payload", "code"),
   [
      ({"policy": "off"}, "invalid_a10_batch_policy"),
      ({"policy": 1}, "invalid_a10_batch_policy"),
      ({}, "invalid_a10_batch_policy_fields"),
      ({"policy": "baseline", "maximum_runtime_batch_size": 4}, "invalid_a10_batch_policy_fields"),
   ],
)
def test_a_malformed_policy_is_refused_and_changes_nothing(payload, code):
   relay = _relay(object())
   before = relay.batch_scheduler.config
   with pytest.raises(NodeCommandError, match=code):
      _service(relay).dispatch(_command(payload))
   assert relay.batch_scheduler.config is before


class _StatusClient:
   def __init__(self, publication):
      self._publication = publication

   def live_status(self):
      return {"peers": [{"a10_publication": self._publication}]}


def _serve_two_concurrent_members(case, policy):
   port = _fresh_ports(case)[0]
   relay = _relay(port)
   _service(relay).dispatch(_command({"policy": policy}))
   for item in _two_prefill_items(case):
      relay.batch_scheduler.enqueue(item)
   batch_sizes = []
   while relay.batch_scheduler.queue_depth():
      candidates = relay.batch_scheduler.batch_candidates(now=0.0)
      decision = relay.batch_controller.decide(candidates, now=0.0, force=True)
      batch = relay.batch_scheduler.pop_batch(
         now=0.0,
         maximum_items=decision.batch_size,
         maximum_bytes=relay.scheduler.config.maximum_runtime_batch_bytes,
         decision=decision,
      )
      results = relay._execute_runtime_batch(batch)
      assert all(result.success for result in results)
      batch_sizes.append(len(batch.items))
   return batch_sizes, port.a10_publication_snapshot()


def test_the_gate_sees_no_runtime_batch_under_the_baseline_policy(runtime_case):  # noqa: F811 - pytest fixture
   batch_sizes, publication = _serve_two_concurrent_members(runtime_case, "baseline")
   assert batch_sizes == [1, 1]
   observe = mode_observer(_StatusClient(publication))
   assert observe("baseline", 0.0) == "verified"
   assert observe("candidate", 0.0) == "contradicted"


def test_the_gate_sees_a_runtime_batch_under_the_candidate_policy(runtime_case):  # noqa: F811 - pytest fixture
   batch_sizes, publication = _serve_two_concurrent_members(runtime_case, "candidate")
   assert batch_sizes == [2]
   observe = mode_observer(_StatusClient(publication))
   assert observe("candidate", 0.0) == "verified"
   assert observe("baseline", 0.0) == "contradicted"
