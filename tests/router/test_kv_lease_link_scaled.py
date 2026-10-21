# SPDX-License-Identifier: AGPL-3.0-only
"""The KV reservation lease scales with the measured link (A14 serve-2 class).

Renewal on progress (``test_kv_lease_progress``) keeps a lease alive between
two applied operations of a stage.  The *issued* lease still has to cover the
time from path build to a stage's first operation, and the renewal window has
to cover the gap between two chunks.  On a 1 KB/s relay one ~33 KB prefill
chunk alone takes ~33 s to cross, so a fixed 30 s lease fails the very first
chunk (``reservation_expired`` at the final stage's manifest lock, or
``kv_lease_expired``).  Per the slow-link policy the window scales with the
measured link: ``min(ceiling, max(reservation_lease_seconds, W))`` with ``W``
the prefill no-progress watchdog of ``PrefillStallPolicy``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import physical_inference_node as node_module
from mycelium_router.contracts import RouterConfig
from mycelium_router.fakes import InMemoryClientSink
from mycelium_router.payloads import encode_token_ids
from mycelium_router.transports.iroh import DEFAULT_PREFILL_STALL_POLICY
from physical_inference_node import PhysicalNodeService
import test_kv_lease_progress as progress
from test_kv_lease_progress import (  # noqa: F401 - fixture re-export
   _failure_reasons,
   _request,
   qwen_case,
)
from test_router_numpy_runtime import _work_item

_BASE = RouterConfig().reservation_lease_seconds
#: A14 startup prompt chunk on Qwen2.5-0.5B: 8 tokens x 896 x 4 B (float32
#: activations) + envelope = 32 KiB (r1 confirmed a 32826 B frame).
_A14_CHUNK_BYTES = 8 * 896 * 4 + 4096


def _link(goodput: float, rtt: float) -> SimpleNamespace:
   return SimpleNamespace(
      goodput_bytes_per_second=goodput,
      rtt_p95_seconds=rtt,
      goodput_source="test",
      rtt_source="test",
   )


@pytest.mark.parametrize(
   ("goodput", "rtt", "minimum"),
   [
      (1024.0, 1.0, 360.0),  # 1 KB/s: S clamps at 120 s -> W >= 3 x 120
      (4096.0, 1.0, 124.0),  # unmeasured floors: S = 40 s -> W = 124 s
      (30_000.0, 0.33, 91.0),  # 30 KB/s: S = 30 s floor -> W ~= 91.3 s
   ],
)
def test_the_lease_window_scales_with_the_measured_link(goodput, rtt, minimum) -> None:
   policy = DEFAULT_PREFILL_STALL_POLICY
   window = policy.kv_lease_seconds(
      base_seconds=_BASE, chunk_bytes=_A14_CHUNK_BYTES, link=_link(goodput, rtt)
   )
   assert window >= minimum
   assert window >= _BASE
   assert window >= policy.watchdog_seconds(_A14_CHUNK_BYTES, _link(goodput, rtt)) or (
      window == policy.kv_lease_ceiling_seconds(_BASE)
   )
   assert window <= policy.kv_lease_ceiling_seconds(_BASE) <= 420.0


def test_the_lease_window_is_bounded_even_for_a_pathological_link() -> None:
   policy = DEFAULT_PREFILL_STALL_POLICY
   window = policy.kv_lease_seconds(
      base_seconds=_BASE, chunk_bytes=_A14_CHUNK_BYTES, link=_link(1.0, 600.0)
   )
   assert window == policy.kv_lease_ceiling_seconds(_BASE)


# -- end to end: a 1 KB/s relay, real Router + NumPy runtimes -------------------


def _one_kbps_route(qwen_case, monkeypatch, *, scaled: bool):
   monkeypatch.setattr(progress, "_HOP_SECONDS", 33.0)
   window = (
      DEFAULT_PREFILL_STALL_POLICY.kv_lease_seconds(
         base_seconds=_BASE, chunk_bytes=33 * 1024, link=_link(1024.0, 1.0)
      )
      if scaled
      else None
   )
   return progress._route(qwen_case, window=window)


def test_a_fixed_lease_cannot_carry_a_one_kilobyte_per_second_first_chunk(
   qwen_case, monkeypatch
) -> None:
   """Control: without link scaling the first 33 s crossing already outlives 30 s."""

   mesh, clock, routers, _ = _one_kbps_route(qwen_case, monkeypatch, scaled=False)
   request = _request("request-1kbps-fixed")
   routers["node-a"].start_distributed_prefill(request, InMemoryClientSink())
   assert routers["node-a"].request_status(request.request_id) != "DECODING"
   reasons = {reason for _, reason, _ in _failure_reasons(mesh)}
   assert reasons & {"kv_lease_expired", "manifest_lock_failed:reservation_expired"}


def test_a_one_kilobyte_per_second_prefill_completes_with_link_scaled_leases(
   qwen_case, monkeypatch
) -> None:
   mesh, clock, routers, ports = _one_kbps_route(qwen_case, monkeypatch, scaled=True)
   entry = routers["node-a"]
   request = _request("request-1kbps-scaled")
   sink = InMemoryClientSink()
   entry.start_distributed_prefill(request, sink)
   assert _failure_reasons(mesh) == []
   assert entry.request_status(request.request_id) == "DECODING"
   assert clock.now() > 10 * 33.0
   while entry.request_status(request.request_id) == "DECODING":
      assert entry.decode_one_distributed(request.request_id)
   assert entry.request_status(request.request_id) == "COMPLETED"
   assert len(sink.token_ids) == progress._MAX_NEW_TOKENS


# -- the physical node composes the window from its transport ----------------------


class _LinkTransport:
   stall_policy = DEFAULT_PREFILL_STALL_POLICY

   def __init__(self, goodput: float, rtt: float) -> None:
      self.link = _link(goodput, rtt)

   def link_estimate(self, destination_node_id=None):
      del destination_node_id
      return self.link


def _bare_service(graph: Any, transport: Any) -> PhysicalNodeService:
   service = object.__new__(PhysicalNodeService)
   service._router_config = RouterConfig(prefill_chunk_size_tokens=8)
   service.graph = graph
   service.transport = transport
   return service


def test_the_node_window_follows_its_transport_link() -> None:
   graph = SimpleNamespace(hidden_size=896, activation_bytes=4)
   slow = _bare_service(graph, _LinkTransport(1024.0, 1.0))._kv_lease_window_seconds()
   fast = _bare_service(graph, _LinkTransport(30_000.0, 0.33))._kv_lease_window_seconds()
   unmeasured = _bare_service(graph, None)._kv_lease_window_seconds()
   assert slow >= 360.0
   assert 91.0 <= fast < 95.0
   assert unmeasured == pytest.approx(124.0)
   assert slow <= DEFAULT_PREFILL_STALL_POLICY.kv_lease_ceiling_seconds(_BASE)


def test_the_node_runtime_renews_by_its_link_window(qwen_case) -> None:
   """``_build_runtime_port`` wires the node's window into the stage runtime."""

   class _Clock:
      def __init__(self) -> None:
         self.value = 1_000.0

      def now(self) -> float:
         return self.value

   service = _bare_service(qwen_case.graph, _LinkTransport(1024.0, 1.0))
   service._clock = _Clock()
   service.node_id = "node-a"
   service.requested_decode_mode = "stage_local_kv"
   placement = qwen_case.graph.stages[0].placements[0]
   port = service._build_runtime_port(placement, qwen_case.graph, qwen_case.loaded[0])
   window = service._kv_lease_window_seconds()
   # The tiny test model's chunks are small: S sits at its 30 s floor, W = 3 x 30 + 4.
   assert window == pytest.approx(94.0)
   lease = 1_030.0
   for index in range(2):
      item = _work_item(
         qwen_case,
         0,
         encode_token_ids((1, 2)),
         phase="PREFILL_CHUNK",
         token_span=2,
         request_id="request:node-window",
         path_id="path:node-window",
         token_index=index,
         position=2 * index,
         lease_expires_at=lease,
      )
      assert port.execute(item).success
   service._clock.value += window - 1.0
   assert port.expire_leases() == ()
   service._clock.value += 1.0
   assert port.expire_leases() == ("path:node-window",)


