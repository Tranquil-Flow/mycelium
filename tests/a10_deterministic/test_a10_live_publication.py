# SPDX-License-Identifier: AGPL-3.0-only
"""A10 live publication: runtime-owned batch observations and stage-compute traces.

The MLX and NumPy stage runtimes record one privacy-reduced stage-compute interval
for every operation they actually execute, and the node co-signs the bounded
publication inside its ordinary snapshot.  ``build_overlap_trace`` assembles the
route nodes' publications into the causal trace ``observe_stage_compute_overlap``
judges.  Runtime intervals here come from real stage execution on the tiny offline
MLX fixture; the overlap-verdict cases drive the recorder with explicit intervals so
the arithmetic is exact.
"""

from __future__ import annotations

from tests.a10_deterministic import ensure_worktree_imports

ensure_worktree_imports()

import json
from types import SimpleNamespace

import pytest

import physical_inference_node as node_module
from mycelium_router.a10_live_publication import (
   MAXIMUM_INTERVALS,
   A10PublicationError,
   StageComputePublication,
   build_overlap_trace,
   pseudonym,
   validate_publication,
)
from mycelium_router.a10_overlap_trace import observe_stage_compute_overlap
from mycelium_router.contracts import RuntimeResult
from mycelium_router.payloads import encode_token_ids
from test_router_mlx_runtime import _work_item
from tests.a10_deterministic.test_runtime_target_verifier import mlx_case  # noqa: F401


def _run_request(case, request_id, tokens):
   payload = encode_token_ids(tokens)
   results = []
   for stage_index, port in enumerate(case.ports):
      result = port.execute(
         _work_item(
            case,
            stage_index,
            payload,
            phase="PREFILL",
            token_span=len(tokens),
            request_id=request_id,
            path_id=f"path:{request_id}",
            emits_token=True,
         )
      )
      assert result.success, result.failure_reason
      results.append(result)
      if result.payload is not None:
         payload = result.payload
   return results


def test_runtimes_publish_privacy_reduced_stage_compute_intervals(mlx_case):
   _run_request(mlx_case, "request-publication-a", (1, 2, 3))
   entry, final = (port.a10_publication_snapshot() for port in mlx_case.ports)

   assert validate_publication(entry) == entry
   assert entry["route_path_ref"] == final["route_path_ref"]
   assert entry["stage_count"] == 2
   microbatch = pseudonym("microbatch", ["request-publication-a", 0, "PREFILL", -1])
   entry_hop = next(i for i in entry["intervals"] if i["microbatch_ref"] == microbatch)
   final_hop = next(
      i
      for i in final["intervals"]
      if i["microbatch_ref"] == microbatch and i["hop_kind"] == "compute"
   )
   assert entry_hop["stage_index"] == 0 and final_hop["stage_index"] == 1
   # The activation chain is bound by digest; token payloads and outputs are not.
   assert entry_hop["input_digest"] == ""
   assert entry_hop["output_digest"] == final_hop["input_digest"] != ""
   assert final_hop["output_digest"] == ""
   assert entry_hop["finished_at"] >= entry_hop["started_at"]
   commit = [
      i for i in final["intervals"] if i["microbatch_ref"] == microbatch and i["hop_kind"] == "commit"
   ]
   assert len(commit) == 1 and commit[0]["stage_index"] == -1
   encoded = json.dumps([entry, final])
   for private in ("request-publication-a", "path:request-publication-a", "node-a", "node-b"):
      assert private not in encoded


def test_failed_operations_publish_no_interval(mlx_case):
   port = mlx_case.ports[0]
   before = len(port.a10_publication_snapshot()["intervals"])
   result = port.execute(
      _work_item(mlx_case, 0, b"not-a-token-payload", request_id="request-bad")
   )
   assert result.success is False
   assert len(port.a10_publication_snapshot()["intervals"]) == before


def test_live_trace_from_real_runtime_publications_is_causally_valid(mlx_case):
   _run_request(mlx_case, "request-trace-a", (1, 2))
   _run_request(mlx_case, "request-trace-b", (3, 4))
   trace = build_overlap_trace(
      [port.a10_publication_snapshot() for port in mlx_case.ports],
      cross_host_clock_uncertainty=0.0,
   )
   verdict = observe_stage_compute_overlap(trace)
   # Serially executed requests are valid causal traces without overlap.
   assert verdict.observed is False


def _item(request_id, placement_id, *, token_index=0, emits_token=False, payload=b"\x01"):
   return SimpleNamespace(
      request_id=request_id,
      path_attempt=0,
      phase="DECODE",
      token_index=token_index,
      placement_id=placement_id,
      emits_token=emits_token,
      payload=payload,
   )


def _publications(case, spans, *, wall=1_000.0):
   """Record explicit (request, stage, start, finish) spans on two recorders."""

   clocks = [SimpleNamespace(now=0.0) for _ in case.ports]
   recorders = [
      StageComputePublication(
         node_id=f"node-{index}",
         graph=case.graph,
         clock=lambda index=index: clocks[index].now,
         wall_clock=lambda: wall,
      )
      for index in range(len(case.ports))
   ]
   placements = [stage.placements[0].placement_id for stage in case.graph.stages]
   for request_id, stage_index, started, finished in spans:
      activation = f"{request_id}-activation".encode()
      final = stage_index == len(placements) - 1
      recorders[stage_index].record(
         _item(
            request_id,
            placements[stage_index],
            emits_token=final,
            payload=b"\x01" if stage_index == 0 else activation,
         ),
         started_at=started,
         finished_at=finished,
         result=RuntimeResult(
            success=True,
            payload=None if final else activation,
            token_id=1 if final else None,
         ),
      )
   return [recorder.snapshot() for recorder in recorders]


