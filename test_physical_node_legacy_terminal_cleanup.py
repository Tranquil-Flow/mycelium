# SPDX-License-Identifier: AGPL-3.0-only
"""The legacy terminal cleanup wait scales with the node's command budget.

The startup qualifier's legacy path waits for the entry-authoritative
cancellation receipt of a remote hop before acknowledging its terminal.  A
fixed two-second wait failed startup qualification whenever one cross-host
receipt took longer (a degraded link with a multi-second round trip), so the
wait is derived from the command budget, bounded, and still fails closed.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import physical_inference_node
from physical_inference_node import NodeCommandError, PhysicalNodeService


class _Clock:
   def __init__(self) -> None:
      self.now = 0.0

   def monotonic(self) -> float:
      return self.now

   def sleep(self, seconds: float) -> None:
      self.now += seconds


class _Transport:
   fatal_error = None

   def __init__(self, clock: _Clock, complete_after_s: float | None) -> None:
      self._clock = clock
      self._complete_after_s = complete_after_s
      self.cancellations = []

   def send_path_cancellation_if_entry(self, cancellation) -> None:
      self.cancellations.append(cancellation)

   def cancellation_cleanup_complete(self, request_id, path_id, path_attempt) -> bool:
      return self._complete_after_s is not None and self._clock.now >= self._complete_after_s


class _Router:
   def get_request(self, request_id):
      return SimpleNamespace(
         status="COMPLETED",
         manifest=SimpleNamespace(path_id="path-legacy", path_attempt=0, topology_version=1),
      )


def _service(transport: _Transport, command_timeout: float) -> PhysicalNodeService:
   service = object.__new__(PhysicalNodeService)
   service.router = _Router()
   service.transport = transport
   service.command_timeout = command_timeout
   return service


@pytest.fixture
def clock(monkeypatch) -> _Clock:
   clock = _Clock()
   monkeypatch.setattr(physical_inference_node, "time", SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep))
   return clock


def test_a_slow_cross_host_receipt_inside_the_command_budget_is_waited_for(clock):
   transport = _Transport(clock, complete_after_s=2.7)
   _service(transport, command_timeout=30.0)._retire_legacy_terminal_path("request-legacy")
   assert len(transport.cancellations) == 1
   assert 2.7 <= clock.now < 3.0


@pytest.mark.parametrize(("command_timeout", "budget"), [(30.0, 10.0), (5.0, 4.0), (1.0, 0.8)])
def test_a_receipt_that_never_arrives_still_fails_closed_within_the_bound(clock, command_timeout, budget):
   transport = _Transport(clock, complete_after_s=None)
   with pytest.raises(NodeCommandError) as raised:
      _service(transport, command_timeout=command_timeout)._retire_legacy_terminal_path("request-legacy")
   assert raised.value.code == "legacy_terminal_cleanup_timeout"
   assert budget <= clock.now < budget + 0.05