def test_the_started_node_issues_leases_with_its_link_window(
   tmp_path: Path, monkeypatch
) -> None:
   captured: dict[str, Any] = {}

   class Sidecar:
      socket_path = tmp_path / "sidecar.sock"
      bootstrap_material = b"s" * 32

      def close(self) -> None:
         return

   class Runtime:
      def close(self, *, reason: str) -> None:
         del reason

   class Transport(_LinkTransport):
      def __init__(self, **kwargs: Any) -> None:
         super().__init__(1024.0, 1.0)

      def bind_router(self, router: Any) -> None:
         return

      def start(self) -> None:
         return

      def close(self) -> None:
         return

   class FakeRouter:
      def __init__(self, **kwargs: Any) -> None:
         captured.update(kwargs)

   monkeypatch.setattr(node_module, "IrohTransport", Transport)
   monkeypatch.setattr(node_module, "Router", FakeRouter)
   service = PhysicalNodeService(
      run_id="run-kv-lease",
      deployment_id="deployment-kv-lease",
      node_id="node-a",
      artifact_root=tmp_path,
      socket_root=tmp_path / "socket",
      sidecar_binary=Path("/usr/bin/false"),
      sidecar_local_only=True,
      command_timeout=270.0,
   )
   service.state = "CONFIGURED"
   service.sidecar = Sidecar()  # type: ignore[assignment]
   service.endpoint_id = "endpoint-a"
   service.runtime = Runtime()
   service.topology = object()  # type: ignore[assignment]
   service.device_states = object()  # type: ignore[assignment]
   service.capacity = object()  # type: ignore[assignment]
   service.graph = SimpleNamespace(hidden_size=896, activation_bytes=4)  # type: ignore[assignment]
   service.signer = node_module.generate_ed25519_signer(endpoint_id="endpoint-a")
   try:
      service._start(
         {
            "peer": {
               "node_id": "node-b",
               "endpoint_id": "endpoint-b",
               "endpoint_addr": {"id": "endpoint-b", "addrs": []},
               "generation": 1,
            },
            "local_generation": 1,
         }
      )
      provider = captured["kv_lease_seconds"]
      assert provider() == pytest.approx(service._kv_lease_window_seconds())
      assert provider() >= 360.0
   finally:
      service.graph = None
      service.close()


def test_a_host_accepts_a_peer_issued_scaled_lease_but_bounds_it() -> None:
   bound = node_module._maximum_imported_lease_seconds(RouterConfig())
   assert bound >= DEFAULT_PREFILL_STALL_POLICY.kv_lease_ceiling_seconds(_BASE)
   assert bound <= DEFAULT_PREFILL_STALL_POLICY.kv_lease_ceiling_seconds(_BASE) + (
      node_module.MAXIMUM_PHYSICAL_CLOCK_SKEW_SECONDS
   )