def test_pipeline_overlap_is_observed_only_after_uncertainty(mlx_case):
   spans = [
      ("request-a", 0, 0.00, 0.10),
      ("request-a", 1, 0.10, 0.30),
      ("request-b", 0, 0.12, 0.28),
      ("request-b", 1, 0.30, 0.40),
   ]
   publications = _publications(mlx_case, spans)

   observed = observe_stage_compute_overlap(
      build_overlap_trace(publications, cross_host_clock_uncertainty=0.01)
   )
   assert observed.observed is True
   assert observed.intersection_seconds == pytest.approx(0.16 - 0.02, abs=1e-6)

   masked = observe_stage_compute_overlap(
      build_overlap_trace(publications, cross_host_clock_uncertainty=0.1)
   )
   assert masked.observed is False


def test_incomplete_microbatches_are_dropped_not_read_as_skipped_stages(mlx_case):
   spans = [
      ("request-a", 0, 0.00, 0.10),
      ("request-a", 1, 0.10, 0.20),
      ("request-late", 0, 0.15, 0.25),
   ]
   trace = build_overlap_trace(_publications(mlx_case, spans), cross_host_clock_uncertainty=0.0)
   assert {hop.request_ref for hop in trace.hops} == {pseudonym("request", "request-a")}


def test_publication_rejects_tampering_and_mixed_routes(mlx_case):
   publications = _publications(mlx_case, [("request-a", 0, 0.0, 0.1), ("request-a", 1, 0.1, 0.2)])
   tampered = json.loads(json.dumps(publications[0]))
   tampered["intervals"][0]["request_ref"] = "request-a"
   with pytest.raises(A10PublicationError):
      validate_publication(tampered)
   extra = json.loads(json.dumps(publications[0]))
   extra["prompt"] = "hello"
   with pytest.raises(A10PublicationError):
      validate_publication(extra)
   reversed_interval = json.loads(json.dumps(publications[0]))
   reversed_interval["intervals"][0]["finished_at"] = -1.0
   with pytest.raises(A10PublicationError):
      validate_publication(reversed_interval)
   other_route = json.loads(json.dumps(publications[1]))
   other_route["route_path_ref"] = "sha256:" + "0" * 64
   with pytest.raises(A10PublicationError) as refused:
      build_overlap_trace([publications[0], other_route], cross_host_clock_uncertainty=0.0)
   assert refused.value.code == "path_or_attempt_mismatch"
   with pytest.raises(A10PublicationError) as refused:
      build_overlap_trace([], cross_host_clock_uncertainty=0.0)
   assert refused.value.code == "a10_publication_absent"


def test_publication_is_bounded_and_counts_what_it_dropped(mlx_case):
   placement = mlx_case.graph.stages[0].placements[0].placement_id
   recorder = StageComputePublication(node_id="node-0", graph=mlx_case.graph)
   for index in range(MAXIMUM_INTERVALS + 44):
      recorder.record(
         _item(f"request-{index}", placement),
         started_at=float(index),
         finished_at=float(index) + 0.5,
         result=RuntimeResult(success=True, payload=b"\x02"),
      )
   snapshot = recorder.snapshot()
   assert len(snapshot["intervals"]) == MAXIMUM_INTERVALS
   assert snapshot["dropped_interval_count"] == 44


def test_node_snapshot_co_signs_the_runtime_publication(mlx_case):
   service = object.__new__(node_module.PhysicalNodeService)
   port = mlx_case.ports[0]
   service.state = "RUNNING"
   service.runtime = port
   service.transport = None
   service.sidecar = None
   service.capacity = None
   service._last_cancellation = None
   service._host_resources = lambda: {}
   service._signed_result = lambda event, details: {"event": event, "details": details}

   signed = service._snapshot({})

   assert signed["event"] == "snapshot"
   publication = signed["details"]["a10_publication"]
   assert validate_publication(publication) == publication
   assert publication["route_path_ref"] == port.a10_publication_snapshot()["route_path_ref"]


def test_receipt_only_snapshot_never_carries_the_publication(mlx_case):
   """A sealed cleanup receipt is request-scoped: the live publication is not in it.

   The route keeps receipt-only observations out of its live-status projection, so
   the publication travels only with full snapshots; a receipt must not grow it.
   """

   import threading

   subject = {
      "deployment_id": "deployment-a10",
      "deployment_epoch": 1,
      "qualification_digest": "sha256:" + "1" * 64,
      "request_id": "request-receipt",
      "request_attempt": 1,
      "path_id": "path-receipt",
      "path_attempt": 0,
      "path_digest": "sha256:" + "2" * 64,
      "topology_generation": 1,
      "command_id": "command-receipt",
      "cancellation_generation": 0,
      "publisher_generation": 1,
      "absolute_deadline_ms": 1_000,
   }
   service = object.__new__(node_module.PhysicalNodeService)
   service.state = "RUNNING"
   service.runtime = mlx_case.ports[0]
   service.transport = None
   service._control_lock = threading.Lock()
   service._request_cleanup_receipts = {"request-receipt": dict(subject)}
   service._signed_result = lambda event, details: {"event": event, "details": details}

   receipt = service._snapshot({"cleanup_subject": subject, "receipt_only": True})

   assert receipt["details"]["request_cleanup"] == subject
   assert "a10_publication" not in receipt["details"]
